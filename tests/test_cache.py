"""Tests for the content-addressed transcription and render caches."""

import os
import time

import pytest

from living_ink.cache import DEFAULT_MAX_AGE_DAYS, RenderCache, TranscriptCache, format_size


@pytest.fixture
def cache(tmp_path):
    """A cache rooted in a throwaway directory."""
    return TranscriptCache(tmp_path / "transcripts")


class TestKeys:
    """The key is everything the transcription depends on, and nothing else."""

    def test_the_same_page_and_fingerprint_give_the_same_key(self):
        first = TranscriptCache.key(b"page bytes", "abc")
        assert first == TranscriptCache.key(b"page bytes", "abc")

    def test_a_different_page_gives_a_different_key(self):
        assert TranscriptCache.key(b"one", "abc") != TranscriptCache.key(b"two", "abc")

    def test_a_different_fingerprint_gives_a_different_key(self):
        """An edited prompt or a switched model must miss the cache."""
        assert TranscriptCache.key(b"page", "abc") != TranscriptCache.key(b"page", "xyz")

    def test_the_boundary_between_page_and_fingerprint_is_real(self):
        """Concatenation alone would collide these two."""
        assert TranscriptCache.key(b"ab", "c") != TranscriptCache.key(b"a", "bc")


class TestStoringAndReading:
    """A stored page comes back exactly as it went in."""

    def test_a_miss_returns_none(self, cache):
        assert cache.get("nothing-here") is None

    def test_a_stored_page_is_returned(self, cache):
        cache.put("k", "clean text")
        assert cache.get("k") == "clean text"

    def test_unicode_survives_the_round_trip(self, cache):
        cache.put("k", "café — naïve")
        assert cache.get("k") == "café — naïve"

    def test_storing_again_replaces_the_entry(self, cache):
        cache.put("k", "old")
        cache.put("k", "new")
        assert cache.get("k") == "new"

    def test_nothing_is_written_until_something_is_stored(self, cache):
        """Merely asking about the cache must not create it."""
        cache.get("k")
        assert not cache.root.exists()

    def test_entries_are_sharded_rather_than_piled_in_one_directory(self, cache):
        cache.put("abcdef", "text")
        assert (cache.root / "ab" / "abcdef.json").exists()

    def test_no_temporary_files_are_left_behind(self, cache):
        cache.put("abcdef", "text")
        assert list(cache.root.glob("*/*.tmp")) == []


class TestDisabled:
    """A disabled cache is inert in both directions."""

    def test_nothing_is_read(self, tmp_path):
        writable = TranscriptCache(tmp_path / "t")
        writable.put("k", "text")

        disabled = TranscriptCache(tmp_path / "t", enabled=False)
        assert disabled.get("k") is None

    def test_nothing_is_written(self, tmp_path):
        disabled = TranscriptCache(tmp_path / "t", enabled=False)
        disabled.put("k", "text")
        assert not disabled.root.exists()


class TestDamagedEntries:
    """A cache that cannot be read degrades to a miss, never to a failure."""

    def test_a_truncated_entry_reads_as_a_miss(self, cache):
        cache.put("abcdef", "text")
        (cache.root / "ab" / "abcdef.json").write_text("{not json")
        assert cache.get("abcdef") is None

    def test_a_truncated_entry_is_discarded_on_the_way_past(self, cache):
        cache.put("abcdef", "text")
        (cache.root / "ab" / "abcdef.json").write_text("{not json")
        cache.get("abcdef")
        assert not (cache.root / "ab" / "abcdef.json").exists()

    def test_an_entry_missing_its_fields_reads_as_a_miss(self, cache):
        """A pair written by the two-backend era is a miss, not half an answer."""
        cache.put("abcdef", "text")
        (cache.root / "ab" / "abcdef.json").write_text('{"raw": "only", "clean": "only"}')
        assert cache.get("abcdef") is None


class TestStats:
    """The size report is what 'living-ink info' prints as its Cache line."""

    def test_an_empty_cache_reports_nothing(self, cache):
        assert cache.stats() == (0, 0)

    def test_entries_are_counted(self, cache):
        cache.put("aa", "text")
        cache.put("bb", "text")
        count, total = cache.stats()
        assert count == 2
        assert total > 0


class TestClearing:
    """Clearing removes the entries and the directories holding them."""

    def test_every_entry_is_removed(self, cache):
        cache.put("aa", "text")
        cache.put("bb", "text")
        assert cache.clear() == 2
        assert cache.stats() == (0, 0)

    def test_empty_shards_do_not_linger(self, cache):
        cache.put("aa", "text")
        cache.clear()
        assert list(cache.root.iterdir()) == []

    def test_clearing_an_empty_cache_is_harmless(self, cache):
        assert cache.clear() == 0


class TestPruning:
    """Pruning drops what has gone unused, measured from the last read."""

    def _age(self, cache, key: str, days: float) -> None:
        """Backdate an entry's last-used time."""
        path = cache._path_for(key)
        old = time.time() - days * 86400
        os.utime(path, (old, old))

    def test_a_fresh_entry_survives(self, cache):
        cache.put("aa", "text")
        assert cache.prune(30) == 0
        assert cache.get("aa") is not None

    def test_a_stale_entry_is_dropped(self, cache):
        cache.put("aa", "text")
        self._age(cache, "aa", 120)
        assert cache.prune(90) == 1
        assert cache.get("aa") is None

    def test_reading_an_entry_keeps_it_alive(self, cache):
        """Age is time since last use, so a page synced weekly stays free."""
        cache.put("aa", "text")
        self._age(cache, "aa", 120)
        cache.get("aa")
        assert cache.prune(90) == 0

    def test_the_configured_age_is_used_by_default(self, tmp_path):
        cache = TranscriptCache(tmp_path / "t", max_age_days=1)
        cache.put("aa", "text")
        self._age(cache, "aa", 5)
        assert cache.prune() == 1

    def test_an_age_of_zero_prunes_nothing(self, cache):
        """Zero would otherwise mean 'expire everything on write'."""
        cache.put("aa", "text")
        assert cache.prune(0) == 0
        assert cache.get("aa") is not None

    def test_pruning_a_cache_that_was_never_written_is_harmless(self, cache):
        assert cache.prune(1) == 0


class TestDefaults:
    """The shipped defaults are the ones the CLI documents."""

    def test_the_default_age_is_a_season(self, cache):
        assert cache.max_age_days == DEFAULT_MAX_AGE_DAYS

    def test_the_cache_is_on_by_default(self, cache):
        assert cache.enabled is True


class TestFormatSize:
    """Sizes are printed the way a person reads them."""

    @pytest.mark.parametrize(
        "num_bytes,expected",
        [(0, "0 B"), (512, "512 B"), (2048, "2.0 KB"), (5 * 1024 * 1024, "5.0 MB")],
    )
    def test_units(self, num_bytes, expected):
        assert format_size(num_bytes) == expected


class TestRenderCache:
    """Rendered pages cache the same way, with bytes instead of JSON."""

    @pytest.fixture
    def renders(self, tmp_path):
        """A render cache rooted in a throwaway directory."""
        return RenderCache(tmp_path / "renders")

    def test_a_miss_returns_none(self, renders):
        assert renders.get("nothing-here") is None

    def test_a_stored_page_comes_back_byte_for_byte(self, renders):
        renders.put("k", b"\x89PNG fake")
        assert renders.get("k") == b"\x89PNG fake"

    def test_entries_are_stored_as_png(self, renders):
        renders.put("abcdef", b"png")
        assert (renders.root / "ab" / "abcdef.png").exists()

    def test_an_empty_render_is_not_stored(self, renders):
        """A render that produced nothing is a failure, not a blank page."""
        renders.put("k", b"")
        assert not renders.root.exists()

    def test_a_zero_byte_entry_reads_as_a_miss(self, renders):
        renders.put("abcdef", b"png")
        (renders.root / "ab" / "abcdef.png").write_bytes(b"")
        assert renders.get("abcdef") is None

    def test_a_disabled_cache_stores_nothing(self, tmp_path):
        off = RenderCache(tmp_path / "r", enabled=False)
        off.put("k", b"png")
        assert not off.root.exists()

    def test_the_two_caches_do_not_share_a_directory(self, tmp_path):
        """A transcript and a render can collide on key; they must not on path."""
        transcripts = TranscriptCache(tmp_path / "transcripts")
        renders = RenderCache(tmp_path / "renders")
        transcripts.put("aa", "text")
        renders.put("aa", b"png")
        assert transcripts.get("aa") == "text"
        assert renders.get("aa") == b"png"
