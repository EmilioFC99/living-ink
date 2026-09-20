"""The source registry and the three renderers it ships.

The registry tests run against a *copy* of ``SOURCE_REGISTRY``: registering
mutates a process-wide dict at import, so a test that registered into the real
one would change what every other test in the session sees.
"""

import json
import zipfile
from pathlib import Path

import pymupdf as fitz
import pytest

from living_ink import extract
from living_ink.sources import (
    EPUB,
    NOTEBOOK,
    PDF,
    SOURCE_REGISTRY,
    PageRef,
    RenderContext,
    Renderer,
    SourceBundle,
    SourceType,
    extract_source_file,
    fallback_source,
    register_source,
    source_for_file_type,
    source_for_filename,
    source_for_name,
    source_suffixes,
)
from living_ink.transport import DeviceInfo


class StubRenderer:
    """A renderer that renders nothing, for tests about the registry itself."""

    version = 1

    def prepare(self, bundle, ctx):
        return True

    def pages(self, bundle, ctx):
        return []

    def render(self, bundle, page, ctx):
        return None

    def text_layer(self, bundle, ctx):
        return None

    def describe_pages(self, bundle, pages):
        return []


def a_source(name: str, **kwargs) -> SourceType:
    """Build a throwaway source with only the fields a test cares about."""
    return SourceType(
        name=name,
        file_type_values=kwargs.pop("file_type_values", ()),
        name_suffixes=kwargs.pop("name_suffixes", ()),
        source_suffix=kwargs.pop("source_suffix", ""),
        renderer=kwargs.pop("renderer", StubRenderer()),
        label=kwargs.pop("label", name.title()),
        **kwargs,
    )


@pytest.fixture
def clean_registry(monkeypatch):
    """Swap in an empty registry for the duration of one test."""
    registry = {}
    monkeypatch.setattr("living_ink.sources.base.SOURCE_REGISTRY", registry)
    return registry


def ctx(screen=(1404, 1872), background="#FFFFFF") -> RenderContext:
    """A render context for a tablet with a known panel."""
    return RenderContext(
        device=DeviceInfo(model="reMarkable 2", firmware="3.0", screen=screen, color=False),
        background=background,
    )


class TestRegistration:
    """One name, one fallback, and a name that answers for an unknown type."""

    def test_a_registered_source_comes_back_by_name(self, clean_registry):
        source = register_source(a_source("djvu"))
        assert source_for_name("djvu") is source

    def test_registering_a_name_twice_is_refused(self, clean_registry):
        register_source(a_source("djvu"))
        with pytest.raises(ValueError, match="already registered"):
            register_source(a_source("djvu"))

    def test_a_second_fallback_is_refused(self, clean_registry):
        """Two fallbacks means the answer depends on dict order."""
        register_source(a_source("notebook", is_fallback=True))
        with pytest.raises(ValueError, match="already is"):
            register_source(a_source("djvu", is_fallback=True))

    def test_no_fallback_at_all_is_an_error_not_a_default(self, clean_registry):
        """A failed import should say so, not quietly render everything as ink."""
        register_source(a_source("pdf"))
        with pytest.raises(LookupError):
            fallback_source()

    def test_the_shipped_registry_holds_the_three(self):
        assert set(SOURCE_REGISTRY) == {"epub", "notebook", "pdf"}

    def test_the_notebook_is_the_shipped_fallback(self):
        assert fallback_source() is NOTEBOOK


class TestResolution:
    """Which source claims a document, and on what evidence."""

    def test_the_file_type_is_the_authoritative_signal(self):
        assert source_for_file_type("pdf") is PDF
        assert source_for_file_type("epub") is EPUB

    def test_the_file_type_match_ignores_case_and_padding(self):
        assert source_for_file_type("  PDF ") is PDF

    def test_a_notebooks_empty_file_type_claims_nothing(self):
        """A notebook reports "" — matching it to a source would be a guess."""
        assert source_for_file_type("") is None
        assert source_for_file_type(None) is None

    def test_an_unknown_file_type_claims_nothing(self):
        assert source_for_file_type("djvu") is None

    def test_a_filename_is_a_weaker_signal_that_still_works(self):
        assert source_for_filename("Lecture notes.pdf") is PDF
        assert source_for_filename("Moby Dick.EPUB") is EPUB
        assert source_for_filename("Journal") is None

    def test_an_unknown_name_falls_back_rather_than_raising(self):
        """A document synced by a newer build must not crash an older one."""
        assert source_for_name("djvu") is NOTEBOOK
        assert source_for_name(None) is NOTEBOOK

    def test_the_suffix_list_is_what_the_transports_look_for(self):
        assert sorted(source_suffixes()) == ["epub", "pdf"]

    def test_the_notebook_contributes_no_suffix(self):
        """It has no embedded source file, so there is nothing to look for."""
        assert NOTEBOOK.source_suffix == ""


class TestEveryShippedSourceSatisfiesTheContract:
    """The Protocol is runtime-checkable, so the ABC-equivalent is testable."""

    @pytest.mark.parametrize("source", sorted(SOURCE_REGISTRY.values(), key=lambda s: s.name))
    def test_the_renderer_implements_the_protocol(self, source):
        assert isinstance(source.renderer, Renderer)

    @pytest.mark.parametrize("source", sorted(SOURCE_REGISTRY.values(), key=lambda s: s.name))
    def test_the_renderer_declares_a_version(self, source):
        """The version is half the cache key; a renderer without one is a bug.

        This replaces the module-wide ``RENDER_FORMAT_VERSION``: one number for
        three renderers meant a change to the PDF compositor threw away every
        cached notebook page.
        """
        assert isinstance(source.renderer.version, int)
        assert source.renderer.version >= 1


class TestRenderContextFingerprint:
    """What the context contributes to a cache key, and what it must not."""

    def test_the_background_changes_it(self):
        assert ctx(background="#FFFFFF").fingerprint() != ctx(background="#000000").fingerprint()

    def test_the_panel_changes_it(self):
        assert ctx(screen=(1404, 1872)).fingerprint() != ctx(screen=(1620, 2160)).fingerprint()

    def test_keeping_temp_files_does_not(self):
        """It decides what survives on disk, not what is drawn."""
        base = ctx()
        from dataclasses import replace

        assert replace(base, keep_temp=True).fingerprint() == base.fingerprint()


def notebook_zip(path: Path, pages: int = 2) -> Path:
    """Write a document zip holding ``pages`` distinct .rm files."""
    with zipfile.ZipFile(path, "w") as zf:
        ids = [f"page-{n}" for n in range(pages)]
        zf.writestr("doc.content", json.dumps({"pages": ids}))
        for n, page_id in enumerate(ids):
            zf.writestr(f"doc/{page_id}.rm", f"strokes-{n}".encode())
    return path


class TestNotebookRenderer:
    """Every page in the zip, in order, with no text layer."""

    def test_a_zip_with_pages_is_worth_rendering(self, tmp_path):
        bundle = SourceBundle(doc_id="d1", title="N", zip_path=notebook_zip(tmp_path / "d.zip"))
        assert NOTEBOOK.renderer.prepare(bundle, ctx()) is True

    def test_an_empty_notebook_has_nothing_to_render(self, tmp_path, monkeypatch):
        monkeypatch.setattr("living_ink.extract.get_document_page_count", lambda z: 0)
        bundle = SourceBundle(doc_id="d1", title="N", zip_path=tmp_path / "d.zip")
        assert NOTEBOOK.renderer.prepare(bundle, ctx()) is False

    def test_an_empty_notebook_is_a_skip_not_a_failure(self):
        """The user has not written anything yet; nothing is wrong."""
        assert NOTEBOOK.empty_is_skip is True

    def test_pages_are_numbered_from_one_with_no_gaps(self, tmp_path, monkeypatch):
        monkeypatch.setattr("living_ink.extract.get_page_source_hashes", lambda z: ["a", "b", "c"])
        bundle = SourceBundle(doc_id="d1", title="N", zip_path=tmp_path / "d.zip")
        refs = NOTEBOOK.renderer.pages(bundle, ctx())
        assert [(r.ordinal, r.number, r.source_key) for r in refs] == [
            (0, 1, "a"),
            (1, 2, "b"),
            (2, 3, "c"),
        ]

    def test_the_background_and_panel_reach_the_render_call(self, tmp_path, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            "living_ink.extract.render_page_from_document_zip",
            lambda zip_path, page, **kwargs: seen.update(kwargs, page=page) or b"png",
        )
        bundle = SourceBundle(doc_id="d1", title="N", zip_path=tmp_path / "d.zip")
        NOTEBOOK.renderer.render(bundle, PageRef(ordinal=0, number=1), ctx(background="#123456"))
        assert seen == {"page": 1, "background_color": "#123456", "screen": (1404, 1872)}

    def test_a_notebook_has_no_text_layer(self, tmp_path):
        bundle = SourceBundle(doc_id="d1", title="N", zip_path=tmp_path / "d.zip")
        assert NOTEBOOK.renderer.text_layer(bundle, ctx()) is None

    def test_pages_are_labelled_by_their_number(self, tmp_path):
        bundle = SourceBundle(doc_id="d1", title="N")
        refs = [PageRef(ordinal=0, number=1), PageRef(ordinal=1, number=2)]
        assert [d.label for d in NOTEBOOK.renderer.describe_pages(bundle, refs)] == [
            "Page 1",
            "Page 2",
        ]

    def test_a_notebook_page_carries_no_breadcrumbs(self, tmp_path):
        bundle = SourceBundle(doc_id="d1", title="N")
        (described,) = NOTEBOOK.renderer.describe_pages(bundle, [PageRef(ordinal=0, number=1)])
        assert described.breadcrumbs == ()


@pytest.fixture
def sample_pdf(tmp_path) -> Path:
    """A three-page PDF with real text on every page."""
    path = tmp_path / "book.pdf"
    doc = fitz.open()
    for n in range(3):
        page = doc.new_page(width=300, height=400)
        page.insert_text((50, 50), f"Chapter {n + 1}", fontsize=14)
    doc.save(str(path))
    doc.close()
    return path


def pdf_zip(path: Path, annotated: dict) -> Path:
    """Write a document zip for a PDF, annotated on the given page indexes.

    Args:
        path: Where to write the zip.
        annotated: ``{pdf page index: annotation bytes}``.
    """
    pages = [{"id": f"pg-{i}", "redir": {"value": i}} for i in sorted(annotated)]
    content = {"fileType": "pdf", "cPages": {"pages": pages}}
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("doc.content", json.dumps(content))
        for i, data in sorted(annotated.items()):
            zf.writestr(f"doc/pg-{i}.rm", data)
    return path


class TestPdfRenderer:
    """Only the pages that were written on, plus the document's own text."""

    def _bundle(self, tmp_path, sample_pdf, annotated) -> SourceBundle:
        return SourceBundle(
            doc_id="d1",
            title="Book",
            zip_path=pdf_zip(tmp_path / "d.zip", annotated),
            source_path=sample_pdf,
        )

    def test_the_pdf_being_on_disk_is_what_prepare_reports(self, tmp_path, sample_pdf):
        bundle = self._bundle(tmp_path, sample_pdf, {})
        assert PDF.renderer.prepare(bundle, ctx()) is True

    def test_a_pdf_that_cannot_be_obtained_is_not_renderable(self, tmp_path):
        bundle = SourceBundle(
            doc_id="d1",
            title="Book",
            zip_path=pdf_zip(tmp_path / "d.zip", {}),
            source_path=tmp_path / "absent.pdf",
        )
        assert PDF.renderer.prepare(bundle, ctx()) is False

    def test_an_unannotated_pdf_renders_zero_pages(self, tmp_path, sample_pdf):
        """It used to render its cover, which is not content the user wrote.

        One OCR call per document, spent transcribing somebody else's title
        page. What an unannotated PDF has worth publishing is its text layer.
        """
        bundle = self._bundle(tmp_path, sample_pdf, {})
        assert list(PDF.renderer.pages(bundle, ctx())) == []

    def test_an_unannotated_pdf_still_has_its_text(self, tmp_path, sample_pdf):
        bundle = self._bundle(tmp_path, sample_pdf, {})
        assert "Chapter 1" in PDF.renderer.text_layer(bundle, ctx())

    def test_a_pdf_with_neither_is_a_failure_not_a_skip(self):
        """It was supposed to have content and did not."""
        assert PDF.empty_is_skip is False

    def test_page_numbers_are_sparse(self, tmp_path, sample_pdf):
        """Annotating page 3 of a 3-page PDF yields page 3, not page 1."""
        bundle = self._bundle(tmp_path, sample_pdf, {2: b"ink"})
        refs = list(PDF.renderer.pages(bundle, ctx()))
        assert [(r.ordinal, r.number) for r in refs] == [(0, 3)]

    def test_the_page_key_covers_the_ink_and_the_page_it_is_on(self, tmp_path, sample_pdf):
        """The same strokes over a different PDF page is a different composite."""
        here = list(PDF.renderer.pages(self._bundle(tmp_path, sample_pdf, {0: b"ink"}), ctx()))
        there = list(PDF.renderer.pages(self._bundle(tmp_path, sample_pdf, {2: b"ink"}), ctx()))
        assert here[0].source_key != there[0].source_key

    def test_the_same_page_hashes_the_same_twice(self, tmp_path, sample_pdf):
        first = list(PDF.renderer.pages(self._bundle(tmp_path, sample_pdf, {1: b"ink"}), ctx()))
        second = list(PDF.renderer.pages(self._bundle(tmp_path, sample_pdf, {1: b"ink"}), ctx()))
        assert first[0].source_key == second[0].source_key

    def test_rendering_composites_onto_the_right_pdf_page(self, tmp_path, sample_pdf):
        bundle = self._bundle(tmp_path, sample_pdf, {1: b""})
        (ref,) = PDF.renderer.pages(bundle, ctx())
        assert PDF.renderer.render(bundle, ref, ctx()) is not None

    def test_a_page_outside_the_pdf_renders_to_nothing(self, tmp_path, sample_pdf):
        """None is a counted failure upstream, not a silent drop."""
        bundle = self._bundle(tmp_path, sample_pdf, {})
        ref = PageRef(ordinal=0, number=999, detail={"pdf_page_index": 998, "rm_bytes": b""})
        assert PDF.renderer.render(bundle, ref, ctx()) is None

    def test_labels_come_from_the_document(self, tmp_path, sample_pdf):
        bundle = self._bundle(tmp_path, sample_pdf, {1: b"ink"})
        refs = list(PDF.renderer.pages(bundle, ctx()))
        (described,) = PDF.renderer.describe_pages(bundle, refs)
        assert "2" in described.label

    def test_describing_pages_does_not_reopen_the_document_per_page(
        self, tmp_path, sample_pdf, monkeypatch
    ):
        """Per-page labelling is how the destination used to reopen a PDF 300×.

        Counted against page count rather than against a fixed number: what
        matters is that describing three pages is not three times the work of
        describing one, whatever the constant happens to be.
        """
        opens = []
        real = fitz.open
        monkeypatch.setattr(fitz, "open", lambda *a, **k: opens.append(a) or real(*a, **k))

        def describe(annotated):
            # The TOC reader is memoised per path, so each subdirectory gets a
            # copy of the PDF and the count starts from cold.
            copy = tmp_path / f"{len(annotated)}.pdf"
            copy.write_bytes(sample_pdf.read_bytes())
            bundle = SourceBundle(
                doc_id="d1",
                title="Book",
                zip_path=pdf_zip(tmp_path / f"{len(annotated)}.zip", annotated),
                source_path=copy,
            )
            opens.clear()
            PDF.renderer.describe_pages(bundle, list(PDF.renderer.pages(bundle, ctx())))
            return len(opens)

        one = describe({1: b"b"})
        assert one, "the document was never read, so this proves nothing"
        assert describe({0: b"a", 1: b"b", 2: b"c"}) == one


class TestEpubRenderer:
    """The book's text, plus whatever was written on top of it."""

    def test_annotation_pages_alone_are_enough_to_render(self, tmp_path, monkeypatch):
        """An EPUB whose source file the cloud did not serve still has its ink."""
        monkeypatch.setattr("living_ink.extract.get_document_page_count", lambda z: 2)
        bundle = SourceBundle(
            doc_id="d1", title="Book", zip_path=tmp_path / "d.zip", source_path=tmp_path / "gone"
        )
        assert EPUB.renderer.prepare(bundle, ctx()) is True

    def test_neither_the_book_nor_a_page_is_not_renderable(self, tmp_path, monkeypatch):
        monkeypatch.setattr("living_ink.extract.get_document_page_count", lambda z: 0)
        bundle = SourceBundle(
            doc_id="d1", title="Book", zip_path=tmp_path / "d.zip", source_path=tmp_path / "gone"
        )
        assert EPUB.renderer.prepare(bundle, ctx()) is False

    def test_an_epub_with_neither_is_a_failure_not_a_skip(self):
        assert EPUB.empty_is_skip is False

    def test_a_missing_book_has_no_text_rather_than_an_empty_one(self, tmp_path):
        bundle = SourceBundle(doc_id="d1", title="Book", source_path=tmp_path / "gone.epub")
        assert EPUB.renderer.text_layer(bundle, ctx()) is None

    def test_annotation_pages_are_numbered_from_one(self, tmp_path, monkeypatch):
        """An EPUB reflows, so a page number is a position, not a page of a book."""
        monkeypatch.setattr("living_ink.extract.get_page_source_hashes", lambda z: ["a", "b"])
        bundle = SourceBundle(doc_id="d1", title="Book", zip_path=tmp_path / "d.zip")
        refs = EPUB.renderer.pages(bundle, ctx())
        assert [(r.ordinal, r.number) for r in refs] == [(0, 1), (1, 2)]

    def test_pages_are_labelled_by_their_number(self, tmp_path):
        bundle = SourceBundle(doc_id="d1", title="Book")
        (described,) = EPUB.renderer.describe_pages(bundle, [PageRef(ordinal=0, number=1)])
        assert described == type(described)(label="Page 1", breadcrumbs=())


class TestExtractingTheSourceFile:
    """Out of the zip, or from the transport when the zip did not carry it."""

    def _zip_with(self, path: Path, name: str, data: bytes) -> Path:
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr(name, data)
        return path

    def test_the_zip_is_the_first_place_looked(self, tmp_path):
        bundle = SourceBundle(
            doc_id="d1",
            title="Book",
            zip_path=self._zip_with(tmp_path / "d.zip", "doc.pdf", b"%PDF-1.4"),
            source_path=tmp_path / "out.pdf",
        )
        assert extract_source_file(bundle) == tmp_path / "out.pdf"
        assert (tmp_path / "out.pdf").read_bytes() == b"%PDF-1.4"

    def test_a_file_already_on_disk_is_not_re_extracted(self, tmp_path):
        existing = tmp_path / "out.pdf"
        existing.write_bytes(b"kept")
        bundle = SourceBundle(
            doc_id="d1",
            title="Book",
            zip_path=self._zip_with(tmp_path / "d.zip", "doc.pdf", b"replaced"),
            source_path=existing,
        )
        assert extract_source_file(bundle) == existing
        assert existing.read_bytes() == b"kept"

    def test_the_transport_is_asked_when_the_zip_has_nothing(self, tmp_path, monkeypatch):
        """Some documents come back from the cloud without their source file."""
        monkeypatch.setattr("living_ink.api.download_raw_file", lambda c, i, s: b"%PDF-direct")
        bundle = SourceBundle(
            doc_id="d1",
            title="Book",
            zip_path=self._zip_with(tmp_path / "d.zip", "doc/pg.rm", b"ink"),
            source_path=tmp_path / "out.pdf",
            client=object(),
        )
        assert extract_source_file(bundle) == tmp_path / "out.pdf"
        assert (tmp_path / "out.pdf").read_bytes() == b"%PDF-direct"

    def test_the_suffix_asked_for_is_the_one_wanted(self, tmp_path, monkeypatch):
        asked = []
        monkeypatch.setattr(
            "living_ink.api.download_raw_file", lambda c, i, s: asked.append(s) or None
        )
        bundle = SourceBundle(
            doc_id="d1",
            title="Book",
            zip_path=self._zip_with(tmp_path / "d.zip", "doc/pg.rm", b"ink"),
            source_path=tmp_path / "out.epub",
            client=object(),
        )
        extract_source_file(bundle)
        assert asked == ["epub"]

    def test_a_notebook_asks_for_nothing(self, tmp_path):
        """It has no source path, so there is no file to look for."""
        bundle = SourceBundle(doc_id="d1", title="N", zip_path=tmp_path / "d.zip")
        assert extract_source_file(bundle) is None

    def test_neither_route_producing_a_file_is_None_not_a_phantom_path(self, tmp_path):
        bundle = SourceBundle(
            doc_id="d1",
            title="Book",
            zip_path=self._zip_with(tmp_path / "d.zip", "doc/pg.rm", b"ink"),
            source_path=tmp_path / "out.pdf",
        )
        assert extract_source_file(bundle) is None


class TestTheSuffixListDrivesExtraction:
    """``extract_raw_document_from_zip`` reads the registry rather than a literal."""

    def _zip(self, path: Path, *names: str) -> Path:
        with zipfile.ZipFile(path, "w") as zf:
            for name in names:
                zf.writestr(name, b"payload")
        return path

    def test_a_registered_suffix_is_pulled_out(self, tmp_path):
        found = extract.extract_raw_document_from_zip(
            self._zip(tmp_path / "d.zip", "doc.epub"), tmp_path / "out.epub"
        )
        assert found == tmp_path / "out.epub"

    def test_an_unregistered_suffix_is_left_alone(self, tmp_path):
        found = extract.extract_raw_document_from_zip(
            self._zip(tmp_path / "d.zip", "doc.djvu"), tmp_path / "out.djvu"
        )
        assert found is None

    def test_a_caller_can_narrow_it(self, tmp_path):
        """A renderer that would rather not accept a neighbouring format."""
        found = extract.extract_raw_document_from_zip(
            self._zip(tmp_path / "d.zip", "doc.epub"), tmp_path / "out", suffixes=("pdf",)
        )
        assert found is None

    def test_an_empty_suffix_list_matches_nothing(self, tmp_path):
        """Not "matches everything" — ``endswith(())`` is False, but say so."""
        found = extract.extract_raw_document_from_zip(
            self._zip(tmp_path / "d.zip", "doc.pdf"), tmp_path / "out", suffixes=()
        )
        assert found is None
