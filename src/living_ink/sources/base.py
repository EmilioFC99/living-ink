"""The source contract: what a document type has to be able to do.

A *source* is a kind of thing the tablet can hold — a handwritten notebook, an
annotated PDF, an EPUB. Each one is a :class:`SourceType` (data: how to
recognise it, what its file extension is, what "nothing to render" means for
it) plus a :class:`Renderer` (behaviour: how to turn a downloaded bundle into
page images and, where the format has one, a text layer).

The obvious ``page_count()`` / ``render_page(index)`` pair does not fit, for
three reasons this contract is shaped around:

1. An annotated PDF renders **sparse page numbers**. A 400-page PDF with three
   annotated pages yields 12, 200 and 377, and an index-based API loses the
   number. Hence :class:`PageRef`, which carries the ordinal and the number as
   two separate facts.
2. PDFs and EPUBs carry an **embedded text layer** that bypasses OCR entirely.
   A protocol returning only page images has nowhere to put it, and dropping it
   turns every unannotated PDF and every EPUB into "nothing could be
   extracted". Hence :meth:`Renderer.text_layer`.
3. The three paths need different preparation — a notebook renders from the
   open zip, an EPUB extracts text from the source file first, a PDF composite
   needs *both* the extracted ``.pdf`` and the still-open zip. Hence
   :meth:`Renderer.prepare` and the :class:`SourceBundle` it fills in.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional, Sequence, Tuple

try:  # pragma: no cover - typing_extensions is not a runtime dependency
    from typing import Protocol, runtime_checkable
except ImportError:  # pragma: no cover
    from typing_extensions import Protocol, runtime_checkable  # type: ignore

from living_ink.transport import DeviceInfo


@dataclass(frozen=True)
class PageRef:
    """One page a renderer intends to produce.

    Attributes:
        ordinal: Zero-based position in the render sequence.
        number: The document page number to display. Equals ``ordinal + 1`` for
            notebooks and EPUB annotation pages; sparse for annotated PDFs,
            where it is the page of the underlying document that was written
            on.
        source_key: Digest of everything about this page that is not the
            renderer or the run — the ``.rm`` bytes for a notebook, the PDF page
            index *and* the annotation bytes for a composite. It is the page's
            half of the render cache key and the value drift detection compares,
            so it has to be content, not a filename. Empty when the source
            could not be hashed, which disables caching for that page rather
            than caching it under a key that does not describe it.
        detail: Whatever the renderer needs to render this page and does not
            want to recompute. Opaque to everyone else.
    """

    ordinal: int
    number: int
    source_key: str = ""
    detail: Dict[str, Any] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class PageDescription:
    """What a page is called and where it sits in the source's structure.

    §9.1 of the design calls this ``page_header()`` and has it return a
    formatted string. It returns the parts instead: the label and the
    breadcrumb trail are payload, and the destination decides how to render
    them — a Markdown heading and a JSON field want the same two facts
    differently. Building the string here would put presentation back in the
    pipeline, which §5.4 spent a row taking out of it.

    Attributes:
        label: The page's own name — "Page 4", or "Page xii (pdf-12)" when the
            document labels its pages differently from their position.
        breadcrumbs: The section trail active at this page, outermost first.
            Empty for anything without a table of contents.
    """

    label: str
    breadcrumbs: Tuple[str, ...] = ()


@dataclass(frozen=True)
class RenderContext:
    """Everything a renderer needs that is not the document.

    Attributes:
        device: What the transport knows about the tablet the pages were drawn
            on. Its panel size is what a page with no content bounds is
            rendered at, so a Paper Pro page rendered as a reMarkable 2 comes
            back squashed.
        background: Background colour, e.g. ``"#FFFFFF"``, or None for
            transparent. **This must actually reach the renderer.** It used to
            be folded into the cache key and then not passed to the render
            call, so changing it invalidated every cached page and produced
            byte-identical images.
        keep_temp: Whether intermediate artifacts survive the purge. A renderer
            that writes one honours this; the pipeline honours it for the
            downloaded zip.
    """

    device: DeviceInfo
    background: Optional[str] = None
    keep_temp: bool = False

    def fingerprint(self) -> str:
        """Identify the parts of this context that change what a page looks like.

        Returns:
            A short string for the render cache key. ``keep_temp`` is absent on
            purpose: it decides what survives on disk, not what is drawn.
        """
        width, height = self.device.screen
        return f"{self.background}:{width}x{height}"


@dataclass
class SourceBundle:
    """One document's files on disk, for the duration of a render.

    Mutable and short-lived: :meth:`Renderer.prepare` is what fills in
    :attr:`source_path`, and the whole object is discarded when the document
    has been rendered.

    Attributes:
        doc_id: The document's identity on the tablet.
        title: Its display title, for log lines.
        zip_path: The downloaded document zip. None in the one case where a
            bundle exists only to label pages that were already rendered — the
            zip is gone by then, and labelling reads the source document rather
            than the zip. Every other method on :class:`Renderer` is entitled to
            assume it is there.
        source_path: Where the embedded ``.pdf``/``.epub`` should live, and does
            once :meth:`Renderer.prepare` has run. None for a notebook, which
            has no source file to extract.
        item: The transport's own handle on the document, for the direct
            download fallback when the zip did not carry the source file.
        client: The transport. A renderer reaches for this only through
            :func:`~living_ink.sources.extract_source_file`.
    """

    doc_id: str
    title: str
    zip_path: Optional[Path] = None
    source_path: Optional[Path] = None
    item: Any = None
    client: Any = None

    def source_file(self) -> Optional[Path]:
        """Return the extracted source document, if it is actually on disk."""
        if self.source_path and self.source_path.exists():
            return self.source_path
        return None


@runtime_checkable
class Renderer(Protocol):
    """Turns a downloaded document bundle into pages and, optionally, text."""

    #: Bumped whenever this renderer's output changes. Part of the page cache
    #: key. This replaces the single module-wide ``RENDER_FORMAT_VERSION``:
    #: once there are three renderers, one global number cannot say which of
    #: them changed, so every bump invalidated all three.
    version: ClassVar[int]

    def prepare(self, bundle: SourceBundle, ctx: RenderContext) -> bool:
        """Extract whatever this source needs from the bundle.

        Args:
            bundle: The document's files; ``source_path`` may be filled in here.
            ctx: The run's render settings.

        Returns:
            False when the document holds nothing this renderer can produce.
            The caller turns that into a *skip* or a *failure* according to
            :attr:`SourceType.empty_is_skip`.
        """
        ...

    def pages(self, bundle: SourceBundle, ctx: RenderContext) -> Sequence[PageRef]:
        """List the pages this renderer will produce, in publication order."""
        ...

    def render(self, bundle: SourceBundle, page: PageRef, ctx: RenderContext) -> Optional[bytes]:
        """Render one page to PNG bytes.

        Returns:
            The PNG, or None when the page cannot be rendered. None is a
            *counted failure*, not a skip — the PDF path used to drop
            unrenderable pages silently and never count them.
        """
        ...

    def text_layer(self, bundle: SourceBundle, ctx: RenderContext) -> Optional[str]:
        """Extract the source's own text, if it has one. None for notebooks."""
        ...

    def describe_pages(
        self, bundle: SourceBundle, pages: Sequence[PageRef]
    ) -> Sequence[PageDescription]:
        """Label every page at once, in the order it was given them.

        Batched rather than per page because labelling an annotated PDF means
        reading the document, and doing that once per page is how the
        destination used to reopen the same PDF three hundred times.

        Args:
            bundle: The document's files, still on disk.
            pages: The pages to describe.

        Returns:
            One description per page, in the same order.
        """
        ...


@dataclass(frozen=True)
class SourceType:
    """A registered document type.

    Attributes:
        name: Registry key, and what ``Document.source`` records. ``"notebook"``,
            ``"pdf"``, ``"epub"``.
        file_type_values: Values of ``fileType`` in the document's ``.content``
            blob that select this source. **This is the authoritative
            discriminator** — it is what the transports report.
        name_suffixes: Title suffixes, e.g. ``(".pdf",)``. A weak fallback only:
            ``.rm`` files are inside *every* zip, so an extension-based registry
            cannot discriminate at all.
        source_suffix: The extracted source file's extension without the dot,
            ``""`` for a notebook, which has no embedded source.
        is_fallback: Exactly one source is the fallback — the answer when
            nothing else matches. It used to be ``"notebook"``, hardcoded in
            four places.
        empty_is_skip: Whether "nothing to render" is a skip or a failure. An
            empty document (notebook, PDF, or EPUB with 0 annotated pages) is
            a skip: there is nothing wrong, the user has not written or
            highlighted anything.
        renderer: The :class:`Renderer` for this type.
        label: Display name for menus and the report.
    """

    name: str
    file_type_values: Tuple[str, ...]
    name_suffixes: Tuple[str, ...]
    source_suffix: str
    renderer: Renderer
    label: str
    is_fallback: bool = False
    empty_is_skip: bool = False


#: Every registered source, keyed by name. Registration order is resolution
#: order for :func:`source_for_name`, which only matters for the suffix
#: fallback; the ``fileType`` match is unambiguous.
SOURCE_REGISTRY: Dict[str, SourceType] = {}


def register_source(source: SourceType) -> SourceType:
    """Add a source to :data:`SOURCE_REGISTRY`.

    Decorator-free on purpose: a source is data plus a renderer, not a class
    hierarchy, so there is nothing to decorate.

    Args:
        source: The source to register.

    Returns:
        The same source, so a module can assign the result.

    Raises:
        ValueError: If the name is already registered, or if registering this
            source would leave the registry with two fallbacks. Two fallbacks
            means the answer to "what is this document" depends on dict order.
    """
    if source.name in SOURCE_REGISTRY:
        raise ValueError(f"source {source.name!r} is already registered")
    if source.is_fallback:
        existing = [s.name for s in SOURCE_REGISTRY.values() if s.is_fallback]
        if existing:
            raise ValueError(
                f"source {source.name!r} claims to be the fallback, but {existing[0]!r} "
                "already is; exactly one source answers when nothing matches"
            )
    SOURCE_REGISTRY[source.name] = source
    return source


def fallback_source() -> SourceType:
    """Return the source that handles a document nothing else claims.

    Returns:
        The one :class:`SourceType` with ``is_fallback``.

    Raises:
        LookupError: If no source claims it. That is a failed import, not a
            configuration a caller should paper over with a default.
    """
    for source in SOURCE_REGISTRY.values():
        if source.is_fallback:
            return source
    raise LookupError("no source is registered as the fallback")


def source_for_name(name: Optional[str]) -> SourceType:
    """Return the source registered under ``name``, or the fallback.

    Args:
        name: A source name as stored on a job, or None.

    Returns:
        The matching source, or :func:`fallback_source` when the name is
        unknown. An unknown name is how a document synced by a newer build
        looks to an older one, and it should render as a notebook rather than
        crash.
    """
    if name and name in SOURCE_REGISTRY:
        return SOURCE_REGISTRY[name]
    return fallback_source()


def source_for_file_type(file_type: Optional[str]) -> Optional[SourceType]:
    """Return the source a transport's ``fileType`` value selects.

    Args:
        file_type: The value the transport reported, in any case.

    Returns:
        The matching source, or None when nothing claims that value — which
        includes the empty string a notebook reports.
    """
    if not file_type:
        return None
    lowered = file_type.strip().lower()
    for source in SOURCE_REGISTRY.values():
        if lowered in source.file_type_values:
            return source
    return None


def source_for_filename(name: Optional[str]) -> Optional[SourceType]:
    """Return the source a filename's extension suggests.

    A weak signal, and the only one available when the transport cannot be
    asked: every document zip contains ``.rm`` files, so the *contents* of the
    zip discriminate nothing.

    Args:
        name: A filename or display title.

    Returns:
        The matching source, or None.
    """
    if not name:
        return None
    lowered = str(name).lower()
    for source in SOURCE_REGISTRY.values():
        if source.name_suffixes and lowered.endswith(source.name_suffixes):
            return source
    return None


def extract_source_file(bundle: SourceBundle) -> Optional[Path]:
    """Put the document's embedded PDF or EPUB on disk and return it.

    The zip usually carries it. When it does not — the cloud API serves some
    documents without their source file — the transport is asked for it
    directly. Both renderers that have a source file need exactly this, so it
    lives here rather than twice.

    Args:
        bundle: The document being rendered. ``source_path`` says where the
            file should land and is left alone if it is already there.

    Returns:
        The path to the source file, or None when neither route produced one.
    """
    from living_ink.api import download_raw_file
    from living_ink.extract import extract_raw_document_from_zip

    if bundle.source_path is None:
        return None
    if bundle.source_path.exists():
        return bundle.source_path

    extract_raw_document_from_zip(bundle.zip_path, bundle.source_path)
    if not bundle.source_path.exists() and bundle.client is not None:
        suffix = bundle.source_path.suffix.lstrip(".")
        raw = download_raw_file(bundle.client, bundle.item, suffix)
        if raw:
            bundle.source_path.write_bytes(raw)
    return bundle.source_file()


def source_suffixes() -> List[str]:
    """Return every embedded source extension, without the dot.

    The transports use this to know which files to look for beside a document
    on the device, instead of each of them carrying its own ``("pdf", "epub")``.

    Returns:
        One suffix per source that has one, in registration order.
    """
    return [s.source_suffix for s in SOURCE_REGISTRY.values() if s.source_suffix]
