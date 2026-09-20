"""What a run would do, decided once — and every way it used to be decided twice.

These tests are written against the real :class:`~living_ink.state.StateStore`
rather than a stub, because the half of change detection that regressed before
was a lookup keyed on the wrong column, and a stub would have agreed with it.
"""

import dataclasses
from typing import ClassVar, Tuple

import pytest

from living_ink.config import Setting, settings_for_section
from living_ink.core.recipe import document_recipe
from living_ink.core.selection import (
    EXCLUDED,
    NO_MATCHING_TAG,
    NOT_TARGETED,
    OUTSIDE_FOLDER,
    TRASHED,
    UNCHANGED,
    WRONG_TYPE,
    Selection,
    SelectionCriteria,
    select,
)
from living_ink.destinations.base import Destination, DestinationStatus
from living_ink.settings import Settings
from living_ink.sources import source_for_name
from living_ink.state import StateStore


class Vault(Destination):
    """A destination that reads the Obsidian settings and can be made forgetful."""

    state_key: ClassVar[str] = "Vault"
    display_name: ClassVar[str] = "Vault"
    settings: ClassVar[Tuple[Setting, ...]] = settings_for_section("obsidian")

    def __init__(self, exists: bool = True) -> None:
        self.exists = exists
        self.asked = []

    def check(self) -> DestinationStatus:
        return DestinationStatus(ok=True, detail="ready")

    def published_exists(self, ctx) -> bool:
        self.asked.append(ctx.doc_id)
        return self.exists

    def publish(self, doc, ctx):  # pragma: no cover - selection never publishes
        raise NotImplementedError


class Archive(Vault):
    """A second destination, so "pending at one but not the other" is testable."""

    state_key: ClassVar[str] = "Archive"
    display_name: ClassVar[str] = "Archive"


class Blind(Vault):
    """A destination whose existence check raises."""

    state_key: ClassVar[str] = "Blind"
    display_name: ClassVar[str] = "Blind"

    def published_exists(self, ctx) -> bool:
        raise OSError("vault is on a network share that went away")


@pytest.fixture
def settings():
    """A resolved-looking Settings with every recipe input populated."""
    return Settings(
        ai_provider="openai",
        ai_model="gpt-4o-mini",
        obsidian_vault_path="/tmp/vault",
        obsidian_root_folder="Inbox",
    )


@pytest.fixture
def store(tmp_path):
    """A real state database, discarded with the test."""
    opened = StateStore(tmp_path / "state.db")
    yield opened
    opened.close()


def doc(doc_id="doc-1", name="Notes", parent="", **fields):
    """Build a listing entry, spelled the way a transport spells one."""
    entry = {
        "ID": doc_id,
        "Type": "DocumentType",
        "VissibleName": name,
        "Parent": parent,
        "hash": "v1",
    }
    entry.update(fields)
    return entry


def folder(folder_id, name, parent=""):
    """Build a folder entry."""
    return {"ID": folder_id, "Type": "CollectionType", "VissibleName": name, "Parent": parent}


def publish(store, candidate, dest, settings, *, version=None, recipe=None):
    """Record what a successful run would have recorded for one candidate."""
    store.record_publication(
        candidate.doc_id if hasattr(candidate, "doc_id") else candidate,
        dest.state_key,
        version if version is not None else "v1",
        recipe=(
            recipe
            if recipe is not None
            else document_recipe(source_for_name("notebook"), dest, settings)
        ),
        target="Notes.md",
    )


class TestTheCandidateSet:
    """Everything the listing offers, narrowed to what a run will touch."""

    def test_a_never_published_document_is_a_candidate(self, store, settings):
        chosen = select([doc()], SelectionCriteria(), store, [Vault()], settings=settings)

        assert [c.doc_id for c in chosen.to_process] == ["doc-1"]
        assert chosen.skipped == ()

    def test_a_folder_is_not_a_skipped_document(self, store, settings):
        """Reporting folders as skips would bury the real skips."""
        listing = [folder("f-1", "Work"), doc(parent="f-1")]

        chosen = select(listing, SelectionCriteria(), store, [Vault()], settings=settings)

        assert len(chosen.to_process) == 1
        assert chosen.skipped == ()

    def test_an_unnamed_document_is_not_a_candidate(self, store, settings):
        chosen = select([doc(name="")], SelectionCriteria(), store, [Vault()], settings=settings)

        assert chosen == Selection()

    def test_a_published_document_is_not_a_candidate(self, store, settings):
        dest = Vault()
        publish(store, "doc-1", dest, settings)

        chosen = select([doc()], SelectionCriteria(), store, [dest], settings=settings)

        assert chosen.to_process == ()
        assert [reason for _, reason in chosen.skipped] == [UNCHANGED]

    def test_the_candidate_carries_what_the_stages_need(self, store, settings):
        listing = [folder("f-1", "Work"), doc(parent="f-1")]

        candidate = select(
            listing, SelectionCriteria(), store, [Vault()], settings=settings
        ).to_process[0]

        assert (candidate.doc_id, candidate.name, candidate.folder) == ("doc-1", "Notes", "Work")
        assert candidate.source == "notebook"
        assert candidate.version == "v1"

    def test_the_listing_order_is_the_run_order(self, store, settings):
        listing = [doc("doc-3"), doc("doc-1"), doc("doc-2")]

        chosen = select(listing, SelectionCriteria(), store, [Vault()], settings=settings)

        assert [c.doc_id for c in chosen.to_process] == ["doc-3", "doc-1", "doc-2"]


class TestATrashedDocumentIsNeverSynced:
    """D-a: a deleted notebook used to be published."""

    def test_a_document_in_the_trash_is_skipped(self, store, settings):
        chosen = select(
            [doc(parent="trash")], SelectionCriteria(), store, [Vault()], settings=settings
        )

        assert chosen.to_process == ()
        assert [reason for _, reason in chosen.skipped] == [TRASHED]

    def test_the_trash_outranks_an_explicit_target(self, store, settings):
        """Naming a notebook the user deleted must not resurrect it."""
        criteria = SelectionCriteria(target="Notes")

        chosen = select([doc(parent="trash")], criteria, store, [Vault()], settings=settings)

        assert [reason for _, reason in chosen.skipped] == [TRASHED]

    def test_the_trash_outranks_force(self, store, settings):
        chosen = select(
            [doc(parent="trash")],
            SelectionCriteria(force=True),
            store,
            [Vault()],
            settings=settings,
        )

        assert chosen.to_process == ()


class TestChangeDetectionCoversTheRecipe:
    """F1: ``version`` alone cannot see a prompt edit or a moved vault."""

    def test_a_changed_version_makes_it_pending(self, store, settings):
        dest = Vault()
        publish(store, "doc-1", dest, settings, version="v0")

        chosen = select([doc()], SelectionCriteria(), store, [dest], settings=settings)

        assert len(chosen.to_process) == 1

    def test_a_changed_recipe_makes_it_pending(self, store, settings):
        """The document did not move; the way it would be produced did."""
        dest = Vault()
        publish(store, "doc-1", dest, settings)
        moved = dataclasses.replace(settings, obsidian_root_folder="Archive")

        chosen = select([doc()], SelectionCriteria(), store, [dest], settings=moved)

        assert len(chosen.to_process) == 1

    def test_a_changed_provider_makes_it_pending(self, store, settings):
        dest = Vault()
        publish(store, "doc-1", dest, settings)
        swapped = dataclasses.replace(settings, ai_model="gpt-4o")

        chosen = select([doc()], SelectionCriteria(), store, [dest], settings=swapped)

        assert len(chosen.to_process) == 1

    def test_an_unrelated_setting_leaves_it_alone(self, store, settings):
        """Raising a threading knob must not re-sync the library."""
        dest = Vault()
        publish(store, "doc-1", dest, settings)
        busier = dataclasses.replace(settings, ocr_concurrency=settings.ocr_concurrency + 4)

        chosen = select([doc()], SelectionCriteria(), store, [dest], settings=busier)

        assert chosen.to_process == ()

    def test_a_row_recorded_without_a_recipe_is_pending(self, store, settings):
        """An imported legacy row says "unknown", and unknown must read as pending."""
        dest = Vault()
        publish(store, "doc-1", dest, settings, recipe="")

        chosen = select([doc()], SelectionCriteria(), store, [dest], settings=settings)

        assert len(chosen.to_process) == 1

    def test_the_recipe_is_carried_to_the_publish_stage(self, store, settings):
        """A forced run must record the right recipe, not an empty one."""
        dest = Vault()

        candidate = select(
            [doc()], SelectionCriteria(force=True), store, [dest], settings=settings
        ).to_process[0]

        assert candidate.recipes["Vault"] == document_recipe(
            source_for_name("notebook"), dest, settings
        )

    def test_the_recipe_is_computed_once_per_type_and_destination(self, store, settings):
        """Two hundred notebooks must not mean two hundred prompt-file reads."""
        from living_ink.core import selection as selection_module

        calls = []
        real = selection_module.document_recipe

        def counted(source, dest, resolved):
            calls.append((source.name, dest.state_key))
            return real(source, dest, resolved)

        selection_module.document_recipe = counted
        try:
            listing = [doc(f"doc-{n}") for n in range(20)]
            select(listing, SelectionCriteria(), store, [Vault()], settings=settings)
        finally:
            selection_module.document_recipe = real

        assert calls == [("notebook", "Vault")]


class TestADeletedNoteComesBack:
    """The other half of F1: the tablet did not change, the vault did."""

    def test_a_missing_note_makes_it_pending(self, store, settings):
        dest = Vault(exists=False)
        publish(store, "doc-1", dest, settings)

        chosen = select([doc()], SelectionCriteria(), store, [dest], settings=settings)

        assert len(chosen.to_process) == 1
        assert dest.asked == ["doc-1"]

    def test_a_present_note_is_left_alone(self, store, settings):
        dest = Vault(exists=True)
        publish(store, "doc-1", dest, settings)

        chosen = select([doc()], SelectionCriteria(), store, [dest], settings=settings)

        assert chosen.to_process == ()

    def test_a_never_published_document_is_not_asked(self, store, settings):
        """The check costs a stat per publication row; there is no row here."""
        dest = Vault()

        select([doc()], SelectionCriteria(), store, [dest], settings=settings)

        assert dest.asked == []

    def test_a_check_that_raises_does_not_republish_the_library(self, store, settings):
        dest = Blind()
        publish(store, "doc-1", dest, settings)

        chosen = select([doc()], SelectionCriteria(), store, [dest], settings=settings)

        assert chosen.to_process == ()

    def test_the_recorded_target_is_handed_to_the_check(self, store, settings):
        seen = {}

        class Recording(Vault):
            state_key: ClassVar[str] = "Recording"

            def published_exists(self, ctx) -> bool:
                seen["target"] = ctx.existing_target
                return True

        dest = Recording()
        publish(store, "doc-1", dest, settings)

        select([doc()], SelectionCriteria(), store, [dest], settings=settings)

        assert seen["target"] == "Notes.md"


class TestPendingIsPerDestination:
    """A notebook can be current in one place and missing from another."""

    def test_only_the_destination_that_is_behind_is_pending(self, store, settings):
        vault, archive = Vault(), Archive()
        publish(store, "doc-1", vault, settings)

        chosen = select([doc()], SelectionCriteria(), store, [vault, archive], settings=settings)

        assert [d.state_key for d in chosen.to_process[0].pending] == ["Archive"]

    def test_pending_is_a_tuple_not_a_stand_in_for_everywhere(self, store, settings):
        """The old map read an empty list as "no entry" and published to all."""
        vault, archive = Vault(), Archive()
        publish(store, "doc-1", vault, settings)
        publish(store, "doc-1", archive, settings)

        chosen = select([doc()], SelectionCriteria(), store, [vault, archive], settings=settings)

        assert chosen.to_process == ()
        assert [reason for _, reason in chosen.skipped] == [UNCHANGED]

    def test_force_marks_every_destination(self, store, settings):
        vault, archive = Vault(), Archive()
        publish(store, "doc-1", vault, settings)
        publish(store, "doc-1", archive, settings)

        chosen = select(
            [doc()], SelectionCriteria(force=True), store, [vault, archive], settings=settings
        )

        assert [d.state_key for d in chosen.to_process[0].pending] == ["Vault", "Archive"]

    def test_no_enabled_destination_means_no_candidate(self, store, settings):
        chosen = select([doc()], SelectionCriteria(), store, [], settings=settings)

        assert chosen.to_process == ()
        assert [reason for _, reason in chosen.skipped] == [UNCHANGED]


class TestTheLimitDefersRatherThanLies:
    """Trimmed-by-the-limit is not the same fact as unchanged."""

    def test_the_limit_trims_the_run(self, store, settings):
        listing = [doc(f"doc-{n}") for n in range(5)]

        chosen = select(listing, SelectionCriteria(limit=2), store, [Vault()], settings=settings)

        assert len(chosen.to_process) == 2

    def test_what_the_limit_trimmed_is_deferred_not_skipped(self, store, settings):
        listing = [doc(f"doc-{n}") for n in range(5)]

        chosen = select(listing, SelectionCriteria(limit=2), store, [Vault()], settings=settings)

        assert [c.doc_id for c in chosen.deferred] == ["doc-2", "doc-3", "doc-4"]
        assert chosen.skipped == ()

    def test_the_pending_total_counts_both(self, store, settings):
        listing = [doc(f"doc-{n}") for n in range(5)]

        chosen = select(listing, SelectionCriteria(limit=2), store, [Vault()], settings=settings)

        assert chosen.pending_total == 5

    def test_no_limit_defers_nothing(self, store, settings):
        listing = [doc(f"doc-{n}") for n in range(5)]

        chosen = select(listing, SelectionCriteria(), store, [Vault()], settings=settings)

        assert chosen.deferred == ()

    def test_a_limit_of_zero_means_unlimited(self, store, settings):
        """Today's config spells "no limit" as 0, and 0 must not mean "nothing"."""
        listing = [doc(f"doc-{n}") for n in range(3)]

        chosen = select(listing, SelectionCriteria(limit=0), store, [Vault()], settings=settings)

        assert len(chosen.to_process) == 3


class TestNarrowingByFolderTypeAndTag:
    """Each filter reports its own reason, so the summary can explain itself."""

    def _listing(self):
        return [
            folder("f-1", "Work"),
            folder("f-2", "Personal"),
            doc("doc-1", "Standup", parent="f-1"),
            doc("doc-2", "Recipes", parent="f-2"),
        ]

    def test_a_folder_substring_narrows_the_run(self, store, settings):
        criteria = SelectionCriteria(source_path="work")

        chosen = select(self._listing(), criteria, store, [Vault()], settings=settings)

        assert [c.doc_id for c in chosen.to_process] == ["doc-1"]
        assert [reason for _, reason in chosen.skipped] == [OUTSIDE_FOLDER]

    def test_a_folder_regex_narrows_the_run(self, store, settings):
        criteria = SelectionCriteria(source_regex=r"^Person")

        chosen = select(self._listing(), criteria, store, [Vault()], settings=settings)

        assert [c.doc_id for c in chosen.to_process] == ["doc-2"]

    def test_a_path_and_a_regex_together_are_refused(self):
        with pytest.raises(ValueError, match="not both"):
            SelectionCriteria(source_path="Work", source_regex="Work")

    def test_a_broken_regex_is_refused_before_the_tablet_is_contacted(self):
        with pytest.raises(ValueError, match="invalid folder regex"):
            SelectionCriteria(source_regex="Work(")

    def test_an_excluded_folder_is_never_synced(self, store, settings):
        """``sync.exclude`` was declared, defaulted and read by nothing.

        Templates and Quick Sheets were rendered and transcribed on every run,
        at real API cost, while ``config`` reported them as excluded.
        """
        listing = [
            folder("f-1", "Templates"),
            doc("doc-1", "Grid", parent="f-1"),
            doc("doc-2", "Standup"),
        ]
        criteria = SelectionCriteria(exclude=frozenset({"Templates"}))

        chosen = select(listing, criteria, store, [Vault()], settings=settings)

        assert [c.doc_id for c in chosen.to_process] == ["doc-2"]
        assert [reason for _, reason in chosen.skipped] == [EXCLUDED]

    def test_an_exclusion_reaches_everything_beneath_it(self, store, settings):
        listing = [
            folder("f-1", "Templates"),
            folder("f-2", "Grids", parent="f-1"),
            doc("doc-1", "Dotted", parent="f-2"),
        ]

        chosen = select(
            listing,
            SelectionCriteria(exclude=frozenset({"templates"})),
            store,
            [Vault()],
            settings=settings,
        )

        assert chosen.to_process == ()

    def test_an_exclusion_matches_a_whole_segment_not_a_substring(self, store, settings):
        """Excluding ``Templates`` must not also exclude ``My Templates 2024``."""
        listing = [
            folder("f-1", "My Templates 2024"),
            doc("doc-1", "Notes", parent="f-1"),
        ]

        chosen = select(
            listing,
            SelectionCriteria(exclude=frozenset({"Templates"})),
            store,
            [Vault()],
            settings=settings,
        )

        assert [c.doc_id for c in chosen.to_process] == ["doc-1"]

    def test_naming_a_notebook_overrides_the_exclusions(self, store, settings):
        """Exclusions narrow a sweep; asking for one document is not a sweep."""
        listing = [
            folder("f-1", "Templates"),
            doc("doc-1", "Grid", parent="f-1"),
        ]
        criteria = SelectionCriteria(target="Grid", exclude=frozenset({"Templates"}))

        chosen = select(listing, criteria, store, [Vault()], settings=settings)

        assert [c.doc_id for c in chosen.to_process] == ["doc-1"]

    def test_a_type_filter_replaces_rather_than_adds(self, store, settings):
        listing = [doc("doc-1", "Standup"), doc("doc-2", "Manual.pdf")]

        chosen = select(
            listing,
            SelectionCriteria(types=frozenset({"pdf"})),
            store,
            [Vault()],
            settings=settings,
        )

        assert [c.doc_id for c in chosen.to_process] == ["doc-2"]
        assert [reason for _, reason in chosen.skipped] == [WRONG_TYPE]

    def test_no_type_filter_takes_everything(self, store, settings):
        listing = [doc("doc-1", "Standup"), doc("doc-2", "Manual.pdf")]

        chosen = select(listing, SelectionCriteria(), store, [Vault()], settings=settings)

        assert len(chosen.to_process) == 2

    def test_a_tag_filter_drops_the_untagged(self, store, settings):
        class Tagged:
            def get_tags(self, item):
                return ["work"] if item["ID"] == "doc-1" else []

            def get_file_type(self, item):
                return ""

        listing = [doc("doc-1"), doc("doc-2")]
        criteria = SelectionCriteria(tags=frozenset({"Work"}))

        chosen = select(listing, criteria, store, [Vault()], settings=settings, client=Tagged())

        assert [c.doc_id for c in chosen.to_process] == ["doc-1"]
        assert [reason for _, reason in chosen.skipped] == [NO_MATCHING_TAG]

    def test_tags_are_read_only_for_survivors(self, store, settings):
        """Reading a tag is a round trip each; the cheap filters run first."""
        asked = []

        class Tagged:
            def get_tags(self, item):
                asked.append(item["ID"])
                return ["work"]

            def get_file_type(self, item):
                return ""

        listing = [doc("doc-1"), doc("doc-2", parent="trash")]
        criteria = SelectionCriteria(tags=frozenset({"work"}))

        select(listing, criteria, store, [Vault()], settings=settings, client=Tagged())

        assert asked == ["doc-1"]

    def test_no_tag_filter_reads_no_tags(self, store, settings):
        class Tagged:
            def get_tags(self, item):  # pragma: no cover - must not be called
                raise AssertionError("tags were read without a tag filter")

            def get_file_type(self, item):
                return ""

        select([doc()], SelectionCriteria(), store, [Vault()], settings=settings, client=Tagged())

    def test_a_transport_that_cannot_report_tags_excludes_nothing(self, store, settings):
        class Mute:
            def get_tags(self, item):
                raise OSError("no such command")

            def get_file_type(self, item):
                return ""

        criteria = SelectionCriteria(tags=frozenset({"work"}))

        chosen = select([doc()], criteria, store, [Vault()], settings=settings, client=Mute())

        assert len(chosen.to_process) == 1

    def test_a_target_narrows_to_one_document(self, store, settings):
        chosen = select(
            self._listing(),
            SelectionCriteria(target="Standup"),
            store,
            [Vault()],
            settings=settings,
        )

        assert [c.doc_id for c in chosen.to_process] == ["doc-1"]
        assert [reason for _, reason in chosen.skipped] == [NOT_TARGETED]

    def test_a_target_overrides_the_type_filter(self, store, settings):
        """Naming a PDF is asking for it, whatever the configured types say."""
        listing = [doc("doc-1", "Manual.pdf")]
        criteria = SelectionCriteria(target="Manual.pdf", types=frozenset({"notebook"}))

        chosen = select(listing, criteria, store, [Vault()], settings=settings)

        assert len(chosen.to_process) == 1


class TestTheTypeIsAuthoritativeWhenItCanBe:
    """A guessed type bakes the wrong renderer version into the recipe."""

    def test_the_transport_is_asked_when_there_is_one(self, store, settings):
        class Knows:
            def get_file_type(self, item):
                return "pdf"

        listing = [doc("doc-1", "Manual")]

        chosen = select(
            listing, SelectionCriteria(), store, [Vault()], settings=settings, client=Knows()
        )

        assert chosen.to_process[0].source == "pdf"

    def test_the_title_answers_when_there_is_no_transport(self, store, settings):
        chosen = select(
            [doc("doc-1", "Manual.pdf")], SelectionCriteria(), store, [Vault()], settings=settings
        )

        assert chosen.to_process[0].source == "pdf"


class TestOrphans:
    """A document with publications that the tablet no longer lists."""

    def test_a_published_document_missing_from_the_listing_is_an_orphan(self, store, settings):
        publish(store, "gone", Vault(), settings)

        chosen = select([doc()], SelectionCriteria(), store, [Vault()], settings=settings)

        assert chosen.orphans == ("gone",)

    def test_a_listed_document_is_not_an_orphan(self, store, settings):
        publish(store, "doc-1", Vault(), settings)

        chosen = select([doc()], SelectionCriteria(), store, [Vault()], settings=settings)

        assert chosen.orphans == ()

    def test_a_document_that_was_never_published_is_not_an_orphan(self, store, settings):
        chosen = select([], SelectionCriteria(), store, [Vault()], settings=settings)

        assert chosen.orphans == ()


class TestAPartialPublishIsNotADonePublish:
    """A note published with gaps in it is a debt the next run has to pay."""

    def _publish_with_gaps(self, store, dest, settings, *, pages_failed):
        """Record exactly what a partial run records: current version, current recipe, N gaps."""
        store.record_publication(
            "doc-1",
            dest.state_key,
            "v1",
            recipe=document_recipe(source_for_name("notebook"), dest, settings),
            pages_failed=pages_failed,
            target="Notes.md",
        )

    def test_a_row_with_failed_pages_is_still_pending(self, store, settings):
        """Nothing else will ever flip: the tablet is unchanged and so is the recipe.

        Publishing 197 of 200 pages and recording the row as done would make
        the three rate-limited gaps permanent — the user would have to force a
        re-sync of the whole library to get pages the run already knew it lost.
        """
        dest = Vault(exists=True)
        self._publish_with_gaps(store, dest, settings, pages_failed=3)

        chosen = select([doc()], SelectionCriteria(), store, [dest], settings=settings)

        assert [c.doc_id for c in chosen.to_process] == ["doc-1"]

    def test_a_row_with_no_failed_pages_is_not_pending(self, store, settings):
        """The control: without it the previous test would pass on any stale row.

        The two rows differ in one column, so a run that re-syncs the complete
        one is re-transcribing a library for nothing.
        """
        dest = Vault(exists=True)
        self._publish_with_gaps(store, dest, settings, pages_failed=0)

        chosen = select([doc()], SelectionCriteria(), store, [dest], settings=settings)

        assert chosen.to_process == ()
        assert [reason for _, reason in chosen.skipped] == [UNCHANGED]

    def test_the_predicate_owns_the_rule_with_gaps(self, store, settings):
        """``sync --status`` and the run both reach this through one predicate.

        Asserting it here as well as through :func:`select` is what stops the
        preview and the run disagreeing about whether a gapped note is done.
        """
        from living_ink.core.selection import _owes_a_publish

        dest = Vault(exists=True)
        recipe = document_recipe(source_for_name("notebook"), dest, settings)
        self._publish_with_gaps(store, dest, settings, pages_failed=1)

        assert _owes_a_publish(store, "doc-1", "v1", recipe, dest) is True

    def test_the_predicate_owns_the_rule_without_gaps(self, store, settings):
        """A complete row with a matching version and recipe owes nothing."""
        from living_ink.core.selection import _owes_a_publish

        dest = Vault(exists=True)
        recipe = document_recipe(source_for_name("notebook"), dest, settings)
        self._publish_with_gaps(store, dest, settings, pages_failed=0)

        assert _owes_a_publish(store, "doc-1", "v1", recipe, dest) is False

    def test_the_gaps_are_retried_only_where_they_happened(self, store, settings):
        """The count is a fact about one row, and a run can lose a page at one place only.

        Reading it per document instead of per destination would rewrite a
        complete note somewhere else every time another destination stumbled.
        """
        vault, archive = Vault(exists=True), Archive(exists=True)
        self._publish_with_gaps(store, vault, settings, pages_failed=2)
        self._publish_with_gaps(store, archive, settings, pages_failed=0)

        chosen = select([doc()], SelectionCriteria(), store, [vault, archive], settings=settings)

        assert [d.state_key for d in chosen.to_process[0].pending] == ["Vault"]
