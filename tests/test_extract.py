"""Tests for living_ink.extract document-zip handling.

Focuses on the shared extraction seam (`_open_document_zip`) and the page
accounting that depends on it.
"""

import hashlib
import json
import zipfile

import pytest

from living_ink import extract
from living_ink.extract import (
    _get_ordered_rm_files,
    _open_document_zip,
    get_document_page_count,
    get_page_source_hashes,
    renderer_fingerprint,
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


class TestPageSourceHashes:
    """The render cache keys on these, so they must track the page order."""

    def test_one_hash_per_page(self, tmp_path):
        zip_path = _write_zip(
            tmp_path / "doc.zip",
            {"doc/p1.rm": b"one", "doc/p2.rm": b"two"},
        )
        assert len(get_page_source_hashes(zip_path)) == 2

    def test_the_order_matches_the_rendered_order(self, tmp_path):
        """Page N's hash must be the source page N renders from."""
        content = json.dumps({"cPages": {"pages": [{"id": "p2"}, {"id": "p1"}]}})
        zip_path = _write_zip(
            tmp_path / "doc.zip",
            {"doc.content": content, "doc/p1.rm": b"one", "doc/p2.rm": b"two"},
        )

        hashes = get_page_source_hashes(zip_path)
        with _open_document_zip(zip_path) as extracted:
            ordered = [f.read_bytes() for f in _get_ordered_rm_files(extracted)]

        expected = [hashlib.sha256(payload).hexdigest() for payload in ordered]
        assert hashes == expected

    def test_identical_pages_hash_identically(self, tmp_path):
        """Two copies of the same strokes are one render, done once."""
        zip_path = _write_zip(
            tmp_path / "doc.zip",
            {"doc/p1.rm": b"same", "doc/p2.rm": b"same"},
        )
        first, second = get_page_source_hashes(zip_path)
        assert first == second

    def test_changed_strokes_change_the_hash(self, tmp_path):
        before = _write_zip(tmp_path / "a.zip", {"doc/p1.rm": b"one"})
        after = _write_zip(tmp_path / "b.zip", {"doc/p1.rm": b"one and a bit"})
        assert get_page_source_hashes(before) != get_page_source_hashes(after)

    def test_an_empty_document_has_no_hashes(self, tmp_path):
        zip_path = _write_zip(tmp_path / "doc.zip", {"doc.metadata": b"{}"})
        assert get_page_source_hashes(zip_path) == []

    def test_an_unreadable_zip_yields_nothing_rather_than_raising(self, tmp_path):
        """No hashes means no caching, which is a slow sync, not a failed one."""
        broken = tmp_path / "broken.zip"
        broken.write_bytes(b"not a zip")
        assert get_page_source_hashes(broken) == []


class TestRendererFingerprint:
    """A render is only reusable while the renderer that made it is unchanged."""

    def test_it_is_stable_within_a_build(self):
        assert renderer_fingerprint() == renderer_fingerprint()

    def test_it_is_a_short_hex_digest(self):
        fingerprint = renderer_fingerprint()
        assert len(fingerprint) == 16
        assert int(fingerprint, 16) >= 0

    def test_bumping_the_format_version_invalidates_every_render(self, monkeypatch):
        """Changing this module's own rendering must miss the cache."""
        before = renderer_fingerprint()
        renderer_fingerprint.cache_clear()
        monkeypatch.setattr(extract, "RENDER_FORMAT_VERSION", extract.RENDER_FORMAT_VERSION + 1)
        after = renderer_fingerprint()
        renderer_fingerprint.cache_clear()
        assert before != after

    def test_a_library_upgrade_invalidates_every_render(self, monkeypatch):
        """Upgrading rmc is the documented cause of a changed page image."""
        before = renderer_fingerprint()
        renderer_fingerprint.cache_clear()
        monkeypatch.setattr(extract, "version", lambda name: "99.0")
        after = renderer_fingerprint()
        renderer_fingerprint.cache_clear()
        assert before != after
