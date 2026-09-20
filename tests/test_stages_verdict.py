"""Tests for the stage that decides whether transcribed pages may be published.

``judge_pages`` answers two questions that arrive looking like one — *did every
page fail* and *did the user ask not to publish an empty document* — and the
order it asks them in is the whole contract. Both conditions present as
``text == ""`` on every page, so a test here is mostly about which verdict wins
when both are true: a notebook that hit a rate limit must never be reported as
"empty, skipped, success", because that is the tool claiming success for the
API refusing to serve it and never retrying.
"""

from living_ink.core.stages.verdict import Blocked, judge_pages
from tests.builders import make_page


class TestWhatIsNotJudgedHere:
    """The stage decides one thing, so what it declines to decide matters."""

    def test_no_pages_is_somebody_elses_verdict(self):
        """A document that rendered nothing is the source's ``empty_is_skip`` call.

        Answering it here too would mean two places deciding what an
        unannotated PDF is, and they would eventually disagree — a PDF with a
        text layer and no annotated pages is a complete result, not an empty one.
        """
        assert judge_pages([], skip_empty=True) is None
        assert judge_pages([], skip_empty=False) is None

    def test_a_document_with_text_is_never_blocked(self):
        """The default path: pages came back, so the run publishes them."""
        pages = [make_page(1, "Monday notes"), make_page(2, "Tuesday notes")]

        assert judge_pages(pages, skip_empty=True) is None


class TestEveryPageFailed:
    """Nothing came back at all, so nothing may be recorded as done."""

    def test_an_all_failed_document_is_not_a_success(self):
        """A failed document must come back next run, and only ``success=False`` does that.

        Reporting it as a success writes it off: the publications row lands,
        the summary says fine, and the pages nobody ever read stay unread.
        """
        pages = [make_page(n, error="429 rate limited") for n in (1, 2, 3)]

        verdict = judge_pages(pages, skip_empty=False)

        assert verdict is not None
        assert verdict.success is False

    def test_the_reason_names_how_many_pages_went_down(self):
        """An uncounted "every page failed" reads like a bug; "3 of 3" reads like a rate limit.

        The count is what tells the user whether to wait a minute or check
        their API key, and it is the only number the summary table gets.
        """
        pages = [make_page(n, error="boom") for n in (1, 2, 3)]

        verdict = judge_pages(pages, skip_empty=False)

        assert "3 of 3" in verdict.reason
        assert "retry" in verdict.reason

    def test_the_verdict_is_reportable_as_a_pair(self):
        """The caller unpacks it straight into ``_StopProcessing(success, reason)``.

        A plain tuple would work until someone reordered the two fields and
        turned every failure into a success; the names are the guard.
        """
        verdict = judge_pages([make_page(1, error="boom")], skip_empty=False)

        success, reason = verdict
        assert isinstance(verdict, Blocked)
        assert success is verdict.success
        assert reason is verdict.reason


class TestOnePageFailingIsNotADocumentFailing:
    """The headline of the partial-page policy: gaps publish, they do not block.

    Holding 197 transcribed pages hostage to 3 rate-limited ones gives the user
    nothing while they have already paid for the 197. The failed pages are
    recorded so the next run picks the document up again and serves the rest
    from the cache.
    """

    def test_a_mixed_document_publishes(self):
        pages = [
            make_page(1, "Monday notes"),
            make_page(2, error="429 rate limited"),
            make_page(3, "Wednesday notes"),
        ]

        assert judge_pages(pages, skip_empty=False) is None

    def test_a_single_surviving_page_is_enough(self):
        """The boundary is *all* pages, not most: one page back means the run worked."""
        pages = [make_page(n, error="429 rate limited") for n in (1, 2, 3)]
        pages.append(make_page(4, "the one page that came back"))

        assert judge_pages(pages, skip_empty=True) is None

    def test_a_failed_page_still_publishes_when_the_rest_are_merely_blank(self):
        """With ``skip_empty`` off, a blank page is content; the gap marker still ships.

        This is what gives the next run's merge a block to heal — a page that
        vanished silently has nowhere to put the real text when it arrives.
        """
        pages = [make_page(1, ""), make_page(2, error="429 rate limited")]

        assert judge_pages(pages, skip_empty=False) is None


class TestSkippingABlankDocument:
    """``sync.skip_empty`` is opt-in, and it means blank — never failed."""

    def test_a_blank_document_publishes_by_default(self):
        """Off by default: a user who never set the flag gets their empty note.

        Silently dropping documents is the behaviour that makes a sync tool
        untrustworthy, so it has to be something the user asked for.
        """
        pages = [make_page(1, ""), make_page(2, "")]

        assert judge_pages(pages, skip_empty=False) is None

    def test_a_blank_document_is_skipped_when_asked(self):
        """A notebook opened and never written in should not litter the vault."""
        pages = [make_page(1, ""), make_page(2, "")]

        verdict = judge_pages(pages, skip_empty=True)

        assert verdict == Blocked(True, "skipped: every page is blank (sync.skip_empty)")

    def test_a_skip_is_a_success(self):
        """Nothing went wrong, so the run must not report a failure or retry forever."""
        verdict = judge_pages([make_page(1, "")], skip_empty=True)

        assert verdict.success is True
        assert "sync.skip_empty" in verdict.reason

    def test_whitespace_is_not_content(self):
        """A model that answers with a newline has read a blank page, not written one.

        Without the strip, ``skip_empty`` would never fire on the documents it
        exists for, because a vision response is rarely byte-empty.
        """
        pages = [make_page(1, "   "), make_page(2, "\n\n"), make_page(3, "\t")]

        verdict = judge_pages(pages, skip_empty=True)

        assert verdict is not None
        assert verdict.success is True


class TestARateLimitIsNotAnEmptyNotebook:
    """The trap the ordering exists for: both conditions are true at once.

    Every page of a rate-limited document is blank *and* every page failed. If
    ``skip_empty`` is consulted first, or without checking ``error``, the run
    answers "skipped: every page is blank" — a success, with no publications
    row's worth of doubt — and the user's notebook is quietly written off as
    something they never wrote in.
    """

    def test_an_all_failed_document_is_a_failure_even_with_skip_empty_on(self):
        pages = [make_page(n, error="429 rate limited") for n in (1, 2, 3)]

        verdict = judge_pages(pages, skip_empty=True)

        assert verdict.success is False
        assert "sync.skip_empty" not in verdict.reason
        assert "3 of 3" in verdict.reason

    def test_one_failed_page_among_blanks_is_not_a_skip(self):
        """A document that is half blank and half broken is broken: retry, do not write off.

        ``skip_empty`` guards on ``error`` as well as on text precisely so the
        one failed page keeps the other blank ones from looking deliberate.
        """
        pages = [make_page(1, ""), make_page(2, ""), make_page(3, error="429 rate limited")]

        assert judge_pages(pages, skip_empty=True) is None

    def test_a_failed_page_that_salvaged_text_is_not_blank(self):
        """Text that did arrive counts, whatever else went wrong on that page.

        An error and a partial transcription are not exclusive — a truncated
        response carries both — and throwing the text away because the page
        also reported a problem loses work the user already paid for.
        """
        pages = [
            make_page(1, "salvaged before the stream cut", error="truncated response"),
            make_page(2, ""),
        ]

        assert judge_pages(pages, skip_empty=True) is None


class TestADocumentThatBroughtItsOwnText:
    """``has_text`` is the carve-out for a PDF or EPUB with an embedded text layer.

    Both verdicts below are ways of saying *nothing came back*. A document whose
    own text extracted cleanly has already disproved that, whatever its
    annotations did: what is missing is the annotations, not the content. Without
    the carve-out an annotated PDF whose three ink pages hit a 429 would publish
    nothing at all, even though the 400 pages of the book itself are in hand.
    """

    def test_a_failed_annotation_does_not_suppress_the_text_layer(self):
        """The pages can all fail and the document still has something to say."""
        pages = [make_page(n, error="429 rate limited") for n in (1, 2, 3)]

        assert judge_pages(pages, skip_empty=True, has_text=True) is None

    def test_a_document_with_a_text_layer_is_never_blank(self):
        """``skip_empty`` means the document is empty, and this one demonstrably is not.

        The ink pages being blank is the normal state of a PDF read but not
        annotated; skipping it would drop the book.
        """
        pages = [make_page(1, ""), make_page(2, "")]

        assert judge_pages(pages, skip_empty=True, has_text=True) is None

    def test_without_a_text_layer_the_same_pages_are_blocked(self):
        """The control, without which the two above pass for the wrong reason."""
        pages = [make_page(1, ""), make_page(2, "")]

        assert judge_pages(pages, skip_empty=True, has_text=False) is not None

    def test_the_carve_out_is_off_unless_asked_for(self):
        """A notebook has no text layer and must not get the carve-out by default.

        ``has_text`` defaults to False so a caller that has never heard of text
        layers — every notebook — keeps the strict verdict.
        """
        assert judge_pages([make_page(1, error="boom")], skip_empty=False) is not None


class TestTextSalvagedFromAFailedPage:
    """A page can report an error *and* carry text; the text still counts."""

    def test_a_lone_page_that_salvaged_text_publishes(self):
        """Every page failed, so the count says 1 of 1 — but the page is not empty.

        Testing ``error`` alone would block this document and throw away a
        transcription the user has already been billed for. The page is still
        counted as failed, so the next run comes back for the rest of it.
        """
        pages = [make_page(1, "the half that arrived", error="truncated response")]

        assert judge_pages(pages, skip_empty=False) is None

    def test_a_lone_page_that_salvaged_only_whitespace_is_still_a_failure(self):
        """Whitespace is not salvaged text, so the strict verdict still applies."""
        pages = [make_page(1, "  \n ", error="truncated response")]

        verdict = judge_pages(pages, skip_empty=False)

        assert verdict is not None
        assert verdict.success is False


class TestThePackageExportsTheStage:
    """``pipeline.py`` imports stages from the package, not from their modules."""

    def test_the_stage_is_re_exported(self):
        """A stage nobody can import from ``core.stages`` has not been extracted.

        Every other stage is reached that way, and the import in ``pipeline.py``
        is written against the package; leaving this one out of ``__init__``
        would only fail at the call site.
        """
        from living_ink.core import stages

        assert stages.judge_pages is judge_pages
        assert stages.Blocked is Blocked
        assert {"judge_pages", "Blocked"} <= set(stages.__all__)
