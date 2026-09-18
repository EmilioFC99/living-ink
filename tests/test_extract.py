"""Tests for living_ink.extract document-zip handling.

Focuses on the shared extraction seam (`_open_document_zip`) and the page
accounting that depends on it.
"""

import hashlib
import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

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


def _rm_bytes(version: int = 6, payload: bytes = b"\x00" * 16) -> bytes:
    """Build a .rm file's opening bytes for a given declared version.

    Args:
        version: The format version to declare in the header.
        payload: Bytes to follow the header.

    Returns:
        A byte string with a valid 43-byte reMarkable lines header.
    """
    header = f"reMarkable .lines file, version={version}".encode("ascii")
    return header.ljust(43, b" ") + payload


class TestRmVersionHeader:
    """A .rm file says what format it is; nothing used to read it."""

    @pytest.mark.parametrize("version", [3, 5, 6])
    def test_the_declared_version_is_read(self, tmp_path, version):
        path = tmp_path / "page.rm"
        path.write_bytes(_rm_bytes(version))

        assert extract.read_rm_version(path) == version

    def test_a_file_that_is_not_a_page_has_no_version(self, tmp_path):
        path = tmp_path / "page.rm"
        path.write_bytes(b"this is not a reMarkable page")

        assert extract.read_rm_version(path) is None

    def test_a_missing_file_has_no_version(self, tmp_path):
        assert extract.read_rm_version(tmp_path / "absent.rm") is None

    def test_an_unparseable_version_is_not_a_crash(self, tmp_path):
        path = tmp_path / "page.rm"
        path.write_bytes(b"reMarkable .lines file, version=x".ljust(43, b" "))

        assert extract.read_rm_version(path) is None

    def test_only_what_the_parser_claims_is_supported(self):
        """rmscene reads v6 alone; v3 and v5 need a different library."""
        assert extract.SUPPORTED_RM_VERSIONS == frozenset({6})


class TestUnsupportedFormatIsLoud:
    """An unreadable format used to render blank and publish an empty note."""

    def test_an_old_format_names_the_version_it_found(self, tmp_path):
        path = tmp_path / "page.rm"
        path.write_bytes(_rm_bytes(3))

        with pytest.raises(extract.UnsupportedRmFormat) as excinfo:
            extract.render_rm_file_to_png(path)

        message = str(excinfo.value)
        assert "version 3" in message
        assert "version 6" in message

    def test_the_error_tells_the_user_what_to_do(self, tmp_path):
        path = tmp_path / "page.rm"
        path.write_bytes(_rm_bytes(9))

        with pytest.raises(extract.UnsupportedRmFormat, match="Upgrade living-ink"):
            extract.render_rm_file_to_png(path)

    def test_a_headerless_file_is_left_to_the_parser(self, tmp_path, monkeypatch):
        """No header is not a wrong header: rmc still gets its turn to try."""
        path = tmp_path / "page.rm"
        path.write_bytes(b"garbage")

        assert extract.render_rm_file_to_png(path) is None

    def test_it_is_a_render_error(self):
        assert issubclass(extract.UnsupportedRmFormat, extract.RenderError)
        assert issubclass(extract.BlankRenderError, extract.RenderError)


class TestBlankRenderIsLoud:
    """A page with strokes that draws nothing is a bug, not an empty page."""

    def _svg(self, tmp_path, markup):
        path = tmp_path / "page.svg"
        path.write_text(markup, encoding="utf-8")
        return path

    def test_ink_is_recognised(self, tmp_path):
        svg = self._svg(tmp_path, '<svg><path d="M0 0 L1 1"/></svg>')
        assert extract._svg_has_ink(svg) is True

    def test_an_empty_canvas_has_no_ink(self, tmp_path):
        svg = self._svg(tmp_path, '<svg height="10" width="10"></svg>')
        assert extract._svg_has_ink(svg) is False

    def test_an_unreadable_svg_is_never_called_blank(self, tmp_path):
        """Reading the file failed; that says nothing about the page."""
        assert extract._svg_has_ink(tmp_path / "absent.svg") is True

    def test_strokes_that_drew_nothing_raise(self, tmp_path, monkeypatch):
        path = tmp_path / "page.rm"
        path.write_bytes(_rm_bytes(6))

        def fake_rm_to_svg(source, target):
            Path(target).write_text('<svg height="10" width="10"></svg>', encoding="utf-8")

        monkeypatch.setattr(extract, "_patch_rmc", lambda: None)
        monkeypatch.setitem(
            sys.modules, "rmc.exporters.svg", SimpleNamespace(rm_to_svg=fake_rm_to_svg)
        )
        monkeypatch.setattr(extract, "inspect_rm_page", lambda p: extract.RmPageStats(42, 0))

        with pytest.raises(extract.BlankRenderError, match="42 strokes"):
            extract.render_rm_file_to_png(path)

    def test_a_genuinely_blank_page_stays_legal(self, tmp_path, monkeypatch):
        path = tmp_path / "page.rm"
        path.write_bytes(_rm_bytes(6))

        def fake_rm_to_svg(source, target):
            Path(target).write_text('<svg height="10" width="10"></svg>', encoding="utf-8")

        monkeypatch.setattr(extract, "_patch_rmc", lambda: None)
        monkeypatch.setitem(
            sys.modules, "rmc.exporters.svg", SimpleNamespace(rm_to_svg=fake_rm_to_svg)
        )
        monkeypatch.setattr(extract, "inspect_rm_page", lambda p: extract.RmPageStats(0, 0))

        assert extract.render_rm_file_to_png(path) is not None

    def test_a_count_we_could_not_take_is_not_zero(self, tmp_path, monkeypatch):
        """Unknown must not be read as "no strokes", nor as "blank render"."""
        path = tmp_path / "page.rm"
        path.write_bytes(_rm_bytes(6))

        def fake_rm_to_svg(source, target):
            Path(target).write_text('<svg height="10" width="10"></svg>', encoding="utf-8")

        monkeypatch.setattr(extract, "_patch_rmc", lambda: None)
        monkeypatch.setitem(
            sys.modules, "rmc.exporters.svg", SimpleNamespace(rm_to_svg=fake_rm_to_svg)
        )
        monkeypatch.setattr(extract, "inspect_rm_page", lambda p: None)

        assert extract.render_rm_file_to_png(path) is not None

    def test_counting_a_file_that_is_not_a_page_gives_unknown(self, tmp_path):
        path = tmp_path / "page.rm"
        path.write_bytes(b"not a page at all")

        assert extract.inspect_rm_page(path) is None

    def test_blocks_the_parser_could_not_decode_also_count_as_content(self, tmp_path, monkeypatch):
        """rmscene wraps an undecodable block and carries on, drawing nothing."""
        path = tmp_path / "page.rm"
        path.write_bytes(_rm_bytes(6))

        def fake_rm_to_svg(source, target):
            Path(target).write_text('<svg height="10" width="10"></svg>', encoding="utf-8")

        monkeypatch.setattr(extract, "_patch_rmc", lambda: None)
        monkeypatch.setitem(
            sys.modules, "rmc.exporters.svg", SimpleNamespace(rm_to_svg=fake_rm_to_svg)
        )
        monkeypatch.setattr(extract, "inspect_rm_page", lambda p: extract.RmPageStats(0, 5))

        with pytest.raises(extract.BlankRenderError) as excinfo:
            extract.render_rm_file_to_png(path)

        assert "5 blocks this build cannot decode" in str(excinfo.value)

    def test_an_empty_page_has_no_content(self):
        assert extract.RmPageStats(0, 0).has_content is False
        assert extract.RmPageStats(1, 0).has_content is True
        assert extract.RmPageStats(0, 1).has_content is True


class TestRenderFingerprintCoversTheGuards:
    """The guards change what a render produces, so cached pages must miss."""

    def test_the_format_version_was_bumped(self):
        assert extract.RENDER_FORMAT_VERSION >= 2

    def test_the_fingerprint_moves_with_it(self, monkeypatch):
        """Otherwise the cache serves images the guarded code would refuse."""
        renderer_fingerprint.cache_clear()
        before = renderer_fingerprint()

        monkeypatch.setattr(extract, "RENDER_FORMAT_VERSION", 99)
        renderer_fingerprint.cache_clear()
        after = renderer_fingerprint()

        renderer_fingerprint.cache_clear()
        assert before != after
