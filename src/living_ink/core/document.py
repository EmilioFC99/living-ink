"""The domain model that travels between the pipeline and a destination.

This module is a leaf: it imports nothing else from Living Ink, which is what
lets ``destinations/`` read it without importing the pipeline it would
otherwise have to depend on. :class:`PublishContext` names
:class:`~living_ink.settings.Settings` in an annotation only, under
``TYPE_CHECKING``, so the leaf stays a leaf at runtime.

Every field here has a named producer and a named consumer. A field with no
consumer gets filled in wrong, because nobody knows what it is for; a field
with no producer hands every consumer ``None`` and surfaces three layers away.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Tuple

if TYPE_CHECKING:  # pragma: no cover - import for type checkers only
    from living_ink.settings import Settings


@dataclass(frozen=True, slots=True)
class Page:
    """One renderable page of a document, with what a destination needs to place it.

    The page number used to be recovered by regexing the PNG filename and the
    label by reopening the source PDF — once per page, from inside the
    destination layer, so a 300-page annotated PDF opened the file 300 times
    during publish. All of it is known at render time, so all of it is a field.

    Attributes:
        index: Zero-based position in the published sequence. Stable within a
            run, and the sort key that keeps pages in page order.
        number: The page number to show a human. ``index + 1`` for a notebook;
            for an annotated PDF it is the document's own page number, which is
            sparse — a 400-page PDF with three annotated pages yields 12, 200
            and 377.
        label: Presentation string for a page heading, e.g. ``"Page 200"``.
            Never appears in :attr:`text`.
        breadcrumbs: The document's TOC path for this page, outermost first.
            Empty for a notebook, which has no outline; a destination renders
            nothing at all rather than an empty separator.
        image_path: The rendered PNG, or None if this page has no image. Valid
            only for the duration of the publish call that received it: it
            points into a temp directory that is purged after every notebook.
        text: The transcription, or ``""``. Body content only — no page
            heading, no divider, no HTML. What a heading looks like is the
            destination's business.
        source_key: Opaque per-source identifier: the ``.rm`` source digest for
            a notebook page, the PDF page index for a composite. Matches a
            re-render to its source.
        error: Why this page has no transcription, or None. The only way to
            tell a page that failed from a page that was blank — ``text`` is
            ``""`` for both, because a marker is presentation and ``text`` is
            body content. A destination turns a non-None error into a visible
            gap so the next run's merge has somewhere to put the real text.
    """

    index: int
    number: int
    label: str
    breadcrumbs: Tuple[str, ...] = ()
    image_path: Optional[Path] = None
    text: str = ""
    source_key: str = ""
    error: Optional[str] = None


@dataclass(frozen=True, slots=True)
class Document:
    """A transcribed document, ready to publish. Destination-neutral.

    Attributes:
        doc_id: The reMarkable document id. This is the identity, not the title.
        title: The document's own name, with no folder path in it.
        folder_path: The tablet's folder tree, outermost first. Empty at the root.
        source: The document type — ``"notebook"``, ``"pdf"``, ``"epub"``.
        modified: When the tablet says the user last wrote on it, normalised to
            a datetime. Not the time of the sync, and not the file's mtime.
        tags: Tags read from the tablet, de-duplicated, order preserved.
        pages: The rendered and transcribed pages, in publication order.
        body_text: A text layer extracted from the source file, if the source
            has one — PDFs and EPUBs do, notebooks do not. This is *not* the
            concatenation of the page texts: it is the publisher's own text,
            and it is what a destination publishes for a document that has a
            text layer and no annotated pages.
        source_file: The extracted ``.pdf`` / ``.epub``, or None. Valid only
            during publish, like :attr:`Page.image_path`.
    """

    doc_id: str
    title: str
    folder_path: Tuple[str, ...] = ()
    source: str = "notebook"
    modified: Optional[datetime] = None
    tags: Tuple[str, ...] = ()
    pages: Tuple[Page, ...] = ()
    body_text: Optional[str] = None
    source_file: Optional[Path] = None

    def has_text(self) -> bool:
        """Report whether anything at all was transcribed or extracted.

        Returns:
            True if any page carries text, or the source had a text layer.
        """
        return bool(self.body_text and self.body_text.strip()) or any(
            p.text.strip() for p in self.pages
        )

    def failed_pages(self) -> int:
        """Count the pages that have an error rather than a transcription.

        Returns:
            How many pages failed. Equal to ``len(pages)`` means the document
            failed, not that it published with gaps.
        """
        return sum(1 for p in self.pages if p.error)


@dataclass(frozen=True, slots=True)
class PublishContext:
    """What a destination needs about *this* publish that is not the document.

    :attr:`doc_id` is duplicated from :class:`Document` on purpose:
    :meth:`~living_ink.destinations.base.Destination.unpublish` is driven from
    a state-store row alone, with no Document in hand.

    Attributes:
        doc_id: The reMarkable document id.
        dry_run: When true the destination must not mutate anything.
        existing_external_id: The id this destination returned last time, or
            None. Replacing exactly that object is the only safe re-publish.
        existing_target: Where this destination reported putting the note last
            time. Feeding it back is what makes a renamed notebook *move* its
            note instead of growing a second one.
        adopt_by_name: Permission to fall back to matching on title when no
            :attr:`existing_external_id` is known. Only true when the state
            store says this document was published here before, which means a
            note with that title was almost certainly written by Living Ink.
        first_published: When this document first reached this destination.
            One term of Obsidian's ``created`` cascade.
        settings: The run's resolved settings.
    """

    doc_id: str
    dry_run: bool = False
    existing_external_id: Optional[str] = None
    existing_target: Optional[str] = None
    adopt_by_name: bool = False
    first_published: Optional[datetime] = None
    settings: Optional["Settings"] = None


@dataclass(frozen=True)
class PublishResult:
    """The outcome of one publish or unpublish.

    Returned rather than recorded on the destination: a destination used to
    announce where a note landed by setting ``last_target`` on itself and
    letting the caller read it back, which made every destination stateful
    across documents for no reason and left the two facts free to describe
    different notes.

    Attributes:
        ok: Whether the document reached the destination.
        target: Where it landed, in whatever form the destination records.
            Obsidian uses a vault-relative posix string. Fed back to the
            destination on the next run so a renamed notebook moves its note
            instead of growing a second one.
        external_id: The destination's own id for the object, for a
            destination that has one. Obsidian identifies a note by the
            ``living_ink_id`` in its frontmatter and sets none.
        detail: One short line for the run report.
        warnings: Things the user has to fix by hand. The publish stage routes
            them to :meth:`~living_ink.report.RunReport.warn`, so they survive
            the log lines scrolling past.
    """

    ok: bool
    target: Optional[str] = None
    external_id: Optional[str] = None
    detail: str = ""
    warnings: Tuple[str, ...] = field(default=())
