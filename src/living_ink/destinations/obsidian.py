"""Publishing to a local Obsidian vault as Markdown files.

Notes are plain files, so this destination can mirror the tablet's folder tree,
move a note whose notebook was renamed, and splice its own content into a note
the user has also written in.
"""

import datetime
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional

from living_ink import notemerge
from living_ink.core.document import PublishResult
from living_ink.destinations.base import (
    Destination,
    DestinationError,
    DestinationStatus,
    MergeUnit,
    register_destination,
)
from living_ink.safeio import write_text_atomic
from living_ink.settings import Settings

logger = logging.getLogger(__name__)


@register_destination("obsidian")
class ObsidianDestination(Destination):
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

    def _sanitize_filename(self, name: str) -> str:
        """Sanitize a filename or folder segment for filesystem compatibility.

        Replaces forbidden characters (/ \\ : * ? " < > | # ^ [ ]) with a hyphen,
        preserving regular spaces and alphanumeric characters.

        Note:
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
        """
        root_dir = self.vault_path
        if self.root_folder:
            # Sanitize each segment of the root_folder path if nested
            for part in self.root_folder.replace("\\", "/").split("/"):
                if part.strip():
                    root_dir = root_dir / self._sanitize_filename(part.strip())
        return root_dir

    def _attachment_dir(self, note_path: Path) -> Path:
        """Return where a note's images and source document belong.

        Derived from the note's own path rather than from the title it was
        built from, so the answer is the same whether a note is being written
        now or was written under a name it no longer has.

        Args:
            note_path: Full path of the ``.md`` file.

        Returns:
            The directory holding that note's attachments.
        """
        if not self.attachments_folder:
            return note_path.parent

        root_dir = self._root_dir()
        attach_dir = root_dir / self._sanitize_filename(self.attachments_folder)
        try:
            parts = note_path.parent.relative_to(root_dir).parts
        except ValueError:
            # A note outside the root folder entirely; keep its attachments
            # directly under the attachments root rather than guessing.
            parts = ()
        for part in parts:
            attach_dir = attach_dir / part
        return attach_dir / note_path.stem

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

        old_path = self.vault_path / existing_target
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

    def unpublish(
        self,
        target: Optional[str] = None,
        external_id: Optional[str] = None,
        doc_id: Optional[str] = None,
    ) -> PublishResult:
        """Delete a note whose notebook is gone from the tablet.

        Refused unless the file carries this document's ``living_ink_id``.
        A note without one may be a user's own, and the cost of being wrong
        here is a file nobody can get back.

        Args:
            target: Vault-relative path recorded for the note.
            external_id: Unused; a note here is identified by its path.
            doc_id: The document the note was published for.

        Returns:
            The outcome, ``ok`` being whether the note was deleted.

        Raises:
            DestinationError: The vault refused the deletion.
        """
        if not target or not doc_id:
            return PublishResult(ok=False, detail="No note recorded for this document.")

        note_path = self.vault_path / target
        if not note_path.is_file():
            # Already gone, which is where this was heading anyway.
            return PublishResult(ok=False, target=target, detail="The note is already gone.")

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

    def publish(
        self,
        notebook_name: str,
        text_content: str,
        image_paths: List[Path],
        sub_folder: Optional[str] = None,
        document_path: Optional[Path] = None,
        tags: Optional[List[str]] = None,
        existing_id: Optional[str] = None,
        adopt_by_name: bool = False,
        doc_id: Optional[str] = None,
        existing_target: Optional[str] = None,
        document_modified: Optional[str] = None,
        first_published: Optional[str] = None,
    ) -> PublishResult:
        """Publish a note to Obsidian as Markdown with image attachments.

        A note here is found by its path, which this destination recomputes
        from the notebook name, and merged rather than replaced. But a path is
        not an identity: two notebooks can carry the same title, and the second
        one must not be merged into the first one's note. So the document id is
        written into the frontmatter as ``living_ink_id`` and checked before
        anything is written — a note belonging to a different document is
        stepped around rather than overwritten.

        Args:
            notebook_name: Title of the notebook. Can be a base name (e.g. "Note")
                or a display title with breadcrumbs ("Work / Projects / Note").
            text_content: Cleaned text content of the notebook.
            image_paths: List of paths to rendered page images.
            sub_folder: Relative folder path mirroring the reMarkable hierarchy
                (e.g., "Work/Projects/Q1").
            document_path: Optional path to underlying raw document (PDF or EPUB).
            tags: Optional list of tags associated with the notebook or its pages.
            existing_id: Unused; accepted to satisfy the Destination contract.
            adopt_by_name: Unused; accepted to satisfy the Destination contract.
            doc_id: The reMarkable document id, stamped into the frontmatter as
                the note's identity. When absent, the path is the only identity
                available and a same-titled note is merged as before.
            existing_target: Vault-relative path this note was last written to.
                When the notebook has since been renamed or moved, the note is
                moved to match instead of being left behind as a duplicate.
            document_modified: ``YYYY-MM-DD`` the notebook was last written on,
                published as ``updated``.
            first_published: ``YYYY-MM-DD`` this note first reached the vault,
                used for ``created`` when the note itself does not say.

        Returns:
            The outcome, carrying the vault-relative path the note was written
            to. No ``external_id``: a note here is identified by the
            ``living_ink_id`` in its frontmatter, which the destination can
            recompute.

        Raises:
            DestinationError: The vault is unreachable or unwritable (missing
                path, permission denied, disk full).
        """
        try:
            # 1. Parse Note Name and Source Path
            if " / " in notebook_name:
                source_path = notebook_name.replace(" / ", "/")
                clean_title = notebook_name.split(" / ")[-1].strip()
            else:
                clean_title = notebook_name.strip()
                source_path = f"{sub_folder}/{clean_title}" if sub_folder else clean_title

            # 2. Determine Target Directory (Notes) and Root Directory
            root_dir = self._root_dir()

            subfolder_parts = []
            if self.mirror_folders and sub_folder:
                # Replicate full folder hierarchy
                for part in sub_folder.replace("\\", "/").split("/"):
                    if part.strip():
                        subfolder_parts.append(self._sanitize_filename(part.strip()))

            target_dir = root_dir
            for part in subfolder_parts:
                target_dir = target_dir / part

            target_dir.mkdir(parents=True, exist_ok=True)

            # 3. Determine File Name
            if self.mirror_folders:
                # Folder structure provides context, note gets clean name
                safe_name = self._sanitize_filename(clean_title)
            else:
                # Flat mode: prepend folder path to avoid collisions between identically named notes
                if sub_folder:
                    flat_prefix = sub_folder.replace("\\", "/").replace("/", " - ")
                    safe_name = self._sanitize_filename(f"{flat_prefix} - {clean_title}")
                else:
                    safe_name = self._sanitize_filename(clean_title)

            # A note already at this path that belongs to a different document
            # is someone else's: take the next free name rather than merging
            # two notebooks into one file.
            safe_name = self._claim_name(target_dir, safe_name, doc_id)

            # A notebook renamed or moved on the tablet keeps its note: bring
            # the old file here rather than writing a second one.
            self._relocate(existing_target, target_dir / f"{safe_name}.md", doc_id)

            # 4. Handle Attachments (centralized _attachments root, mirroring subfolders + dedicated note folder)
            note_path = target_dir / f"{safe_name}.md"
            attach_dir = self._attachment_dir(note_path)
            attach_dir.mkdir(parents=True, exist_ok=True)

            # Path relative to vault root for clean, reliable WikiLinks
            rel_attach_path = attach_dir.relative_to(self.vault_path).as_posix()
            link_prefix = f"{rel_attach_path}/" if rel_attach_path != "." else ""

            doc_filename = None
            doc_link_target = None
            if document_path and document_path.exists():
                ext = document_path.suffix.lower()
                doc_filename = f"{safe_name}{ext}"
                dest_doc_path = attach_dir / doc_filename
                shutil.copy2(document_path, dest_doc_path)
                doc_link_target = f"{link_prefix}{doc_filename}"

            image_links = []
            for img_p in image_paths:
                if img_p.exists():
                    page_match = re.search(r"page-(\d+)", img_p.name, re.IGNORECASE)
                    if page_match:
                        p_num = int(page_match.group(1))
                        from living_ink.extract import format_page_label

                        label = format_page_label(p_num, document_path)
                        page_filename = f"page-{p_num}{img_p.suffix.lower()}"
                    else:
                        label = img_p.stem.replace("_", " ").title()
                        page_filename = img_p.name

                    # In a dedicated attachments subfolder, use clean page filename;
                    # if alongside note, prefix with safe_name to prevent collisions.
                    if self.attachments_folder:
                        new_filename = page_filename
                    else:
                        new_filename = f"{safe_name}_{page_filename}"

                    dest_path = attach_dir / new_filename
                    shutil.copy2(img_p, dest_path)

                    img_link_target = f"{link_prefix}{new_filename}"
                    image_links.append(f"- [[{img_link_target}|{label}]]")

            # 5. Build Markdown Content
            existing = notemerge.read_existing(note_path)
            existing_front, _ = notemerge.split_frontmatter(existing or "")

            # --- YAML Frontmatter ---
            today_str = datetime.date.today().isoformat()
            # Three dates, three meanings. They used to be one field called
            # `created` that was regenerated on every sync, so it silently
            # meant "last synced" — and reported today for a note written in
            # March.
            created = (
                # What the note already says is the best evidence there is.
                notemerge.frontmatter_value(existing_front, "created")
                or first_published
                # Better a date the notebook was demonstrably alive on than
                # today, which is certainly wrong.
                or document_modified
                or today_str
            )
            doc_type = None
            combined_tags = ["remarkable"]
            if document_path and document_path.exists():
                doc_type = document_path.suffix.lstrip(".").lower()
                combined_tags.append(doc_type)
            else:
                combined_tags.append("handwritten")

            if tags:
                for t in tags:
                    clean_t = self._sanitize_tag(t)
                    if clean_t and clean_t.lower() not in [ct.lower() for ct in combined_tags]:
                        combined_tags.append(clean_t)

            owned = notemerge.owned_frontmatter_lines(
                {
                    # The identity of the note, and the only part of the
                    # frontmatter that is not a description of it.
                    "living_ink_id": doc_id,
                    # When this note came into existence.
                    "created": created,
                    # When the user last wrote on the tablet. The one worth
                    # sorting by, and the one that used to be thrown away.
                    "updated": document_modified or today_str,
                    # When Living Ink last wrote this file. Bookkeeping.
                    "synced": today_str,
                    "source": f"Remarkable/{source_path}",
                    "type": doc_type,
                    "document": f'"[[{doc_link_target}]]"' if doc_link_target else None,
                    "tags": combined_tags,
                }
            )

            # --- Generated body ---
            md_lines = []
            if text_content.strip():
                md_lines.append(text_content.strip())
                md_lines.append("")

            if image_links:
                md_lines.append("---")
                md_lines.append("")
                md_lines.append("## Original Pages")
                for link in image_links:
                    md_lines.append(link)
                md_lines.append("")

            # 6. Write Note File
            final_md = notemerge.render(owned, "\n".join(md_lines), existing)
            # Written in one step: a note half-replaced by an interrupted sync
            # is indistinguishable from a transcription that came back
            # truncated, so the user would have no reason to suspect a crash.
            write_text_atomic(note_path, final_md)

            target = note_path.relative_to(self.vault_path).as_posix()
            logger.info("Obsidian note written at: %s", note_path)
            return PublishResult(ok=True, target=target, detail=target)

        except (OSError, shutil.Error) as e:
            raise DestinationError(
                f"Could not write '{notebook_name}' into the vault at {self.vault_path}: {e}"
            ) from e
