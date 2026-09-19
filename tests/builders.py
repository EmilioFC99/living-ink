"""Builders for the two objects a destination is handed.

:meth:`Destination.publish` takes a :class:`Document` and a
:class:`PublishContext`. Almost every test cares about one field of one of
them, so these fill in the rest — and they are the only place a test has to be
edited when a field is added to either.

Dates are accepted as ``YYYY-MM-DD`` strings, because that is what a test wants
to read; the domain model keeps them as datetimes.
"""

import datetime
from pathlib import Path
from typing import Iterable, Optional, Sequence, Union

from living_ink.core.document import Document, Page, PublishContext

When = Union[str, datetime.datetime, None]


def _moment(value: When) -> Optional[datetime.datetime]:
    """Turn a ``YYYY-MM-DD`` string into a datetime, passing anything else on.

    Args:
        value: A date string, a datetime, or None.

    Returns:
        The datetime, or None.
    """
    if isinstance(value, str):
        return datetime.datetime.fromisoformat(value)
    return value


def make_page(
    number: int = 1,
    text: str = "",
    *,
    index: Optional[int] = None,
    label: Optional[str] = None,
    image: Optional[Path] = None,
    breadcrumbs: Sequence[str] = (),
    error: Optional[str] = None,
    source_key: str = "",
) -> Page:
    """Build one page, defaulting the presentation fields the renderer sets.

    Args:
        number: The page number as the document counts them.
        text: What the page transcribed to.
        index: Position in the document, defaulting to ``number - 1``.
        label: What the page is called, defaulting to ``Page <number>``.
        image: The rendered PNG, when the test has one.
        breadcrumbs: The source document's TOC path to this page.
        error: Why the page has no text, when it failed.
        source_key: The cache key of the page this was rendered from.

    Returns:
        The page.
    """
    return Page(
        index=number - 1 if index is None else index,
        number=number,
        label=label if label is not None else f"Page {number}",
        breadcrumbs=tuple(breadcrumbs),
        image_path=image,
        text=text,
        source_key=source_key,
        error=error,
    )


def make_document(
    title: str = "Notes",
    text: str = "",
    images: Iterable[Path] = (),
    *,
    pages: Optional[Sequence[Page]] = None,
    body_text: Optional[str] = None,
    folder: Sequence[str] = (),
    tags: Sequence[str] = (),
    doc_id: str = "doc-1",
    source: str = "notebook",
    modified: When = None,
    source_file: Optional[Path] = None,
) -> Document:
    """Build a document, spelling ``text`` as pages when there are pages.

    ``text`` is a convenience, not a field: a document with rendered pages
    carries its text on them, and one without carries it in ``body_text``.
    Pass ``pages`` when the arrangement matters.

    Args:
        title: The notebook's own title, with no folder glued to it.
        text: What the document says, placed wherever it belongs.
        images: One rendered page image per page, in page order.
        pages: The pages, when the test builds them itself.
        body_text: Text lifted out of the source file rather than read off a
            page. Overrides where ``text`` would otherwise land.
        folder: The reMarkable folder hierarchy, outermost first.
        tags: Tags on the notebook or its pages.
        doc_id: reMarkable document id.
        source: ``notebook``, ``pdf`` or ``epub``.
        modified: When the tablet says it was last written on.
        source_file: The original PDF or EPUB, when one was retrieved.

    Returns:
        The document.
    """
    image_list = list(images)
    if pages is None:
        pages = [
            make_page(number=n, text=text if n == 1 else "", image=image)
            for n, image in enumerate(image_list, start=1)
        ]
    if body_text is None:
        body_text = text if text and not pages else None

    return Document(
        doc_id=doc_id,
        title=title,
        folder_path=tuple(folder),
        source=source,
        modified=_moment(modified),
        tags=tuple(tags),
        pages=tuple(pages),
        body_text=body_text,
        source_file=source_file,
    )


def make_context(
    doc_id: str = "doc-1",
    *,
    dry_run: bool = False,
    existing_external_id: Optional[str] = None,
    existing_target: Optional[str] = None,
    adopt_by_name: bool = False,
    first_published: When = None,
    settings=None,
) -> PublishContext:
    """Build the context describing what a destination did with this document.

    Args:
        doc_id: reMarkable document id.
        dry_run: Whether the run is forbidden from writing anything.
        existing_external_id: The id the destination reported last time.
        existing_target: Where the note landed last time.
        adopt_by_name: Permission to match a note on its title.
        first_published: When this note first reached the destination.
        settings: The run's resolved settings, when a test needs them.

    Returns:
        The context.
    """
    return PublishContext(
        doc_id=doc_id,
        dry_run=dry_run,
        existing_external_id=existing_external_id,
        existing_target=existing_target,
        adopt_by_name=adopt_by_name,
        first_published=_moment(first_published),
        settings=settings,
    )


def make_both(title: str = "Notes", text: str = "", images: Iterable[Path] = (), **kwargs):
    """Build a document and a context together, splitting the keyword arguments.

    A shorthand for the many tests that publish once and assert on the note:
    ``dest.publish(*make_both("Notes", "body", doc_id="doc-1"))``.

    Args:
        title: The notebook's title.
        text: What the document says.
        images: Rendered page images.
        **kwargs: Any field of either object. ``doc_id`` goes to both.

    Returns:
        The ``(document, context)`` pair.

    Raises:
        TypeError: A keyword names no field of either object.
    """
    doc_fields = ("pages", "body_text", "folder", "tags", "source", "modified", "source_file")
    ctx_fields = (
        "dry_run",
        "existing_external_id",
        "existing_target",
        "adopt_by_name",
        "first_published",
        "settings",
    )
    unknown = set(kwargs) - set(doc_fields) - set(ctx_fields) - {"doc_id"}
    if unknown:
        raise TypeError(f"Not a field of Document or PublishContext: {', '.join(sorted(unknown))}")

    doc_id = kwargs.get("doc_id", "doc-1")
    doc = make_document(
        title,
        text,
        images,
        doc_id=doc_id,
        **{k: v for k, v in kwargs.items() if k in doc_fields},
    )
    ctx = make_context(doc_id, **{k: v for k, v in kwargs.items() if k in ctx_fields})
    return doc, ctx


__all__ = ["make_both", "make_context", "make_document", "make_page"]
