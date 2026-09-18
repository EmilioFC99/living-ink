"""Tests for living_ink.report — the end-of-run summary."""

import json

from living_ink.report import (
    FAILED,
    PUBLISHED,
    SKIPPED,
    WOULD_PUBLISH,
    DocumentOutcome,
    RunReport,
)


def _published(name="Meeting Notes", pages=6, transcribed=4, cached=2, dests=("Obsidian",)):
    return DocumentOutcome(
        name=name,
        doc_id="nb-1",
        status=PUBLISHED,
        pages=pages,
        transcribed=transcribed,
        cached=cached,
        destinations=list(dests),
    )


class TestDocumentLines:
    """One line per document, and the line says what happened to it."""

    def test_a_published_document_shows_its_work_and_its_destinations(self):
        line = _published().describe()
        assert "✓" in line
        assert "6 pages" in line
        assert "4 transcribed, 2 cached" in line
        assert "→ Obsidian" in line

    def test_one_page_is_not_pluralised(self):
        assert "1 page " in _published(pages=1).describe()

    def test_a_skipped_document_says_why(self):
        outcome = DocumentOutcome(name="Journal", status=SKIPPED, reason="unchanged")
        assert "⊘" in outcome.describe()
        assert "unchanged" in outcome.describe()

    def test_a_failed_document_says_how(self):
        outcome = DocumentOutcome(name="Sketches", status=FAILED, reason="render failed")
        assert "✗" in outcome.describe()
        assert "render failed" in outcome.describe()

    def test_a_long_title_does_not_break_the_column(self):
        line = _published(name="A" * 80).describe()
        assert "→ Obsidian" in line
        assert "A" * 25 not in line

    def test_a_document_published_nowhere_says_so(self):
        assert "nowhere" in _published(dests=()).describe()

    def test_an_adopted_transcript_says_so_instead_of_showing_zeroes(self):
        """A page count beside two zeroes reads as a failure; it is a shortcut."""
        outcome = _published(pages=1, transcribed=0, cached=0)
        outcome.reused_transcript = True
        line = outcome.describe()
        assert "transcript reused" in line
        assert "0 transcribed" not in line


class TestTotals:
    """The numbers under the table."""

    def _report(self):
        report = RunReport()
        report.add(_published())
        report.add(_published(name="Highlights", pages=2, transcribed=0, cached=2))
        report.add(DocumentOutcome(name="Journal", status=SKIPPED, reason="unchanged"))
        report.add(DocumentOutcome(name="Sketches", status=FAILED, reason="boom"))
        return report

    def test_documents_are_counted_by_status(self):
        report = self._report()
        assert (report.published, report.skipped, report.failed) == (2, 1, 1)

    def test_pages_are_summed_across_documents(self):
        report = self._report()
        assert (report.transcribed, report.cached) == (4, 4)

    def test_the_cache_hit_rate_is_the_fraction_that_cost_nothing(self):
        assert self._report().cache_hit_rate == 0.5

    def test_a_run_that_read_no_pages_has_no_hit_rate(self):
        """Zero of zero is not zero percent; it is unanswerable."""
        report = RunReport()
        report.add(DocumentOutcome(name="Journal", status=SKIPPED))
        assert report.cache_hit_rate is None

    def test_finish_stops_the_clock(self):
        report = RunReport()
        assert report.finish().elapsed >= 0


class TestRenderedSummary:
    """What a person actually sees."""

    def test_an_empty_run_says_there_was_nothing_to_do(self):
        assert "already up to date" in RunReport().finish().render()

    def test_the_header_counts_published_against_seen(self):
        report = RunReport()
        report.add(_published())
        report.add(DocumentOutcome(name="Journal", status=SKIPPED))
        assert "Synced 1 of 2 documents" in report.finish().render()

    def test_the_cost_line_reports_api_calls_not_dollars(self):
        """No price table can be trusted, so the run reports what it knows."""
        report = RunReport()
        report.add(_published())
        rendered = report.finish().render()
        assert "API calls: 4" in rendered
        assert "From cache: 2 (33%)" in rendered
        assert "$" not in rendered

    def test_failures_are_called_out_under_the_table(self):
        report = RunReport()
        report.add(DocumentOutcome(name="Sketches", status=FAILED, reason="boom"))
        assert "1 document(s) failed" in report.finish().render()

    def test_warnings_appear_at_the_end_where_they_will_not_scroll(self):
        report = RunReport()
        report.add(_published())
        report.warn("the renderer moved")
        assert report.finish().render().endswith("⚠️  the renderer moved")

    def test_the_same_warning_is_not_repeated(self):
        report = RunReport()
        report.warn("same")
        report.warn("same")
        assert report.warnings == ["same"]


class TestJsonSummary:
    """``--json`` emits the same facts, for scripting."""

    def test_the_structure_mirrors_the_table(self):
        report = RunReport()
        report.add(_published())
        report.add(DocumentOutcome(name="Journal", status=SKIPPED, reason="unchanged"))
        data = json.loads(report.finish().as_json())

        assert data["seen"] == 2
        assert data["published"] == 1
        assert data["skipped"] == 1
        assert data["pages_transcribed"] == 4
        assert data["cache_hit_rate"] == 2 / 6
        assert data["documents"][0]["destinations"] == ["Obsidian"]

    def test_it_is_valid_json_even_when_nothing_happened(self):
        assert json.loads(RunReport().finish().as_json())["seen"] == 0


class TestDryRunSummary:
    """A rehearsal is not a failed run, and must not read like one."""

    def _rehearsed(self):
        return DocumentOutcome(
            name="Test",
            status=WOULD_PUBLISH,
            pages=1,
            transcribed=1,
            destinations=["ObsidianDestination"],
        )

    def test_the_headline_says_would_sync_not_synced_zero(self):
        report = RunReport()
        report.add(self._rehearsed())
        assert "Would sync 1 of 1 documents" in report.finish().render()

    def test_the_destination_arrow_is_conditional(self):
        assert "⇢ ObsidianDestination" in self._rehearsed().describe()

    def test_a_real_publish_still_reads_as_synced(self):
        report = RunReport()
        report.add(_published())
        report.add(self._rehearsed())
        assert "Synced 1 of 2 documents" in report.finish().render()

    def test_json_reports_the_rehearsal_separately(self):
        report = RunReport()
        report.add(self._rehearsed())
        data = json.loads(report.finish().as_json())
        assert (data["published"], data["would_publish"]) == (0, 1)


class TestNameTruncation:
    """A clipped title must not read as the notebook's actual name."""

    def test_a_long_name_is_marked_as_clipped(self):
        outcome = DocumentOutcome(name="Fundamentals of Data Engineering", status=SKIPPED)
        assert "…" in outcome.describe()

    def test_a_name_that_fits_is_left_alone(self):
        assert "…" not in DocumentOutcome(name="Test", status=SKIPPED).describe()

    def test_a_name_of_exactly_the_limit_is_not_clipped(self):
        assert "…" not in DocumentOutcome(name="A" * 24, status=SKIPPED).describe()


class TestPartialDocuments:
    """A note that published without every page must not read as a clean ✓."""

    def _partial(self, missing=1, status=PUBLISHED):
        return DocumentOutcome(
            name="Sketches",
            status=status,
            pages=3,
            transcribed=2,
            destinations=["Obsidian"],
            pages_failed=missing,
        )

    def test_a_missing_page_is_marked(self):
        assert self._partial().is_partial is True

    def test_a_complete_document_is_not(self):
        assert _published().is_partial is False

    def test_a_failed_document_is_not_merely_partial(self):
        """It did not publish at all; ✗ is the honest marker."""
        outcome = DocumentOutcome(name="Sketches", status=FAILED, pages_failed=3)
        assert outcome.is_partial is False

    def test_the_line_warns_instead_of_ticking(self):
        line = self._partial().describe()
        assert line.lstrip().startswith("⚠")
        assert "✓" not in line

    def test_the_line_says_how_many_pages_are_gone(self):
        assert "(2 pages missing)" in self._partial(missing=2).describe()
        assert "(1 page missing)" in self._partial(missing=1).describe()

    def test_a_dry_run_can_be_partial_too(self):
        assert self._partial(status=WOULD_PUBLISH).is_partial is True

    def test_the_summary_totals_the_damage(self):
        report = RunReport()
        report.add(self._partial(missing=2))
        report.add(_published())
        report.finish()

        assert report.partial == 1
        assert report.pages_failed == 2
        assert "1 document(s) published without every page" in report.render()

    def test_a_clean_run_says_nothing_about_missing_pages(self):
        report = RunReport()
        report.add(_published())
        report.finish()

        assert report.partial == 0
        assert "without every page" not in report.render()

    def test_json_carries_the_counts(self):
        report = RunReport()
        report.add(self._partial(missing=2))
        report.finish()

        data = json.loads(report.as_json())
        assert data["partial"] == 1
        assert data["pages_failed"] == 2
        assert data["documents"][0]["pages_failed"] == 2
