"""The domain model that travels between the pipeline and a destination.

This module is a leaf: it imports nothing else from Living Ink, which is what
lets ``destinations/`` read it without importing the pipeline it would
otherwise have to depend on.
"""

from dataclasses import dataclass, field
from typing import Optional, Tuple


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
