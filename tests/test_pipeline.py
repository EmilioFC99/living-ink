"""Tests for living_ink.pipeline module and SyncPipeline class."""

import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from living_ink import pipeline
from living_ink.destinations import AppleNotesDestination, Destination
from living_ink.pipeline import DocumentJob, SyncOptions, SyncPipeline


class MockDestination(Destination):
    """Mock destination for testing."""

    def __init__(self, name: str = "Mock"):
        self.name = name
        self.published = []

    def publish(
        self,
        notebook_name: str,
        text_content: str,
        image_paths: list,
        sub_folder: str = None,
        document_path=None,
        tags: list = None,
    ) -> bool:
        self.published.append(
            {
                "notebook_name": notebook_name,
                "text_content": text_content,
                "image_paths": image_paths,
                "sub_folder": sub_folder,
                "document_path": document_path,
                "tags": tags,
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
