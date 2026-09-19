"""Publishing to a local Obsidian vault as Markdown files.

Notes are plain files, so this destination can mirror the tablet's folder tree,
move a note whose notebook was renamed, and splice its own content into a note
the user has also written in.

The publish lifecycle lives in
:class:`~living_ink.destinations.filesystem.FileSystemDestination` and the
Markdown itself in :mod:`living_ink.destinations.markup`; what is left here is
the part that is genuinely Obsidian's — its frontmatter, its WikiLinks, and the
``living_ink_id`` that makes a note's identity survive a rename.
"""

import datetime
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional

from living_ink import notemerge
from living_ink.core.document import Document, Page, PublishContext, PublishResult
from living_ink.destinations.base import (
    Destination,
    DestinationError,
    DestinationStatus,
    MergeUnit,
    register_destination,
)
from living_ink.destinations.filesystem import FileSystemDestination, NoteLayout
from living_ink.destinations.markup import (
    Block,
    BlockKind,
    ObsidianWriter,
    WriteContext,
    image_block,
    to_blocks,
)
from living_ink.settings import Settings

logger = logging.getLogger(__name__)


def _iso_date(moment: Optional[datetime.datetime]) -> Optional[str]:
    """Render a timestamp as the ``YYYY-MM-DD`` a frontmatter date wants.

    Args:
        moment: The timestamp, or None when nothing is known.

    Returns:
        The date, or None. Never today's date as a stand-in: the caller has to
        be able to tell "the tablet did not say" from "the tablet said today".
    """
    return moment.date().isoformat() if moment else None


@register_destination("obsidian")
class ObsidianDestination(FileSystemDestination):
    """Publishes notes to a local Obsidian Vault as Markdown files.

    Supports configurable root folder prefixes, complete directory hierarchy
    mirroring, WikiLink image attachments, and YAML frontmatter.

    Attributes:
        vault_path: Path to the root of the Obsidian Vault.
        attachments_folder: Subfolder name for image attachments.
        root_folder: Optional folder prefix inside the vault.
        mirror_folders: Whether to replicate reMarkable folder hierarchy.
    """

    # The class name, because that is what every existing state.db row says.
    state_key: ClassVar[str] = "ObsidianDestination"
    display_name: ClassVar[str] = "Obsidian"

    # A note is one managed region today, so a re-publish rewrites all of it.
    # This becomes PAGE once the region is split into a marked block per source
    # page; until then, claiming PAGE would promise the user something the
    # write path does not honour.
    merge_unit: ClassVar[MergeUnit] = MergeUnit.DOCUMENT

    # Characters forbidden in filenames across macOS, Windows, Linux, and Obsidian
    FORBIDDEN_CHARS_REGEX = re.compile(r'[/\\:*?"<>|#^\[\]]')

    @classmethod
    def from_config(cls, section: Dict[str, Any], settings: Settings) -> Optional["Destination"]:
        """Build an Obsidian destination, or skip it if no vault is configured.

        Every value comes from the resolved settings rather than the section,
        so a vault named by ``LIVING_INK_OBSIDIAN_VAULT_PATH`` or by a flag
        outranks the one in the file. The section is still what decides whether
        this destination runs at all; :func:`build_destinations` reads that.

        Returns:
            The destination, or None when ``vault_path`` is missing — a vault
            is the one thing this destination cannot guess.
        """
        if not settings.obsidian_vault_path:
            print("⚠️ Obsidian enabled but 'vault_path' is missing. Skipping.")
            return None
        return cls(
            vault_path=settings.obsidian_vault_path,
            attachments_folder=settings.obsidian_attachments_folder,
            root_folder=settings.obsidian_root_folder,
            mirror_folders=settings.obsidian_mirror_folders,
        )

    def describe(self) -> str:
        """Name this destination, its vault, and the root folder if one is set."""
        root = f" (Root: {self.root_folder})" if self.root_folder else ""
        return f"Obsidian (Vault: {self.vault_path}{root})"

    def check(self) -> DestinationStatus:
        """Confirm the vault is a directory this process can write into.

        Three separate failures, because the remedy differs: the path is not
        there, the path is a file, or the path is read-only. A vault on an
        unmounted drive is the common one and looks like the first.

        Returns:
            Whether notes can be written, and what to fix if not.
        """
        if not self.vault_path.exists():
            return DestinationStatus(
                ok=False,
                detail=f"The Obsidian vault '{self.vault_path}' does not exist.",
                remedy=(
                    "Check the path, or mount the drive it lives on, then set it with "
                    "'living-ink config' or LIVING_INK_OBSIDIAN_VAULT_PATH."
                ),
            )
        if not self.vault_path.is_dir():
            return DestinationStatus(
                ok=False,
                detail=f"The Obsidian vault '{self.vault_path}' is a file, not a folder.",
                remedy="Point vault_path at the vault folder itself.",
            )
        if not os.access(self.vault_path, os.W_OK):
            return DestinationStatus(
                ok=False,
                detail=f"The Obsidian vault '{self.vault_path}' is not writable.",
                remedy="Grant write access to the vault folder, or choose another vault.",
            )
        return DestinationStatus(ok=True, detail=f"Vault '{self.vault_path}' is writable.")

    def __init__(
        self,
        vault_path: str,
        attachments_folder: str = "_attachments",
        root_folder: Optional[str] = None,
        mirror_folders: bool = True,
    ) -> None:
        """Initialize ObsidianDestination.

        Args:
            vault_path: Absolute or home-relative path to the Obsidian Vault.
            attachments_folder: Subfolder name for page attachments.
                Set to empty string ("") to store attachments in the same
                directory as the note. Defaults to "_attachments".
            root_folder: Optional folder inside the vault where all notes
                will be stored (e.g., "Living Ink" or "reMarkable"). If omitted
                or empty, notes are placed in the vault root. Defaults to None.
            mirror_folders: If True, mirrors the full reMarkable folder
                structure inside the vault/root_folder. If False, all notes
                are placed flat directly inside the vault/root_folder.
                Defaults to True.

        Note:
            A vault that does not exist is not an error here. Construction
            never validates; :meth:`check` reports, and preflight refuses the
            run. Raising here meant ``build_destinations`` caught it, printed a
            warning, and left the run with nothing to publish to and no way to
            tell that apart from having nothing to publish.
        """
        self.vault_path = Path(vault_path).expanduser().resolve()
        self.attachments_folder = attachments_folder.strip() if attachments_folder else ""
        self.root_folder = root_folder.strip() if root_folder else ""
        self.mirror_folders = mirror_folders
        self._writer = ObsidianWriter()

    @property
    def root_path(self) -> Path:
        """The vault, which is what every path here is contained within."""
        return self.vault_path

    @property
    def notes_root(self) -> Path:
        """The configured root folder inside the vault, where notes are filed."""
        return self._root_dir()

    def _sanitize_filename(self, name: str) -> str:
        """Sanitize a filename or folder segment for filesystem compatibility.

        Replaces forbidden characters (/ \\ : * ? " < > | # ^ [ ]) with a hyphen,
        preserving regular spaces and alphanumeric characters.

        Note:
            This is cosmetic, not a containment check. A segment of ``..``
            survives it unchanged, because ``.`` is a legal filename character
            and stripping it would mangle ordinary names. Containment is
            :meth:`~living_ink.destinations.filesystem.FileSystemDestination.contained`,
            which every path here is built through.

            Distinct from ``pipeline.sanitize_filename`` on purpose: this names
            files the user sees in their vault, so spaces are preserved. The
            pipeline's version names temporary artifacts and underscores them.

        Args:
            name: The raw string to sanitize.

        Returns:
            A filesystem-safe string.
        """
        sanitized = self.FORBIDDEN_CHARS_REGEX.sub("-", name)
        # Collapse multiple dashes and strip edge whitespace/dashes
        sanitized = re.sub(r"-+", "-", sanitized).strip(" -")
        return sanitized or "Untitled"

    def _root_dir(self) -> Path:
        """Return the directory inside the vault that notes are written under.

        Returns:
            The vault path, or the configured root folder beneath it.

        Raises:
            DestinationError: ``root_folder`` names a path outside the vault.
        """
        if not self.root_folder:
            return self.vault_path
        parts = [
            self._sanitize_filename(part.strip())
            for part in self.root_folder.replace("\\", "/").split("/")
            if part.strip()
        ]
        return self.contained(self.vault_path, *parts)

    def _attachment_dir(self, note_path: Path) -> Path:
        """Return where a note's images and source document belong.

        Derived from the note's own path rather than from the title it was
        built from, so the answer is the same whether a note is being written
        now or was written under a name it no longer has.

        Args:
            note_path: Full path of the ``.md`` file.

        Returns:
            The directory holding that note's attachments.

        Raises:
            DestinationError: The result would land outside the vault.
        """
        if not self.attachments_folder:
            return note_path.parent

        root_dir = self._root_dir()
        try:
            parts = note_path.parent.relative_to(root_dir).parts
        except ValueError:
            # A note outside the root folder entirely; keep its attachments
            # directly under the attachments root rather than guessing.
            parts = ()
        return self.contained(
            root_dir,
            self._sanitize_filename(self.attachments_folder),
            *parts,
            note_path.stem,
        )

    def _relocate(
        self, existing_target: Optional[str], note_path: Path, doc_id: Optional[str]
    ) -> bool:
        """Move a note that has been renamed or moved on the tablet.

        Without this, renaming a notebook writes a second note under the new
        name and abandons the first, and moving one between folders leaves a
        copy in both. With identity recorded in the frontmatter, both are the
        same operation: the note for this document is already somewhere, and
        it belongs somewhere else now.

        The move is refused unless the old note is provably this document's and
        the new path is free. A refused move costs a duplicate; a wrong move
        costs somebody else's note.

        Args:
            existing_target: Vault-relative path recorded for this document.
            note_path: Where the note belongs now.
            doc_id: The document being published.

        Returns:
            True if a note was moved.
        """
        if not existing_target or not doc_id:
            return False

        old_path = self.contained(self.vault_path, *Path(existing_target).parts)
        if old_path == note_path or not old_path.is_file():
            return False

        front, _ = notemerge.split_frontmatter(notemerge.read_existing(old_path) or "")
        if notemerge.frontmatter_value(front, "living_ink_id") != doc_id:
            # Either not ours or not this document's. Leave it alone; the note
            # at the new path is written as usual.
            return False

        if note_path.exists():
            logger.warning(
                "Not moving %s to %s: a note is already there. The old one is now a duplicate.",
                old_path,
                note_path,
            )
            return False

        note_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.replace(note_path)
        self._relocate_attachments(old_path, note_path)
        logger.info("Moved %s to %s", old_path, note_path)
        return True

    def _relocate_attachments(self, old_path: Path, note_path: Path) -> None:
        """Move a note's attachment folder alongside the note itself.

        Only meaningful when attachments live in a dedicated folder per note;
        when they sit beside the note, there is nothing to move that is not
        somebody else's as well.

        Args:
            old_path: Where the note used to be.
            note_path: Where the note is now.
        """
        if not self.attachments_folder:
            return

        old_attach = self._attachment_dir(old_path)
        new_attach = self._attachment_dir(note_path)
        if old_attach == new_attach or not old_attach.is_dir() or new_attach.exists():
            return

        try:
            new_attach.parent.mkdir(parents=True, exist_ok=True)
            old_attach.replace(new_attach)
        except OSError:
            # The images are re-copied on every publish, so a failed move
            # leaves stale files rather than a broken note.
            logger.warning("Could not move attachments from %s", old_attach, exc_info=True)

    def _claim_name(self, target_dir: Path, safe_name: str, doc_id: Optional[str]) -> str:
        """Return a filename that is either this document's note or a free one.

        Two notebooks can have the same title, and before identity was recorded
        the second one to sync would merge itself into the first one's note.
        A note is this document's if its ``living_ink_id`` matches, or if it
        carries no id at all — the latter being every note written before ids
        existed, which is adopted rather than duplicated.

        Args:
            target_dir: Directory the note goes in.
            safe_name: Sanitized filename, without the extension.
            doc_id: The document being published, or None when it is unknown.

        Returns:
            The name to write under: ``safe_name``, or ``safe_name (2)`` and
            upward when the obvious name belongs to somebody else.
        """
        if safe_name == self.sentinel_name:
            # Reserved for the sync-failed note, which is not a publication.
            # A notebook that happens to share its title gets the next name.
            safe_name = f"{safe_name} (2)"

        if not doc_id:
            # Nothing to compare against, so the path is the identity, exactly
            # as it was before ids were recorded.
            return safe_name

        candidate = safe_name
        # Bounded rather than unbounded: a hundred same-titled notebooks in one
        # folder is a sign something is wrong, not a case worth serving.
        for suffix in range(1, 100):
            existing = notemerge.read_existing(target_dir / f"{candidate}.md")
            if existing is None:
                return candidate
            front, _ = notemerge.split_frontmatter(existing)
            owner = notemerge.frontmatter_value(front, "living_ink_id")
            if owner is None or owner == doc_id:
                return candidate
            candidate = f"{safe_name} ({suffix + 1})"

        logger.warning(
            "Too many notes named '%s' in %s; merging into the last one.", safe_name, target_dir
        )
        return candidate

    def _sanitize_tag(self, tag: str) -> str:
        """Sanitize a tag for Obsidian YAML frontmatter.

        Args:
            tag: Raw tag string.

        Returns:
            Clean tag string suitable for Obsidian.
        """
        clean = tag.strip().lstrip("#").strip()
        clean = re.sub(r"\s+", "-", clean)
        clean = re.sub(r"[^\w\-/]", "", clean)
        return clean

    def _page_blocks(self, page: Page) -> List[Block]:
        """Turn one page into the blocks that introduce and carry it.

        The heading is built from fields the page already carries, so nothing
        here reopens the source PDF to ask what page 77 is called.

        Args:
            page: The page to render.

        Returns:
            A divider, a heading, and whatever the page produced.
        """
        from living_ink.extract import format_page_section_header

        header = format_page_section_header(
            page.number,
            include_divider=True,
            label=page.label,
            breadcrumbs=page.breadcrumbs,
        )
        blocks: List[Block] = list(to_blocks(header))
        # A page that failed says so where its text would have been. Publishing
        # the heading alone would be indistinguishable from a blank page.
        body = page.error if page.error else page.text.strip()
        if body:
            blocks.extend(to_blocks(body))
        return blocks

    # ------------------------------------------------------------------
    # The seven stages
    # ------------------------------------------------------------------

    def resolve_location(self, doc: Document, ctx: PublishContext, layout: NoteLayout) -> None:
        """Stage 1 — mirror the tablet's folder tree, or flatten it.

        Reads only. Every segment goes through
        :meth:`~living_ink.destinations.filesystem.FileSystemDestination.contained`,
        so a tablet folder literally named ``..`` is refused rather than
        climbing out of the vault.
        """
        root_dir = self._root_dir()
        if not self.mirror_folders:
            layout.target_dir = root_dir
            return
        parts = [self._sanitize_filename(part.strip()) for part in doc.folder_path if part.strip()]
        layout.target_dir = self.contained(root_dir, *parts)

    def resolve_name(self, doc: Document, ctx: PublishContext, layout: NoteLayout) -> None:
        """Stage 2 — name the note, stepping around another document's.

        Reads the target directory to see whose note is already at the obvious
        name, and writes nothing.
        """
        assert layout.target_dir is not None, "resolve_location runs first"
        clean_title = doc.title.strip()
        folder_parts = [part.strip() for part in doc.folder_path if part.strip()]

        if self.mirror_folders or not folder_parts:
            # The folder structure provides the context, so the note gets a
            # clean name.
            safe_name = self._sanitize_filename(clean_title)
        else:
            # Flat mode: prepend the folder path so two notebooks with the same
            # title in different folders do not collide.
            safe_name = self._sanitize_filename(f"{' - '.join(folder_parts)} - {clean_title}")

        safe_name = self._claim_name(layout.target_dir, safe_name, ctx.doc_id)
        layout.note_path = self.contained(layout.target_dir, f"{safe_name}{self.note_suffix}")

    def prepare(self, doc: Document, ctx: PublishContext, layout: NoteLayout) -> None:
        """Stage 3 — create the folders, move a renamed note, read what is there.

        The first stage that touches the disk, and the home of both ``mkdir``
        calls: the note's folder and its attachment folder.
        """
        assert layout.target_dir is not None and layout.note_path is not None
        layout.target_dir.mkdir(parents=True, exist_ok=True)

        # A notebook renamed or moved on the tablet keeps its note: bring the
        # old file here rather than writing a second one.
        self._relocate(ctx.existing_target, layout.note_path, ctx.doc_id)

        self._attachment_dir(layout.note_path).mkdir(parents=True, exist_ok=True)
        layout.existing_text = notemerge.read_existing(layout.note_path)

    def write_attachments(self, doc: Document, ctx: PublishContext, layout: NoteLayout) -> None:
        """Stage 4 — copy the page images and the source file into the vault."""
        assert layout.note_path is not None
        attach_dir = self._attachment_dir(layout.note_path)
        rel_attach = self.relative_target(attach_dir)
        link_prefix = f"{rel_attach}/" if rel_attach not in (".", "") else ""
        stem = layout.note_path.stem

        source = doc.source_file
        if source and source.exists():
            filename = f"{stem}{source.suffix.lower()}"
            shutil.copy2(source, self.contained(attach_dir, filename))
            layout.doc_link_target = f"{link_prefix}{filename}"

        for page in doc.pages:
            image = page.image_path
            if not image or not image.exists():
                continue
            # In a dedicated attachments subfolder the page filename is already
            # unique; beside the note it needs the note's name to stay so.
            page_filename = f"page-{page.number}{image.suffix.lower()}"
            filename = page_filename if self.attachments_folder else f"{stem}_{page_filename}"
            shutil.copy2(image, self.contained(attach_dir, filename))
            layout.attachment_links[page.index] = f"{link_prefix}{filename}"

    def render_body(self, doc: Document, ctx: PublishContext, layout: NoteLayout) -> None:
        """Stage 5 — assemble the note's content through the Markdown writer.

        Every block goes through :class:`~living_ink.destinations.markup.ObsidianWriter`
        rather than being concatenated by hand, so an element this destination
        could not represent would have to say so rather than vanish. Obsidian
        represents all eleven natively, so nothing degrades here today — which
        is precisely why the contract needs a second writer to prove it, and
        why ``PlainTextWriter`` exists in the test suite.
        """
        blocks: List[Block] = []
        for page in doc.pages:
            blocks.extend(self._page_blocks(page))

        # Text lifted out of the PDF or EPUB itself, which is worth publishing
        # only when no page produced anything — otherwise it duplicates them.
        if doc.body_text and not any(page.text.strip() for page in doc.pages):
            blocks.extend(to_blocks(doc.body_text.strip()))

        images = [image_block(page) for page in doc.pages if page.index in layout.attachment_links]
        if images:
            blocks.append(Block(kind=BlockKind.DIVIDER))
            blocks.append(Block(kind=BlockKind.HEADING, text="Original Pages", level=2))
            blocks.extend(images)

        write_ctx = WriteContext(
            resolve_attachment=lambda page: layout.attachment_links.get(page.index, ""),
            settings=ctx.settings,
        )
        body, degradations = self._writer.render(blocks, write_ctx)
        layout.warnings.extend(d.describe() for d in degradations)
        layout.body = str(body)

    def render_metadata(self, doc: Document, ctx: PublishContext, layout: NoteLayout) -> None:
        """Stage 6 — build the YAML frontmatter Living Ink owns.

        Runs after the body because two of its fields describe things the
        earlier stages worked out: the source document link, and the ``created``
        date read off the note being replaced.
        """
        today_str = datetime.date.today().isoformat()
        document_modified = _iso_date(doc.modified)
        existing_front, _ = notemerge.split_frontmatter(layout.existing_text or "")

        # Three dates, three meanings. They used to be one field called
        # `created` that was regenerated on every sync, so it silently meant
        # "last synced" — and reported today for a note written in March.
        created = (
            # What the note already says is the best evidence there is.
            notemerge.frontmatter_value(existing_front, "created")
            or _iso_date(ctx.first_published)
            # Better a date the notebook was demonstrably alive on than today,
            # which is certainly wrong.
            or document_modified
            or today_str
        )

        source = doc.source_file
        doc_type = source.suffix.lstrip(".").lower() if source and source.exists() else None
        combined_tags = ["remarkable", doc_type or "handwritten"]
        for tag in doc.tags:
            clean = self._sanitize_tag(tag)
            if clean and clean.lower() not in [t.lower() for t in combined_tags]:
                combined_tags.append(clean)

        folder_parts = [part.strip() for part in doc.folder_path if part.strip()]
        source_path = "/".join([*folder_parts, doc.title.strip()])

        layout.metadata = notemerge.owned_frontmatter_lines(
            {
                # The identity of the note, and the only part of the
                # frontmatter that is not a description of it.
                "living_ink_id": ctx.doc_id,
                # When this note came into existence.
                "created": created,
                # When the user last wrote on the tablet. The one worth sorting
                # by, and the one that used to be thrown away.
                "updated": document_modified or today_str,
                # When Living Ink last wrote this file. Bookkeeping.
                "synced": today_str,
                "source": f"Remarkable/{source_path}",
                "type": doc_type,
                "document": f'"[[{layout.doc_link_target}]]"' if layout.doc_link_target else None,
                "tags": combined_tags,
            }
        )

    def commit(self, doc: Document, ctx: PublishContext, layout: NoteLayout) -> PublishResult:
        """Stage 7 — splice the generated region into the note and write it once."""
        final_md = notemerge.render(layout.metadata, layout.body, layout.existing_text)
        return self.write_note(layout, final_md)

    # ------------------------------------------------------------------

    def unpublish(self, ctx: PublishContext) -> PublishResult:
        """Delete a note whose notebook is gone from the tablet.

        Refused unless the file carries this document's ``living_ink_id``.
        A note without one may be a user's own, and the cost of being wrong
        here is a file nobody can get back.

        Args:
            ctx: The note's coordinates. ``existing_target`` is the
                vault-relative path recorded for it; ``existing_external_id``
                is unused, because a note here is identified by its path.

        Returns:
            The outcome, ``ok`` being whether the note was deleted.

        Raises:
            DestinationError: The vault refused the deletion, or the recorded
                path points outside it.
        """
        target = ctx.existing_target
        doc_id = ctx.doc_id
        if not target or not doc_id:
            return PublishResult(ok=False, detail="No note recorded for this document.")

        if not self.published_exists(ctx):
            # Already gone, which is where this was heading anyway.
            return PublishResult(ok=False, target=target, detail="The note is already gone.")

        note_path = self.contained(self.vault_path, *Path(target).parts)
        front, _ = notemerge.split_frontmatter(notemerge.read_existing(note_path) or "")
        if notemerge.frontmatter_value(front, "living_ink_id") != doc_id:
            logger.info("Not deleting %s: it does not carry this document's id.", note_path)
            return PublishResult(
                ok=False,
                target=target,
                detail=f"'{target}' does not carry this document's id.",
            )

        attach_dir = self._attachment_dir(note_path)
        try:
            note_path.unlink()
            if self.attachments_folder and attach_dir.is_dir():
                shutil.rmtree(attach_dir)
        except OSError as e:
            raise DestinationError(f"Could not delete '{note_path}': {e}") from e

        logger.info("Deleted %s", note_path)
        return PublishResult(ok=True, target=target, detail=f"Deleted '{target}'.")
