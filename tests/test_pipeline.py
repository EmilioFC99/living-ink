"""Tests for living_ink.pipeline module and SyncPipeline class."""

import datetime
import json
import logging
import os
import sqlite3
import stat
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from living_ink import logs, pipeline, state
from living_ink.config import ConfigurationMissing, credentials
from living_ink.core import selection
from living_ink.core.document import Document, PublishContext, PublishResult
from living_ink.core.listing import (
    document_id,
    document_name,
    document_version,
    get_notebook_path,
)
from living_ink.core.selection import Candidate, Selection, SelectionCriteria, select
from living_ink.core.temp import DocumentWorkspace
from living_ink.destinations import (
    Destination,
    DestinationError,
    DestinationStatus,
    MergeUnit,
    ObsidianDestination,
)
from living_ink.pipeline import (
    DocumentJob,
    SyncPipeline,
    log,
)
from living_ink.redact import clear_secrets, register_secret
from living_ink.report import (
    DEFERRED,
    FAILED,
    PUBLISHED,
    SKIPPED,
    WOULD_PUBLISH,
    DocumentOutcome,
    RunReport,
)
from living_ink.settings import Settings
from tests.builders import make_page
from tests.fixtures.listing import make_item


def make_candidate(item, *, pending=(), source="notebook", id_map=None, recipes=None):
    """Build the selection's verdict about one document.

    The stages take a :class:`Candidate` now, so a test that exercises one
    starts where the selection pass left off rather than re-deriving the facts.

    Args:
        item: The raw listing entry.
        pending: The destinations that owe it a publish.
        source: Its registered source name.
        id_map: Every listed item by id, for the folder path.
        recipes: Recipe digests by destination state key.

    Returns:
        The candidate.
    """
    id_map = id_map if id_map is not None else {document_id(item): item}
    return Candidate(
        item=item,
        doc_id=document_id(item),
        name=document_name(item),
        folder=get_notebook_path(item, id_map),
        source=source,
        version=document_version(item),
        pending=tuple(pending),
        recipes=dict(recipes or {}),
    )


class MockDestination(Destination):
    """Mock destination for testing."""

    state_key = "MockDestination"
    display_name = "MockDestination"

    def __init__(self, name: str = "Mock"):
        self.name = name
        self.published = []
        self.unpublished = []
        self.unpublish_result = True
        self.unpublish_error = None
        self.publish_target = None
        self.publish_warnings = ()
        self.publish_ok = True
        self.ready = True
        self.failure_reports = []
        self.failures_cleared = 0

    def report_failure(self, summary: str) -> None:
        self.failure_reports.append(summary)

    def clear_failure(self) -> None:
        self.failures_cleared += 1

    def check(self) -> DestinationStatus:
        return DestinationStatus(ok=self.ready, detail="mock")

    def unpublish(self, ctx: PublishContext) -> PublishResult:
        if self.unpublish_error:
            raise self.unpublish_error
        self.unpublished.append((ctx.existing_target, ctx.existing_external_id, ctx.doc_id))
        return PublishResult(ok=self.unpublish_result, target=ctx.existing_target)

    def publish(self, doc: Document, ctx: PublishContext) -> PublishResult:
        # Both objects are kept whole rather than unpacked field by field, so a
        # new field on either does not need this double edited.
        self.published.append({"doc": doc, "ctx": ctx})
        return PublishResult(
            ok=self.publish_ok, target=self.publish_target, warnings=self.publish_warnings
        )


def test_sync_pipeline_init_defaults():
    """SyncPipeline initializes with standard configuration and data paths."""
    pipeline = SyncPipeline(keep_temp=True)
    assert pipeline.keep_temp is True
    assert pipeline.config_path.name == "config.yml"


def test_sync_pipeline_custom_destinations():
    """SyncPipeline accepts custom destination instances."""
    mock_dest = MockDestination("Custom")
    pipeline = SyncPipeline(destinations=[mock_dest])
    assert len(pipeline.destinations) == 1
    assert pipeline.destinations[0] is mock_dest


def test_sync_pipeline_properties_ssh_and_cloud():
    """SyncPipeline sets connection properties and synchronizes environment."""
    pipeline_ssh = SyncPipeline(ssh=True)
    assert pipeline_ssh.preferred_connection == "ssh"
    assert pipeline_ssh.use_ssh is True

    pipeline_cloud = SyncPipeline(cloud=True)
    assert pipeline_cloud.preferred_connection == "cloud"
    assert pipeline_cloud.use_ssh is False


def test_the_configured_types_become_selection_criteria():
    """One setting narrows the run by naming source types, nothing more."""
    default = SyncPipeline(destinations=[])
    assert default._criteria().types == frozenset({"notebook"})

    chosen = SyncPipeline(flags={"sync_types": ("pdf", "epub")}, destinations=[])
    assert chosen._criteria().types == frozenset({"pdf", "epub"})


def test_the_per_run_cap_becomes_the_selection_limit():
    """`--limit` narrows a sweep, and says so in the criteria rather than later."""
    pipe = SyncPipeline(limit=2, destinations=[MockDestination()])
    assert pipe._criteria().limit == 2


def test_a_named_notebook_is_neither_capped_nor_second_guessed():
    """Naming one document is not a sweep, so the sweep's cap does not apply."""
    criteria = SyncPipeline(notebook="Notes", limit=1, destinations=[])._criteria()

    assert criteria.target == "Notes"
    assert criteria.limit is None
    assert criteria.force is True


def test_sync_pipeline_run_no_notebooks():
    """SyncPipeline.run returns True gracefully when no items need updating."""
    pipeline = SyncPipeline(destinations=[MockDestination()])
    with patch("living_ink.pipeline.validate_environment"):
        with patch.object(pipeline, "connect") as mock_connect:
            mock_client = MagicMock()
            mock_client.get_meta_items.return_value = []
            mock_connect.return_value = mock_client
            result = pipeline.run()
            assert result is True


def test_sync_pipeline_run_targeted_not_found():
    """SyncPipeline.run returns False when a targeted notebook is not in the library."""
    pipeline = SyncPipeline(notebook="NonExistentBook", destinations=[MockDestination()])
    with patch("living_ink.pipeline.validate_environment"):
        with patch.object(pipeline, "connect") as mock_connect:
            mock_client = MagicMock()
            mock_client.get_meta_items.return_value = []
            mock_connect.return_value = mock_client
            result = pipeline.run()
            assert result is False


def test_sync_pipeline_run_targeted_ambiguous_syncs_every_match():
    """``--notebook`` matching several documents processes all of them.

    It used to stop and offer a menu, which product §7 forbids a bare ``sync``
    from doing — and which only ever appeared at a tty, so ``watch``, cron and
    a piped run already behaved this way.
    """
    matches = [
        make_item("doc-123", "Meeting Notes", content_hash="h1"),
        make_item("doc-456", "Meeting Notes", content_hash="h2"),
    ]
    pipeline = SyncPipeline(notebook="Meeting Notes", destinations=[MockDestination()])

    with patch("living_ink.pipeline.validate_environment"):
        with patch.object(pipeline, "connect") as mock_connect:
            mock_client = MagicMock()
            mock_client.get_meta_items.return_value = matches
            mock_connect.return_value = mock_client
            with patch.object(pipeline, "process_notebook_item", return_value=True) as processed:
                assert pipeline.run() is True

    synced = {call.kwargs["candidate"].item.id for call in processed.call_args_list}
    assert synced == {"doc-123", "doc-456"}


def test_sync_pipeline_process_notebook_item():
    """process_notebook_item processes empty notebook without error."""
    mock_dest = MockDestination("MockDest")
    pipeline = SyncPipeline(destinations=[mock_dest])

    nb_item = make_item("nb-001", "Test Notebook", content_hash="hash-abc")
    mock_client = MagicMock()
    mock_client.download.return_value = b""

    success = pipeline.process_notebook_item(
        candidate=make_candidate(nb_item, pending=(mock_dest,)),
        client=mock_client,
        keep_temp=True,
    )
    assert success is False


class TestImportPurity:
    """Importing the pipeline module must have no observable side effects."""

    def _import_in_subprocess(self, tmp_path):
        """Import the module in a clean interpreter and return its output."""
        env = {**os.environ, "LIVING_INK_DATA_DIR": str(tmp_path / "data")}
        return subprocess.run(
            [sys.executable, "-c", "import living_ink.pipeline"],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

    def test_import_creates_no_directories(self, tmp_path):
        """A bare import leaves LIVING_INK_DATA_DIR untouched."""
        self._import_in_subprocess(tmp_path)
        assert not (tmp_path / "data").exists()

    def test_import_prints_nothing(self, tmp_path):
        """A bare import does not build destinations, so it stays quiet."""
        result = self._import_in_subprocess(tmp_path)
        assert result.stdout == ""

    def test_import_does_not_reconfigure_a_third_party_logger(self, tmp_path):
        """The old suppression block raised rmscene to ERROR at import time.

        Two things were wrong with it and only the second was cosmetic: it
        threw away the only signal that a page parsed incompletely, and it did
        so to any application that merely imported Living Ink.
        """
        env = {**os.environ, "LIVING_INK_DATA_DIR": str(tmp_path / "data")}
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import logging, living_ink.pipeline;"
                "print(logging.getLogger('rmscene').level,"
                "logging.getLogger('rmscene').handlers)",
            ],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == "0 []"

    def test_default_config_is_loaded_once(self, monkeypatch):
        """get_default_config caches, so config is read a single time."""
        monkeypatch.setattr(pipeline, "_default_config", None)
        calls = []

        def fake_load(path=None):
            calls.append(path)
            return {"ai": {"provider": "none"}}

        monkeypatch.setattr(pipeline, "load_yaml_config", fake_load)

        assert pipeline.get_default_config() == {"ai": {"provider": "none"}}
        assert pipeline.get_default_config() == {"ai": {"provider": "none"}}
        assert len(calls) == 1

    def test_ensure_runtime_dirs_creates_them(self, monkeypatch, tmp_path):
        """ensure_runtime_dirs creates every runtime folder on demand."""
        monkeypatch.setattr(pipeline, "_runtime_dirs_ready", False)
        monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path / "data")
        monkeypatch.setattr(pipeline, "WORK_DIR", tmp_path / "data" / "work")
        monkeypatch.setattr(pipeline, "LOGS_DIR", tmp_path / "logs")

        pipeline.ensure_runtime_dirs()

        assert (tmp_path / "data").is_dir()
        assert (tmp_path / "data" / "work").is_dir()
        assert (tmp_path / "logs").is_dir()


class TestTheProcessCachesAreDroppable:
    """`watch` ticks in one process, so the accessors need an off switch."""

    def test_reset_caches_clears_all_three(self, monkeypatch):
        """Config, destinations and the state store all go together."""
        closed = []
        monkeypatch.setattr(pipeline, "_default_config", {"ai": {"provider": "none"}})
        monkeypatch.setattr(pipeline, "_default_destinations", [MockDestination()])
        monkeypatch.setattr(
            pipeline, "_state_store", SimpleNamespace(close=lambda: closed.append(True))
        )

        pipeline.reset_caches()

        assert pipeline._default_config is None
        assert pipeline._default_destinations is None
        assert pipeline._state_store is None
        assert closed == [True]

    def test_the_next_tick_gets_freshly_built_destinations(self, monkeypatch):
        """Not the same mutable objects the last tick may have written to."""
        monkeypatch.setattr(pipeline, "_default_config", {"ai": {"provider": "none"}})
        monkeypatch.setattr(pipeline, "_default_destinations", None)
        monkeypatch.setattr(
            pipeline, "get_destinations_from_config", lambda config: [MockDestination()]
        )

        first = pipeline.get_default_destinations()
        assert pipeline.get_default_destinations() is first

        monkeypatch.setattr(pipeline, "_state_store", None)
        pipeline.reset_caches()
        monkeypatch.setattr(pipeline, "_default_config", {"ai": {"provider": "none"}})

        assert pipeline.get_default_destinations() is not first


class TestTheExitHandlerIsRegisteredOnce:
    """One handler per process, not one per run — `watch` runs for weeks."""

    def test_repeated_runs_register_a_single_handler(self, monkeypatch):
        registered = []
        monkeypatch.setattr(pipeline, "_temp_cleanup_registered", False)
        monkeypatch.setattr(pipeline.atexit, "register", lambda fn: registered.append(fn))

        pipeline.register_temp_cleanup(keep_temp=False)
        pipeline.register_temp_cleanup(keep_temp=False)
        pipeline.register_temp_cleanup(keep_temp=False)

        assert len(registered) == 1

    def test_the_handler_obeys_the_last_run_to_ask(self, monkeypatch):
        """The flag is read at exit, not bound at registration."""
        registered = []
        kept = []
        monkeypatch.setattr(pipeline, "_temp_cleanup_registered", False)
        monkeypatch.setattr(pipeline.atexit, "register", lambda fn: registered.append(fn))
        monkeypatch.setattr(
            pipeline, "cleanup_temp_artifacts", lambda keep_temp=False: kept.append(keep_temp)
        )

        pipeline.register_temp_cleanup(keep_temp=False)
        pipeline.register_temp_cleanup(keep_temp=True)
        registered[0]()

        assert kept == [True]


def make_job(**overrides) -> DocumentJob:
    """Build a DocumentJob with harmless defaults for the field under test."""
    fields = {
        "item": {"ID": "nb-1"},
        "notebook": "Test Notebook",
        "notebook_id": "nb-1",
        "doc_type": "notebook",
        "version": "hash-1",
        "workspace": DocumentWorkspace(pipeline.WORK_DIR, "nb-1"),
        "folder_path": "",
        "display_title": "Test Notebook",
        "keep_temp": True,
    }
    fields.update(overrides)
    return DocumentJob(**fields)


class TestDocumentJob:
    """The job carries the per-document state the stages share."""

    def test_page_number_comes_from_the_image_name(self):
        job = make_job(imgs=[Path("nb.page-4.png"), Path("nb.page-9.png")])

        assert job.page_number(0) == 4
        assert job.page_number(1) == 9

    def test_page_number_falls_back_to_position(self):
        assert make_job(imgs=[Path("nb.cover.png")]).page_number(0) == 1
        assert make_job().page_number(2) == 3

    def test_source_file_is_none_unless_the_document_was_retrieved(self, tmp_path):
        assert make_job().source_file() is None
        assert make_job(doc_file_path=tmp_path / "missing.pdf").source_file() is None

        present = tmp_path / "book.pdf"
        present.write_bytes(b"%PDF")
        assert make_job(doc_file_path=present).source_file() == present

    def test_the_folder_splits_into_its_parts(self):
        job = make_job(folder_path="Work / Projects / Q3")

        assert job.folder_parts() == ("Work", "Projects", "Q3")

    def test_the_library_root_has_no_parts(self):
        assert make_job().folder_parts() == ()


class TestPagesAreDescribedAtRenderTime:
    """Everything needed to place a page is recorded once, when it is rendered.

    It used to be recovered at publish time by regexing the PNG filename and
    reopening the source PDF — once per page, from inside the destination.
    """

    def _pipeline(self):
        return SyncPipeline(destinations=[MockDestination()])

    def test_a_page_is_described_for_every_image(self):
        job = make_job(imgs=[Path("nb.page-1.png"), Path("nb.page-2.png")])
        self._pipeline()._describe_pages(job)

        assert [p.index for p in job.pages] == [0, 1]
        assert [p.image_path for p in job.pages] == job.imgs

    def test_the_number_is_the_documents_own_not_the_position(self):
        """A 400-page PDF with two annotated pages yields 12 and 377."""
        job = make_job(imgs=[Path("nb.page-12.png"), Path("nb.page-377.png")])
        self._pipeline()._describe_pages(job)

        assert [p.number for p in job.pages] == [12, 377]

    def test_a_notebook_page_is_labelled_by_its_number(self):
        job = make_job(imgs=[Path("nb.page-3.png")])
        self._pipeline()._describe_pages(job)

        assert job.pages[0].label == "Page 3"

    def test_a_notebook_has_no_breadcrumbs(self):
        """An empty tuple, so a writer renders nothing rather than a bare separator."""
        job = make_job(imgs=[Path("nb.page-1.png")])
        self._pipeline()._describe_pages(job)

        assert job.pages[0].breadcrumbs == ()

    def test_the_source_digest_is_carried_when_the_page_was_rendered_this_run(self):
        job = make_job(
            imgs=[Path("nb.page-1.png"), Path("nb.page-2.png")],
            source_hashes=["aaa", "bbb"],
        )
        self._pipeline()._describe_pages(job)

        assert [p.source_key for p in job.pages] == ["aaa", "bbb"]

    def test_a_page_reused_from_disk_has_no_source_digest(self):
        """Nothing hashed the zip, because nothing downloaded it."""
        job = make_job(imgs=[Path("nb.page-1.png")])
        self._pipeline()._describe_pages(job)

        assert job.pages[0].source_key == ""

    def test_no_page_carries_text_yet(self):
        """Text arrives three stages later; the page exists before it does."""
        job = make_job(imgs=[Path("nb.page-1.png")])
        self._pipeline()._describe_pages(job)

        assert job.pages[0].text == ""
        assert job.pages[0].error is None


class TestRendererDispatch:
    """Document type selects the renderer, and the registry is the only table.

    This used to assert the shape of a ``_RENDERERS`` dict on the pipeline,
    which meant adding a source format meant editing ``pipeline.py``. The
    pipeline now resolves the source by name and calls the contract, so the
    thing worth pinning is that resolution, not a dispatch table.
    """

    def test_pdf_and_epub_have_their_own_renderers(self):
        from living_ink.sources import source_for_name

        assert source_for_name("pdf").renderer is not source_for_name("epub").renderer

    def test_anything_else_renders_as_a_notebook(self):
        from living_ink.sources import fallback_source, source_for_name

        assert source_for_name("notebook") is fallback_source()
        assert source_for_name("djvu") is fallback_source()

    def test_the_pipeline_holds_no_dispatch_table_of_its_own(self):
        assert not hasattr(SyncPipeline, "_RENDERERS")


class TestOcrPreflight:
    """One backend means a provider that cannot read an image is a hard error."""

    def _validate(self, reads_images: bool):
        """Run the preflight with the provider's vision support forced.

        Args:
            reads_images: What ``vision_ocr_available()`` should report.
        """
        with patch("living_ink.pipeline.vision_ocr_available", return_value=reads_images):
            with patch("living_ink.clean._get_provider", return_value=MagicMock()):
                pipeline.validate_environment()

    def test_a_provider_that_reads_images_is_enough(self):
        self._validate(reads_images=True)

    def test_a_provider_that_cannot_read_an_image_stops_the_run(self):
        """There is nothing left to fall back to, so this is not a warning."""
        with pytest.raises(ConfigurationMissing) as err:
            self._validate(reads_images=False)
        assert "No OCR method available" in str(err.value)

    def test_the_remedy_is_the_setup_command(self):
        """The preflight used to point at a guide that no longer exists."""
        with pytest.raises(ConfigurationMissing) as err:
            self._validate(reads_images=False)
        assert "SETUP_GUIDE" not in str(err.value)
        assert err.value.hint == "run: living-ink setup"


class TestAFailedPageIsNotABlankPage:
    """Three pages out of two hundred failing is not a failed notebook.

    Both a failure and a blank arrive as ``text == ""``. Only ``Page.error``
    tells them apart, and without it the partial-page policy is unimplementable:
    the page vanishes from the note with nothing marking where it was, and the
    next run's merge has no block to heal.
    """

    def _pipeline(self):
        pipe = SyncPipeline(destinations=[MockDestination()])
        pipe.settings = replace(pipe.settings, ocr_concurrency=1)
        return pipe

    def _job_with_pages(self, count: int) -> DocumentJob:
        job = make_job(imgs=[Path(f"nb.page-{n}.png") for n in range(1, count + 1)])
        job.pre_paths = list(job.imgs)
        SyncPipeline(destinations=[MockDestination()])._describe_pages(job)
        return job

    def test_the_failure_is_recorded_on_the_page_that_failed(self):
        pipe = self._pipeline()
        job = self._job_with_pages(2)

        with patch.object(
            pipe.transcriber, "transcribe_one", side_effect=[("a", None), ("", "boom")]
        ):
            pipe._ocr_pages(job)

        assert job.pages[0].error is None
        assert job.pages[1].error == "boom"

    def test_a_blank_page_is_left_unmarked(self):
        pipe = self._pipeline()
        job = self._job_with_pages(1)

        with patch.object(pipe.transcriber, "transcribe_one", return_value=("", None)):
            pipe._ocr_pages(job)

        assert job.pages[0].error is None

    def test_failures_are_counted(self):
        pipe = self._pipeline()
        job = self._job_with_pages(2)

        with patch.object(
            pipe.transcriber, "transcribe_one", side_effect=[("", "boom"), ("", "boom")]
        ):
            pipe._ocr_pages(job)

        assert job.failed_pages == 2

    def test_the_run_report_names_the_page_not_just_the_document(self):
        pipe = self._pipeline()
        pipe.report = RunReport()
        job = self._job_with_pages(2)

        with patch.object(
            pipe.transcriber, "transcribe_one", side_effect=[("a", None), ("", "boom")]
        ):
            pipe._ocr_pages(job)

        assert any("Page 2" in w and "boom" in w for w in pipe.report.warnings)

    def test_the_transcript_shows_the_reason_where_the_text_would_be(self, tmp_path, monkeypatch):
        """A reader of the transcript sees a gap, not a page that was blank."""
        pipe = self._pipeline()
        job = self._job_with_pages(2)
        job = replace(job, workspace=DocumentWorkspace(tmp_path, "nb-1").ensure())
        job.pages = [
            replace(job.pages[0], text="written"),
            replace(job.pages[1], error="429 rate limited"),
        ]

        pipe._write_transcripts(job)

        assert "429 rate limited" in job.workspace.transcript.read_text(encoding="utf-8")


class TestDryRun:
    """A dry run transcribes as usual, then publishes and records nothing."""

    def _job(self, tmp_path) -> DocumentJob:
        workspace = DocumentWorkspace(tmp_path, "nb-1").ensure()
        workspace.transcript.write_text('{"notebook": "Notes"}\n\n### Page 1\n\nHello\n')
        return make_job(folder_path="Work", workspace=workspace)

    def test_nothing_is_published(self, tmp_path):
        dest = MockDestination("MockDest")
        pipeline_obj = SyncPipeline(dry_run=True, destinations=[dest])

        with patch("living_ink.pipeline.add_to_processed_log") as recorded:
            assert pipeline_obj._publish(self._job(tmp_path), [dest]) is True

        assert dest.published == []
        recorded.assert_not_called()

    def test_it_reports_where_the_transcript_landed(self, tmp_path, capsys):
        dest = MockDestination("MockDest")
        pipeline_obj = SyncPipeline(dry_run=True, destinations=[dest])
        job = self._job(tmp_path)

        pipeline_obj._publish(job, [dest])
        out = capsys.readouterr().out

        assert "Dry run" in out
        assert str(job.workspace.transcript) in out

    def test_it_says_how_much_of_an_existing_note_would_be_rewritten(self, tmp_path, capsys):
        """The whole-note promise is what a user needs before the run, not after."""
        dest = MockDestination("MockDest")
        pipeline_obj = SyncPipeline(dry_run=True, destinations=[dest])

        pipeline_obj._publish(self._job(tmp_path), [dest])

        assert "Replaces the whole note" in capsys.readouterr().out

    def test_a_page_level_destination_promises_something_different(self, tmp_path, capsys):
        dest = MockDestination("MockDest")
        pipeline_obj = SyncPipeline(dry_run=True, destinations=[dest])

        with patch.object(type(dest), "merge_unit", MergeUnit.PAGE):
            pipeline_obj._publish(self._job(tmp_path), [dest])

        out = capsys.readouterr().out
        assert "only the pages that changed" in out
        assert "whole note" not in out

    def test_a_normal_run_still_publishes(self, tmp_path):
        dest = MockDestination("MockDest")
        pipeline_obj = SyncPipeline(destinations=[dest])

        with patch("living_ink.pipeline.add_to_processed_log") as recorded:
            assert pipeline_obj._publish(self._job(tmp_path), [dest]) is True

        assert len(dest.published) == 1
        recorded.assert_called_once()

    def test_a_dry_run_does_not_secretly_keep_the_artifacts(self):
        """It used to, and one flag turning on another is a third concept.

        The transcripts a rehearsal leaves behind are a debugging artifact, so
        a user who wants them asks for them; the implication meant a rehearsal
        littered the temp directory that a real sync would have purged.
        """
        assert SyncPipeline(dry_run=True).keep_temp is False
        assert SyncPipeline(dry_run=True, keep_temp=True).keep_temp is True


class TestOrderedDurability:
    """The note is written before the row that claims it. Never the reverse.

    Reversed, a crash between the two leaves a ``publications`` row pointing at
    a note that does not exist: the document matches on version for ever, is
    never pending again, and the content is silently gone. In this order the
    same crash costs one redundant republish, which the transcript cache makes
    nearly free.
    """

    @pytest.fixture(autouse=True)
    def _state_dir(self, tmp_path, monkeypatch):
        """Point the state layer at a temp directory, and drop it afterwards."""
        monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path)
        monkeypatch.setattr(pipeline, "ensure_runtime_dirs", lambda: None)
        pipeline.reset_state_store()
        yield
        pipeline.reset_state_store()

    def _job(self, tmp_path) -> DocumentJob:
        workspace = DocumentWorkspace(tmp_path, "nb-1").ensure()
        workspace.transcript.write_text("### Page 1\n\nHello\n", encoding="utf-8")
        return make_job(workspace=workspace)

    def _row(self, dest):
        return pipeline.get_state_store().get_publication("nb-1", dest.state_key)

    def test_a_successful_publish_records_the_row(self, tmp_path):
        dest = MockDestination("MockDest")
        pipe = SyncPipeline(destinations=[dest])

        assert pipe._publish(self._job(tmp_path), [dest]) is True
        assert self._row(dest)["version"] == "hash-1"

    def test_a_state_write_that_fails_leaves_the_note_and_keeps_it_pending(self, tmp_path):
        """The note is out; the row is not. The next run republishes it."""
        dest = MockDestination("MockDest")
        pipe = SyncPipeline(destinations=[dest])

        def explode(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        with patch("living_ink.pipeline.add_to_processed_log", side_effect=explode):
            assert pipe._publish(self._job(tmp_path), [dest]) is False

        assert len(dest.published) == 1
        assert self._row(dest) is None

    def test_a_publish_that_fails_records_nothing(self, tmp_path):
        """Nothing was written, so nothing may claim it was."""
        dest = MockDestination("MockDest")
        dest.publish_ok = False
        pipe = SyncPipeline(destinations=[dest])

        assert pipe._publish(self._job(tmp_path), [dest]) is False
        assert self._row(dest) is None

    def test_a_publish_that_raises_records_nothing(self, tmp_path):
        dest = MockDestination("MockDest")
        pipe = SyncPipeline(destinations=[dest])

        with patch.object(dest, "publish", side_effect=OSError("disk full")):
            assert pipe._publish(self._job(tmp_path), [dest]) is False

        assert self._row(dest) is None

    def test_the_recipe_reaches_the_row(self, tmp_path):
        """A recorded row carries what produced it, not only which version."""
        from living_ink.core.recipe import document_recipe
        from living_ink.sources import source_for_name

        dest = MockDestination("MockDest")
        pipe = SyncPipeline(destinations=[dest])

        pipe._publish(self._job(tmp_path), [dest])

        expected = document_recipe(source_for_name("notebook"), dest, pipe.settings)
        assert self._row(dest)["recipe"] == expected
        assert expected != ""


class TestOrderedDurabilityUnderAKill:
    """The same ordering, against a real vault and a crash placed between.

    The class above asserts the order with a mock destination. This one kills
    the process where it hurts — after the note is on disk and before the row
    that claims it — and then runs again, because the ordering is only worth
    anything if the recovery it buys actually happens. Every assertion here
    would still hold if the two steps were merely *sequenced*; what makes them
    durable is the ``fsync`` in between, which is the first test.
    """

    @pytest.fixture(autouse=True)
    def _state_dir(self, tmp_path, monkeypatch):
        """Point the state layer at a temp directory, and drop it afterwards."""
        monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path)
        monkeypatch.setattr(pipeline, "ensure_runtime_dirs", lambda: None)
        pipeline.reset_state_store()
        yield
        pipeline.reset_state_store()

    @pytest.fixture
    def vault(self, tmp_path):
        """A real Obsidian vault, so the note side of the test is not a mock."""
        path = tmp_path / "vault"
        path.mkdir()
        return path

    def _dest(self, vault):
        return ObsidianDestination(vault_path=str(vault))

    def _job(self, tmp_path, text="Hello"):
        workspace = DocumentWorkspace(tmp_path / "work", "nb-1").ensure()
        return make_job(
            workspace=workspace,
            pages=[make_page(index=0, number=1, text=text)],
        )

    def _notes(self, vault):
        return sorted(p.name for p in vault.rglob("*.md"))

    def test_the_note_is_fsynced_before_the_row_is_written(self, tmp_path, vault):
        """Sequencing alone is not durability: a crash flushes no page cache."""
        order = []
        real_fsync = os.fsync

        def watched_fsync(fd):
            order.append("fsync")
            return real_fsync(fd)

        def watched_record(*args, **kwargs):
            order.append("record")

        pipe = SyncPipeline(destinations=[self._dest(vault)])
        with (
            patch("living_ink.safeio.os.fsync", side_effect=watched_fsync),
            patch("living_ink.pipeline.add_to_processed_log", side_effect=watched_record),
        ):
            pipe._publish(self._job(tmp_path), pipe.destinations)

        assert "fsync" in order and "record" in order
        assert order.index("fsync") < order.index("record")

    def test_a_kill_between_the_two_leaves_the_note_and_no_row(self, tmp_path, vault):
        dest = self._dest(vault)
        pipe = SyncPipeline(destinations=[dest])

        with patch("living_ink.pipeline.add_to_processed_log", side_effect=KeyboardInterrupt):
            with pytest.raises(KeyboardInterrupt):
                pipe._publish(self._job(tmp_path), [dest])

        assert self._notes(vault) == ["Test Notebook.md"]
        assert pipeline.get_state_store().get_publication("nb-1", dest.state_key) is None

    def test_the_next_run_republishes_into_the_same_note(self, tmp_path, vault):
        """The cost of the ordering is one redundant publish, not a second note."""
        dest = self._dest(vault)
        pipe = SyncPipeline(destinations=[dest])

        with patch("living_ink.pipeline.add_to_processed_log", side_effect=KeyboardInterrupt):
            with pytest.raises(KeyboardInterrupt):
                pipe._publish(self._job(tmp_path, text="first"), [dest])

        assert pipe._publish(self._job(tmp_path, text="second"), [dest]) is True

        assert self._notes(vault) == ["Test Notebook.md"]
        written = (vault / "Test Notebook.md").read_text(encoding="utf-8")
        assert "second" in written and "first" not in written

    def test_the_row_the_second_run_writes_is_the_one_that_was_lost(self, tmp_path, vault):
        dest = self._dest(vault)
        pipe = SyncPipeline(destinations=[dest])

        with patch("living_ink.pipeline.add_to_processed_log", side_effect=KeyboardInterrupt):
            with pytest.raises(KeyboardInterrupt):
                pipe._publish(self._job(tmp_path), [dest])
        pipe._publish(self._job(tmp_path), [dest])

        row = pipeline.get_state_store().get_publication("nb-1", dest.state_key)
        assert row["version"] == "hash-1"
        assert row["target"] == "Test Notebook.md"

    def test_the_reverse_order_would_have_lost_the_note(self, tmp_path, vault):
        """Why the order is not arbitrary, stated as a test rather than a comment.

        A row recorded first and a note that never lands matches on version for
        ever: the document is never pending again and the content is gone. The
        run has to fail *with the note written*, which is what it does.
        """
        dest = self._dest(vault)
        pipe = SyncPipeline(destinations=[dest])

        with patch.object(dest, "commit", side_effect=OSError("disk full")):
            assert pipe._publish(self._job(tmp_path), [dest]) is False

        assert self._notes(vault) == []
        assert pipeline.get_state_store().get_publication("nb-1", dest.state_key) is None


class TestAPartialPublishStaysPending:
    """The gap markers are retried because the row says how many there were.

    ``state`` stores ``pages_failed`` and ``selection._owes_a_publish`` reads
    it, and both ends were tested — but ``_publish`` never passed the count, so
    every partial publish wrote a zero. The version matched, the recipe
    matched, and the clause that exists to catch exactly this had nothing left
    to notice: a notebook published with three
    ``> [!warning] Page not transcribed`` callouts kept them for ever.
    """

    @pytest.fixture(autouse=True)
    def _state_dir(self, tmp_path, monkeypatch):
        """Point the state layer at a temp directory, and drop it afterwards."""
        monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path)
        monkeypatch.setattr(pipeline, "ensure_runtime_dirs", lambda: None)
        pipeline.reset_state_store()
        yield
        pipeline.reset_state_store()

    def _job(self, tmp_path, *, failed: int) -> DocumentJob:
        workspace = DocumentWorkspace(tmp_path, "nb-1").ensure()
        workspace.transcript.write_text("### Page 1\n\nHello\n", encoding="utf-8")
        job = make_job(workspace=workspace)
        job.failed_pages = failed
        return job

    def _row(self, dest):
        return pipeline.get_state_store().get_publication("nb-1", dest.state_key)

    def test_the_count_reaches_the_row(self, tmp_path):
        dest = MockDestination("MockDest")
        pipe = SyncPipeline(destinations=[dest])

        assert pipe._publish(self._job(tmp_path, failed=3), [dest]) is True
        assert self._row(dest)["pages_failed"] == 3

    def test_a_whole_document_records_no_gaps(self, tmp_path):
        dest = MockDestination("MockDest")
        pipe = SyncPipeline(destinations=[dest])

        assert pipe._publish(self._job(tmp_path, failed=0), [dest]) is True
        assert self._row(dest)["pages_failed"] == 0

    def test_a_later_whole_publish_clears_the_gaps(self, tmp_path):
        """The retry has to be able to end, or the notebook is pending for ever."""
        dest = MockDestination("MockDest")
        pipe = SyncPipeline(destinations=[dest])

        pipe._publish(self._job(tmp_path, failed=3), [dest])
        pipe._publish(self._job(tmp_path, failed=0), [dest])

        assert self._row(dest)["pages_failed"] == 0


class TestEveryRunReadsItsPages:
    """A leftover transcript on disk is not a shortcut around the OCR stages.

    It used to be: a transcript newer than its pages was adopted whole and
    stages 4-6 were skipped. But the note's text comes from the pages now, and
    that path never filled them in — so it published a transcript-shaped file
    and an empty note. Free repeats come from the transcript cache, which is
    keyed by page bytes and cannot go stale.
    """

    def test_a_leftover_transcript_does_not_skip_ocr(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pipeline, "WORK_DIR", tmp_path / "work")
        workspace = DocumentWorkspace(tmp_path / "work", "nb-1").ensure()

        page = workspace.page_image(1)
        page.write_bytes(b"png")
        transcript = workspace.transcript
        transcript.write_text('{"notebook": "Notes"}\n\n### Page 1\n\nHello\n')
        os.utime(transcript, (page.stat().st_mtime + 10, page.stat().st_mtime + 10))

        pipeline_obj = SyncPipeline(destinations=[MockDestination()])
        nb_item = make_item("nb-1", "Notes", content_hash="h")

        with (
            patch("living_ink.pipeline.get_document_type", return_value="notebook"),
            patch.object(
                pipeline_obj, "_acquire_pages", side_effect=lambda job, c: job.imgs.append(page)
            ),
            patch.object(pipeline_obj, "_collect_tags"),
            patch.object(pipeline_obj, "_publish", return_value=True),
            patch.object(pipeline_obj, "_ocr_pages") as ocr_stage,
            patch.object(pipeline_obj, "_preprocess_images") as preprocess,
        ):
            assert (
                pipeline_obj.process_notebook_item(
                    make_candidate(nb_item), MagicMock(), keep_temp=True
                )
                is True
            )

        ocr_stage.assert_called_once()
        preprocess.assert_called_once()


class TestConfigPermissionRepair:
    """A config holding credentials is tightened on the way past, not just warned about."""

    def _write_config(self, tmp_path, mode):
        """Write a minimal config file at the given permission mode."""
        cfg = tmp_path / "config.yml"
        cfg.write_text("ai:\n  provider: gemini\n  api_key: secret\n", encoding="utf-8")
        cfg.chmod(mode)
        return cfg

    def test_a_world_readable_config_is_tightened(self, tmp_path, capsys):
        cfg = self._write_config(tmp_path, 0o644)
        pipeline.load_yaml_config(cfg)
        assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
        assert "Tightened permissions" in capsys.readouterr().err

    def test_an_already_private_config_is_left_alone(self, tmp_path, capsys):
        cfg = self._write_config(tmp_path, 0o600)
        pipeline.load_yaml_config(cfg)
        assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
        assert "Tightened permissions" not in capsys.readouterr().err

    def test_the_config_is_still_read(self, tmp_path):
        cfg = self._write_config(tmp_path, 0o666)
        loaded = pipeline.load_yaml_config(cfg)
        assert loaded["ai"]["provider"] == "gemini"

    def test_a_missing_config_is_not_an_error(self, tmp_path):
        assert pipeline.load_yaml_config(tmp_path / "absent.yml") == {}


class TestTheSecondOcrBackendIsGone:
    """Loading a config that still names Google Cloud Vision does nothing at all."""

    def _write_config(self, tmp_path, body):
        """Write a config file holding the given YAML body."""
        cfg = tmp_path / "config.yml"
        cfg.write_text(body, encoding="utf-8")
        return cfg

    def test_a_credentials_path_is_not_exported(self, tmp_path, monkeypatch):
        """It used to become ``GOOGLE_APPLICATION_CREDENTIALS`` for the SDK."""
        monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
        creds = tmp_path / "creds.json"
        creds.write_text("{}", encoding="utf-8")
        cfg = self._write_config(
            tmp_path, f"google_vision:\n  credentials_path: '{creds}'\nai:\n  provider: none\n"
        )

        pipeline.load_yaml_config(cfg)

        assert "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ

    def test_embedded_credentials_are_not_written_to_disk(self, tmp_path):
        """A service-account key landed beside config.yml at the default umask."""
        cfg = self._write_config(
            tmp_path,
            'google_vision:\n  credentials_json: \'{"private_key": "x"}\'\nai:\n  provider: none\n',
        )

        pipeline.load_yaml_config(cfg)

        assert not (tmp_path / "google_creds.json").exists()

    def test_the_rest_of_the_config_is_still_read(self, tmp_path):
        """The section is inert, not fatal: an old config still works."""
        cfg = self._write_config(
            tmp_path, "google_vision:\n  credentials_path: '/nowhere'\nai:\n  provider: none\n"
        )

        assert pipeline.load_yaml_config(cfg)["ai"]["provider"] == "none"


class TestConfigIsValidatedOnLoad:
    """A config that parses is not the same as a config that means something."""

    def _write(self, tmp_path, body):
        """Write a config file and return its path."""
        cfg = tmp_path / "config.yml"
        cfg.write_text(body, encoding="utf-8")
        # Already private, so the permission repair does not add a warning of
        # its own to the output these tests read.
        cfg.chmod(0o600)
        return cfg

    def test_a_misspelled_section_stops_the_run_with_a_suggestion(self, tmp_path):
        """The old behaviour was silence, then "0 notebooks published"."""
        cfg = self._write(tmp_path, "obsidain:\n  vault_path: /tmp/v\n")
        with pytest.raises(ConfigurationMissing) as excinfo:
            pipeline.load_yaml_config(cfg)
        assert "did you mean obsidian?" in str(excinfo.value)

    def test_every_problem_is_listed_at_once(self, tmp_path):
        """Reporting one per run means as many runs as the user made mistakes."""
        cfg = self._write(
            tmp_path, "obsidain:\n  vault_path: /tmp/v\nsync:\n  max_notebooks_per_run: many\n"
        )
        with pytest.raises(ConfigurationMissing) as excinfo:
            pipeline.load_yaml_config(cfg)
        message = str(excinfo.value)
        assert "2 problems" in message
        assert "obsidain" in message and "max_notebooks_per_run" in message

    def test_an_unusable_value_stops_the_run(self, tmp_path):
        """Proceeding would silently substitute the default for what was asked."""
        cfg = self._write(tmp_path, "sync:\n  max_notebooks_per_run: many\n")
        with pytest.raises(ConfigurationMissing) as excinfo:
            pipeline.load_yaml_config(cfg)
        assert "max_notebooks_per_run" in str(excinfo.value)
        assert str(cfg) in excinfo.value.hint

    def test_a_valid_config_says_nothing(self, tmp_path, capsys):
        """Validation must not add noise to the normal path."""
        cfg = self._write(tmp_path, "sync:\n  limit: 5\n")
        pipeline.load_yaml_config(cfg)
        assert "⚠️" not in capsys.readouterr().out

    def test_a_registered_destination_section_is_not_a_typo(self, tmp_path, capsys):
        """Every shipped destination's own section must pass its own check."""
        cfg = self._write(tmp_path, "obsidian:\n  enabled: true\n")
        pipeline.load_yaml_config(cfg)
        assert "unknown section" not in capsys.readouterr().out

    def test_a_syntax_error_still_degrades_instead_of_raising(self, tmp_path):
        """An unparseable file yields no config to validate, and already reports itself."""
        cfg = self._write(tmp_path, "ai:\n  provider: [unclosed\n")
        assert pipeline.load_yaml_config(cfg) == {}


class TestTheStoredApiKeyReachesTheProvider:
    """The key is stored per provider; nothing downstream should know that."""

    @pytest.fixture(autouse=True)
    def _fresh_provider(self):
        """Configuring a provider is module state; do not leak it."""
        from living_ink import clean

        clean._provider = None
        yield
        clean._provider = None

    def _write(self, tmp_path, body):
        """Write a private config file and return its path."""
        cfg = tmp_path / "config.yml"
        cfg.write_text(body, encoding="utf-8")
        cfg.chmod(0o600)
        return cfg

    def _provider(self):
        """Return the provider the last load configured."""
        from living_ink import clean

        return clean._get_provider()

    def test_the_key_for_the_selected_provider_reaches_it(self, tmp_path):
        """A config with no key at all still configures the provider."""
        cfg = self._write(tmp_path, "ai:\n  provider: gemini\n")
        credentials.write_secret("ai.api_key.gemini", "AIza-stored", config_path=cfg)

        pipeline.load_yaml_config(cfg)

        assert self._provider().api_key == "AIza-stored"

    def test_another_provider_s_key_is_not_used(self, tmp_path):
        """The keys are separate slots, not a single one with a label."""
        cfg = self._write(tmp_path, "ai:\n  provider: gemini\n")
        credentials.write_secret("ai.api_key.openai", "sk-openai", config_path=cfg)

        pipeline.load_yaml_config(cfg)

        assert self._provider().api_key == ""

    def test_a_key_left_in_the_config_still_works(self, tmp_path):
        """An install that has not been through the wizard again must keep syncing."""
        cfg = self._write(tmp_path, "ai:\n  provider: gemini\n  api_key: AIza-legacy\n")

        pipeline.load_yaml_config(cfg)

        assert self._provider().api_key == "AIza-legacy"

    def test_the_key_never_reaches_the_dictionary_callers_hold(self, tmp_path):
        """It is a credential. Nothing that walks the config should meet it."""
        cfg = self._write(tmp_path, "ai:\n  provider: gemini\n")
        credentials.write_secret("ai.api_key.gemini", "AIza-stored", config_path=cfg)

        loaded = pipeline.load_yaml_config(cfg)

        assert "api_key" not in loaded["ai"]

    def test_a_key_left_in_the_config_is_migrated(self, tmp_path):
        """Once, silently, on the next run — no prompt, no re-typing."""
        cfg = self._write(tmp_path, "ai:\n  provider: gemini\n  api_key: AIza-legacy\n")

        pipeline.load_yaml_config(cfg)

        assert credentials.read_secret("ai.api_key.gemini", config_path=cfg) == "AIza-legacy"

    def test_the_config_file_itself_is_not_rewritten(self, tmp_path):
        """Migration copies; it does not edit a file the user owns."""
        cfg = self._write(tmp_path, "ai:\n  provider: gemini\n  api_key: AIza-legacy\n")
        before = cfg.read_text(encoding="utf-8")

        pipeline.load_yaml_config(cfg)

        assert cfg.read_text(encoding="utf-8") == before

    def test_provider_none_looks_for_nothing(self, tmp_path):
        """Cleanup disabled means there is no key to want."""
        from living_ink.providers import NoneProvider

        cfg = self._write(tmp_path, "ai:\n  provider: none\n")
        credentials.write_secret("ai.api_key.gemini", "AIza-stored", config_path=cfg)

        pipeline.load_yaml_config(cfg)

        assert isinstance(self._provider(), NoneProvider)

    def test_no_ai_section_is_not_an_error(self, tmp_path):
        cfg = self._write(tmp_path, "sync:\n  limit: 5\n")

        assert "ai" not in pipeline.load_yaml_config(cfg)


class TestProcessedLog:
    """Sync state lives in SQLite; losing it re-pays for OCR on every notebook."""

    def _state_dir(self, tmp_path, monkeypatch):
        """Point the state layer at a temp directory."""
        monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path)
        monkeypatch.setattr(pipeline, "ensure_runtime_dirs", lambda: None)
        pipeline.reset_state_store()
        return tmp_path

    @pytest.fixture(autouse=True)
    def _drop_cached_store(self):
        """A cached store would point at a previous test's temp directory."""
        yield
        pipeline.reset_state_store()

    def test_entries_round_trip(self, tmp_path, monkeypatch):
        self._state_dir(tmp_path, monkeypatch)
        pipeline.add_to_processed_log("ObsidianDestination", "doc-1", "v1", recipe="")
        pipeline.add_to_processed_log("ObsidianDestination", "doc-2", "v9", recipe="")
        assert pipeline.get_state_store().published_versions("ObsidianDestination") == {
            "doc-1": "v1",
            "doc-2": "v9",
        }

    def test_republishing_updates_rather_than_duplicating(self, tmp_path, monkeypatch):
        self._state_dir(tmp_path, monkeypatch)
        pipeline.add_to_processed_log("ObsidianDestination", "doc-1", "v1", recipe="")
        pipeline.add_to_processed_log("ObsidianDestination", "doc-1", "v2", recipe="")
        assert pipeline.get_state_store().published_versions("ObsidianDestination") == {
            "doc-1": "v2"
        }

    def test_destinations_do_not_share_state(self, tmp_path, monkeypatch):
        """A notebook can be published to one destination and pending for another."""
        self._state_dir(tmp_path, monkeypatch)
        pipeline.add_to_processed_log("ObsidianDestination", "doc-1", "v1", recipe="")
        assert pipeline.get_state_store().published_versions("NotionDestination") == {}

    def test_state_survives_a_restart(self, tmp_path, monkeypatch):
        self._state_dir(tmp_path, monkeypatch)
        pipeline.add_to_processed_log("ObsidianDestination", "doc-1", "v1", recipe="")
        pipeline.reset_state_store()
        assert pipeline.get_state_store().published_versions("ObsidianDestination") == {
            "doc-1": "v1"
        }

    def test_a_second_process_sees_the_write(self, tmp_path, monkeypatch):
        """`watch` and a manual sync used to overwrite each other's progress."""
        from living_ink import state

        self._state_dir(tmp_path, monkeypatch)
        pipeline.add_to_processed_log("ObsidianDestination", "doc-1", "v1", recipe="")

        with state.StateStore(pipeline.get_state_db_path()) as other:
            other.record_publication("doc-2", "ObsidianDestination", "v2", recipe="")

        assert pipeline.get_state_store().published_versions("ObsidianDestination") == {
            "doc-1": "v1",
            "doc-2": "v2",
        }


class TestOpeningTheStoreSweepsDeadDestinations:
    """A destination that no longer ships loses its rows on the first open.

    ``state`` cannot do this itself — it must not know what a destination is —
    and ``destinations`` cannot, because it never sees the store. The pipeline
    is the only layer holding both, so this is where the two are joined and
    where it has to be tested.
    """

    @pytest.fixture(autouse=True)
    def _state_dir(self, tmp_path, monkeypatch):
        """Point the state layer at a temp directory, before and after."""
        monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path)
        monkeypatch.setattr(pipeline, "ensure_runtime_dirs", lambda: None)
        pipeline.reset_state_store()
        yield
        pipeline.reset_state_store()

    def _seed(self, destination: str, doc_id: str = "doc-1") -> None:
        """Write one publication row straight into the database on disk.

        Args:
            destination: The state key to file it under.
            doc_id: The document it belongs to.
        """
        from living_ink import state

        with state.StateStore(pipeline.get_state_db_path()) as store:
            store.record_publication(doc_id, destination, "v1", recipe="")

    def test_the_registry_is_what_counts_as_known(self):
        """Every shipped destination's declared key, not its class name."""
        from living_ink.destinations import ObsidianDestination

        assert pipeline.registered_state_keys() == [ObsidianDestination.state_key]

    def test_rows_from_a_deleted_destination_are_gone(self, capsys):
        self._seed("AppleNotesDestination")
        capsys.readouterr()

        store = pipeline.get_state_store()

        assert store.published_versions("AppleNotesDestination") == {}
        assert "AppleNotesDestination" in capsys.readouterr().out

    def test_the_shipped_destination_keeps_its_rows(self):
        self._seed("ObsidianDestination")

        store = pipeline.get_state_store()

        assert store.published_versions("ObsidianDestination") == {"doc-1": "v1"}

    def test_a_clean_database_says_nothing(self, capsys):
        self._seed("ObsidianDestination")
        capsys.readouterr()

        pipeline.get_state_store()

        assert "Forgot" not in capsys.readouterr().out

    def test_a_row_arriving_from_legacy_json_is_swept_in_the_same_pass(self):
        """The import runs first, so its rows must be visible to the sweep.

        Otherwise an upgrade from the JSON era resurrects exactly the rows this
        is meant to remove, and they survive until the run after next.
        """
        legacy = pipeline.DATA_DIR / "processed_notebooks_AppleNotesDestination.json"
        legacy.write_text('{"doc-1": 7}', encoding="utf-8")

        store = pipeline.get_state_store()

        assert store.published_versions("AppleNotesDestination") == {}


class TestConfigSecretsAreRegistered:
    """Loading a config arms the redactor before anything can log a key."""

    def test_config_credentials_are_registered_on_load(self, tmp_path):
        from living_ink import redact as redact_mod

        redact_mod.clear_secrets()
        cfg = tmp_path / "config.yml"
        cfg.write_text(
            "ai:\n  provider: gemini\n  api_key: AIzaSyExampleKeyForTesting1234\n"
            "remarkable:\n  device_token: rm-device-token-abcdef123456\n",
            encoding="utf-8",
        )
        try:
            pipeline.load_yaml_config(cfg)
            secrets = redact_mod.registered_secrets()
        finally:
            redact_mod.clear_secrets()

        assert "AIzaSyExampleKeyForTesting1234" in secrets
        assert "rm-device-token-abcdef123456" in secrets


class TestExternalIdRoundTrip:
    """The id a destination assigns has to survive until the next sync."""

    @pytest.fixture(autouse=True)
    def _state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path)
        monkeypatch.setattr(pipeline, "ensure_runtime_dirs", lambda: None)
        pipeline.reset_state_store()
        yield
        pipeline.reset_state_store()

    def test_an_id_is_stored_with_the_publication(self):
        pipeline.add_to_processed_log(
            "FakeApiDestination", "doc-1", "v1", external_id="obj-7", recipe=""
        )
        record = pipeline.get_state_store().get_publication("doc-1", "FakeApiDestination")
        assert record["external_id"] == "obj-7"

    def test_a_later_sync_without_an_id_keeps_the_old_one(self):
        """A destination that fails to report an id must not erase the record."""
        pipeline.add_to_processed_log(
            "FakeApiDestination", "doc-1", "v1", external_id="obj-7", recipe=""
        )
        pipeline.add_to_processed_log("FakeApiDestination", "doc-1", "v2", recipe="")

        record = pipeline.get_state_store().get_publication("doc-1", "FakeApiDestination")
        assert record["external_id"] == "obj-7"
        assert record["version"] == "v2"


class TestOutcomeRecording:
    """A document that failed has to say so until it succeeds."""

    @pytest.fixture(autouse=True)
    def _state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path)
        monkeypatch.setattr(pipeline, "ensure_runtime_dirs", lambda: None)
        pipeline.reset_state_store()
        yield
        pipeline.reset_state_store()

    def _pipeline(self, dry_run=False):
        """A pipeline with no config work done, for calling one method on."""
        pipe = pipeline.SyncPipeline.__new__(pipeline.SyncPipeline)
        pipe.dry_run = dry_run
        return pipe

    def _job(self, doc_id="doc-1"):
        """The minimum of a job that _record_outcome reads."""
        return SimpleNamespace(notebook_id=doc_id, notebook="Notes")

    def test_a_failure_is_written(self):
        store = pipeline.get_state_store()
        store.record_document("doc-1")

        self._pipeline()._record_outcome(self._job(), False, "Download timed out")

        assert store.get_document("doc-1")["last_error"] == "Download timed out"

    def test_a_success_clears_an_earlier_failure(self):
        store = pipeline.get_state_store()
        store.record_document("doc-1")
        pipe = self._pipeline()

        pipe._record_outcome(self._job(), False, "boom")
        pipe._record_outcome(self._job(), True, None)

        assert store.get_document("doc-1")["last_error"] is None

    def test_a_dry_run_records_nothing(self):
        store = pipeline.get_state_store()
        store.record_document("doc-1")
        pipe = self._pipeline(dry_run=True)

        pipe._record_outcome(self._job(), False, "boom")

        assert store.get_document("doc-1")["last_error"] is None

    def test_secrets_are_redacted_from_the_message(self):
        """An error quoting a token must not park it in the database."""

        store = pipeline.get_state_store()
        store.record_document("doc-1")
        register_secret("sk-abcdefghijklmnopqrstuvwx")
        try:
            self._pipeline()._record_outcome(
                self._job(), False, "auth failed for sk-abcdefghijklmnopqrstuvwx"
            )
        finally:
            clear_secrets()

        assert "sk-abcdefghijklmnopqrstuvwx" not in store.get_document("doc-1")["last_error"]

    def test_a_broken_store_does_not_fail_the_sync(self, monkeypatch):
        monkeypatch.setattr(
            pipeline, "get_state_store", MagicMock(side_effect=OSError("disk is gone"))
        )
        self._pipeline()._record_outcome(self._job(), False, "boom")


class TestPageHashRecording:
    """Every rendered page is remembered, so a later run can tell what moved."""

    @pytest.fixture(autouse=True)
    def _state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path)
        monkeypatch.setattr(pipeline, "ensure_runtime_dirs", lambda: None)
        pipeline.reset_state_store()
        yield
        pipeline.reset_state_store()

    def _pipeline(self, dry_run=False):
        pipe = SyncPipeline.__new__(SyncPipeline)
        pipe.dry_run = dry_run
        pipe.run_id = None
        pipe.report = RunReport()
        return pipe

    def _job(self, tmp_path, pages=2) -> DocumentJob:
        imgs = []
        for index in range(pages):
            path = tmp_path / f"page-{index}.png"
            path.write_bytes(f"page {index}".encode())
            imgs.append(path)
        return DocumentJob(
            item={},
            notebook="Notes",
            notebook_id="doc-1",
            doc_type="notebook",
            version="v1",
            folder_path="",
            display_title="Notes",
            keep_temp=False,
            imgs=imgs,
        )

    def test_every_page_gets_a_hash(self, tmp_path):
        job = self._job(tmp_path)
        self._pipeline()._record_page_hashes(job)
        assert len(job.page_hashes) == 2
        assert job.page_hashes[0] != job.page_hashes[1]

    def test_the_hashes_are_stored_against_the_document(self, tmp_path):
        job = self._job(tmp_path)
        self._pipeline()._record_page_hashes(job)

        pages = pipeline.get_state_store().get_pages("doc-1")
        assert [pages[i]["render_hash"] for i in (0, 1)] == job.page_hashes

    def test_an_unchanged_page_hashes_the_same_way(self, tmp_path):
        job = self._job(tmp_path)
        pipe = self._pipeline()
        pipe._record_page_hashes(job)
        first = list(job.page_hashes)

        pipe._record_page_hashes(job)
        assert job.page_hashes == first

    def test_rewriting_a_page_changes_its_hash(self, tmp_path):
        job = self._job(tmp_path)
        pipe = self._pipeline()
        pipe._record_page_hashes(job)
        before = job.page_hashes[0]

        job.imgs[0].write_bytes(b"annotated")
        pipe._record_page_hashes(job)
        assert job.page_hashes[0] != before

    def test_a_dry_run_records_nothing(self, tmp_path):
        job = self._job(tmp_path)
        self._pipeline(dry_run=True)._record_page_hashes(job)

        assert job.page_hashes
        assert pipeline.get_state_store().get_pages("doc-1") == {}

    def test_an_unreadable_page_is_skipped(self, tmp_path):
        job = self._job(tmp_path, pages=1)
        job.imgs.append(tmp_path / "missing.png")
        self._pipeline()._record_page_hashes(job)
        assert len(job.page_hashes) == 1


class TestRenderCaching:
    """A page whose strokes have not changed is never rendered twice."""

    @pytest.fixture
    def rendered(self, tmp_path, monkeypatch):
        """Record every page the real renderer is asked for."""
        calls = []

        def fake_render(zip_path, page, **kwargs):
            calls.append(page)
            return f"png-{page}".encode()

        monkeypatch.setattr(
            "living_ink.extract.render_page_from_document_zip", fake_render, raising=True
        )
        monkeypatch.setattr(
            "living_ink.extract.get_page_source_hashes",
            lambda zip_path: ["hash-1", "hash-2"],
            raising=True,
        )
        monkeypatch.setattr(
            "living_ink.extract.get_document_page_count", lambda zip_path: 2, raising=True
        )
        monkeypatch.setattr("living_ink.extract.renderer_fingerprint", lambda: "fp", raising=True)
        return calls

    def _pipeline(self, tmp_path, enabled=True, device=None, background="white"):
        from living_ink.cache import RenderCache
        from living_ink.devices import default_reading

        pipe = SyncPipeline.__new__(SyncPipeline)
        pipe.device = device or default_reading()
        pipe.settings = Settings(render_background=background)
        pipe.renders = RenderCache(tmp_path / "renders", enabled=enabled)
        pipe.report = RunReport()
        pipe.keep_temp = False
        pipe.saved = []
        pipe._save_page = lambda job, page, data, label="Saved": pipe.saved.append((page, data))
        return pipe

    def _render(self, pipe, job, tmp_path):
        """Render a notebook the way the pipeline does, through its source.

        The page count is no longer passed in: the renderer enumerates its own
        pages, which is what lets a PDF report the three it was written on out
        of four hundred.
        """
        from living_ink.sources import NOTEBOOK, SourceBundle

        pipe._render_source(
            job,
            NOTEBOOK,
            SourceBundle(doc_id=job.notebook_id, title=job.notebook, zip_path=tmp_path / "doc.zip"),
        )

    def _job(self) -> DocumentJob:
        return DocumentJob(
            item={},
            notebook="Notes",
            notebook_id="doc-1",
            doc_type="notebook",
            version="v1",
            folder_path="",
            display_title="Notes",
            keep_temp=False,
        )

    def test_a_render_error_names_its_reason_in_the_report(self, tmp_path, rendered, monkeypatch):
        """A blank or unparseable page used to publish as an empty note."""
        from living_ink.extract import UnsupportedRmFormat

        def refuse(zip_path, page, **kwargs):
            raise UnsupportedRmFormat("page-2.rm is .rm format version 3")

        monkeypatch.setattr(
            "living_ink.extract.render_page_from_document_zip", refuse, raising=True
        )
        pipe = self._pipeline(tmp_path)

        self._render(pipe, self._job(), tmp_path)

        assert pipe.saved == []
        assert any("format version 3" in w for w in pipe.report.warnings)

    def test_the_document_counts_the_pages_it_lost(self, tmp_path, rendered, monkeypatch):
        """The outcome reads the count, so the summary can stop printing a clean ✓."""
        from living_ink.extract import UnsupportedRmFormat

        def refuse(zip_path, page, **kwargs):
            raise UnsupportedRmFormat("nope")

        monkeypatch.setattr(
            "living_ink.extract.render_page_from_document_zip", refuse, raising=True
        )
        pipe = self._pipeline(tmp_path)
        job = self._job()

        self._render(pipe, job, tmp_path)

        assert job.failed_pages == 2

    def test_one_bad_page_does_not_cost_the_others(self, tmp_path, rendered, monkeypatch):
        def refuse_page_one(zip_path, page, **kwargs):
            from living_ink.extract import BlankRenderError

            if page == 1:
                raise BlankRenderError("page-1.rm holds 12 strokes but rendered to an empty image")
            return b"png-2"

        monkeypatch.setattr(
            "living_ink.extract.render_page_from_document_zip", refuse_page_one, raising=True
        )
        pipe = self._pipeline(tmp_path)

        self._render(pipe, self._job(), tmp_path)

        assert [page for page, _ in pipe.saved] == [2]

    def test_a_refused_page_is_not_cached_as_an_empty_render(self, tmp_path, rendered, monkeypatch):
        """The next run, with a renderer that works, must still do the work."""
        from living_ink.extract import UnsupportedRmFormat

        broken = True

        def maybe_refuse(zip_path, page, **kwargs):
            if broken:
                raise UnsupportedRmFormat("nope")
            rendered.append(page)
            return f"png-{page}".encode()

        monkeypatch.setattr(
            "living_ink.extract.render_page_from_document_zip", maybe_refuse, raising=True
        )
        self._render(self._pipeline(tmp_path), self._job(), tmp_path)
        assert rendered == []

        broken = False
        again = self._pipeline(tmp_path)
        self._render(again, self._job(), tmp_path)
        assert rendered == [1, 2]

    def test_the_first_run_renders_every_page(self, tmp_path, rendered):
        pipe = self._pipeline(tmp_path)
        self._render(pipe, self._job(), tmp_path)
        assert rendered == [1, 2]

    def test_the_second_run_renders_nothing(self, tmp_path, rendered):
        pipe = self._pipeline(tmp_path)
        self._render(pipe, self._job(), tmp_path)
        rendered.clear()

        again = self._pipeline(tmp_path)
        self._render(again, self._job(), tmp_path)
        assert rendered == []

    def test_a_cached_page_is_still_saved(self, tmp_path, rendered):
        pipe = self._pipeline(tmp_path)
        self._render(pipe, self._job(), tmp_path)

        again = self._pipeline(tmp_path)
        self._render(again, self._job(), tmp_path)
        assert again.saved == [(1, b"png-1"), (2, b"png-2")]

    def test_only_the_changed_page_is_re_rendered(self, tmp_path, rendered, monkeypatch):
        """This is the whole point: an edited notebook costs one page, not all."""
        pipe = self._pipeline(tmp_path)
        self._render(pipe, self._job(), tmp_path)
        rendered.clear()

        monkeypatch.setattr(
            "living_ink.extract.get_page_source_hashes",
            lambda zip_path: ["hash-1", "hash-2-edited"],
            raising=True,
        )
        again = self._pipeline(tmp_path)
        self._render(again, self._job(), tmp_path)
        assert rendered == [2]

    def test_a_renderer_upgrade_re_renders_everything(self, tmp_path, rendered, monkeypatch):
        pipe = self._pipeline(tmp_path)
        self._render(pipe, self._job(), tmp_path)
        rendered.clear()

        monkeypatch.setattr("living_ink.extract.renderer_fingerprint", lambda: "fp2")
        again = self._pipeline(tmp_path)
        self._render(again, self._job(), tmp_path)
        assert rendered == [1, 2]

    def test_a_new_background_re_renders_everything(self, tmp_path, rendered):
        """The background is baked into the PNG, so it belongs in the key."""
        pipe = self._pipeline(tmp_path)
        self._render(pipe, self._job(), tmp_path)
        rendered.clear()

        again = self._pipeline(tmp_path, background="yellow")
        self._render(again, self._job(), tmp_path)
        assert rendered == [1, 2]

    def test_the_background_reaches_the_renderer(self, tmp_path, rendered, monkeypatch):
        """The colour in the key is the colour the page is rendered on.

        It used to be in the key and nowhere else, so setting it invalidated
        every cached render and produced byte-identical PNGs.
        """
        seen = {}

        def fake_render(zip_path, page, **kwargs):
            seen.update(kwargs)
            return b"png"

        monkeypatch.setattr(
            "living_ink.extract.render_page_from_document_zip", fake_render, raising=True
        )
        monkeypatch.setattr(
            "living_ink.extract.get_page_source_hashes", lambda zip_path: ["h1"], raising=True
        )

        pipe = self._pipeline(tmp_path, background="#123456")
        self._render(pipe, self._job(), tmp_path)

        assert seen["background_color"] == "#123456"

    def test_a_disabled_cache_renders_every_time(self, tmp_path, rendered):
        pipe = self._pipeline(tmp_path, enabled=False)
        self._render(pipe, self._job(), tmp_path)
        rendered.clear()

        again = self._pipeline(tmp_path, enabled=False)
        self._render(again, self._job(), tmp_path)
        assert rendered == [1, 2]

    def test_a_page_that_fails_to_render_is_skipped_not_cached(
        self, tmp_path, rendered, monkeypatch
    ):
        monkeypatch.setattr(
            "living_ink.extract.render_page_from_document_zip",
            lambda zip_path, page, **kwargs: None if page == 2 else b"png",
            raising=True,
        )
        monkeypatch.setattr(
            "living_ink.extract.get_page_source_hashes",
            lambda zip_path: ["hash-1", "hash-2"],
            raising=True,
        )
        monkeypatch.setattr("living_ink.extract.renderer_fingerprint", lambda: "fp")

        pipe = self._pipeline(tmp_path)
        self._render(pipe, self._job(), tmp_path)
        assert [page for page, _ in pipe.saved] == [1]
        assert pipe.renders.stats()[0] == 1

    def test_a_page_with_no_source_hash_is_rendered_anyway(self, tmp_path, rendered, monkeypatch):
        """An unhashable page loses the cache, not the render."""
        monkeypatch.setattr(
            "living_ink.extract.get_page_source_hashes", lambda zip_path: ["", "hash-2"]
        )
        pipe = self._pipeline(tmp_path)
        self._render(pipe, self._job(), tmp_path)
        assert rendered == [1, 2]
        assert pipe.renders.stats()[0] == 1


class TestTheRendererContractDrivesTheRun:
    """The pipeline calls prepare → pages → text_layer → render, and nothing else.

    It used to hold three ``_render_*`` methods and a dispatch table, so adding
    a format meant editing ``pipeline.py``. These tests run a source nothing
    ships against the real ``_render_source``, which is the only way to tell
    "the pipeline honours the contract" from "the three shipped renderers
    happen to work".
    """

    class RecordingRenderer:
        """A renderer that records the contract calls it received."""

        version = 7

        def __init__(self, pages=(), text=None, prepared=True, png=b"png"):
            self.calls = []
            self._pages = list(pages)
            self._text = text
            self._prepared = prepared
            self._png = png

        def prepare(self, bundle, ctx):
            self.calls.append("prepare")
            return self._prepared

        def pages(self, bundle, ctx):
            self.calls.append("pages")
            return self._pages

        def render(self, bundle, page, ctx):
            self.calls.append(f"render:{page.number}")
            return self._png(page) if callable(self._png) else self._png

        def text_layer(self, bundle, ctx):
            self.calls.append("text_layer")
            return self._text

        def describe_pages(self, bundle, pages):
            raise AssertionError("describe_pages belongs to a later stage")

    def _source(self, renderer, **kwargs):
        from living_ink.sources import SourceType

        return SourceType(
            name=kwargs.pop("name", "fake"),
            file_type_values=(),
            name_suffixes=(),
            source_suffix="",
            renderer=renderer,
            label=kwargs.pop("label", "Fake"),
            **kwargs,
        )

    def _pipeline(self, tmp_path, enabled=False):
        from living_ink.cache import RenderCache
        from living_ink.devices import default_reading

        pipe = SyncPipeline.__new__(SyncPipeline)
        pipe.device = default_reading()
        pipe.settings = Settings(render_background="white")
        pipe.renders = RenderCache(tmp_path / "renders", enabled=enabled)
        pipe.report = RunReport()
        pipe.keep_temp = False
        pipe.saved = []
        pipe._save_page = lambda job, page, data, label="Saved": pipe.saved.append((page, data))
        return pipe

    def _job(self) -> DocumentJob:
        return DocumentJob(
            item={},
            notebook="Notes",
            notebook_id="doc-1",
            doc_type="fake",
            version="v1",
            folder_path="",
            display_title="Notes",
            keep_temp=False,
        )

    def _bundle(self):
        from living_ink.sources import SourceBundle

        return SourceBundle(doc_id="doc-1", title="Notes", zip_path=Path("doc.zip"))

    def _refs(self, *numbers):
        from living_ink.sources import PageRef

        return [PageRef(ordinal=i, number=n, source_key=f"k{n}") for i, n in enumerate(numbers)]

    def test_the_contract_is_called_in_order(self, tmp_path):
        renderer = self.RecordingRenderer(pages=self._refs(1, 2))
        pipe = self._pipeline(tmp_path)
        pipe._render_source(self._job(), self._source(renderer), self._bundle())
        assert renderer.calls == ["prepare", "pages", "text_layer", "render:1", "render:2"]

    def test_a_sparse_page_number_survives_to_the_image(self, tmp_path):
        """A 400-page PDF annotated on page 377 saves page 377, not page 1."""
        pipe = self._pipeline(tmp_path)
        renderer = self.RecordingRenderer(pages=self._refs(12, 200, 377))
        pipe._render_source(self._job(), self._source(renderer), self._bundle())
        assert [page for page, _ in pipe.saved] == [12, 200, 377]

    def test_the_source_keys_line_up_with_the_saved_images(self, tmp_path):
        """Appended per saved page: indexing by page number breaks on sparse ones."""
        job = self._job()
        renderer = self.RecordingRenderer(pages=self._refs(12, 377))
        self._pipeline(tmp_path)._render_source(job, self._source(renderer), self._bundle())
        assert job.source_hashes == ["k12", "k377"]

    def test_a_page_that_renders_to_nothing_is_counted_not_dropped(self, tmp_path):
        """The PDF path used to drop an uncompositable page and never say so."""
        job = self._job()
        renderer = self.RecordingRenderer(
            pages=self._refs(1, 2), png=lambda page: None if page.number == 1 else b"png"
        )
        pipe = self._pipeline(tmp_path)
        pipe._render_source(job, self._source(renderer), self._bundle())
        assert job.failed_pages == 1
        assert [page for page, _ in pipe.saved] == [2]

    def test_the_text_layer_lands_on_the_job(self, tmp_path):
        job = self._job()
        renderer = self.RecordingRenderer(pages=(), text="Chapter One")
        self._pipeline(tmp_path)._render_source(job, self._source(renderer), self._bundle())
        assert job.extracted_doc_text == "Chapter One"

    def test_text_with_no_pages_is_a_complete_result(self, tmp_path):
        """An unannotated PDF has no pages at all, and that is not an error."""
        renderer = self.RecordingRenderer(pages=(), text="Chapter One")
        self._pipeline(tmp_path)._render_source(self._job(), self._source(renderer), self._bundle())

    def test_neither_pages_nor_text_stops_the_document(self, tmp_path):
        renderer = self.RecordingRenderer(pages=(), text=None)
        with pytest.raises(pipeline._StopProcessing) as err:
            self._pipeline(tmp_path)._render_source(
                self._job(), self._source(renderer), self._bundle()
            )
        assert err.value.success is False

    def test_a_source_that_calls_empty_a_skip_is_believed(self, tmp_path):
        """An empty notebook is a user who has not written anything yet."""
        renderer = self.RecordingRenderer(pages=(), text=None)
        with pytest.raises(pipeline._StopProcessing) as err:
            self._pipeline(tmp_path)._render_source(
                self._job(), self._source(renderer, empty_is_skip=True), self._bundle()
            )
        assert err.value.success is True

    def test_prepare_refusing_stops_before_anything_else_is_asked(self, tmp_path):
        renderer = self.RecordingRenderer(prepared=False)
        with pytest.raises(pipeline._StopProcessing):
            self._pipeline(tmp_path)._render_source(
                self._job(), self._source(renderer), self._bundle()
            )
        assert renderer.calls == ["prepare"]

    def test_the_renderer_version_is_in_the_cache_key(self, tmp_path):
        """One global format number could not say which of three changed."""
        source = self._source(self.RecordingRenderer(pages=self._refs(1)))

        first = self._pipeline(tmp_path, enabled=True)
        first._render_source(self._job(), source, self._bundle())

        source.renderer.version = 8
        second = self._pipeline(tmp_path, enabled=True)
        second._render_source(self._job(), source, self._bundle())
        assert source.renderer.calls.count("render:1") == 2

    def test_two_sources_do_not_share_a_cached_page(self, tmp_path):
        """Same digest, same version, different format — different image."""
        a = self.RecordingRenderer(pages=self._refs(1))
        b = self.RecordingRenderer(pages=self._refs(1))
        self._pipeline(tmp_path, enabled=True)._render_source(
            self._job(), self._source(a, name="alpha"), self._bundle()
        )
        self._pipeline(tmp_path, enabled=True)._render_source(
            self._job(), self._source(b, name="beta"), self._bundle()
        )
        assert b.calls.count("render:1") == 1

    def test_a_cached_page_is_not_rendered_twice(self, tmp_path):
        """Every source is cached now; the PDF path used to render every run."""
        source = self._source(self.RecordingRenderer(pages=self._refs(1)))
        for _ in range(2):
            self._pipeline(tmp_path, enabled=True)._render_source(
                self._job(), source, self._bundle()
            )
        assert source.renderer.calls.count("render:1") == 1


class TestParserWarningsReachTheReport:
    """rmscene's complaints are the only sign a page parsed incompletely.

    They used to be discarded — both parser loggers were raised to ``ERROR``
    when ``pipeline`` was imported — so a partial render published silently.
    Letting them out untouched is no better: they repeat per page, they name a
    block type and not a document, and they land in the middle of whatever the
    run is printing. They belong in the run report, attributed to the notebook.
    """

    @pytest.fixture
    def contract(self):
        """The renderer-contract fixtures, reused rather than copied."""
        return TestTheRendererContractDrivesTheRun()

    class WarningRenderer(TestTheRendererContractDrivesTheRun.RecordingRenderer):
        """A renderer whose parser complains the way rmscene does."""

        def __init__(self, *args, complaints=(), at="render", **kwargs):
            super().__init__(*args, **kwargs)
            self._complaints = list(complaints)
            self._at = at

        def _complain(self):
            for message in self._complaints:
                logging.getLogger("rmscene.scene_stream").warning(message)

        def prepare(self, bundle, ctx):
            if self._at == "prepare":
                self._complain()
            return super().prepare(bundle, ctx)

        def render(self, bundle, page, ctx):
            if self._at == "render":
                self._complain()
            return super().render(bundle, page, ctx)

    def _run(self, contract, tmp_path, renderer):
        """Render one document and return the pipeline that did it."""
        pipe = contract._pipeline(tmp_path)
        pipe._render_source(contract._job(), contract._source(renderer), contract._bundle())
        return pipe

    def test_a_parse_warning_becomes_a_report_warning(self, contract, tmp_path):
        pipe = self._run(
            contract,
            tmp_path,
            self.WarningRenderer(pages=contract._refs(1), complaints=["Unknown block type 42"]),
        )
        assert pipe.report.warnings == ["Notes: Unknown block type 42"]

    def test_it_never_reaches_a_stream(self, contract, tmp_path, capsys):
        """The whole point: a ``--json`` run writes one document to stdout."""
        self._run(
            contract,
            tmp_path,
            self.WarningRenderer(pages=contract._refs(1), complaints=["Unknown block type 42"]),
        )
        captured = capsys.readouterr()
        assert "Unknown block type" not in captured.out
        assert "Unknown block type" not in captured.err

    def test_one_complaint_repeated_per_page_is_reported_once(self, contract, tmp_path):
        pipe = self._run(
            contract,
            tmp_path,
            self.WarningRenderer(
                pages=contract._refs(1, 2, 3), complaints=["Some data has not been read"]
            ),
        )
        assert pipe.report.warnings == ["Notes: Some data has not been read"]

    def test_a_complaint_made_while_preparing_is_caught_too(self, contract, tmp_path):
        """rmscene reads the document in ``prepare`` and ``pages``, not only render."""
        pipe = self._run(
            contract,
            tmp_path,
            self.WarningRenderer(
                pages=contract._refs(1), complaints=["Unknown block type 42"], at="prepare"
            ),
        )
        assert pipe.report.warnings == ["Notes: Unknown block type 42"]

    def test_a_document_that_stopped_early_still_reports_them(self, contract, tmp_path):
        """That document is exactly the one whose warnings explain why."""
        renderer = self.WarningRenderer(
            pages=(), text=None, complaints=["Unknown block type 42"], at="prepare"
        )
        pipe = contract._pipeline(tmp_path)
        with pytest.raises(pipeline._StopProcessing):
            pipe._render_source(contract._job(), contract._source(renderer), contract._bundle())
        assert pipe.report.warnings == ["Notes: Unknown block type 42"]

    def test_a_quiet_render_reports_nothing(self, contract, tmp_path):
        pipe = self._run(contract, tmp_path, self.WarningRenderer(pages=contract._refs(1)))
        assert pipe.report.warnings == []


class TestTheDownloadedZipHonoursKeepTemp:
    """``--keep-temp`` is what CLAUDE.md tells people to debug rendering with."""

    def _pipeline(self, tmp_path, keep_temp):
        pipe = SyncPipeline.__new__(SyncPipeline)
        pipe.keep_temp = keep_temp
        pipe._render_source = lambda job, source, bundle: None
        return pipe

    def _job(self, tmp_path) -> DocumentJob:
        return DocumentJob(
            item={},
            notebook="Notes",
            notebook_id="doc-1",
            doc_type="notebook",
            version="v1",
            workspace=DocumentWorkspace(tmp_path, "doc-1").ensure(),
            folder_path="",
            display_title="Notes",
            keep_temp=False,
        )

    def _client(self):
        client = MagicMock()
        client.download.return_value = b"PK\x03\x04zip"
        return client

    def test_the_zip_is_removed_by_default(self, tmp_path, monkeypatch):
        monkeypatch.setattr("living_ink.extract.extract_tags_from_zip", lambda z: [])
        job = self._job(tmp_path)
        self._pipeline(tmp_path, keep_temp=False)._render_document(job, self._client())
        assert not job.workspace.download.exists()

    def test_keeping_temp_files_keeps_the_zip(self, tmp_path, monkeypatch):
        """The .rm source is exactly what a render bug needs; it used to vanish."""
        monkeypatch.setattr("living_ink.extract.extract_tags_from_zip", lambda z: [])
        job = self._job(tmp_path)
        self._pipeline(tmp_path, keep_temp=True)._render_document(job, self._client())
        assert job.workspace.download.read_bytes() == b"PK\x03\x04zip"

    def test_a_failed_render_still_cleans_up(self, tmp_path, monkeypatch):
        monkeypatch.setattr("living_ink.extract.extract_tags_from_zip", lambda z: [])
        pipe = self._pipeline(tmp_path, keep_temp=False)
        job = self._job(tmp_path)

        def boom(job, source, bundle):
            raise pipeline._StopProcessing(False, "nope")

        pipe._render_source = boom
        with pytest.raises(pipeline._StopProcessing):
            pipe._render_document(job, self._client())
        assert not job.workspace.download.exists()

    def test_a_download_that_returns_nothing_is_a_failure(self, tmp_path):
        client = MagicMock()
        client.download.return_value = None
        with pytest.raises(pipeline._StopProcessing) as err:
            self._pipeline(tmp_path, keep_temp=False)._render_document(self._job(tmp_path), client)
        assert err.value.success is False


class TestPublicationIdentity:
    """A destination is told which document it is publishing, and where it put it."""

    def _job(self, tmp_path) -> DocumentJob:
        workspace = DocumentWorkspace(tmp_path, "nb-1").ensure()
        workspace.transcript.write_text("Transcript", encoding="utf-8")
        return DocumentJob(
            item={"ID": "nb-1"},
            notebook="Notes",
            notebook_id="nb-1",
            doc_type="notebook",
            version="hash-1",
            workspace=workspace,
            folder_path="",
            display_title="Notes",
            keep_temp=False,
        )

    def test_the_document_id_reaches_the_destination(self, tmp_path):
        dest = MockDestination("MockDest")
        pipe = SyncPipeline(destinations=[dest])

        with patch("living_ink.pipeline.add_to_processed_log"):
            pipe._publish(self._job(tmp_path), [dest])

        assert dest.published[0]["doc"].doc_id == "nb-1"

    def test_a_destinations_warning_reaches_the_run_summary(self, tmp_path):
        """A log line scrolls past; the summary is the last thing on screen."""
        dest = MockDestination("MockDest")
        dest.publish_warnings = ("Notes (2).md belongs to another document.",)
        pipe = SyncPipeline(destinations=[dest])
        pipe.report = RunReport()

        with patch("living_ink.pipeline.add_to_processed_log"):
            pipe._publish(self._job(tmp_path), [dest])

        assert pipe.report.warnings == [
            "MockDestination: Notes (2).md belongs to another document."
        ]

    def test_a_warning_from_a_failed_publish_is_still_reported(self, tmp_path):
        """The run that went wrong is the one whose warnings matter most."""
        dest = MockDestination("MockDest")
        dest.publish_warnings = ("The vault is read-only.",)
        dest.publish_ok = False
        pipe = SyncPipeline(destinations=[dest])
        pipe.report = RunReport()

        with patch("living_ink.pipeline.add_to_processed_log"):
            pipe._publish(self._job(tmp_path), [dest])

        assert pipe.report.warnings == ["MockDestination: The vault is read-only."]

    def test_where_the_note_landed_is_recorded(self, tmp_path):
        dest = MockDestination("MockDest")
        dest.publish_target = "Work/Notes.md"
        pipe = SyncPipeline(destinations=[dest])

        with patch("living_ink.pipeline.add_to_processed_log") as recorded:
            pipe._publish(self._job(tmp_path), [dest])

        assert recorded.call_args.kwargs["target"] == "Work/Notes.md"

    def test_the_tablets_modification_date_reaches_the_destination(self, tmp_path):
        """It used to be fetched for one log line and then thrown away."""
        dest = MockDestination("MockDest")
        job = self._job(tmp_path)
        job.item = make_item("nb-1", modified="2026-03-04T09:30:00")
        pipe = SyncPipeline(destinations=[dest])

        with patch("living_ink.pipeline.add_to_processed_log"):
            pipe._publish(job, [dest])

        assert dest.published[0]["doc"].modified == datetime.datetime(2026, 3, 4, 9, 30)


class TestOrphanedNotebooks:
    """A notebook deleted on the tablet is reported, and pruned only when asked."""

    @pytest.fixture(autouse=True)
    def _state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path)
        monkeypatch.setattr(pipeline, "ensure_runtime_dirs", lambda: None)
        pipeline.reset_state_store()
        yield
        pipeline.reset_state_store()

    def _published(self, doc_id="nb-1", name="Old Notes", dest="MockDestination"):
        store = pipeline.get_state_store()
        store.record_document(doc_id, name=name)
        store.record_publication(doc_id, dest, "v1", target=f"{name}.md", recipe="")

    def _pipeline(self, dest, prune=False, dry_run=False):
        pipe = SyncPipeline.__new__(SyncPipeline)
        pipe.dry_run = dry_run
        pipe.destinations = [dest]
        pipe.report = None
        # ``prune`` is a view onto the resolved settings, not an attribute, so
        # a bare pipeline is given the settings that say it.
        pipe.settings = Settings.resolve({}, flags={"prune": prune or None})
        return pipe

    def test_a_missing_notebook_is_reported(self, capsys):
        self._published()
        self._pipeline(MockDestination())._handle_orphans(["nb-1"], {"nb-2": object()})
        out = capsys.readouterr().out
        assert "no longer on the tablet" in out
        assert "Old Notes" in out

    def test_reporting_deletes_nothing(self, capsys):
        self._published()
        dest = MockDestination()
        self._pipeline(dest)._handle_orphans(["nb-1"], {"nb-2": object()})

        assert dest.unpublished == []
        assert pipeline.get_state_store().get_publication("nb-1", "MockDestination") is not None

    def test_a_notebook_still_on_the_tablet_is_not_an_orphan(self, capsys):
        self._published()
        self._pipeline(MockDestination())._handle_orphans([], {"nb-1": object()})
        assert "no longer on the tablet" not in capsys.readouterr().out

    def test_an_empty_listing_is_never_treated_as_a_deletion(self, capsys):
        """A transport that returned nothing has not told us the tablet is empty."""
        self._published()
        self._pipeline(MockDestination())._handle_orphans(["nb-1"], {})
        assert capsys.readouterr().out == ""

    def test_a_dry_run_says_nothing_and_does_nothing(self, capsys):
        self._published()
        dest = MockDestination()
        self._pipeline(dest, prune=True, dry_run=True)._handle_orphans(["nb-1"], {"nb-2": object()})

        assert capsys.readouterr().out == ""
        assert dest.unpublished == []

    def test_pruning_deletes_the_note(self, capsys):
        self._published()
        dest = MockDestination()
        self._pipeline(dest, prune=True)._handle_orphans(["nb-1"], {"nb-2": object()})

        assert dest.unpublished == [("Old Notes.md", None, "nb-1")]

    def test_pruning_forgets_the_document(self, capsys):
        self._published()
        self._pipeline(MockDestination(), prune=True)._handle_orphans(["nb-1"], {"nb-2": object()})
        assert pipeline.get_state_store().get_publication("nb-1", "MockDestination") is None

    def test_a_destination_that_refuses_is_still_forgotten(self, capsys):
        """Otherwise the same orphan is reported again on every single run."""
        self._published()
        dest = MockDestination()
        dest.unpublish_result = False
        self._pipeline(dest, prune=True)._handle_orphans(["nb-1"], {"nb-2": object()})

        assert pipeline.get_state_store().get_publication("nb-1", "MockDestination") is None
        assert "left alone" in capsys.readouterr().out

    def test_a_destination_error_does_not_stop_the_run(self, capsys):
        self._published()
        dest = MockDestination()
        dest.unpublish_error = DestinationError("vault is gone")
        self._pipeline(dest, prune=True)._handle_orphans(["nb-1"], {"nb-2": object()})

        assert "vault is gone" in capsys.readouterr().out

    def test_a_destination_no_longer_configured_is_left_alone(self, capsys):
        self._published(dest="SomethingElse")
        dest = MockDestination()
        self._pipeline(dest, prune=True)._handle_orphans(["nb-1"], {"nb-2": object()})

        assert dest.unpublished == []
        assert "not configured" in capsys.readouterr().out


class TestTimestampCoercion:
    """The two transports disagree about what a timestamp looks like."""

    def test_a_datetime_passes_through(self):
        moment = datetime.datetime(2026, 3, 4, 9, 30)
        assert pipeline.to_datetime(moment) is moment

    def test_epoch_seconds(self):
        seconds = datetime.datetime(2026, 3, 4, 9, 30).timestamp()
        assert pipeline.to_datetime(seconds).year == 2026

    def test_the_device_counts_in_milliseconds(self):
        """SSH hands back the tablet's clock, which is 1000x everyone else's."""
        moment = datetime.datetime(2026, 3, 4, 9, 30)
        assert pipeline.to_datetime(int(moment.timestamp() * 1000)) == moment

    def test_milliseconds_spelled_as_a_string(self):
        moment = datetime.datetime(2026, 3, 4, 9, 30)
        assert pipeline.to_datetime(str(int(moment.timestamp() * 1000))) == moment

    def test_an_iso_string(self):
        assert pipeline.to_datetime("2026-03-04T09:30:00").year == 2026

    def test_an_iso_string_ending_in_z(self):
        assert pipeline.to_datetime("2026-03-04T09:30:00Z") is not None

    @pytest.mark.parametrize("value", [None, True, False, "", "   ", "not a date", object()])
    def test_nothing_intelligible_is_none(self, value):
        assert pipeline.to_datetime(value) is None

    def test_an_unrepresentable_timestamp_is_none(self):
        assert pipeline.to_datetime(1e18) is None

    def test_to_iso_date_drops_the_time(self):
        assert pipeline.to_iso_date("2026-03-04T09:30:00") == "2026-03-04"

    def test_to_iso_date_of_nothing_is_none(self):
        assert pipeline.to_iso_date(None) is None


class TestJobModifiedDate:
    """The tablet knows when the notebook was last written on. Ask it."""

    def _job(self, item):
        return DocumentJob(
            item=item,
            notebook="Notes",
            notebook_id="doc-1",
            doc_type="notebook",
            version=1,
            folder_path="",
            display_title="Notes",
            keep_temp=False,
        )

    def test_reads_the_field_the_transports_fill_in(self):
        moment = self._job(make_item(modified="2026-03-04T09:30:00")).modified_at()
        assert moment == datetime.datetime(2026, 3, 4, 9, 30)

    def test_a_transport_that_already_parsed_it_is_passed_through(self):
        item = make_item(modified=datetime.datetime(2026, 3, 4, 9, 30))
        assert self._job(item).modified_at() == datetime.datetime(2026, 3, 4, 9, 30)

    def test_an_item_with_no_date_reports_none(self):
        assert self._job(make_item()).modified_at() is None


class FakeCache:
    """A cache that records whether the run asked it to evict anything."""

    def __init__(self, noun, removed=0):
        self.noun = noun
        self.enabled = True
        self.removed = removed
        self.prunes = 0

    def prune(self):
        self.prunes += 1
        return self.removed


class TestPruningRunsLast:
    """An entry is only ever evicted after a run that did not want it."""

    @pytest.fixture(autouse=True)
    def _state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path)
        monkeypatch.setattr(pipeline, "ensure_runtime_dirs", lambda: None)
        pipeline.reset_state_store()
        yield
        pipeline.reset_state_store()

    def _pipeline(self, execute, dry_run=False):
        pipe = SyncPipeline.__new__(SyncPipeline)
        pipe.dry_run = dry_run
        pipe.cache = FakeCache("transcribed page", removed=2)
        pipe.renders = FakeCache("rendered page")
        pipe._counts = (0, 0, 0)
        pipe._execute = execute
        pipe.destinations = []
        pipe.report = None
        pipe.trigger = state.TRIGGER_MANUAL
        pipe.scheduled_fire_time = None
        pipe.nothing_pending = False
        pipe._failure_detail = None
        return pipe

    def test_a_successful_run_prunes_both_caches(self):
        """One call site at the end covers renders and transcriptions alike."""
        pipe = self._pipeline(lambda: True)

        pipe._run_recorded()

        assert (pipe.cache.prunes, pipe.renders.prunes) == (1, 1)

    def test_a_failed_run_prunes_nothing(self):
        """It does not know which entries it would have used."""
        pipe = self._pipeline(lambda: False)

        pipe._run_recorded()

        assert (pipe.cache.prunes, pipe.renders.prunes) == (0, 0)

    def test_an_interrupted_run_prunes_nothing(self):
        pipe = self._pipeline(lambda: (_ for _ in ()).throw(KeyboardInterrupt))

        with pytest.raises(KeyboardInterrupt):
            pipe._run_recorded()

        assert pipe.cache.prunes == 0

    def test_a_dry_run_prunes_nothing_because_it_changes_nothing(self):
        pipe = self._pipeline(lambda: True, dry_run=True)

        pipe._run_recorded()

        assert pipe.cache.prunes == 0

    def test_the_run_says_what_it_evicted(self, capsys):
        pipe = self._pipeline(lambda: True)

        pipe._run_recorded()

        assert "Pruned 2 unused transcribed page cache entries." in capsys.readouterr().out


class TestOneDocumentCannotEndTheRun:
    """An unexpected error costs one document, the way one costs one page.

    Only ``_StopProcessing`` was caught, so a transport that gave up after
    both fallbacks, or a truncated PNG throwing inside PIL, propagated out of
    the batch loop and past every handler to ``cli.main``. Document 12 of 40
    took the other 28 with it — unprocessed, unrecorded, and with no summary,
    because ``_print_summary`` runs after the loop.
    """

    @pytest.fixture(autouse=True)
    def _state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path)
        monkeypatch.setattr(pipeline, "ensure_runtime_dirs", lambda: None)
        pipeline.reset_state_store()
        yield
        pipeline.reset_state_store()

    def _pipeline(self, dry_run=False):
        pipe = SyncPipeline(dry_run=dry_run, destinations=[MockDestination()])
        pipe.report = RunReport()
        return pipe

    def _candidate(self, doc_id, name):
        return make_candidate(make_item(doc_id, name, content_hash="h"))

    def test_a_document_that_raises_is_a_failure_not_a_crash(self, monkeypatch):
        pipe = self._pipeline()
        monkeypatch.setattr(
            pipe,
            "process_notebook_item",
            lambda **kw: (_ for _ in ()).throw(ConnectionError("both transports refused")),
        )

        assert pipe._process_one(self._candidate("doc-1", "Notes"), MagicMock()) is False

    def test_the_documents_after_it_are_still_processed(self, monkeypatch):
        """The whole point: notebook 12 does not take notebooks 13-40 with it."""
        pipe = self._pipeline()
        seen = []

        def flaky(*, candidate, client, keep_temp):
            seen.append(candidate.doc_id)
            if candidate.doc_id == "doc-2":
                raise ConnectionError("both transports refused")
            return True

        monkeypatch.setattr(pipe, "process_notebook_item", flaky)
        results = [
            pipe._process_one(self._candidate(doc_id, doc_id), MagicMock())
            for doc_id in ("doc-1", "doc-2", "doc-3")
        ]

        assert seen == ["doc-1", "doc-2", "doc-3"]
        assert results == [True, False, True]

    def test_the_failure_reaches_the_run_summary(self, monkeypatch):
        pipe = self._pipeline()
        monkeypatch.setattr(
            pipe,
            "process_notebook_item",
            lambda **kw: (_ for _ in ()).throw(ValueError("truncated PNG")),
        )

        pipe._process_one(self._candidate("doc-1", "Notes"), MagicMock())

        outcome = pipe.report.documents[0]
        assert (outcome.name, outcome.doc_id, outcome.status) == ("Notes", "doc-1", FAILED)
        assert "truncated PNG" in outcome.reason

    def test_the_failure_is_remembered_for_the_next_run(self, monkeypatch):
        """``list`` has to be able to say this document is broken."""
        pipe = self._pipeline()
        pipeline.get_state_store().record_document("doc-1", name="Notes")
        monkeypatch.setattr(
            pipe,
            "process_notebook_item",
            lambda **kw: (_ for _ in ()).throw(ValueError("truncated PNG")),
        )

        pipe._process_one(self._candidate("doc-1", "Notes"), MagicMock())

        assert "truncated PNG" in pipeline.get_state_store().get_document("doc-1")["last_error"]

    def test_a_dry_run_records_nothing(self, monkeypatch):
        """A rehearsal that hit an error still leaves the state store alone."""
        pipe = self._pipeline(dry_run=True)
        pipeline.get_state_store().record_document("doc-1", name="Notes")
        monkeypatch.setattr(
            pipe, "process_notebook_item", lambda **kw: (_ for _ in ()).throw(ValueError("boom"))
        )

        pipe._process_one(self._candidate("doc-1", "Notes"), MagicMock())

        assert pipeline.get_state_store().get_document("doc-1")["last_error"] is None

    def test_an_interrupt_is_not_a_document_failure(self, monkeypatch):
        """Ctrl+C ends the run; swallowing it would make the next document start."""
        pipe = self._pipeline()
        monkeypatch.setattr(
            pipe, "process_notebook_item", lambda **kw: (_ for _ in ()).throw(KeyboardInterrupt)
        )

        with pytest.raises(KeyboardInterrupt):
            pipe._process_one(self._candidate("doc-1", "Notes"), MagicMock())


class TestRenderedPagesAreWrittenAtomically:
    """A half-written PNG is indistinguishable from a finished one."""

    def test_the_page_arrives_whole_or_not_at_all(self, tmp_path, monkeypatch):
        """``rendered_pages()`` trusts the filename, so the filename must lie."""
        job = MagicMock()
        job.workspace = DocumentWorkspace(tmp_path, "doc-1").ensure()
        seen = []
        real_replace = os.replace

        def watch(src, dst):
            # What is on disk under the final name at the moment of the rename.
            seen.append(job.workspace.rendered_pages())
            real_replace(src, dst)

        monkeypatch.setattr(pipeline.os, "replace", watch)
        SyncPipeline._save_page(MagicMock(), job, 1, b"PNG-bytes")

        assert seen == [[]]
        assert job.workspace.page_image(1).read_bytes() == b"PNG-bytes"

    def test_no_temp_file_is_left_behind(self, tmp_path):
        job = MagicMock()
        job.workspace = DocumentWorkspace(tmp_path, "doc-1").ensure()

        SyncPipeline._save_page(MagicMock(), job, 1, b"PNG-bytes")

        assert [p.name for p in job.workspace.pages_dir.iterdir()] == ["page-1.png"]


class TestInterruptedRuns:
    """Ctrl+C is not a failure, and the run did not do nothing."""

    @pytest.fixture(autouse=True)
    def _state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path)
        monkeypatch.setattr(pipeline, "ensure_runtime_dirs", lambda: None)
        pipeline.reset_state_store()
        yield
        pipeline.reset_state_store()

    def _pipeline(self, execute, dry_run=False):
        pipe = SyncPipeline.__new__(SyncPipeline)
        pipe.dry_run = dry_run
        pipe.cache = FakeCache("transcribed page")
        pipe.renders = FakeCache("rendered page")
        pipe._counts = (0, 0, 0)
        pipe._execute = execute
        pipe.destinations = []
        pipe.report = None
        pipe.trigger = state.TRIGGER_MANUAL
        pipe.scheduled_fire_time = None
        pipe.nothing_pending = False
        pipe._failure_detail = None
        return pipe

    def _last_run(self):
        return pipeline.get_state_store().last_run()

    def test_an_interrupt_is_recorded_as_an_interrupt_not_an_error(self):
        def execute():
            raise KeyboardInterrupt

        pipe = self._pipeline(execute)
        with pytest.raises(KeyboardInterrupt):
            pipe._run_recorded()

        assert self._last_run()["outcome"] == "interrupted"

    def test_work_done_before_the_interrupt_is_recorded(self):
        """The old code reported 0 published however far the run had got."""

        def execute():
            pipe._counts = (10, 3, 1)
            raise KeyboardInterrupt

        pipe = self._pipeline(execute)
        with pytest.raises(KeyboardInterrupt):
            pipe._run_recorded()

        row = self._last_run()
        assert (row["documents_seen"], row["documents_published"], row["documents_failed"]) == (
            10,
            3,
            1,
        )

    def test_a_run_that_dies_on_entry_still_records_itself(self, monkeypatch):
        """The counts are a constructor field, so there is always an answer.

        They used to be created at the top of ``_execute``, so anything that
        raised before that line — a Ctrl+C landing on entry, a refused
        connection — turned run-recording into an ``AttributeError`` that hid
        whatever had actually gone wrong.
        """
        pipe = SyncPipeline(destinations=[])
        monkeypatch.setattr(
            pipe, "_execute", lambda: (_ for _ in ()).throw(RuntimeError("no tablet"))
        )

        with pytest.raises(RuntimeError, match="no tablet"):
            pipe._run_recorded()

        assert self._last_run()["outcome"] == "error"

    def test_the_interrupt_still_reaches_the_caller(self):
        def execute():
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            self._pipeline(execute)._run_recorded()

    def test_a_real_error_is_still_an_error(self):
        def execute():
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            self._pipeline(execute)._run_recorded()

        assert self._last_run()["outcome"] == "error"

    def test_a_failed_run_tells_the_destination_so(self):
        # A CLI that fails in a terminal nobody is watching has told nobody.
        dest = MockDestination()
        pipe = self._pipeline(lambda: False)
        pipe.destinations = [dest]
        pipe._counts = (4, 1, 3)
        pipe._run_recorded()

        assert dest.failure_reports
        assert "1 of 4" in dest.failure_reports[0]
        assert dest.failures_cleared == 0

    def test_a_successful_run_takes_the_notice_away_again(self):
        dest = MockDestination()
        pipe = self._pipeline(lambda: True)
        pipe.destinations = [dest]
        pipe._run_recorded()

        assert dest.failures_cleared == 1
        assert dest.failure_reports == []

    def test_an_interrupt_says_nothing_because_the_user_did_it(self):
        dest = MockDestination()

        def execute():
            raise KeyboardInterrupt

        pipe = self._pipeline(execute)
        pipe.destinations = [dest]
        with pytest.raises(KeyboardInterrupt):
            pipe._run_recorded()

        assert dest.failure_reports == []
        assert dest.failures_cleared == 0

    def test_a_dry_run_leaves_the_destinations_untouched(self):
        dest = MockDestination()
        pipe = self._pipeline(lambda: True, dry_run=True)
        pipe.destinations = [dest]
        pipe._run_recorded()

        assert dest.failure_reports == []
        assert dest.failures_cleared == 0

    def test_a_destination_that_breaks_while_reporting_does_not_break_the_run(self):
        dest = MockDestination()
        dest.report_failure = lambda summary: (_ for _ in ()).throw(RuntimeError("nope"))
        pipe = self._pipeline(lambda: False)
        pipe.destinations = [dest]

        assert pipe._run_recorded() is False

    def test_a_dry_run_records_no_interrupt(self):
        def execute():
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            self._pipeline(execute, dry_run=True)._run_recorded()

        assert self._last_run() is None

    def test_the_user_is_told_the_transcripts_survived(self, capsys):
        def execute():
            pipe._counts = (10, 3, 0)
            raise KeyboardInterrupt

        pipe = self._pipeline(execute)
        with pytest.raises(KeyboardInterrupt):
            pipe._run_recorded()

        out = capsys.readouterr().out
        assert "Interrupted" in out
        assert "3 notebook(s) were published" in out
        assert "will not pay for them twice" in out

    def test_nothing_is_said_about_the_cache_when_it_is_off(self, capsys):
        def execute():
            raise KeyboardInterrupt

        pipe = self._pipeline(execute)
        pipe.cache = SimpleNamespace(enabled=False)
        with pytest.raises(KeyboardInterrupt):
            pipe._run_recorded()

        assert "twice" not in capsys.readouterr().out


class TestWhatTheRunRowRemembers:
    """A run row is what ``info`` reads back days later, so it has to say why.

    The counts alone cannot: "0 of 0 published" is the same row whether the
    tablet was unreachable, the token was revoked or there was genuinely
    nothing to sync — and those are three different things to do about it.
    """

    @pytest.fixture(autouse=True)
    def _state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path)
        monkeypatch.setattr(pipeline, "ensure_runtime_dirs", lambda: None)
        pipeline.reset_state_store()
        yield
        pipeline.reset_state_store()

    def _pipeline(self, execute, **fields):
        """Build a pipeline whose only real behaviour is ``_execute``."""
        pipe = SyncPipeline.__new__(SyncPipeline)
        pipe.dry_run = False
        pipe.cache = FakeCache("transcribed page")
        pipe.renders = FakeCache("rendered page")
        pipe._counts = (0, 0, 0)
        pipe._execute = execute
        pipe.destinations = []
        pipe.report = None
        pipe.trigger = state.TRIGGER_MANUAL
        pipe.scheduled_fire_time = None
        pipe.nothing_pending = False
        pipe._failure_detail = None
        for name, value in fields.items():
            setattr(pipe, name, value)
        return pipe

    def _last_run(self):
        return pipeline.get_state_store().last_run()

    def test_a_run_is_manual_unless_the_scheduler_asked(self):
        self._pipeline(lambda: True)._run_recorded()

        assert self._last_run()["trigger"] == state.TRIGGER_MANUAL

    def test_a_scheduled_run_carries_the_time_it_was_due(self):
        self._pipeline(
            lambda: True,
            trigger=state.TRIGGER_SCHEDULED,
            scheduled_fire_time="2026-09-19T07:00:00+00:00",
        )._run_recorded()

        row = self._last_run()
        assert (row["trigger"], row["scheduled_fire_time"]) == (
            state.TRIGGER_SCHEDULED,
            "2026-09-19T07:00:00+00:00",
        )

    def test_a_run_with_nothing_to_do_says_so_rather_than_success(self):
        """A healthy idle daemon and a dead one used to look identical."""

        def execute():
            pipe.nothing_pending = True
            return True

        pipe = self._pipeline(lambda: True)
        pipe._execute = execute
        pipe._run_recorded()

        assert self._last_run()["outcome"] == state.OUTCOME_NOTHING_TO_DO

    def test_finding_work_is_still_a_plain_success(self):
        self._pipeline(lambda: True)._run_recorded()

        assert self._last_run()["outcome"] == state.OUTCOME_SUCCESS

    def test_an_exception_keeps_what_it_said(self):
        def execute():
            raise RuntimeError("reMarkable Cloud pairing was revoked")

        with pytest.raises(RuntimeError):
            self._pipeline(execute)._run_recorded()

        assert self._last_run()["error"] == "reMarkable Cloud pairing was revoked"

    def test_an_exception_with_no_message_falls_back_to_its_type(self):
        def execute():
            raise TimeoutError

        with pytest.raises(TimeoutError):
            self._pipeline(execute)._run_recorded()

        assert self._last_run()["error"] == "TimeoutError"

    def test_a_secret_in_the_message_is_redacted_before_it_is_stored(self):
        """The row is printed back by ``info``, which is not a log file."""
        register_secret("sk-super-secret-token")

        def execute():
            raise RuntimeError("401 from provider using key sk-super-secret-token")

        with pytest.raises(RuntimeError):
            self._pipeline(execute)._run_recorded()

        assert "sk-super-secret-token" not in self._last_run()["error"]

    def test_a_partial_run_names_the_document_that_failed(self):
        report = RunReport()
        report.add(
            DocumentOutcome(name="Journal", status=FAILED, reason="every page was rate limited")
        )
        pipe = self._pipeline(lambda: False, report=report)
        pipe._counts = (2, 1, 1)
        pipe._run_recorded()

        assert self._last_run()["error"] == "Journal: every page was rate limited"

    def test_a_run_level_warning_is_used_when_no_document_failed(self):
        report = RunReport()
        report.warn("The vault is on a disconnected volume.")
        pipe = self._pipeline(lambda: False, report=report)
        pipe._run_recorded()

        assert self._last_run()["error"] == "The vault is on a disconnected volume."

    def test_the_counts_are_the_last_resort_and_not_the_first(self):
        pipe = self._pipeline(lambda: False)
        pipe._counts = (4, 1, 3)
        pipe._run_recorded()

        assert self._last_run()["error"] == "1 of 4 document(s) published; 3 failed."

    def test_an_interrupt_leaves_no_error_because_the_user_did_it(self):
        def execute():
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            self._pipeline(execute)._run_recorded()

        row = self._last_run()
        assert (row["outcome"], row["error"]) == (state.OUTCOME_INTERRUPTED, None)

    def test_a_success_leaves_no_error_either(self):
        self._pipeline(lambda: True)._run_recorded()

        assert self._last_run()["error"] is None


class TestResettingTheCachesBetweenTicks:
    """A daemon must pick up a config change; it must not reopen the database.

    Reopening per tick pays the ``_ADDED_COLUMNS`` probe and the legacy-import
    sweep every night for nothing, and nothing in ``config.yml`` can move the
    database anyway. Everything else that resets — a test, a wizard that moved
    the data directory — does want the handle dropped, so that is the default.
    """

    @pytest.fixture(autouse=True)
    def _state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path)
        monkeypatch.setattr(pipeline, "ensure_runtime_dirs", lambda: None)
        pipeline.reset_state_store()
        yield
        pipeline.reset_state_store()

    def test_the_open_database_survives_a_tick(self):
        before = pipeline.get_state_store()
        pipeline.reset_caches(keep_state_store=True)

        assert pipeline.get_state_store() is before

    def test_the_default_drops_it(self):
        before = pipeline.get_state_store()
        pipeline.reset_caches()

        assert pipeline.get_state_store() is not before

    def test_the_config_is_re_read_either_way(self, monkeypatch):
        reads = []
        monkeypatch.setattr(pipeline, "_default_config", {"already": "read"})
        monkeypatch.setattr(
            pipeline, "load_yaml_config", lambda *a, **kw: reads.append(1) or {"sync": {}}
        )

        pipeline.reset_caches(keep_state_store=True)
        pipeline.get_default_config()

        assert reads == [1]

    def test_the_destinations_are_rebuilt_so_no_tick_inherits_them(self, monkeypatch):
        """They are mutable objects; one tick must not hand them to the next."""
        monkeypatch.setattr(pipeline, "_default_destinations", [MockDestination()])
        pipeline.reset_caches(keep_state_store=True)

        assert pipeline._default_destinations is None


class TestPreflightRefusesABadDestination:
    """Nowhere to publish is a hard stop, not a quiet success.

    A vault that did not exist used to be swallowed into one warning, leaving
    the run to compare every document against zero destinations, report "no new
    or updated notebooks", and exit 0.
    """

    def test_a_ready_destination_passes(self):
        SyncPipeline(destinations=[MockDestination()]).preflight_destinations()

    def test_no_destinations_at_all_is_refused(self):
        pipe = SyncPipeline.__new__(SyncPipeline)
        pipe.destinations = []

        with patch("living_ink.pipeline.get_default_destinations", return_value=[]):
            with pytest.raises(ConfigurationMissing, match="nowhere to publish"):
                pipe.preflight_destinations()

    def test_a_failing_check_stops_the_run_before_any_page_is_rendered(self):
        dest = MockDestination()
        dest.ready = False

        with pytest.raises(ConfigurationMissing) as excinfo:
            SyncPipeline(destinations=[dest]).preflight_destinations()
        assert "mock" in str(excinfo.value)

    def test_every_destination_is_checked_not_just_the_first_to_fail(self):
        """Two misconfigured destinations should cost one run, not two."""
        first, second = MockDestination("one"), MockDestination("two")
        first.ready = second.ready = False
        checked = []
        for dest in (first, second):
            original = dest.check
            dest.check = lambda d=dest, o=original: (checked.append(d.name), o())[1]

        with pytest.raises(ConfigurationMissing):
            SyncPipeline(destinations=[first, second]).preflight_destinations()
        assert checked == ["one", "two"]

    def test_the_remedy_is_shown_alongside_the_reason(self, tmp_path):
        dest = ObsidianDestination(vault_path=str(tmp_path / "gone"))

        with pytest.raises(ConfigurationMissing) as excinfo:
            SyncPipeline(destinations=[dest]).preflight_destinations()
        assert "does not exist" in str(excinfo.value)
        assert "LIVING_INK_OBSIDIAN_VAULT_PATH" in str(excinfo.value)


class TestProgressIsRecordedPerNotebook:
    """A run interrupted mid-loop has still published what it published."""

    def test_counts_advance_as_each_notebook_finishes(self, monkeypatch):
        pipe = SyncPipeline.__new__(SyncPipeline)
        pipe._counts = (0, 0, 0)
        pipe.dry_run = False
        pipe.keep_temp = True
        pipe.settings = Settings.resolve({})
        pipe.target_notebook = None
        seen_counts = []

        def process(candidate, **kwargs):
            seen_counts.append(pipe._counts)
            return candidate.name != "bad"

        chosen = Selection(
            to_process=tuple(make_candidate(make_item(name, name)) for name in ("a", "bad", "c"))
        )

        monkeypatch.setattr(pipe, "process_notebook_item", process)
        monkeypatch.setattr(pipe, "connect", lambda: SimpleNamespace(get_meta_items=lambda: []))
        monkeypatch.setattr(pipe, "preflight_destinations", lambda: None)
        monkeypatch.setattr(pipe, "_learn_device", lambda client: None)
        monkeypatch.setattr(pipe, "select_documents", lambda listing, client: chosen)
        monkeypatch.setattr(pipe, "_handle_orphans", lambda orphans, id_map: None)
        monkeypatch.setattr(pipe, "_report_selection", lambda selection: None)
        monkeypatch.setattr(pipeline, "validate_environment", lambda: None)
        monkeypatch.setattr(pipeline, "cleanup_temp_artifacts", lambda **kw: None)

        pipe._execute()

        # Before the second notebook the first is already counted, so an
        # interrupt in the middle of the second still reports one published.
        assert seen_counts[1] == (3, 1, 0)
        assert seen_counts[2] == (3, 1, 1)


class TestRendererRegressionDetection:
    """The pages table earns its keep: it catches a renderer that moved."""

    @pytest.fixture(autouse=True)
    def _state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path)
        monkeypatch.setattr(pipeline, "ensure_runtime_dirs", lambda: None)
        pipeline.reset_state_store()
        yield
        pipeline.reset_state_store()

    def _pipeline(self):
        pipe = SyncPipeline.__new__(SyncPipeline)
        pipe.dry_run = False
        pipe.run_id = None
        pipe.report = RunReport()
        return pipe

    def _job(self, tmp_path, renders, sources):
        tmp_path.mkdir(parents=True, exist_ok=True)
        imgs = []
        for index, content in enumerate(renders):
            path = tmp_path / f"page-{index}.png"
            path.write_bytes(content)
            imgs.append(path)
        return DocumentJob(
            item={},
            notebook="Notes",
            notebook_id="nb-1",
            doc_type="notebook",
            version="v1",
            folder_path="",
            display_title="Notes",
            keep_temp=False,
            imgs=imgs,
            source_hashes=list(sources),
        )

    def _sync(self, tmp_path, renders, sources):
        pipe = self._pipeline()
        pipe._record_page_hashes(self._job(tmp_path, renders, sources))
        return pipe

    def test_an_unchanged_source_rendering_differently_is_reported(self, tmp_path):
        sources = ["src-a", "src-b"]
        self._sync(tmp_path / "run1", [b"page A", b"page B"], sources)

        # Same .rm sources, different pixels out: only the renderer can have moved.
        second = self._sync(tmp_path / "run2", [b"page A", b"DIFFERENT"], sources)

        assert any("the renderer moved" in w for w in second.report.warnings)
        assert any("1 page(s)" in w for w in second.report.warnings)

    def test_a_page_the_user_actually_edited_is_not_reported(self, tmp_path):
        self._sync(tmp_path / "run1", [b"page A", b"page B"], ["src-a", "src-b"])

        # New source and new render: the user wrote on the page. Normal.
        second = self._sync(tmp_path / "run2", [b"page A", b"EDITED"], ["src-a", "src-b-edited"])

        assert second.report.warnings == []

    def test_a_first_run_reports_nothing(self, tmp_path):
        first = self._sync(tmp_path, [b"page A", b"page B"], ["src-a", "src-b"])
        assert first.report.warnings == []

    def test_pages_with_no_recorded_source_are_not_reported(self, tmp_path):
        """A document synced before source hashes were stored is not evidence."""
        pipeline.get_state_store().record_page("nb-1", 0, render_hash="old")
        second = self._sync(tmp_path, [b"page A"], ["src-a"])
        assert second.report.warnings == []

    def test_pages_that_all_render_identically_are_reported_as_blank(self, tmp_path):
        blank = [b"blank"] * 4
        pipe = self._sync(tmp_path, blank, ["a", "b", "c", "d"])
        assert any("almost certainly blank" in w for w in pipe.report.warnings)

    def test_two_identical_pages_are_a_coincidence_not_a_bug(self, tmp_path):
        pipe = self._sync(tmp_path, [b"same", b"same"], ["a", "b"])
        assert pipe.report.warnings == []

    def test_a_notebook_of_real_pages_is_not_reported(self, tmp_path):
        pipe = self._sync(tmp_path, [b"a", b"b", b"c", b"d"], ["a", "b", "c", "d"])
        assert pipe.report.warnings == []

    def test_a_pipeline_with_no_report_does_not_crash(self, tmp_path):
        pipe = self._pipeline()
        pipe.report = None
        pipe._record_page_hashes(self._job(tmp_path, [b"a"], ["src-a"]))


class TestRunSummary:
    """Each document reaches the summary with what it cost."""

    def _pipeline(self):
        pipe = SyncPipeline.__new__(SyncPipeline)
        pipe.report = RunReport()
        pipe.settings = Settings.resolve({})
        pipe.target_notebook = None
        return pipe

    def _job(self, **kwargs):
        defaults = dict(
            item={},
            notebook="Notes",
            notebook_id="nb-1",
            doc_type="notebook",
            version="v1",
            folder_path="",
            display_title="Meeting Notes",
            keep_temp=False,
        )
        defaults.update(kwargs)
        return DocumentJob(**defaults)

    def test_a_published_document_carries_its_page_costs(self):
        pipe = self._pipeline()
        job = self._job(imgs=[Path("a.png")] * 6, transcribed_pages=4, cached_pages=2)
        job.published_to = ["ObsidianDestination"]

        pipe._report_job(job, True, None)

        entry = pipe.report.documents[0]
        assert (entry.status, entry.pages, entry.transcribed, entry.cached) == (PUBLISHED, 6, 4, 2)
        assert entry.destinations == ["ObsidianDestination"]

    def test_a_failure_carries_its_reason(self):
        pipe = self._pipeline()
        pipe._report_job(self._job(), False, "render failed")
        entry = pipe.report.documents[0]
        assert (entry.status, entry.reason) == (FAILED, "render failed")

    def test_a_success_that_published_nowhere_is_a_skip_not_a_win(self):
        """A dry run, or a notebook every destination already had."""
        pipe = self._pipeline()
        pipe._report_job(self._job(), True, None)
        assert pipe.report.documents[0].status == SKIPPED

    def test_documents_never_processed_are_listed_as_unchanged(self):
        pipe = self._pipeline()
        stale = make_candidate(make_item("nb-2", "Journal"))
        fresh = make_candidate(make_item("nb-1", "Notes"))

        pipe._report_selection(
            Selection(to_process=(fresh,), skipped=((stale, selection.UNCHANGED),))
        )

        assert len(pipe.report.documents) == 1
        assert pipe.report.documents[0].name == "Journal"
        assert pipe.report.documents[0].status == SKIPPED

    def test_a_skip_carries_the_real_reason(self):
        """Every one of these used to read "unchanged", whatever had happened."""
        pipe = self._pipeline()
        trashed = make_item("nb-3", "Deleted")

        pipe._report_selection(Selection(skipped=((trashed, selection.TRASHED),)))

        assert pipe.report.documents[0].reason == "in the trash"

    def test_a_pending_document_past_the_limit_is_deferred_not_unchanged(self):
        """With the default limit of one, this used to call nine notebooks fine."""
        pipe = self._pipeline()
        waiting = make_candidate(make_item("nb-9", "Later"), pending=(MockDestination(),))

        pipe._report_selection(Selection(deferred=(waiting,)))

        entry = pipe.report.documents[0]
        assert entry.status == DEFERRED
        assert entry.destinations == ["MockDestination"]

    def test_the_summary_is_printed_at_the_end(self, capsys):
        pipe = self._pipeline()
        pipe.report.add(DocumentOutcome(name="Notes", status=SKIPPED))

        pipe._print_summary()

        assert "Pages:" in capsys.readouterr().out

    def test_the_default_summary_is_the_compact_one(self, capsys):
        pipe = self._pipeline()
        pipe.report.add(DocumentOutcome(name="Notes", status=SKIPPED))

        assert pipe.verbose is False
        pipe._print_summary()

        assert "Notes" not in capsys.readouterr().out

    def test_verbose_lists_every_document(self, capsys):
        """``--verbose`` used to raise the log level and then print the same
        compact block, because ``_print_summary`` read a ``self.verbose`` that
        nothing ever set — the per-document table was unreachable."""
        pipe = self._pipeline()
        pipe.settings = Settings.resolve({}, flags={"verbosity": "verbose"})
        pipe.report.add(DocumentOutcome(name="Notes", status=SKIPPED))

        assert pipe.verbose is True
        pipe._print_summary()

        assert "Notes" in capsys.readouterr().out

    def test_quiet_is_not_verbose(self):
        pipe = self._pipeline()
        pipe.settings = Settings.resolve({}, flags={"verbosity": "quiet"})

        assert pipe.quiet is True
        assert pipe.verbose is False

    def test_json_output_is_machine_readable(self, capsys):
        pipe = self._pipeline()
        pipe.settings = Settings.resolve({}, flags={"output_json": True})
        pipe.report.add(DocumentOutcome(name="Notes", status=SKIPPED))

        pipe._print_summary()

        assert json.loads(capsys.readouterr().out)["skipped"] == 1


class TestDryRunReporting:
    """A dry run rehearses; the summary has to say so."""

    def _pipeline(self, target=None):
        pipe = SyncPipeline.__new__(SyncPipeline)
        pipe.report = RunReport()
        pipe.target_notebook = target
        return pipe

    def _job(self, **kwargs):
        defaults = dict(
            item={},
            notebook="Test",
            notebook_id="nb-1",
            doc_type="notebook",
            version="v1",
            folder_path="",
            display_title="Test",
            keep_temp=False,
        )
        defaults.update(kwargs)
        return DocumentJob(**defaults)

    def test_a_rehearsed_document_is_not_reported_as_skipped(self):
        pipe = self._pipeline()
        job = self._job(imgs=[Path("a.png")], transcribed_pages=1)
        job.would_publish_to = ["ObsidianDestination"]

        pipe._report_job(job, True, None)

        entry = pipe.report.documents[0]
        assert entry.status == WOULD_PUBLISH
        assert entry.destinations == ["ObsidianDestination"]
        assert entry.reason is None

    def test_a_document_nobody_wanted_is_still_a_skip(self):
        pipe = self._pipeline()
        pipe._report_job(self._job(), True, None)
        assert pipe.report.documents[0].status == SKIPPED

    def test_a_targeted_run_does_not_call_the_rest_unchanged(self):
        """It never looked at them, so it cannot vouch for them."""
        pipe = self._pipeline(target="Test")
        other = make_item("nb-2", "Other")

        pipe._report_selection(Selection(skipped=((other, selection.NOT_TARGETED),)))

        assert pipe.report.documents == []

    def test_an_untargeted_run_still_lists_them(self):
        pipe = self._pipeline()
        other = make_candidate(make_item("nb-2", "Other"))

        pipe._report_selection(Selection(skipped=((other, selection.UNCHANGED),)))

        assert pipe.report.documents[0].name == "Other"


class TestJsonSummaryReachesStdout:
    """``sync --json`` is only useful if its output can be piped into a parser."""

    def _pipeline(self, json_output):
        pipe = SyncPipeline.__new__(SyncPipeline)
        pipe.report = RunReport()
        pipe.settings = Settings.resolve({}, flags={"output_json": json_output or None})
        pipe.report.add(DocumentOutcome(name="Notes", status=SKIPPED, reason="unchanged"))
        return pipe

    def test_the_summary_is_parseable_json_on_stdout(self, capsys):
        self._pipeline(json_output=True)._print_summary()
        out = capsys.readouterr().out

        assert json.loads(out)["seen"] == 1

    def test_stdout_holds_the_document_and_nothing_else(self, capsys):
        """A stray progress line ahead of the JSON is what made this unusable."""
        pipe = self._pipeline(json_output=True)
        logs.configure(logs.LOG_PATH, json_output=True)
        log("Destination added: Obsidian")
        pipe._print_summary()
        captured = capsys.readouterr()

        json.loads(captured.out)
        assert "Destination added" in captured.err
        logs.configure(logs.LOG_PATH)

    def test_without_the_flag_the_table_is_printed_instead(self, capsys):
        self._pipeline(json_output=False)._print_summary()
        out = capsys.readouterr().out

        assert "Pages:" in out
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)


class TestThePreviewAndTheRunAgree:
    """`sync --status` predicts the run — because it makes the same call.

    The two used to be separate expressions of the same rule, free to drift.
    They are now one function, and these tests are what says so: the preview
    reaches it through ``cli.rows_from_selection``, the run through
    ``SyncPipeline.select_documents``, and the answers have to match.
    """

    @pytest.fixture
    def store(self, tmp_path, monkeypatch):
        """A real state store on a throwaway database."""
        monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path)
        monkeypatch.setattr(pipeline, "ensure_runtime_dirs", lambda: None)
        pipeline.reset_state_store()
        yield pipeline.get_state_store()
        pipeline.reset_state_store()

    ITEMS = [
        make_item("doc-new", "New", content_hash="h1"),
        make_item("doc-changed", "Changed", content_hash="h2"),
        make_item("doc-settled", "Settled", content_hash="h3"),
    ]

    def _client(self):
        """A transport that answers the listing and knows no file types."""
        return SimpleNamespace(
            get_meta_items=lambda: list(self.ITEMS),
            get_file_type=lambda item: None,
        )

    def _pipeline(self, dest, limit=10):
        """A pipeline whose per-run cap is above the document count.

        The cap defers rather than settles, so a low one here would look like a
        disagreement with a preview that is not capped at all.
        """
        return SyncPipeline(limit=limit, destinations=[dest])

    def _settle(self, store, pipe, dest, doc_id, version):
        """Record the publication a successful run would have left behind."""
        from living_ink.core.recipe import document_recipe
        from living_ink.sources import source_for_name

        store.record_publication(
            doc_id,
            dest.state_key,
            version,
            recipe=document_recipe(source_for_name("notebook"), dest, pipe.settings),
        )

    def _preview(self, store, dest, settings):
        """Classify the same items the way ``sync --status`` does."""
        from living_ink.cli import rows_from_selection

        chosen = select(list(self.ITEMS), SelectionCriteria(), store, [dest], settings=settings)
        rows = rows_from_selection(list(self.ITEMS), chosen, store, [dest])
        return {row["id"]: row["status"] for row in rows}

    def test_the_run_processes_exactly_what_the_preview_flagged(self, store):
        dest = MockDestination()
        pipe = self._pipeline(dest)
        self._settle(store, pipe, dest, "doc-changed", "old")
        self._settle(store, pipe, dest, "doc-settled", "h3")

        chosen = pipe.select_documents(list(self.ITEMS), self._client())
        processed = [candidate.doc_id for candidate in chosen.to_process]
        flagged = [
            doc_id
            for doc_id, status in self._preview(store, dest, pipe.settings).items()
            if status.needs_sync
        ]

        assert sorted(processed) == sorted(flagged) == ["doc-changed", "doc-new"]

    def test_a_settled_document_is_left_alone_by_both(self, store):
        dest = MockDestination()
        pipe = self._pipeline(dest)
        for item in self.ITEMS:
            self._settle(store, pipe, dest, item.id, item.hash)

        chosen = pipe.select_documents(list(self.ITEMS), self._client())

        assert chosen.to_process == ()
        assert chosen.deferred == ()
        assert not any(
            status.needs_sync for status in self._preview(store, dest, pipe.settings).values()
        )

    def test_the_run_names_the_destination_that_is_owed(self, store):
        """On the candidate itself, as a tuple — never a falsy stand-in."""
        dest = MockDestination()
        pipe = self._pipeline(dest)
        self._settle(store, pipe, dest, "doc-settled", "h3")

        chosen = pipe.select_documents(list(self.ITEMS), self._client())
        owed = {candidate.doc_id: candidate.pending for candidate in chosen.to_process}

        assert owed["doc-new"] == (dest,)
        assert "doc-settled" not in owed

    def test_the_per_run_cap_shortens_the_run_not_the_preview(self, store):
        """A capped run is not a disagreement: the rest is still owed."""
        dest = MockDestination()
        pipe = self._pipeline(dest, limit=1)

        chosen = pipe.select_documents(list(self.ITEMS), self._client())

        assert len(chosen.to_process) == 1
        assert len(chosen.deferred) == 2
        assert chosen.pending_total == 3

    def test_the_capped_remainder_is_deferred_rather_than_called_unchanged(self, store):
        """The bug this replaced: nine pending notebooks reported as fine."""
        dest = MockDestination()
        pipe = self._pipeline(dest, limit=1)
        pipe.report = RunReport()

        chosen = pipe.select_documents(list(self.ITEMS), self._client())
        pipe._report_selection(chosen)

        assert [entry.status for entry in pipe.report.documents] == [DEFERRED, DEFERRED]


class TestRenderGeometryFollowsTheDevice:
    """A page with no content bounds is a sheet of *this* tablet, not of a rM2."""

    def _reading(self, model):
        """A live USB reading for one known model."""
        from living_ink.devices import DEVICE_PROFILES, SOURCE_USB, DeviceReading
        from living_ink.transport import DeviceInfo

        profile = DEVICE_PROFILES[model]
        return DeviceReading(
            info=DeviceInfo(
                model=profile.name, firmware="3.0", screen=profile.screen, color=profile.color
            ),
            source=SOURCE_USB,
        )

    def _render(self, pipe, job, tmp_path):
        """Render a notebook through its source, the way the pipeline does."""
        from living_ink.sources import NOTEBOOK, SourceBundle

        pipe._render_source(
            job,
            NOTEBOOK,
            SourceBundle(doc_id=job.notebook_id, title=job.notebook, zip_path=tmp_path / "doc.zip"),
        )

    def test_the_panel_is_taken_from_the_profile_table(self):
        from living_ink.devices import DEVICE_PROFILES

        assert DEVICE_PROFILES["reMarkable Paper Pro"].screen == (1620, 2160)
        assert DEVICE_PROFILES["reMarkable 2"].screen == (1404, 1872)

    def test_the_default_stands_in_when_no_device_is_known(self):
        from living_ink.devices import DEFAULT_PROFILE, default_reading

        assert default_reading().info.screen == DEFAULT_PROFILE.screen

    def test_a_different_tablet_does_not_reuse_the_other_ones_renders(self, tmp_path, monkeypatch):
        """The panel is in the cache key, so swapping tablets re-renders."""
        from living_ink.cache import RenderCache

        calls = []

        def fake_render(zip_path, page, **kwargs):
            calls.append(kwargs.get("screen"))
            return b"png"

        monkeypatch.setattr(
            "living_ink.extract.render_page_from_document_zip", fake_render, raising=True
        )
        monkeypatch.setattr(
            "living_ink.extract.get_page_source_hashes", lambda zip_path: ["h1"], raising=True
        )
        monkeypatch.setattr("living_ink.extract.renderer_fingerprint", lambda: "fp", raising=True)
        monkeypatch.setattr(
            "living_ink.extract.get_document_page_count", lambda zip_path: 1, raising=True
        )

        def pipe_for(model):
            pipe = SyncPipeline.__new__(SyncPipeline)
            pipe.device = self._reading(model)
            pipe.settings = Settings(render_background="white")
            pipe.renders = RenderCache(tmp_path / "renders", enabled=True)
            pipe.report = RunReport()
            pipe.keep_temp = False
            pipe._save_page = lambda job, page, data, label="Saved": None
            return pipe

        job = DocumentJob(
            item={},
            notebook="Notes",
            notebook_id="doc-1",
            doc_type="notebook",
            version="v1",
            folder_path="",
            display_title="Notes",
            keep_temp=False,
        )

        self._render(pipe_for("reMarkable 2"), job, tmp_path)
        self._render(pipe_for("reMarkable Paper Pro"), job, tmp_path)
        # Same page, same renderer, same background — only the tablet differs.
        self._render(pipe_for("reMarkable 2"), job, tmp_path)

        assert calls == [(1404, 1872), (1620, 2160)]

    def test_the_device_panel_reaches_the_renderer(self, tmp_path, monkeypatch):
        """The screen is passed down, not just used for the cache key."""
        from living_ink.cache import RenderCache

        seen = {}

        def fake_render(zip_path, page, **kwargs):
            seen.update(kwargs)
            return b"png"

        monkeypatch.setattr(
            "living_ink.extract.render_page_from_document_zip", fake_render, raising=True
        )
        monkeypatch.setattr(
            "living_ink.extract.get_page_source_hashes", lambda zip_path: ["h1"], raising=True
        )
        monkeypatch.setattr(
            "living_ink.extract.get_document_page_count", lambda zip_path: 1, raising=True
        )

        pipe = SyncPipeline.__new__(SyncPipeline)
        pipe.device = self._reading("reMarkable Paper Pro")
        pipe.settings = Settings(render_background="white")
        pipe.renders = RenderCache(tmp_path / "renders", enabled=False)
        pipe.report = RunReport()
        pipe.keep_temp = False
        pipe._save_page = lambda job, page, data, label="Saved": None

        self._render(
            pipe,
            DocumentJob(
                item={},
                notebook="Notes",
                notebook_id="doc-1",
                doc_type="notebook",
                version="v1",
                folder_path="",
                display_title="Notes",
                keep_temp=False,
            ),
            tmp_path,
        )

        assert seen["screen"] == (1620, 2160)


class TestTheVerdictStageIsWiredIntoTheSequence:
    """Stage 7 is asked, is asked in its place, and its answer is what is reported.

    What the verdict *is* belongs to ``core.stages.verdict``; what breaks
    silently is the wiring. Dropped from ``process_notebook_item``, a notebook
    whose every page hit a 429 publishes as a note of nothing but gap markers
    and then writes a ``publications`` row, so the next run sees nothing pending
    and the real text never arrives. Reported with the wrong verdict, a rate
    limit is recorded as a successful skip. Moved one line earlier, a run that
    published nothing also leaves no transcript to say why.
    """

    def _pipeline(self, dest, *, skip_empty=False):
        """Build a pipeline carrying a run report and a resolved ``skip_empty``.

        Args:
            dest: The destination the document is pending for.
            skip_empty: What ``sync.skip_empty`` resolved to for this run.

        Returns:
            The pipeline.
        """
        pipe = SyncPipeline(destinations=[dest])
        pipe.settings = replace(pipe.settings, skip_empty=skip_empty)
        pipe.report = RunReport()
        return pipe

    def _process(
        self,
        pipe,
        pages,
        dest,
        tmp_path,
        monkeypatch,
        *,
        on_transcripts=None,
        on_judge=None,
    ):
        """Drive one document through the real sequence with stages 2-5 stubbed.

        Only the tail of the sequence is under test, so acquiring, tagging,
        preprocessing and OCR are replaced by handing the job the pages they
        would have produced. Stage 7 itself is wrapped rather than replaced:
        the real verdict runs, and the wrapper only records that it was reached.

        Args:
            pipe: The pipeline under test.
            pages: The transcribed pages stage 5 hands on.
            dest: The destination the document is pending for.
            tmp_path: Where the document workspace is allowed to land.
            monkeypatch: Pytest's patcher, for the work directory.
            on_transcripts: Called instead of writing the transcript artifact.
            on_judge: Called just before the real stage 7.

        Returns:
            What ``process_notebook_item`` returned.
        """
        monkeypatch.setattr(pipeline, "WORK_DIR", tmp_path / "work")
        real_judge = pipe._judge_pages

        def judge(job):
            if on_judge is not None:
                on_judge(job)
            return real_judge(job)

        item = make_item("nb-1", "Notes", content_hash="h")
        with (
            patch.object(
                pipe,
                "_acquire_pages",
                side_effect=lambda job, client: job.imgs.extend(
                    Path(f"nb.page-{page.number}.png") for page in pages
                ),
            ),
            patch.object(pipe, "_collect_tags"),
            patch.object(pipe, "_preprocess_images"),
            patch.object(pipe, "_ocr_pages", side_effect=lambda job: job.pages.extend(pages)),
            patch.object(
                pipe,
                "_write_transcripts",
                side_effect=on_transcripts if on_transcripts else lambda job: None,
            ),
            patch.object(pipe, "_judge_pages", side_effect=judge),
        ):
            return pipe.process_notebook_item(
                candidate=make_candidate(item, pending=(dest,)),
                client=MagicMock(),
                keep_temp=True,
            )

    def test_every_page_failing_stops_the_document_short_of_publish(self):
        """A rate-limited notebook has to come back next run, so it is not a success.

        ``_StopProcessing(True)`` here would have the caller record the document
        as done and clear its failure, and the 429'd pages would never be asked
        for again.
        """
        pipe = self._pipeline(MockDestination())
        job = make_job(pages=[make_page(1, error="429 rate limited")])

        with pytest.raises(pipeline._StopProcessing) as err:
            pipe._judge_pages(job)

        assert err.value.success is False

    def test_the_reason_reaches_the_report_under_the_notebooks_name(self, tmp_path, monkeypatch):
        """A summary line reading only "every page failed" names no document.

        On a forty-document nightly run the reason is the only thing tying the
        failure to the notebook the user has to go and look at.
        """
        dest = MockDestination("MockDest")
        pipe = self._pipeline(dest)

        result = self._process(
            pipe,
            [make_page(1, error="429 rate limited"), make_page(2, error="429 rate limited")],
            dest,
            tmp_path,
            monkeypatch,
        )

        entry = pipe.report.documents[0]
        assert result is False
        assert entry.status == FAILED
        assert entry.reason.startswith("Notes: ")
        assert "every page failed" in entry.reason
        assert dest.published == []

    def test_a_blank_document_is_a_skip_the_run_counts_as_a_success(self):
        """The user asked for empty notebooks to be skipped; a skip is not a failure.

        Reported as a failure it would be recorded as broken in the state store
        and shown in red every night for a notebook that is merely unwritten.
        """
        pipe = self._pipeline(MockDestination(), skip_empty=True)
        job = make_job(pages=[make_page(1, text="   ")])

        with pytest.raises(pipeline._StopProcessing) as err:
            pipe._judge_pages(job)

        assert err.value.success is True

    def test_the_blank_skip_publishes_nothing_and_reports_the_skip(self, tmp_path, monkeypatch):
        """An empty notebook must not reach the vault as an empty note."""
        dest = MockDestination("MockDest")
        pipe = self._pipeline(dest, skip_empty=True)

        result = self._process(pipe, [make_page(1, text="")], dest, tmp_path, monkeypatch)

        assert result is True
        assert dest.published == []
        assert pipe.report.documents[0].status == SKIPPED

    def test_the_stage_reads_skip_empty_off_the_runs_settings(self, tmp_path, monkeypatch):
        """The same blank document publishes with the setting off.

        The flag is resolved once into ``Settings``, so ``--skip-empty`` and the
        env var outrank the config file. A stage reading the config section
        directly would answer the file's value whatever the user typed.
        """
        dest = MockDestination("MockDest")
        pipe = self._pipeline(dest, skip_empty=False)

        result = self._process(pipe, [make_page(1, text="")], dest, tmp_path, monkeypatch)

        assert result is True
        assert len(dest.published) == 1

    def test_a_partially_failed_document_is_published_gaps_and_all(self, tmp_path, monkeypatch):
        """Holding 197 good pages hostage to 3 rate-limited ones gives the user nothing.

        The failed pages travel with the document so the destination can leave a
        marked gap for the next run to heal, which only happens if stage 7 lets
        the document through.
        """
        dest = MockDestination("MockDest")
        pipe = self._pipeline(dest)

        result = self._process(
            pipe,
            [make_page(1, text="written"), make_page(2, error="429 rate limited")],
            dest,
            tmp_path,
            monkeypatch,
        )

        assert result is True
        assert len(dest.published) == 1
        assert dest.published[0]["doc"].failed_pages() == 1

    def test_the_transcript_is_written_before_the_verdict_is_taken(self, tmp_path, monkeypatch):
        """A run that published nothing is debugged from the transcript artifact.

        Judging first would be cheaper and would leave a user staring at "every
        page failed" with no file saying what the model actually returned.
        """
        order = []
        dest = MockDestination("MockDest")
        pipe = self._pipeline(dest)

        result = self._process(
            pipe,
            [make_page(1, error="429 rate limited")],
            dest,
            tmp_path,
            monkeypatch,
            on_transcripts=lambda job: order.append("transcripts"),
            on_judge=lambda job: order.append("verdict"),
        )

        assert result is False
        assert order == ["transcripts", "verdict"]
        assert dest.published == []

    def test_an_extracted_text_layer_is_what_the_stage_is_told_about(self):
        """Stage 7 is the only place that knows a PDF brought its own content.

        ``judge_pages`` takes ``has_text`` because both its verdicts mean
        "nothing came back", and a 400-page book whose three annotated pages hit
        a 429 has plenty in hand. Only the job knows that, so the wiring has to
        pass it — dropping the keyword silently reinstates the old behaviour of
        publishing nothing.
        """
        pipe = self._pipeline(MockDestination(), skip_empty=True)
        pages = [make_page(1, error="429 rate limited")]

        pipe._judge_pages(make_job(pages=pages, extracted_doc_text="Chapter 1\n\nIt was..."))

        with pytest.raises(pipeline._StopProcessing):
            pipe._judge_pages(make_job(pages=pages, extracted_doc_text=""))

    def test_a_whitespace_only_text_layer_is_not_content(self):
        """An extractor that yields form feeds has found no text, and must not rescue the run.

        Some PDFs extract to nothing but page separators. Treating that as
        content would report a rate-limited document as published.
        """
        pipe = self._pipeline(MockDestination())
        job = make_job(pages=[make_page(1, error="429 rate limited")], extracted_doc_text="\n\f\n")

        with pytest.raises(pipeline._StopProcessing) as err:
            pipe._judge_pages(job)

        assert err.value.success is False
