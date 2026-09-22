"""Turning prepared page images into text.

This is where a sync spends its money and most of its wall-clock time: one
page is one network round trip, and nothing else in the run is charged per
page. Everything here exists to make the second reading of a page free and to
stop one unreadable page from costing the other 199.
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from living_ink.cache import TranscriptCache
from living_ink.clean import ocr_and_repair, transcription_fingerprint
from living_ink.redact import redact
from living_ink.settings import Settings

logger = logging.getLogger(__name__)

#: What one page came back as: its text, and why it has none.
PageResult = Tuple[str, Optional[str]]


class Transcriber:
    """Reads prepared page images, several at a time, in page order.

    One instance per run. It holds the cache counters, which used to be three
    attributes on ``SyncPipeline`` guarded by a fourth — a lock that only this
    code ever took, on a class where any of sixty methods could have taken it.

    Attributes:
        cache: Where a page already read is found again.
        settings: The run's resolved settings; supplies the concurrency and
            the fingerprint the cache key is built from.
        hits: Pages served from the cache so far, across every document.
    """

    def __init__(self, cache: TranscriptCache, settings: Settings) -> None:
        """Build a transcriber for one run.

        Args:
            cache: The transcript cache.
            settings: The run's resolved settings.
        """
        self.cache = cache
        self.settings = settings
        self.hits = 0
        self._lock = threading.Lock()

    def transcribe(self, paths: Sequence[Path]) -> List[PageResult]:
        """Transcribe pages, several at a time, and return them in page order.

        A page is one network round trip and nothing else, so running a few
        concurrently is most of the wall-clock win available in a sync. The
        ceiling is the AI provider's rate limit, which is why the width is the
        configurable ``ocr_concurrency`` rather than the page count.

        Args:
            paths: Prepared page images, in page order.

        Returns:
            One ``(text, error)`` pair per page, in the order given.
        """
        width = min(self.settings.ocr_concurrency, len(paths))
        if width <= 1:
            return [self.transcribe_one(p) for p in paths]

        logger.debug("Transcribing %d pages, %d at a time...", len(paths), width)
        with ThreadPoolExecutor(max_workers=width) as pool:
            # ``map`` yields in submission order, so pages stay in page order
            # however the calls happen to finish.
            return list(pool.map(self.transcribe_one, paths))

    def transcribe_one(self, path: Path) -> PageResult:
        """Transcribe one page, from the cache when it is there.

        Args:
            path: The prepared page image.

        Returns:
            ``(text, error)``. A blank page is ``("", None)`` and a failed one
            is ``("", "<reason>")`` — the two are indistinguishable by text
            alone, which is the whole reason the second element exists. An
            error here costs one page, never the document: the caller marks a
            gap and publishes the rest.
        """
        from living_ink.extract import is_image_blank

        if is_image_blank(path):
            logger.debug("  Page %s is blank; skipping AI vision call.", path.name)
            return "", None

        key = self.cache_key(path)
        if key:
            cached = self.cache.get(key)
            if cached is not None:
                with self._lock:
                    self.hits += 1
                logger.debug("  Cached: %s", path.name)
                return cached, None

        try:
            return self._store(key, self._read(path)), None
        except Exception as e:
            # Deliberately broad, and it does not swallow: the reason is put on
            # the page, warned about, and counted. An unreadable image or a
            # provider that finally gave up used to propagate out of the thread
            # pool and fail every other page in the notebook with it.
            return "", redact(f"{type(e).__name__}: {e}")

    def cache_key(self, path: Path) -> Optional[str]:
        """Return the cache key for one page, or None if it cannot be computed.

        The key covers the page image and the model and prompts behind it, so
        editing a prompt or switching provider correctly misses.

        Args:
            path: The prepared page image.

        Returns:
            A cache key, or None when caching is off or the page is unreadable.
        """
        if not self.cache.enabled:
            return None
        try:
            image_bytes = path.read_bytes()
        except OSError:
            # No page to hash means nothing to key on; transcribe uncached.
            return None
        return self.cache.key(image_bytes, transcription_fingerprint(self.settings))

    def _store(self, key: Optional[str], text: str) -> str:
        """Store a freshly transcribed page and return it unchanged.

        An empty result is not stored. A page that read as nothing is usually a
        provider hiccup or a rate limit rather than a blank page, and caching
        it would make one bad minute permanent.

        Args:
            key: The cache key, or None if this page is not cacheable.
            text: The page's text.

        Returns:
            The text it was given.
        """
        if key and text.strip():
            self.cache.put(key, text)
        return text

    def _read(self, path: Path) -> str:
        """Read and clean one page in a single AI vision call.

        Args:
            path: The prepared page image.

        Returns:
            The cleaned text, or an empty string if vision returned nothing.
        """
        logger.debug("  AI Vision OCR: %s...", path.name)
        cleaned_text = ocr_and_repair(str(path))
        if cleaned_text:
            return cleaned_text

        logger.debug("  AI Vision returned empty for %s", path.name)
        return ""
