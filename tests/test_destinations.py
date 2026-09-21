"""Tests for living_ink.destinations module.

Covers the Destination abstract base class and ObsidianDestination,
including full folder mirroring, root folder configuration, attachment
handling, and filename sanitization.
"""

import datetime
import re
from dataclasses import replace
from unittest.mock import patch

import pytest

from living_ink import notemerge, safeio
from living_ink.core.document import PublishResult
from living_ink.destinations import (
    Destination,
    DestinationStatus,
    ObsidianDestination,
)
from living_ink.destinations.markup import BlockKind, to_blocks
from tests.builders import make_both, make_context, make_page
from tests.fakes import PlainTextWriter

# =========================================================================
# Destination (ABC)
# =========================================================================


class TestDestinationABC:
    """Tests for the Destination abstract base class contract."""

    def test_cannot_instantiate_directly(self):
        """Destination cannot be instantiated without subclassing."""
        with pytest.raises(TypeError):
            Destination()

    def test_subclass_must_implement_publish(self):
        """Subclass missing publish raises TypeError."""

        class Incomplete(Destination):
            pass

        with pytest.raises(TypeError):
            Incomplete()

    def test_valid_subclass_works(self):
        """A complete subclass can be instantiated and called."""

        class Complete(Destination):
            def check(self):
                return DestinationStatus(ok=True, detail="ready")

            def publish(self, doc, ctx):
                return PublishResult(ok=True)

        dest = Complete()
        assert dest.publish(*make_both("Test", "Content")).ok is True


# =========================================================================
# ObsidianDestination — initialization and options
# =========================================================================


class TestObsidianDestinationInit:
    """Tests for ObsidianDestination initialization and configuration."""

    def test_valid_vault_path(self, tmp_path):
        """Valid existing directory initializes correctly."""
        dest = ObsidianDestination(vault_path=str(tmp_path))
        assert dest.vault_path == tmp_path.resolve()
        assert dest.attachments_folder == "_attachments"
        assert dest.root_folder == ""
        assert dest.mirror_folders is True

    def test_a_nonexistent_vault_still_constructs(self, tmp_path):
        """It used to raise, and the run then exited 0 having published nothing."""
        nonexistent = tmp_path / "does_not_exist"
        dest = ObsidianDestination(vault_path=str(nonexistent))

        assert dest.vault_path == nonexistent.resolve()

    def test_custom_options(self, tmp_path):
        """Custom configuration options are stored properly."""
        dest = ObsidianDestination(
            vault_path=str(tmp_path),
            attachments_folder="media",
            root_folder="Living Ink",
            mirror_folders=False,
        )
        assert dest.attachments_folder == "media"
        assert dest.root_folder == "Living Ink"
        assert dest.mirror_folders is False

    def test_strips_whitespace_from_folders(self, tmp_path):
        """Whitespace is stripped from folder options."""
        dest = ObsidianDestination(
            vault_path=str(tmp_path),
            attachments_folder="  images  ",
            root_folder="  Notes  ",
        )
        assert dest.attachments_folder == "images"
        assert dest.root_folder == "Notes"


# =========================================================================
# ObsidianDestination — _sanitize_filename
# =========================================================================


class TestObsidianSanitizeFilename:
    """Tests for filename sanitization."""

    @pytest.fixture
    def dest(self, tmp_path):
        """Provide an ObsidianDestination instance for testing."""
        return ObsidianDestination(vault_path=str(tmp_path))

    def test_replaces_illegal_characters(self, dest):
        """Forbidden characters are replaced with hyphens."""
        raw = 'My:Special/File\\Name*With?Illegal"Chars<And>Pipes|Tags#Carats^Brackets[1]'
        sanitized = dest._sanitize_filename(raw)
        for char in ["/", "\\", ":", "*", "?", '"', "<", ">", "|", "#", "^", "[", "]"]:
            assert char not in sanitized

    def test_preserves_regular_spaces(self, dest):
        """Normal spaces between words are preserved."""
        assert dest._sanitize_filename("Meeting Notes 2026") == "Meeting Notes 2026"

    def test_collapses_consecutive_dashes(self, dest):
        """Multiple consecutive dashes are collapsed into one."""
        assert dest._sanitize_filename("Work----Projects") == "Work-Projects"
        assert dest._sanitize_filename("Work // Projects") == "Work - Projects"

    def test_empty_string_fallback(self, dest):
        """Empty or all-illegal strings fall back to 'Untitled'."""
        assert dest._sanitize_filename("") == "Untitled"
        assert dest._sanitize_filename("///:::***") == "Untitled"


# =========================================================================
# ObsidianDestination — publish & folder structure
# =========================================================================


class TestObsidianPublishFolders:
    """Tests for publishing notes with various folder hierarchies."""

    def test_publish_vault_root(self, tmp_path):
        """Note is published directly in vault root when no folders set."""
        dest = ObsidianDestination(vault_path=str(tmp_path))
        success = dest.publish(*make_both("Daily Note", "Some text"))

        assert success.ok is True
        note_file = tmp_path / "Daily Note.md"
        assert note_file.exists()
        content = note_file.read_text(encoding="utf-8")
        assert "Some text" in content
        assert "source: Remarkable/Daily Note" in content

    def test_publish_with_root_folder(self, tmp_path):
        """Note is placed under root_folder when specified."""
        dest = ObsidianDestination(vault_path=str(tmp_path), root_folder="Living Ink")
        success = dest.publish(*make_both("Quick Note", "Quick thoughts"))

        assert success.ok is True
        note_file = tmp_path / "Living Ink" / "Quick Note.md"
        assert note_file.exists()
        assert "Quick thoughts" in note_file.read_text(encoding="utf-8")

    def test_publish_with_nested_subfolder(self, tmp_path):
        """Full reMarkable folder hierarchy is mirrored in Obsidian."""
        dest = ObsidianDestination(vault_path=str(tmp_path), root_folder="Living Ink")
        success = dest.publish(
            *make_both("Roadmap", "Q1 plans", folder=("Work", "Projects", "2026"))
        )

        assert success.ok is True
        note_file = tmp_path / "Living Ink" / "Work" / "Projects" / "2026" / "Roadmap.md"
        assert note_file.exists()
        content = note_file.read_text(encoding="utf-8")
        assert "Q1 plans" in content
        assert "source: Remarkable/Work/Projects/2026/Roadmap" in content

    def test_the_folder_comes_from_the_folder_not_from_the_title(self, tmp_path):
        """A title is a title, even one that reads like a path.

        The title and the folder used to arrive glued together as
        ``"Work / Finance / Budget 2026"`` and be split apart again here, so a
        notebook genuinely called ``"Q1 / Q2"`` was filed in a folder named
        ``Q1`` and lost half its name.
        """
        dest = ObsidianDestination(vault_path=str(tmp_path), root_folder="Living Ink")
        success = dest.publish(*make_both("Q1 / Q2", "Financial summary", folder=("Work",)))

        assert success.ok is True
        note_file = tmp_path / "Living Ink" / "Work" / "Q1 - Q2.md"
        assert note_file.exists()
        assert not (tmp_path / "Living Ink" / "Work" / "Q1").exists()
        content = note_file.read_text(encoding="utf-8")
        assert "source: Remarkable/Work/Q1 / Q2" in content

    def test_publish_mirror_folders_false_flat_mode(self, tmp_path):
        """When mirror_folders is False, notes are placed flat with prefixed names."""
        dest = ObsidianDestination(
            vault_path=str(tmp_path),
            root_folder="All Notes",
            mirror_folders=False,
        )
        success = dest.publish(*make_both("Budget", "Numbers", folder=("Work", "Finance")))

        assert success.ok is True
        # Should be flat inside "All Notes" with folder prefix to avoid collision
        note_file = tmp_path / "All Notes" / "Work - Finance - Budget.md"
        assert note_file.exists()


# =========================================================================
# ObsidianDestination — attachments & YAML frontmatter
# =========================================================================


class TestObsidianPublishAttachmentsAndFrontmatter:
    """Tests for attachments copying, linking, and frontmatter generation."""

    def test_attachments_copied_and_wikilinked(self, tmp_path):
        """Page images are copied to attachments folder and WikiLinked."""
        vault = tmp_path / "vault"
        vault.mkdir()

        # Create dummy image files
        img1 = tmp_path / "page-1.png"
        img2 = tmp_path / "page-2.png"
        img1.write_bytes(b"PNG1")
        img2.write_bytes(b"PNG2")

        dest = ObsidianDestination(vault_path=str(vault), root_folder="Living Ink")
        success = dest.publish(
            *make_both("Sketches", "Handwritten notes", [img1, img2], folder=("Personal",))
        )

        assert success.ok is True
        note_file = vault / "Living Ink" / "Personal" / "Sketches.md"
        assert note_file.exists()
        content = note_file.read_text(encoding="utf-8")

        # Check WikiLinks
        assert "## Original Pages" in content
        assert "![[Living Ink/_attachments/Personal/Sketches/page-1.png|Page 1]]" in content
        assert "![[Living Ink/_attachments/Personal/Sketches/page-2.png|Page 2]]" in content

        # Check copied files in centralized _attachments with mirrored subfolder and note folder
        attach_dir = vault / "Living Ink" / "_attachments" / "Personal" / "Sketches"
        assert (attach_dir / "page-1.png").exists()
        assert (attach_dir / "page-2.png").exists()

    def test_nested_subfolder_mirrored_in_attachments(self, tmp_path):
        """Nested subfolders and note folder are mirrored inside the central _attachments folder."""
        vault = tmp_path / "vault"
        vault.mkdir()
        img = tmp_path / "page-1.png"
        img.write_bytes(b"PNG")

        dest = ObsidianDestination(vault_path=str(vault), root_folder="Living Ink")
        success = dest.publish(
            *make_both("Roadmap", "Notes", [img], folder=("Work", "Projects", "2026"))
        )
        assert success.ok is True
        note_file = vault / "Living Ink" / "Work" / "Projects" / "2026" / "Roadmap.md"
        assert note_file.exists()

        attach_dir = (
            vault / "Living Ink" / "_attachments" / "Work" / "Projects" / "2026" / "Roadmap"
        )
        assert (attach_dir / "page-1.png").exists()

        content = note_file.read_text(encoding="utf-8")
        assert (
            "![[Living Ink/_attachments/Work/Projects/2026/Roadmap/page-1.png|Page 1]]" in content
        )

    def test_missing_attachment_skipped_gracefully(self, tmp_path):
        """Missing image files do not crash the publication."""
        dest = ObsidianDestination(vault_path=str(tmp_path))
        missing_img = tmp_path / "nonexistent.png"

        success = dest.publish(*make_both("Note", "Content", [missing_img]))
        assert success.ok is True
        note_file = tmp_path / "Note.md"
        assert note_file.exists()
        # Should not include Original Pages header if no attachments copied
        assert "## Original Pages" not in note_file.read_text(encoding="utf-8")

    def test_empty_attachments_folder_places_alongside_note(self, tmp_path):
        """Setting attachments_folder to empty string places images next to note."""
        img = tmp_path / "page-1.png"
        img.write_bytes(b"PNG")

        dest = ObsidianDestination(
            vault_path=str(tmp_path),
            attachments_folder="",
        )
        success = dest.publish(*make_both("DirectNote", "Text", [img], folder=("Sub",)))

        assert success.ok is True
        target_dir = tmp_path / "Sub"
        assert (target_dir / "DirectNote.md").exists()
        assert (target_dir / "DirectNote_page-1.png").exists()

    def test_frontmatter_format(self, tmp_path):
        """YAML frontmatter includes created, source, and tags."""
        dest = ObsidianDestination(vault_path=str(tmp_path))
        dest.publish(*make_both("TagTest", "Body"))

        note_file = tmp_path / "TagTest.md"
        content = note_file.read_text(encoding="utf-8")
        assert content.startswith("---\n")
        assert "tags:\n  - remarkable\n  - handwritten" in content
        assert "source: Remarkable/TagTest" in content


class TestObsidianFailureReporting:
    """An unwritable vault is a reported failure, not a traceback."""

    def test_an_unwritable_vault_comes_back_as_a_refusal_with_a_reason(self, tmp_path):
        # The stages raise; FileSystemDestination.publish catches at one seam
        # and answers with a result, so the caller never has to decide what a
        # filesystem error from a destination means.
        vault = tmp_path / "vault"
        vault.mkdir()
        dest = ObsidianDestination(vault_path=str(vault), root_folder="Living Ink")

        with patch(
            "living_ink.destinations.obsidian.Path.mkdir", side_effect=PermissionError("denied")
        ):
            result = dest.publish(*make_both("Note", "Content"))

        assert result.ok is False
        assert "Could not write" in result.detail
        assert "denied" in result.detail


# =========================================================================
# State File Management
# =========================================================================


class TestStateLocation:
    """Sync state lives in the data directory and nowhere else."""

    def _at(self, tmp_path, monkeypatch):
        """Point the state layer at a temp directory and drop any cached store."""
        from living_ink import pipeline

        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path / "data")
        monkeypatch.setattr(pipeline, "ensure_runtime_dirs", lambda: None)
        pipeline.reset_state_store()
        return pipeline

    def test_the_database_lives_in_the_data_dir(self, tmp_path, monkeypatch):
        pipeline = self._at(tmp_path, monkeypatch)
        try:
            assert pipeline.get_state_db_path() == tmp_path / "data" / "state.db"
        finally:
            pipeline.reset_state_store()


# =========================================================================
# ObsidianDestination — durability of the note write
# =========================================================================


class TestObsidianWriteDurability:
    """An interrupted publish must not leave a half-written note in the vault."""

    def test_an_interrupted_write_preserves_the_previous_note(self, tmp_path, monkeypatch):
        dest = ObsidianDestination(vault_path=str(tmp_path))
        assert dest.publish(*make_both("Meeting Notes", "first version")).ok is True
        note = tmp_path / "Meeting Notes.md"
        original = note.read_text(encoding="utf-8")

        def interrupt(*_args, **_kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(safeio.os, "replace", interrupt)
        with pytest.raises(KeyboardInterrupt):
            dest.publish(*make_both("Meeting Notes", "second version"))

        assert note.read_text(encoding="utf-8") == original

    def test_no_temporary_file_is_left_in_the_vault(self, tmp_path):
        dest = ObsidianDestination(vault_path=str(tmp_path))
        dest.publish(*make_both("Meeting Notes", "content"))
        # The attachments folder is expected; a leftover ".tmp" would not be.
        assert sorted(p.name for p in tmp_path.iterdir()) == ["Meeting Notes.md", "_attachments"]


class TestTagsAreUnionedNotOverwritten:
    """The one frontmatter edit a user is most likely to make."""

    def _publish(self, tmp_path, tags=()):
        dest = ObsidianDestination(vault_path=str(tmp_path))
        doc, ctx = make_both("Daily Note", "Transcript")
        doc = replace(doc, tags=tuple(tags))
        result = dest.publish(doc, ctx)
        return dest, tmp_path / result.target

    def test_a_tag_the_user_added_survives_a_resync(self, tmp_path):
        dest, note = self._publish(tmp_path)
        note.write_text(
            note.read_text(encoding="utf-8").replace(
                "  - handwritten", "  - handwritten\n  - todo", 1
            ),
            encoding="utf-8",
        )

        dest.publish(*make_both("Daily Note", "Transcript v2"))

        assert "  - todo" in note.read_text(encoding="utf-8")

    def test_the_tablets_tags_come_first_and_the_users_follow(self, tmp_path):
        dest, note = self._publish(tmp_path, tags=["work"])
        note.write_text(
            note.read_text(encoding="utf-8").replace("  - work", "  - work\n  - todo", 1),
            encoding="utf-8",
        )

        doc, ctx = make_both("Daily Note", "v2")
        dest.publish(replace(doc, tags=("work",)), ctx)

        written = note.read_text(encoding="utf-8")
        tags = notemerge.frontmatter_list(notemerge.split_frontmatter(written)[0], "tags")
        assert tags == ["remarkable", "handwritten", "work", "todo"]

    def test_a_tag_is_not_duplicated_by_the_union(self, tmp_path):
        dest, note = self._publish(tmp_path, tags=["work"])
        doc, ctx = make_both("Daily Note", "v2")
        dest.publish(replace(doc, tags=("work",)), ctx)

        written = note.read_text(encoding="utf-8")
        assert written.count("  - work") == 1

    def test_a_user_tag_is_sanitized_like_any_other(self, tmp_path):
        dest, note = self._publish(tmp_path)
        note.write_text(
            note.read_text(encoding="utf-8").replace(
                "  - handwritten", "  - handwritten\n  - my notes!", 1
            ),
            encoding="utf-8",
        )

        dest.publish(*make_both("Daily Note", "v2"))

        assert "  - my-notes" in note.read_text(encoding="utf-8")

    def test_removing_a_tag_on_the_tablet_leaves_it_in_the_note(self, tmp_path):
        # The stated limitation. Living Ink does not record which tags it wrote
        # last time, so the conservative error is a stale tag, not a deleted one.
        dest, note = self._publish(tmp_path, tags=["work"])
        dest.publish(*make_both("Daily Note", "v2"))

        assert "  - work" in note.read_text(encoding="utf-8")


class TestTheSourceKeyIsAContract:
    """``source: Remarkable/`` is read by one module and written by another."""

    def test_what_obsidian_writes_is_what_notemerge_recognises(self, tmp_path):
        # A trap, not a typo. destinations writes `Remarkable/`; notemerge
        # matches `^source\s*:\s*Remarkable/` to decide whether a note with no
        # markers was generated by Living Ink. "Correcting" the casing on
        # either side makes looks_generated() False for every note written
        # before the change, so the merge keeps the whole old body as a prefix
        # and appends a second copy of the transcript.
        dest = ObsidianDestination(vault_path=str(tmp_path))
        result = dest.publish(*make_both("Daily Note", "Transcript"))
        written = (tmp_path / result.target).read_text(encoding="utf-8")

        assert notemerge.looks_generated(written) is True

    def test_the_two_spellings_are_the_same_spelling(self, tmp_path):
        dest = ObsidianDestination(vault_path=str(tmp_path))
        result = dest.publish(*make_both("Daily Note", "Transcript"))
        written = (tmp_path / result.target).read_text(encoding="utf-8")

        assert "source: Remarkable/" in written
        assert notemerge._OURS_MARKER.search(written) is not None


class TestObsidianPreservesUserEdits:
    """A sync used to replace the whole note, discarding anything you added."""

    def _publish(self, tmp_path, text="Transcript v1"):
        dest = ObsidianDestination(vault_path=str(tmp_path))
        assert dest.publish(*make_both("Meeting Notes", text)).ok is True
        return dest, tmp_path / "Meeting Notes.md"

    def test_notes_below_the_transcript_survive_a_resync(self, tmp_path):
        dest, note = self._publish(tmp_path)
        note.write_text(
            note.read_text(encoding="utf-8") + "\n## My action items\n\n- Email Dana\n",
            encoding="utf-8",
        )

        dest.publish(*make_both("Meeting Notes", "Transcript v2"))

        written = note.read_text(encoding="utf-8")
        assert "- Email Dana" in written
        assert "Transcript v2" in written
        assert "Transcript v1" not in written

    def test_a_user_frontmatter_key_survives_a_resync(self, tmp_path):
        dest, note = self._publish(tmp_path)
        note.write_text(
            note.read_text(encoding="utf-8").replace("---\n", "---\naliases:\n  - Standup\n", 1),
            encoding="utf-8",
        )

        dest.publish(*make_both("Meeting Notes", "Transcript v2"))
        assert "  - Standup" in note.read_text(encoding="utf-8")

    def test_a_note_written_by_someone_else_is_not_destroyed(self, tmp_path):
        """A title collision used to silently delete an unrelated note."""
        note = tmp_path / "Meeting Notes.md"
        note.write_text("# My own note\n\nDo not delete this.\n", encoding="utf-8")

        ObsidianDestination(vault_path=str(tmp_path)).publish(
            *make_both("Meeting Notes", "Transcript")
        )

        written = note.read_text(encoding="utf-8")
        assert "Do not delete this." in written
        assert "Transcript" in written

    def test_the_creation_date_stops_meaning_last_synced(self, tmp_path):
        dest, note = self._publish(tmp_path)
        note.write_text(
            re.sub(r"created: .*", "created: 2020-01-01", note.read_text(encoding="utf-8")),
            encoding="utf-8",
        )

        dest.publish(*make_both("Meeting Notes", "Transcript v2"))

        written = note.read_text(encoding="utf-8")
        assert "created: 2020-01-01" in written
        assert f"updated: {datetime.date.today().isoformat()}" in written

    def test_a_new_note_gets_both_dates(self, tmp_path):
        _, note = self._publish(tmp_path)
        today = datetime.date.today().isoformat()
        written = note.read_text(encoding="utf-8")
        assert f"created: {today}" in written
        assert f"updated: {today}" in written

    def test_resyncing_does_not_stack_transcripts(self, tmp_path):
        dest, note = self._publish(tmp_path)
        for version in range(2, 5):
            dest.publish(*make_both("Meeting Notes", f"Transcript v{version}"))

        written = note.read_text(encoding="utf-8")
        assert written.count("living-ink:begin") == 1
        assert written.count("Transcript v") == 1


class TestObsidianIgnoresIdentityArguments:
    """Obsidian finds its note by path, so both arguments are accepted and unused."""

    def test_publishing_with_an_id_still_works(self, tmp_path):
        dest = ObsidianDestination(vault_path=str(tmp_path))
        assert dest.publish(*make_both("Note", "body", existing_external_id="ignored")).ok is True
        assert (tmp_path / "Note.md").exists()

    def test_no_external_id_is_reported(self, tmp_path):
        dest = ObsidianDestination(vault_path=str(tmp_path))
        assert dest.publish(*make_both("Note", "body")).external_id is None


class TestObsidianNoteIdentity:
    """The document id is the identity of a note; the filename only labels it."""

    def _dest(self, tmp_path):
        return ObsidianDestination(vault_path=str(tmp_path))

    def test_the_document_id_is_stamped_into_the_note(self, tmp_path):
        self._dest(tmp_path).publish(*make_both("Notes", "body", doc_id="doc-1"))
        assert "living_ink_id: doc-1" in (tmp_path / "Notes.md").read_text(encoding="utf-8")

    def test_the_id_survives_a_resync(self, tmp_path):
        dest = self._dest(tmp_path)
        dest.publish(*make_both("Notes", "v1", doc_id="doc-1"))
        dest.publish(*make_both("Notes", "v2", doc_id="doc-1"))
        written = (tmp_path / "Notes.md").read_text(encoding="utf-8")
        assert written.count("living_ink_id: doc-1") == 1

    def test_a_different_document_with_the_same_title_gets_its_own_note(self, tmp_path):
        """Two notebooks called 'Notes' used to merge into one file."""
        dest = self._dest(tmp_path)
        dest.publish(*make_both("Notes", "first notebook", doc_id="doc-1"))
        dest.publish(*make_both("Notes", "second notebook", doc_id="doc-2"))

        assert "first notebook" in (tmp_path / "Notes.md").read_text(encoding="utf-8")
        assert "second notebook" in (tmp_path / "Notes (2).md").read_text(encoding="utf-8")

    def test_the_same_document_keeps_its_note_rather_than_multiplying(self, tmp_path):
        dest = self._dest(tmp_path)
        dest.publish(*make_both("Notes", "v1", doc_id="doc-1"))
        dest.publish(*make_both("Notes", "v2", doc_id="doc-1"))
        assert not (tmp_path / "Notes (2).md").exists()

    def test_a_third_collision_takes_the_next_free_name(self, tmp_path):
        dest = self._dest(tmp_path)
        for index in range(1, 4):
            dest.publish(*make_both("Notes", f"notebook {index}", doc_id=f"doc-{index}"))
        assert (tmp_path / "Notes (3).md").exists()

    def test_a_note_predating_ids_is_adopted_not_duplicated(self, tmp_path):
        """Everything synced before this existed carries no id, and is still ours."""
        (tmp_path / "Notes.md").write_text(
            "---\nsource: Remarkable/Notes\ntype: handwritten\n---\n\nv1\n",
            encoding="utf-8",
        )
        self._dest(tmp_path).publish(*make_both("Notes", "v2", doc_id="doc-1"))

        assert not (tmp_path / "Notes (2).md").exists()
        assert "living_ink_id: doc-1" in (tmp_path / "Notes.md").read_text(encoding="utf-8")

    def test_someone_elses_note_is_still_merged_into(self, tmp_path):
        """A hand-written note has no id; refusing to touch it would orphan the sync."""
        note = tmp_path / "Notes.md"
        note.write_text("# Mine\n\nKeep this.\n", encoding="utf-8")

        self._dest(tmp_path).publish(*make_both("Notes", "Transcript", doc_id="doc-1"))

        written = note.read_text(encoding="utf-8")
        assert "Keep this." in written
        assert "Transcript" in written

    def test_where_the_note_landed_is_reported(self, tmp_path):
        dest = self._dest(tmp_path)
        result = dest.publish(*make_both("Notes", "body", folder=("Work",), doc_id="doc-1"))
        assert result.target == "Work/Notes.md"

    def test_the_reported_target_is_the_name_actually_used(self, tmp_path):
        dest = self._dest(tmp_path)
        dest.publish(*make_both("Notes", "first", doc_id="doc-1"))
        result = dest.publish(*make_both("Notes", "second", doc_id="doc-2"))
        assert result.target == "Notes (2).md"

    def test_the_attachments_follow_the_note_that_was_written(self, tmp_path, monkeypatch):
        """A renamed note must not point its links at the other document's images."""
        page = tmp_path / "page-1.png"
        page.write_bytes(b"png")

        dest = ObsidianDestination(vault_path=str(tmp_path), attachments_folder="_attachments")
        dest.publish(*make_both("Notes", "first", doc_id="doc-1"))
        dest.publish(*make_both("Notes", "second", [page], doc_id="doc-2"))

        assert (tmp_path / "_attachments" / "Notes (2)" / "page-1.png").exists()


class TestObsidianRenamesAndMoves:
    """A notebook renamed on the tablet keeps its note instead of growing a second."""

    def _dest(self, tmp_path, **kwargs):
        return ObsidianDestination(vault_path=str(tmp_path), **kwargs)

    def test_a_renamed_notebook_moves_its_note(self, tmp_path):
        dest = self._dest(tmp_path)
        dest.publish(*make_both("Old Name", "body", doc_id="doc-1"))

        dest.publish(*make_both("New Name", "body", doc_id="doc-1", existing_target="Old Name.md"))

        assert (tmp_path / "New Name.md").exists()
        assert not (tmp_path / "Old Name.md").exists()

    def test_the_moved_note_keeps_what_the_user_added(self, tmp_path):
        dest = self._dest(tmp_path)
        dest.publish(*make_both("Old Name", "v1", doc_id="doc-1"))
        old = tmp_path / "Old Name.md"
        old.write_text(
            old.read_text(encoding="utf-8") + "\n## Mine\n\n- Keep me\n", encoding="utf-8"
        )

        dest.publish(*make_both("New Name", "v2", doc_id="doc-1", existing_target="Old Name.md"))

        assert "- Keep me" in (tmp_path / "New Name.md").read_text(encoding="utf-8")

    def test_a_moved_notebook_follows_its_folder(self, tmp_path):
        dest = self._dest(tmp_path)
        dest.publish(*make_both("Notes", "body", folder=("Work",), doc_id="doc-1"))

        dest.publish(
            *make_both(
                "Notes",
                "body",
                folder=("Archive",),
                doc_id="doc-1",
                existing_target="Work/Notes.md",
            )
        )

        assert (tmp_path / "Archive" / "Notes.md").exists()
        assert not (tmp_path / "Work" / "Notes.md").exists()

    def test_the_attachments_move_with_the_note(self, tmp_path):
        page = tmp_path / "page-1.png"
        page.write_bytes(b"png")
        dest = self._dest(tmp_path, attachments_folder="_attachments")
        dest.publish(*make_both("Old Name", "body", [page], doc_id="doc-1"))

        dest.publish(
            *make_both("New Name", "body", [page], doc_id="doc-1", existing_target="Old Name.md")
        )

        assert (tmp_path / "_attachments" / "New Name" / "page-1.png").exists()
        assert not (tmp_path / "_attachments" / "Old Name").exists()

    def test_another_documents_note_is_not_dragged_along(self, tmp_path):
        """A stale recorded path must never move a note that is not ours."""
        dest = self._dest(tmp_path)
        dest.publish(*make_both("Theirs", "their body", doc_id="doc-2"))

        dest.publish(*make_both("Mine", "my body", doc_id="doc-1", existing_target="Theirs.md"))

        assert "their body" in (tmp_path / "Theirs.md").read_text(encoding="utf-8")
        assert (tmp_path / "Mine.md").exists()

    def test_a_note_already_at_the_new_path_is_not_overwritten(self, tmp_path):
        dest = self._dest(tmp_path)
        dest.publish(*make_both("Old Name", "mine", doc_id="doc-1"))
        (tmp_path / "New Name.md").write_text("# Somebody else\n", encoding="utf-8")

        dest.publish(*make_both("New Name", "mine", doc_id="doc-1", existing_target="Old Name.md"))

        assert "Somebody else" in (tmp_path / "New Name.md").read_text(encoding="utf-8")

    def test_a_recorded_path_that_no_longer_exists_is_harmless(self, tmp_path):
        dest = self._dest(tmp_path)
        assert dest.publish(
            *make_both("Notes", "body", doc_id="doc-1", existing_target="Gone.md")
        ).ok
        assert (tmp_path / "Notes.md").exists()

    def test_the_new_location_is_reported(self, tmp_path):
        dest = self._dest(tmp_path)
        dest.publish(*make_both("Old Name", "body", doc_id="doc-1"))
        result = dest.publish(
            *make_both("New Name", "body", doc_id="doc-1", existing_target="Old Name.md")
        )
        assert result.target == "New Name.md"


class TestObsidianUnpublish:
    """Deleting a note is refused unless the note is provably ours."""

    def _dest(self, tmp_path, **kwargs):
        return ObsidianDestination(vault_path=str(tmp_path), **kwargs)

    def test_our_own_note_is_deleted(self, tmp_path):
        dest = self._dest(tmp_path)
        dest.publish(*make_both("Notes", "body", doc_id="doc-1"))

        assert dest.unpublish(make_context("doc-1", existing_target="Notes.md")).ok is True
        assert not (tmp_path / "Notes.md").exists()

    def test_the_attachments_go_too(self, tmp_path):
        page = tmp_path / "page-1.png"
        page.write_bytes(b"png")
        dest = self._dest(tmp_path, attachments_folder="_attachments")
        dest.publish(*make_both("Notes", "body", [page], doc_id="doc-1"))

        dest.unpublish(make_context("doc-1", existing_target="Notes.md"))
        assert not (tmp_path / "_attachments" / "Notes").exists()

    def test_another_documents_note_is_left_alone(self, tmp_path):
        dest = self._dest(tmp_path)
        dest.publish(*make_both("Notes", "body", doc_id="doc-2"))

        assert dest.unpublish(make_context("doc-1", existing_target="Notes.md")).ok is False
        assert (tmp_path / "Notes.md").exists()

    def test_a_note_with_no_id_is_left_alone(self, tmp_path):
        """A hand-written note is unrecoverable; never delete on a path alone."""
        (tmp_path / "Notes.md").write_text("# Mine\n", encoding="utf-8")

        assert (
            self._dest(tmp_path).unpublish(make_context("doc-1", existing_target="Notes.md")).ok
            is False
        )
        assert (tmp_path / "Notes.md").exists()

    def test_a_note_that_is_already_gone_is_not_an_error(self, tmp_path):
        assert (
            self._dest(tmp_path).unpublish(make_context("doc-1", existing_target="Gone.md")).ok
            is False
        )

    def test_nothing_happens_without_a_target(self, tmp_path):
        assert self._dest(tmp_path).unpublish(make_context("doc-1")).ok is False


class TestUnpublishDefault:
    """A destination that cannot prove which note is which deletes none."""

    def test_the_base_class_refuses(self):
        class Bare(Destination):
            @classmethod
            def from_config(cls, section, settings):
                return cls()

            def check(self):
                return DestinationStatus(ok=True, detail="ready")

            def publish(self, doc, ctx):
                return PublishResult(ok=True)

        assert (
            Bare().unpublish(make_context("z", existing_target="x", existing_external_id="y")).ok
            is False
        )


class TestObsidianNoteDates:
    """Three dates, three meanings. There used to be one, and it was wrong."""

    def _dest(self, tmp_path):
        return ObsidianDestination(vault_path=str(tmp_path))

    def _front(self, tmp_path, name="Notes"):
        return (tmp_path / f"{name}.md").read_text(encoding="utf-8")

    def test_updated_is_when_the_notebook_was_written_on(self, tmp_path):
        self._dest(tmp_path).publish(*make_both("Notes", "body", modified="2026-03-04"))
        assert "updated: 2026-03-04" in self._front(tmp_path)

    def test_synced_is_today(self, tmp_path):
        self._dest(tmp_path).publish(*make_both("Notes", "body", modified="2026-03-04"))
        today = datetime.date.today().isoformat()
        assert f"synced: {today}" in self._front(tmp_path)

    def test_created_stops_moving_on_every_sync(self, tmp_path):
        """This is the bug: a note written in March reported today as its birthday."""
        dest = self._dest(tmp_path)
        dest.publish(*make_both("Notes", "v1", modified="2026-03-04"))
        (tmp_path / "Notes.md").write_text(
            self._front(tmp_path).replace(
                f"created: {datetime.date.today().isoformat()}", "created: 2026-03-04"
            ),
            encoding="utf-8",
        )

        dest.publish(*make_both("Notes", "v2", modified="2026-09-17"))
        assert "created: 2026-03-04" in self._front(tmp_path)

    def test_created_falls_back_to_when_the_note_first_appeared(self, tmp_path):
        """A note from before this existed has no created line to preserve."""
        self._dest(tmp_path).publish(*make_both("Notes", "body", first_published="2025-11-02"))
        assert "created: 2025-11-02" in self._front(tmp_path)

    def test_created_prefers_the_note_over_the_database(self, tmp_path):
        dest = self._dest(tmp_path)
        dest.publish(*make_both("Notes", "v1", first_published="2025-11-02"))
        dest.publish(*make_both("Notes", "v2", first_published="2026-01-01"))
        assert "created: 2025-11-02" in self._front(tmp_path)

    def test_created_falls_back_to_the_modification_date_before_today(self, tmp_path):
        """A date the notebook was demonstrably alive on beats one that is wrong."""
        self._dest(tmp_path).publish(*make_both("Notes", "body", modified="2026-03-04"))
        assert "created: 2026-03-04" in self._front(tmp_path)

    def test_a_notebook_with_no_known_dates_still_publishes(self, tmp_path):
        assert self._dest(tmp_path).publish(*make_both("Notes", "body")).ok is True
        today = datetime.date.today().isoformat()
        assert f"created: {today}" in self._front(tmp_path)
        assert f"updated: {today}" in self._front(tmp_path)

    def test_the_three_dates_are_all_present(self, tmp_path):
        self._dest(tmp_path).publish(*make_both("Notes", "body", modified="2026-03-04"))
        written = self._front(tmp_path)
        assert all(key in written for key in ("created:", "updated:", "synced:"))


class TestObsidianCheck:
    """A bad vault is reported by check(), never by a constructor."""

    def test_a_writable_vault_is_ready(self, tmp_path):
        assert ObsidianDestination(vault_path=str(tmp_path)).check().ok is True

    def test_a_missing_vault_names_itself_and_the_remedy(self, tmp_path):
        missing = tmp_path / "gone"
        status = ObsidianDestination(vault_path=str(missing)).check()

        assert status.ok is False
        assert str(missing) in status.detail
        assert "vault_path" in (status.remedy or "").lower() or "path" in (status.remedy or "")

    def test_a_file_where_the_vault_should_be_is_its_own_failure(self, tmp_path):
        """An unmounted drive and a typo'd path need different answers."""
        not_a_vault = tmp_path / "vault.md"
        not_a_vault.write_text("hello", encoding="utf-8")
        status = ObsidianDestination(vault_path=str(not_a_vault)).check()

        assert status.ok is False
        assert "folder" in status.detail

    def test_a_read_only_vault_is_caught_before_the_run(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir(mode=0o500)
        try:
            status = ObsidianDestination(vault_path=str(vault)).check()
            assert status.ok is False
            assert "writable" in status.detail
        finally:
            vault.chmod(0o700)


class TestObsidianGapMarker:
    """A page that failed to transcribe leaves a visible gap in the note.

    A partial document publishes rather than being held back, so the note the
    user reads contains pages that produced nothing. Two very different
    failures look identical there unless the note says which is which: a page
    that is genuinely blank, and a page the API refused to serve. The second is
    temporary and will be retried, and the callout is where next run's text
    goes.
    """

    def _publish(self, tmp_path, pages):
        """Publish one document made of the given pages and return the note.

        Args:
            tmp_path: The pytest temp directory standing in for the vault.
            pages: The pages the document is made of.

        Returns:
            The text of the note that was written.
        """
        dest = ObsidianDestination(vault_path=str(tmp_path))
        doc, ctx = make_both("Notes", pages=pages)
        result = dest.publish(doc, ctx)
        assert result.ok is True
        return (tmp_path / result.target).read_text(encoding="utf-8")

    def _page_section(self, written, number):
        """Return the body of one page's managed block, markers stripped.

        Args:
            written: The whole note, frontmatter included.
            number: The page number whose block is wanted.

        Returns:
            What Living Ink wrote between that page's begin and end markers.
        """
        _, body = notemerge.split_frontmatter(written)
        segments = notemerge.parse_segments(body)
        return next(seg.content for seg in segments if seg.block_id == f"page-{number}")

    def test_a_failed_page_reads_as_a_warning_not_as_a_stack_trace(self, tmp_path):
        """The note is read by a person, and a bare exception tells them nothing.

        The page used to emit its error text as if it were the transcription,
        so ``RateLimitError: 429`` sat in the body looking like something the
        user had written on the tablet.
        """
        written = self._publish(
            tmp_path, [make_page(1, "", error="RateLimitError: 429 Too Many Requests")]
        )

        assert "> [!warning] Page not transcribed" in written
        # The reason is kept, but only inside the callout — never as a bare
        # line masquerading as transcribed text.
        mentions = [line for line in written.splitlines() if "RateLimitError" in line]
        assert mentions == ["> RateLimitError: 429 Too Many Requests"]

    def test_the_marker_parses_back_as_one_callout(self, tmp_path):
        """A marker that does not survive the round trip is a marker that can be lost.

        Every destination's body goes through ``to_blocks``. If the gap marker
        parsed as a plain quote, or as two blocks, a writer with no callout
        support would flatten it into ordinary text instead of reporting the
        degradation the contract promises.
        """
        written = self._publish(tmp_path, [make_page(1, "", error="429 Too Many Requests")])

        callouts = [
            block
            for block in to_blocks(self._page_section(written, 1))
            if block.kind is BlockKind.CALLOUT
        ]

        assert len(callouts) == 1
        assert callouts[0].attrs["callout_type"] == "warning"
        assert callouts[0].text == "Page not transcribed"

    def test_a_writer_without_callouts_reports_the_gap_rather_than_swallowing_it(self, tmp_path):
        """The marker is only worth writing if a second writer cannot lose it quietly.

        Obsidian renders callouts natively, so nothing shipped exercises
        ``degrade()``; a destination added later that cannot must still tell the
        user the gap marker was flattened.
        """
        written = self._publish(tmp_path, [make_page(1, "", error="429 Too Many Requests")])

        _, degradations = PlainTextWriter().render(to_blocks(self._page_section(written, 1)))

        assert BlockKind.CALLOUT in {degradation.kind for degradation in degradations}

    def test_a_blank_line_in_the_reason_does_not_strand_the_tail(self, tmp_path):
        """This is why every line of the reason is quoted, blank ones included.

        A blank line closes a Markdown blockquote. An unquoted one in the
        middle of a multi-line reason ends the callout early and drops the rest
        of the explanation into the note as loose text below the marker, where
        it reads as part of the transcription.
        """
        written = self._publish(
            tmp_path,
            [make_page(1, "", error="429 Too Many Requests\n\nRetrying on the next sync.")],
        )

        blocks = to_blocks(self._page_section(written, 1))
        callouts = [block for block in blocks if block.kind is BlockKind.CALLOUT]

        assert len(callouts) == 1
        assert [child.text for child in callouts[0].children] == [
            "429 Too Many Requests",
            "Retrying on the next sync.",
        ]
        # Nothing escaped: the tail is not a sibling of the callout.
        assert not [
            block for block in blocks if block is not callouts[0] and "Retrying" in block.text
        ]

    def test_a_failed_page_does_not_look_like_a_blank_one(self, tmp_path):
        """A blank page and an unread page call for different actions.

        A page that failed is worth re-running; a page that is blank is worth
        nothing. Publishing the heading alone for both made them identical, so
        a rate-limited notebook looked like a mostly empty one.
        """
        written = self._publish(
            tmp_path, [make_page(1, "", error="429 Too Many Requests"), make_page(2, "")]
        )

        assert "Page not transcribed" in self._page_section(written, 1)
        assert "Page not transcribed" not in self._page_section(written, 2)
        assert not [
            block
            for block in to_blocks(self._page_section(written, 2))
            if block.kind is BlockKind.CALLOUT
        ]

    def test_a_page_that_transcribed_is_left_exactly_as_it_was(self, tmp_path):
        """The marker is for gaps only; a warning on a good page would be noise.

        ``Page.error`` is the only signal, so a page carrying text and no error
        has to come through untouched — otherwise every note in the vault grows
        a callout.
        """
        written = self._publish(tmp_path, [make_page(1, "Handwritten thoughts")])

        assert "Handwritten thoughts" in written
        assert "Page not transcribed" not in written
        assert "[!warning]" not in written

    def test_text_salvaged_from_a_failed_page_is_published_under_the_marker(self, tmp_path):
        """An error and a partial transcription are not exclusive, and both belong in the note.

        Rendering the marker *instead of* the text discards a transcription the
        user has already paid for. The marker leads so a reader who stops at the
        first line knows the page is incomplete before reading the part of it
        that did arrive.
        """
        written = self._publish(
            tmp_path, [make_page(1, "The half that arrived", error="truncated response")]
        )

        section = self._page_section(written, 1)
        assert "The half that arrived" in section
        assert section.index("Page not transcribed") < section.index("The half that arrived")

    def test_a_reason_that_says_nothing_still_makes_a_well_formed_marker(self, tmp_path):
        """A provider that fails without a message must not produce a malformed callout.

        The reason is whatever the exception carried, which can be an empty
        string. The marker still has to parse back as one callout, because a
        trailing quote line with nothing after it is a block a writer can trip on.
        """
        written = self._publish(tmp_path, [make_page(1, "", error="   ")])

        callouts = [
            block
            for block in to_blocks(self._page_section(written, 1))
            if block.kind is BlockKind.CALLOUT
        ]

        assert len(callouts) == 1
        assert callouts[0].children == ()
