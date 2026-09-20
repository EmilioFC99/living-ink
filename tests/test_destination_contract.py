"""The destination contract, exercised through the pipeline with a fake.

Everything here runs :class:`~tests.fakes.FakeApiDestination` against the real
``SyncPipeline`` and a real ``state.db`` in a temp directory. The point is the
loop no shipped destination closes on its own: an id a destination invents has
to reach the database and come back to that destination on the next run.
"""

from pathlib import Path

import pytest

from living_ink import pipeline
from living_ink.destinations import MergeUnit
from living_ink.pipeline import DocumentJob, SyncPipeline
from tests.builders import make_page
from tests.fakes import FakeApiDestination


def make_job(tmp_path: Path, doc_id: str = "nb-1", version: str = "v1") -> DocumentJob:
    """Build a minimal job carrying one transcribed page."""
    job = DocumentJob(
        item={"ID": doc_id},
        notebook="Notes",
        notebook_id=doc_id,
        doc_type="notebook",
        version=version,
        folder_path="",
        display_title="Notes",
        keep_temp=True,
    )
    job.pages = [make_page(1, "Hello", image=tmp_path / f"{doc_id}.page-1.png")]
    return job


class TestExternalIdRoundTrip:
    """An id a destination mints has to survive until the next sync.

    Three hops, and nothing shipped travels all three: the destination reports
    it in :attr:`PublishResult.external_id`, the pipeline writes it to the
    ``publications`` row, and the next publish reads it back out as
    ``existing_id`` so the destination replaces that object rather than
    creating a second one.
    """

    @pytest.fixture(autouse=True)
    def _state(self, tmp_path, monkeypatch):
        """Point the state store at a temp directory for the duration."""
        monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path)
        monkeypatch.setattr(pipeline, "ROOT", tmp_path)
        monkeypatch.setattr(pipeline, "ensure_runtime_dirs", lambda: None)
        pipeline.reset_state_store()
        yield
        pipeline.reset_state_store()

    def _publish(self, dest, tmp_path, version="v1"):
        pipe = SyncPipeline(destinations=[dest])
        job = make_job(tmp_path, version=version)
        assert pipe._publish(job, [dest]) is True
        return job

    def test_the_first_publish_has_no_id_to_work_from(self, tmp_path):
        dest = FakeApiDestination()
        self._publish(dest, tmp_path)

        assert dest.seen_existing_id == [None]
        assert dest.objects == ["obj-1"]

    def test_the_id_is_written_to_the_publications_row(self, tmp_path):
        dest = FakeApiDestination()
        self._publish(dest, tmp_path)

        row = pipeline.get_state_store().get_publication("nb-1", "FakeApiDestination")
        assert row["external_id"] == "obj-1"

    def test_the_second_publish_is_handed_the_first_ones_id(self, tmp_path):
        dest = FakeApiDestination()
        self._publish(dest, tmp_path, version="v1")
        self._publish(dest, tmp_path, version="v2")

        assert dest.seen_existing_id == [None, "obj-1"]

    def test_a_replaced_note_does_not_become_a_second_note(self, tmp_path):
        """The whole reason the id is kept: one document, one object, forever."""
        dest = FakeApiDestination()
        for version in ("v1", "v2", "v3"):
            self._publish(dest, tmp_path, version=version)

        assert dest.objects == ["obj-1"]

    def test_the_row_is_filed_under_the_state_key_not_the_display_name(self, tmp_path):
        """A lookup under the wrong name reads as "never published"."""
        dest = FakeApiDestination()
        self._publish(dest, tmp_path)

        store = pipeline.get_state_store()
        assert store.get_publication("nb-1", dest.state_key) is not None
        assert store.get_publication("nb-1", dest.display_name) is None

    def test_where_it_landed_comes_back_too(self, tmp_path):
        """A renamed notebook is the same note in a new place, not a new note."""
        dest = FakeApiDestination()
        self._publish(dest, tmp_path, version="v1")
        self._publish(dest, tmp_path, version="v2")

        assert dest.seen_existing_target == [None, "inbox/Notes"]

    def test_a_dry_run_records_nothing_so_the_next_run_is_still_the_first(self, tmp_path):
        dest = FakeApiDestination()
        pipe = SyncPipeline(dry_run=True, destinations=[dest])
        pipe._publish(make_job(tmp_path), [dest])

        assert dest.seen_existing_id == []
        assert pipeline.get_state_store().get_publication("nb-1", "FakeApiDestination") is None


class TestUnpublishThroughThePipeline:
    """Pruning hands a destination the id it reported, and nothing else."""

    @pytest.fixture(autouse=True)
    def _state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path)
        monkeypatch.setattr(pipeline, "ROOT", tmp_path)
        monkeypatch.setattr(pipeline, "ensure_runtime_dirs", lambda: None)
        pipeline.reset_state_store()
        yield
        pipeline.reset_state_store()

    def test_pruning_deletes_exactly_the_object_that_was_published(self, tmp_path):
        dest = FakeApiDestination()
        pipe = SyncPipeline(prune=True, destinations=[dest])
        pipe._publish(make_job(tmp_path), [dest])

        pipe._prune_orphan(
            "nb-1", {"FakeApiDestination": {"external_id": "obj-1", "target": "inbox/Notes"}}
        )

        assert dest.deleted == ["obj-1"]
        assert dest.objects == []

    def test_an_orphan_with_no_id_is_left_alone(self, tmp_path):
        dest = FakeApiDestination()
        pipe = SyncPipeline(prune=True, destinations=[dest])
        pipe._publish(make_job(tmp_path), [dest])

        pipe._prune_orphan("nb-1", {"FakeApiDestination": {"external_id": None, "target": None}})

        assert dest.deleted == []
        assert dest.objects == ["obj-1"]

    def test_the_document_is_forgotten_either_way(self, tmp_path):
        """Keeping the row would only report the same orphan on every run."""
        dest = FakeApiDestination()
        pipe = SyncPipeline(prune=True, destinations=[dest])
        pipe._publish(make_job(tmp_path), [dest])

        pipe._prune_orphan("nb-1", {"FakeApiDestination": {"external_id": None, "target": None}})

        assert pipeline.get_state_store().get_publication("nb-1", "FakeApiDestination") is None


class TestTheDocumentThatArrives:
    """A destination is handed the document, not a file to go and read.

    The transcript used to be written to disk and parsed back out again, so a
    destination's idea of the text was whatever survived a round trip through
    a text file. Now the pages arrive as pages.
    """

    @pytest.fixture(autouse=True)
    def _state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path)
        monkeypatch.setattr(pipeline, "ROOT", tmp_path)
        monkeypatch.setattr(pipeline, "ensure_runtime_dirs", lambda: None)
        pipeline.reset_state_store()
        yield
        pipeline.reset_state_store()

    def _published(self, tmp_path):
        dest = FakeApiDestination()
        pipe = SyncPipeline(destinations=[dest])
        pipe._publish(make_job(tmp_path), [dest])
        return dest.seen_documents[0]

    def test_the_pages_arrive_as_pages(self, tmp_path):
        doc = self._published(tmp_path)

        assert [page.number for page in doc.pages] == [1]
        assert doc.pages[0].text == "Hello"

    def test_the_title_is_the_title_and_nothing_else(self, tmp_path):
        assert self._published(tmp_path).title == "Notes"


class TestTheFakeHonoursTheContract:
    """If the ABC grows a member, this fake stops constructing."""

    def test_it_is_a_complete_destination(self):
        FakeApiDestination()

    def test_it_declares_its_own_state_key(self):
        assert "state_key" in FakeApiDestination.__dict__

    def test_it_takes_the_safe_default_for_merge_unit(self):
        assert FakeApiDestination.merge_unit is MergeUnit.DOCUMENT
