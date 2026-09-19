"""Publishing to the macOS Notes app, over AppleScript.

Everything here goes through ``osascript``: the note, its attachments and the
removal of the note it replaces are all interpolated into one script, because
the note object does not exist until that script runs.
"""

import html
import json
import logging
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional

from PIL import Image

from living_ink.core.document import PublishResult
from living_ink.destinations.base import (
    Destination,
    DestinationStatus,
    DestinationUnavailable,
    MergeUnit,
    register_destination,
)
from living_ink.settings import Settings

logger = logging.getLogger(__name__)


@register_destination("apple_notes", enabled_by_default=True)
class AppleNotesDestination(Destination):
    """Publishes notes to Apple Notes application via AppleScript.

    Attributes:
        folder_name: The root folder in Apple Notes where notes are stored.
    """

    # The class name, because that is what every existing state.db row says.
    state_key: ClassVar[str] = "AppleNotesDestination"
    display_name: ClassVar[str] = "Apple Notes"

    # Inherent, not a limitation of this code: one osascript call sets one note
    # body, and Apple Notes strips anything that could mark a page boundary.
    merge_unit: ClassVar[MergeUnit] = MergeUnit.DOCUMENT

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

    def check(self) -> DestinationStatus:
        """Confirm this machine can run AppleScript at all.

        Deliberately not a ``tell application "Notes"``: that launches Notes,
        and on a first run it also raises the automation-permission dialog —
        neither belongs in a check that only reports readiness. Whether the
        user has granted automation access is discovered on the first publish,
        where :class:`DestinationUnavailable` already explains it.

        Returns:
            Whether ``osascript`` exists, and what it means if it does not.
        """
        if shutil.which("osascript") is None:
            return DestinationStatus(
                ok=False,
                detail="Apple Notes needs AppleScript, which only exists on macOS.",
                remedy="Disable apple_notes in your config, or publish to Obsidian instead.",
            )
        return DestinationStatus(ok=True, detail=f"Notes folder '{self.folder_name}'.")

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

    def unpublish(
        self,
        target: Optional[str] = None,
        external_id: Optional[str] = None,
        doc_id: Optional[str] = None,
    ) -> PublishResult:
        """Delete the note this destination created, by its Apple Notes id.

        By id and only by id. Matching on title here would mean deleting a note
        on the strength of what it is called, which is exactly the behaviour
        that used to destroy notes people had written themselves.

        Args:
            target: Unused; the id is the only thing worth matching on.
            external_id: Apple Notes id recorded for the note.
            doc_id: Unused.

        Returns:
            The outcome, ``ok`` being whether a note was deleted.

        Raises:
            DestinationUnavailable: osascript is missing or Notes did not
                respond.
        """
        if not external_id:
            logger.info("No Apple Notes id recorded; leaving the note in place.")
            return PublishResult(ok=False, detail="No Apple Notes id recorded.")

        safe_id = json.dumps(external_id, ensure_ascii=False)
        script = f"""
tell application "Notes"
    delete note id {safe_id}
end tell
"""
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except FileNotFoundError as e:
            raise DestinationUnavailable(
                "osascript not found — Apple Notes publishing requires macOS."
            ) from e
        except subprocess.TimeoutExpired as e:
            raise DestinationUnavailable(f"Apple Notes did not respond within {e.timeout}s.") from e

        if result.returncode != 0:
            # The usual cause is a note the user already deleted by hand, which
            # is the outcome we wanted anyway.
            logger.info("Apple Notes did not delete %s: %s", external_id, result.stderr.strip())
            return PublishResult(ok=False, detail="Apple Notes did not delete the note.")
        return PublishResult(ok=True, external_id=external_id, detail="Note deleted.")

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
            doc_id: Unused. Apple Notes has no place to keep it, and does not
                need one: the note id it returns is a stabler identity than
                anything that could be written into the note body.
            existing_target: Unused. A rename or a move needs no special
                handling here, because the note is deleted by id and recreated
                in the folder it now belongs to.
            document_modified: Unused. Apple Notes keeps its own creation and
                modification dates, and they are right as long as the note is
                updated rather than recreated.
            first_published: Unused, for the same reason.

        Returns:
            The outcome, carrying the folder path the note landed in and the
            Apple Notes id AppleScript returned for it.

        Raises:
            DestinationUnavailable: osascript is missing, timed out, or Notes
                rejected the script on every attempt.
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

                target = (
                    f"{self.folder_name}/{effective_sub_folder}/{notebook_name}"
                    if effective_sub_folder
                    else f"{self.folder_name}/{notebook_name}"
                )
                logger.info("Apple Note created for %s", notebook_name)
                return PublishResult(
                    ok=True,
                    target=target,
                    external_id=result.stdout.strip() or None,
                    detail=target,
                )

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
