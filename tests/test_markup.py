"""Tests for the canonical block vocabulary and the writer contract."""

import pytest

from living_ink.core.document import Page
from living_ink.destinations.markup import (
    Block,
    BlockKind,
    Degradation,
    MarkupWriter,
    ObsidianWriter,
    WriteContext,
    image_block,
    to_blocks,
)
from living_ink.settings import Settings
from tests.builders import make_page
from tests.fakes import PlainTextWriter


def render(text, ctx=None):
    """Parse canonical text and render it back as Obsidian Markdown.

    Args:
        text: Canonical body text.
        ctx: Optional write context.

    Returns:
        ``(body, degradations)``.
    """
    return ObsidianWriter().render(to_blocks(text), ctx)


class TestParsing:
    """to_blocks recognises every kind the canonical dialect can contain."""

    def test_empty_text_is_no_blocks(self):
        assert to_blocks("") == ()
        assert to_blocks("   \n\n  ") == ()

    def test_headings_carry_their_level(self):
        blocks = to_blocks("# One\n\n### Three")
        assert [(b.kind, b.text, b.level) for b in blocks] == [
            (BlockKind.HEADING, "One", 1),
            (BlockKind.HEADING, "Three", 3),
        ]

    def test_blank_lines_separate_paragraphs(self):
        blocks = to_blocks("First para\nstill first\n\nSecond para")
        assert [b.text for b in blocks] == ["First para\nstill first", "Second para"]

    def test_a_fenced_block_keeps_its_language_and_its_blank_lines(self):
        blocks = to_blocks("```python\nx = 1\n\ny = 2\n```")
        assert blocks[0].kind is BlockKind.CODE
        assert blocks[0].text == "x = 1\n\ny = 2"
        assert blocks[0].attrs["language"] == "python"

    def test_an_unclosed_fence_runs_to_the_end_rather_than_swallowing_nothing(self):
        blocks = to_blocks("```\nstill code")
        assert blocks[0].kind is BlockKind.CODE
        assert blocks[0].text == "still code"

    def test_display_math_is_its_own_kind(self):
        blocks = to_blocks("$$\nE = mc^2\n$$")
        assert blocks[0].kind is BlockKind.MATH
        assert blocks[0].text == "E = mc^2"
        assert blocks[0].attrs["display"] is True

    @pytest.mark.parametrize("line", ["---", "***", "___", "- - -", "  ---"])
    def test_dividers(self, line):
        assert to_blocks(line)[0].kind is BlockKind.DIVIDER

    def test_a_pipe_table_is_kept_whole(self):
        table = "| a | b |\n| --- | --- |\n| 1 | 2 |"
        blocks = to_blocks(table)
        assert len(blocks) == 1
        assert blocks[0].kind is BlockKind.TABLE
        assert blocks[0].text == table

    def test_a_plain_blockquote_is_a_quote(self):
        blocks = to_blocks("> line one\n> line two")
        assert blocks[0].kind is BlockKind.QUOTE
        assert blocks[0].text == "line one\nline two"

    def test_a_callout_is_not_mistaken_for_a_blockquote(self):
        # The whole reason to_blocks is not a stock CommonMark parser: the
        # prompt file emits these, and CommonMark loses the type.
        blocks = to_blocks("> [!warning] Careful\n> body text")
        assert blocks[0].kind is BlockKind.CALLOUT
        assert blocks[0].text == "Careful"
        assert blocks[0].attrs["callout_type"] == "warning"
        assert blocks[0].children[0].text == "body text"

    def test_a_callout_type_is_lowercased_and_a_title_is_optional(self):
        blocks = to_blocks("> [!NOTE]\n> body")
        assert blocks[0].attrs["callout_type"] == "note"
        assert blocks[0].text == ""

    def test_a_foldable_callout_marker_is_still_a_callout(self):
        assert to_blocks("> [!tip]- Folded\n> body")[0].attrs["callout_type"] == "tip"

    def test_a_callout_nests_whatever_is_inside_it(self):
        blocks = to_blocks("> [!info] Head\n> - one\n> - two")
        assert [c.kind for c in blocks[0].children] == [BlockKind.LIST, BlockKind.LIST]


class TestListParsing:
    """Lists are the only kind with structure, so they get their own class."""

    def test_bullets_are_siblings(self):
        blocks = to_blocks("- one\n- two")
        assert [b.text for b in blocks] == ["one", "two"]
        assert all(b.kind is BlockKind.LIST and b.level == 0 for b in blocks)

    def test_indentation_becomes_nesting(self):
        blocks = to_blocks("- parent\n  - child\n    - grandchild\n- sibling")
        assert len(blocks) == 2
        child = blocks[0].children[0]
        assert child.text == "child"
        assert child.children[0].text == "grandchild"
        assert blocks[1].text == "sibling"

    def test_an_ordered_item_keeps_the_number_it_was_written_with(self):
        # A transcribed page can legitimately start at 3. Renumbering from 1
        # would silently contradict what the user wrote.
        blocks = to_blocks("3. third\n4. fourth")
        assert [b.attrs["number"] for b in blocks] == [3, 4]
        assert all(b.attrs["ordered"] for b in blocks)

    @pytest.mark.parametrize("line", ["1. item", "1) item"])
    def test_both_ordered_delimiters(self, line):
        assert to_blocks(line)[0].attrs["ordered"] is True

    def test_a_checkbox_is_a_task_not_a_bullet(self):
        blocks = to_blocks("- [ ] todo\n- [x] done\n- [X] also done")
        assert all(b.kind is BlockKind.TASK_LIST for b in blocks)
        assert [b.attrs["checked"] for b in blocks] == [False, True, True]

    def test_a_task_nested_under_a_bullet_keeps_its_kind(self):
        blocks = to_blocks("- parent\n  - [x] done")
        assert blocks[0].children[0].kind is BlockKind.TASK_LIST


class TestObsidianWriter:
    """The canonical dialect is Obsidian's, so rendering is near-identity."""

    @pytest.mark.parametrize(
        "text",
        [
            "# Heading",
            "Just a paragraph.",
            "- one\n- two\n  - nested",
            "1. first\n2. second",
            "- [ ] todo\n- [x] done",
            "> quoted line",
            "> [!warning] Careful\n> body text",
            "```python\nx = 1\n```",
            "$$\nE = mc^2\n$$",
            "---",
            "| a | b |\n| --- | --- |\n| 1 | 2 |",
        ],
    )
    def test_every_kind_round_trips_unchanged(self, text):
        body, degradations = render(text)
        assert body == text
        assert degradations == ()

    def test_a_whole_page_round_trips(self):
        source = (
            "# Title\n\nIntro with **bold**.\n\n- one\n- two\n  - nested\n\n"
            "1. first\n2. second\n\n- [ ] todo\n\n> [!note] Head\n> body\n\n"
            "```sh\nls\n```\n\n---"
        )
        body, degradations = render(source)
        assert body == source
        assert degradations == ()
        # And it is a fixed point: rendering the output again changes nothing.
        assert render(body)[0] == body

    def test_sibling_items_are_not_separated_into_a_loose_list(self):
        # assemble() joins parts with a blank line. A blank line between two
        # items is what makes Markdown render a list loose, wrapping every
        # item in its own paragraph, so sibling items must be one part.
        body, _ = render("- one\n- two\n- three")
        assert "\n\n" not in body

    def test_a_bullet_list_under_a_numbered_one_stays_two_lists(self):
        body, _ = render("1. first\n\n- bullet")
        assert body == "1. first\n\n- bullet"

    def test_a_heading_deeper_than_six_is_clamped(self):
        block = Block(kind=BlockKind.HEADING, text="Deep", level=9)
        body, _ = ObsidianWriter().render([block])
        assert body == "###### Deep"

    def test_a_heading_with_no_level_is_still_a_heading(self):
        block = Block(kind=BlockKind.HEADING, text="Flat", level=0)
        assert ObsidianWriter().render([block])[0] == "# Flat"

    def test_empty_parts_are_dropped_rather_than_left_as_blank_gaps(self):
        blocks = [
            Block(kind=BlockKind.PARAGRAPH, text="one"),
            Block(kind=BlockKind.PARAGRAPH, text="   "),
            Block(kind=BlockKind.PARAGRAPH, text="two"),
        ]
        assert ObsidianWriter().render(blocks)[0] == "one\n\ntwo"


class TestImages:
    """An image block names a page; the destination says where the file went."""

    def test_the_block_holds_the_page_not_a_path(self):
        page = make_page(2, "text", label="Page 2")
        block = image_block(page)
        assert block.kind is BlockKind.IMAGE
        assert block.text == "Page 2"
        assert block.attrs["page"] is page

    def test_the_resolver_decides_the_link_target(self):
        page = make_page(1, "text", label="Page 1")
        ctx = WriteContext(resolve_attachment=lambda p: f"attachments/{p.label}.png")
        body, _ = ObsidianWriter().render([image_block(page)], ctx)
        assert body == "![[attachments/Page 1.png|Page 1]]"

    @pytest.mark.parametrize(
        "embed,expected",
        [(True, "![[img.png|Page 1]]"), (False, "- [[img.png|Page 1]]")],
    )
    def test_embedding_is_a_setting_not_a_hardcoded_bang(self, embed, expected):
        # This is the bug the writer exists to fix: the link used to be
        # hand-built as "- [[target|label]]", so every rendered page landed as
        # a bulleted list of links rather than as an image, and
        # obsidian.embed_images was a setting nothing read.
        ctx = WriteContext(
            resolve_attachment=lambda p: "img.png",
            settings=Settings(obsidian_embed_images=embed),
        )
        body, _ = ObsidianWriter().render([image_block(make_page(1, label="Page 1"))], ctx)
        assert body == expected

    def test_a_page_with_no_label_gets_a_bare_link(self):
        page = Page(index=0, number=1, label="")
        ctx = WriteContext(resolve_attachment=lambda p: "img.png")
        assert ObsidianWriter().render([image_block(page)], ctx)[0] == "![[img.png]]"

    def test_no_resolver_is_not_a_crash(self):
        # The default context exists so a text-only writer can call render()
        # with nothing to say. An image then has no target, but the run lives.
        body, _ = ObsidianWriter().render([image_block(make_page(1, "t", label="P"))])
        assert body == "![[|P]]"


class TestDegradation:
    """The contract's central promise: a writer can never lose content quietly.

    ObsidianWriter handles all eleven kinds and so never reaches degrade().
    PlainTextWriter is what proves the path works before a second real writer
    depends on it.
    """

    def test_the_one_handled_kind_is_rendered_natively(self):
        body, degradations = PlainTextWriter().render(to_blocks("Just words."))
        assert body == "Just words."
        assert degradations == ()

    def test_an_unhandled_kind_is_reported_not_dropped(self):
        body, degradations = PlainTextWriter().render(to_blocks("# Title"))
        assert body == "Title"
        assert len(degradations) == 1
        assert degradations[0].kind is BlockKind.HEADING
        assert degradations[0].lossy is False

    def test_content_survives_even_when_the_kind_does_not(self):
        body, _ = PlainTextWriter().render(to_blocks("# Title\n\nBody.\n\n> quoted"))
        assert "Title" in body and "Body." in body and "quoted" in body

    def test_forty_callouts_warn_once(self):
        source = "\n\n".join(f"> [!note] {n}\n> body" for n in range(40))
        _, degradations = PlainTextWriter().render(to_blocks(source))
        assert len(degradations) == 1

    def test_distinct_losses_are_reported_distinctly(self):
        _, degradations = PlainTextWriter().render(to_blocks("# Title\n\n| a |\n| - |"))
        assert {d.kind for d in degradations} == {BlockKind.HEADING, BlockKind.TABLE}

    def test_lossy_is_a_separate_fact_from_degraded(self):
        # A divider becoming dashes is a restyling; a table flattened to its
        # cell text has actually lost the user something.
        _, degradations = PlainTextWriter().render(to_blocks("---\n\n| a |\n| - |"))
        by_kind = {d.kind: d for d in degradations}
        assert by_kind[BlockKind.DIVIDER].lossy is False
        assert by_kind[BlockKind.TABLE].lossy is True

    def test_a_degradation_describes_itself_for_a_warning_line(self):
        plain = Degradation(kind=BlockKind.CALLOUT, rendered_as="a blockquote", lossy=False)
        lossy = Degradation(kind=BlockKind.TABLE, rendered_as="its cell text", lossy=True)
        assert plain.describe() == "callout became a blockquote."
        assert lossy.describe() == "table became its cell text, losing detail."


class TestTheWriterContract:
    """What the base class guarantees, independent of any one writer."""

    def test_a_writer_that_claims_a_kind_with_no_method_raises(self):
        # Silently dropping the block would be the failure mode the contract
        # exists to prevent, so this is loud and it is at render time.
        class Liar(MarkupWriter):
            handles = frozenset({BlockKind.HEADING})

        with pytest.raises(NotImplementedError, match="has no heading"):
            Liar().render(to_blocks("# Title"))

    def test_a_writer_that_declines_a_kind_and_never_says_what_it_did_raises(self):
        class Silent(MarkupWriter):
            handles = frozenset()

        with pytest.raises(NotImplementedError, match="cannot render paragraph"):
            Silent().render(to_blocks("words"))

    def test_obsidian_handles_every_kind_there_is_today(self):
        assert ObsidianWriter.handles == frozenset(BlockKind)

    def test_every_kind_a_writer_claims_has_a_method(self):
        # The guarantee behind spelling `handles` out rather than deriving it
        # from frozenset(BlockKind): a derived set claims a newly added kind
        # and then fails at render time with no method for it.
        for writer in (ObsidianWriter, PlainTextWriter):
            for kind in writer.handles:
                assert callable(getattr(writer, kind.value, None)), (
                    f"{writer.__name__} claims {kind.value} with no method"
                )

    def test_rendering_no_blocks_is_an_empty_body_not_a_crash(self):
        assert ObsidianWriter().render([]) == ("", ())
