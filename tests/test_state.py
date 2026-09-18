"""Tests for the SQLite sync-state store."""

import json
import sqlite3
import threading

import pytest

from living_ink.state import SCHEMA_VERSION, StateStore, import_legacy_json


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
            first.record_publication("doc-1", "Obsidian", "v1")
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
        store.record_publication("doc-1", "Obsidian", "v1")
        assert store.integrity_check() == "ok"


class TestPublications:
    """The table that replaces processed_notebooks_<destination>.json."""

    def test_a_publication_round_trips(self, store):
        store.record_publication("doc-1", "Obsidian", "v1")
        assert store.published_versions("Obsidian") == {"doc-1": "v1"}

    def test_destinations_are_independent(self, store):
        store.record_publication("doc-1", "Obsidian", "v1")
        assert store.published_versions("AppleNotesDestination") == {}

    def test_republishing_merges_on_the_natural_key(self, store):
        store.record_publication("doc-1", "Obsidian", "v1")
        store.record_publication("doc-1", "Obsidian", "v2")

        assert store.published_versions("Obsidian") == {"doc-1": "v2"}
        rows = store.dump()["publications"]
        assert len(rows) == 1

    def test_the_first_publication_time_is_never_overwritten(self, store):
        """It is the only truthful source for a note's `created` date."""
        store.record_publication(
            "doc-1", "Obsidian", "v1", published_at="2020-01-01T00:00:00+00:00"
        )
        store.record_publication("doc-1", "Obsidian", "v2")

        record = store.get_publication("doc-1", "Obsidian")
        assert record["first_published_at"] == "2020-01-01T00:00:00+00:00"
        assert record["last_published_at"] != "2020-01-01T00:00:00+00:00"

    def test_an_external_id_survives_an_update_that_omits_it(self, store):
        """Most republish calls do not know the far-side id; they must not erase it."""
        store.record_publication("doc-1", "AppleNotes", "v1", external_id="x-coredata://42")
        store.record_publication("doc-1", "AppleNotes", "v2")

        assert store.get_publication("doc-1", "AppleNotes")["external_id"] == "x-coredata://42"

    def test_a_content_hash_survives_an_update_that_omits_it(self, store):
        store.record_publication("doc-1", "Obsidian", "v1", content_hash="abc123")
        store.record_publication("doc-1", "Obsidian", "v2")

        assert store.get_publication("doc-1", "Obsidian")["content_hash"] == "abc123"

    def test_versions_are_stored_as_text(self, store):
        """Device versions are integers, cloud hashes are strings; comparison is textual."""
        store.record_publication("doc-1", "Obsidian", 7)
        assert store.published_versions("Obsidian") == {"doc-1": "7"}

    def test_an_unknown_publication_is_none(self, store):
        assert store.get_publication("nope", "Obsidian") is None

    def test_forgetting_one_destination(self, store):
        store.record_publication("doc-1", "Obsidian", "v1")
        store.record_publication("doc-1", "AppleNotes", "v1")

        assert store.forget("doc-1", "Obsidian") == 1
        assert store.published_versions("Obsidian") == {}
        assert store.published_versions("AppleNotes") == {"doc-1": "v1"}

    def test_forgetting_every_destination(self, store):
        store.record_publication("doc-1", "Obsidian", "v1")
        store.record_publication("doc-1", "AppleNotes", "v1")

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
        store.record_publication("doc-1", "Obsidian", "v1", run_id=run_id)
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
            first.record_publication("doc-1", "Obsidian", "v1")
            second.record_publication("doc-2", "Obsidian", "v2")

            assert first.published_versions("Obsidian") == {"doc-1": "v1", "doc-2": "v2"}

    def test_threads_do_not_interleave_statements(self, store):
        """OCR fans out across threads; the shared connection needs the lock."""

        def write(start):
            for index in range(start, start + 25):
                store.record_publication(f"doc-{index}", "Obsidian", "v1")

        threads = [threading.Thread(target=write, args=(base,)) for base in (0, 100, 200)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(store.published_versions("Obsidian")) == 75

    def test_a_failed_write_rolls_back(self, store):
        store.record_publication("doc-1", "Obsidian", "v1")
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
        self._write(tmp_path, "processed_notebooks_AppleNotes.json", {"doc-2": 2})

        assert import_legacy_json(store, tmp_path) == 2
        assert store.published_versions("Obsidian") == {"doc-1": "1"}
        assert store.published_versions("AppleNotes") == {"doc-2": "2"}

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
