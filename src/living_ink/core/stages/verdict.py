"""Whether a transcribed document is worth publishing at all.

Two questions that look like one, asked after OCR because neither is knowable
before it: *did every page fail*, and *did the user say not to publish a
document that came back empty*. Getting them the wrong way round is the defect
this module exists to prevent — a notebook whose every page hit a 429 looks
exactly like a notebook nobody ever wrote in, and answering "empty, skipped,
success" to a rate limit is the tool reporting success for the API refusing to
serve it.

So the order is fixed and the predicates are separate: a failure is a failure
whatever ``sync.skip_empty`` says, and ``skip_empty`` only ever fires on pages
that genuinely produced nothing.
"""

from typing import NamedTuple, Optional, Sequence

from living_ink.core.document import Page


class Blocked(NamedTuple):
    """Why a document must not be published, and how to report that.

    Attributes:
        success: Whether this counts as a successful run for the document. An
            empty notebook is a skip and a success; a document whose every page
            failed is neither, and has to come back next run.
        reason: One line, phrased for the summary table and the log.
    """

    success: bool
    reason: str


def judge_pages(
    pages: Sequence[Page], *, skip_empty: bool, has_text: bool = False
) -> Optional[Blocked]:
    """Decide whether a document's transcribed pages may be published.

    A document with some failed pages and some good ones publishes: holding a
    200-page notebook hostage to three rate-limited pages gives the user
    nothing while they have already paid for 197 transcriptions. The caller
    records the failed count so the next run picks the document up again and
    serves the other 197 from the cache.

    Args:
        pages: The document's pages, after OCR. An empty sequence is not this
            function's business — a document that rendered nothing is decided
            by its source's ``empty_is_skip``, long before here.
        skip_empty: The resolved ``sync.skip_empty`` setting.
        has_text: Whether the document carries an embedded text layer of its
            own. Both verdicts below mean "nothing came back", and a PDF or
            EPUB that yielded its own text has already disproved that — the
            annotations are missing, not the content.

    Returns:
        None when the document should publish, or the :class:`Blocked` verdict
        to report instead.
    """
    if not pages or has_text:
        return None

    if all(page.error and not page.text.strip() for page in pages):
        # Every page, not merely most, and *empty* as well as failed: the
        # boundary between "publish with gaps" and "this document did not
        # work" is that nothing came back at all. A page that salvaged part of
        # its text and then failed came back. No publications row is written,
        # so the next run retries it.
        return Blocked(False, f"every page failed ({len(pages)} of {len(pages)}); will retry")

    if skip_empty and not any(page.text.strip() for page in pages):
        # Checked second and guarded on `error` below, because a rate-limited
        # notebook and a never-written one produce the same empty text.
        if not any(page.error for page in pages):
            return Blocked(True, "skipped: every page is blank (sync.skip_empty)")

    return None
