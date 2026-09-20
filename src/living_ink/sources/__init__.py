"""Document types and how to render them.

This is the extension point for new source formats. Adding one is: a module
here holding a :class:`~living_ink.sources.base.Renderer` implementation and a
:class:`~living_ink.sources.base.SourceType`, one
:func:`~living_ink.sources.base.register_source` call, and one import below.
Nothing in the pipeline changes — it resolves the source from the registry and
calls the contract.

The imports below are what populates :data:`SOURCE_REGISTRY`, so they are not
decoration: a source module nobody imports is a source that does not exist.
They are alphabetical, and unlike the destination registry the order carries no
meaning — a document matches exactly one source by ``fileType``, and the one
fallback is declared rather than positional.
"""

from living_ink.sources.base import (
    SOURCE_REGISTRY,
    PageDescription,
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
from living_ink.sources.epub import EPUB, EpubRenderer
from living_ink.sources.notebook import NOTEBOOK, NotebookRenderer
from living_ink.sources.pdf import PDF, PdfRenderer

__all__ = [
    "EPUB",
    "NOTEBOOK",
    "PDF",
    "SOURCE_REGISTRY",
    "EpubRenderer",
    "NotebookRenderer",
    "PageDescription",
    "PageRef",
    "PdfRenderer",
    "RenderContext",
    "Renderer",
    "SourceBundle",
    "SourceType",
    "extract_source_file",
    "fallback_source",
    "register_source",
    "source_for_file_type",
    "source_for_filename",
    "source_for_name",
    "source_suffixes",
]
