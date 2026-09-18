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
from living_ink.destinations import AppleNotesDestination, Destination, DestinationError
from living_ink.pipeline import (
    LOG_PATH,
    DocumentJob,
    SyncOptions,
    SyncPipeline,
    log,
)
from living_ink.report import (
    FAILED,
    PUBLISHED,
    SKIPPED,
    WOULD_PUBLISH,
    DocumentOutcome,
    RunReport,
)


class MockDestination(Destination):
    """Mock destination for testing."""

    def __init__(self, name: str = "Mock"):
        self.name = name
        self.published = []
        self.unpublished = []
        self.unpublish_result = True
        self.unpublish_error = None

    def unpublish(self, target=None, external_id=None, doc_id=None) -> bool:
        if self.unpublish_error:
            raise self.unpublish_error
        self.unpublished.append((target, external_id, doc_id))
        return self.unpublish_result

    def publish(
        self,
        notebook_name: str,
        text_content: str,
        image_paths: list,
        **kwargs,
    ) -> bool:
        # Recorded as passed rather than named one by one, so a new argument on
        # the contract does not need this double edited to keep the suite green.
        self.published.append(
            {
                "notebook_name": notebook_name,
                "text_content": text_content,
                "image_paths": image_paths,
                **kwargs,
            }
        )
        return True


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
    with patch("living_ink.pipeline.load_processed_log", return_value={}):
        to_process, needs_update, cont = pipeline.filter_pending_documents(items, id_map)
        assert cont is True
        assert len(to_process) == 2


def test_sync_pipeline_run_no_notebooks():
    """SyncPipeline.run returns True gracefully when no items need updating."""
    pipeline = SyncPipeline(destinations=[])
    with patch("living_ink.pipeline.validate_environment"):
        with patch.object(pipeline, "connect") as mock_connect:
            mock_client = MagicMock()
            mock_connect.return_value = mock_client
            with patch.object(pipeline, "discover_documents", return_value=([], {})):
                result = pipeline.run()
                assert result is True


def test_sync_pipeline_run_targeted_not_found():
    """SyncPipeline.run returns False when a targeted notebook is not in the library."""
    pipeline = SyncPipeline(SyncOptions(notebook="NonExistentBook"), destinations=[])
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
    pipeline = SyncPipeline(SyncOptions(notebook="Meeting Notes"), destinations=[])

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

    def test_subfolders_split_the_remarkable_path(self):
        job = make_job(folder_path="Work / Projects / Q3")

        assert job.full_subfolder() == "Work/Projects/Q3"
        assert job.top_level_subfolder() == "Work"

    def test_subfolders_are_none_at_the_library_root(self):
        job = make_job()

        assert job.full_subfolder() is None
        assert job.top_level_subfolder() is None


class TestJobHelpers:
    """Small pure helpers the stages rely on."""

    def test_version_prefers_the_content_hash(self):
        assert pipeline._item_version({"hash": "abc", "Version": "3"}) == "abc"

    def test_version_falls_back_to_the_integer_version(self):
        assert pipeline._item_version({"Version": "7"}) == 7

    def test_version_defaults_to_one_when_unusable(self):
        assert pipeline._item_version({"Version": "not-a-number"}) == 1

    def test_metadata_line_is_stripped_from_the_transcript(self, tmp_path):
        transcript = tmp_path / "clean.txt"
        transcript.write_text('{"notebook": "N"}\n\n### Page 1\n\nHello\n')

        assert pipeline._strip_transcript_metadata(transcript) == "### Page 1\n\nHello"

    def test_missing_transcript_reads_as_empty(self, tmp_path):
        assert pipeline._strip_transcript_metadata(None) == ""
        assert pipeline._strip_transcript_metadata(tmp_path / "gone.txt") == ""


class TestRendererDispatch:
    """Document type selects the renderer; unknown types render as notebooks."""

    def test_pdf_and_epub_have_their_own_renderers(self):
        assert SyncPipeline._RENDERERS["pdf"] is SyncPipeline._render_pdf
        assert SyncPipeline._RENDERERS["epub"] is SyncPipeline._render_epub

    def test_anything_else_renders_as_a_notebook(self):
        assert SyncPipeline._RENDERERS.get("notebook") is None
        assert SyncPipeline._RENDERERS.get("djvu") is None


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

        def transcribe(path, use_vision_ocr):
            # Earlier pages finish last, which reorders anything unordered.
            time.sleep(0.05 * (len(paths) - int(path.stem.split("-")[1])))
            return path.name, path.name

        with patch.object(pipeline_obj, "_transcribe_page", side_effect=transcribe):
            results = pipeline_obj._transcribe_pages(paths, use_vision_ocr=True)

        assert [cleaned for _, cleaned in results] == [p.name for p in paths]

    def test_pages_are_transcribed_concurrently(self):
        """Four pages at width four take about one page's time, not four."""
        pipeline_obj = self._pipeline(4)
        paths = [Path(f"page-{i}.png") for i in range(4)]

        def transcribe(path, use_vision_ocr):
            time.sleep(0.1)
            return "", ""

        with patch.object(pipeline_obj, "_transcribe_page", side_effect=transcribe):
            started = time.monotonic()
            pipeline_obj._transcribe_pages(paths, use_vision_ocr=True)
            elapsed = time.monotonic() - started

        assert elapsed < 0.3, f"pages appear to have run serially ({elapsed:.2f}s)"

    def test_concurrency_of_one_runs_serially(self):
        pipeline_obj = self._pipeline(1)
        paths = [Path("a.png"), Path("b.png")]
        in_flight = []

        def transcribe(path, use_vision_ocr):
            in_flight.append(path.name)
            assert len(in_flight) == 1
            in_flight.pop()
            return path.name, path.name

        with patch.object(pipeline_obj, "_transcribe_page", side_effect=transcribe):
            results = pipeline_obj._transcribe_pages(paths, use_vision_ocr=False)

        assert results == [("a.png", "a.png"), ("b.png", "b.png")]

    def test_no_pages_needs_no_workers(self):
        assert self._pipeline(4)._transcribe_pages([], use_vision_ocr=True) == []

    def test_vision_result_is_used_for_both_transcripts(self):
        pipeline_obj = self._pipeline(1)

        with patch.object(pipeline_obj, "_vision_ocr_page", return_value="clean text"):
            assert pipeline_obj._transcribe_page(Path("p.png"), True) == (
                "clean text",
                "clean text",
            )

    def test_empty_vision_result_falls_back_to_google(self):
        pipeline_obj = self._pipeline(1)

        with patch.object(pipeline_obj, "_vision_ocr_page", return_value=""):
            with patch.object(pipeline_obj, "_google_ocr_page", return_value=("raw", "clean")):
                assert pipeline_obj._transcribe_page(Path("p.png"), True) == ("raw", "clean")


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


class TestTranscriptReuse:
    """Transcribing costs money, so a transcript that is still current is reused."""

    def _pages_and_transcript(self, tmp_path, monkeypatch):
        """Lay out one page image and a transcript written after it."""
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
        return page, transcript

    def test_a_current_transcript_is_adopted(self, tmp_path, monkeypatch):
        page, transcript = self._pages_and_transcript(tmp_path, monkeypatch)
        job = make_job(safe_name="Notes", imgs=[page])

        assert SyncPipeline(destinations=[])._reuse_transcript(job) is True
        assert job.clean_out_txt == transcript

    def test_redrawn_pages_force_a_new_transcript(self, tmp_path, monkeypatch):
        """A page rendered after the transcript means the transcript is stale."""
        page, transcript = self._pages_and_transcript(tmp_path, monkeypatch)
        newer = transcript.stat().st_mtime + 10
        os.utime(page, (newer, newer))
        job = make_job(safe_name="Notes", imgs=[page])

        assert SyncPipeline(destinations=[])._reuse_transcript(job) is False
        assert job.clean_out_txt is None

    def test_no_transcript_means_no_reuse(self, tmp_path, monkeypatch):
        page, transcript = self._pages_and_transcript(tmp_path, monkeypatch)
        transcript.unlink()
        job = make_job(safe_name="Notes", imgs=[page])

        assert SyncPipeline(destinations=[])._reuse_transcript(job) is False

    def test_an_empty_transcript_is_not_reused(self, tmp_path, monkeypatch):
        page, transcript = self._pages_and_transcript(tmp_path, monkeypatch)
        transcript.write_text("")
        os.utime(transcript, (page.stat().st_mtime + 10, page.stat().st_mtime + 10))
        job = make_job(safe_name="Notes", imgs=[page])

        assert SyncPipeline(destinations=[])._reuse_transcript(job) is False

    def test_reuse_skips_ocr_entirely(self, tmp_path, monkeypatch):
        """The expensive stages are not merely fast on reuse — they do not run."""
        page, _ = self._pages_and_transcript(tmp_path, monkeypatch)
        pipeline_obj = SyncPipeline(destinations=[])
        nb_item = {"ID": "nb-1", "VissibleName": "Notes", "hash": "h"}

        with (
            patch("living_ink.pipeline.get_document_type", return_value="notebook"),
            patch.object(
                pipeline_obj, "_acquire_pages", side_effect=lambda job, c: job.imgs.append(page)
            ),
            patch.object(pipeline_obj, "_collect_tags"),
            patch.object(pipeline_obj, "_publish", return_value=True),
            patch.object(pipeline_obj, "_ocr_pages") as ocr,
            patch.object(pipeline_obj, "_preprocess_images") as preprocess,
        ):
            assert (
                pipeline_obj.process_notebook_item(nb_item, MagicMock(), {}, {}, keep_temp=True)
                is True
            )

        ocr.assert_not_called()
        preprocess.assert_not_called()


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
            assert pipe._transcribe_page(page, True) == ("text", "text")
        assert ocr.call_count == 1

    def test_the_second_read_does_not(self, tmp_path, page):
        """The whole point: an unchanged page costs nothing the next time."""
        pipe = self._pipeline(tmp_path)
        with patch.object(pipe, "_vision_ocr_page", return_value="text"):
            pipe._transcribe_page(page, True)

        with patch.object(pipe, "_vision_ocr_page") as ocr:
            assert pipe._transcribe_page(page, True) == ("text", "text")
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
                pipe._transcribe_pages(pages, True)

        resumed = self._pipeline(tmp_path)
        resumed.settings = SimpleNamespace(ocr_concurrency=1)
        with patch.object(resumed, "_vision_ocr_page", side_effect=lambda p: p.name) as ocr:
            resumed._transcribe_pages(pages, True)

        # Pages 0 and 1 were banked before the interrupt; only 2 and 3 are paid for.
        assert [call.args[0] for call in ocr.call_args_list] == pages[2:]
        assert resumed._cache_hits == 2

    def test_a_cache_survives_a_new_pipeline(self, tmp_path, page):
        """Entries outlive the run, which is what the temp purge does not."""
        with patch.object(SyncPipeline, "_vision_ocr_page", return_value="text"):
            self._pipeline(tmp_path)._transcribe_page(page, True)

        second = self._pipeline(tmp_path)
        with patch.object(second, "_vision_ocr_page") as ocr:
            assert second._transcribe_page(page, True) == ("text", "text")
        ocr.assert_not_called()

    def test_an_edited_page_is_read_again(self, tmp_path, page):
        pipe = self._pipeline(tmp_path)
        with patch.object(pipe, "_vision_ocr_page", return_value="text"):
            pipe._transcribe_page(page, True)

        page.write_bytes(b"different png bytes")
        with patch.object(pipe, "_vision_ocr_page", return_value="new text") as ocr:
            assert pipe._transcribe_page(page, True) == ("new text", "new text")
        assert ocr.call_count == 1

    def test_the_two_ocr_routes_do_not_share_an_entry(self, tmp_path, page):
        """Google Vision plus repair is a different answer from vision OCR."""
        pipe = self._pipeline(tmp_path)
        with patch.object(pipe, "_vision_ocr_page", return_value="vision text"):
            pipe._transcribe_page(page, True)

        with patch.object(pipe, "_google_ocr_page", return_value=("raw", "google text")) as ocr:
            assert pipe._transcribe_page(page, False) == ("raw", "google text")
        assert ocr.call_count == 1

    def test_an_empty_transcription_is_not_cached(self, tmp_path, page):
        """A blank page is usually a rate limit, and must not become permanent."""
        pipe = self._pipeline(tmp_path)
        with patch.object(pipe, "_vision_ocr_page", return_value=""):
            with patch.object(pipe, "_google_ocr_page", return_value=("", "")):
                pipe._transcribe_page(page, True)

        with patch.object(pipe, "_vision_ocr_page", return_value="text") as ocr:
            assert pipe._transcribe_page(page, True) == ("text", "text")
        assert ocr.call_count == 1

    def test_a_disabled_cache_reads_every_time(self, tmp_path, page):
        pipe = self._pipeline(tmp_path, enabled=False)
        with patch.object(pipe, "_vision_ocr_page", return_value="text") as ocr:
            pipe._transcribe_page(page, True)
            pipe._transcribe_page(page, True)
        assert ocr.call_count == 2
        assert not pipe.cache.root.exists()

    def test_an_unreadable_page_is_transcribed_uncached(self, tmp_path):
        """No bytes to hash means no key; transcribe rather than fail."""
        pipe = self._pipeline(tmp_path)
        missing = tmp_path / "gone.png"
        with patch.object(pipe, "_vision_ocr_page", return_value="text") as ocr:
            assert pipe._transcribe_page(missing, True) == ("text", "text")
        assert ocr.call_count == 1
        assert not pipe.cache.root.exists()

    def test_the_google_route_is_cached_too(self, tmp_path, page):
        pipe = self._pipeline(tmp_path)
        with patch.object(pipe, "_google_ocr_page", return_value=("raw", "clean")):
            pipe._transcribe_page(page, False)

        with patch.object(pipe, "_google_ocr_page") as ocr:
            assert pipe._transcribe_page(page, False) == ("raw", "clean")
        ocr.assert_not_called()


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
        monkeypatch.setattr(
            "living_ink.extract.get_background_color", lambda: "white", raising=True
        )
        return calls

    def _pipeline(self, tmp_path, enabled=True):
        from living_ink.cache import RenderCache

        pipe = SyncPipeline.__new__(SyncPipeline)
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

    def test_a_new_background_re_renders_everything(self, tmp_path, rendered, monkeypatch):
        """The background is baked into the PNG, so it belongs in the key."""
        pipe = self._pipeline(tmp_path)
        pipe._render_zip_pages(self._job(), tmp_path / "doc.zip", 2)
        rendered.clear()

        monkeypatch.setattr("living_ink.extract.get_background_color", lambda: "yellow")
        again = self._pipeline(tmp_path)
        again._render_zip_pages(self._job(), tmp_path / "doc.zip", 2)
        assert rendered == [1, 2]

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
        monkeypatch.setattr("living_ink.extract.get_background_color", lambda: "white")

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

        assert dest.published[0]["doc_id"] == "nb-1"

    def test_where_the_note_landed_is_recorded(self, tmp_path):
        dest = MockDestination("MockDest")
        dest.last_target = "Work/Notes.md"
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

        assert dest.published[0]["document_modified"] == "2026-03-04"


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
        assert self._job({"ModifiedClient": "2026-03-04T09:30:00"}).modified_date() == "2026-03-04"

    def test_falls_back_to_the_document_attribute(self):
        item = SimpleNamespace(last_modified=datetime.datetime(2026, 3, 4, 9, 30))
        assert self._job(item).modified_date() == "2026-03-04"

    def test_an_item_with_no_date_reports_none(self):
        assert self._job({}).modified_date() is None


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
