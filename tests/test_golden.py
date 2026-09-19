"""Golden files: the rendered note is the product.

Every other test asserts that one substring is present somewhere. These compare
the whole file, byte for byte, so a change nobody meant to make — a reordered
frontmatter key, a lost blank line, a block that drifted one position — shows
up as a diff instead of passing.

Six files in ``tests/golden/``. One is a note published for the first time,
and it is the fixed-``Document`` golden §17.3 asks for: frontmatter ordering,
the ``created``/``updated``/``synced`` cascade, attachment links, tag
sanitisation, and a callout surviving the round trip. The other five are the
ways a second sync can meet a note the user has since written in, one per
property in §10.6 — text between blocks, a page inserted mid-note, a page
updated in place, a page absent from the run, and an edit inside a block.

Regenerate after an intentional change with::

    LIVING_INK_UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py

then read the diff before committing it. A golden file regenerated without
being read is not an expectation, it is a transcript of whatever the code did
that day.
"""

import datetime
import difflib
import os
import re
from pathlib import Path
from types import SimpleNamespace
from typing import List, Sequence

import pytest
from PIL import Image

from living_ink.core.document import Page
from living_ink.destinations import ObsidianDestination
from living_ink.destinations import obsidian as obsidian_module
from tests.builders import make_context, make_document, make_page

#: Where the expected notes live.
GOLDEN_DIR = Path(__file__).parent / "golden"

#: The day every golden file was rendered on, so ``synced:`` is not a clock.
TODAY = datetime.date(2026, 3, 14)

#: What the tablet says about the notebook, which is a different day again.
MODIFIED = "2026-03-02"


class _FrozenDate(datetime.date):
    """``datetime.date`` whose ``today()`` is the day the goldens were made."""

    @classmethod
    def today(cls) -> datetime.date:
        """Return the pinned date.

        Returns:
            :data:`TODAY`.
        """
        return TODAY


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch):
    """Pin the one clock read in the destination.

    ``synced:`` is today by definition, so without this every golden file
    would fail the day after it was written.
    """
    monkeypatch.setattr(
        obsidian_module,
        "datetime",
        SimpleNamespace(date=_FrozenDate, datetime=datetime.datetime),
    )


@pytest.fixture
def renders(tmp_path) -> List[Path]:
    """Three rendered page images, one per page the fixtures can use.

    Returns:
        The PNG paths, in page order.
    """
    directory = tmp_path / "renders"
    directory.mkdir()
    paths = []
    for number in (1, 2, 3):
        path = directory / f"page-{number}.png"
        Image.new("L", (8, 8), color=255 - number).save(path)
        paths.append(path)
    return paths


@pytest.fixture
def vault(tmp_path) -> ObsidianDestination:
    """An Obsidian destination pointed at an empty vault.

    Returns:
        The destination.
    """
    root = tmp_path / "vault"
    root.mkdir()
    return ObsidianDestination(vault_path=str(root))


# The text on each page. Between them they exercise nine of the eleven block
# kinds, including the callout, which is the one element that is Obsidian's
# own dialect rather than CommonMark.
PAGE_TEXT = {
    1: (
        "# Standup\n"
        "\n"
        "Shipped the exporter. Still chasing the cache key.\n"
        "\n"
        "- Ship the exporter\n"
        "- Fix the cache key\n"
        "\n"
        "> [!warning] Careful\n"
        "> The tablet is read-only.\n"
    ),
    2: (
        "## Decisions\n"
        "\n"
        "1. Keep the digest content-only\n"
        "2. Park an edited block\n"
        "\n"
        "> A page is the merge unit.\n"
        "\n"
        "```python\n"
        "merge(merge(x)) == merge(x)\n"
        "```\n"
    ),
    3: "## Follow-ups\n\n- [ ] Write the golden files\n- [x] Read the diff\n",
}


def pages(numbers: Sequence[int], renders: Sequence[Path], **overrides: str) -> List[Page]:
    """Build the fixed pages, optionally rewriting one page's text.

    Args:
        numbers: Which page numbers the run produced.
        renders: The page images, indexed by page number minus one.
        **overrides: ``page2="..."`` style replacements for a page's text.

    Returns:
        The pages, in order.
    """
    return [
        make_page(
            number=number,
            text=overrides.get(f"page{number}", PAGE_TEXT[number]),
            image=renders[number - 1],
        )
        for number in numbers
    ]


def meeting_notes(page_list: Sequence[Page]):
    """Build the one document every golden file renders.

    Args:
        page_list: The pages this run produced.

    Returns:
        The document.
    """
    return make_document(
        title="Meeting Notes",
        # "Project Alpha" has to come out sanitised; "meeting" is already a
        # tag and must not be duplicated by the type tag.
        tags=("Project Alpha", "meeting"),
        folder=("Work", "2026 Q1"),
        doc_id="doc-golden",
        modified=MODIFIED,
        pages=list(page_list),
    )


def publish(vault: ObsidianDestination, page_list: Sequence[Page], **ctx_kwargs):
    """Publish the fixed document and hand back the result and the note.

    Args:
        vault: The destination.
        page_list: The pages this run produced.
        **ctx_kwargs: Passed to :func:`tests.builders.make_context`.

    Returns:
        A ``(result, note_path)`` pair.
    """
    result = vault.publish(meeting_notes(page_list), make_context("doc-golden", **ctx_kwargs))
    assert result.ok, result.detail
    return result, vault.vault_path / result.target


def assert_golden(name: str, note: Path) -> None:
    """Compare a rendered note against its golden file.

    Args:
        name: File name inside ``tests/golden/``.
        note: The note that was just written.

    Raises:
        AssertionError: The note and the golden file differ, or the golden
            file does not exist.
    """
    actual = note.read_text(encoding="utf-8")
    path = GOLDEN_DIR / name

    if os.environ.get("LIVING_INK_UPDATE_GOLDEN"):
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8")
        return

    assert path.exists(), f"No golden file at {path}. Regenerate with LIVING_INK_UPDATE_GOLDEN=1."
    expected = path.read_text(encoding="utf-8")
    if expected != actual:
        diff = "\n".join(
            difflib.unified_diff(
                expected.splitlines(),
                actual.splitlines(),
                fromfile=f"golden/{name}",
                tofile="rendered",
                lineterm="",
            )
        )
        pytest.fail(f"The rendered note no longer matches {name}:\n{diff}")


def rewrite(note: Path, old: str, new: str) -> None:
    """Stand in for the user editing the note in Obsidian.

    Args:
        note: The note on disk.
        old: The text to find. Must appear exactly once.
        new: What to put there instead.

    Raises:
        AssertionError: The text is missing or ambiguous.
    """
    text = note.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"{old!r} appears {text.count(old)} times, expected once"
    note.write_text(text.replace(old, new), encoding="utf-8")


# =========================================================================
# The note itself
# =========================================================================


class TestAFreshNote:
    """The whole file, published into an empty vault."""

    def test_the_rendered_note(self, vault, renders):
        """Frontmatter, dates, attachment links, tags, and the callout."""
        _, note = publish(vault, pages([1, 2], renders))

        assert_golden("fresh_note.md", note)

    def test_the_attachments_landed_where_the_links_point(self, vault, renders):
        """A golden file full of dead links would still pass the diff."""
        _, note = publish(vault, pages([1, 2], renders))

        targets = re.findall(r"!\[\[([^\]|]+)", note.read_text(encoding="utf-8"))
        assert len(targets) == 2
        for target in targets:
            assert (vault.vault_path / target).exists(), target

    def test_created_comes_from_the_state_store_not_today(self, vault, renders):
        """``first_published`` outranks the tablet's date and today alike."""
        _, note = publish(vault, pages([1], renders), first_published="2025-11-08")

        assert "created: 2025-11-08" in note.read_text(encoding="utf-8")


# =========================================================================
# The five ways a second sync meets a note the user has touched
# =========================================================================


class TestASecondSync:
    """One golden file per property in §10.6."""

    def test_text_between_blocks_is_preserved(self, vault, renders):
        """Property 1: free text is never written to."""
        _, note = publish(vault, pages([1, 2], renders))
        rewrite(
            note,
            "<!-- living-ink:end page-1 -->\n",
            "<!-- living-ink:end page-1 -->\n\nI disagree with all of this.\n",
        )

        _, note = publish(vault, pages([1, 2], renders))

        assert_golden("text_between_blocks.md", note)

    def test_a_new_page_lands_before_the_next_block(self, vault, renders):
        """A page that appeared mid-notebook goes where it belongs.

        Not straight after page 1, which would wedge it between a page and
        the user's commentary on that page, but before page 3.
        """
        _, note = publish(vault, pages([1, 3], renders))
        rewrite(
            note,
            "<!-- living-ink:end page-1 -->\n",
            "<!-- living-ink:end page-1 -->\n\nMy note on the standup.\n",
        )

        _, note = publish(vault, pages([1, 2, 3], renders))

        assert_golden("page_inserted.md", note)

    def test_a_page_rewritten_on_the_tablet_is_rewritten_here(self, vault, renders):
        """The block is replaced in place, digest and all."""
        publish(vault, pages([1, 2], renders))

        _, note = publish(
            vault,
            pages([1, 2], renders, page2="## Decisions\n\nWe changed our minds.\n"),
        )

        assert_golden("page_updated.md", note)

    def test_a_page_missing_from_this_run_is_kept(self, vault, renders):
        """Property 2: a block this run did not generate is not deleted.

        A page absent from a run usually means a failed render, not a deleted
        page, and deleting on that evidence is unrecoverable.
        """
        publish(vault, pages([1, 2], renders))

        result, note = publish(vault, pages([1], renders))

        assert_golden("page_absent.md", note)
        assert any("page-2" in warning for warning in result.warnings), result.warnings

    def test_an_edit_inside_a_block_is_parked(self, vault, renders):
        """Property 3: an edit inside a block is preserved, not overwritten."""
        _, note = publish(vault, pages([1, 2], renders))
        rewrite(note, "Fix the cache key", "Fix the cache key (done, see #41)")

        result, note = publish(vault, pages([1, 2], renders))

        assert_golden("edited_block.md", note)
        assert any("page-1" in warning for warning in result.warnings), result.warnings

        # Parking is a one-off, not a growth rate: the third sync finds the
        # block matching its digest again and leaves the parked copy alone.
        parked_once = note.read_text(encoding="utf-8")
        result, note = publish(vault, pages([1, 2], renders))
        assert note.read_text(encoding="utf-8") == parked_once
        assert result.warnings == ()


class TestTheGoldenFilesAreStable:
    """A second identical sync must not move a byte."""

    def test_resyncing_an_untouched_note_changes_nothing(self, vault, renders):
        """The digest is content-only, so an unedited block looks unedited.

        Were it not, every block would read as user-edited on every sync and
        the note would grow a parked copy of itself each time (§20.3 F4).
        """
        _, note = publish(vault, pages([1, 2], renders))
        first = note.read_text(encoding="utf-8")

        result, note = publish(vault, pages([1, 2], renders))

        assert note.read_text(encoding="utf-8") == first
        assert result.warnings == ()
