"""Tests for the SQLite sync-state store."""

import json
import sqlite3
import threading

import pytest

from living_ink import state
from living_ink.state import (
    SCHEMA_VERSION,
    STATUS_CHANGED,
    STATUS_FAILED,
    STATUS_NEW,
    STATUS_UP_TO_DATE,
    SYNC_STATUSES,
    StateStore,
    classify,
    import_legacy_json,
)
from living_ink.transport import DeviceInfo


@pytest.fixture
def store(tmp_path):
    """An open store on a throwaway database."""
    with StateStore(tmp_path / "state.db") as opened:
        yield opened


class TestSchema:
    """The database describes its own version so it can be upgraded later."""

    def test_the_file_is_created(self, tmp_path):
        path = tmp_path / "nested" / "state.db"
        with StateStore(path):
            assert path.exists()

    def test_the_schema_version_is_stamped(self, store):
        assert store._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION

    def test_reopening_does_not_reset_anything(self, tmp_path):
        path = tmp_path / "state.db"
        with StateStore(path) as first:
            first.record_publication("doc-1", "Obsidian", "v1", recipe="")
        with StateStore(path) as second:
            assert second.published_versions("Obsidian") == {"doc-1": "v1"}

    def test_a_newer_schema_is_refused(self, tmp_path):
        """Downgrading Living Ink must not silently corrupt newer state."""
        path = tmp_path / "state.db"
        with StateStore(path):
            pass
        conn = sqlite3.connect(str(path))
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
        conn.close()

        with pytest.raises(RuntimeError, match="newer Living Ink"):
            StateStore(path)

    def test_the_database_is_intact(self, store):
        store.record_publication("doc-1", "Obsidian", "v1", recipe="")
        assert store.integrity_check() == "ok"

    def test_a_column_is_added_even_at_the_current_schema_version(self, tmp_path):
        """A live sync died on exactly this: the table existed, the column did not.

        A column added to _ADDED_COLUMNS without a SCHEMA_VERSION bump used to
        be skipped by the early return, and the miss only surfaced when
        something wrote to it.
        """
        path = tmp_path / "state.db"
        with StateStore(path) as first:
            first.remember_device(
                DeviceInfo("reMarkable 2", "3.20.0", (1404, 1872), screen_measured=False)
            )

        conn = sqlite3.connect(str(path))
        conn.execute("ALTER TABLE device DROP COLUMN measured")
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.close()

        with StateStore(path) as reopened:
            columns = {row["name"] for row in reopened._conn.execute("PRAGMA table_info(device)")}
            assert "measured" in columns
            # And the store is usable, not merely shaped right.
            reopened.remember_device(DeviceInfo("reMarkable Paper Pure", "3.28", (1404, 1872)))
            assert reopened.recall_device()[0].model == "reMarkable Paper Pure"

    def test_an_untouched_database_is_not_rewritten(self, tmp_path):
        """The per-open column check must not disturb existing rows."""
        path = tmp_path / "state.db"
        with StateStore(path) as first:
            first.record_publication("doc-1", "Obsidian", "v1", recipe="")

        with StateStore(path) as second:
            assert second.published_versions("Obsidian") == {"doc-1": "v1"}
            assert second.integrity_check() == "ok"


class TestPublications:
    """The table that replaces processed_notebooks_<destination>.json."""

    def test_a_publication_round_trips(self, store):
        store.record_publication("doc-1", "Obsidian", "v1", recipe="")
        assert store.published_versions("Obsidian") == {"doc-1": "v1"}

    def test_destinations_are_independent(self, store):
        store.record_publication("doc-1", "Obsidian", "v1", recipe="")
        assert store.published_versions("NotionDestination") == {}

    def test_republishing_merges_on_the_natural_key(self, store):
        store.record_publication("doc-1", "Obsidian", "v1", recipe="")
        store.record_publication("doc-1", "Obsidian", "v2", recipe="")

        assert store.published_versions("Obsidian") == {"doc-1": "v2"}
        rows = store.dump()["publications"]
        assert len(rows) == 1

    def test_the_first_publication_time_is_never_overwritten(self, store):
        """It is the only truthful source for a note's `created` date."""
        store.record_publication(
            "doc-1", "Obsidian", "v1", published_at="2020-01-01T00:00:00+00:00", recipe=""
        )
        store.record_publication("doc-1", "Obsidian", "v2", recipe="")

        record = store.get_publication("doc-1", "Obsidian")
        assert record["first_published_at"] == "2020-01-01T00:00:00+00:00"
        assert record["last_published_at"] != "2020-01-01T00:00:00+00:00"

    def test_an_external_id_survives_an_update_that_omits_it(self, store):
        """Most republish calls do not know the far-side id; they must not erase it."""
        store.record_publication("doc-1", "Notion", "v1", external_id="note-42", recipe="")
        store.record_publication("doc-1", "Notion", "v2", recipe="")

        assert store.get_publication("doc-1", "Notion")["external_id"] == "note-42"

    def test_the_recipe_is_recorded_beside_the_version(self, store):
        store.record_publication("doc-1", "Obsidian", "v1", recipe="abc123")
        assert store.get_publication("doc-1", "Obsidian")["recipe"] == "abc123"

    def test_a_republish_overwrites_the_recipe(self, store):
        """Unlike external_id, the recipe is always known and always current."""
        store.record_publication("doc-1", "Obsidian", "v1", recipe="abc123")
        store.record_publication("doc-1", "Obsidian", "v1", recipe="def456")

        assert store.get_publication("doc-1", "Obsidian")["recipe"] == "def456"

    def test_the_recipe_has_no_default(self):
        """A row silently recorded as '' would republish for ever."""
        with pytest.raises(TypeError):
            StateStore.record_publication(None, "doc-1", "Obsidian", "v1")

    def test_pages_failed_defaults_to_none_lost(self, store):
        store.record_publication("doc-1", "Obsidian", "v1", recipe="r")
        assert store.get_publication("doc-1", "Obsidian")["pages_failed"] == 0

    def test_pages_failed_is_recorded_and_cleared(self, store):
        """A partial publish is recorded; the retry that completes it clears it."""
        store.record_publication("doc-1", "Obsidian", "v1", recipe="r", pages_failed=3)
        assert store.get_publication("doc-1", "Obsidian")["pages_failed"] == 3

        store.record_publication("doc-1", "Obsidian", "v1", recipe="r")
        assert store.get_publication("doc-1", "Obsidian")["pages_failed"] == 0

    def test_the_content_hash_column_is_gone(self, store):
        """It was NULL in every row ever written, and named for the live column's job."""
        columns = {row["name"] for row in store._conn.execute("PRAGMA table_info(publications)")}
        assert "content_hash" not in columns
        assert {"recipe", "pages_failed", "profile"} <= columns

    def test_versions_are_stored_as_text(self, store):
        """Device versions are integers, cloud hashes are strings; comparison is textual."""
        store.record_publication("doc-1", "Obsidian", 7, recipe="")
        assert store.published_versions("Obsidian") == {"doc-1": "7"}

    def test_an_unknown_publication_is_none(self, store):
        assert store.get_publication("nope", "Obsidian") is None

    def test_forgetting_one_destination(self, store):
        store.record_publication("doc-1", "Obsidian", "v1", recipe="")
        store.record_publication("doc-1", "Notion", "v1", recipe="")

        assert store.forget("doc-1", "Obsidian") == 1
        assert store.published_versions("Obsidian") == {}
        assert store.published_versions("Notion") == {"doc-1": "v1"}

    def test_forgetting_every_destination(self, store):
        store.record_publication("doc-1", "Obsidian", "v1", recipe="")
        store.record_publication("doc-1", "Notion", "v1", recipe="")

        assert store.forget("doc-1") == 2
        assert store.dump()["publications"] == []


class TestRuns:
    """Every row carries the run that wrote it, so a sync can be traced."""

    def test_a_run_gets_an_id(self, store):
        assert isinstance(store.start_run(), int)

    def test_runs_are_distinct(self, store):
        assert store.start_run() != store.start_run()

    def test_finishing_records_the_counts(self, store):
        run_id = store.start_run()
        store.finish_run(run_id, outcome="success", seen=47, published=3, failed=1)

        run = store.last_run()
        assert (run["outcome"], run["documents_seen"], run["documents_published"]) == (
            "success",
            47,
            3,
        )
        assert run["documents_failed"] == 1
        assert run["finished_at"] is not None

    def test_an_unfinished_run_has_no_end(self, store):
        store.start_run()
        assert store.last_run()["finished_at"] is None

    def test_no_runs_yet(self, store):
        assert store.last_run() is None

    def test_a_publication_remembers_its_run(self, store):
        run_id = store.start_run()
        store.record_publication("doc-1", "Obsidian", "v1", run_id=run_id, recipe="")
        assert store.get_publication("doc-1", "Obsidian")["run_id"] == run_id


class TestDocuments:
    """An inventory of the device, so `what is pending` needs no tablet."""

    def test_a_document_round_trips(self, store):
        store.record_document(
            "doc-1", name="Meeting Notes", folder="Work", doc_type="notebook", version=3
        )
        record = store.get_document("doc-1")
        assert (record["name"], record["folder"], record["doc_type"]) == (
            "Meeting Notes",
            "Work",
            "notebook",
        )
        assert record["version"] == "3"

    def test_seeing_it_again_updates_rather_than_duplicates(self, store):
        store.record_document("doc-1", name="Draft", version=1)
        store.record_document("doc-1", name="Final", version=2)

        assert len(store.all_documents()) == 1
        assert store.get_document("doc-1")["name"] == "Final"

    def test_an_unknown_document_is_none(self, store):
        assert store.get_document("nope") is None

    def test_the_last_modified_time_is_kept(self, store):
        """The device-side timestamp a published note's `updated` field needs."""
        store.record_document("doc-1", last_modified="2026-09-01T10:00:00+00:00")
        assert store.get_document("doc-1")["last_modified"] == "2026-09-01T10:00:00+00:00"


class TestPages:
    """Per-page hashes, so a later run can skip pages that did not change."""

    def test_a_page_round_trips(self, store):
        store.record_page("doc-1", 0, source_hash="aaa", render_hash="bbb")
        assert store.get_pages("doc-1")[0]["source_hash"] == "aaa"

    def test_pages_come_back_in_order(self, store):
        for index in (2, 0, 1):
            store.record_page("doc-1", index)
        assert list(store.get_pages("doc-1")) == [0, 1, 2]

    def test_reprocessing_a_page_updates_it(self, store):
        store.record_page("doc-1", 0, render_hash="old")
        store.record_page("doc-1", 0, render_hash="new")

        pages = store.get_pages("doc-1")
        assert len(pages) == 1
        assert pages[0]["render_hash"] == "new"


class TestConcurrency:
    """A `watch` daemon and a manual sync both write; neither may lose the other."""

    def test_two_connections_both_land(self, tmp_path):
        path = tmp_path / "state.db"
        with StateStore(path) as first, StateStore(path) as second:
            first.record_publication("doc-1", "Obsidian", "v1", recipe="")
            second.record_publication("doc-2", "Obsidian", "v2", recipe="")

            assert first.published_versions("Obsidian") == {"doc-1": "v1", "doc-2": "v2"}

    def test_threads_do_not_interleave_statements(self, store):
        """OCR fans out across threads; the shared connection needs the lock."""

        def write(start):
            for index in range(start, start + 25):
                store.record_publication(f"doc-{index}", "Obsidian", "v1", recipe="")

        threads = [threading.Thread(target=write, args=(base,)) for base in (0, 100, 200)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(store.published_versions("Obsidian")) == 75

    def test_a_failed_write_rolls_back(self, store):
        store.record_publication("doc-1", "Obsidian", "v1", recipe="")
        with pytest.raises(sqlite3.Error):
            with store._write() as conn:
                conn.execute("DELETE FROM publications")
                conn.execute("SELECT * FROM no_such_table")

        assert store.published_versions("Obsidian") == {"doc-1": "v1"}


class TestLegacyImport:
    """Upgrading must not re-OCR every notebook the user already paid for."""

    def _write(self, tmp_path, name, payload):
        path = tmp_path / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_a_version_map_is_imported(self, store, tmp_path):
        self._write(tmp_path, "processed_notebooks_ObsidianDestination.json", {"doc-1": 4})

        assert import_legacy_json(store, tmp_path) == 1
        assert store.published_versions("ObsidianDestination") == {"doc-1": "4"}

    def test_the_oldest_bare_list_format_is_imported(self, store, tmp_path):
        self._write(tmp_path, "processed_notebooks_Obsidian.json", ["doc-1", "doc-2"])

        import_legacy_json(store, tmp_path)
        assert store.published_versions("Obsidian") == {"doc-1": "0", "doc-2": "0"}

    def test_each_file_becomes_its_own_destination(self, store, tmp_path):
        self._write(tmp_path, "processed_notebooks_Obsidian.json", {"doc-1": 1})
        self._write(tmp_path, "processed_notebooks_Notion.json", {"doc-2": 2})

        assert import_legacy_json(store, tmp_path) == 2
        assert store.published_versions("Obsidian") == {"doc-1": "1"}
        assert store.published_versions("Notion") == {"doc-2": "2"}

    def test_the_source_file_is_renamed_not_deleted(self, store, tmp_path):
        path = self._write(tmp_path, "processed_notebooks_Obsidian.json", {"doc-1": 1})

        import_legacy_json(store, tmp_path)
        assert not path.exists()
        assert (tmp_path / "processed_notebooks_Obsidian.json.migrated").exists()

    def test_importing_twice_is_a_no_op(self, store, tmp_path):
        self._write(tmp_path, "processed_notebooks_Obsidian.json", {"doc-1": 1})

        import_legacy_json(store, tmp_path)
        assert import_legacy_json(store, tmp_path) == 0

    def test_a_corrupt_file_is_left_alone(self, store, tmp_path):
        """Renaming it away would hide the evidence from the user."""
        path = tmp_path / "processed_notebooks_Obsidian.json"
        path.write_text("{not json", encoding="utf-8")

        assert import_legacy_json(store, tmp_path) == 0
        assert path.exists()

    def test_the_import_timestamp_is_the_file_mtime(self, store, tmp_path):
        """Better than claiming everything was first published during the upgrade."""
        import os

        path = self._write(tmp_path, "processed_notebooks_Obsidian.json", {"doc-1": 1})
        os.utime(path, (1_600_000_000, 1_600_000_000))

        import_legacy_json(store, tmp_path)
        assert store.get_publication("doc-1", "Obsidian")["first_published_at"].startswith("2020-")

    def test_a_missing_directory_is_harmless(self, store, tmp_path):
        assert import_legacy_json(store, tmp_path / "nope") == 0


class TestUpgradingAnOlderDatabase:
    """A database written before a column existed has to gain it, not break."""

    def _v1_database(self, path):
        """Build a database with the version-1 documents table."""
        conn = sqlite3.connect(str(path))
        conn.executescript(
            """
            CREATE TABLE documents (
                id            TEXT PRIMARY KEY,
                name          TEXT,
                folder        TEXT,
                doc_type      TEXT,
                version       TEXT,
                last_modified TEXT,
                seen_at       TEXT NOT NULL,
                seen_run_id   INTEGER
            );
            INSERT INTO documents (id, name, seen_at) VALUES ('doc-1', 'Old', '2020-01-01');
            PRAGMA user_version=1;
            """
        )
        conn.commit()
        conn.close()

    def test_the_new_columns_are_added(self, tmp_path):
        path = tmp_path / "state.db"
        self._v1_database(path)

        with StateStore(path) as store:
            columns = {row["name"] for row in store._conn.execute("PRAGMA table_info(documents)")}

        assert {"last_error", "last_error_at"} <= columns

    def test_a_publications_table_without_target_gains_it(self, tmp_path):
        """The column arrived after the table shipped, so it needs an ALTER."""
        path = tmp_path / "state.db"
        conn = sqlite3.connect(str(path))
        conn.executescript(
            """
            CREATE TABLE publications (
                doc_id             TEXT NOT NULL,
                destination        TEXT NOT NULL,
                version            TEXT,
                external_id        TEXT,
                content_hash       TEXT,
                first_published_at TEXT NOT NULL,
                last_published_at  TEXT NOT NULL,
                run_id             INTEGER,
                PRIMARY KEY (doc_id, destination)
            );
            INSERT INTO publications
                (doc_id, destination, first_published_at, last_published_at)
            VALUES ('doc-1', 'Obsidian', '2020-01-01', '2020-01-01');
            PRAGMA user_version=1;
            """
        )
        conn.commit()
        conn.close()

        with StateStore(path) as store:
            store.record_publication("doc-1", "Obsidian", "v2", target="Work/Notes.md", recipe="")
            assert store.get_publication("doc-1", "Obsidian")["target"] == "Work/Notes.md"

    def test_existing_rows_survive_the_upgrade(self, tmp_path):
        path = tmp_path / "state.db"
        self._v1_database(path)

        with StateStore(path) as store:
            assert store.get_document("doc-1")["name"] == "Old"

    def test_the_version_is_restamped(self, tmp_path):
        path = tmp_path / "state.db"
        self._v1_database(path)

        with StateStore(path) as store:
            assert store._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION

    def test_upgrading_twice_is_harmless(self, tmp_path):
        path = tmp_path / "state.db"
        self._v1_database(path)

        with StateStore(path):
            pass
        with StateStore(path) as store:
            store.record_failure("doc-1", "boom")
            assert store.get_document("doc-1")["last_error"] == "boom"


class TestRebuildingPublications:
    """The one change here that is not a column addition.

    SQLite cannot alter a primary key in place, so ``profile`` cannot arrive
    through ``_ADDED_COLUMNS`` — that would give the column with the old key
    still in force, which looks applied and is not. These cover the rebuild
    that replaces it, and the rows it has to carry across intact.
    """

    def _old_database(self, path, rows=()):
        """Build a database with the pre-rebuild publications table."""
        conn = sqlite3.connect(str(path))
        conn.executescript(
            """
            CREATE TABLE publications (
                doc_id             TEXT NOT NULL,
                destination        TEXT NOT NULL,
                version            TEXT,
                external_id        TEXT,
                target             TEXT,
                content_hash       TEXT,
                first_published_at TEXT NOT NULL,
                last_published_at  TEXT NOT NULL,
                run_id             INTEGER,
                PRIMARY KEY (doc_id, destination)
            );
            PRAGMA user_version=4;
            """
        )
        for row in rows:
            conn.execute(
                "INSERT INTO publications (doc_id, destination, version, external_id, target,"
                " content_hash, first_published_at, last_published_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
        conn.commit()
        conn.close()

    def test_the_primary_key_gains_the_profile(self, tmp_path):
        path = tmp_path / "state.db"
        self._old_database(path)

        with StateStore(path) as store:
            key = [
                row["name"]
                for row in store._conn.execute("PRAGMA table_info(publications)")
                if row["pk"]
            ]
        assert key == ["doc_id", "destination", "profile"]

    def test_every_row_is_carried_across(self, tmp_path):
        path = tmp_path / "state.db"
        self._old_database(
            path,
            [
                ("doc-1", "Obsidian", "v1", "x-1", "Work/Notes.md", None, "2020-01-01", "2021-0"),
                ("doc-2", "Obsidian", "v2", None, None, None, "2020-02-02", "2021-0"),
            ],
        )

        with StateStore(path) as store:
            first = store.get_publication("doc-1", "Obsidian")
            assert first["version"] == "v1"
            assert first["external_id"] == "x-1"
            assert first["target"] == "Work/Notes.md"
            assert first["first_published_at"] == "2020-01-01"
            assert store.get_publication("doc-2", "Obsidian")["version"] == "v2"

    def test_a_carried_row_is_pending_exactly_once(self, tmp_path):
        """'' mismatches any real digest, so the first 1.0 run self-heals."""
        path = tmp_path / "state.db"
        self._old_database(path, [("doc-1", "Obsidian", "v1", None, None, None, "2020", "2020")])

        with StateStore(path) as store:
            assert store.get_publication("doc-1", "Obsidian")["recipe"] == ""
            store.record_publication("doc-1", "Obsidian", "v1", recipe="real")
            assert store.get_publication("doc-1", "Obsidian")["recipe"] == "real"

    def test_the_destination_index_is_rebuilt_too(self, tmp_path):
        """DROP TABLE takes its indexes with it."""
        path = tmp_path / "state.db"
        self._old_database(path)

        with StateStore(path) as store:
            names = {
                row["name"]
                for row in store._conn.execute("PRAGMA index_list(publications)")
                if row["name"] == "publications_by_destination"
            }
        assert names == {"publications_by_destination"}

    def test_a_rebuilt_database_is_not_rebuilt_again(self, tmp_path):
        path = tmp_path / "state.db"
        self._old_database(path, [("doc-1", "Obsidian", "v1", None, None, None, "2020", "2020")])

        with StateStore(path) as store:
            store.record_publication("doc-1", "Obsidian", "v2", recipe="kept")
        with StateStore(path) as store:
            assert store.get_publication("doc-1", "Obsidian")["recipe"] == "kept"

    def test_a_failed_rebuild_leaves_the_old_table_intact(self, tmp_path, monkeypatch):
        """One transaction: a crash halfway must not lose the table."""
        path = tmp_path / "state.db"
        self._old_database(path, [("doc-1", "Obsidian", "v1", None, None, None, "2020", "2020")])

        broken = list(state._REBUILD_PUBLICATIONS[:2]) + ["THIS IS NOT SQL"]
        monkeypatch.setattr(state, "_REBUILD_PUBLICATIONS", tuple(broken))
        with pytest.raises(sqlite3.Error):
            StateStore(path)

        conn = sqlite3.connect(str(path))
        try:
            rows = conn.execute("SELECT doc_id, version FROM publications").fetchall()
            assert rows == [("doc-1", "v1")]
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        finally:
            conn.close()


class TestPublicationTargets:
    """Where a note landed is recorded, so a later run can tell it has moved."""

    def test_the_target_is_stored(self, store):
        store.record_publication("doc-1", "Obsidian", "v1", target="Work/Notes.md", recipe="")
        assert store.get_publication("doc-1", "Obsidian")["target"] == "Work/Notes.md"

    def test_republishing_elsewhere_updates_the_target(self, store):
        store.record_publication("doc-1", "Obsidian", "v1", target="Work/Notes.md", recipe="")
        store.record_publication("doc-1", "Obsidian", "v2", target="Archive/Notes.md", recipe="")
        assert store.get_publication("doc-1", "Obsidian")["target"] == "Archive/Notes.md"

    def test_a_caller_that_does_not_know_the_target_does_not_erase_it(self, store):
        """A partial record must never turn a known location into an unknown one."""
        store.record_publication("doc-1", "Obsidian", "v1", target="Work/Notes.md", recipe="")
        store.record_publication("doc-1", "Obsidian", "v2", recipe="")
        assert store.get_publication("doc-1", "Obsidian")["target"] == "Work/Notes.md"

    def test_each_destination_keeps_its_own_target(self, store):
        store.record_publication("doc-1", "Obsidian", "v1", target="Work/Notes.md", recipe="")
        store.record_publication("doc-1", "Notion", "v1", target="Inbox/Notes", recipe="")
        assert store.get_publication("doc-1", "Obsidian")["target"] == "Work/Notes.md"
        assert store.get_publication("doc-1", "Notion")["target"] == "Inbox/Notes"


class TestFailures:
    """A document that broke has to stay visibly broken until it works."""

    def test_a_failure_is_recorded(self, store):
        store.record_document("doc-1", name="Notes")
        store.record_failure("doc-1", "Download timed out")

        row = store.get_document("doc-1")
        assert row["last_error"] == "Download timed out"
        assert row["last_error_at"]

    def test_a_newer_failure_replaces_the_old_one(self, store):
        store.record_document("doc-1")
        store.record_failure("doc-1", "first")
        store.record_failure("doc-1", "second")

        assert store.get_document("doc-1")["last_error"] == "second"

    def test_success_clears_it(self, store):
        store.record_document("doc-1")
        store.record_failure("doc-1", "boom")
        store.clear_failure("doc-1")

        row = store.get_document("doc-1")
        assert row["last_error"] is None
        assert row["last_error_at"] is None

    def test_seeing_the_document_again_does_not_clear_it(self, store):
        """A new sighting is not evidence the problem went away."""
        store.record_document("doc-1", version="v1")
        store.record_failure("doc-1", "boom")
        store.record_document("doc-1", version="v2")

        assert store.get_document("doc-1")["last_error"] == "boom"

    def test_clearing_an_unknown_document_is_silent(self, store):
        store.clear_failure("never-seen")


class TestStatusRegistry:
    """A status is a value in an ordered registry, not a bare string."""

    def _view(self, **overrides):
        from living_ink.state import DocumentView

        defaults = {
            "doc_id": "doc-1",
            "last_error": None,
            "published": {},
            "pending": [],
            "destinations": [],
        }
        return DocumentView(**{**defaults, **overrides})

    def test_the_last_status_matches_everything(self):
        """classify() must always return; the fallback carries that guarantee."""
        assert SYNC_STATUSES[-1].matches(self._view()) is True

    def test_classify_returns_the_first_match_not_the_best(self):
        """Order is the rule: a document that both errored and changed is failed."""
        view = self._view(
            last_error="boom", pending=["ObsidianDestination"], destinations=["ObsidianDestination"]
        )
        assert classify(view) is STATUS_FAILED

    def test_new_is_checked_before_changed(self):
        view = self._view(pending=["ObsidianDestination"], destinations=["ObsidianDestination"])
        assert classify(view) is STATUS_NEW

    def test_a_document_owed_to_a_second_destination_is_changed_not_new(self):
        view = self._view(
            published={"ObsidianDestination": "v1"},
            pending=["NotionDestination"],
            destinations=["ObsidianDestination", "NotionDestination"],
        )
        assert classify(view) is STATUS_CHANGED

    def test_a_disabled_destination_does_not_make_a_document_look_synced_before(self):
        """Published only to a destination since turned off: new to the ones on."""
        view = self._view(
            published={"NotionDestination": "v1"},
            pending=["ObsidianDestination"],
            destinations=["ObsidianDestination"],
        )
        assert classify(view) is STATUS_NEW

    def test_no_destinations_enabled_is_up_to_date_not_new(self):
        """Nothing is owed, so nothing is outstanding — do not alarm the user."""
        assert classify(self._view()) is STATUS_UP_TO_DATE

    def test_every_status_key_is_unique(self):
        keys = [status.key for status in SYNC_STATUSES]
        assert len(keys) == len(set(keys))

    def test_only_the_up_to_date_status_is_skipped_by_a_run(self):
        skipped = [status for status in SYNC_STATUSES if not status.needs_sync]
        assert skipped == [STATUS_UP_TO_DATE]

    def test_a_status_prints_as_its_label(self):
        assert f"{STATUS_UP_TO_DATE}" == "up to date"

    def test_the_key_is_machine_stable_and_the_label_is_not(self):
        """--json consumers key off `key`; renaming the label must not move it."""
        assert STATUS_UP_TO_DATE.key == "up_to_date"
        assert " " in STATUS_UP_TO_DATE.label


class TestSyncOverview:
    """The join the per-destination JSON files could never do."""

    def test_a_document_published_everywhere_is_up_to_date(self, store):
        store.record_document("doc-1", name="Notes", version="v1")
        store.record_publication("doc-1", "ObsidianDestination", "v1", recipe="")
        store.record_publication("doc-1", "NotionDestination", "v1", recipe="")

        row = store.sync_overview(["ObsidianDestination", "NotionDestination"])[0]
        assert row["status"] is STATUS_UP_TO_DATE
        assert row["pending"] == []

    def test_one_lagging_destination_makes_it_changed(self, store):
        store.record_document("doc-1", version="v2")
        store.record_publication("doc-1", "ObsidianDestination", "v2", recipe="")
        store.record_publication("doc-1", "NotionDestination", "v1", recipe="")

        row = store.sync_overview(["ObsidianDestination", "NotionDestination"])[0]
        assert row["status"] is STATUS_CHANGED
        assert row["pending"] == ["NotionDestination"]

    def test_a_never_published_document_is_new(self, store):
        """Never synced and merely stale are different bills; do not conflate."""
        store.record_document("doc-1", version="v1")

        row = store.sync_overview(["ObsidianDestination"])[0]
        assert row["status"] is STATUS_NEW
        assert row["pending"] == ["ObsidianDestination"]

    def test_a_disabled_destination_is_not_counted_as_missing(self, store):
        """Turning a destination off must not make the whole library pending."""
        store.record_document("doc-1", version="v1")
        store.record_publication("doc-1", "ObsidianDestination", "v1", recipe="")

        row = store.sync_overview(["ObsidianDestination"])[0]
        assert row["status"] is STATUS_UP_TO_DATE

    def test_a_failure_outranks_being_up_to_date(self, store):
        store.record_document("doc-1", version="v1")
        store.record_publication("doc-1", "ObsidianDestination", "v1", recipe="")
        store.record_failure("doc-1", "boom")

        assert store.sync_overview(["ObsidianDestination"])[0]["status"] is STATUS_FAILED

    def test_published_versions_are_reported_per_destination(self, store):
        store.record_document("doc-1", version="v2")
        store.record_publication("doc-1", "ObsidianDestination", "v2", recipe="")

        row = store.sync_overview(["ObsidianDestination"])[0]
        assert row["published"] == {"ObsidianDestination": "v2"}

    def test_document_fields_are_carried_through(self, store):
        store.record_document("doc-1", name="Standup", folder="Work", doc_type="notebook")

        row = store.sync_overview([])[0]
        assert (row["name"], row["folder"], row["doc_type"]) == ("Standup", "Work", "notebook")

    def test_an_empty_database_returns_nothing(self, store):
        assert store.sync_overview(["ObsidianDestination"]) == []

    def test_every_document_appears_once(self, store):
        for index in range(3):
            store.record_document(f"doc-{index}", version="v1")
            store.record_publication(f"doc-{index}", "ObsidianDestination", "v1", recipe="")

        overview = store.sync_overview(["ObsidianDestination"])
        assert len({row["id"] for row in overview}) == 3


class TestAllPublications:
    """One query, because the inventory needs all of them at once."""

    def test_rows_are_grouped_by_document(self, store):
        store.record_publication("doc-1", "ObsidianDestination", "v1", recipe="")
        store.record_publication("doc-1", "NotionDestination", "v1", recipe="")
        store.record_publication("doc-2", "ObsidianDestination", "v3", recipe="")

        grouped = store.all_publications()
        assert set(grouped) == {"doc-1", "doc-2"}
        assert set(grouped["doc-1"]) == {"ObsidianDestination", "NotionDestination"}

    def test_an_empty_database_returns_nothing(self, store):
        assert store.all_publications() == {}


class TestFindDocuments:
    """A user types a name; the database is keyed by an opaque id."""

    @pytest.fixture
    def populated(self, store):
        """Three documents, two of them sharing a name in different folders."""
        store.record_document("id-1", name="Journal", folder="Personal")
        store.record_document("id-2", name="Notes", folder="Work")
        store.record_document("id-3", name="Notes", folder="Home")
        return store

    def test_an_id_matches(self, populated):
        assert [row["id"] for row in populated.find_documents("id-1")] == ["id-1"]

    def test_a_name_matches(self, populated):
        assert [row["id"] for row in populated.find_documents("Journal")] == ["id-1"]

    def test_a_name_is_case_insensitive(self, populated):
        assert [row["id"] for row in populated.find_documents("journal")] == ["id-1"]

    def test_a_folder_path_matches(self, populated):
        assert [row["id"] for row in populated.find_documents("Work/Notes")] == ["id-2"]

    def test_an_ambiguous_name_returns_every_candidate(self, populated):
        assert len(populated.find_documents("Notes")) == 2

    def test_an_id_is_never_ambiguous_with_a_name(self, store):
        """A document named after another's id must not shadow it."""
        store.record_document("id-1", name="Journal")
        store.record_document("id-2", name="id-1")

        assert [row["id"] for row in store.find_documents("id-1")] == ["id-1"]

    def test_an_unknown_query_matches_nothing(self, populated):
        assert populated.find_documents("nope") == []

    def test_a_partial_name_does_not_match(self, populated):
        """Forgetting the wrong notebook costs a full re-OCR; require the name."""
        assert populated.find_documents("Jour") == []


class TestForgetting:
    """Starting a document over means dropping everything derived from it."""

    def test_publications_are_removed(self, store):
        store.record_document("doc-1")
        store.record_publication("doc-1", "ObsidianDestination", "v1", recipe="")

        assert store.forget("doc-1") == 1
        assert store.published_versions("ObsidianDestination") == {}

    def test_page_hashes_go_too(self, store):
        store.record_document("doc-1")
        store.record_page("doc-1", 0, source_hash="abc")

        store.forget("doc-1")

        assert store.get_pages("doc-1") == {}

    def test_a_recorded_failure_is_cleared(self, store):
        store.record_document("doc-1")
        store.record_failure("doc-1", "boom")

        store.forget("doc-1")

        assert store.get_document("doc-1")["last_error"] is None

    def test_the_document_itself_is_kept(self, store):
        """It is still on the tablet; the next sync will see it again."""
        store.record_document("doc-1", name="Notes")
        store.forget("doc-1")

        assert store.get_document("doc-1")["name"] == "Notes"

    def test_forgetting_one_destination_leaves_the_others(self, store):
        store.record_document("doc-1")
        store.record_publication("doc-1", "ObsidianDestination", "v1", recipe="")
        store.record_publication("doc-1", "NotionDestination", "v1", recipe="")

        store.forget("doc-1", "ObsidianDestination")

        assert store.published_versions("NotionDestination") == {"doc-1": "v1"}

    def test_forgetting_one_destination_keeps_the_page_hashes(self, store):
        """Other destinations still rely on them."""
        store.record_document("doc-1")
        store.record_page("doc-1", 0, source_hash="abc")

        store.forget("doc-1", "ObsidianDestination")

        assert store.get_pages("doc-1")[0]["source_hash"] == "abc"


class TestForgettingADestinationThatIsGone:
    """Deleting a destination has to take its publication rows with it.

    Apple Notes was deleted in 1.0 and its rows were not. Every later run read
    them as real: the document looked published somewhere, ``--prune`` declined
    because the destination "is not configured", and ``sync --status`` printed
    a key no code could act on. Nothing the user could type would clear it.
    """

    def test_a_destination_that_no_longer_exists_loses_its_rows(self, store):
        store.record_publication("doc-1", "ObsidianDestination", "v1", recipe="")
        store.record_publication("doc-1", "AppleNotesDestination", "v1", recipe="")

        assert store.forget_unknown_destinations(["ObsidianDestination"]) == {
            "AppleNotesDestination": 1
        }
        assert store.published_versions("AppleNotesDestination") == {}

    def test_the_destinations_that_remain_are_untouched(self, store):
        store.record_publication("doc-1", "ObsidianDestination", "v1", recipe="")
        store.record_publication("doc-1", "AppleNotesDestination", "v1", recipe="")

        store.forget_unknown_destinations(["ObsidianDestination"])

        assert store.published_versions("ObsidianDestination") == {"doc-1": "v1"}

    def test_it_counts_every_document_the_dead_destination_held(self, store):
        for doc in ("doc-1", "doc-2", "doc-3"):
            store.record_publication(doc, "AppleNotesDestination", "v1", recipe="")

        assert store.forget_unknown_destinations(["ObsidianDestination"]) == {
            "AppleNotesDestination": 3
        }

    def test_nothing_to_forget_reports_nothing(self, store):
        store.record_publication("doc-1", "ObsidianDestination", "v1", recipe="")

        assert store.forget_unknown_destinations(["ObsidianDestination"]) == {}

    def test_it_is_safe_to_run_on_every_open(self, store):
        """It runs once per process, so running it twice must be a no-op."""
        store.record_publication("doc-1", "ObsidianDestination", "v1", recipe="")
        store.record_publication("doc-1", "AppleNotesDestination", "v1", recipe="")

        store.forget_unknown_destinations(["ObsidianDestination"])

        assert store.forget_unknown_destinations(["ObsidianDestination"]) == {}
        assert store.published_versions("ObsidianDestination") == {"doc-1": "v1"}

    def test_a_disabled_destination_is_not_a_deleted_one(self, store):
        """Registered is the question, not enabled.

        A destination the user turned off keeps its rows, so turning it back on
        republishes nothing. Passing the enabled list here instead of the
        registry would silently make every toggle a full re-sync.
        """
        store.record_publication("doc-1", "ObsidianDestination", "v1", recipe="")
        store.record_publication("doc-1", "NotionDestination", "v1", recipe="")

        store.forget_unknown_destinations(["ObsidianDestination", "NotionDestination"])

        assert store.published_versions("NotionDestination") == {"doc-1": "v1"}

    def test_an_empty_registry_deletes_nothing(self, store):
        """No destination at all is a failed import, not a mass retirement."""
        store.record_publication("doc-1", "ObsidianDestination", "v1", recipe="")

        with pytest.raises(ValueError, match="no destination is registered"):
            store.forget_unknown_destinations([])

        assert store.published_versions("ObsidianDestination") == {"doc-1": "v1"}

    def test_the_document_and_its_pages_survive(self, store):
        """The rows that go are the ones naming the destination, and no others."""
        store.record_document("doc-1", name="Notes")
        store.record_page("doc-1", 0, source_hash="abc")
        store.record_publication("doc-1", "AppleNotesDestination", "v1", recipe="")

        store.forget_unknown_destinations(["ObsidianDestination"])

        assert store.get_document("doc-1")["name"] == "Notes"
        assert store.get_pages("doc-1")[0]["source_hash"] == "abc"


class TestMaintenance:
    """The two things a user can safely do to the file by hand."""

    def test_counts_report_every_table(self, store):
        store.record_document("doc-1")
        store.record_publication("doc-1", "ObsidianDestination", "v1", recipe="")

        counts = store.counts()
        assert counts["documents"] == 1
        assert counts["publications"] == 1
        assert counts["pages"] == 0

    def test_a_healthy_database_passes_its_integrity_check(self, store):
        assert store.integrity_check() == "ok"

    def test_vacuum_leaves_the_data_alone(self, store):
        store.record_publication("doc-1", "ObsidianDestination", "v1", recipe="")

        store.vacuum()

        assert store.published_versions("ObsidianDestination") == {"doc-1": "v1"}


class TestCompareWithListing:
    """The live listing outranks whatever the last run happened to record."""

    def _entry(self, doc_id="doc-1", **overrides):
        """One listing entry, as the CLI builds it from the tablet's metadata."""
        return {
            "id": doc_id,
            "name": "Notes",
            "folder": "Work",
            "doc_type": "notebook",
            "version": "v1",
            **overrides,
        }

    def test_a_document_the_database_has_never_seen_is_new(self, store):
        rows, orphans = store.compare_with_listing([self._entry()], ["ObsidianDestination"])

        assert orphans == []
        assert rows[0]["status"] is STATUS_NEW
        assert rows[0]["pending"] == ["ObsidianDestination"]

    def test_a_newer_version_on_the_tablet_is_changed(self, store):
        store.record_document("doc-1", version="v1")
        store.record_publication("doc-1", "ObsidianDestination", "v1", recipe="")

        rows, _ = store.compare_with_listing([self._entry(version="v2")], ["ObsidianDestination"])
        assert rows[0]["status"] is STATUS_CHANGED

    def test_the_listing_decides_the_version_not_the_database(self, store):
        """The database's version is what a past run saw, not what is there now."""
        store.record_document("doc-1", version="v9")
        store.record_publication("doc-1", "ObsidianDestination", "v1", recipe="")

        rows, _ = store.compare_with_listing([self._entry()], ["ObsidianDestination"])
        assert rows[0]["status"] is STATUS_UP_TO_DATE

    def test_the_database_fills_in_a_type_the_listing_could_not_name(self, store):
        store.record_document("doc-1", doc_type="epub", version="v1")

        rows, _ = store.compare_with_listing([self._entry(doc_type=None)], ["ObsidianDestination"])
        assert rows[0]["doc_type"] == "epub"

    def test_the_listing_wins_over_a_stale_remembered_folder(self, store):
        store.record_document("doc-1", folder="Old", version="v1")

        rows, _ = store.compare_with_listing(
            [self._entry(folder="Archive")], ["ObsidianDestination"]
        )
        assert rows[0]["folder"] == "Archive"

    def test_a_published_document_absent_from_the_listing_is_an_orphan(self, store):
        store.record_document("doc-gone", name="Deleted", version="v1")
        store.record_publication("doc-gone", "ObsidianDestination", "v1", recipe="")

        rows, orphans = store.compare_with_listing([self._entry()], ["ObsidianDestination"])
        assert [row["id"] for row in orphans] == ["doc-gone"]
        assert [row["id"] for row in rows] == ["doc-1"]

    def test_a_never_published_absence_is_not_an_orphan(self, store):
        """Nothing was ever written for it, so there is nothing left behind."""
        store.record_document("doc-gone", version="v1")

        _, orphans = store.compare_with_listing([self._entry()], ["ObsidianDestination"])
        assert orphans == []

    def test_a_recorded_failure_outranks_the_listing(self, store):
        store.record_document("doc-1", version="v1")
        store.record_publication("doc-1", "ObsidianDestination", "v1", recipe="")
        store.record_failure("doc-1", "boom")

        rows, _ = store.compare_with_listing([self._entry()], ["ObsidianDestination"])
        assert rows[0]["status"] is STATUS_FAILED
        assert rows[0]["last_error"] == "boom"
