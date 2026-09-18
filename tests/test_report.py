"""Tests for living_ink.report — the end-of-run summary."""

import json

from living_ink.report import FAILED, PUBLISHED, SKIPPED, DocumentOutcome, RunReport


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
