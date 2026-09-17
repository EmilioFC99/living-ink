"""Tests for living_ink.pipeline module and SyncPipeline class."""

from unittest.mock import MagicMock, patch

from living_ink.destinations import AppleNotesDestination, Destination
from living_ink.pipeline import SyncOptions, SyncPipeline


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
