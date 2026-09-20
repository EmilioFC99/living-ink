"""Tests for notebook path matching and interactive disambiguation selection."""

import datetime
from unittest.mock import MagicMock

from living_ink.core.listing import matches_notebook_target, normalize_path_str
from living_ink.pipeline import format_notebook_item, select_notebook_interactive
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


class TestFormatNotebookItem:
    """Tests for format_notebook_item."""

    def test_format_without_parent(self):
        doc = make_item("doc-12345678-abcd", "RootNote", parent="")
        res = format_notebook_item(doc, {})
        assert "RootNote" in res
        assert "[ID: doc-1234]" in res

    def test_format_with_parent_and_datetime(self):
        folder = make_folder("f1", "Work")
        dt = datetime.datetime(2026, 9, 16, 15, 30)
        doc = make_item("doc-9999", "SprintPlan", parent="f1", modified=dt)
        res = format_notebook_item(doc, {"f1": folder})
        assert "Work / SprintPlan" in res
        assert "[ID: doc-9999]" in res
        assert "(modified: 2026-09-16 15:30)" in res


class TestSelectNotebookInteractive:
    """Tests for select_notebook_interactive."""

    def setup_method(self):
        self.doc1 = make_item("id1", "Notes", parent="")
        self.doc2 = make_item("id2", "Notes", parent="")
        self.id_map = {}

    def test_single_match_returns_directly(self):
        res = select_notebook_interactive([self.doc1], "Notes", self.id_map)
        assert res == [self.doc1]

    def test_empty_match_returns_empty(self):
        res = select_notebook_interactive([], "Notes", self.id_map)
        assert res == []

    def test_non_interactive_returns_all(self):
        mock_print = MagicMock()
        res = select_notebook_interactive(
            [self.doc1, self.doc2],
            "Notes",
            self.id_map,
            print_func=mock_print,
            is_interactive=False,
        )
        assert res == [self.doc1, self.doc2]
        assert any("non-interactive" in str(c) for c in mock_print.call_args_list)

    def test_interactive_select_first(self):
        inputs = iter(["1"])
        prints = []
        res = select_notebook_interactive(
            [self.doc1, self.doc2],
            "Notes",
            self.id_map,
            input_func=lambda _: next(inputs),
            print_func=prints.append,
            is_interactive=True,
        )
        assert res == [self.doc1]

    def test_interactive_select_second(self):
        inputs = iter(["2"])
        prints = []
        res = select_notebook_interactive(
            [self.doc1, self.doc2],
            "Notes",
            self.id_map,
            input_func=lambda _: next(inputs),
            print_func=prints.append,
            is_interactive=True,
        )
        assert res == [self.doc2]

    def test_interactive_select_all(self):
        inputs = iter(["a"])
        prints = []
        res = select_notebook_interactive(
            [self.doc1, self.doc2],
            "Notes",
            self.id_map,
            input_func=lambda _: next(inputs),
            print_func=prints.append,
            is_interactive=True,
        )
        assert res == [self.doc1, self.doc2]

    def test_interactive_default_enter_selects_all(self):
        inputs = iter([""])
        prints = []
        res = select_notebook_interactive(
            [self.doc1, self.doc2],
            "Notes",
            self.id_map,
            input_func=lambda _: next(inputs),
            print_func=prints.append,
            is_interactive=True,
        )
        assert res == [self.doc1, self.doc2]

    def test_interactive_cancel(self):
        inputs = iter(["q"])
        prints = []
        res = select_notebook_interactive(
            [self.doc1, self.doc2],
            "Notes",
            self.id_map,
            input_func=lambda _: next(inputs),
            print_func=prints.append,
            is_interactive=True,
        )
        assert res == []
        assert any("Cancelled" in str(p) for p in prints)

    def test_interactive_retry_on_invalid_input(self):
        inputs = iter(["99", "abc", "1"])
        prints = []
        res = select_notebook_interactive(
            [self.doc1, self.doc2],
            "Notes",
            self.id_map,
            input_func=lambda _: next(inputs),
            print_func=prints.append,
            is_interactive=True,
        )
        assert res == [self.doc1]
        assert any("Invalid selection" in str(p) for p in prints)
