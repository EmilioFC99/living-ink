"""The publish lifecycle shared by every destination that writes files.

A filesystem destination always does the same seven things in the same order,
and the order is a dependency order rather than an obvious one:

1. ``resolve_location`` — which directory.
2. ``resolve_name`` — which filename, knowing the directory.
3. ``prepare`` — create directories, move a renamed note, read what is there.
4. ``write_attachments`` — images and the source document, knowing the note's
   own path, because that is what decides where its attachments live.
5. ``render_body`` — the note's content, knowing what the attachments are
   called so it can reference them.
6. ``render_metadata`` — the frontmatter, knowing the body and the existing
   note, because a ``created`` date is read off the note it is replacing.
7. ``commit`` — one atomic write.

**Stages 1 and 2 must be safe to run against a read-only vault.** That is the
invariant the split exists to protect: everything before stage 3 only reads, so
``--dry-run`` cuts cleanly between 2 and 3 and can still report the exact path
it would have written. Before the split, the first thing publish did was
``mkdir``, so a dry run left empty folders across the vault. **Both ``mkdir``
calls belong in ``prepare``** — putting one in ``resolve_location`` puts it back
on the wrong side of the cut.

This is the lifecycle axis, not the content axis. Turning a document into one
destination's markup is :mod:`living_ink.destinations.markup`, and it is
deliberately separate: Apple Notes shares the content transformation and can
share none of this, because it creates a note and its attachments inside a
single ``osascript`` call.
"""

import abc
import logging
import shutil
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import ClassVar, Dict, List, Optional

from living_ink.core.document import Document, PublishContext, PublishResult
from living_ink.destinations.base import Destination, DestinationError
from living_ink.safeio import PathEscapesRoot, contained_path, write_text_atomic

logger = logging.getLogger(__name__)


class AttachmentPolicy(str, Enum):
    """Whether a note's attachments get a directory of their own.

    One question with four consequences, and they were four separate readings
    of one overloaded blank string. ``attachments_folder = ""`` meant "beside
    the note", *and* silently disabled relocating attachments when a note moved,
    *and* disabled deleting them on unpublish, *and* changed the image filename
    to a note-prefixed form. Four booleans in a trench coat: the combinations
    nothing handles are the ones a reader cannot see are impossible.

    They are all the same question. In ``OWNED`` the directory holds this note's
    attachments and nothing else, so it can be moved wholesale, deleted
    wholesale, and its filenames need no prefix to stay unique. In ``BESIDE``
    the directory is the note's own folder, shared with every other note and
    with whatever else the user keeps there, so none of those three are safe.

    A ``str`` mixin because the floor is Python 3.10, which has no ``StrEnum``.
    """

    OWNED = "owned"
    BESIDE = "beside"


@dataclass(frozen=True)
class NoteBlock:
    """One addressable unit of generated content.

    A destination whose :attr:`~living_ink.destinations.base.Destination.merge_unit`
    is ``PAGE`` emits one of these per source page, so a re-publish rewrites
    that page and leaves the note the user wrote underneath it alone. One whose
    merge unit is ``DOCUMENT`` emits exactly one.

    Attributes:
        block_id: Stable across syncs, and the whole point: it comes from the
            source document, so it does not renumber when a highlight is added
            above it and does not move when the model transcribes the page
            differently.
        content: The rendered markup for that unit.
    """

    block_id: str
    content: str


@dataclass
class NoteLayout:
    """The scratch state one publish accumulates as it moves through the stages.

    Mutable and short-lived, and the reason the stages can be separate methods
    without becoming twelve-argument ones. A destination instance holds none of
    this: two documents published in the same run share nothing, which is what
    stops one note's target being written into another note's state row.

    Attributes:
        target_dir: The directory the note goes in. Set by ``resolve_location``.
        note_path: The note's full path. None until ``resolve_name`` has run,
            and asserted before the dry-run cut so a destination cannot skip
            the one stage whose answer the cut reports.
        existing_text: The note that is already there, or None. Read exactly
            once, in ``prepare`` — re-reading it after the attachments are
            written would be reading a file the same publish may have moved.
        attachment_links: Page index → the reference this destination uses for
            that page's image. What
            :attr:`~living_ink.destinations.markup.WriteContext.resolve_attachment`
            answers from.
        doc_link_target: Reference to the copied source PDF or EPUB, if there
            was one.
        blocks: The rendered content, as addressable units in the order they
            should appear. Set by ``render_body``.
        metadata: The rendered frontmatter or header. Set by ``render_metadata``.
        warnings: Things the user has to fix by hand. Returned on the
            :class:`PublishResult` whether the publish succeeded or not.
    """

    target_dir: Optional[Path] = None
    note_path: Optional[Path] = None
    existing_text: Optional[str] = None
    attachment_links: Dict[int, str] = field(default_factory=dict)
    doc_link_target: Optional[str] = None
    blocks: List[NoteBlock] = field(default_factory=list)
    metadata: str = ""
    warnings: List[str] = field(default_factory=list)


class FileSystemDestination(Destination):
    """A destination whose notes are files in a directory tree.

    Provides the lifecycle, the containment guarantee and the sentinel note.
    Implements **none** of :meth:`check`, :meth:`unpublish`, :meth:`describe` or
    :meth:`from_config` on purpose: there is one filesystem destination today,
    and a shared default written from one data point is a guess that the second
    subclass would have to unpick.

    Class attributes:
        note_suffix: The extension a note is written with.
        sentinel_name: Filename of the note written by :meth:`report_failure`.
        sentinel_id: The reserved document id that note carries. A subclass
            that assigns filenames must never hand this out.
    """

    note_suffix: ClassVar[str] = ".md"
    sentinel_name: ClassVar[str] = "Living Ink — sync failed"
    sentinel_id: ClassVar[str] = "living-ink-sentinel"

    # ------------------------------------------------------------------
    # What a subclass must answer
    # ------------------------------------------------------------------

    @property
    @abc.abstractmethod
    def root_path(self) -> Path:
        """The directory every note and attachment must stay inside.

        Returns:
            The tree's root. Everything :meth:`contained` builds is checked
            against it, and every ``target`` reported is relative to it.
        """

    @property
    def notes_root(self) -> Path:
        """The directory notes are filed under, which may be below the root.

        Distinct from :attr:`root_path`, which is the containment boundary:
        Obsidian contains everything within the vault but files its notes under
        a configured folder inside it. The sentinel note goes here, so it sits
        with the notes it is reporting on rather than at the top of somebody's
        whole vault.

        Returns:
            The notes directory. The containment root, unless overridden.
        """
        return self.root_path

    @property
    def attachment_policy(self) -> AttachmentPolicy:
        """Whether this destination owns the directory a note's images land in.

        Returns:
            :attr:`AttachmentPolicy.OWNED` unless overridden. A destination that
            can put attachments in a directory it does not own says so here
            once, rather than at each of the places that would be unsafe.
        """
        return AttachmentPolicy.OWNED

    @abc.abstractmethod
    def resolve_location(self, doc: Document, ctx: PublishContext, layout: NoteLayout) -> None:
        """Stage 1 — decide which directory the note belongs in.

        Must not create it. Runs before the dry-run cut, so it must be safe
        against a read-only vault.

        Args:
            doc: The document being published.
            ctx: This publish.
            layout: Scratch state; set :attr:`NoteLayout.target_dir`.
        """

    @abc.abstractmethod
    def resolve_name(self, doc: Document, ctx: PublishContext, layout: NoteLayout) -> None:
        """Stage 2 — decide the note's filename.

        May read the directory to avoid claiming another document's note. Must
        not write. Runs before the dry-run cut.

        Args:
            doc: The document being published.
            ctx: This publish.
            layout: Scratch state; set :attr:`NoteLayout.note_path`.
        """

    @abc.abstractmethod
    def prepare(self, doc: Document, ctx: PublishContext, layout: NoteLayout) -> None:
        """Stage 3 — create directories, relocate a renamed note, read it.

        The first stage allowed to touch the disk, and the home of **both**
        ``mkdir`` calls.

        Args:
            doc: The document being published.
            ctx: This publish.
            layout: Scratch state; set :attr:`NoteLayout.existing_text`.
        """

    @abc.abstractmethod
    def write_attachments(self, doc: Document, ctx: PublishContext, layout: NoteLayout) -> None:
        """Stage 4 — copy the page images and the source document into place.

        Args:
            doc: The document being published.
            ctx: This publish.
            layout: Scratch state; fill :attr:`NoteLayout.attachment_links` and
                :attr:`NoteLayout.doc_link_target`.
        """

    @abc.abstractmethod
    def render_body(self, doc: Document, ctx: PublishContext, layout: NoteLayout) -> None:
        """Stage 5 — turn the document into this destination's markup.

        Args:
            doc: The document being published.
            ctx: This publish.
            layout: Scratch state; set :attr:`NoteLayout.body`.
        """

    @abc.abstractmethod
    def render_metadata(self, doc: Document, ctx: PublishContext, layout: NoteLayout) -> None:
        """Stage 6 — build the frontmatter or header.

        Args:
            doc: The document being published.
            ctx: This publish.
            layout: Scratch state; set :attr:`NoteLayout.metadata`.
        """

    @abc.abstractmethod
    def commit(self, doc: Document, ctx: PublishContext, layout: NoteLayout) -> PublishResult:
        """Stage 7 — write the note, once.

        Args:
            doc: The document being published.
            ctx: This publish.
            layout: Scratch state, fully populated.

        Returns:
            The outcome. :meth:`write_note` builds the usual one.
        """

    # ------------------------------------------------------------------
    # What the base provides
    # ------------------------------------------------------------------

    def contained(self, root: Path, *segments: str) -> Path:
        """Join untrusted path segments under a root, or refuse.

        A folder name comes off the tablet and a root folder comes out of
        ``config.yml``; neither is trusted to be a name rather than an
        instruction. Translates :class:`~living_ink.safeio.PathEscapesRoot`
        into a :class:`DestinationError`, because ``safeio`` is a
        standard-library-only leaf and cannot name this layer's error type.

        Args:
            root: The directory the result must stay inside.
            *segments: Path components, innermost last.

        Returns:
            The resolved path.

        Raises:
            DestinationError: The result would land outside ``root``.
        """
        try:
            return contained_path(root, *segments)
        except PathEscapesRoot as e:
            raise DestinationError(f"Refusing to write outside '{root}': {e}") from e

    def relative_target(self, path: Path) -> str:
        """Render a path as the root-relative string recorded in ``state.db``.

        Args:
            path: A path inside :attr:`root_path`.

        Returns:
            The posix-style relative path, or the absolute path when it is
            somehow outside the root — a target nobody can resolve is worse
            than an ugly one.
        """
        try:
            return path.relative_to(self.root_path).as_posix()
        except ValueError:
            return path.as_posix()

    def published_exists(self, ctx: PublishContext) -> bool:
        """Report whether the note recorded for this document is still there.

        Args:
            ctx: Carries the recorded ``existing_target``.

        Returns:
            True if a file sits at the recorded path.
        """
        if not ctx.existing_target:
            return False
        return (self.root_path / ctx.existing_target).is_file()

    def write_note(self, layout: NoteLayout, text: str) -> PublishResult:
        """Write the note atomically and describe where it went.

        Written in one step: a note half-replaced by an interrupted sync is
        indistinguishable from a transcription that came back truncated, so the
        user would have no reason to suspect a crash.

        Args:
            layout: Scratch state, with :attr:`NoteLayout.note_path` set.
            text: The complete file content.

        Returns:
            The outcome, carrying the root-relative target.
        """
        assert layout.note_path is not None, "commit runs after resolve_name"
        write_text_atomic(layout.note_path, text)
        target = self.relative_target(layout.note_path)
        logger.info("%s note written at: %s", self.display_name, layout.note_path)
        return PublishResult(ok=True, target=target, detail=target)

    def publish(self, doc: Document, ctx: PublishContext) -> PublishResult:
        """Run the seven stages, and turn any expected failure into a result.

        One ``try`` for the whole sequence, so a stage says what went wrong by
        raising and never has to decide what a failure means. What it means is
        decided here, once: the document did not publish, the reason is
        user-facing, and the run continues to the next document.

        Args:
            doc: The transcribed document.
            ctx: This publish.

        Returns:
            The outcome. Warnings collected by the stages ride along whether
            the publish succeeded or not — a failed run is the one whose
            warnings matter most.
        """
        layout = NoteLayout()
        try:
            self.resolve_location(doc, ctx, layout)
            self.resolve_name(doc, ctx, layout)
            if layout.note_path is None:
                raise DestinationError(f"No path was worked out for '{doc.title}'.")

            target = self.relative_target(layout.note_path)
            if ctx.dry_run:
                # The cut. Stages 1 and 2 only read, so the exact path can be
                # promised without anything having been created to promise it.
                return PublishResult(
                    ok=True,
                    target=target,
                    detail=f"Would write '{target}'.",
                    warnings=tuple(layout.warnings),
                )

            self.prepare(doc, ctx, layout)
            self.write_attachments(doc, ctx, layout)
            self.render_body(doc, ctx, layout)
            self.render_metadata(doc, ctx, layout)
            result = self.commit(doc, ctx, layout)
        except DestinationError as e:
            return PublishResult(ok=False, detail=str(e), warnings=tuple(layout.warnings))
        except (OSError, shutil.Error) as e:
            detail = f"Could not write '{doc.title}' under {self.root_path}: {e}"
            return PublishResult(ok=False, detail=detail, warnings=tuple(layout.warnings))

        if layout.warnings:
            result = PublishResult(
                ok=result.ok,
                target=result.target,
                external_id=result.external_id,
                detail=result.detail,
                warnings=tuple([*result.warnings, *layout.warnings]),
            )
        return result

    # ------------------------------------------------------------------
    # The sentinel note
    # ------------------------------------------------------------------

    def _sentinel_path(self) -> Path:
        """Where the sync-failed note lives.

        Returns:
            A path directly under :attr:`notes_root`, not under the mirrored
            folder tree — the failure is the run's, not one notebook's.
        """
        return self.notes_root / f"{self.sentinel_name}{self.note_suffix}"

    def report_failure(self, summary: str) -> None:
        """Leave a note in the vault saying the last sync did not finish.

        A CLI that fails in a terminal nobody is watching has told nobody. The
        vault is the one place the user *is* looking, so the failure goes
        there — as an ordinary note they can read, and one that disappears by
        itself when a sync next succeeds.

        Deliberately not a publication: no ``state.db`` row, no ``doc_id`` that
        could collide with a real notebook, and an id
        (:attr:`sentinel_id`) that :meth:`resolve_name` must never assign.

        Args:
            summary: What went wrong, in the user's words. Written as the
                note's body.
        """
        try:
            path = self._sentinel_path()
        except DestinationError:
            logger.warning("Nowhere to write the sync-failed note.", exc_info=True)
            return
        text = (
            "---\n"
            f"living_ink_id: {self.sentinel_id}\n"
            "tags:\n  - living-ink\n"
            "---\n\n"
            "# Living Ink could not finish its last sync\n\n"
            f"{summary.strip()}\n\n"
            "This note is written by Living Ink and deleted automatically the "
            "next time a sync completes.\n"
        )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            write_text_atomic(path, text)
        except OSError:
            # The run has already failed; failing to say so is not worth a
            # second traceback on top of the first.
            logger.warning("Could not write the sync-failed note at %s", path, exc_info=True)

    def clear_failure(self) -> None:
        """Remove the sync-failed note, if one is there.

        Only removes a file carrying :attr:`sentinel_id`, so a note the user
        happens to have named the same thing survives.
        """
        try:
            path = self._sentinel_path()
            if not path.is_file():
                return
            if self.sentinel_id not in path.read_text(encoding="utf-8"):
                logger.info("Not deleting %s: it is not Living Ink's.", path)
                return
            path.unlink()
        except (OSError, DestinationError):
            logger.warning("Could not remove the sync-failed note at %s", path, exc_info=True)


__all__ = ["FileSystemDestination", "NoteLayout"]
