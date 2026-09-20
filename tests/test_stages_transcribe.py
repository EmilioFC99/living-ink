"""Tests for the transcription stage.

These used to live in ``test_pipeline.py`` and reach into ``SyncPipeline`` for
a cache, a lock and two counters. The stage owns all four now, so a test needs
a ``Transcriber`` and a throwaway directory and nothing else.
"""

import dataclasses
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from living_ink.cache import TranscriptCache
from living_ink.core.stages import Transcriber
from living_ink.redact import clear_secrets, register_secret
from living_ink.settings import Settings


def make_transcriber(tmp_path, *, concurrency=1, enabled=True) -> Transcriber:
    """A transcriber whose cache is a throwaway directory.

    Args:
        tmp_path: The test's temporary directory.
        concurrency: How many pages may be in flight at once.
        enabled: Whether the transcript cache is on.

    Returns:
        A ready transcriber.
    """
    settings = Settings(ai_provider="openai", ai_model="gpt-4o-mini", ocr_concurrency=concurrency)
    return Transcriber(TranscriptCache(tmp_path / "transcripts", enabled=enabled), settings)


@pytest.fixture
def page(tmp_path):
    """A file standing in for a prepared page image."""
    path = tmp_path / "page-1.png"
    path.write_bytes(b"fake png bytes")
    return path


class TestPageConcurrency:
    """Pages are transcribed several at a time, but always reported in order."""

    def test_results_stay_in_page_order(self, tmp_path):
        """A slow first page must not end up after a fast last page."""
        transcriber = make_transcriber(tmp_path, concurrency=4)
        paths = [Path(f"page-{i}.png") for i in range(4)]

        def transcribe(path):
            # Earlier pages finish last, which reorders anything unordered.
            time.sleep(0.05 * (len(paths) - int(path.stem.split("-")[1])))
            return path.name

        with patch.object(transcriber, "transcribe_one", side_effect=transcribe):
            results = transcriber.transcribe(paths)

        assert results == [p.name for p in paths]

    def test_pages_are_transcribed_concurrently(self, tmp_path):
        """Four pages at width four take about one page's time, not four."""
        transcriber = make_transcriber(tmp_path, concurrency=4)
        paths = [Path(f"page-{i}.png") for i in range(4)]

        def transcribe(path):
            time.sleep(0.1)
            return ""

        with patch.object(transcriber, "transcribe_one", side_effect=transcribe):
            started = time.monotonic()
            transcriber.transcribe(paths)
            elapsed = time.monotonic() - started

        assert elapsed < 0.3, f"pages appear to have run serially ({elapsed:.2f}s)"

    def test_concurrency_of_one_runs_serially(self, tmp_path):
        transcriber = make_transcriber(tmp_path, concurrency=1)
        paths = [Path("a.png"), Path("b.png")]
        in_flight = []

        def transcribe(path):
            in_flight.append(path.name)
            assert len(in_flight) == 1
            in_flight.pop()
            return path.name

        with patch.object(transcriber, "transcribe_one", side_effect=transcribe):
            results = transcriber.transcribe(paths)

        assert results == ["a.png", "b.png"]

    def test_no_pages_needs_no_workers(self, tmp_path):
        assert make_transcriber(tmp_path, concurrency=4).transcribe([]) == []

    def test_the_vision_result_is_the_transcript(self, tmp_path):
        transcriber = make_transcriber(tmp_path)

        with patch.object(transcriber, "_read", return_value="clean text"):
            assert transcriber.transcribe_one(Path("p.png")) == ("clean text", None)

    def test_an_empty_vision_result_has_nowhere_left_to_fall_back_to(self, tmp_path):
        """One backend: a page the model could not read is an empty page."""
        transcriber = make_transcriber(tmp_path)

        with patch.object(transcriber, "_read", return_value=""):
            assert transcriber.transcribe_one(Path("p.png")) == ("", None)


class TestAFailedPageIsNotABlankPage:
    """Three pages out of two hundred failing is not a failed notebook.

    Both a failure and a blank arrive as ``text == ""``. Only the second half
    of the pair tells them apart, and without it the partial-page policy is
    unimplementable: the page vanishes from the note with nothing marking where
    it was, and the next run's merge has no block to heal.
    """

    def test_a_raising_page_reports_the_reason_instead_of_propagating(self, tmp_path):
        transcriber = make_transcriber(tmp_path)

        with patch.object(transcriber, "_read", side_effect=RuntimeError("429 rate limited")):
            text, error = transcriber.transcribe_one(Path("p.png"))

        assert text == ""
        assert "429 rate limited" in error

    def test_the_error_names_the_exception_type(self, tmp_path):
        transcriber = make_transcriber(tmp_path)

        with patch.object(transcriber, "_read", side_effect=OSError("truncated")):
            _, error = transcriber.transcribe_one(Path("p.png"))

        assert error.startswith("OSError:")

    def test_a_key_in_the_message_is_redacted_before_it_reaches_the_user(self, tmp_path):
        """The reason is printed and put in the report; a provider URL can carry a key."""
        transcriber = make_transcriber(tmp_path)
        register_secret("sk-supersecret")

        try:
            with patch.object(transcriber, "_read", side_effect=RuntimeError("sk-supersecret")):
                _, error = transcriber.transcribe_one(Path("p.png"))
        finally:
            clear_secrets()

        assert "sk-supersecret" not in error

    def test_one_bad_page_does_not_cost_the_other_two(self, tmp_path):
        transcriber = make_transcriber(tmp_path)
        paths = [tmp_path / f"page-{n}.png" for n in range(1, 4)]
        for index, path in enumerate(paths):
            path.write_bytes(f"page {index}".encode("utf-8"))

        def read(path):
            if path.name.endswith("page-2.png"):
                raise RuntimeError("boom")
            return "text"

        with patch.object(transcriber, "_read", side_effect=read):
            results = transcriber.transcribe(paths)

        assert [text for text, _ in results] == ["text", "", "text"]
        assert "boom" in results[1][1]


class TestTranscriptionCaching:
    """A page already paid for is never paid for twice."""

    def test_the_first_read_calls_the_provider(self, tmp_path, page):
        transcriber = make_transcriber(tmp_path)

        with patch.object(transcriber, "_read", return_value="text") as ocr:
            assert transcriber.transcribe_one(page) == ("text", None)

        assert ocr.call_count == 1

    def test_the_second_read_does_not(self, tmp_path, page):
        """The whole point: an unchanged page costs nothing the next time."""
        transcriber = make_transcriber(tmp_path)
        with patch.object(transcriber, "_read", return_value="text"):
            transcriber.transcribe_one(page)

        with patch.object(transcriber, "_read") as ocr:
            assert transcriber.transcribe_one(page) == ("text", None)

        ocr.assert_not_called()
        assert transcriber.hits == 1

    def test_a_run_interrupted_mid_notebook_only_pays_for_what_it_missed(self, tmp_path):
        """Each page is banked as it comes back, so Ctrl+C loses one page."""
        pages = []
        for index in range(4):
            path = tmp_path / f"page-{index}.png"
            path.write_bytes(f"page {index}".encode("utf-8"))
            pages.append(path)

        transcriber = make_transcriber(tmp_path)

        def transcribe_then_quit(path):
            if path == pages[2]:
                raise KeyboardInterrupt
            return path.name

        with patch.object(transcriber, "_read", side_effect=transcribe_then_quit):
            with pytest.raises(KeyboardInterrupt):
                transcriber.transcribe(pages)

        resumed = make_transcriber(tmp_path)
        with patch.object(resumed, "_read", side_effect=lambda p: p.name) as ocr:
            resumed.transcribe(pages)

        # Pages 0 and 1 were banked before the interrupt; only 2 and 3 are paid for.
        assert [call.args[0] for call in ocr.call_args_list] == pages[2:]
        assert resumed.hits == 2

    def test_a_cache_survives_a_new_transcriber(self, tmp_path, page):
        """Entries outlive the run, which is what the temp purge does not."""
        first = make_transcriber(tmp_path)
        with patch.object(first, "_read", return_value="text"):
            first.transcribe_one(page)

        second = make_transcriber(tmp_path)
        with patch.object(second, "_read") as ocr:
            assert second.transcribe_one(page) == ("text", None)

        ocr.assert_not_called()

    def test_an_edited_page_is_read_again(self, tmp_path, page):
        transcriber = make_transcriber(tmp_path)
        with patch.object(transcriber, "_read", return_value="text"):
            transcriber.transcribe_one(page)

        page.write_bytes(b"different png bytes")
        with patch.object(transcriber, "_read", return_value="new text") as ocr:
            assert transcriber.transcribe_one(page) == ("new text", None)

        assert ocr.call_count == 1

    def test_an_entry_from_the_two_backend_era_is_not_served(self, tmp_path, page):
        """Its payload is a raw/clean pair, and neither half is this build's answer."""
        transcriber = make_transcriber(tmp_path)
        key = transcriber.cache_key(page)
        transcriber.cache._write(key, b'{"raw": "google text", "clean": "repaired text"}')

        with patch.object(transcriber, "_read", return_value="vision text") as ocr:
            assert transcriber.transcribe_one(page) == ("vision text", None)

        assert ocr.call_count == 1

    def test_an_empty_transcription_is_not_cached(self, tmp_path, page):
        """A blank page is usually a rate limit, and must not become permanent."""
        transcriber = make_transcriber(tmp_path)
        with patch.object(transcriber, "_read", return_value=""):
            transcriber.transcribe_one(page)

        with patch.object(transcriber, "_read", return_value="text") as ocr:
            assert transcriber.transcribe_one(page) == ("text", None)

        assert ocr.call_count == 1

    def test_a_disabled_cache_reads_every_time(self, tmp_path, page):
        transcriber = make_transcriber(tmp_path, enabled=False)

        with patch.object(transcriber, "_read", return_value="text") as ocr:
            transcriber.transcribe_one(page)
            transcriber.transcribe_one(page)

        assert ocr.call_count == 2
        assert not transcriber.cache.root.exists()

    def test_an_unreadable_page_is_transcribed_uncached(self, tmp_path):
        """No bytes to hash means no key; transcribe rather than fail."""
        transcriber = make_transcriber(tmp_path)
        missing = tmp_path / "gone.png"

        with patch.object(transcriber, "_read", return_value="text") as ocr:
            assert transcriber.transcribe_one(missing) == ("text", None)

        assert ocr.call_count == 1
        assert not transcriber.cache.root.exists()

    def test_the_concurrency_setting_is_what_bounds_the_pool(self, tmp_path):
        """A hundred pages at width two is two in flight, not a hundred."""
        transcriber = make_transcriber(tmp_path, concurrency=2)
        transcriber.settings = dataclasses.replace(transcriber.settings, ocr_concurrency=2)
        paths = [Path(f"page-{i}.png") for i in range(8)]
        live, peak = [], 0

        def read(path):
            nonlocal peak
            live.append(path)
            peak = max(peak, len(live))
            time.sleep(0.01)
            live.pop()
            return path.name

        with patch.object(transcriber, "_read", side_effect=read):
            transcriber.transcribe(paths)

        assert peak <= 2
