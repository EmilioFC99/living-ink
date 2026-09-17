"""Tests for living_ink.extract document-zip handling.

Focuses on the shared extraction seam (`_open_document_zip`) and the page
accounting that depends on it.
"""

import json
import zipfile

import pytest

from living_ink.extract import (
    _get_ordered_rm_files,
    _open_document_zip,
    get_document_page_count,
)


def _write_zip(path, members):
    """Build a zip from a {arcname: bytes} mapping."""
    with zipfile.ZipFile(path, "w") as zf:
        for name, payload in members.items():
            zf.writestr(name, payload)
    return path


class TestOpenDocumentZip:
    """Tests for the shared zip extraction context manager."""

    def test_extracts_members_and_cleans_up(self, tmp_path):
        """Contents are available inside the block and gone after it."""
        zip_path = _write_zip(tmp_path / "doc.zip", {"a/b.rm": b"data"})

        with _open_document_zip(zip_path) as extracted:
            assert (extracted / "a" / "b.rm").read_bytes() == b"data"
            captured = extracted

        assert not captured.exists()

    def test_rejects_path_traversal(self, tmp_path):
        """An entry escaping the temp directory is refused, not written."""
        zip_path = tmp_path / "evil.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("../escaped.rm", b"nope")

        with pytest.raises(ValueError, match="outside the temp directory"):
            with _open_document_zip(zip_path):
                pass

        assert not (tmp_path / "escaped.rm").exists()


class TestPageCount:
    """Tests for get_document_page_count()."""

    def test_counts_pages(self, tmp_path):
        """Every .rm file is one page."""
        zip_path = _write_zip(
            tmp_path / "doc.zip",
            {"doc/p1.rm": b"", "doc/p2.rm": b"", "doc/p3.rm": b""},
        )
        assert get_document_page_count(zip_path) == 3

    def test_agrees_with_the_ordered_page_list(self, tmp_path):
        """The count must match the list the renderer indexes into."""
        content = json.dumps({"cPages": {"pages": [{"id": "p2"}, {"id": "p1"}]}})
        zip_path = _write_zip(
            tmp_path / "doc.zip",
            {"doc.content": content, "doc/p1.rm": b"", "doc/p2.rm": b""},
        )

        with _open_document_zip(zip_path) as extracted:
            ordered = _get_ordered_rm_files(extracted)

        assert get_document_page_count(zip_path) == len(ordered)

    def test_zero_pages_for_an_empty_document(self, tmp_path):
        """A zip with no .rm files reports zero pages rather than raising."""
        zip_path = _write_zip(tmp_path / "doc.zip", {"doc.metadata": b"{}"})
        assert get_document_page_count(zip_path) == 0
