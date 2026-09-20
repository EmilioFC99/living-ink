"""The annotated-PDF source: composites of the page and what was drawn on it."""

import hashlib
import zipfile
from typing import List, Optional, Sequence

from living_ink.sources.base import (
    PageDescription,
    PageRef,
    RenderContext,
    SourceBundle,
    SourceType,
    extract_source_file,
    register_source,
)


class PdfRenderer:
    """Renders the pages of a PDF that were written on, and nothing else.

    **An unannotated PDF renders zero pages.** It used to render its first page
    as a "cover preview", which is not content the user wrote, costs one OCR
    call per document, and produced a note whose whole body was a transcription
    of somebody else's title page. What an unannotated PDF has that is worth
    publishing is its text layer, and :meth:`text_layer` is where that lives —
    so a document with no pages and a text layer is a complete, valid result,
    not an empty one.
    """

    version = 1

    def prepare(self, bundle: SourceBundle, ctx: RenderContext) -> bool:
        """Put the underlying PDF on disk.

        Returns:
            False when the PDF itself could not be obtained. Without it there
            is neither a page to composite onto nor a text layer to read, and
            :class:`SourceType` declares that a failure rather than a skip.
        """
        return extract_source_file(bundle) is not None

    def pages(self, bundle: SourceBundle, ctx: RenderContext) -> Sequence[PageRef]:
        """List the annotated pages, by their number in the underlying document.

        Sparse by nature: a 400-page PDF with three annotated pages yields 12,
        200 and 377. The annotation bytes are read here, both to key the page
        on its content and so :meth:`render` does not reopen the zip per page.
        """
        from living_ink.extract import get_pdf_annotated_page_map

        annotated = get_pdf_annotated_page_map(bundle.zip_path)
        if not annotated:
            return []

        refs: List[PageRef] = []
        with zipfile.ZipFile(bundle.zip_path, "r") as zf:
            names = set(zf.namelist())
            for ordinal, info in enumerate(annotated):
                rm_name = info["rm_file_name"]
                rm_bytes = zf.read(rm_name) if rm_name in names else b""
                # Both halves matter: the same strokes over a different page of
                # the PDF is a different image, and the composite is what is
                # cached.
                digest = hashlib.sha256()
                digest.update(str(info["pdf_page_index"]).encode("utf-8"))
                digest.update(b"\0")
                digest.update(rm_bytes)
                refs.append(
                    PageRef(
                        ordinal=ordinal,
                        number=info["page_num"],
                        source_key=digest.hexdigest(),
                        detail={
                            "pdf_page_index": info["pdf_page_index"],
                            "rm_bytes": rm_bytes,
                        },
                    )
                )
        return refs

    def render(self, bundle: SourceBundle, page: PageRef, ctx: RenderContext) -> Optional[bytes]:
        """Composite one annotated page over the PDF page it belongs to."""
        from living_ink.extract import render_composite_pdf_page

        source = bundle.source_file()
        if source is None:
            return None
        return render_composite_pdf_page(
            source,
            page.detail["pdf_page_index"],
            page.detail["rm_bytes"],
            screen=ctx.device.screen,
        )

    def text_layer(self, bundle: SourceBundle, ctx: RenderContext) -> Optional[str]:
        """Return the PDF's embedded text, or None when it has none."""
        from living_ink.extract import extract_text_from_pdf

        source = bundle.source_file()
        if source is None:
            return None
        return extract_text_from_pdf(source) or None

    def describe_pages(
        self, bundle: SourceBundle, pages: Sequence[PageRef]
    ) -> Sequence[PageDescription]:
        """Label pages from the PDF's own labels and outline, reading it once."""
        from living_ink.extract import get_pdf_toc_breadcrumbs, page_labels

        source = bundle.source_file()
        numbers = [page.number for page in pages]
        labels = page_labels(numbers, source)
        return [
            PageDescription(
                label=labels.get(page.number, f"Page {page.number}"),
                breadcrumbs=tuple(get_pdf_toc_breadcrumbs(page.number, source)),
            )
            for page in pages
        ]


PDF = register_source(
    SourceType(
        name="pdf",
        file_type_values=("pdf",),
        name_suffixes=(".pdf",),
        source_suffix="pdf",
        renderer=PdfRenderer(),
        label="PDF",
        empty_is_skip=False,
    )
)
