"""Tests for splicing generated content into a note without destroying it."""

from living_ink import notemerge


def build(body="Transcript.", **front):
    """Render a note the way ObsidianDestination would."""
    values = {"source": "Remarkable/Work/Notes", "tags": ["remarkable"]}
    values.update(front)
    return notemerge.owned_frontmatter_lines(values), [("page-1", body)]


def text(front, blocks, existing=None):
    """Render and drop the warnings, for tests that are not about them."""
    return notemerge.render(front, blocks, existing)[0]


def begin(block_id="page-1"):
    """The begin marker for a block, without its digest."""
    return f"<!-- living-ink:begin {block_id}"


def end(block_id="page-1"):
    """The end marker for a block."""
    return notemerge.end_marker(block_id)


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
        front, blocks = build()
        out = text(front, blocks)
        assert begin() in out and end() in out
        assert "Transcript." in out

    def test_every_block_gets_its_own_pair(self):
        front, _ = build()
        out = text(front, [("page-1", "One"), ("page-2", "Two")])
        assert out.count("living-ink:begin") == 2
        assert out.index(end("page-1")) < out.index(begin("page-2"))

    def test_a_block_carries_the_digest_of_its_own_content(self):
        front, _ = build()
        out = text(front, [("page-1", "One")])
        assert f"h={notemerge.block_digest('One')}" in out

    def test_the_frontmatter_is_fenced(self):
        front, blocks = build()
        out = text(front, blocks)
        assert out.startswith("---\n")
        assert "source: Remarkable/Work/Notes" in out

    def test_a_list_value_becomes_a_yaml_sequence(self):
        front, _ = build(tags=["remarkable", "work"])
        out = text(front, [("page-1", "x")])
        assert "tags:\n  - remarkable\n  - work" in out

    def test_empty_values_are_omitted(self):
        front, _ = build(type=None, document="")
        out = text(front, [("page-1", "x")])
        assert "type:" not in out
        assert "document:" not in out

    def test_the_file_ends_with_one_newline(self):
        front, blocks = build()
        assert text(front, blocks).endswith("\n")
        assert not text(front, blocks).endswith("\n\n")

    def test_no_blocks_at_all_still_produces_a_note(self):
        front, _ = build()
        out = text(front, [])
        assert out.startswith("---\n")
        assert "living-ink:begin" not in out


class TestTheDigestIsDeterministic:
    """§20.3 F4: the one failure mode worse than the problem blocks solve."""

    def test_the_same_content_digests_the_same_way_every_time(self):
        assert notemerge.block_digest("Page 1") == notemerge.block_digest("Page 1")

    def test_surrounding_whitespace_is_not_content(self):
        assert notemerge.block_digest("  Page 1\n\n") == notemerge.block_digest("Page 1")

    def test_different_content_digests_differently(self):
        assert notemerge.block_digest("Page 1") != notemerge.block_digest("Page 2")

    def test_merging_twice_is_merging_once(self):
        # If anything run-to-run variable reached the digest, the second merge
        # would read every block as user-edited and park a duplicate of it —
        # on every sync, forever.
        front, _ = build()
        blocks = [("page-1", "One"), ("page-2", "Two")]
        once = text(front, blocks)
        twice = text(front, blocks, once)
        assert twice == once

    def test_merging_twice_is_merging_once_with_user_text_in_between(self):
        front, _ = build()
        blocks = [("page-1", "One"), ("page-2", "Two")]
        once = text(front, blocks)
        annotated = once.replace(end("page-1"), f"{end('page-1')}\n\nMy thought.")
        twice = text(front, blocks, annotated)
        assert text(front, blocks, twice) == twice
        assert "My thought." in twice


class TestRenderPreservesUserContent:
    """The whole point: a sync must not delete what someone wrote."""

    def _existing(self, before="", after=""):
        front, _ = build()
        out = text(front, [("page-1", "Old transcript.")])
        if before:
            out = out.replace(begin(), f"{before}{begin()}", 1)
        if after:
            out = out.replace(end(), f"{end()}{after}", 1)
        return out

    def test_text_above_the_block_survives(self):
        existing = self._existing(before="My own intro.\n\n")
        front, _ = build()
        out = text(front, [("page-1", "New transcript.")], existing)

        assert "My own intro." in out
        assert "New transcript." in out
        assert "Old transcript." not in out

    def test_text_below_the_block_survives(self):
        existing = self._existing(after="\n\n## My follow-up\n\nThoughts.")
        front, _ = build()
        out = text(front, [("page-1", "New transcript.")], existing)

        assert "## My follow-up" in out
        assert "Thoughts." in out

    def test_text_on_both_sides_survives(self):
        existing = self._existing(before="Above.\n\n", after="\n\nBelow.")
        front, _ = build()
        out = text(front, [("page-1", "New.")], existing)

        assert out.index("Above.") < out.index("New.") < out.index("Below.")

    def test_a_user_frontmatter_key_survives(self):
        existing = self._existing()
        existing = existing.replace("---\n", "---\naliases:\n  - Standup\n", 1)
        front, _ = build()
        out = text(front, [("page-1", "New.")], existing)

        assert "aliases:" in out
        assert "  - Standup" in out

    def test_the_creation_date_is_not_duplicated(self):
        """Owned keys come from this run, not from both runs."""
        existing = self._existing()
        front, _ = build(created="2020-01-01", updated="2026-09-17")
        out = text(front, [("page-1", "New.")], existing)

        assert out.count("created:") == 1
        assert "created: 2020-01-01" in out


class TestTextBetweenBlocks:
    """Everything between one block's end and the next block's begin is theirs."""

    def _annotated(self):
        front, _ = build()
        out = text(front, [("page-1", "One"), ("page-2", "Two")])
        return front, out.replace(end("page-1"), f"{end('page-1')}\n\nRe page 1: see [[DMBOK]].")

    def test_it_stays_with_the_page_it_is_about(self):
        front, existing = self._annotated()
        out = text(front, [("page-1", "One v2"), ("page-2", "Two v2")], existing)

        assert out.index("One v2") < out.index("Re page 1") < out.index("Two v2")

    def test_a_page_inserted_between_two_others_does_not_split_it(self):
        # The reason a new block goes in before the *next* block rather than
        # straight after the previous one.
        front, existing = self._annotated()
        out = text(
            front,
            [("page-1", "One"), ("page-1b", "Inserted"), ("page-2", "Two")],
            existing,
        )

        assert out.index("Re page 1") < out.index("Inserted") < out.index("Two")

    def test_a_page_appended_at_the_end_goes_last(self):
        front, _ = build()
        existing = text(front, [("page-1", "One")])
        out = text(front, [("page-1", "One"), ("page-2", "Two")], existing)

        assert out.index("One") < out.index("Two")

    def test_free_text_is_never_rewritten(self):
        front, existing = self._annotated()
        out = text(front, [("page-1", "One v2"), ("page-2", "Two v2")], existing)

        assert "Re page 1: see [[DMBOK]]." in out


class TestABlockThisRunDidNotGenerate:
    """A page that failed must not erase the commentary underneath it."""

    def test_it_is_left_exactly_as_it_was(self):
        front, _ = build()
        existing = text(front, [("page-1", "One"), ("page-2", "Two")])
        out = text(front, [("page-1", "One v2")], existing)

        assert "Two" in out
        assert "One v2" in out

    def test_the_user_is_told(self):
        front, _ = build()
        existing = text(front, [("page-1", "One"), ("page-2", "Two")])
        _, warnings = notemerge.render(front, [("page-1", "One v2")], existing)

        assert any("page-2" in w and "not part of this sync" in w for w in warnings)


class TestAnEditInsideABlock:
    """A mistake, but not one that should cost the user anything."""

    def _edited(self):
        front, _ = build()
        existing = text(front, [("page-1", "Machine text.")])
        return front, existing.replace("Machine text.", "Machine text. And mine.")

    def test_both_versions_survive(self):
        front, edited = self._edited()
        out = text(front, [("page-1", "Machine text.")], edited)

        assert "Machine text. And mine." in out
        assert out.count("Machine text.") == 2

    def test_the_users_version_is_parked_below_as_free_text(self):
        front, edited = self._edited()
        out = text(front, [("page-1", "Machine text.")], edited)

        assert notemerge.PARKED_PREFIX in out
        assert out.index(end("page-1")) < out.index("And mine.")

    def test_the_parked_copy_is_not_reclaimed_on_the_next_sync(self):
        front, edited = self._edited()
        once = text(front, [("page-1", "Machine text.")], edited)
        twice = text(front, [("page-1", "Machine text.")], once)

        assert twice == once
        assert twice.count("And mine.") == 1

    def test_the_user_is_told(self):
        front, edited = self._edited()
        _, warnings = notemerge.render(front, [("page-1", "Machine text.")], edited)

        assert any("page-1" in w and "edited inside" in w for w in warnings)

    def test_an_untouched_block_is_not_parked(self):
        front, _ = build()
        existing = text(front, [("page-1", "Machine text.")])
        out, warnings = notemerge.render(front, [("page-1", "Newer text.")], existing)

        assert notemerge.PARKED_PREFIX not in out
        assert warnings == []


class TestParseSegments:
    """Turning a note body back into ordered segments."""

    def test_a_body_with_nothing_of_ours(self):
        assert notemerge.parse_segments("plain") == [notemerge.Segment(content="plain")]

    def test_a_block_is_recognised_with_its_digest(self):
        body = f"a\n{begin()} h=abc123 -->\nmine\n{end()}\nb"
        segments = notemerge.parse_segments(body)

        assert [s.block_id for s in segments] == [None, "page-1", None]
        assert segments[1].content == "mine"
        assert segments[1].written_hash == "abc123"

    def test_a_block_without_a_digest_is_still_a_block(self):
        segments = notemerge.parse_segments(f"{begin()} -->\nmine\n{end()}")
        assert segments[0].block_id == "page-1"
        assert segments[0].written_hash is None

    def test_an_unterminated_block_is_kept_as_text(self):
        # A begin with no end means something truncated the file. Keeping every
        # line is the only answer that cannot lose a sentence.
        segments = notemerge.parse_segments(f"a\n{begin()} -->\nhalf")
        assert [s.block_id for s in segments] == [None]
        assert "half" in segments[0].content

    def test_segments_round_trip(self):
        body = f"above\n\n{begin()} h=abc123 -->\nmine\n{end()}\n\nbelow"
        segments = notemerge.parse_segments(body)
        assert notemerge.parse_segments(notemerge.render_segments(segments)) == segments

    def test_a_duplicated_id_is_reported_and_only_the_first_is_updated(self):
        segments = notemerge.parse_segments(
            f"{begin()} -->\nfirst\n{end()}\n{begin()} -->\nsecond\n{end()}"
        )
        merged, warnings = notemerge.merge_segments(segments, [("page-1", "fresh")])

        assert [s.content for s in merged if s.block_id] == ["fresh", "second"]
        assert any("more than once" in w for w in warnings)


class TestRenderAdoptsOlderNotes:
    """Notes written before blocks existed have to be handled without guessing."""

    def test_a_generated_note_without_blocks_is_regenerated(self):
        """`source: Remarkable/` means Living Ink wrote every line of it."""
        existing = (
            "---\ncreated: 2020-01-01\nsource: Remarkable/Work/Notes\n---\n\nOld transcript.\n"
        )
        front, _ = build()
        out = text(front, [("page-1", "New transcript.")], existing)

        assert "Old transcript." not in out
        assert "New transcript." in out
        assert begin() in out

    def test_a_foreign_note_is_kept_above_the_blocks(self):
        """A file that is not ours must survive a name collision intact."""
        existing = "# Somebody else's note\n\nImportant.\n"
        front, _ = build()
        out = text(front, [("page-1", "Transcript.")], existing)

        assert "Somebody else's note" in out
        assert "Important." in out
        assert out.index("Important.") < out.index(begin())

    def test_a_foreign_note_keeps_its_frontmatter(self):
        existing = "---\nauthor: Someone\n---\n\nTheir words.\n"
        front, _ = build()
        out = text(front, [("page-1", "Transcript.")], existing)

        assert "author: Someone" in out
        assert "Their words." in out

    def test_an_empty_file_is_treated_as_new(self):
        front, _ = build()
        out = text(front, [("page-1", "Transcript.")], "")
        assert out.count("living-ink:begin") == 1


class TestFrontmatterList:
    """Reading a list key back, so ``tags`` can be merged instead of replaced."""

    def test_the_block_sequence_living_ink_writes(self):
        lines = ["tags:", "  - remarkable", "  - todo", "created: x"]
        assert notemerge.frontmatter_list(lines, "tags") == ["remarkable", "todo"]

    def test_the_inline_flow_a_user_may_have_typed(self):
        assert notemerge.frontmatter_list(["tags: [remarkable, todo]"], "tags") == [
            "remarkable",
            "todo",
        ]

    def test_a_single_scalar_value(self):
        assert notemerge.frontmatter_list(["tags: todo"], "tags") == ["todo"]

    def test_quotes_are_stripped(self):
        assert notemerge.frontmatter_list(["tags:", '  - "todo"'], "tags") == ["todo"]

    def test_another_keys_list_is_not_read(self):
        lines = ["aliases:", "  - Standup", "tags:", "  - todo"]
        assert notemerge.frontmatter_list(lines, "tags") == ["todo"]

    def test_a_missing_key_is_empty(self):
        assert notemerge.frontmatter_list(["created: x"], "tags") == []

    def test_an_empty_list_is_empty(self):
        assert notemerge.frontmatter_list(["tags:", "created: x"], "tags") == []


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


class TestDividerNewlineSeparation:
    """A horizontal divider immediately under an HTML comment must have a blank line."""

    def test_divider_has_blank_line_after_begin_marker(self):
        segment = notemerge.Segment(block_id="page-1", content="---\n\nHeader content")
        rendered = notemerge.render_segments([segment])
        expected = f"{begin()} -->\n\n---\n\nHeader content\n{end()}"
        assert rendered == expected

    def test_divider_segments_round_trip(self):
        segment = notemerge.Segment(block_id="page-1", content="---\n\nHeader content")
        rendered = notemerge.render_segments([segment])
        round_tripped = notemerge.parse_segments(rendered)
        assert round_tripped == [segment]

    def test_non_divider_has_no_extra_blank_line(self):
        segment = notemerge.Segment(block_id="page-1", content="# Title\n\nBody")
        rendered = notemerge.render_segments([segment])
        expected = f"{begin()} -->\n# Title\n\nBody\n{end()}"
        assert rendered == expected
