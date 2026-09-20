"""Tests for temporary artifacts cleanup in Living Ink pipeline.

Ensures that intermediate page images, vision inputs, OCR text files,
and downloaded documents are automatically purged to prevent disk leakage.
"""

from unittest.mock import MagicMock, patch

from living_ink.cli import SyncCommand, main
from living_ink.core.temp import DocumentWorkspace
from living_ink.pipeline import WORK_DIR, cleanup_temp_artifacts


def _populate(doc_id: str) -> DocumentWorkspace:
    """Give one document a workspace with something in every folder."""
    workspace = DocumentWorkspace(WORK_DIR, doc_id).ensure()
    workspace.page_image(1).write_text("png data")
    (workspace.preprocessed_dir / "page-1.png").write_text("png data")
    workspace.transcript.write_text("transcribed")
    workspace.download.write_text("zip data")
    return workspace


def test_cleanup_temp_artifacts_cleans_every_workspace():
    """cleanup_temp_artifacts leaves no document's artifacts behind."""
    first = _populate("doc-one")
    second = _populate("doc-two")

    try:
        cleanup_temp_artifacts(keep_temp=False)

        assert not first.dir.exists()
        assert not second.dir.exists()
    finally:
        first.purge()
        second.purge()


def test_cleanup_temp_artifacts_respects_keep_temp():
    """cleanup_temp_artifacts preserves all files when keep_temp=True."""
    workspace = _populate("doc-keep")

    try:
        cleanup_temp_artifacts(keep_temp=True)
        assert workspace.page_image(1).exists()
    finally:
        workspace.purge()


def test_purging_one_document_leaves_its_neighbours_alone():
    """The unit is a directory, so a purge cannot reach past its own document."""
    first = _populate("doc-one")
    second = _populate("doc-two")

    try:
        first.purge()

        assert not first.dir.exists()
        assert second.page_image(1).exists()
        assert second.transcript.exists()
    finally:
        first.purge()
        second.purge()


def test_two_similarly_named_documents_do_not_share_artifacts():
    """The bug this replaced: "Notes" also matched "Notes (2)"'s pages."""
    notes = DocumentWorkspace(WORK_DIR, "doc-notes").ensure()
    notes_two = DocumentWorkspace(WORK_DIR, "doc-notes-two").ensure()
    notes.page_image(1).write_text("the first notebook")
    notes_two.page_image(1).write_text("the second notebook")

    try:
        notes.purge()

        assert notes_two.page_image(1).read_text() == "the second notebook"
    finally:
        notes.purge()
        notes_two.purge()


@patch.object(SyncCommand, "run", return_value=0)
def test_cli_keep_temp_flag(mock_sync):
    """CLI parses --keep-temp flag and passes it to SyncCommand.run."""
    with patch("sys.argv", ["living-ink", "sync", "--keep-temp"]):
        main()
        mock_sync.assert_called_once()
        args = mock_sync.call_args[0][0]
        assert args.keep_temp is True


def test_sync_command_forwards_keep_temp():
    """SyncCommand forwards --keep-temp to pipeline."""
    args = MagicMock(
        status=False,
        keep_temp=True,
        notebook=None,
        limit=0,
        folder=None,
        ssh=False,
        cloud=False,
        sync_pdfs=False,
        sync_epubs=False,
        all_types=False,
    )
    with patch("living_ink.pipeline.SyncPipeline.__init__", return_value=None) as mock_init:
        with patch("living_ink.pipeline.SyncPipeline.run", return_value=True) as mock_run:
            SyncCommand().run(args)
            mock_init.assert_called_once()
            assert mock_init.call_args.kwargs["keep_temp"] is True
            mock_run.assert_called_once()
