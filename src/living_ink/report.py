"""What a sync actually did, accumulated as it happens and printed once.

A run used to end in a stream of per-notebook log lines and nothing else: no
count of what was skipped as unchanged, no count of what was read from cache
rather than paid for, no list of what failed and why. This module holds that
tally. The pipeline appends to a :class:`RunReport` as each document finishes
and prints it at the end, in a human table or as JSON for scripting.

Deliberately absent: a cost estimate in dollars. Prices differ per provider and
per model, change without notice, and are not something this project can read
from anywhere authoritative. A wrong number would be worse than none, so the
report gives the API call count — the thing that is actually known — and lets
the user apply their own provider's rate.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

#: A document that was published to at least one destination.
PUBLISHED = "published"
#: A document that needed no work: already current at every destination.
SKIPPED = "skipped"
#: A document that was attempted and did not make it.
FAILED = "failed"
#: A document a real run would have published: transcribed under ``--dry-run``.
WOULD_PUBLISH = "would_publish"
#: A document that needs work but fell outside ``--limit``. Distinct from
#: :data:`SKIPPED` because it is not up to date: reporting it as unchanged is
#: how a run with the default limit of 1 told the user nine pending notebooks
#: were fine.
DEFERRED = "deferred"

_MARKERS = {PUBLISHED: "✓", SKIPPED: "⊘", FAILED: "✗", WOULD_PUBLISH: "◦", DEFERRED: "…"}


@dataclass
class DocumentOutcome:
    """What happened to one document during a run.

    Attributes:
        name: The notebook's display title.
        doc_id: reMarkable document id.
        status: One of :data:`PUBLISHED`, :data:`SKIPPED`, :data:`FAILED`.
        pages: Pages the document turned out to have.
        transcribed: Pages sent to the AI provider, and therefore paid for.
        cached: Pages served from the transcript cache at no cost.
        destinations: Destinations that accepted the note.
        reason: Why it was skipped or how it failed.
        pages_failed: Pages that could not be rendered. The note still
            published, without them, so a ✓ alone would overstate the result.
    """

    name: str
    doc_id: Optional[str] = None
    status: str = PUBLISHED
    pages: int = 0
    transcribed: int = 0
    cached: int = 0
    destinations: List[str] = field(default_factory=list)
    reason: Optional[str] = None
    pages_failed: int = 0

    @property
    def is_partial(self) -> bool:
        """Whether the note published but is missing pages."""
        return bool(self.pages_failed) and self.status in (PUBLISHED, WOULD_PUBLISH)

    def describe(self) -> str:
        """Render this document as one line of the summary table.

        Returns:
            A single line, already padded to line up with its neighbours.
        """
        marker = "⚠" if self.is_partial else _MARKERS.get(self.status, "·")
        # Truncated names are marked, so a clipped title does not read as a
        # notebook that is genuinely called "Fundamentals of Data Eng".
        name = self.name if len(self.name) <= 24 else self.name[:23] + "…"
        line = f"  {marker} {name:<24}"
        if self.status == SKIPPED:
            return f"{line} {self.reason or 'unchanged'}"
        if self.status == DEFERRED:
            return f"{line} {self.reason or 'pending, past the limit'}"
        if self.status == FAILED:
            return f"{line} {self.reason or 'failed'}"

        pages = f"{self.pages} page{'' if self.pages == 1 else 's'}"
        work = f"{self.transcribed} transcribed, {self.cached} cached"
        where = ", ".join(self.destinations) or "nowhere"
        arrow = "⇢" if self.status == WOULD_PUBLISH else "→"
        described = f"{line} {pages:<9} {work:<28} {arrow} {where}"
        if self.is_partial:
            missing = f"{self.pages_failed} page{'' if self.pages_failed == 1 else 's'}"
            described += f"   ({missing} missing)"
        return described


@dataclass
class RunReport:
    """Everything a run did, in the order it did it.

    Attributes:
        documents: One entry per document the run considered.
        warnings: Run-level problems worth saying out loud at the end, where
            they will not scroll past.
        started_at: Monotonic clock reading when the run began.
        elapsed: Seconds the run took, filled in by :meth:`finish`.
    """

    documents: List[DocumentOutcome] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.monotonic)
    elapsed: float = 0.0

    def add(self, outcome: DocumentOutcome) -> DocumentOutcome:
        """Record one document's outcome.

        Args:
            outcome: The finished document.

        Returns:
            The outcome it was given, so a caller can keep editing it.
        """
        self.documents.append(outcome)
        return outcome

    def warn(self, message: str) -> None:
        """Record a run-level warning, ignoring one already recorded.

        Args:
            message: What to tell the user at the end of the run.
        """
        if message not in self.warnings:
            self.warnings.append(message)

    def finish(self) -> "RunReport":
        """Stop the clock.

        Returns:
            This report, so the call can be chained.
        """
        self.elapsed = time.monotonic() - self.started_at
        return self

    def _of(self, status: str) -> List[DocumentOutcome]:
        return [d for d in self.documents if d.status == status]

    @property
    def published(self) -> int:
        """Documents published to at least one destination."""
        return len(self._of(PUBLISHED))

    @property
    def would_publish(self) -> int:
        """Documents a real run would have published, under ``--dry-run``."""
        return len(self._of(WOULD_PUBLISH))

    @property
    def skipped(self) -> int:
        """Documents that needed no work."""
        return len(self._of(SKIPPED))

    @property
    def deferred(self) -> int:
        """Documents that need work but fell outside the run's limit."""
        return len(self._of(DEFERRED))

    @property
    def failed(self) -> int:
        """Documents that were attempted and did not make it."""
        return len(self._of(FAILED))

    @property
    def partial(self) -> int:
        """Documents that published without every page."""
        return len([d for d in self.documents if d.is_partial])

    @property
    def pages_failed(self) -> int:
        """Pages that could not be rendered, across the whole run."""
        return sum(d.pages_failed for d in self.documents)

    @property
    def transcribed(self) -> int:
        """Pages sent to the AI provider, across the whole run."""
        return sum(d.transcribed for d in self.documents)

    @property
    def cached(self) -> int:
        """Pages served from the transcript cache, across the whole run."""
        return sum(d.cached for d in self.documents)

    @property
    def cache_hit_rate(self) -> Optional[float]:
        """Fraction of pages that cost nothing, or None if no page was read."""
        total = self.transcribed + self.cached
        return None if not total else self.cached / total

    def as_dict(self) -> Dict[str, Any]:
        """Return the whole report as plain data, for ``--json``.

        Returns:
            A JSON-serialisable dict mirroring the printed summary.
        """
        return {
            "documents": [asdict(d) for d in self.documents],
            "warnings": list(self.warnings),
            "seen": len(self.documents),
            "published": self.published,
            "would_publish": self.would_publish,
            "skipped": self.skipped,
            "deferred": self.deferred,
            "failed": self.failed,
            "partial": self.partial,
            "pages_failed": self.pages_failed,
            "pages_transcribed": self.transcribed,
            "pages_cached": self.cached,
            "cache_hit_rate": self.cache_hit_rate,
            "elapsed_seconds": round(self.elapsed, 1),
        }

    def as_json(self) -> str:
        """Return the report as a JSON document.

        Returns:
            Indented JSON, matching the ``status --json`` convention.
        """
        return json.dumps(self.as_dict(), indent=2)

    def render(self) -> str:
        """Render the report as the summary a person reads.

        Returns:
            The full summary block, without a trailing newline.
        """
        if not self.documents:
            return "Nothing to sync: every document is already up to date."

        # A dry run publishes nothing by design, so reporting "synced 0"
        # would read as a failed run rather than a successful rehearsal.
        if self.would_publish and not self.published:
            headline = f"Would sync {self.would_publish} of {len(self.documents)} documents"
        else:
            headline = f"Synced {self.published} of {len(self.documents)} documents"
        lines = ["", f"{headline} in {self.elapsed:.0f}s"]
        lines.extend(d.describe() for d in self.documents)

        read = self.transcribed + self.cached
        if read:
            rate = self.cache_hit_rate or 0.0
            lines.append(
                f"Pages: {read}   API calls: {self.transcribed}   "
                f"From cache: {self.cached} ({rate:.0%})"
            )
        if self.deferred:
            lines.append(
                f"{self.deferred} document(s) still need syncing and were left for the next "
                "run; raise --limit to take more at once."
            )
        if self.failed:
            lines.append(f"{self.failed} document(s) failed; see the lines marked ✗ above.")
        if self.partial:
            lines.append(
                f"{self.partial} document(s) published without every page; "
                f"{self.pages_failed} page(s) could not be rendered — see ⚠ above."
            )
        lines.extend(f"⚠️  {w}" for w in self.warnings)
        return "\n".join(lines)
