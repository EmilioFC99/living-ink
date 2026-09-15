"""Destinations for publishing reMarkable notes.

Abstracts the publication target (Apple Notes, Obsidian, etc.) from the
processing logic. All publication targets inherit from the ``Destination``
base class.

Example:
    >>> from remarkable_mcp.destinations import ObsidianDestination
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
from typing import List, Optional

from PIL import Image

logger = logging.getLogger(__name__)


class Destination(abc.ABC):
    """Abstract base class for publication destinations.

    Subclasses must implement the ``publish`` method to handle formatting
    and writing notes to their respective storage systems.
    """

    @abc.abstractmethod
    def publish(
        self,
        notebook_name: str,
        text_content: str,
        image_paths: List[Path],
        sub_folder: Optional[str] = None,
    ) -> bool:
        """Publish a notebook to the destination.

        Args:
            notebook_name: Title of the notebook.
            text_content: The cleaned-up text content.
            image_paths: List of file paths to rendered page images.
            sub_folder: Optional relative sub-folder path (e.g., "Work/Projects").

        Returns:
            True if publication succeeded, False otherwise.
        """


class AppleNotesDestination(Destination):
    """Publishes notes to Apple Notes application via AppleScript.

    Attributes:
        folder_name: The root folder in Apple Notes where notes are stored.
    """

    def __init__(self, folder_name: str = "reMarkable") -> None:
        """Initialize AppleNotesDestination.

        Args:
            folder_name: The top-level folder name inside Apple Notes.
                Defaults to "reMarkable".
        """
        self.folder_name = folder_name

    def _convert_to_html(self, text: str) -> str:
        """Convert plain text to the HTML format expected by Apple Notes.

        Args:
            text: Plain text content to format.

        Returns:
            HTML string suitable for Apple Notes note body.
        """
        text = text.lstrip()
        html_lines = []
        for line in text.splitlines():
            if not line:
                html_lines.append("<div><br></div>")
            else:
                html_lines.append(f"<div>{html.escape(line)}</div>")
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
        except Exception as e:
            logger.warning(
                "Failed to create opaque PNG for %s: %s. Using original.",
                img_path.name,
                e,
            )
            return img_path

    def publish(
        self,
        notebook_name: str,
        text_content: str,
        image_paths: List[Path],
        sub_folder: Optional[str] = None,
    ) -> bool:
        """Publish a note to Apple Notes via osascript.

        Args:
            notebook_name: Title of the note.
            text_content: Cleaned note text.
            image_paths: Paths to page images.
            sub_folder: Sub-folder name. Apple Notes supports one level of
                nesting beneath ``folder_name``; if a nested path is provided,
                the top-level segment is used.

        Returns:
            True if AppleScript executed successfully, False otherwise.
        """
        retries = 3

        # Apple Notes supports 1 level of sub-folder under rootFolder.
        # If a nested path like "Work/Projects/Q1" is passed, use the top-level segment.
        effective_sub_folder = None
        if sub_folder:
            top_part = sub_folder.replace("\\", "/").split("/")[0].strip()
            if top_part:
                effective_sub_folder = top_part

        # 1. Prepare Content
        text_html = self._convert_to_html(text_content)
        final_body = "<div><br></div>" + text_html

        # 2. Prepare Attachments
        attachment_cmds = ""
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

    -- Check if note exists in that folder and delete it to avoid duplication
    set noteName to {safe_name}
    try
        delete (every note in targetFolder whose name is noteName)
    end try

    -- Create the new note with HTML body in the specific folder
    set newNote to make new note at targetFolder with properties {{name:noteName, body:{safe_body}}}

    -- Attach images
    {attachment_cmds}
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
                    return False

                logger.info("Apple Note created for %s", notebook_name)
                return True

        except Exception as e:
            logger.error("Failed creating Apple Note: %s", e)
            return False

        return False


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

    def __init__(
        self,
        vault_path: str,
        attachments_folder: str = "attachments",
        root_folder: Optional[str] = None,
        mirror_folders: bool = True,
    ) -> None:
        """Initialize ObsidianDestination.

        Args:
            vault_path: Absolute or home-relative path to the Obsidian Vault.
            attachments_folder: Subfolder name for page attachments.
                Set to empty string ("") to store attachments in the same
                directory as the note. Defaults to "attachments".
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

        Args:
            name: The raw string to sanitize.

        Returns:
            A filesystem-safe string.
        """
        sanitized = self.FORBIDDEN_CHARS_REGEX.sub("-", name)
        # Collapse multiple dashes and strip edge whitespace/dashes
        sanitized = re.sub(r"-+", "-", sanitized).strip(" -")
        return sanitized or "Untitled"

    def publish(
        self,
        notebook_name: str,
        text_content: str,
        image_paths: List[Path],
        sub_folder: Optional[str] = None,
    ) -> bool:
        """Publish a note to Obsidian as Markdown with image attachments.

        Args:
            notebook_name: Title of the notebook. Can be a base name (e.g. "Note")
                or a display title with breadcrumbs ("Work / Projects / Note").
            text_content: Cleaned text content of the notebook.
            image_paths: List of paths to rendered page images.
            sub_folder: Relative folder path mirroring the reMarkable hierarchy
                (e.g., "Work/Projects/Q1").

        Returns:
            True if the Markdown file and attachments were written successfully,
            False otherwise.
        """
        try:
            # 1. Parse Note Name and Source Path
            if " / " in notebook_name:
                source_path = notebook_name.replace(" / ", "/")
                clean_title = notebook_name.split(" / ")[-1].strip()
            else:
                clean_title = notebook_name.strip()
                source_path = f"{sub_folder}/{clean_title}" if sub_folder else clean_title

            # 2. Determine Target Directory
            target_dir = self.vault_path
            if self.root_folder:
                # Sanitize each segment of the root_folder path if nested
                for part in self.root_folder.replace("\\", "/").split("/"):
                    if part.strip():
                        target_dir = target_dir / self._sanitize_filename(part.strip())

            if self.mirror_folders and sub_folder:
                # Replicate full folder hierarchy
                for part in sub_folder.replace("\\", "/").split("/"):
                    if part.strip():
                        target_dir = target_dir / self._sanitize_filename(part.strip())

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

            # 4. Handle Attachments
            if self.attachments_folder:
                attach_dir = target_dir / self._sanitize_filename(self.attachments_folder)
            else:
                attach_dir = target_dir

            attach_dir.mkdir(parents=True, exist_ok=True)

            image_refs = []
            for img_p in image_paths:
                if img_p.exists():
                    new_filename = f"{safe_name}_{img_p.name}"
                    dest_path = attach_dir / new_filename
                    shutil.copy2(img_p, dest_path)
                    image_refs.append(f"![[{new_filename}]]")

            # 5. Build Markdown Content
            md_lines = []

            # --- YAML Frontmatter ---
            today_str = datetime.date.today().isoformat()
            md_lines.append("---")
            md_lines.append(f"created: {today_str}")
            md_lines.append(f"source: Remarkable/{source_path}")
            md_lines.append("tags:")
            md_lines.append("  - remarkable")
            md_lines.append("  - handwritten")
            md_lines.append("---")
            md_lines.append("")

            # --- Text Content ---
            md_lines.append(text_content)
            md_lines.append("")

            # --- Attachments ---
            if image_refs:
                md_lines.append("## Original Pages")
                for ref in image_refs:
                    md_lines.append(ref)
                    md_lines.append("")

            final_md = "\n".join(md_lines)

            # 6. Write Note File
            note_path = target_dir / f"{safe_name}.md"
            with open(note_path, "w", encoding="utf-8") as f:
                f.write(final_md)

            logger.info("Obsidian note created at: %s", note_path)
            return True

        except Exception as e:
            logger.error("Failed creating Obsidian note for %s: %s", notebook_name, e)
            return False
