"""Tests for splicing generated content into a note without destroying it."""

from living_ink import notemerge
from living_ink.notemerge import MANAGED_BEGIN, MANAGED_END


def build(body="Transcript.", **front):
    """Render a note the way ObsidianDestination would."""
    values = {"source": "Remarkable/Work/Notes", "tags": ["remarkable"]}
    values.update(front)
    return notemerge.owned_frontmatter_lines(values), body


class TestSplitFrontmatter:
    """A YAML block is only frontmatter when it is fenced and terminated."""

    def test_a_block_is_separated_from_the_body(self):
        front, body = notemerge.split_frontmatter("---\ncreated: 2026-01-01\n---\n\nHello")
        assert front == ["created: 2026-01-01"]
        assert body == "Hello"

    def test_a_note_without_frontmatter(self):
        front, body = notemerge.split_frontmatter("Hello")
        assert (front, body) == ([], "Hello")

    def test_an_unterminated_fence_is_not_frontmatter(self):
        """Swallowing the whole file as frontmatter would delete it."""
        text = "---\ncreated: 2026-01-01\nstill going"
        assert notemerge.split_frontmatter(text) == ([], text)

    def test_a_horizontal_rule_inside_the_body_is_not_a_fence(self):
        front, body = notemerge.split_frontmatter("---\na: 1\n---\n\ntop\n\n---\n\nbottom")
        assert front == ["a: 1"]
        assert "bottom" in body


class TestForeignFrontmatter:
    """Keys Living Ink does not write belong to the user."""

    def test_owned_keys_are_dropped(self):
        assert notemerge.foreign_frontmatter(["created: x", "source: y"]) == []

    def test_unknown_keys_are_kept(self):
        assert notemerge.foreign_frontmatter(["aliases: [Foo]"]) == ["aliases: [Foo]"]

    def test_a_multiline_owned_list_is_dropped_whole(self):
        lines = ["tags:", "  - remarkable", "  - work", "cssclass: wide"]
        assert notemerge.foreign_frontmatter(lines) == ["cssclass: wide"]

    def test_a_multiline_foreign_list_is_kept_whole(self):
        lines = ["created: x", "aliases:", "  - Foo", "  - Bar"]
        assert notemerge.foreign_frontmatter(lines) == ["aliases:", "  - Foo", "  - Bar"]

    def test_order_is_preserved(self):
        lines = ["zeta: 1", "created: x", "alpha: 2"]
        assert notemerge.foreign_frontmatter(lines) == ["zeta: 1", "alpha: 2"]


class TestFrontmatterValue:
    """Reading one key back out, to carry it forward."""

    def test_a_value_is_read(self):
        assert notemerge.frontmatter_value(["created: 2020-05-04"], "created") == "2020-05-04"

    def test_quotes_are_stripped(self):
        assert notemerge.frontmatter_value(['title: "Foo"'], "title") == "Foo"

    def test_a_missing_key_is_none(self):
        assert notemerge.frontmatter_value(["created: x"], "updated") is None

    def test_an_empty_value_is_none(self):
        assert notemerge.frontmatter_value(["created:"], "created") is None


class TestRenderNewNote:
    """With no existing file the output is straightforward."""

    def test_the_body_is_wrapped_in_markers(self):
        front, body = build()
        out = notemerge.render(front, body)
        assert MANAGED_BEGIN in out and MANAGED_END in out
        assert "Transcript." in out

    def test_the_frontmatter_is_fenced(self):
        front, body = build()
        out = notemerge.render(front, body)
        assert out.startswith("---\n")
        assert "source: Remarkable/Work/Notes" in out

    def test_a_list_value_becomes_a_yaml_sequence(self):
        front, _ = build(tags=["remarkable", "work"])
        out = notemerge.render(front, "x")
        assert "tags:\n  - remarkable\n  - work" in out

    def test_empty_values_are_omitted(self):
        front, _ = build(type=None, document="")
        out = notemerge.render(front, "x")
        assert "type:" not in out
        assert "document:" not in out

    def test_the_file_ends_with_one_newline(self):
        front, body = build()
        assert notemerge.render(front, body).endswith("\n")
        assert not notemerge.render(front, body).endswith("\n\n")


class TestRenderPreservesUserContent:
    """The whole point: a sync must not delete what someone wrote."""

    def _existing(self, before="", after=""):
        front, _ = build()
        return (
            notemerge.render(front, "Old transcript.")
            .replace(MANAGED_BEGIN, f"{before}{MANAGED_BEGIN}")
            .replace(MANAGED_END, f"{MANAGED_END}{after}")
        )

    def test_text_above_the_block_survives(self):
        existing = self._existing(before="My own intro.\n\n")
        front, _ = build()
        out = notemerge.render(front, "New transcript.", existing)

        assert "My own intro." in out
        assert "New transcript." in out
        assert "Old transcript." not in out

    def test_text_below_the_block_survives(self):
        existing = self._existing(after="\n\n## My follow-up\n\nThoughts.")
        front, _ = build()
        out = notemerge.render(front, "New transcript.", existing)

        assert "## My follow-up" in out
        assert "Thoughts." in out

    def test_text_on_both_sides_survives(self):
        existing = self._existing(before="Above.\n\n", after="\n\nBelow.")
        front, _ = build()
        out = notemerge.render(front, "New.", existing)

        assert out.index("Above.") < out.index("New.") < out.index("Below.")

    def test_a_user_frontmatter_key_survives(self):
        existing = self._existing()
        existing = existing.replace("---\n", "---\naliases:\n  - Standup\n", 1)
        front, _ = build()
        out = notemerge.render(front, "New.", existing)

        assert "aliases:" in out
        assert "  - Standup" in out

    def test_the_creation_date_is_not_duplicated(self):
        """Owned keys come from this run, not from both runs."""
        existing = self._existing()
        front, _ = build(created="2020-01-01", updated="2026-09-17")
        out = notemerge.render(front, "New.", existing)

        assert out.count("created:") == 1
        assert "created: 2020-01-01" in out


class TestRenderAdoptsOlderNotes:
    """Notes written before markers existed have to be handled without guessing."""

    def test_a_generated_note_without_markers_is_regenerated(self):
        """`source: Remarkable/` means Living Ink wrote every line of it."""
        existing = (
            "---\ncreated: 2020-01-01\nsource: Remarkable/Work/Notes\n---\n\nOld transcript.\n"
        )
        front, _ = build()
        out = notemerge.render(front, "New transcript.", existing)

        assert "Old transcript." not in out
        assert "New transcript." in out
        assert MANAGED_BEGIN in out

    def test_a_foreign_note_is_kept_above_the_block(self):
        """A file that is not ours must survive a name collision intact."""
        existing = "# Somebody else's note\n\nImportant.\n"
        front, _ = build()
        out = notemerge.render(front, "Transcript.", existing)

        assert "Somebody else's note" in out
        assert "Important." in out
        assert out.index("Important.") < out.index(MANAGED_BEGIN)

    def test_a_foreign_note_keeps_its_frontmatter(self):
        existing = "---\nauthor: Someone\n---\n\nTheir words.\n"
        front, _ = build()
        out = notemerge.render(front, "Transcript.", existing)

        assert "author: Someone" in out
        assert "Their words." in out

    def test_an_empty_file_is_treated_as_new(self):
        front, _ = build()
        out = notemerge.render(front, "Transcript.", "")
        assert out.count(MANAGED_BEGIN) == 1


class TestSplitManagedRegion:
    """Locating the block Living Ink owns."""

    def test_no_markers(self):
        assert notemerge.split_managed_region("plain") == ("plain", None, "")

    def test_content_is_returned(self):
        body = f"a\n{MANAGED_BEGIN}\nmine\n{MANAGED_END}\nb"
        before, current, after = notemerge.split_managed_region(body)
        assert before == "a\n"
        assert current.strip() == "mine"
        assert after.strip() == "b"

    def test_an_unterminated_block_is_regenerated(self):
        """A begin with no end means a truncated file, not user content."""
        before, current, after = notemerge.split_managed_region(f"a\n{MANAGED_BEGIN}\nhalf")
        assert before == "a\n"
        assert current == ""
        assert after == ""


class TestLooksGenerated:
    """The discriminator for markerless notes."""

    def test_a_generated_note(self):
        assert notemerge.looks_generated("---\nsource: Remarkable/Foo\n---\n\nx") is True

    def test_a_foreign_note(self):
        assert notemerge.looks_generated("---\nauthor: Someone\n---\n\nx") is False

    def test_a_note_with_no_frontmatter(self):
        assert notemerge.looks_generated("just text") is False

    def test_the_key_must_be_in_the_frontmatter(self):
        """A body that merely mentions the path is not a generated note."""
        assert notemerge.looks_generated("I wrote source: Remarkable/Foo in my note") is False


class TestReadExisting:
    """Reading is best-effort; an unreadable note is treated as absent."""

    def test_a_missing_file(self, tmp_path):
        assert notemerge.read_existing(tmp_path / "nope.md") is None

    def test_a_file_is_read(self, tmp_path):
        path = tmp_path / "note.md"
        path.write_text("hello", encoding="utf-8")
        assert notemerge.read_existing(path) == "hello"

    def test_undecodable_bytes_are_treated_as_absent(self, tmp_path):
        path = tmp_path / "note.md"
        path.write_bytes(b"\xff\xfe\x00binary")
        assert notemerge.read_existing(path) is None


class TestInspectExisting:
    """Absent, readable, and unreadable are three answers, not two."""

    def test_nothing_there(self, tmp_path):
        seen = notemerge.inspect_existing(tmp_path / "gone.md")
        assert seen.text is None
        assert seen.unreadable is False
        assert seen.occupied is False

    def test_a_note_we_can_read(self, tmp_path):
        note = tmp_path / "note.md"
        note.write_text("hello", encoding="utf-8")

        seen = notemerge.inspect_existing(note)

        assert seen.text == "hello"
        assert seen.unreadable is False
        assert seen.occupied is True

    def test_bytes_that_are_not_utf_8(self, tmp_path):
        note = tmp_path / "note.md"
        note.write_bytes(b"\xff\xfe\x00")

        seen = notemerge.inspect_existing(note)

        assert seen.text is None
        assert seen.unreadable is True
        assert seen.occupied is True

    def test_a_directory_wearing_the_name(self, tmp_path):
        (tmp_path / "note.md").mkdir()
        assert notemerge.inspect_existing(tmp_path / "note.md").unreadable is True

    def test_read_existing_still_flattens_both_to_none(self, tmp_path):
        # Kept for callers that only want the text; the point of the split is
        # that the ones deciding whether to overwrite no longer use it.
        (tmp_path / "bad.md").write_bytes(b"\xff")
        assert notemerge.read_existing(tmp_path / "bad.md") is None
        assert notemerge.read_existing(tmp_path / "gone.md") is None
