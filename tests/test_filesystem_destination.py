"""Tests for the shared publish lifecycle of a file-writing destination.

Exercised through ``ObsidianDestination``, which is the only subclass today.
What is tested here is the base class's promises — the dry-run cut and path
containment — not Obsidian's Markdown, which lives in
``test_destinations.py``.
"""

import os
import stat

import pytest

from living_ink.destinations.filesystem import NoteLayout
from living_ink.destinations.obsidian import ObsidianDestination
from tests.builders import make_both, make_context, make_document, make_page


@pytest.fixture
def vault(tmp_path):
    """An empty vault directory.

    Returns:
        The path.
    """
    path = tmp_path / "vault"
    path.mkdir()
    return path


def files_under(root):
    """List every file in a tree, root-relative and sorted.

    Args:
        root: Directory to walk.

    Returns:
        Sorted posix-style relative paths.
    """
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


class TestTheDryRunCut:
    """Stages 1 and 2 only read, so a dry run can promise a path it never made."""

    def test_nothing_at_all_is_created(self, vault):
        dest = ObsidianDestination(vault_path=str(vault), root_folder="Living Ink")
        doc, ctx = make_both("Meeting", "Notes", folder=["Work", "2026"])

        result = dest.publish(doc, make_context(doc_id=ctx.doc_id, dry_run=True))

        assert result.ok is True
        # The old code's first act was mkdir, so a dry run scattered empty
        # folders across the vault for every notebook it looked at.
        assert list(vault.rglob("*")) == []

    def test_the_exact_path_is_reported_before_it_exists(self, vault):
        dest = ObsidianDestination(vault_path=str(vault), root_folder="Living Ink")
        doc, ctx = make_both("Meeting", "Notes", folder=["Work"])

        dry = dest.publish(doc, make_context(doc_id=ctx.doc_id, dry_run=True))
        real = dest.publish(doc, ctx)

        assert dry.target == "Living Ink/Work/Meeting.md"
        assert dry.target == real.target
        assert "Would write" in dry.detail

    def test_stages_one_and_two_survive_a_read_only_vault(self, vault):
        # The invariant the split exists to protect. If a stage before the cut
        # ever writes, this is the test that fails.
        dest = ObsidianDestination(vault_path=str(vault), root_folder="Living Ink")
        doc, ctx = make_both("Meeting", "Notes")
        original = stat.S_IMODE(vault.stat().st_mode)
        vault.chmod(0o500)
        try:
            if os.access(vault, os.W_OK):  # pragma: no cover - running as root
                pytest.skip("Cannot make a directory read-only for this user.")
            result = dest.publish(doc, make_context(doc_id=ctx.doc_id, dry_run=True))
        finally:
            vault.chmod(original)

        assert result.ok is True
        assert result.target == "Living Ink/Meeting.md"


class TestContainment:
    """A folder name off the tablet is a name, never an instruction."""

    @pytest.mark.parametrize("folder", [["..", "..", ".."], [".."], ["Work", "..", ".."], ["."]])
    def test_a_traversing_folder_name_is_refused_not_sanitized(self, vault, tmp_path, folder):
        # §20.3 F3: _sanitize_filename never touched "." at all, and the
        # containment check ran *after* the file had been written. A tablet
        # folder tree of ../../.. wrote three levels above the vault.
        dest = ObsidianDestination(vault_path=str(vault))
        doc, ctx = make_both("Escape", "Content", folder=folder)

        result = dest.publish(doc, ctx)

        assert result.ok is False
        assert "outside" in result.detail
        assert files_under(vault) == []
        assert not (tmp_path / "Escape.md").exists()

    def test_a_root_folder_that_climbs_out_is_refused(self, vault, tmp_path):
        dest = ObsidianDestination(vault_path=str(vault), root_folder="../elsewhere")
        doc, ctx = make_both("Note", "Content")

        result = dest.publish(doc, ctx)

        assert result.ok is False
        assert files_under(vault) == []
        assert not (tmp_path / "elsewhere").exists()

    def test_the_refusal_happens_before_anything_is_created(self, vault):
        dest = ObsidianDestination(vault_path=str(vault), root_folder="Living Ink")
        doc, ctx = make_both("Note", "Content", folder=["Work", ".."])

        dest.publish(doc, ctx)

        # Not even the root folder, which resolve_location would have to walk
        # through on its way to refusing.
        assert list(vault.rglob("*")) == []

    def test_an_ordinary_folder_with_dots_in_the_name_still_works(self, vault):
        dest = ObsidianDestination(vault_path=str(vault))
        doc, ctx = make_both("Note", "Content", folder=["v1.2.3"])

        result = dest.publish(doc, ctx)

        assert result.ok is True
        assert result.target == "v1.2.3/Note.md"


class TestPublishedExists:
    """Whether the note a state row points at is still on disk."""

    def test_no_recorded_target_is_not_a_note(self, vault):
        dest = ObsidianDestination(vault_path=str(vault))
        assert dest.published_exists(make_context()) is False

    def test_a_recorded_note_that_was_deleted_by_hand(self, vault):
        dest = ObsidianDestination(vault_path=str(vault))
        doc, ctx = make_both("Note", "Content")
        result = dest.publish(doc, ctx)
        recorded = make_context(doc_id=ctx.doc_id, existing_target=result.target)

        assert dest.published_exists(recorded) is True
        (vault / result.target).unlink()
        assert dest.published_exists(recorded) is False


class TestTheStageContract:
    """What the base class guarantees about the sequence itself."""

    def test_a_layout_starts_empty_so_two_documents_share_nothing(self):
        # The state used to live on the destination instance, which meant one
        # note's target could be recorded against another note.
        first, second = NoteLayout(), NoteLayout()
        first.warnings.append("only mine")
        assert second.warnings == []
        assert second.note_path is None

    def test_a_stage_warning_reaches_the_result_on_success(self, vault, monkeypatch):
        dest = ObsidianDestination(vault_path=str(vault))
        original = ObsidianDestination.render_body

        def noisy(self, doc, ctx, layout):
            original(self, doc, ctx, layout)
            layout.warnings.append("something to fix by hand")

        monkeypatch.setattr(ObsidianDestination, "render_body", noisy)
        result = dest.publish(*make_both("Note", "Content"))

        assert result.ok is True
        assert "something to fix by hand" in result.warnings

    def test_a_stage_warning_reaches_the_result_on_failure_too(self, vault, monkeypatch):
        # The run whose warnings matter most is the one that failed.
        dest = ObsidianDestination(vault_path=str(vault))

        def noisy_then_broken(self, doc, ctx, layout):
            layout.warnings.append("said before it broke")
            raise OSError("disk full")

        monkeypatch.setattr(ObsidianDestination, "write_attachments", noisy_then_broken)
        result = dest.publish(*make_both("Note", "Content"))

        assert result.ok is False
        assert "said before it broke" in result.warnings
        assert "disk full" in result.detail

    def test_the_pages_still_land_where_the_body_says_they_do(self, vault, tmp_path):
        # The end-to-end fact the four stages exist to keep true: what
        # write_attachments copied is what render_body linked to.
        image = tmp_path / "page-1.png"
        image.write_bytes(b"png")
        doc = make_document("Note", pages=[make_page(1, "Text", image=image)], folder=["Work"])
        dest = ObsidianDestination(vault_path=str(vault), root_folder="Ink")

        result = dest.publish(doc, make_context(doc_id=doc.doc_id))

        body = (vault / result.target).read_text(encoding="utf-8")
        link = "Ink/_attachments/Work/Note/page-1.png"
        assert f"![[{link}|Page 1]]" in body
        assert (vault / link).is_file()
