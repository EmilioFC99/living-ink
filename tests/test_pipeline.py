"""Tests for living_ink.pipeline module and SyncPipeline class."""

import datetime
import json
import os
import stat
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from living_ink import logs, pipeline
from living_ink.config import ConfigurationMissing, credentials
from living_ink.core.document import Document, PublishContext, PublishResult
from living_ink.destinations import (
    AppleNotesDestination,
    Destination,
    DestinationError,
    DestinationStatus,
    MergeUnit,
    ObsidianDestination,
)
from living_ink.pipeline import (
    LOG_PATH,
    DocumentJob,
    SyncOptions,
    SyncPipeline,
    log,
)
from living_ink.redact import clear_secrets, register_secret
from living_ink.report import (
    FAILED,
    PUBLISHED,
    SKIPPED,
    WOULD_PUBLISH,
    DocumentOutcome,
    RunReport,
)
from living_ink.settings import Settings


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


class TestSyncOptions:
    """Tests for the SyncOptions value object."""

    def test_defaults_are_all_deferrals(self):
        """A bare SyncOptions overrides nothing."""
        opts = SyncOptions()
        assert opts.notebook is None
        assert opts.sync_pdfs is None
        assert opts.sync_epubs is None
        assert opts.all_types is False
        assert opts.keep_temp is False

    def test_from_args_maps_unset_store_true_flags_to_none(self):
        """Unset --sync-pdfs/--sync-epubs must defer to config, not disable them."""
        import argparse as _argparse

        args = _argparse.Namespace(
            notebook="Book", limit=3, sync_pdfs=False, sync_epubs=True, keep_temp=True
        )
        opts = SyncOptions.from_args(args)
        assert opts.notebook == "Book"
        assert opts.limit == 3
        assert opts.sync_pdfs is None
        assert opts.sync_epubs is True
        assert opts.keep_temp is True
        # Fields absent from the namespace fall back to the dataclass defaults.
        assert opts.cloud is False

    def test_merged_with_ignores_none_and_does_not_mutate(self):
        """merged_with returns a new object and skips None overrides."""
        base = SyncOptions(notebook="Original", limit=5)
        derived = base.merged_with(limit=1, notebook=None)
        assert derived.limit == 1
        assert derived.notebook == "Original"
        assert base.limit == 5
        assert derived is not base


def test_sync_pipeline_init_defaults():
    """SyncPipeline initializes with standard configuration and data paths."""
    pipeline = SyncPipeline(SyncOptions(keep_temp=True))
    assert pipeline.keep_temp is True
    assert pipeline.config_path.name == "config.yml"
    assert pipeline.data_dir.exists()


def test_sync_pipeline_custom_destinations():
    """SyncPipeline accepts custom destination instances."""
    mock_dest = MockDestination("Custom")
    pipeline = SyncPipeline(destinations=[mock_dest])
    assert len(pipeline.destinations) == 1
    assert pipeline.destinations[0] is mock_dest


def test_sync_pipeline_properties_all_types():
    """SyncPipeline(all_types=True) enables sync_pdfs and sync_epubs."""
    pipeline = SyncPipeline(SyncOptions(all_types=True))
    assert pipeline.all_types is True
    assert pipeline.sync_pdfs is True
    assert pipeline.sync_epubs is True


def test_sync_pipeline_properties_ssh_and_cloud():
    """SyncPipeline sets connection properties and synchronizes environment."""
    pipeline_ssh = SyncPipeline(SyncOptions(ssh=True))
    assert pipeline_ssh.preferred_connection == "ssh"
    assert pipeline_ssh.use_ssh is True

    pipeline_cloud = SyncPipeline(SyncOptions(cloud=True))
    assert pipeline_cloud.preferred_connection == "cloud"
    assert pipeline_cloud.use_ssh is False


def test_sync_pipeline_folder_override(monkeypatch):
    """SyncPipeline(folder=...) overrides AppleNotes folder in environment and destination."""
    monkeypatch.delenv("APPLE_NOTES_FOLDER", raising=False)
    an_dest = AppleNotesDestination(folder_name="InitialFolder")
    pipeline = SyncPipeline(SyncOptions(folder="WorkNotes"), destinations=[an_dest])

    assert pipeline.folder == "WorkNotes"
    assert an_dest.folder_name == "WorkNotes"


def test_sync_pipeline_discover_documents_filtering():
    """discover_documents respects sync_pdfs and sync_epubs properties."""
    doc_nb = {"ID": "1", "Type": "DocumentType", "VissibleName": "Notebook 1"}
    doc_pdf = {"ID": "2", "Type": "DocumentType", "VissibleName": "Paper"}
    doc_epub = {"ID": "3", "Type": "DocumentType", "VissibleName": "Book"}

    mock_client = MagicMock()
    mock_client.get_meta_items.return_value = [doc_nb, doc_pdf, doc_epub]

    def mock_doc_type(item, client):
        if item["ID"] == "1":
            return "notebook"
        if item["ID"] == "2":
            return "pdf"
        return "epub"

    with patch("living_ink.pipeline.get_document_type", side_effect=mock_doc_type):
        # Default: only notebooks
        pipeline_default = SyncPipeline(
            SyncOptions(sync_pdfs=False, sync_epubs=False), destinations=[]
        )
        items, _ = pipeline_default.discover_documents(mock_client)
        assert len(items) == 1
        assert items[0]["ID"] == "1"

        # All types: notebooks, pdfs, epubs
        pipeline_all = SyncPipeline(SyncOptions(all_types=True), destinations=[])
        items_all, _ = pipeline_all.discover_documents(mock_client)
        assert len(items_all) == 3


def test_sync_pipeline_filter_pending_documents_limit():
    """filter_pending_documents respects self.limit property."""
    items = [
        {"ID": f"doc-{i}", "Type": "DocumentType", "VissibleName": f"Note {i}", "hash": f"h{i}"}
        for i in range(5)
    ]
    id_map = {it["ID"]: it for it in items}

    pipeline = SyncPipeline(SyncOptions(limit=2), destinations=[MockDestination()])
    to_process, needs_update, cont = pipeline.filter_pending_documents(items, id_map)
    assert cont is True
    assert len(to_process) == 2


def test_sync_pipeline_run_no_notebooks():
    """SyncPipeline.run returns True gracefully when no items need updating."""
    pipeline = SyncPipeline(destinations=[MockDestination()])
    with patch("living_ink.pipeline.validate_environment"):
        with patch.object(pipeline, "connect") as mock_connect:
            mock_client = MagicMock()
            mock_connect.return_value = mock_client
            with patch.object(pipeline, "discover_documents", return_value=([], {})):
                result = pipeline.run()
                assert result is True


def test_sync_pipeline_run_targeted_not_found():
    """SyncPipeline.run returns False when a targeted notebook is not in the library."""
    pipeline = SyncPipeline(
        SyncOptions(notebook="NonExistentBook"), destinations=[MockDestination()]
    )
    with patch("living_ink.pipeline.validate_environment"):
        with patch.object(pipeline, "connect") as mock_connect:
            mock_client = MagicMock()
            mock_connect.return_value = mock_client
            with patch.object(pipeline, "discover_documents", return_value=([], {})):
                result = pipeline.run()
                assert result is False


def test_sync_pipeline_run_targeted_user_cancelled():
    """SyncPipeline.run returns True when user cancels interactive disambiguation."""
    doc_item = {
        "ID": "doc-123",
        "Type": "DocumentType",
        "VissibleName": "Meeting Notes",
        "hash": "h1",
    }
    id_map = {"doc-123": doc_item}
    pipeline = SyncPipeline(SyncOptions(notebook="Meeting Notes"), destinations=[MockDestination()])

    with patch("living_ink.pipeline.validate_environment"):
        with patch.object(pipeline, "connect"):
            with patch.object(pipeline, "discover_documents", return_value=([doc_item], id_map)):
                with patch("living_ink.pipeline.select_notebook_interactive", return_value=[]):
                    result = pipeline.run()
                    assert result is True


def test_sync_pipeline_process_notebook_item():
    """process_notebook_item processes empty notebook without error."""
    mock_dest = MockDestination("MockDest")
    pipeline = SyncPipeline(destinations=[mock_dest])

    nb_item = {
        "ID": "nb-001",
        "Type": "DocumentType",
        "VissibleName": "Test Notebook",
        "hash": "hash-abc",
    }
    mock_client = MagicMock()
    mock_client.download.return_value = b""
    id_map = {"nb-001": nb_item}
    needs_update = {"nb-001": [mock_dest]}

    with patch("living_ink.pipeline.get_document_type", return_value="notebook"):
        success = pipeline.process_notebook_item(
            nb_item=nb_item,
            client=mock_client,
            id_map=id_map,
            needs_update=needs_update,
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
        monkeypatch.setattr(pipeline, "WHITE_DIR", tmp_path / "data" / "white")
        monkeypatch.setattr(pipeline, "LOGS_DIR", tmp_path / "logs")

        pipeline.ensure_runtime_dirs()

        assert (tmp_path / "data").is_dir()
        assert (tmp_path / "data" / "white").is_dir()
        assert (tmp_path / "logs").is_dir()


def make_job(**overrides) -> DocumentJob:
    """Build a DocumentJob with harmless defaults for the field under test."""
    fields = {
        "item": {"ID": "nb-1"},
        "notebook": "Test Notebook",
        "notebook_id": "nb-1",
        "doc_type": "notebook",
        "version": "hash-1",
        "safe_name": "Test_Notebook",
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


class TestJobHelpers:
    """Small pure helpers the stages rely on."""

    def test_version_prefers_the_content_hash(self):
        assert pipeline._item_version({"hash": "abc", "Version": "3"}) == "abc"

    def test_version_falls_back_to_the_integer_version(self):
        assert pipeline._item_version({"Version": "7"}) == 7

    def test_version_defaults_to_one_when_unusable(self):
        assert pipeline._item_version({"Version": "not-a-number"}) == 1


class TestRendererDispatch:
    """Document type selects the renderer; unknown types render as notebooks."""

    def test_pdf_and_epub_have_their_own_renderers(self):
        assert SyncPipeline._RENDERERS["pdf"] is SyncPipeline._render_pdf
        assert SyncPipeline._RENDERERS["epub"] is SyncPipeline._render_epub

    def test_anything_else_renders_as_a_notebook(self):
        assert SyncPipeline._RENDERERS.get("notebook") is None
        assert SyncPipeline._RENDERERS.get("djvu") is None


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


class TestPageConcurrency:
    """Pages are transcribed several at a time, but always reported in order."""

    def _pipeline(self, concurrency: int) -> SyncPipeline:
        p = SyncPipeline(destinations=[])
        p.settings = replace(p.settings, ocr_concurrency=concurrency)
        return p

    def test_results_stay_in_page_order(self):
        """A slow first page must not end up after a fast last page."""
        pipeline_obj = self._pipeline(4)
        paths = [Path(f"page-{i}.png") for i in range(4)]

        def transcribe(path):
            # Earlier pages finish last, which reorders anything unordered.
            time.sleep(0.05 * (len(paths) - int(path.stem.split("-")[1])))
            return path.name

        with patch.object(pipeline_obj, "_transcribe_page", side_effect=transcribe):
            results = pipeline_obj._transcribe_pages(paths)

        assert results == [p.name for p in paths]

    def test_pages_are_transcribed_concurrently(self):
        """Four pages at width four take about one page's time, not four."""
        pipeline_obj = self._pipeline(4)
        paths = [Path(f"page-{i}.png") for i in range(4)]

        def transcribe(path):
            time.sleep(0.1)
            return ""

        with patch.object(pipeline_obj, "_transcribe_page", side_effect=transcribe):
            started = time.monotonic()
            pipeline_obj._transcribe_pages(paths)
            elapsed = time.monotonic() - started

        assert elapsed < 0.3, f"pages appear to have run serially ({elapsed:.2f}s)"

    def test_concurrency_of_one_runs_serially(self):
        pipeline_obj = self._pipeline(1)
        paths = [Path("a.png"), Path("b.png")]
        in_flight = []

        def transcribe(path):
            in_flight.append(path.name)
            assert len(in_flight) == 1
            in_flight.pop()
            return path.name

        with patch.object(pipeline_obj, "_transcribe_page", side_effect=transcribe):
            results = pipeline_obj._transcribe_pages(paths)

        assert results == ["a.png", "b.png"]

    def test_no_pages_needs_no_workers(self):
        assert self._pipeline(4)._transcribe_pages([]) == []

    def test_the_vision_result_is_the_transcript(self):
        pipeline_obj = self._pipeline(1)

        with patch.object(pipeline_obj, "_vision_ocr_page", return_value="clean text"):
            assert pipeline_obj._transcribe_page(Path("p.png")) == ("clean text", None)

    def test_an_empty_vision_result_has_nowhere_left_to_fall_back_to(self):
        """One backend: a page the model could not read is an empty page."""
        pipeline_obj = self._pipeline(1)

        with patch.object(pipeline_obj, "_vision_ocr_page", return_value=""):
            assert pipeline_obj._transcribe_page(Path("p.png")) == ("", None)


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

    def test_a_raising_page_reports_the_reason_instead_of_propagating(self):
        pipe = self._pipeline()

        with patch.object(pipe, "_vision_ocr_page", side_effect=RuntimeError("429 rate limited")):
            text, error = pipe._transcribe_page(Path("p.png"))

        assert text == ""
        assert "429 rate limited" in error

    def test_the_error_names_the_exception_type(self):
        pipe = self._pipeline()

        with patch.object(pipe, "_vision_ocr_page", side_effect=OSError("truncated")):
            _, error = pipe._transcribe_page(Path("p.png"))

        assert error.startswith("OSError:")

    def test_a_key_in_the_message_is_redacted_before_it_reaches_the_user(self):
        """The reason is printed and put in the report; a provider URL can carry a key."""
        pipe = self._pipeline()
        register_secret("sk-supersecret")

        try:
            with patch.object(pipe, "_vision_ocr_page", side_effect=RuntimeError("sk-supersecret")):
                _, error = pipe._transcribe_page(Path("p.png"))
        finally:
            clear_secrets()

        assert "sk-supersecret" not in error

    def test_one_bad_page_does_not_cost_the_other_two(self):
        pipe = self._pipeline()
        job = self._job_with_pages(3)

        def read(path):
            if path.name.endswith("page-2.png"):
                raise RuntimeError("boom")
            return "text"

        with patch.object(pipe, "_vision_ocr_page", side_effect=read):
            pipe._ocr_pages(job)

        assert [p.text for p in job.pages] == ["text", "", "text"]
        assert "boom" in job.pages[1].error

    def test_the_failure_is_recorded_on_the_page_that_failed(self):
        pipe = self._pipeline()
        job = self._job_with_pages(2)

        with patch.object(pipe, "_transcribe_page", side_effect=[("a", None), ("", "boom")]):
            pipe._ocr_pages(job)

        assert job.pages[0].error is None
        assert job.pages[1].error == "boom"

    def test_a_blank_page_is_left_unmarked(self):
        pipe = self._pipeline()
        job = self._job_with_pages(1)

        with patch.object(pipe, "_transcribe_page", return_value=("", None)):
            pipe._ocr_pages(job)

        assert job.pages[0].error is None

    def test_failures_are_counted(self):
        pipe = self._pipeline()
        job = self._job_with_pages(2)

        with patch.object(pipe, "_transcribe_page", side_effect=[("", "boom"), ("", "boom")]):
            pipe._ocr_pages(job)

        assert job.failed_pages == 2

    def test_the_run_report_names_the_page_not_just_the_document(self):
        pipe = self._pipeline()
        pipe.report = RunReport()
        job = self._job_with_pages(2)

        with patch.object(pipe, "_transcribe_page", side_effect=[("a", None), ("", "boom")]):
            pipe._ocr_pages(job)

        assert any("Page 2" in w and "boom" in w for w in pipe.report.warnings)

    def test_the_transcript_shows_the_reason_where_the_text_would_be(self, tmp_path, monkeypatch):
        """A reader of the transcript sees a gap, not a page that was blank."""
        monkeypatch.setattr(pipeline, "OCR_DIR", tmp_path)
        pipe = self._pipeline()
        job = self._job_with_pages(2)
        job.pages = [
            replace(job.pages[0], text="written"),
            replace(job.pages[1], error="429 rate limited"),
        ]

        pipe._write_transcripts(job)

        assert "429 rate limited" in job.clean_out_txt.read_text(encoding="utf-8")


class TestDryRun:
    """A dry run transcribes as usual, then publishes and records nothing."""

    def _job(self, tmp_path) -> DocumentJob:
        transcript = tmp_path / "Notes_clean.txt"
        transcript.write_text('{"notebook": "Notes"}\n\n### Page 1\n\nHello\n')
        return make_job(folder_path="Work", clean_out_txt=transcript)

    def test_nothing_is_published(self, tmp_path):
        dest = MockDestination("MockDest")
        pipeline_obj = SyncPipeline(options=SyncOptions(dry_run=True), destinations=[dest])

        with patch("living_ink.pipeline.add_to_processed_log") as recorded:
            assert pipeline_obj._publish(self._job(tmp_path), {"nb-1": [dest]}) is True

        assert dest.published == []
        recorded.assert_not_called()

    def test_it_reports_where_the_transcript_landed(self, tmp_path, capsys):
        dest = MockDestination("MockDest")
        pipeline_obj = SyncPipeline(options=SyncOptions(dry_run=True), destinations=[dest])
        job = self._job(tmp_path)

        pipeline_obj._publish(job, {"nb-1": [dest]})
        out = capsys.readouterr().out

        assert "Dry run" in out
        assert str(job.clean_out_txt) in out

    def test_it_says_how_much_of_an_existing_note_would_be_rewritten(self, tmp_path, capsys):
        """The whole-note promise is what a user needs before the run, not after."""
        dest = MockDestination("MockDest")
        pipeline_obj = SyncPipeline(options=SyncOptions(dry_run=True), destinations=[dest])

        pipeline_obj._publish(self._job(tmp_path), {"nb-1": [dest]})

        assert "Replaces the whole note" in capsys.readouterr().out

    def test_a_page_level_destination_promises_something_different(self, tmp_path, capsys):
        dest = MockDestination("MockDest")
        pipeline_obj = SyncPipeline(options=SyncOptions(dry_run=True), destinations=[dest])

        with patch.object(type(dest), "merge_unit", MergeUnit.PAGE):
            pipeline_obj._publish(self._job(tmp_path), {"nb-1": [dest]})

        out = capsys.readouterr().out
        assert "only the pages that changed" in out
        assert "whole note" not in out

    def test_a_normal_run_still_publishes(self, tmp_path):
        dest = MockDestination("MockDest")
        pipeline_obj = SyncPipeline(destinations=[dest])

        with patch("living_ink.pipeline.add_to_processed_log") as recorded:
            assert pipeline_obj._publish(self._job(tmp_path), {"nb-1": [dest]}) is True

        assert len(dest.published) == 1
        recorded.assert_called_once()

    def test_dry_run_keeps_the_artifacts_it_points_at(self):
        assert SyncPipeline(options=SyncOptions(dry_run=True)).keep_temp is True
        assert SyncPipeline(options=SyncOptions()).keep_temp is False

    def test_the_flag_reaches_the_options(self):
        args = SimpleNamespace(dry_run=True)
        assert SyncOptions.from_args(args).dry_run is True
        assert SyncOptions.from_args(SimpleNamespace()).dry_run is False


class TestEveryRunReadsItsPages:
    """A leftover transcript on disk is not a shortcut around the OCR stages.

    It used to be: a transcript newer than its pages was adopted whole and
    stages 4-6 were skipped. But the note's text comes from the pages now, and
    that path never filled them in — so it published a transcript-shaped file
    and an empty note. Free repeats come from the transcript cache, which is
    keyed by page bytes and cannot go stale.
    """

    def test_a_leftover_transcript_does_not_skip_ocr(self, tmp_path, monkeypatch):
        white = tmp_path / "white"
        ocr = tmp_path / "ocr"
        white.mkdir()
        ocr.mkdir()
        monkeypatch.setattr(pipeline, "OCR_DIR", ocr)

        page = white / "Notes.page-1.png"
        page.write_bytes(b"png")
        transcript = ocr / "Notes_clean.txt"
        transcript.write_text('{"notebook": "Notes"}\n\n### Page 1\n\nHello\n')
        os.utime(transcript, (page.stat().st_mtime + 10, page.stat().st_mtime + 10))

        pipeline_obj = SyncPipeline(destinations=[MockDestination()])
        nb_item = {"ID": "nb-1", "VissibleName": "Notes", "hash": "h"}

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
                pipeline_obj.process_notebook_item(nb_item, MagicMock(), {}, {}, keep_temp=True)
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
        assert "Tightened permissions" in capsys.readouterr().out

    def test_an_already_private_config_is_left_alone(self, tmp_path, capsys):
        cfg = self._write_config(tmp_path, 0o600)
        pipeline.load_yaml_config(cfg)
        assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
        assert "Tightened permissions" not in capsys.readouterr().out

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
        cfg = self._write(tmp_path, "apple_notes:\n  enabled: true\n")
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
        monkeypatch.setattr(pipeline, "ROOT", tmp_path)
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
        pipeline.add_to_processed_log("Obsidian", "doc-1", "v1")
        pipeline.add_to_processed_log("Obsidian", "doc-2", "v9")
        assert pipeline.load_processed_log("Obsidian") == {"doc-1": "v1", "doc-2": "v9"}

    def test_republishing_updates_rather_than_duplicating(self, tmp_path, monkeypatch):
        self._state_dir(tmp_path, monkeypatch)
        pipeline.add_to_processed_log("Obsidian", "doc-1", "v1")
        pipeline.add_to_processed_log("Obsidian", "doc-1", "v2")
        assert pipeline.load_processed_log("Obsidian") == {"doc-1": "v2"}

    def test_destinations_do_not_share_state(self, tmp_path, monkeypatch):
        """A notebook can be published to Obsidian and still pending for Notes."""
        self._state_dir(tmp_path, monkeypatch)
        pipeline.add_to_processed_log("Obsidian", "doc-1", "v1")
        assert pipeline.load_processed_log("AppleNotes") == {}

    def test_state_survives_a_restart(self, tmp_path, monkeypatch):
        self._state_dir(tmp_path, monkeypatch)
        pipeline.add_to_processed_log("Obsidian", "doc-1", "v1")
        pipeline.reset_state_store()
        assert pipeline.load_processed_log("Obsidian") == {"doc-1": "v1"}

    def test_a_second_process_sees_the_write(self, tmp_path, monkeypatch):
        """`watch` and a manual sync used to overwrite each other's progress."""
        from living_ink import state

        self._state_dir(tmp_path, monkeypatch)
        pipeline.add_to_processed_log("Obsidian", "doc-1", "v1")

        with state.StateStore(pipeline.get_state_db_path()) as other:
            other.record_publication("doc-2", "Obsidian", "v2")

        assert pipeline.load_processed_log("Obsidian") == {"doc-1": "v1", "doc-2": "v2"}

    def test_legacy_json_state_is_imported_once(self, tmp_path, monkeypatch):
        self._state_dir(tmp_path, monkeypatch)
        legacy = tmp_path / "processed_notebooks_Obsidian.json"
        legacy.write_text('{"doc-1": 7}', encoding="utf-8")

        assert pipeline.load_processed_log("Obsidian") == {"doc-1": "7"}
        # Renamed rather than deleted, so a downgrade still has the state.
        assert not legacy.exists()
        assert (tmp_path / "processed_notebooks_Obsidian.json.migrated").exists()


class TestLogRedaction:
    """pipeline.log writes the file users attach to bug reports."""

    def test_a_registered_secret_never_reaches_the_log_file(self, tmp_path, monkeypatch, capsys):
        from living_ink import redact as redact_mod

        redact_mod.clear_secrets()
        redact_mod.register_secret("rm-device-token-abcdef123456")
        log_path = tmp_path / "pipeline.log"
        monkeypatch.setattr(pipeline, "LOG_PATH", log_path)
        monkeypatch.setattr(pipeline, "ensure_runtime_dirs", lambda: None)

        try:
            pipeline.log("connecting with rm-device-token-abcdef123456")
        finally:
            redact_mod.clear_secrets()

        written = log_path.read_text(encoding="utf-8")
        assert "rm-device-token-abcdef123456" not in written
        assert "***redacted***" in written
        # The same masked text is what the user saw on screen.
        assert "rm-device-token-abcdef123456" not in capsys.readouterr().out

    def test_ordinary_messages_are_untouched(self, tmp_path, monkeypatch):
        log_path = tmp_path / "pipeline.log"
        monkeypatch.setattr(pipeline, "LOG_PATH", log_path)
        monkeypatch.setattr(pipeline, "ensure_runtime_dirs", lambda: None)
        pipeline.log("Publishing Meeting Notes")
        assert "Publishing Meeting Notes" in log_path.read_text(encoding="utf-8")

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


class TestLogPersistence:
    """The log used to be truncated at the top of every run()."""

    @pytest.fixture(autouse=True)
    def _isolated(self):
        from living_ink import logs

        logs.reset_handlers()
        yield
        logs.reset_handlers()

    def test_a_new_run_keeps_the_previous_run(self, tmp_path, monkeypatch):
        """`watch` calls run() every interval; it used to keep only the last."""
        from living_ink import logs

        log_path = tmp_path / "pipeline.log"
        monkeypatch.setattr(pipeline, "LOG_PATH", log_path)
        monkeypatch.setattr(pipeline, "ensure_runtime_dirs", lambda: None)

        pipeline.log("connection refused")
        logs.mark_run_start()
        pipeline.log("all good")

        written = log_path.read_text(encoding="utf-8")
        assert "connection refused" in written
        assert "all good" in written

    def test_log_is_silent_on_the_console_when_quiet(self, tmp_path, monkeypatch, capsys):
        from living_ink import logs

        log_path = tmp_path / "pipeline.log"
        monkeypatch.setattr(pipeline, "LOG_PATH", log_path)
        monkeypatch.setattr(pipeline, "ensure_runtime_dirs", lambda: None)
        logs.configure(log_path, quiet=True)
        try:
            pipeline.log("Publishing Meeting Notes")
            assert capsys.readouterr().out == ""
            assert "Publishing Meeting Notes" in log_path.read_text(encoding="utf-8")
        finally:
            logs._console_mode = logs.ConsoleMode.PLAIN


class TestExternalIdRoundTrip:
    """The id a destination assigns has to survive until the next sync."""

    @pytest.fixture(autouse=True)
    def _state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path)
        monkeypatch.setattr(pipeline, "ROOT", tmp_path)
        monkeypatch.setattr(pipeline, "ensure_runtime_dirs", lambda: None)
        pipeline.reset_state_store()
        yield
        pipeline.reset_state_store()

    def test_an_id_is_stored_with_the_publication(self):
        pipeline.add_to_processed_log(
            "AppleNotesDestination", "doc-1", "v1", external_id="x-coredata://p7"
        )
        record = pipeline.get_state_store().get_publication("doc-1", "AppleNotesDestination")
        assert record["external_id"] == "x-coredata://p7"

    def test_a_later_sync_without_an_id_keeps_the_old_one(self):
        """A destination that fails to report an id must not erase the record."""
        pipeline.add_to_processed_log(
            "AppleNotesDestination", "doc-1", "v1", external_id="x-coredata://p7"
        )
        pipeline.add_to_processed_log("AppleNotesDestination", "doc-1", "v2")

        record = pipeline.get_state_store().get_publication("doc-1", "AppleNotesDestination")
        assert record["external_id"] == "x-coredata://p7"
        assert record["version"] == "v2"


class TestOutcomeRecording:
    """A document that failed has to say so until it succeeds."""

    @pytest.fixture(autouse=True)
    def _state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path)
        monkeypatch.setattr(pipeline, "ROOT", tmp_path)
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
        from living_ink.redact import clear_secrets, register_secret

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


class TestTranscriptionCaching:
    """A page already paid for is never paid for twice."""

    @pytest.fixture
    def page(self, tmp_path):
        """A file standing in for a prepared page image."""
        path = tmp_path / "page-1.png"
        path.write_bytes(b"fake png bytes")
        return path

    def _pipeline(self, tmp_path, enabled=True):
        """A pipeline whose cache is a throwaway directory."""
        from living_ink.cache import TranscriptCache

        pipe = SyncPipeline.__new__(SyncPipeline)
        pipe.cache = TranscriptCache(tmp_path / "transcripts", enabled=enabled)
        pipe._cache_lock = threading.Lock()
        pipe._cache_hits = 0
        pipe._cache_misses = 0
        return pipe

    def test_the_first_read_calls_the_provider(self, tmp_path, page):
        pipe = self._pipeline(tmp_path)
        with patch.object(pipe, "_vision_ocr_page", return_value="text") as ocr:
            assert pipe._transcribe_page(page) == ("text", None)
        assert ocr.call_count == 1

    def test_the_second_read_does_not(self, tmp_path, page):
        """The whole point: an unchanged page costs nothing the next time."""
        pipe = self._pipeline(tmp_path)
        with patch.object(pipe, "_vision_ocr_page", return_value="text"):
            pipe._transcribe_page(page)

        with patch.object(pipe, "_vision_ocr_page") as ocr:
            assert pipe._transcribe_page(page) == ("text", None)
        ocr.assert_not_called()
        assert pipe._cache_hits == 1

    def test_a_run_interrupted_mid_notebook_only_pays_for_what_it_missed(self, tmp_path):
        """Each page is banked as it comes back, so Ctrl+C loses one page."""
        pages = []
        for index in range(4):
            path = tmp_path / f"page-{index}.png"
            path.write_bytes(f"page {index}".encode("utf-8"))
            pages.append(path)

        pipe = self._pipeline(tmp_path)
        pipe.settings = SimpleNamespace(ocr_concurrency=1)

        def transcribe_then_quit(path):
            if path == pages[2]:
                raise KeyboardInterrupt
            return path.name

        with patch.object(pipe, "_vision_ocr_page", side_effect=transcribe_then_quit):
            with pytest.raises(KeyboardInterrupt):
                pipe._transcribe_pages(pages)

        resumed = self._pipeline(tmp_path)
        resumed.settings = SimpleNamespace(ocr_concurrency=1)
        with patch.object(resumed, "_vision_ocr_page", side_effect=lambda p: p.name) as ocr:
            resumed._transcribe_pages(pages)

        # Pages 0 and 1 were banked before the interrupt; only 2 and 3 are paid for.
        assert [call.args[0] for call in ocr.call_args_list] == pages[2:]
        assert resumed._cache_hits == 2

    def test_a_cache_survives_a_new_pipeline(self, tmp_path, page):
        """Entries outlive the run, which is what the temp purge does not."""
        with patch.object(SyncPipeline, "_vision_ocr_page", return_value="text"):
            self._pipeline(tmp_path)._transcribe_page(page)

        second = self._pipeline(tmp_path)
        with patch.object(second, "_vision_ocr_page") as ocr:
            assert second._transcribe_page(page) == ("text", None)
        ocr.assert_not_called()

    def test_an_edited_page_is_read_again(self, tmp_path, page):
        pipe = self._pipeline(tmp_path)
        with patch.object(pipe, "_vision_ocr_page", return_value="text"):
            pipe._transcribe_page(page)

        page.write_bytes(b"different png bytes")
        with patch.object(pipe, "_vision_ocr_page", return_value="new text") as ocr:
            assert pipe._transcribe_page(page) == ("new text", None)
        assert ocr.call_count == 1

    def test_an_entry_from_the_two_backend_era_is_not_served(self, tmp_path, page):
        """Its payload is a raw/clean pair, and neither half is this build's answer."""
        pipe = self._pipeline(tmp_path)
        key = pipe._cache_key(page)
        pipe.cache._write(key, b'{"raw": "google text", "clean": "repaired text"}')

        with patch.object(pipe, "_vision_ocr_page", return_value="vision text") as ocr:
            assert pipe._transcribe_page(page) == ("vision text", None)
        assert ocr.call_count == 1

    def test_an_empty_transcription_is_not_cached(self, tmp_path, page):
        """A blank page is usually a rate limit, and must not become permanent."""
        pipe = self._pipeline(tmp_path)
        with patch.object(pipe, "_vision_ocr_page", return_value=""):
            pipe._transcribe_page(page)

        with patch.object(pipe, "_vision_ocr_page", return_value="text") as ocr:
            assert pipe._transcribe_page(page) == ("text", None)
        assert ocr.call_count == 1

    def test_a_disabled_cache_reads_every_time(self, tmp_path, page):
        pipe = self._pipeline(tmp_path, enabled=False)
        with patch.object(pipe, "_vision_ocr_page", return_value="text") as ocr:
            pipe._transcribe_page(page)
            pipe._transcribe_page(page)
        assert ocr.call_count == 2
        assert not pipe.cache.root.exists()

    def test_an_unreadable_page_is_transcribed_uncached(self, tmp_path):
        """No bytes to hash means no key; transcribe rather than fail."""
        pipe = self._pipeline(tmp_path)
        missing = tmp_path / "gone.png"
        with patch.object(pipe, "_vision_ocr_page", return_value="text") as ocr:
            assert pipe._transcribe_page(missing) == ("text", None)
        assert ocr.call_count == 1
        assert not pipe.cache.root.exists()


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
            safe_name="Notes",
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
        pipe.saved = []
        pipe._save_page = lambda job, page, data, label="Saved": pipe.saved.append((page, data))
        return pipe

    def _job(self) -> DocumentJob:
        return DocumentJob(
            item={},
            notebook="Notes",
            notebook_id="doc-1",
            doc_type="notebook",
            version="v1",
            safe_name="Notes",
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

        pipe._render_zip_pages(self._job(), tmp_path / "doc.zip", 2)

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

        pipe._render_zip_pages(job, tmp_path / "doc.zip", 2)

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

        pipe._render_zip_pages(self._job(), tmp_path / "doc.zip", 2)

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
        self._pipeline(tmp_path)._render_zip_pages(self._job(), tmp_path / "doc.zip", 2)
        assert rendered == []

        broken = False
        again = self._pipeline(tmp_path)
        again._render_zip_pages(self._job(), tmp_path / "doc.zip", 2)
        assert rendered == [1, 2]

    def test_the_first_run_renders_every_page(self, tmp_path, rendered):
        pipe = self._pipeline(tmp_path)
        pipe._render_zip_pages(self._job(), tmp_path / "doc.zip", 2)
        assert rendered == [1, 2]

    def test_the_second_run_renders_nothing(self, tmp_path, rendered):
        pipe = self._pipeline(tmp_path)
        pipe._render_zip_pages(self._job(), tmp_path / "doc.zip", 2)
        rendered.clear()

        again = self._pipeline(tmp_path)
        again._render_zip_pages(self._job(), tmp_path / "doc.zip", 2)
        assert rendered == []

    def test_a_cached_page_is_still_saved(self, tmp_path, rendered):
        pipe = self._pipeline(tmp_path)
        pipe._render_zip_pages(self._job(), tmp_path / "doc.zip", 2)

        again = self._pipeline(tmp_path)
        again._render_zip_pages(self._job(), tmp_path / "doc.zip", 2)
        assert again.saved == [(1, b"png-1"), (2, b"png-2")]

    def test_only_the_changed_page_is_re_rendered(self, tmp_path, rendered, monkeypatch):
        """This is the whole point: an edited notebook costs one page, not all."""
        pipe = self._pipeline(tmp_path)
        pipe._render_zip_pages(self._job(), tmp_path / "doc.zip", 2)
        rendered.clear()

        monkeypatch.setattr(
            "living_ink.extract.get_page_source_hashes",
            lambda zip_path: ["hash-1", "hash-2-edited"],
            raising=True,
        )
        again = self._pipeline(tmp_path)
        again._render_zip_pages(self._job(), tmp_path / "doc.zip", 2)
        assert rendered == [2]

    def test_a_renderer_upgrade_re_renders_everything(self, tmp_path, rendered, monkeypatch):
        pipe = self._pipeline(tmp_path)
        pipe._render_zip_pages(self._job(), tmp_path / "doc.zip", 2)
        rendered.clear()

        monkeypatch.setattr("living_ink.extract.renderer_fingerprint", lambda: "fp2")
        again = self._pipeline(tmp_path)
        again._render_zip_pages(self._job(), tmp_path / "doc.zip", 2)
        assert rendered == [1, 2]

    def test_a_new_background_re_renders_everything(self, tmp_path, rendered):
        """The background is baked into the PNG, so it belongs in the key."""
        pipe = self._pipeline(tmp_path)
        pipe._render_zip_pages(self._job(), tmp_path / "doc.zip", 2)
        rendered.clear()

        again = self._pipeline(tmp_path, background="yellow")
        again._render_zip_pages(self._job(), tmp_path / "doc.zip", 2)
        assert rendered == [1, 2]

    def test_the_background_reaches_the_renderer(self, tmp_path, monkeypatch):
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
        pipe._render_zip_pages(self._job(), tmp_path / "doc.zip", 1)

        assert seen["background_color"] == "#123456"

    def test_a_disabled_cache_renders_every_time(self, tmp_path, rendered):
        pipe = self._pipeline(tmp_path, enabled=False)
        pipe._render_zip_pages(self._job(), tmp_path / "doc.zip", 2)
        rendered.clear()

        again = self._pipeline(tmp_path, enabled=False)
        again._render_zip_pages(self._job(), tmp_path / "doc.zip", 2)
        assert rendered == [1, 2]

    def test_a_page_that_fails_to_render_is_skipped_not_cached(self, tmp_path, monkeypatch):
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
        pipe._render_zip_pages(self._job(), tmp_path / "doc.zip", 2)
        assert [page for page, _ in pipe.saved] == [1]
        assert pipe.renders.stats()[0] == 1

    def test_a_page_with_no_source_hash_is_rendered_anyway(self, tmp_path, rendered, monkeypatch):
        """An unhashable page loses the cache, not the render."""
        monkeypatch.setattr(
            "living_ink.extract.get_page_source_hashes", lambda zip_path: ["", "hash-2"]
        )
        pipe = self._pipeline(tmp_path)
        pipe._render_zip_pages(self._job(), tmp_path / "doc.zip", 2)
        assert rendered == [1, 2]
        assert pipe.renders.stats()[0] == 1


class TestPublicationIdentity:
    """A destination is told which document it is publishing, and where it put it."""

    def _job(self, tmp_path) -> DocumentJob:
        clean = tmp_path / "Notes_clean.txt"
        clean.write_text("Transcript", encoding="utf-8")
        return DocumentJob(
            item={"ID": "nb-1"},
            notebook="Notes",
            notebook_id="nb-1",
            doc_type="notebook",
            version="hash-1",
            safe_name="Notes",
            folder_path="",
            display_title="Notes",
            keep_temp=False,
            clean_out_txt=clean,
        )

    def test_the_document_id_reaches_the_destination(self, tmp_path):
        dest = MockDestination("MockDest")
        pipe = SyncPipeline(destinations=[dest])

        with patch("living_ink.pipeline.add_to_processed_log"):
            pipe._publish(self._job(tmp_path), {"nb-1": [dest]})

        assert dest.published[0]["doc"].doc_id == "nb-1"

    def test_a_destinations_warning_reaches_the_run_summary(self, tmp_path):
        """A log line scrolls past; the summary is the last thing on screen."""
        dest = MockDestination("MockDest")
        dest.publish_warnings = ("Notes (2).md belongs to another document.",)
        pipe = SyncPipeline(destinations=[dest])
        pipe.report = RunReport()

        with patch("living_ink.pipeline.add_to_processed_log"):
            pipe._publish(self._job(tmp_path), {"nb-1": [dest]})

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
            pipe._publish(self._job(tmp_path), {"nb-1": [dest]})

        assert pipe.report.warnings == ["MockDestination: The vault is read-only."]

    def test_where_the_note_landed_is_recorded(self, tmp_path):
        dest = MockDestination("MockDest")
        dest.publish_target = "Work/Notes.md"
        pipe = SyncPipeline(destinations=[dest])

        with patch("living_ink.pipeline.add_to_processed_log") as recorded:
            pipe._publish(self._job(tmp_path), {"nb-1": [dest]})

        assert recorded.call_args.kwargs["target"] == "Work/Notes.md"

    def test_the_tablets_modification_date_reaches_the_destination(self, tmp_path):
        """It used to be fetched for one log line and then thrown away."""
        dest = MockDestination("MockDest")
        job = self._job(tmp_path)
        job.item = {"ID": "nb-1", "ModifiedClient": "2026-03-04T09:30:00"}
        pipe = SyncPipeline(destinations=[dest])

        with patch("living_ink.pipeline.add_to_processed_log"):
            pipe._publish(job, {"nb-1": [dest]})

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
        store.record_publication(doc_id, dest, "v1", target=f"{name}.md")

    def _pipeline(self, dest, prune=False, dry_run=False):
        pipe = SyncPipeline.__new__(SyncPipeline)
        pipe.dry_run = dry_run
        pipe.prune = prune
        pipe.destinations = [dest]
        pipe.report = None
        pipe.settings = None
        return pipe

    def test_a_missing_notebook_is_reported(self, capsys):
        self._published()
        self._pipeline(MockDestination())._handle_orphans({"nb-2": object()})
        out = capsys.readouterr().out
        assert "no longer on the tablet" in out
        assert "Old Notes" in out

    def test_reporting_deletes_nothing(self, capsys):
        self._published()
        dest = MockDestination()
        self._pipeline(dest)._handle_orphans({"nb-2": object()})

        assert dest.unpublished == []
        assert pipeline.get_state_store().get_publication("nb-1", "MockDestination") is not None

    def test_a_notebook_still_on_the_tablet_is_not_an_orphan(self, capsys):
        self._published()
        self._pipeline(MockDestination())._handle_orphans({"nb-1": object()})
        assert "no longer on the tablet" not in capsys.readouterr().out

    def test_an_empty_listing_is_never_treated_as_a_deletion(self, capsys):
        """A transport that returned nothing has not told us the tablet is empty."""
        self._published()
        self._pipeline(MockDestination())._handle_orphans({})
        assert capsys.readouterr().out == ""

    def test_a_dry_run_says_nothing_and_does_nothing(self, capsys):
        self._published()
        dest = MockDestination()
        self._pipeline(dest, prune=True, dry_run=True)._handle_orphans({"nb-2": object()})

        assert capsys.readouterr().out == ""
        assert dest.unpublished == []

    def test_pruning_deletes_the_note(self, capsys):
        self._published()
        dest = MockDestination()
        self._pipeline(dest, prune=True)._handle_orphans({"nb-2": object()})

        assert dest.unpublished == [("Old Notes.md", None, "nb-1")]

    def test_pruning_forgets_the_document(self, capsys):
        self._published()
        self._pipeline(MockDestination(), prune=True)._handle_orphans({"nb-2": object()})
        assert pipeline.get_state_store().get_publication("nb-1", "MockDestination") is None

    def test_a_destination_that_refuses_is_still_forgotten(self, capsys):
        """Otherwise the same orphan is reported again on every single run."""
        self._published()
        dest = MockDestination()
        dest.unpublish_result = False
        self._pipeline(dest, prune=True)._handle_orphans({"nb-2": object()})

        assert pipeline.get_state_store().get_publication("nb-1", "MockDestination") is None
        assert "left alone" in capsys.readouterr().out

    def test_a_destination_error_does_not_stop_the_run(self, capsys):
        self._published()
        dest = MockDestination()
        dest.unpublish_error = DestinationError("vault is gone")
        self._pipeline(dest, prune=True)._handle_orphans({"nb-2": object()})

        assert "vault is gone" in capsys.readouterr().out

    def test_a_destination_no_longer_configured_is_left_alone(self, capsys):
        self._published(dest="SomethingElse")
        dest = MockDestination()
        self._pipeline(dest, prune=True)._handle_orphans({"nb-2": object()})

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
            safe_name="Notes",
            folder_path="",
            display_title="Notes",
            keep_temp=False,
        )

    def test_reads_the_cloud_metadata_field(self):
        moment = self._job({"ModifiedClient": "2026-03-04T09:30:00"}).modified_at()
        assert moment == datetime.datetime(2026, 3, 4, 9, 30)

    def test_falls_back_to_the_document_attribute(self):
        item = SimpleNamespace(last_modified=datetime.datetime(2026, 3, 4, 9, 30))
        assert self._job(item).modified_at() == datetime.datetime(2026, 3, 4, 9, 30)

    def test_an_item_with_no_date_reports_none(self):
        assert self._job({}).modified_at() is None


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
        pipe.cache = SimpleNamespace(enabled=True)
        pipe._counts = (0, 0, 0)
        pipe._execute = execute
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
        pipe.json_output = False
        pipe.target_notebook = None
        seen_counts = []

        def process(nb_item, **kwargs):
            seen_counts.append(pipe._counts)
            return nb_item != "bad"

        monkeypatch.setattr(pipe, "process_notebook_item", process)
        monkeypatch.setattr(pipe, "connect", lambda: object())
        monkeypatch.setattr(pipe, "preflight_destinations", lambda: None)
        monkeypatch.setattr(pipe, "_learn_device", lambda client: None)
        monkeypatch.setattr(pipe, "discover_documents", lambda c: (["a", "bad", "c"], {}))
        monkeypatch.setattr(pipe, "_handle_orphans", lambda id_map: None)
        monkeypatch.setattr(pipe, "filter_pending_documents", lambda nbs, id_map: (nbs, {}, True))
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
            safe_name="Notes",
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
        pipe.target_notebook = None
        return pipe

    def _job(self, **kwargs):
        defaults = dict(
            item={},
            notebook="Notes",
            notebook_id="nb-1",
            doc_type="notebook",
            version="v1",
            safe_name="Notes",
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
        stale = {"ID": "nb-2", "VissibleName": "Journal"}
        fresh = {"ID": "nb-1", "VissibleName": "Notes"}

        pipe._report_unchanged([stale, fresh], [fresh])

        assert len(pipe.report.documents) == 1
        assert pipe.report.documents[0].name == "Journal"
        assert pipe.report.documents[0].status == SKIPPED

    def test_the_summary_is_printed_at_the_end(self, capsys):
        pipe = self._pipeline()
        pipe.json_output = False
        pipe.report.add(DocumentOutcome(name="Notes", status=SKIPPED))

        pipe._print_summary()

        assert "Synced 0 of 1 documents" in capsys.readouterr().out

    def test_json_output_is_machine_readable(self, capsys):
        pipe = self._pipeline()
        pipe.json_output = True
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
            safe_name="Test",
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
        pipe._report_unchanged([{"ID": "nb-2", "VissibleName": "Other"}], [])
        assert pipe.report.documents == []

    def test_an_untargeted_run_still_lists_them(self):
        pipe = self._pipeline()
        pipe._report_unchanged([{"ID": "nb-2", "VissibleName": "Other"}], [])
        assert pipe.report.documents[0].name == "Other"


class TestJsonSummaryReachesStdout:
    """``sync --json`` is only useful if its output can be piped into a parser."""

    def _pipeline(self, json_output):
        pipe = SyncPipeline.__new__(SyncPipeline)
        pipe.report = RunReport()
        pipe.json_output = json_output
        pipe.report.add(DocumentOutcome(name="Notes", status=SKIPPED, reason="unchanged"))
        return pipe

    def test_the_summary_is_parseable_json_on_stdout(self, capsys):
        self._pipeline(json_output=True)._print_summary()
        out = capsys.readouterr().out

        assert json.loads(out)["seen"] == 1

    def test_stdout_holds_the_document_and_nothing_else(self, capsys):
        """A stray progress line ahead of the JSON is what made this unusable."""
        pipe = self._pipeline(json_output=True)
        logs.configure(LOG_PATH, json_output=True)
        log("Destination added: Obsidian")
        pipe._print_summary()
        captured = capsys.readouterr()

        json.loads(captured.out)
        assert "Destination added" in captured.err
        logs.configure(LOG_PATH)

    def test_without_the_flag_the_table_is_printed_instead(self, capsys):
        self._pipeline(json_output=False)._print_summary()
        out = capsys.readouterr().out

        assert "Synced 0 of 1 documents" in out
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)


class TestThePreviewAndTheRunAgree:
    """`sync --status` predicts the run; a second opinion would be a bug."""

    @pytest.fixture
    def store(self, tmp_path, monkeypatch):
        """A real state store on a throwaway database."""
        monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path)
        monkeypatch.setattr(pipeline, "ROOT", tmp_path)
        monkeypatch.setattr(pipeline, "ensure_runtime_dirs", lambda: None)
        pipeline.reset_state_store()
        yield pipeline.get_state_store()
        pipeline.reset_state_store()

    ITEMS = [
        {"ID": "doc-new", "Type": "DocumentType", "VissibleName": "New", "hash": "h1"},
        {"ID": "doc-changed", "Type": "DocumentType", "VissibleName": "Changed", "hash": "h2"},
        {"ID": "doc-settled", "Type": "DocumentType", "VissibleName": "Settled", "hash": "h3"},
    ]

    def _sync(self, store):
        """Run the filter over ITEMS against one destination."""
        dest = MockDestination()
        # Above the document count: the per-run cap is applied after the
        # filter, and capping it here would look like a disagreement.
        pipe = SyncPipeline(SyncOptions(limit=10), destinations=[dest])
        id_map = {item["ID"]: item for item in self.ITEMS}
        to_process, needs_update, _ = pipe.filter_pending_documents(list(self.ITEMS), id_map)
        return [item["ID"] for item in to_process], needs_update, dest

    def _preview(self, store):
        """Classify the same items the way `sync --status` does."""
        listing = [
            {
                "id": item["ID"],
                "name": item["VissibleName"],
                "folder": None,
                "doc_type": "notebook",
                "version": item["hash"],
            }
            for item in self.ITEMS
        ]
        rows, _ = store.compare_with_listing(listing, ["MockDestination"])
        return {row["id"]: row["status"] for row in rows}

    def test_the_run_processes_exactly_what_the_preview_flagged(self, store):
        store.record_publication("doc-changed", "MockDestination", "old")
        store.record_publication("doc-settled", "MockDestination", "h3")

        processed, _, _ = self._sync(store)
        flagged = [doc_id for doc_id, status in self._preview(store).items() if status.needs_sync]
        assert sorted(processed) == sorted(flagged) == ["doc-changed", "doc-new"]

    def test_a_settled_document_is_left_alone_by_both(self, store):
        for item in self.ITEMS:
            store.record_publication(item["ID"], "MockDestination", item["hash"])

        processed, needs_update, _ = self._sync(store)
        assert processed == []
        assert needs_update == {}
        assert not any(status.needs_sync for status in self._preview(store).values())

    def test_the_run_names_the_destination_that_is_owed(self, store):
        store.record_publication("doc-settled", "MockDestination", "h3")

        _, needs_update, dest = self._sync(store)
        assert needs_update["doc-new"] == [dest]
        assert "doc-settled" not in needs_update

    def test_the_per_run_cap_shortens_the_run_not_the_preview(self, store):
        """A capped run is not a disagreement: the rest is still owed."""
        pipe = SyncPipeline(SyncOptions(limit=1), destinations=[MockDestination()])
        id_map = {item["ID"]: item for item in self.ITEMS}
        to_process, needs_update, _ = pipe.filter_pending_documents(list(self.ITEMS), id_map)

        assert len(to_process) == 1
        assert set(needs_update) == {"doc-new", "doc-changed", "doc-settled"}


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

        def pipe_for(model):
            pipe = SyncPipeline.__new__(SyncPipeline)
            pipe.device = self._reading(model)
            pipe.settings = Settings(render_background="white")
            pipe.renders = RenderCache(tmp_path / "renders", enabled=True)
            pipe.report = RunReport()
            pipe._save_page = lambda job, page, data, label="Saved": None
            return pipe

        job = DocumentJob(
            item={},
            notebook="Notes",
            notebook_id="doc-1",
            doc_type="notebook",
            version="v1",
            safe_name="Notes",
            folder_path="",
            display_title="Notes",
            keep_temp=False,
        )

        pipe_for("reMarkable 2")._render_zip_pages(job, tmp_path / "doc.zip", 1)
        pipe_for("reMarkable Paper Pro")._render_zip_pages(job, tmp_path / "doc.zip", 1)
        # Same page, same renderer, same background — only the tablet differs.
        pipe_for("reMarkable 2")._render_zip_pages(job, tmp_path / "doc.zip", 1)

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

        pipe = SyncPipeline.__new__(SyncPipeline)
        pipe.device = self._reading("reMarkable Paper Pro")
        pipe.settings = Settings(render_background="white")
        pipe.renders = RenderCache(tmp_path / "renders", enabled=False)
        pipe.report = RunReport()
        pipe._save_page = lambda job, page, data, label="Saved": None

        pipe._render_zip_pages(
            DocumentJob(
                item={},
                notebook="Notes",
                notebook_id="doc-1",
                doc_type="notebook",
                version="v1",
                safe_name="Notes",
                folder_path="",
                display_title="Notes",
                keep_temp=False,
            ),
            tmp_path / "doc.zip",
            1,
        )

        assert seen["screen"] == (1620, 2160)
