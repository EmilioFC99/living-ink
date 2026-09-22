"""The EPUB source: the book's text, plus whatever was written on top of it."""

from pathlib import Path
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

    When opened on a reMarkable tablet, the device reflows the EPUB into a
    fixed-layout PDF (cached directly inside the document bundle) and anchors
    ink strokes and highlights to the pages of that PDF. When that pre-rendered
    PDF is present, this composites annotations onto it just like an annotated
    PDF and publishes the PDF to attachments. If no PDF is available, it falls
    back to rendering ink on a plain canvas.
    """

    version = 4

    def prepare(self, bundle: SourceBundle, ctx: RenderContext) -> bool:
        """Put the book or its device-rendered PDF on disk.

        Returns:
            True when either the book itself, the device-rendered PDF, or an
            annotation page was found.
        """
        from living_ink.extract import extract_raw_document_from_zip, get_document_page_count

        book = extract_source_file(bundle)

        if bundle.zip_path and bundle.source_path:
            pdf_path = bundle.source_path.with_suffix(".pdf")
            if not pdf_path.exists():
                extract_raw_document_from_zip(bundle.zip_path, pdf_path, suffixes=("pdf",))
            if not pdf_path.exists() and bundle.client is not None:
                from living_ink.api import download_raw_file

                raw = download_raw_file(bundle.client, bundle.item, "pdf")
                if raw:
                    pdf_path.write_bytes(raw)
            if pdf_path.exists():
                bundle.source_path = pdf_path

        return (
            bundle.source_file() is not None
            or book is not None
            or get_document_page_count(bundle.zip_path) > 0
        )

    def _pdf_path(self, bundle: SourceBundle) -> Optional[Path]:
        source = bundle.source_file()
        if source and source.suffix.lower() == ".pdf":
            return source
        if bundle.source_path and bundle.source_path.with_suffix(".pdf").exists():
            return bundle.source_path.with_suffix(".pdf")
        return None

    def pages(self, bundle: SourceBundle, ctx: RenderContext) -> Sequence[PageRef]:
        """List the annotated pages, in order."""
        import hashlib
        import zipfile

        from living_ink.extract import (
            get_page_source_hashes,
            get_pdf_annotated_page_map,
            inspect_rm_bytes,
        )

        pdf_path = self._pdf_path(bundle)
        if pdf_path and bundle.zip_path and bundle.zip_path.exists():
            annotated = get_pdf_annotated_page_map(bundle.zip_path)
            if annotated:
                refs: List[PageRef] = []
                with zipfile.ZipFile(bundle.zip_path, "r") as zf:
                    names = set(zf.namelist())
                    for ordinal, info in enumerate(annotated):
                        rm_name = info["rm_file_name"]
                        rm_bytes = zf.read(rm_name) if rm_name in names else b""
                        stats = inspect_rm_bytes(rm_bytes)
                        if stats is not None and not stats.has_content:
                            continue
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

        return [
            PageRef(ordinal=index, number=index + 1, source_key=digest)
            for index, digest in enumerate(get_page_source_hashes(bundle.zip_path))
        ]

    def render(self, bundle: SourceBundle, page: PageRef, ctx: RenderContext) -> Optional[bytes]:
        """Render one annotated page composite, or fall back to ink-only."""
        from living_ink.extract import render_composite_pdf_page, render_page_from_document_zip

        pdf_path = self._pdf_path(bundle)
        if pdf_path and "pdf_page_index" in page.detail:
            return render_composite_pdf_page(
                pdf_path,
                page.detail["pdf_page_index"],
                page.detail["rm_bytes"],
                screen=ctx.device.screen,
            )

        return render_page_from_document_zip(
            bundle.zip_path,
            page.number,
            background_color=ctx.background,
            screen=ctx.device.screen,
        )

    def text_layer(self, bundle: SourceBundle, ctx: RenderContext) -> Optional[str]:
        """Return None: only annotated pages are synced from EPUBs."""
        return None

    def describe_pages(
        self, bundle: SourceBundle, pages: Sequence[PageRef]
    ) -> Sequence[PageDescription]:
        """Label annotation pages by their number and breadcrumbs if available."""
        from living_ink.extract import get_pdf_toc_breadcrumbs, page_labels

        pdf_path = self._pdf_path(bundle)
        if pdf_path:
            numbers = [page.number for page in pages]
            labels = page_labels(numbers, pdf_path)
            return [
                PageDescription(
                    label=labels.get(page.number, f"Page {page.number}"),
                    breadcrumbs=tuple(get_pdf_toc_breadcrumbs(page.number, pdf_path)),
                )
                for page in pages
            ]

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
        empty_is_skip=True,
    )
)
