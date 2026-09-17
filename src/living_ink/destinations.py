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
        document_path: Optional[Path] = None,
        tags: Optional[List[str]] = None,
    ) -> bool:
        """Publish a notebook to the destination.

        Args:
            notebook_name: Title of the notebook.
            text_content: The cleaned-up text content.
            image_paths: List of file paths to rendered page images.
            sub_folder: Optional relative sub-folder path (e.g., "Work/Projects").
            document_path: Optional path to underlying raw document (PDF or EPUB).
            tags: Optional list of tags associated with the notebook or its pages.

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
        document_path: Optional[Path] = None,
        tags: Optional[List[str]] = None,
    ) -> bool:
        """Publish a note to Apple Notes via osascript.

        Args:
            notebook_name: Title of the note.
            text_content: Cleaned note text.
            image_paths: Paths to page images.
            sub_folder: Sub-folder name. Apple Notes supports one level of
                nesting beneath ``folder_name``; if a nested path is provided,
                the top-level segment is used.
            document_path: Optional path to underlying raw document (PDF or EPUB).
            tags: Optional list of tags associated with the notebook or its pages.

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
    ) -> bool:
        """Publish a note to Obsidian as Markdown with image attachments.

        Args:
            notebook_name: Title of the notebook. Can be a base name (e.g. "Note")
                or a display title with breadcrumbs ("Work / Projects / Note").
            text_content: Cleaned text content of the notebook.
            image_paths: List of paths to rendered page images.
            sub_folder: Relative folder path mirroring the reMarkable hierarchy
                (e.g., "Work/Projects/Q1").
            document_path: Optional path to underlying raw document (PDF or EPUB).
            tags: Optional list of tags associated with the notebook or its pages.

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
            md_lines = []

            # --- YAML Frontmatter ---
            today_str = datetime.date.today().isoformat()
            md_lines.append("---")
            md_lines.append(f"created: {today_str}")
            md_lines.append(f"source: Remarkable/{source_path}")

            combined_tags = ["remarkable"]
            if document_path and document_path.exists():
                doc_type = document_path.suffix.lstrip(".").lower()
                md_lines.append(f"type: {doc_type}")
                if doc_link_target:
                    md_lines.append(f'document: "[[{doc_link_target}]]"')
                combined_tags.append(doc_type)
            else:
                combined_tags.append("handwritten")

            if tags:
                for t in tags:
                    clean_t = self._sanitize_tag(t)
                    if clean_t and clean_t.lower() not in [ct.lower() for ct in combined_tags]:
                        combined_tags.append(clean_t)

            md_lines.append("tags:")
            for t in combined_tags:
                md_lines.append(f"  - {t}")
            md_lines.append("---")
            md_lines.append("")

            # --- Text Content ---
            if text_content.strip():
                md_lines.append(text_content.strip())
                md_lines.append("")

            # --- Attachments ---
            if image_links:
                md_lines.append("---")
                md_lines.append("")
                md_lines.append("## Original Pages")
                for link in image_links:
                    md_lines.append(link)
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
