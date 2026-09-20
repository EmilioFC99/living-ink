"""The EPUB source: the book's text, plus whatever was written on top of it."""

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


class EpubRenderer:
    """Reads an EPUB's text and renders the annotation pages beside it.

    An EPUB has no fixed pages to composite onto — the tablet reflows it — so
    an annotation is stored as a standalone ``.rm`` page in the zip, the same
    shape a notebook page has. That is why this renders through the notebook
    path while reading its text from the book file.
    """

    version = 1

    def prepare(self, bundle: SourceBundle, ctx: RenderContext) -> bool:
        """Put the book on disk, and report whether there is anything to publish.

        Returns:
            True when either the book itself or an annotation page was found.
            A book that yielded neither is a failure — the document exists on
            the tablet, so producing nothing from it means something went
            wrong.
        """
        from living_ink.extract import get_document_page_count

        book = extract_source_file(bundle)
        return book is not None or get_document_page_count(bundle.zip_path) > 0

    def pages(self, bundle: SourceBundle, ctx: RenderContext) -> Sequence[PageRef]:
        """List the annotation pages the zip carries, in order."""
        from living_ink.extract import get_page_source_hashes

        return [
            PageRef(ordinal=index, number=index + 1, source_key=digest)
            for index, digest in enumerate(get_page_source_hashes(bundle.zip_path))
        ]

    def render(self, bundle: SourceBundle, page: PageRef, ctx: RenderContext) -> Optional[bytes]:
        """Render one annotation page — the same ink path a notebook uses."""
        from living_ink.extract import render_page_from_document_zip

        return render_page_from_document_zip(
            bundle.zip_path,
            page.number,
            background_color=ctx.background,
            screen=ctx.device.screen,
        )

    def text_layer(self, bundle: SourceBundle, ctx: RenderContext) -> Optional[str]:
        """Return the book's text, or None when the book could not be read."""
        from living_ink.extract import extract_text_from_epub

        book = bundle.source_file()
        if book is None:
            return None
        return extract_text_from_epub(book) or None

    def describe_pages(
        self, bundle: SourceBundle, pages: Sequence[PageRef]
    ) -> Sequence[PageDescription]:
        """Label annotation pages by their number.

        An EPUB has no PDF outline and no page labels: the positions the tablet
        assigns its annotations are the only numbering there is.
        """
        descriptions: List[PageDescription] = []
        for page in pages:
            descriptions.append(PageDescription(label=f"Page {page.number}"))
        return descriptions


EPUB = register_source(
    SourceType(
        name="epub",
        file_type_values=("epub",),
        name_suffixes=(".epub",),
        source_suffix="epub",
        renderer=EpubRenderer(),
        label="EPUB",
        empty_is_skip=False,
    )
)
