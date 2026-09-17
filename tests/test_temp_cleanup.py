"""Tests for temporary artifacts cleanup in Living Ink pipeline.

Ensures that intermediate page images, vision inputs, OCR text files,
and downloaded documents are automatically purged to prevent disk leakage.
"""

from unittest.mock import MagicMock, patch

from living_ink.cli import SyncCommand, main
from living_ink.pipeline import (
    DOCS_DIR,
    OCR_DIR,
    PDF_DIR,
    VISION_DIR,
    WHITE_DIR,
    clean_notebook_temp_artifacts,
    cleanup_temp_artifacts,
)


def test_cleanup_temp_artifacts_cleans_all_folders(tmp_path, monkeypatch):
    """cleanup_temp_artifacts purges all temporary working directories."""
    # Populate temp folders with dummy artifacts
    folders = [WHITE_DIR, VISION_DIR, OCR_DIR, PDF_DIR, DOCS_DIR]
    created_files = []
    for f in folders:
        f.mkdir(parents=True, exist_ok=True)
        dummy = f / "test_artifact.tmp"
        dummy.write_text("temporary data")
        created_files.append(dummy)

    # Also add a nested directory in VISION_DIR
    sub_dir = VISION_DIR / "nested_book"
    sub_dir.mkdir(exist_ok=True)
    (sub_dir / "page-1.png").write_text("png data")

    cleanup_temp_artifacts(keep_temp=False)

    for dummy in created_files:
        assert not dummy.exists(), f"Expected {dummy} to be deleted"
    assert not sub_dir.exists(), "Expected nested vision directory to be deleted"


def test_cleanup_temp_artifacts_respects_keep_temp():
    """cleanup_temp_artifacts preserves all files when keep_temp=True."""
    dummy = WHITE_DIR / "keep_me.png"
    WHITE_DIR.mkdir(parents=True, exist_ok=True)
    dummy.write_text("png data")

    try:
        cleanup_temp_artifacts(keep_temp=True)
        assert dummy.exists()
    finally:
        dummy.unlink(missing_ok=True)


def test_clean_notebook_temp_artifacts():
    """clean_notebook_temp_artifacts deletes only artifacts for the target notebook."""
    WHITE_DIR.mkdir(parents=True, exist_ok=True)
    OCR_DIR.mkdir(parents=True, exist_ok=True)

    nb1_img = WHITE_DIR / "NotebookOne.page-1.png"
    nb1_ocr = OCR_DIR / "NotebookOne_clean.txt"
    nb2_img = WHITE_DIR / "NotebookTwo.page-1.png"
    nb2_ocr = OCR_DIR / "NotebookTwo_clean.txt"

    nb1_img.write_text("nb1")
    nb1_ocr.write_text("nb1 text")
    nb2_img.write_text("nb2")
    nb2_ocr.write_text("nb2 text")

    try:
        clean_notebook_temp_artifacts("NotebookOne", keep_temp=False)
        assert not nb1_img.exists()
        assert not nb1_ocr.exists()
        assert nb2_img.exists()
        assert nb2_ocr.exists()
    finally:
        nb1_img.unlink(missing_ok=True)
        nb1_ocr.unlink(missing_ok=True)
        nb2_img.unlink(missing_ok=True)
        nb2_ocr.unlink(missing_ok=True)


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
            assert mock_init.call_args.kwargs["options"].keep_temp is True
            mock_run.assert_called_once()
