"""Tests for the path matching behind ``--notebook``.

There used to be a third pair of classes here, over a ``format_notebook_item``
and a ``select_notebook_interactive`` that prompted when the query matched
several documents. Both are gone: product §7 says a bare ``sync`` never
prompts, and the menu was the one place in the library that asked.
"""

from living_ink.core.listing import matches_notebook_target, normalize_path_str
from tests.fixtures.listing import make_folder, make_item


class TestPathNormalization:
    """Tests for normalize_path_str."""

    def test_trims_and_lowercases(self):
        assert normalize_path_str("  Work / Notes  ") == "work/notes"

    def test_handles_multiple_slashes_and_backslashes(self):
        assert normalize_path_str(r"Work\Projects/2026/Plan") == "work/projects/2026/plan"

    def test_single_element(self):
        assert normalize_path_str("Notes") == "notes"


class TestMatchesNotebookTarget:
    """Tests for matches_notebook_target."""

    def setup_method(self):
        self.folder_work = make_folder("f1", "Work")
        self.folder_proj = make_folder("f2", "Projects", parent="f1")
        self.id_map = {
            "f1": self.folder_work,
            "f2": self.folder_proj,
        }

    def test_match_by_exact_name(self):
        doc = make_item("doc1", "Notes", parent="f1")
        assert matches_notebook_target(doc, "Notes", self.id_map) is True
        assert matches_notebook_target(doc, "notes", self.id_map) is True

    def test_match_by_document_id(self):
        doc = make_item("uuid-1234-abcd", "Notes", parent="f1")
        assert matches_notebook_target(doc, "uuid-1234-abcd", self.id_map) is True
        assert matches_notebook_target(doc, "UUID-1234-ABCD", self.id_map) is True

    def test_match_by_folder_path_slash(self):
        doc = make_item("doc1", "Sprint", parent="f1")
        assert matches_notebook_target(doc, "Work/Sprint", self.id_map) is True
        assert matches_notebook_target(doc, "work/sprint", self.id_map) is True

    def test_match_by_folder_path_spaced(self):
        doc = make_item("doc1", "Sprint", parent="f1")
        assert matches_notebook_target(doc, "Work / Sprint", self.id_map) is True
        assert matches_notebook_target(doc, "  work  /  sprint  ", self.id_map) is True

    def test_match_nested_folder_path(self):
        doc = make_item("doc1", "Milestones", parent="f2")
        assert matches_notebook_target(doc, "Work/Projects/Milestones", self.id_map) is True
        assert matches_notebook_target(doc, "Work / Projects / Milestones", self.id_map) is True

    def test_non_matching_query(self):
        doc = make_item("doc1", "Notes", parent="f1")
        assert matches_notebook_target(doc, "Personal/Notes", self.id_map) is False
        assert matches_notebook_target(doc, "OtherBook", self.id_map) is False
        assert matches_notebook_target(doc, "", self.id_map) is False
