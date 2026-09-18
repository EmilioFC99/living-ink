"""Destinations for publishing reMarkable notes.

Abstracts the publication target (Apple Notes, Obsidian, etc.) from the
processing logic. All publication targets inherit from the ``Destination``
base class.

Example:
    >>> from living_ink.destinations import ObsidianDestination
    >>> dest = ObsidianDestination(vault_path="/path/to/vault", root_folder="Living Ink")
    >>> dest.publish("Meeting Notes", "# Content", [], sub_folder="Work/Projects")
"""

import abc
import datetime
import html
import json
import logging
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional, Type

from PIL import Image

from living_ink import notemerge
from living_ink.safeio import write_text_atomic
from living_ink.settings import Settings

logger = logging.getLogger(__name__)


class DestinationError(Exception):
    """Publishing failed for an expected, user-actionable reason.

    Raised for conditions the user can fix (a vault path that no longer
    exists, a full disk, macOS denying automation access). Anything that is
    *not* one of these — an ``AttributeError`` in our own code, say — is
    deliberately left to propagate rather than being reported as an ordinary
    publish failure.
    """


class DestinationUnavailable(DestinationError):
    """The destination could not be reached; retrying later is sensible."""


DESTINATION_REGISTRY: Dict[str, Type["Destination"]] = {}


def register_destination(config_key: str, enabled_by_default: bool = False):
    """Register a Destination subclass under its ``config.yml`` section name.

    Adding a destination is then a matter of writing the class and decorating
    it; :func:`build_destinations` picks it up without anyone editing the
    pipeline.

    Args:
        config_key: The config section that configures this destination.
        enabled_by_default: Whether it runs when ``enabled`` is not stated.

    Returns:
        The class decorator.
    """

    def decorator(cls: Type["Destination"]) -> Type["Destination"]:
        cls.config_key = config_key
        cls.enabled_by_default = enabled_by_default
        DESTINATION_REGISTRY[config_key] = cls
        return cls

    return decorator


class Destination(abc.ABC):
    """Abstract base class for publication destinations.

    Subclasses implement :meth:`publish` to format and write notes, and
    :meth:`from_config` to build themselves from their own section of
    ``config.yml``. Decorating the subclass with :func:`register_destination`
    is what makes it reachable from configuration — no other module needs to
    learn the new name.

    Attributes:
        config_key: The ``config.yml`` section this destination reads, set by
            :func:`register_destination`.
        enabled_by_default: Whether the destination is active when its section
            says nothing about ``enabled``.
    """

    config_key: ClassVar[str] = ""
    enabled_by_default: ClassVar[bool] = False

    #: Identifier the destination assigned to the note it last published, for
    #: destinations that have one. Obsidian leaves it None: a note there is
    #: identified by its path, which the destination can recompute.
    last_external_id: Optional[str] = None

    @classmethod
    def from_config(cls, section: Dict[str, Any], settings: Settings) -> Optional["Destination"]:
        """Build this destination from its config section.

        Args:
            section: The destination's own section of ``config.yml``.
            settings: The run's resolved settings.

        Returns:
            The configured destination, or None if the section is incomplete
            and the destination should be skipped. Implementations explain the
            skip to the user rather than failing the whole run.
        """
        raise NotImplementedError

    def describe(self) -> str:
        """Return a one-line description for the startup summary.

        Returns:
            The destination's name and the setting a user would want confirmed.
        """
        return self.config_key or type(self).__name__

    @abc.abstractmethod
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
    ) -> bool:
        """Publish a notebook to the destination.

        Args:
            notebook_name: Title of the notebook.
            text_content: The cleaned-up text content.
            image_paths: List of file paths to rendered page images.
            sub_folder: Optional relative sub-folder path (e.g., "Work/Projects").
            document_path: Optional path to underlying raw document (PDF or EPUB).
            tags: Optional list of tags associated with the notebook or its pages.
            existing_id: Identifier this destination returned the last time it
                published this notebook, if one was recorded. Replacing exactly
                that object is the only safe way to re-publish.
            adopt_by_name: Permission to fall back to matching on title when no
                ``existing_id`` is known. Only true when sync state says this
                notebook was published here before, which means the note with
                that title was almost certainly created by Living Ink.

        Returns:
            True if publication succeeded. On success, implementations set
            :attr:`last_external_id` when the destination has an identifier
            worth remembering.

        Raises:
            DestinationError: Publication failed for an expected reason. The
                message is user-facing and names the cause.
        """


@register_destination("apple_notes", enabled_by_default=True)
class AppleNotesDestination(Destination):
    """Publishes notes to Apple Notes application via AppleScript.

    Attributes:
        folder_name: The root folder in Apple Notes where notes are stored.
    """

    @classmethod
    def from_config(cls, section: Dict[str, Any], settings: Settings) -> "Destination":
        """Build an Apple Notes destination.

        The folder name comes from the resolved settings rather than the
        section, because a ``--folder`` flag and the ``APPLE_NOTES_FOLDER``
        environment variable both outrank the config file.
        """
        return cls(folder_name=settings.apple_notes_folder)

    def describe(self) -> str:
        """Name this destination and the Apple Notes folder it writes to."""
        return f"Apple Notes (Folder: {self.folder_name})"

    def __init__(self, folder_name: str = "reMarkable") -> None:
        """Initialize AppleNotesDestination.

        Args:
            folder_name: The top-level folder name inside Apple Notes.
                Defaults to "reMarkable".
        """
        self.folder_name = folder_name

    def _removal_script(self, existing_id: Optional[str], adopt_by_name: bool) -> str:
        """Build the AppleScript fragment that removes the previous note.

        Three cases, and the difference between them is the whole point of
        this method:

        * A recorded id — delete exactly that note. Nothing else can match.
        * No id, but sync state says this notebook was published here before —
          the note carrying this title in Living Ink's own folder was created
          by Living Ink, so matching on title is safe enough to avoid leaving
          a duplicate behind for every notebook synced before ids were kept.
        * Neither — delete nothing. A duplicate note is an annoyance the user
          can fix; a deleted note is not recoverable.

        Args:
            existing_id: Apple Notes id recorded for this notebook, if any.
            adopt_by_name: Whether title matching is permitted as a fallback.

        Returns:
            AppleScript lines, indented to sit inside the ``tell`` block.
        """
        if existing_id:
            safe_id = json.dumps(existing_id, ensure_ascii=False)
            return f"""    -- Replace exactly the note this sync created last time
    try
        delete note id {safe_id}
    on error
        try
            delete (every note of targetFolder whose id is {safe_id})
        end try
    end try
"""
        if adopt_by_name:
            return """    -- Synced before ids were recorded: the note with this title in our
    -- own folder is one we created, so replacing it will not lose anything.
    try
        delete (every note in targetFolder whose name is noteName)
    end try
"""
        return "    -- No recorded id: create rather than guess which note to delete.\n"

    def _convert_to_html(self, text: str) -> str:
        """Convert plain text to the HTML format expected by Apple Notes.

        Args:
            text: Plain text content to format.

        Returns:
            HTML string suitable for Apple Notes note body.
        """
        text = text.lstrip()
        html_lines = []
        in_callout = False
        callout_lines = []

        def flush_callout():
            nonlocal in_callout, callout_lines
            if in_callout:
                if callout_lines:
                    joined = "<br>".join(callout_lines)
                    html_lines.append(f"<blockquote>{joined}</blockquote>")
                    callout_lines = []
                in_callout = False

        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                flush_callout()
                html_lines.append("<div><br></div>")
            elif stripped == "---":
                flush_callout()
                html_lines.append("<hr>")
            elif stripped.startswith("### "):
                flush_callout()
                header_text = html.escape(stripped[4:].strip())
                html_lines.append(f"<h3>{header_text}</h3>")
            elif stripped.startswith("## "):
                flush_callout()
                header_text = html.escape(stripped[3:].strip())
                html_lines.append(f"<h2>{header_text}</h2>")
            elif (
                (stripped.startswith("<span") and stripped.endswith("</span>"))
                or (stripped.startswith("<small") and stripped.endswith("</small>"))
                or (stripped.startswith("<div") and stripped.endswith("</div>"))
            ):
                flush_callout()
                html_lines.append(f"<div>{stripped}</div>")
            elif stripped.startswith("> [!"):
                flush_callout()
                m = re.match(r"^>\s*\[!\w+\]\s*(.*)$", stripped)
                title = m.group(1).strip() if m and m.group(1).strip() else "Note"
                html_lines.append(f"<div><b>{html.escape(title)}</b></div>")
                in_callout = True
            elif in_callout and (stripped.startswith(">") or stripped == ">"):
                content = stripped[1:].strip()
                if content:
                    callout_lines.append(html.escape(content))
                else:
                    callout_lines.append("<br>")
            else:
                flush_callout()
                html_lines.append(f"<div>{html.escape(line)}</div>")

        flush_callout()
        return "".join(html_lines)

    def _create_opaque_image(self, img_path: Path) -> Path:
        """Create a version of the image with a white background.

        Apple Notes handles transparent alpha channels poorly, so images
        are composited against solid white before importing.

        Args:
            img_path: Path to the original PNG image.

        Returns:
            Path to the opaque PNG file, or the original path on error.
        """
        opaque_path = img_path.parent / f"opaque_{img_path.name}"
        if opaque_path.exists():
            return opaque_path

        try:
            pil_img = Image.open(img_path)
            bg = Image.new("RGB", pil_img.size, (255, 255, 255))
            if pil_img.mode in ("RGBA", "LA") or (
                pil_img.mode == "P" and "transparency" in pil_img.info
            ):
                pil_img = pil_img.convert("RGBA")
                bg.paste(pil_img, mask=pil_img.split()[3])
            else:
                bg.paste(pil_img)
            bg.save(opaque_path, "PNG")
            return opaque_path
        except (OSError, ValueError) as e:
            # Pillow reports an unreadable file, an unsupported mode and a
            # failed write all as one of these. Any of them means the original
            # is still the best thing to hand over.
            logger.warning(
                "Failed to create opaque PNG for %s: %s. Using original.",
                img_path.name,
                e,
                exc_info=True,
            )
            return img_path

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
    ) -> bool:
        """Publish a note to Apple Notes via osascript.

        The previous note is removed by identifier, never by title. Deleting
        every note whose *name* matched destroyed unrelated notes a user had
        written themselves, and there was no way to get them back.

        Args:
            notebook_name: Title of the note.
            text_content: Cleaned note text.
            image_paths: Paths to page images.
            sub_folder: Sub-folder name. Apple Notes supports one level of
                nesting beneath ``folder_name``; if a nested path is provided,
                the top-level segment is used.
            document_path: Optional path to underlying raw document (PDF or EPUB).
            tags: Optional list of tags associated with the notebook or its pages.
            existing_id: Apple Notes id of the note published last time.
            adopt_by_name: Permission to match on title when no id is recorded,
                which is the case for notebooks synced before ids were stored.

        Returns:
            True if AppleScript executed successfully.

        Raises:
            DestinationUnavailable: osascript is missing, timed out, or Notes
                rejected the script on every attempt.
        """
        retries = 3
        self.last_external_id = None

        # Apple Notes supports 1 level of sub-folder under rootFolder.
        # If a nested path like "Work/Projects/Q1" is passed, use the top-level segment.
        effective_sub_folder = None
        if sub_folder:
            top_part = sub_folder.replace("\\", "/").split("/")[0].strip()
            if top_part:
                effective_sub_folder = top_part

        # 1. Prepare Content
        doc_header = ""
        if document_path and document_path.exists():
            doc_header = f"<div><b>Source Document:</b> {html.escape(document_path.name)}</div><div><br></div>"
        text_html = self._convert_to_html(text_content)
        tag_footer = ""
        if tags:
            tag_badges = " ".join(f"#{t.lstrip('#').replace(' ', '-')}" for t in tags if t)
            if tag_badges:
                tag_footer = f'<div><br></div><div><span style="color: #666;">{html.escape(tag_badges)}</span></div>'
        final_body = "<div><br></div>" + doc_header + text_html + tag_footer

        # 2. Prepare Attachments
        attachment_cmds = ""
        if document_path and document_path.exists():
            safe_doc = json.dumps(str(document_path.resolve()), ensure_ascii=False)
            attachment_cmds += (
                f"make new attachment at end of attachments of newNote with "
                f"data (POSIX file {safe_doc})\n    "
            )
        for img_p in image_paths:
            if img_p.exists():
                final_path = self._create_opaque_image(img_p)
                safe_path = json.dumps(str(final_path.resolve()), ensure_ascii=False)
                attachment_cmds += (
                    f"make new attachment at end of attachments of newNote with "
                    f"data (POSIX file {safe_path})\n    "
                )

        # 3. Execute AppleScript
        try:
            for attempt in range(1, retries + 1):
                safe_folder = json.dumps(self.folder_name, ensure_ascii=False)
                safe_name = json.dumps(notebook_name, ensure_ascii=False)
                safe_body = json.dumps(final_body, ensure_ascii=False)

                if effective_sub_folder:
                    safe_sub_folder = json.dumps(effective_sub_folder, ensure_ascii=False)
                    has_sub = "true"
                else:
                    safe_sub_folder = '""'
                    has_sub = "false"

                applescript = f"""
tell application "Notes"
    set rootFolderName to {safe_folder}

    -- Check if root folder exists, if not create it
    if not (exists folder rootFolderName) then
        make new folder with properties {{name:rootFolderName}}
    end if
    set rootFolder to folder rootFolderName

    -- Determine target folder (Root or Sub)
    set targetFolder to rootFolder

    if {has_sub} then
        set subFolderName to {safe_sub_folder}
        -- Check if subFolder exists INSIDE rootFolder
        if not (exists folder subFolderName of rootFolder) then
            make new folder at rootFolder with properties {{name:subFolderName}}
        end if
        set targetFolder to folder subFolderName of rootFolder
    end if

    set noteName to {safe_name}
{self._removal_script(existing_id, adopt_by_name)}
    -- Create the new note with HTML body in the specific folder
    set newNote to make new note at targetFolder with properties {{name:noteName, body:{safe_body}}}

    -- Attach images
    {attachment_cmds}
    -- Reported back so the next sync replaces exactly this note and no other
    return id of newNote as string
end tell
"""
                result = subprocess.run(
                    ["osascript", "-e", applescript],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )

                if result.returncode != 0:
                    logger.warning(
                        "AppleScript error (attempt %d/%d, code %d): %s",
                        attempt,
                        retries,
                        result.returncode,
                        result.stderr,
                    )
                    if attempt < retries:
                        time.sleep(2)
                        continue
                    raise DestinationUnavailable(
                        f"Apple Notes rejected the script after {retries} attempts "
                        f"(exit {result.returncode}): {result.stderr.strip()}"
                    )

                self.last_external_id = result.stdout.strip() or None
                logger.info("Apple Note created for %s", notebook_name)
                return True

        except FileNotFoundError as e:
            raise DestinationUnavailable(
                "osascript not found — Apple Notes publishing requires macOS."
            ) from e
        except subprocess.TimeoutExpired as e:
            raise DestinationUnavailable(
                f"Apple Notes did not respond within {e.timeout}s. Is the Notes app "
                "busy or awaiting a permission prompt?"
            ) from e
        except OSError as e:
            raise DestinationUnavailable(f"Could not run osascript: {e}") from e

        raise DestinationUnavailable("Apple Notes publishing exhausted all retries.")


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

    # Characters forbidden in filenames across macOS, Windows, Linux, and Obsidian
    FORBIDDEN_CHARS_REGEX = re.compile(r'[/\\:*?"<>|#^\[\]]')

    @classmethod
    def from_config(cls, section: Dict[str, Any], settings: Settings) -> Optional["Destination"]:
        """Build an Obsidian destination, or skip it if no vault is configured.

        Returns:
            The destination, or None when ``vault_path`` is missing — a vault
            is the one thing this destination cannot guess.
        """
        vault_path = section.get("vault_path")
        if not vault_path:
            print("⚠️ Obsidian enabled but 'vault_path' is missing. Skipping.")
            return None
        return cls(
            vault_path=vault_path,
            attachments_folder=section.get("attachments_folder", "_attachments"),
            root_folder=section.get("root_folder"),
            mirror_folders=section.get("mirror_folders", True),
        )

    def describe(self) -> str:
        """Name this destination, its vault, and the root folder if one is set."""
        root = f" (Root: {self.root_folder})" if self.root_folder else ""
        return f"Obsidian (Vault: {self.vault_path}{root})"

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

        Raises:
            ValueError: If ``vault_path`` does not exist on disk.
        """
        self.vault_path = Path(vault_path).expanduser().resolve()
        if not self.vault_path.exists():
            raise ValueError(f"Obsidian Vault path does not exist: {self.vault_path}")
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
    ) -> bool:
        """Publish a note to Obsidian as Markdown with image attachments.

        Both identity arguments are accepted and ignored: a note here is
        identified by its path, which this destination recomputes from the
        notebook name, and an existing file is merged rather than replaced.

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

        Returns:
            True if the Markdown file and attachments were written successfully.

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
            root_dir = self.vault_path
            if self.root_folder:
                # Sanitize each segment of the root_folder path if nested
                for part in self.root_folder.replace("\\", "/").split("/"):
                    if part.strip():
                        root_dir = root_dir / self._sanitize_filename(part.strip())

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

            # 4. Handle Attachments (centralized _attachments root, mirroring subfolders + dedicated note folder)
            if self.attachments_folder:
                attach_dir = root_dir / self._sanitize_filename(self.attachments_folder)
                for part in subfolder_parts:
                    attach_dir = attach_dir / part
                attach_dir = attach_dir / safe_name
            else:
                attach_dir = target_dir

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
            note_path = target_dir / f"{safe_name}.md"
            existing = notemerge.read_existing(note_path)
            existing_front, _ = notemerge.split_frontmatter(existing or "")

            # --- YAML Frontmatter ---
            today_str = datetime.date.today().isoformat()
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
                    # Kept from the note that is already there. Regenerating it
                    # from today's date is what made `created` silently mean
                    # "last synced" on every note that had ever been re-synced.
                    "created": notemerge.frontmatter_value(existing_front, "created") or today_str,
                    "updated": today_str,
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

            logger.info("Obsidian note written at: %s", note_path)
            return True

        except (OSError, shutil.Error) as e:
            raise DestinationError(
                f"Could not write '{notebook_name}' into the vault at {self.vault_path}: {e}"
            ) from e


def _apply_legacy_destination(config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Translate the old single ``destination`` key into per-destination sections.

    Early configs named one destination at the top level, either as a string
    (``destination: obsidian``) or as a dict carrying that destination's own
    settings. Both forms mean "this one and no other", so they are normalized
    into the same per-section shape the registry reads.

    Args:
        config: Parsed ``config.yml`` contents.

    Returns:
        One section per registered destination, legacy overrides applied.
    """
    sections = {key: dict(config.get(key) or {}) for key in DESTINATION_REGISTRY}

    legacy = config.get("destination")
    if legacy is None:
        return sections

    if isinstance(legacy, str):
        # The string form only rules the others out; the named destination is
        # still configured by, and enabled by, its own section.
        named, extras, selects = legacy.strip(), {}, False
    elif isinstance(legacy, dict):
        named = str(legacy.get("type", "")).strip()
        extras = {k: v for k, v in legacy.items() if k != "type"}
        selects = True
    else:
        return sections

    for key, section in sections.items():
        if key != named:
            section["enabled"] = False
        elif selects:
            # A legacy dict both selects the destination and configures it.
            section.update(extras)
            section["enabled"] = True

    return sections


def build_destinations(config: Dict[str, Any], settings: Settings) -> List[Destination]:
    """Build every destination the configuration enables.

    Walks :data:`DESTINATION_REGISTRY` rather than naming destinations one by
    one, so a newly registered subclass is picked up here for free.

    Args:
        config: Parsed ``config.yml`` contents.
        settings: The run's resolved settings.

    Returns:
        The enabled destinations, in registration order. A destination whose
        section is incomplete is skipped with a warning rather than aborting
        the run.
    """
    sections = _apply_legacy_destination(config)
    built: List[Destination] = []

    for key, cls in DESTINATION_REGISTRY.items():
        section = sections.get(key, {})
        if not section.get("enabled", cls.enabled_by_default):
            continue

        try:
            destination = cls.from_config(section, settings)
        except Exception as e:
            # Broad by contract: one misconfigured destination skips itself
            # rather than taking the other destinations down with it.
            print(f"⚠️ Could not set up destination '{key}': {e}")
            logger.warning("Destination '%s' failed to build: %s", key, e, exc_info=True)
            continue

        if destination is not None:
            built.append(destination)
            print(f"Destination added: {destination.describe()}")

    return built
