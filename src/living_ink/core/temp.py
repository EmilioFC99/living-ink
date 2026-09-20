"""Where one document's throwaway artifacts live while it is being synced.

Everything under here is deleted after the document publishes, and again when
the process exits. Nothing in it is an input to a later run: the caches
(``cache.py``) are what make a repeat sync cheap, and they live elsewhere on
purpose so that purging temp files cannot cost money.
"""

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

#: Pulls the page number back out of a rendered page's filename.
_PAGE_NUMBER = re.compile(r"page-(\d+)", re.IGNORECASE)


@dataclass(frozen=True)
class DocumentWorkspace:
    """Every temp path one document uses, under a directory only it owns.

    Artifacts used to be named after the document's *title*. ``"My Notes"`` and
    ``"My/Notes"`` both sanitise to ``My_Notes``, and the page lookup matched on
    a filename *prefix*, so ``Notes`` also matched ``Notes.2.page-1.png``. Two
    documents with similar titles shared their pages, their transcript and
    their download, and published each other's handwriting — a failure that
    reads as a bad transcription and so never gets reported as a bug.

    The identity is therefore the document id, which is what identifies a
    document everywhere else, and it is a *directory* rather than a filename
    prefix: purging is removing one tree, and a tree cannot match a neighbour.

    Attributes:
        root: The directory all workspaces live under.
        doc_id: The document's id on the tablet.
    """

    root: Path
    doc_id: str

    @property
    def dir(self) -> Path:
        """This document's own directory."""
        return self.root / self.doc_id

    @property
    def pages_dir(self) -> Path:
        """Rendered page images, one PNG per page."""
        return self.dir / "pages"

    @property
    def preprocessed_dir(self) -> Path:
        """Page images after the OCR preprocessing pass."""
        return self.dir / "preprocessed"

    @property
    def download(self) -> Path:
        """The document zip as it came off the tablet."""
        return self.dir / "source.zip"

    @property
    def transcript(self) -> Path:
        """The assembled transcript, written for a human to read."""
        return self.dir / "transcript.txt"

    def source_file(self, suffix: str) -> Optional[Path]:
        """Where the original PDF or EPUB inside the zip is unpacked.

        Args:
            suffix: The source's file extension, without a dot. A source with
                no original file passes an empty string.

        Returns:
            The path to unpack to, or None for a source that has no original.
        """
        return self.dir / f"source.{suffix}" if suffix else None

    def page_image(self, page: int) -> Path:
        """Where the rendered image for one page belongs.

        Args:
            page: The page's one-based number.
        """
        return self.pages_dir / f"page-{page}.png"

    def rendered_pages(self) -> List[Path]:
        """Every page image already rendered, **in page order**.

        Sorted on the number, not on the name. Sorting on the name puts
        ``page-10`` second, and the caller zips this list against the page
        descriptions the renderer produced — so on any document with ten or
        more pages, page 10's image was published under page 2's heading.

        Returns:
            The page images, ascending by page number.
        """
        if not self.pages_dir.exists():
            return []
        images = [p for p in self.pages_dir.iterdir() if p.suffix.lower() == ".png"]
        return sorted(images, key=lambda p: (page_number(p) or 0, p.name))

    def ensure(self) -> "DocumentWorkspace":
        """Create the directories the stages write into.

        Returns:
            Itself, so a caller can build and use it in one expression.
        """
        self.pages_dir.mkdir(parents=True, exist_ok=True)
        self.preprocessed_dir.mkdir(parents=True, exist_ok=True)
        return self

    def purge(self) -> None:
        """Delete everything this document left behind.

        Errors are swallowed: a temp file that will not delete is a nuisance,
        never a reason to fail a document that has already published.
        """
        shutil.rmtree(self.dir, ignore_errors=True)


def page_number(image: Path) -> Optional[int]:
    """Read a page's number back out of its filename.

    Args:
        image: A rendered page image.

    Returns:
        The one-based page number, or None if the name does not carry one.
    """
    match = _PAGE_NUMBER.search(image.name)
    return int(match.group(1)) if match else None


def purge_all(root: Path) -> None:
    """Delete every document's workspace.

    Run at the start of a sync and again when the process exits, so a run that
    died half way through does not leave the disk filling up.

    Args:
        root: The directory all workspaces live under.
    """
    if not root.exists():
        return
    for entry in root.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            try:
                entry.unlink()
            except OSError:
                pass
