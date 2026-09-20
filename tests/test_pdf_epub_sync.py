"""Tests for PDF and EPUB document syncing."""

import io
import json
import zipfile
from unittest.mock import MagicMock, patch

import pymupdf as fitz
import pytest
from PIL import Image

from living_ink.destinations import ObsidianDestination
from living_ink.extract import (
    extract_raw_document_from_zip,
    format_page_label,
    get_pdf_annotated_page_map,
    render_composite_pdf_page,
    render_pdf_page_preview,
)
from living_ink.pipeline import get_document_type
from tests.builders import make_both, make_page


class TestDocumentTypeDetection:
    """Test get_document_type classification logic."""

    def test_detects_via_client_get_file_type(self):
        client = MagicMock()
        client.get_file_type.return_value = "pdf"
        item = {"ID": "doc-1", "VissibleName": "My Document"}
        assert get_document_type(item, client) == "pdf"

        client.get_file_type.return_value = "epub"
        assert get_document_type(item, client) == "epub"

    def test_detects_via_files_metadata(self):
        item_pdf = {
            "ID": "doc-1",
            "VissibleName": "Book",
            "files": [{"id": "doc-1.content"}, {"id": "doc-1.pdf"}],
        }
        assert get_document_type(item_pdf) == "pdf"

        item_epub = {
            "ID": "doc-2",
            "VissibleName": "Novel",
            "files": [{"id": "doc-2.content"}, {"id": "doc-2.epub"}],
        }
        assert get_document_type(item_epub) == "epub"

    def test_detects_via_filename_extension(self):
        item_pdf = {"ID": "doc-1", "VissibleName": "Report.pdf"}
        assert get_document_type(item_pdf) == "pdf"

        item_epub = {"ID": "doc-2", "VissibleName": "Ebook.epub"}
        assert get_document_type(item_epub) == "epub"

    def test_defaults_to_notebook(self):
        item_nb = {
            "ID": "doc-1",
            "VissibleName": "My Meeting Notes",
            "files": [{"id": "doc-1.content"}, {"id": "page1.rm"}],
        }
        assert get_document_type(item_nb) == "notebook"


class TestExtractRawDocumentFromZip:
    """Test extract_raw_document_from_zip."""

    def test_extracts_pdf_from_zip(self, tmp_path):
        zip_file = tmp_path / "test.zip"
        with zipfile.ZipFile(zip_file, "w") as zf:
            zf.writestr("doc-123.pdf", b"%PDF-1.4 mock pdf data")
            zf.writestr("doc-123.content", b"{}")

        out_pdf = tmp_path / "extracted.pdf"
        res = extract_raw_document_from_zip(zip_file, out_pdf)
        assert res == out_pdf
        assert out_pdf.exists()
        assert out_pdf.read_bytes() == b"%PDF-1.4 mock pdf data"

    def test_extracts_epub_from_zip(self, tmp_path):
        zip_file = tmp_path / "test.zip"
        with zipfile.ZipFile(zip_file, "w") as zf:
            zf.writestr("doc-456.epub", b"mock epub data")

        out_epub = tmp_path / "extracted.epub"
        res = extract_raw_document_from_zip(zip_file, out_epub)
        assert res == out_epub
        assert out_epub.exists()
        assert out_epub.read_bytes() == b"mock epub data"

    def test_returns_none_when_no_document(self, tmp_path):
        zip_file = tmp_path / "test.zip"
        with zipfile.ZipFile(zip_file, "w") as zf:
            zf.writestr("doc.content", b"{}")

        out = tmp_path / "extracted.pdf"
        assert extract_raw_document_from_zip(zip_file, out) is None
        assert not out.exists()


class TestGetPdfAnnotatedPageMap:
    """Test get_pdf_annotated_page_map."""

    def test_maps_pages_with_redir(self, tmp_path):
        content_json = {
            "cPages": {
                "pages": [
                    {"id": "page-uuid-1", "redir": {"value": 0}},
                    {"id": "page-uuid-2", "redir": {"value": 76}},
                    {"id": "page-uuid-3", "redir": {"value": 120}},
                ]
            }
        }
        zip_file = tmp_path / "doc.zip"
        with zipfile.ZipFile(zip_file, "w") as zf:
            zf.writestr("doc.content", json.dumps(content_json))
            zf.writestr("page-uuid-2.rm", b"rm_stroke_bytes")

        page_map = get_pdf_annotated_page_map(zip_file)
        assert len(page_map) == 1
        assert page_map[0]["page_id"] == "page-uuid-2"
        assert page_map[0]["pdf_page_index"] == 76
        assert page_map[0]["page_num"] == 77
        assert page_map[0]["rm_file_name"] == "page-uuid-2.rm"

    def test_returns_empty_when_no_rm_files(self, tmp_path):
        content_json = {"cPages": {"pages": [{"id": "page-uuid-1"}]}}
        zip_file = tmp_path / "doc.zip"
        with zipfile.ZipFile(zip_file, "w") as zf:
            zf.writestr("doc.content", json.dumps(content_json))

        assert get_pdf_annotated_page_map(zip_file) == []


class TestPdfRendering:
    """Test render_composite_pdf_page and render_pdf_page_preview."""

    @pytest.fixture
    def sample_pdf(self, tmp_path):
        """Create a valid minimal 2-page PDF file."""
        pdf_path = tmp_path / "sample.pdf"
        doc = fitz.open()
        page1 = doc.new_page(width=300, height=400)
        page1.insert_text((50, 50), "Hello Page 1", fontsize=14)
        page2 = doc.new_page(width=300, height=400)
        page2.insert_text((50, 50), "Hello Page 2", fontsize=14)
        doc.save(str(pdf_path))
        doc.close()
        return pdf_path

    def test_render_pdf_page_preview(self, sample_pdf):
        png_bytes = render_pdf_page_preview(sample_pdf, 0)
        assert png_bytes is not None
        img = Image.open(io.BytesIO(png_bytes))
        assert img.width > 0
        assert img.height > 0

    def test_render_composite_pdf_page_without_rm(self, sample_pdf):
        png_bytes = render_composite_pdf_page(sample_pdf, 0, rm_bytes=b"")
        assert png_bytes is not None
        img = Image.open(io.BytesIO(png_bytes))
        assert img.width > 0
        assert img.height > 0

    def test_render_composite_pdf_page_out_of_bounds(self, sample_pdf):
        assert render_composite_pdf_page(sample_pdf, 999, b"") is None


class TestDestinationDocumentPublishing:
    """Test document publishing in Obsidian and Apple Notes destinations."""

    def test_obsidian_publishes_document_attachment(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        dest = ObsidianDestination(
            vault_path=str(vault),
            root_folder="Remarkable",
            attachments_folder="attachments",
        )

        dummy_doc = tmp_path / "Book.pdf"
        dummy_doc.write_bytes(b"%PDF-1.4 dummy book content")

        success = dest.publish(
            *make_both("Book", "Notes on Book", source="pdf", source_file=dummy_doc)
        )
        assert success.ok is True

        # Note created
        note_file = vault / "Remarkable" / "Book.md"
        assert note_file.exists()
        content = note_file.read_text(encoding="utf-8")
        assert "type: pdf" in content
        assert 'document: "[[Remarkable/attachments/Book/Book.pdf]]"' in content
        assert "**Source Document:**" not in content

        # Attachment copied
        attach_file = vault / "Remarkable" / "attachments" / "Book" / "Book.pdf"
        assert attach_file.exists()
        assert attach_file.read_bytes() == b"%PDF-1.4 dummy book content"


class TestPageLabelFormatting:
    """Test format_page_label and dual page number formatting."""

    def test_format_without_pdf(self):
        assert format_page_label(1, None) == "Page 1"
        assert format_page_label(42, None) == "Page 42"

    def test_format_standard_pdf(self, tmp_path):
        pdf_path = tmp_path / "plain.pdf"
        doc = fitz.open()
        doc.new_page()
        doc.save(str(pdf_path))
        doc.close()

        assert format_page_label(1, pdf_path) == "Page 1"

    def test_format_with_custom_labels(self, tmp_path):
        pdf_path = tmp_path / "book.pdf"
        doc = fitz.open()
        doc.new_page()
        doc.new_page()
        doc.set_page_labels([{"startpage": 0, "prefix": "xiii"}, {"startpage": 1, "prefix": "51"}])
        doc.save(str(pdf_path))
        doc.close()

        assert format_page_label(1, pdf_path) == "Page xiii (pdf-1)"
        assert format_page_label(2, pdf_path) == "Page 51 (pdf-2)"

    def test_obsidian_wikilinks_use_page_labels(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        dest = ObsidianDestination(vault_path=str(vault), root_folder="Living Ink")

        pdf_path = tmp_path / "book.pdf"
        doc = fitz.open()
        doc.new_page()
        doc.set_page_labels([{"startpage": 0, "prefix": "xiii"}])
        doc.save(str(pdf_path))
        doc.close()

        img_path = tmp_path / "page-1.png"
        img_path.write_bytes(b"dummy image")

        page = make_page(1, "Notes", label=format_page_label(1, pdf_path), image=img_path)
        dest.publish(*make_both("Book", pages=[page], source="pdf", source_file=pdf_path))

        note_file = vault / "Living Ink" / "Book.md"
        content = note_file.read_text(encoding="utf-8")
        assert "![[Living Ink/_attachments/Book/page-1.png|Page xiii (pdf-1)]]" in content


class TestLabellingEveryPageAtOnce:
    """``page_labels`` is ``format_page_label`` for a whole document, one open.

    The per-page function opens and closes the PDF on every call, so labelling
    a 300-page annotated PDF opened the file 300 times.
    """

    def test_it_agrees_with_the_single_page_function(self, tmp_path):
        from living_ink.extract import page_labels

        pdf_path = tmp_path / "book.pdf"
        doc = fitz.open()
        doc.new_page()
        doc.new_page()
        doc.set_page_labels([{"startpage": 0, "prefix": "xiii"}, {"startpage": 1, "prefix": "51"}])
        doc.save(str(pdf_path))
        doc.close()

        assert page_labels([1, 2], pdf_path) == {
            1: format_page_label(1, pdf_path),
            2: format_page_label(2, pdf_path),
        }

    def test_a_notebook_has_no_document_to_read(self):
        from living_ink.extract import page_labels

        assert page_labels([1, 7], None) == {1: "Page 1", 7: "Page 7"}

    def test_a_missing_document_still_labels_every_page(self, tmp_path):
        from living_ink.extract import page_labels

        assert page_labels([3], tmp_path / "gone.pdf") == {3: "Page 3"}

    def test_it_opens_the_document_once_for_the_whole_set(self, tmp_path):
        """The reason the function exists."""
        import living_ink.extract as extract

        pdf_path = tmp_path / "book.pdf"
        doc = fitz.open()
        for _ in range(5):
            doc.new_page()
        doc.save(str(pdf_path))
        doc.close()

        opens = []
        real_open = fitz.open

        def counting_open(*args, **kwargs):
            opens.append(args[0] if args else None)
            return real_open(*args, **kwargs)

        with patch.object(extract.fitz, "open", counting_open):
            extract.page_labels([1, 2, 3, 4, 5], pdf_path)

        assert len(opens) == 1

    def test_a_page_past_the_end_of_the_document_is_still_labelled(self, tmp_path):
        from living_ink.extract import page_labels

        pdf_path = tmp_path / "one.pdf"
        doc = fitz.open()
        doc.new_page()
        doc.save(str(pdf_path))
        doc.close()

        assert page_labels([1, 99], pdf_path)[99] == "Page 99"


class TestPageSectionHeaderFormatting:
    """Test format_page_section_header and TOC breadcrumb extraction."""

    def test_header_without_pdf(self):
        from living_ink.extract import format_page_section_header

        header = format_page_section_header(1, None)
        assert "---" in header
        assert '<span style="font-size: 0.9em; color: #777777"><b>Page 1</b></span>' in header

    def test_header_without_divider(self):
        from living_ink.extract import format_page_section_header

        header = format_page_section_header(2, None, include_divider=False)
        assert "---" not in header
        assert '<span style="font-size: 0.9em; color: #777777"><b>Page 2</b></span>' in header

    def test_header_with_pdf_toc_hierarchy(self, tmp_path):
        from living_ink.extract import format_page_section_header, get_pdf_toc_breadcrumbs

        pdf_path = tmp_path / "toc_book.pdf"
        doc = fitz.open()
        doc.new_page()
        doc.new_page()
        doc.set_page_labels([{"startpage": 0, "prefix": "xiii"}, {"startpage": 1, "prefix": "51"}])
        doc.set_toc(
            [
                [1, "Part I. Foundation and Building Blocks", 1],
                [2, "Chapter 2. The Data Engineering Lifecycle", 2],
                [3, "Data Management", 2],
            ]
        )
        doc.save(str(pdf_path))
        doc.close()

        crumbs_p1 = get_pdf_toc_breadcrumbs(1, pdf_path)
        assert crumbs_p1 == ["Part I. Foundation and Building Blocks"]

        crumbs_p2 = get_pdf_toc_breadcrumbs(2, pdf_path)
        assert crumbs_p2 == [
            "Part I. Foundation and Building Blocks",
            "Chapter 2. The Data Engineering Lifecycle",
            "Data Management",
        ]

        header = format_page_section_header(2, pdf_path)
        assert "---" in header
        assert '<span style="font-size: 0.9em; color: #777777"><b>Data Management</b><br>' in header
        assert (
            "Part I. Foundation and Building Blocks | Chapter 2. The Data Engineering Lifecycle | Page 51 (pdf-2)"
            in header
        )

    def test_normalize_callout_annotations(self):
        from living_ink.clean import normalize_callout_annotations

        sample = (
            "[Boxed passage]:\n"
            '"Data management has quite a few facets..."\n\n'
            "[Margin annotation to the right of the boxed passage]:\n"
            "!!!\n\n"
            'Highlighted passage:\n"Important text"'
        )
        normalized = normalize_callout_annotations(sample)
        assert "> [!example] Boxed Passage" in normalized
        assert '> "Data management has quite a few facets..."' in normalized
        assert "> [!note] Margin Note" in normalized
        assert "> !!!" in normalized
        assert "> [!quote] Highlight" in normalized
        assert '> "Important text"' in normalized
