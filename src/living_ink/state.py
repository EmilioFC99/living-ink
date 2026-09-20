"""Durable sync state, in SQLite.

Living Ink used to remember what it had published in one JSON file per
destination — ``processed_notebooks_ObsidianDestination.json`` — holding a flat
``{document id: version}`` map. That worked while the only question was "have I
seen this version before", and stopped working for three reasons:

1. **It races.** Every write was a whole-file read-modify-rewrite. Since
   ``watch`` shipped, a daemon and a manual ``living-ink sync`` can run at the
   same time and silently overwrite each other's progress. SQLite serialises
   writers for us, and WAL mode lets a reader run while one writes.
2. **It forgets everything except the version.** When a note was first
   published, which run published it, what was actually sent, what the note is
   called on the far side — none of it had anywhere to live, so features that
   need that history had nowhere to start.
3. **It cannot answer a question.** "What is pending?" meant loading every
   file and joining them by hand.

The schema is deliberately small. Blobs — PNGs, transcripts, downloads — stay
on the filesystem where they already are; this database holds keys and
timestamps, never page content.

Notable invariant: ``publications.first_published_at`` is written once and
never updated, so it can back a truthful ``created`` date on a published note
even after that note has been re-synced a hundred times.
"""

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    Tuple,
)

if TYPE_CHECKING:  # pragma: no cover - import cycle: transport does not need state
    from living_ink.transport import DeviceInfo

#: Bumped whenever the schema changes; drives the migration ladder in _migrate.
#: 5 rebuilt ``publications`` onto a three-part key. A column addition does not
#: need a bump — :data:`_ADDED_COLUMNS` runs on every open — but a primary key
#: cannot be altered in place, so that one does.
SCHEMA_VERSION = 5

#: Name of the database inside the data directory.
DB_FILENAME = "state.db"

#: The only profile 1.0 writes. Named sync profiles are out of scope, but the
#: column and the key they need are here from the start: adding a column later
#: is free, and changing a primary key later means rebuilding the table under
#: every real user's ``created`` dates. One constant string buys that away.
DEFAULT_PROFILE = "default"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    outcome             TEXT,
    documents_seen      INTEGER NOT NULL DEFAULT 0,
    documents_published INTEGER NOT NULL DEFAULT 0,
    documents_failed    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS documents (
    id             TEXT PRIMARY KEY,
    name           TEXT,
    folder         TEXT,
    doc_type       TEXT,
    version        TEXT,
    last_modified  TEXT,
    seen_at        TEXT NOT NULL,
    seen_run_id    INTEGER REFERENCES runs(id),
    last_error     TEXT,
    last_error_at  TEXT
);

CREATE TABLE IF NOT EXISTS publications (
    doc_id             TEXT NOT NULL,
    destination        TEXT NOT NULL,
    profile            TEXT NOT NULL DEFAULT 'default',
    version            TEXT,
    recipe             TEXT NOT NULL DEFAULT '',
    pages_failed       INTEGER NOT NULL DEFAULT 0,
    external_id        TEXT,
    target             TEXT,
    first_published_at TEXT NOT NULL,
    last_published_at  TEXT NOT NULL,
    run_id             INTEGER REFERENCES runs(id),
    PRIMARY KEY (doc_id, destination, profile)
);

CREATE INDEX IF NOT EXISTS publications_by_destination
    ON publications (destination);

-- At most one row: the tablet this installation syncs with. Written only
-- when USB SSH can actually see the hardware, and read on every other run so
-- a Cloud-only sync still knows the geometry a single USB session taught it.
CREATE TABLE IF NOT EXISTS device (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    model      TEXT NOT NULL,
    firmware   TEXT,
    width      INTEGER NOT NULL,
    height     INTEGER NOT NULL,
    color      INTEGER NOT NULL DEFAULT 0,
    measured   INTEGER NOT NULL DEFAULT 1,
    learned_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pages (
    doc_id      TEXT NOT NULL,
    page_index  INTEGER NOT NULL,
    source_hash TEXT,
    render_hash TEXT,
    run_id      INTEGER REFERENCES runs(id),
    PRIMARY KEY (doc_id, page_index)
);
"""

#: Columns added to a table after its first release. ``CREATE TABLE IF NOT
#: EXISTS`` cannot add a column to a database that already has the table, so
#: every column introduced later has to be listed here as well as in
#: :data:`_SCHEMA`, and is applied with an ALTER on open.
_ADDED_COLUMNS = {
    "documents": {
        "last_error": "TEXT",
        "last_error_at": "TEXT",
    },
    "publications": {
        "target": "TEXT",
    },
    "device": {
        "measured": "INTEGER NOT NULL DEFAULT 1",
    },
}

#: How an older ``publications`` table reaches its current shape. Not an ALTER,
#: because the change that forces the rebuild is the primary key, and SQLite
#: cannot alter one in place. ``recipe`` and ``pages_failed`` ride along rather
#: than going through :data:`_ADDED_COLUMNS`: one rebuild that lands all three
#: is strictly less risk than a rebuild plus two ALTERs that must not race it.
#:
#: One statement per entry rather than one script, because
#: ``Cursor.executescript`` can commit an open transaction before it runs, and
#: the whole point here is that these five either all land or none do.
_REBUILD_PUBLICATIONS = (
    """
    CREATE TABLE publications_new (
        doc_id             TEXT NOT NULL,
        destination        TEXT NOT NULL,
        profile            TEXT NOT NULL DEFAULT 'default',
        version            TEXT,
        recipe             TEXT NOT NULL DEFAULT '',
        pages_failed       INTEGER NOT NULL DEFAULT 0,
        external_id        TEXT,
        target             TEXT,
        first_published_at TEXT NOT NULL,
        last_published_at  TEXT NOT NULL,
        run_id             INTEGER REFERENCES runs(id),
        PRIMARY KEY (doc_id, destination, profile)
    )
    """,
    # recipe takes its '' default for every carried-over row, so each one
    # mismatches any real digest and the first run on the new scheme
    # re-publishes exactly once, then settles. content_hash is not carried:
    # it was NULL in every row ever written.
    """
    INSERT INTO publications_new
        (doc_id, destination, profile, version, external_id, target,
         first_published_at, last_published_at, run_id)
    SELECT doc_id, destination, 'default', version, external_id, target,
           first_published_at, last_published_at, run_id
    FROM publications
    """,
    "DROP TABLE publications",
    "ALTER TABLE publications_new RENAME TO publications",
    "CREATE INDEX IF NOT EXISTS publications_by_destination ON publications (destination)",
)


@dataclass(frozen=True)
class DocumentView:
    """The facts a status predicate is allowed to look at.

    Deciding a document's status must be a pure function of facts already
    gathered — never a fresh read. A status that needs something nobody has
    yet is a reason to add a field here, gathered once in bulk, not a reason
    to let a predicate go to disk per document.

    Attributes:
        doc_id: The reMarkable document id.
        last_error: The error the last attempt ended with, if it was never
            followed by a success.
        published: Destination class name to the version it currently holds,
            including destinations that have since been disabled.
        pending: Enabled destination class names that do not hold the current
            version.
        destinations: The enabled destination class names, so that a document
            published only to a destination the user has since turned off is
            still judged against the ones that are on.
    """

    doc_id: str
    last_error: Optional[str]
    published: Dict[str, str]
    pending: List[str]
    destinations: List[str]

    @property
    def ever_published(self) -> bool:
        """Whether any currently enabled destination has ever held this document.

        Returns:
            True if at least one enabled destination has a publication record,
            regardless of which version it holds.
        """
        return any(name in self.published for name in self.destinations)


@dataclass(frozen=True)
class SyncStatus:
    """Where one document stands, as a value rather than a bare string.

    Attributes:
        key: The stable machine name, used in ``--json`` output. Renaming
            ``label`` must never change this.
        label: The words a user reads.
        tone: How to colour it — ``"good"``, ``"warn"``, ``"bad"`` or
            ``"muted"``. Named rather than a colour function so that this
            module stays free of presentation imports.
        needs_sync: Whether a run acts on a document in this state. The same
            field drives the preview and the pipeline's filter, so the two
            cannot disagree about what a sync is about to do.
        matches: Predicate over a :class:`DocumentView`.
    """

    key: str
    label: str
    tone: str
    needs_sync: bool
    matches: Callable[[DocumentView], bool]

    def __str__(self) -> str:
        """Return the human-readable label.

        Returns:
            The label, so an f-string prints words rather than a repr.
        """
        return self.label


#: The last attempt ended in an error that was never followed by a success.
STATUS_FAILED = SyncStatus("failed", "failed", "bad", True, lambda view: bool(view.last_error))

#: Owed to a destination that has never held it. Checked before ``changed``,
#: and only when something is actually owed: with no destinations enabled
#: nothing is pending, and "up to date" is the truthful answer rather than
#: calling the whole library new.
STATUS_NEW = SyncStatus(
    "new", "new", "warn", True, lambda view: bool(view.pending) and not view.ever_published
)

#: Published before; the tablet has moved on since.
STATUS_CHANGED = SyncStatus("changed", "changed", "warn", True, lambda view: bool(view.pending))

#: Every enabled destination holds the current version.
STATUS_UP_TO_DATE = SyncStatus("up_to_date", "up to date", "good", False, lambda view: True)

#: Every status, in classification order: the first whose predicate holds wins,
#: and the last matches everything. Order encodes a real decision — a document
#: that both errored and changed is reported as failed, because that is the one
#: worth acting on. Adding a status is one entry here; the summary counts, the
#: row renderer and the pipeline's filter all iterate this tuple rather than
#: naming statuses, so none of them needs editing.
SYNC_STATUSES: Tuple[SyncStatus, ...] = (
    STATUS_FAILED,
    STATUS_NEW,
    STATUS_CHANGED,
    STATUS_UP_TO_DATE,
)


def classify(view: DocumentView) -> SyncStatus:
    """Decide where one document stands.

    Args:
        view: The gathered facts about the document.

    Returns:
        The first status in :data:`SYNC_STATUSES` whose predicate holds.
        :data:`STATUS_UP_TO_DATE` matches everything, so this always returns.
    """
    return next(status for status in SYNC_STATUSES if status.matches(view))


def _now() -> str:
    """Return the current time as an ISO-8601 UTC string.

    Stored as text rather than a SQLite timestamp so the value is readable in
    a ``state --dump`` and unambiguous about its zone.

    Returns:
        Timestamp such as ``2026-09-17T14:03:11+00:00``.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StateStore:
    """Every durable fact Living Ink keeps between runs.

    The connection is opened once and shared. Writes are serialised with a
    lock so the OCR thread pool cannot interleave statements, and WAL mode
    means a concurrent ``watch`` daemon blocks briefly rather than clobbering.

    Attributes:
        path: Location of the database file.
    """

    def __init__(self, path: Path):
        """Open (and create, if needed) the state database.

        Args:
            path: Database file. Parent directories are created.
        """
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self.path),
            # The pipeline fans out OCR across threads; the guarantee we need
            # is the lock below, not a per-thread connection.
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self._migrate()

    def _configure(self) -> None:
        """Apply the connection pragmas this store depends on."""
        # WAL is the reason a daemon and a manual sync can coexist.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        # Rather than failing instantly when the other process holds the write
        # lock, wait for it; a sync takes minutes, five seconds is nothing.
        self._conn.execute("PRAGMA busy_timeout=5000")

    def _migrate(self) -> None:
        """Create or upgrade the schema to :data:`SCHEMA_VERSION`."""
        with self._lock:
            current = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if current > SCHEMA_VERSION:
                raise RuntimeError(
                    f"{self.path} was written by a newer Living Ink "
                    f"(schema {current}, this build understands {SCHEMA_VERSION})."
                )
            if current != SCHEMA_VERSION:
                self._conn.executescript(_SCHEMA)

            # Checked on every open, not only on a version change. A column
            # added to _ADDED_COLUMNS without a matching SCHEMA_VERSION bump
            # would otherwise never be applied, and the miss does not surface
            # until something writes to it — a live sync died on exactly that
            # ("table device has no column named measured"). Four PRAGMA
            # table_info calls are not worth a class of silent breakage.
            # After the ALTERs, so the SELECT below can name every column the
            # old table was supposed to have; before the version stamp, so a
            # crash mid-rebuild leaves the database claiming the old version
            # and the next open tries again.
            self._add_missing_columns()
            self._rebuild_publications()
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def _add_missing_columns(self) -> None:
        """Bring an older database's tables up to the current column list.

        Additive only. A column that is already there is left alone, so this
        is safe to run against a fresh database and against one written by any
        earlier version.
        """
        for table, columns in _ADDED_COLUMNS.items():
            present = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns.items():
                if name not in present:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def _rebuild_publications(self) -> None:
        """Move an old ``publications`` table onto the three-part primary key.

        A no-op on a database that already has ``profile``, which covers every
        fresh one: :data:`_SCHEMA` declares the current shape, so the rebuild
        only ever meets a table written by an earlier version.

        :meth:`_add_missing_columns` cannot do this. It issues
        ``ALTER TABLE ... ADD COLUMN`` and nothing else, so putting ``profile``
        through it would give the column with the *old* key still in force — a
        change that looks applied and is not, and the one shape this whole
        method exists to avoid.

        One transaction, so a crash halfway leaves the old table intact rather
        than a ``publications_new`` nobody reads and no ``publications`` at all.
        """
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(publications)")}
        if not columns or "profile" in columns:
            return

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _REBUILD_PUBLICATIONS:
                self._conn.execute(statement)
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def close(self) -> None:
        """Close the underlying connection."""
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "StateStore":
        """Return self, so the store can be used as a context manager."""
        return self

    def __exit__(self, *exc_info) -> None:
        """Close the connection on the way out."""
        self.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """Run a statement group as one locked, atomic transaction.

        Yields:
            The shared connection, inside a transaction.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    # --- runs -------------------------------------------------------------

    def start_run(self) -> int:
        """Open a run and return its id.

        Every row written afterwards carries this id, so a later question of
        the form "what did that failing sync at 03:00 actually touch" has an
        answer.

        Returns:
            The new run's primary key.
        """
        with self._write() as conn:
            cursor = conn.execute("INSERT INTO runs (started_at) VALUES (?)", (_now(),))
            return int(cursor.lastrowid)

    def finish_run(
        self,
        run_id: int,
        *,
        outcome: str,
        seen: int = 0,
        published: int = 0,
        failed: int = 0,
    ) -> None:
        """Close a run and record what it did.

        Args:
            run_id: Value returned by :meth:`start_run`.
            outcome: Short status word, e.g. ``"success"`` or ``"error"``.
            seen: Documents discovered on the device.
            published: Documents published to at least one destination.
            failed: Documents that failed.
        """
        with self._write() as conn:
            conn.execute(
                "UPDATE runs SET finished_at = ?, outcome = ?, documents_seen = ?, "
                "documents_published = ?, documents_failed = ? WHERE id = ?",
                (_now(), outcome, seen, published, failed, run_id),
            )

    def last_run(self) -> Optional[Dict[str, Any]]:
        """Return the most recently started run, if there is one.

        Returns:
            A dict of the run's columns, or None on a fresh install.
        """
        row = self._conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    # --- documents --------------------------------------------------------

    def record_document(
        self,
        doc_id: str,
        *,
        name: Optional[str] = None,
        folder: Optional[str] = None,
        doc_type: Optional[str] = None,
        version: Optional[Any] = None,
        last_modified: Optional[str] = None,
        run_id: Optional[int] = None,
    ) -> None:
        """Remember that a document exists on the device.

        An upsert rather than an insert: a document is seen again on every
        run, and only the mutable columns should move.

        Args:
            doc_id: reMarkable document id.
            name: Visible name.
            folder: Folder path on the device.
            doc_type: ``notebook``, ``pdf`` or ``epub``.
            version: Device version or content hash, whichever is available.
            last_modified: Device-side modification time, if known.
            run_id: Run that saw it.
        """
        with self._write() as conn:
            conn.execute(
                """
                INSERT INTO documents
                    (id, name, folder, doc_type, version, last_modified, seen_at, seen_run_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    folder = excluded.folder,
                    doc_type = excluded.doc_type,
                    version = excluded.version,
                    last_modified = excluded.last_modified,
                    seen_at = excluded.seen_at,
                    seen_run_id = excluded.seen_run_id
                """,
                (
                    doc_id,
                    name,
                    folder,
                    doc_type,
                    None if version is None else str(version),
                    last_modified,
                    _now(),
                    run_id,
                ),
            )

    def get_document(self, doc_id: str) -> Optional[Dict[str, Any]]:
        """Return a document row, if it has ever been seen.

        Args:
            doc_id: reMarkable document id.

        Returns:
            A dict of the document's columns, or None.
        """
        row = self._conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        return dict(row) if row else None

    def all_documents(self) -> List[Dict[str, Any]]:
        """Return every document ever seen, newest sighting first.

        Returns:
            A list of dicts of document columns.
        """
        rows = self._conn.execute("SELECT * FROM documents ORDER BY seen_at DESC").fetchall()
        return [dict(row) for row in rows]

    def record_failure(self, doc_id: str, message: str) -> None:
        """Remember that the last attempt at a document did not work.

        Kept on the document rather than in a log table because the only
        question anyone asks is "what is broken right now", and the previous
        failure stops mattering the moment a newer one replaces it.

        Args:
            doc_id: reMarkable document id.
            message: Short description of what went wrong.
        """
        with self._write() as conn:
            conn.execute(
                "UPDATE documents SET last_error = ?, last_error_at = ? WHERE id = ?",
                (message, _now(), doc_id),
            )

    def clear_failure(self, doc_id: str) -> None:
        """Forget a document's recorded failure after it succeeds.

        Args:
            doc_id: reMarkable document id.
        """
        with self._write() as conn:
            conn.execute(
                "UPDATE documents SET last_error = NULL, last_error_at = NULL WHERE id = ?",
                (doc_id,),
            )

    # --- publications -----------------------------------------------------

    def published_versions(self, destination: str) -> Dict[str, str]:
        """Return the published version of every document for one destination.

        The direct replacement for reading
        ``processed_notebooks_<destination>.json``.

        Args:
            destination: Destination class name, e.g. ``ObsidianDestination``.

        Returns:
            Mapping of document id to the version last published there.
        """
        rows = self._conn.execute(
            "SELECT doc_id, version FROM publications WHERE destination = ?",
            (destination,),
        ).fetchall()
        return {row["doc_id"]: row["version"] for row in rows}

    def get_publication(self, doc_id: str, destination: str) -> Optional[Dict[str, Any]]:
        """Return what is known about one document at one destination.

        Args:
            doc_id: reMarkable document id.
            destination: Destination class name.

        Returns:
            A dict of the publication's columns, or None if never published.
        """
        row = self._conn.execute(
            "SELECT * FROM publications WHERE doc_id = ? AND destination = ? AND profile = ?",
            (doc_id, destination, DEFAULT_PROFILE),
        ).fetchone()
        return dict(row) if row else None

    def record_publication(
        self,
        doc_id: str,
        destination: str,
        version: Any,
        *,
        recipe: str,
        pages_failed: int = 0,
        external_id: Optional[str] = None,
        target: Optional[str] = None,
        run_id: Optional[int] = None,
        published_at: Optional[str] = None,
    ) -> None:
        """Record that a document reached a destination.

        Merged on the natural key, so re-publishing updates rather than
        duplicating. ``first_published_at`` survives every update — it is the
        only record of when the note came into existence, and what makes the
        ``created`` date a destination writes stable across republishes.

        A row is assembled from two sides and half of it is not on
        :class:`~living_ink.core.document.PublishResult`. The destination
        supplies ``target`` and ``external_id``; the caller supplies
        ``version``, ``recipe`` and ``pages_failed``, because all three are
        facts about the run rather than about where the note landed — a
        destination has no way to know the tablet's content hash or how many
        pages the OCR stage lost.

        Args:
            doc_id: reMarkable document id.
            destination: The destination's ``state_key``.
            version: Device version or content hash that was published.
            recipe: Digest of everything other than the document that shaped
                the output (see :mod:`living_ink.core.recipe`). Required, and
                deliberately without a default: a row silently recorded with
                the empty default would never match a real digest again, so the
                document would republish on every run for ever.
            pages_failed: How many pages did not transcribe. A non-zero count
                keeps the document pending so the next run retries, while the
                pages that did work are already published.
            external_id: Identifier on the far side, where the destination has
                one (the id an API hands back, for instance).
            target: Where the note landed, in whatever terms the destination
                names its notes — a vault-relative path, a folder and title.
                Recorded so a later run can tell that a note has moved.
            run_id: Run that published it.
            published_at: Override the timestamp; for migration of old state.
        """
        stamp = published_at or _now()
        with self._write() as conn:
            conn.execute(
                """
                INSERT INTO publications
                    (doc_id, destination, profile, version, recipe, pages_failed,
                     external_id, target, first_published_at, last_published_at, run_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(doc_id, destination, profile) DO UPDATE SET
                    version = excluded.version,
                    recipe = excluded.recipe,
                    pages_failed = excluded.pages_failed,
                    -- Only overwrite these when the caller actually knows
                    -- them, so a partial record never erases a full one.
                    external_id = COALESCE(excluded.external_id, publications.external_id),
                    target = COALESCE(excluded.target, publications.target),
                    last_published_at = excluded.last_published_at,
                    run_id = excluded.run_id
                """,
                (
                    doc_id,
                    destination,
                    DEFAULT_PROFILE,
                    None if version is None else str(version),
                    recipe,
                    pages_failed,
                    external_id,
                    target,
                    stamp,
                    stamp,
                    run_id,
                ),
            )

    def forget(self, doc_id: str, destination: Optional[str] = None) -> int:
        """Drop publication records so a document syncs again.

        Forgetting a document everywhere also drops its page hashes and any
        recorded failure, because the point of asking is to start that
        document over from nothing. Forgetting one destination leaves both
        alone — the other destinations still rely on them.

        Nothing outside the database is touched. The note already published
        stays where it is; the next sync rewrites it.

        Args:
            doc_id: reMarkable document id.
            destination: Only forget this destination; all of them if None.

        Returns:
            Number of publication rows removed.
        """
        with self._write() as conn:
            if destination:
                cursor = conn.execute(
                    "DELETE FROM publications WHERE doc_id = ? AND destination = ?",
                    (doc_id, destination),
                )
                return cursor.rowcount

            cursor = conn.execute("DELETE FROM publications WHERE doc_id = ?", (doc_id,))
            conn.execute("DELETE FROM pages WHERE doc_id = ?", (doc_id,))
            conn.execute(
                "UPDATE documents SET last_error = NULL, last_error_at = NULL WHERE id = ?",
                (doc_id,),
            )
            return cursor.rowcount

    def forget_unknown_destinations(self, known: Iterable[str]) -> Dict[str, int]:
        """Drop publication rows filed under a destination that no longer exists.

        A deleted destination leaves its rows behind, and every later run reads
        them as real: the selection pass reports the document as still
        published somewhere, ``_prune_orphan`` declines to prune because the
        destination "is not configured", and ``sync --preview`` prints a dead
        key in each row's published map. None of that is recoverable by the
        user, because there is no longer any code that could unpublish.

        Enabled is not the question — *registered* is. A destination the user
        turned off keeps its rows, so turning it back on does not re-publish
        the whole library; only a destination the build no longer ships loses
        them.

        Args:
            known: Every :attr:`Destination.state_key` this build registers.

        Returns:
            Mapping of the destination name removed to how many rows it had,
            empty when there was nothing to forget.

        Raises:
            ValueError: If ``known`` is empty. No registered destination at all
                means the registry failed to populate, not that every
                destination retired at once, and the difference is the whole
                publication history.
        """
        keys = list(known)
        if not keys:
            raise ValueError(
                "refusing to forget every destination: no destination is registered, "
                "which is a failed import rather than a retirement"
            )

        placeholders = ",".join("?" * len(keys))
        with self._write() as conn:
            rows = conn.execute(
                f"SELECT destination, COUNT(*) AS n FROM publications "
                f"WHERE destination NOT IN ({placeholders}) GROUP BY destination",
                keys,
            ).fetchall()
            if not rows:
                return {}
            conn.execute(
                f"DELETE FROM publications WHERE destination NOT IN ({placeholders})",
                keys,
            )
        return {row["destination"]: row["n"] for row in rows}

    def all_publications(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """Return every publication row, grouped by document.

        One query instead of one per document, because the caller building an
        inventory needs all of them at once.

        Returns:
            Mapping of document id to {destination: publication columns}.
        """
        grouped: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for row in self._conn.execute("SELECT * FROM publications"):
            record = dict(row)
            grouped.setdefault(record["doc_id"], {})[record["destination"]] = record
        return grouped

    def sync_overview(self, destinations: List[str]) -> List[Dict[str, Any]]:
        """Describe where every known document stands against each destination.

        This is the join the old per-destination JSON files could not do: a
        document is only up to date once every destination that wants it holds
        its current version, and one destination lagging is enough to make it
        pending.

        Args:
            destinations: Destination class names that are currently enabled.
                A destination that was disabled since the last sync is ignored
                rather than counted as missing.

        Returns:
            One dict per document — its columns plus ``status`` (a
            :class:`SyncStatus`), ``pending`` (the destinations still owing it)
            and ``published`` (destination to the version it holds) — in the
            order :meth:`all_documents` returns them.
        """
        publications = self.all_publications()
        overview: List[Dict[str, Any]] = []

        for document in self.all_documents():
            published = {
                name: row["version"] for name, row in publications.get(document["id"], {}).items()
            }
            pending = [name for name in destinations if published.get(name) != document["version"]]

            status = classify(
                DocumentView(
                    doc_id=document["id"],
                    last_error=document.get("last_error"),
                    published=published,
                    pending=pending,
                    destinations=destinations,
                )
            )

            overview.append(
                {**document, "status": status, "pending": pending, "published": published}
            )

        return overview

    # --- device -----------------------------------------------------------

    def remember_device(self, info: "DeviceInfo") -> None:
        """Record the tablet this installation syncs with.

        Only USB SSH can see the hardware, and most runs are Cloud-only, so
        what a single USB session learns has to outlive it. The row is
        overwritten rather than appended: there is one tablet, and a later
        reading of it is better than an earlier one.

        Args:
            info: What the transport reported about the device.
        """
        with self._write() as conn:
            conn.execute(
                """
                INSERT INTO device
                    (id, model, firmware, width, height, color, measured, learned_at)
                VALUES (1, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    model = excluded.model,
                    firmware = excluded.firmware,
                    width = excluded.width,
                    height = excluded.height,
                    color = excluded.color,
                    measured = excluded.measured,
                    learned_at = excluded.learned_at
                """,
                (
                    info.model,
                    info.firmware,
                    info.screen[0],
                    info.screen[1],
                    int(info.color),
                    int(info.screen_measured),
                    _now(),
                ),
            )

    def recall_device(self) -> Optional[Tuple["DeviceInfo", str]]:
        """Return the tablet learned during some past USB session.

        Returns:
            The remembered device and the ISO timestamp it was learned at, or
            None if USB SSH has never connected. The timestamp is returned
            alongside so callers can say "remembered" rather than implying a
            live reading.
        """
        row = self._conn.execute("SELECT * FROM device WHERE id = 1").fetchone()
        if row is None:
            return None

        from living_ink.transport import DeviceInfo

        return (
            DeviceInfo(
                model=row["model"],
                firmware=row["firmware"] or "",
                screen=(row["width"], row["height"]),
                color=bool(row["color"]),
                screen_measured=bool(row["measured"]),
            ),
            row["learned_at"],
        )

    # --- pages ------------------------------------------------------------

    def record_page(
        self,
        doc_id: str,
        page_index: int,
        *,
        source_hash: Optional[str] = None,
        render_hash: Optional[str] = None,
        run_id: Optional[int] = None,
    ) -> None:
        """Remember what a single page looked like when it was processed.

        Args:
            doc_id: reMarkable document id.
            page_index: Zero-based page number.
            source_hash: Hash of the ``.rm`` source.
            render_hash: Hash of the rendered PNG.
            run_id: Run that processed it.
        """
        with self._write() as conn:
            conn.execute(
                """
                INSERT INTO pages (doc_id, page_index, source_hash, render_hash, run_id)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(doc_id, page_index) DO UPDATE SET
                    source_hash = excluded.source_hash,
                    render_hash = excluded.render_hash,
                    run_id = excluded.run_id
                """,
                (doc_id, page_index, source_hash, render_hash, run_id),
            )

    def get_pages(self, doc_id: str) -> Dict[int, Dict[str, Any]]:
        """Return everything known about a document's pages.

        Args:
            doc_id: reMarkable document id.

        Returns:
            Mapping of page index to that page's columns.
        """
        rows = self._conn.execute(
            "SELECT * FROM pages WHERE doc_id = ? ORDER BY page_index", (doc_id,)
        ).fetchall()
        return {row["page_index"]: dict(row) for row in rows}

    # --- maintenance ------------------------------------------------------

    def dump(self) -> Dict[str, List[Dict[str, Any]]]:
        """Return the whole database as plain dicts, for inspection.

        Returns:
            Mapping of table name to its rows.
        """
        tables = ("runs", "documents", "publications", "pages")
        return {
            table: [dict(row) for row in self._conn.execute(f"SELECT * FROM {table}").fetchall()]
            for table in tables
        }

    def integrity_check(self) -> str:
        """Ask SQLite whether the file is intact.

        Returns:
            ``"ok"`` when healthy, otherwise SQLite's description of the damage.
        """
        return str(self._conn.execute("PRAGMA integrity_check").fetchone()[0])

    def counts(self) -> Dict[str, int]:
        """Return the number of rows in each table.

        Returns:
            Mapping of table name to row count.
        """
        tables = ("runs", "documents", "publications", "pages")
        return {
            table: int(self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }

    def vacuum(self) -> None:
        """Rebuild the file, reclaiming space freed by deletes.

        Runs outside the usual write helper: SQLite refuses to VACUUM inside a
        transaction.
        """
        with self._lock:
            self._conn.execute("VACUUM")

    def find_documents(self, query: str) -> List[Dict[str, Any]]:
        """Find documents by id, name, or folder path.

        The same three things a user might type: the id from a ``--dump``, the
        name they see on the tablet, or ``Work/Meeting Notes``. Matching on
        name is case-insensitive because nobody remembers the capitalisation.

        Args:
            query: Id, visible name, or ``folder/name`` path.

        Returns:
            Matching document rows. An exact id match wins outright and is
            returned alone, so an id can never be ambiguous with a name.
        """
        exact = self.get_document(query)
        if exact:
            return [exact]

        needle = query.strip().lower().strip("/")
        matches = []
        for document in self.all_documents():
            name = (document["name"] or "").lower()
            folder = (document["folder"] or "").lower().strip("/")
            path = f"{folder}/{name}" if folder else name
            if needle in (name, path):
                matches.append(document)
        return matches


def import_legacy_json(store: StateStore, data_dir: Path) -> int:
    """Fold any ``processed_notebooks_*.json`` files into the database.

    Run once, on first open. Each imported file is renamed with a
    ``.migrated`` suffix rather than deleted, so a user who downgrades still
    has their state and an unexpected failure is recoverable.

    Timestamps are taken from the file's own mtime. It is not when the note
    was really first published, but it is an upper bound and a great deal
    better than pretending everything was published during the migration.

    Args:
        store: Open state store to write into.
        data_dir: Directory the JSON state files live in.

    Returns:
        Number of publication records imported.
    """
    data_dir = Path(data_dir)
    if not data_dir.is_dir():
        return 0

    imported = 0
    for path in sorted(data_dir.glob("processed_notebooks_*.json")):
        destination = path.stem[len("processed_notebooks_") :]
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A corrupt file was already treated as empty by the old loader;
            # leave it in place so the user can see it rather than renaming
            # away the evidence.
            continue

        if isinstance(raw, list):
            # The oldest format: a bare list of ids, with no version at all.
            raw = {doc_id: 0 for doc_id in raw}
        if not isinstance(raw, dict):
            continue

        stamp = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(
            timespec="seconds"
        )
        for doc_id, version in raw.items():
            # An empty recipe on purpose: the JSON never recorded one, so the
            # honest answer is "unknown", and unknown must read as pending.
            store.record_publication(
                str(doc_id), destination, version, recipe="", published_at=stamp, run_id=None
            )
            imported += 1

        path.rename(path.with_suffix(".json.migrated"))

    return imported
