"""The transcript artifact — the one output written for a person, not a run.

Nothing in the pipeline reads this back. It exists so a user can see what the
model actually read before a destination reshapes it, which is why the page
numbering here matches the notebook rather than the list index, and why a page
that failed says so in the place its text would have been.
"""

import json
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from living_ink.core.document import Page


def write_transcript(
    path: Path,
    meta: Dict[str, Any],
    pages: Sequence[Page],
    extracted_text: str = "",
    source_path: Optional[Path] = None,
) -> None:
    """Write one transcript: a metadata line, then a section per page.

    A page that produced no text keeps its header and gets no body, so the page
    numbering still lines up with the notebook, and a page that failed says so
    where the missing text would have been.

    Args:
        path: File to write.
        meta: Metadata dict, written as the first line.
        pages: The document's pages, in page order.
        extracted_text: An embedded text layer, written on its own when no page
            produced any text — a PDF can be annotated on none of its pages and
            still have everything worth reading.
        source_path: The document the pages came from, for the page headers.
    """
    from living_ink.extract import format_page_section_header

    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(meta) + "\n\n")

        if not pages and extracted_text:
            f.write(extracted_text + "\n")
            return

        for page in pages:
            header = format_page_section_header(
                page.number,
                source_path,
                include_divider=True,
                # Already read once, when the page was rendered. Letting the
                # header re-read them reopens the PDF once per page.
                label=page.label,
                breadcrumbs=page.breadcrumbs,
            )
            body = page.error if page.error else page.text.strip()
            f.write(f"{header}\n\n{body}\n\n" if body else f"{header}\n\n")

        if extracted_text and not any(p.text.strip() for p in pages):
            f.write(extracted_text + "\n")
