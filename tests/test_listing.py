"""Reading the tablet's listing, including the trash it does not admit to.

The functions themselves are old; the tests for the one that is new are the
point of this file. A trashed document on a real Paper Pure carries
``parent: "trash"`` and **no ``deleted`` key at all**, so the documented flag
reads False and the notebook the user threw away gets published.
"""

from living_ink.core.listing import (
    document_name,
    document_path,
    document_version,
    get_notebook_path,
    get_val,
    is_trashed,
)


def doc(**fields):
    """Build a listing entry, spelled the way a transport spells one."""
    entry = {"ID": "doc-1", "Type": "DocumentType", "VissibleName": "Notes", "Parent": ""}
    entry.update(fields)
    return entry


class TestATrashedDocumentIsNotACandidate:
    """D-a: the device signals the trash through the parent, not a flag."""

    def test_a_document_in_the_trash_is_trashed_without_the_flag(self):
        """The whole defect in one assertion: no ``deleted`` key anywhere."""
        item = doc(Parent="trash")

        assert "deleted" not in item
        assert is_trashed(item, {"doc-1": item}) is True

    def test_a_document_inside_a_trashed_folder_is_trashed(self):
        folder = {"ID": "f-1", "Type": "CollectionType", "VissibleName": "Old", "Parent": "trash"}
        item = doc(Parent="f-1")

        assert is_trashed(item, {"f-1": folder, "doc-1": item}) is True

    def test_the_documented_flag_still_counts(self):
        """Cloud sets it; SSH does not. Either one is enough."""
        item = doc(deleted=True)

        assert is_trashed(item, {"doc-1": item}) is True

    def test_a_live_document_is_not_trashed(self):
        folder = {"ID": "f-1", "Type": "CollectionType", "VissibleName": "Work", "Parent": ""}
        item = doc(Parent="f-1")

        assert is_trashed(item, {"f-1": folder, "doc-1": item}) is False

    def test_a_root_document_is_not_trashed(self):
        item = doc()

        assert is_trashed(item, {"doc-1": item}) is False

    def test_a_folder_the_user_named_trash_is_not_the_trash(self):
        """``[TRASH]`` is this module's marker, not a name the device uses."""
        folder = {"ID": "f-1", "Type": "CollectionType", "VissibleName": "[TRASH]", "Parent": ""}
        item = doc(Parent="f-1")

        assert get_notebook_path(item, {"f-1": folder, "doc-1": item}) == "[TRASH]"
        assert is_trashed(item, {"f-1": folder, "doc-1": item}) is True


class TestTheTwoWaysAPathIsWritten:
    """One walk up the tree, joined for a table or joined for a pattern.

    ``get_notebook_path`` spaces its separators out because it is read in the
    summary; ``document_path`` does not, because it is matched against
    something a user typed, and ``--source-path "Work/"`` has to be a folder
    filter without a second flag to make it one.
    """

    def _tree(self):
        outer = {"ID": "f-1", "Type": "CollectionType", "VissibleName": "Journal", "Parent": ""}
        inner = {"ID": "f-2", "Type": "CollectionType", "VissibleName": "2026", "Parent": "f-1"}
        item = doc(VissibleName="diary", Parent="f-2")
        return {"f-1": outer, "f-2": inner, "doc-1": item}, item

    def test_the_full_path_carries_the_title(self):
        id_map, item = self._tree()

        assert document_path(item, id_map) == "Journal/2026/diary"

    def test_the_folder_path_stops_short_of_it_and_spaces_out(self):
        id_map, item = self._tree()

        assert get_notebook_path(item, id_map) == "Journal / 2026"

    def test_a_document_at_the_root_is_just_its_title(self):
        item = doc()

        assert document_path(item, {"doc-1": item}) == "Notes"


class TestReadingTheFieldsATransportSpellsTwoWays:
    """``get_val`` speaks rmapy; the models expose aliases for it."""

    def test_a_dict_and_an_object_read_the_same(self):
        class Item:
            ID = "doc-9"

        assert get_val({"ID": "doc-9"}, "ID") == get_val(Item(), "ID")

    def test_a_missing_key_is_none(self):
        assert get_val({}, "ID") is None

    def test_the_name_falls_back_through_every_spelling(self):
        assert document_name({"VissibleName": "A"}) == "A"
        assert document_name({"VisibleName": "B"}) == "B"
        assert document_name({}) == ""

    def test_the_name_is_stripped(self):
        assert document_name({"VissibleName": "  Notes  "}) == "Notes"

    def test_the_content_hash_wins_over_the_counter(self):
        assert document_version({"hash": "abc", "Version": 3}) == "abc"

    def test_the_counter_answers_when_there_is_no_hash(self):
        assert document_version({"Version": 3}) == "3"

    def test_a_document_with_neither_still_answers(self):
        assert document_version({}) == "1"
