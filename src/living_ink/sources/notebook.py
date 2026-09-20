"""The handwritten notebook source — the fallback every other type falls back to."""

from typing import List, Optional, Sequence

from living_ink.sources.base import (
    PageDescription,
    PageRef,
    RenderContext,
    SourceBundle,
    SourceType,
    register_source,
)


class NotebookRenderer:
    """Renders every page of a reMarkable notebook from its ``.rm`` sources.

    The simplest of the three: the zip *is* the document, every page renders,
    and the page numbers are consecutive. There is no text layer — a notebook
    is ink, and reading it is what the OCR pass is for.
    """

    #: 1 because the version is now per renderer. The module-wide
    #: ``RENDER_FORMAT_VERSION`` it replaces had reached 4, but it also covered
    #: the PDF and EPUB paths, so carrying its number forward here would claim
    #: a history this renderer does not have.
    version = 1

    def prepare(self, bundle: SourceBundle, ctx: RenderContext) -> bool:
        """Report whether the notebook has any pages at all.

        Returns:
            False for an empty notebook, which :class:`SourceType` declares a
            skip rather than a failure.
        """
        from living_ink.extract import get_document_page_count

        return get_document_page_count(bundle.zip_path) > 0

    def pages(self, bundle: SourceBundle, ctx: RenderContext) -> Sequence[PageRef]:
        """List every page of the notebook, in the order the tablet holds them.

        The per-page source digest is computed here even when the render cache
        is off: hashing the ``.rm`` entries of a zip already on disk is cheap,
        and the digests are what let a later stage tell "this page changed"
        from "the renderer changed".
        """
        from living_ink.extract import get_page_source_hashes

        hashes = get_page_source_hashes(bundle.zip_path)
        return [
            PageRef(ordinal=index, number=index + 1, source_key=digest)
            for index, digest in enumerate(hashes)
        ]

    def render(self, bundle: SourceBundle, page: PageRef, ctx: RenderContext) -> Optional[bytes]:
        """Render one page of ink to a PNG."""
        from living_ink.extract import render_page_from_document_zip

        return render_page_from_document_zip(
            bundle.zip_path,
            page.number,
            background_color=ctx.background,
            screen=ctx.device.screen,
        )

    def text_layer(self, bundle: SourceBundle, ctx: RenderContext) -> Optional[str]:
        """Return None: a notebook carries no text, only strokes."""
        return None

    def describe_pages(
        self, bundle: SourceBundle, pages: Sequence[PageRef]
    ) -> Sequence[PageDescription]:
        """Label pages by their number. A notebook has no table of contents."""
        descriptions: List[PageDescription] = []
        for page in pages:
            descriptions.append(PageDescription(label=f"Page {page.number}"))
        return descriptions


NOTEBOOK = register_source(
    SourceType(
        name="notebook",
        # A notebook is what a document is when it is nothing else: the
        # transports report no fileType for one, so it matches by exclusion.
        file_type_values=(),
        name_suffixes=(),
        source_suffix="",
        renderer=NotebookRenderer(),
        label="Notebook",
        is_fallback=True,
        empty_is_skip=True,
    )
)
