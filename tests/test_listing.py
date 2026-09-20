"""Reading the tablet's listing, including the trash it does not admit to.

The functions themselves are old; the tests for the one that is new are the
point of this file. A trashed document on a real Paper Pure carries
``parent: "trash"`` and **no ``deleted`` key at all**, so the documented flag
reads False and the notebook the user threw away gets published.
"""

from living_ink.core.listing import (
    document_id,
    document_name,
    document_path,
    document_version,
    get_notebook_path,
    is_document,
    is_trashed,
)
from tests.fixtures.listing import make_folder as folder
from tests.fixtures.listing import make_item as doc


class TestATrashedDocumentIsNotACandidate:
    """D-a: the device signals the trash through the parent, not a flag."""

    def test_a_document_in_the_trash_is_trashed_without_the_flag(self):
        """The whole defect in one assertion: the flag says nothing."""
        item = doc(parent="trash")

        assert item.deleted is False
        assert is_trashed(item, {"doc-1": item}) is True

    def test_a_document_inside_a_trashed_folder_is_trashed(self):
        folder_ = folder("f-1", "Old", parent="trash")
        item = doc(parent="f-1")

        assert is_trashed(item, {"f-1": folder_, "doc-1": item}) is True

    def test_the_documented_flag_still_counts(self):
        """Cloud sets it; SSH does not. Either one is enough."""
        item = doc(deleted=True)

        assert is_trashed(item, {"doc-1": item}) is True

    def test_a_live_document_is_not_trashed(self):
        folder_ = folder("f-1", "Work")
        item = doc(parent="f-1")

        assert is_trashed(item, {"f-1": folder_, "doc-1": item}) is False

    def test_a_root_document_is_not_trashed(self):
        item = doc()

        assert is_trashed(item, {"doc-1": item}) is False

    def test_a_folder_the_user_named_trash_is_not_the_trash(self):
        """``[TRASH]`` is this module's marker, not a name the device uses."""
        folder_ = folder("f-1", "[TRASH]")
        item = doc(parent="f-1")

        assert get_notebook_path(item, {"f-1": folder_, "doc-1": item}) == "[TRASH]"
        assert is_trashed(item, {"f-1": folder_, "doc-1": item}) is True


class TestTheTwoWaysAPathIsWritten:
    """One walk up the tree, joined for a table or joined for a pattern.

    ``get_notebook_path`` spaces its separators out because it is read in the
    summary; ``document_path`` does not, because it is matched against
    something a user typed, and ``--source-path "Work/"`` has to be a folder
    filter without a second flag to make it one.
    """

    def _tree(self):
        outer = folder("f-1", "Journal")
        inner = folder("f-2", "2026", parent="f-1")
        item = doc(name="diary", parent="f-2")
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


class TestReadingTheFieldsTheModelDeclares:
    """The readers name the model's own fields, and nothing else.

    They used to go through a ``get_val(item, key)`` that took the key as a
    string, tried rmapy's spelling, then the lower-cased one, then a dict
    lookup — so a key naming nothing returned ``None`` and a document arrived
    with no title, no parent or no type rather than an error.
    """

    def test_the_id_is_read_off_the_model(self):
        assert document_id(doc("doc-9")) == "doc-9"

    def test_the_name_answers_empty_when_there_is_none(self):
        assert document_name(doc(name="A")) == "A"
        assert document_name(doc(name="")) == ""

    def test_the_name_is_stripped(self):
        assert document_name(doc(name="  Notes  ")) == "Notes"

    def test_a_folder_is_not_a_document(self):
        assert is_document(doc()) is True
        assert is_document(folder()) is False

    def test_an_unknown_type_is_not_a_document_either(self):
        """Asked positively: a third type is not a document by default."""
        assert is_document(doc(doc_type="TemplateType")) is False

    def test_the_content_hash_wins_over_the_counter(self):
        item = doc(content_hash="abc")
        item.version = 3

        assert document_version(item) == "abc"

    def test_the_counter_answers_when_there_is_no_hash(self):
        item = doc()
        item.version = 3

        assert document_version(item) == "3"

    def test_a_counter_the_transport_sent_as_text_is_still_a_counter(self):
        """The SSH transport reads it out of a JSON file, so it arrives typed."""
        item = doc()
        item.version = "7"

        assert document_version(item) == "7"

    def test_a_counter_that_is_not_a_number_falls_back(self):
        item = doc()
        item.version = "not-a-number"

        assert document_version(item) == "1"

    def test_a_document_with_neither_still_answers(self):
        assert document_version(doc()) == "1"
