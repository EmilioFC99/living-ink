"""Content-addressed caches for the two expensive steps of a sync.

Transcribing a page costs money and rendering one costs CPU, and both are pure
functions: the same input, processed by the same code under the same settings,
produces the same output. Anything pure is cacheable, and the key is simply
everything the answer depends on.

Two caches, one shape:

* :class:`TranscriptCache` keys a page's text on the page image plus a
  fingerprint of the model and prompts
  (:func:`living_ink.clean.transcription_fingerprint`).
* :class:`RenderCache` keys a page's PNG on the ``.rm`` source plus a
  fingerprint of the renderer
  (:func:`living_ink.extract.renderer_fingerprint`).

That the key contains the fingerprint is the point rather than an
implementation detail. Editing ``ocr_prompt.txt`` is supposed to change the
transcription, and upgrading ``rmc`` is supposed to change the render; both
have to miss. A cache keyed on the input alone would silently serve the old
code's answers forever.

Entries live under the data directory and therefore survive the temp purge that
removes page images and transcripts after every run — which is what makes a
repeat sync of an unchanged notebook free rather than merely fast.
"""

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Iterator, Optional, Tuple

logger = logging.getLogger(__name__)

#: Directory under the data dir that holds the cached transcriptions.
CACHE_DIRNAME = "transcripts"

#: Directory under the data dir that holds the cached page renders.
RENDER_CACHE_DIRNAME = "renders"

#: An entry unused for this long is dropped by :meth:`FileCache.prune`. Long
#: enough that a notebook revisited next season is still free, short enough
#: that the directory does not grow without bound.
DEFAULT_MAX_AGE_DAYS = 90

_SECONDS_PER_DAY = 86400


class FileCache:
    """A durable, content-addressed store of one kind of artifact.

    Entries are files named after their key and sharded one level deep, so a
    library of thousands of pages does not land in a single directory. Every
    operation is best-effort: a cache that cannot be read or written degrades
    to doing the work again, never to a failed run.

    Attributes:
        root: Directory holding the entries.
        enabled: When False, every lookup misses and nothing is written.
        max_age_days: Idle age at which :meth:`prune` drops an entry.
        suffix: File extension entries are stored under.
    """

    #: Extension for this cache's entries; subclasses override it.
    suffix = ".bin"

    #: What one entry is called in a message to the user.
    noun = "entry"

    def __init__(
        self,
        root: Path,
        enabled: bool = True,
        max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    ) -> None:
        """Open a cache rooted at ``root``, without creating it.

        Args:
            root: Directory to hold the entries. Created on first write.
            enabled: Whether the cache does anything at all.
            max_age_days: Idle age at which :meth:`prune` drops an entry.
        """
        self.root = root
        self.enabled = enabled
        self.max_age_days = max_age_days

    # --- keys -------------------------------------------------------------

    @staticmethod
    def key(payload: bytes, fingerprint: str) -> str:
        """Return the cache key for one input processed under one fingerprint.

        Args:
            payload: The input bytes, exactly as they will be processed.
            fingerprint: Identifies the code and settings that will process
                them, so a change in either misses rather than lies.

        Returns:
            A hex digest usable as a filename.
        """
        digest = hashlib.sha256()
        digest.update(payload)
        digest.update(b"\0")
        digest.update(fingerprint.encode("utf-8"))
        return digest.hexdigest()

    def _path_for(self, key: str) -> Path:
        """Return the file that holds ``key``, sharded by its first two chars."""
        return self.root / key[:2] / f"{key}{self.suffix}"

    # --- entries ----------------------------------------------------------

    def _read(self, key: str) -> Optional[bytes]:
        """Return the raw bytes stored under ``key``, refreshing its last use.

        Args:
            key: A key from :meth:`key`.

        Returns:
            The stored bytes, or None on a miss or an unreadable entry.
        """
        if not self.enabled:
            return None

        path = self._path_for(key)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            logger.debug("Discarding an unreadable cache entry: %s", path, exc_info=True)
            self._discard(path)
            return None

        self._touch(path)
        return data

    def _write(self, key: str, data: bytes) -> None:
        """Store raw bytes under ``key``.

        Writing through a temporary file keeps a half-written entry from ever
        being visible: pages are processed concurrently, and an interrupted run
        must not leave a valid-looking filename holding nothing.

        Args:
            key: A key from :meth:`key`.
            data: The bytes to store.
        """
        if not self.enabled:
            return

        path = self._path_for(key)
        # Unique per writer: two threads storing the same key at once must not
        # write the same temporary file.
        tmp = path.with_suffix(f".{os.getpid()}.{id(data)}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(data)
            os.replace(tmp, path)
        except OSError:
            logger.debug("Could not write the cache entry at %s", path, exc_info=True)
            self._discard(tmp)

    @staticmethod
    def _touch(path: Path) -> None:
        """Mark an entry as used just now, ignoring a read-only cache."""
        try:
            os.utime(path, None)
        except OSError:
            pass

    @staticmethod
    def _discard(path: Path) -> None:
        """Remove a file that is of no use, ignoring a failure to do so."""
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    # --- maintenance ------------------------------------------------------

    def entries(self) -> Iterator[Path]:
        """Yield the path of every cached entry.

        Yields:
            One path per entry. Empty when the cache has never been written.
        """
        if not self.root.exists():
            return
        yield from self.root.glob(f"*/*{self.suffix}")

    def stats(self) -> Tuple[int, int]:
        """Return how much the cache is holding.

        Returns:
            A (number of entries, total bytes) pair.
        """
        count = 0
        total = 0
        for entry in self.entries():
            try:
                total += entry.stat().st_size
            except OSError:
                continue
            count += 1
        return count, total

    def clear(self) -> int:
        """Delete every entry.

        Returns:
            How many entries were removed.
        """
        removed = 0
        for entry in self.entries():
            self._discard(entry)
            removed += 1
        self._drop_empty_shards()
        return removed

    def prune(self, max_age_days: Optional[int] = None) -> int:
        """Delete entries that have gone unused for too long.

        Args:
            max_age_days: Override for :attr:`max_age_days`. A value of 0 or
                less prunes nothing, since that would mean "expire on write".

        Returns:
            How many entries were removed.
        """
        limit = self.max_age_days if max_age_days is None else max_age_days
        if limit <= 0:
            return 0

        cutoff = time.time() - limit * _SECONDS_PER_DAY
        removed = 0
        for entry in self.entries():
            try:
                used_at = entry.stat().st_mtime
            except OSError:
                continue
            if used_at < cutoff:
                self._discard(entry)
                removed += 1
        self._drop_empty_shards()
        return removed

    def _drop_empty_shards(self) -> None:
        """Remove shard directories left behind with nothing in them."""
        if not self.root.exists():
            return
        for shard in self.root.iterdir():
            if not shard.is_dir():
                continue
            try:
                shard.rmdir()
            except OSError:
                # Not empty, which is the normal case.
                pass


class TranscriptCache(FileCache):
    """Page transcriptions, keyed by the page image and the model reading it."""

    suffix = ".json"
    noun = "transcribed page"

    def get(self, key: str) -> Optional[Tuple[str, str]]:
        """Return the cached (raw, cleaned) pair for ``key``, if there is one.

        A hit refreshes the entry's last-used time, so :meth:`prune` measures
        how long an entry has gone *unused* rather than how long ago it was
        written.

        Args:
            key: A key from :meth:`FileCache.key`.

        Returns:
            The cached pair, or None on a miss or an unreadable entry.
        """
        data = self._read(key)
        if data is None:
            return None

        try:
            payload = json.loads(data.decode("utf-8"))
            return str(payload["raw"]), str(payload["clean"])
        except (ValueError, TypeError, KeyError):
            # A truncated or hand-edited entry is indistinguishable from a
            # miss, and treating it as one repairs it on the way past.
            path = self._path_for(key)
            logger.debug("Discarding a malformed cache entry: %s", path, exc_info=True)
            self._discard(path)
            return None

    def put(self, key: str, raw: str, cleaned: str) -> None:
        """Store one page's transcription.

        Args:
            key: A key from :meth:`FileCache.key`.
            raw: The raw OCR text.
            cleaned: The cleaned text, as published.
        """
        payload = json.dumps({"raw": raw, "clean": cleaned}, ensure_ascii=False)
        self._write(key, payload.encode("utf-8"))


class RenderCache(FileCache):
    """Rendered page images, keyed by the ``.rm`` source and the renderer."""

    suffix = ".png"
    noun = "rendered page"

    def get(self, key: str) -> Optional[bytes]:
        """Return the cached PNG for ``key``, if there is one.

        Args:
            key: A key from :meth:`FileCache.key`.

        Returns:
            The PNG bytes, or None on a miss.
        """
        data = self._read(key)
        if not data:
            # A zero-byte entry is not a page; treat it as a miss so the next
            # render replaces it.
            return None
        return data

    def put(self, key: str, png_bytes: bytes) -> None:
        """Store one rendered page.

        Args:
            key: A key from :meth:`FileCache.key`.
            png_bytes: The rendered PNG.
        """
        if png_bytes:
            self._write(key, png_bytes)


def format_size(num_bytes: int) -> str:
    """Render a byte count the way a person reads one.

    Args:
        num_bytes: A size in bytes.

    Returns:
        A short string such as ``"12 B"``, ``"4.2 KB"`` or ``"1.1 MB"``.
    """
    if num_bytes < 1024:
        return f"{num_bytes} B"
    size = float(num_bytes)
    for unit in ("KB", "MB", "GB"):
        size /= 1024
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
    return f"{size:.1f} GB"
