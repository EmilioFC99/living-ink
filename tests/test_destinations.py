"""Tests for living_ink.destinations module.

Covers Destination abstract base class, AppleNotesDestination,
and ObsidianDestination including full folder mirroring, root folder
configuration, attachment handling, and filename sanitization.
"""

import datetime
import re
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from living_ink import notemerge, safeio
from living_ink.destinations import (
    AppleNotesDestination,
    Destination,
    DestinationError,
    DestinationUnavailable,
    ObsidianDestination,
)

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
            def publish(self, notebook_name, text_content, image_paths, sub_folder=None):
                return True

        dest = Complete()
        assert dest.publish("Test", "Content", []) is True


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

    def test_nonexistent_vault_path_raises(self, tmp_path):
        """Non-existent vault path raises ValueError."""
        nonexistent = tmp_path / "does_not_exist"
        with pytest.raises(ValueError, match="does not exist"):
            ObsidianDestination(vault_path=str(nonexistent))

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
        success = dest.publish("Daily Note", "Some text", [])

        assert success is True
        note_file = tmp_path / "Daily Note.md"
        assert note_file.exists()
        content = note_file.read_text(encoding="utf-8")
        assert "Some text" in content
        assert "source: Remarkable/Daily Note" in content

    def test_publish_with_root_folder(self, tmp_path):
        """Note is placed under root_folder when specified."""
        dest = ObsidianDestination(vault_path=str(tmp_path), root_folder="Living Ink")
        success = dest.publish("Quick Note", "Quick thoughts", [])

        assert success is True
        note_file = tmp_path / "Living Ink" / "Quick Note.md"
        assert note_file.exists()
        assert "Quick thoughts" in note_file.read_text(encoding="utf-8")

    def test_publish_with_nested_subfolder(self, tmp_path):
        """Full reMarkable folder hierarchy is mirrored in Obsidian."""
        dest = ObsidianDestination(vault_path=str(tmp_path), root_folder="Living Ink")
        success = dest.publish(
            notebook_name="Roadmap",
            text_content="Q1 plans",
            image_paths=[],
            sub_folder="Work/Projects/2026",
        )

        assert success is True
        note_file = tmp_path / "Living Ink" / "Work" / "Projects" / "2026" / "Roadmap.md"
        assert note_file.exists()
        content = note_file.read_text(encoding="utf-8")
        assert "Q1 plans" in content
        assert "source: Remarkable/Work/Projects/2026/Roadmap" in content

    def test_publish_extracts_clean_title_from_breadcrumbs(self, tmp_path):
        """Extracts base title when display_title with breadcrumbs is passed."""
        dest = ObsidianDestination(vault_path=str(tmp_path), root_folder="Living Ink")
        success = dest.publish(
            notebook_name="Work / Finance / Budget 2026",
            text_content="Financial summary",
            image_paths=[],
            sub_folder="Work/Finance",
        )

        assert success is True
        # Note should be Budget 2026.md, NOT Work - Finance - Budget 2026.md
        note_file = tmp_path / "Living Ink" / "Work" / "Finance" / "Budget 2026.md"
        assert note_file.exists()
        content = note_file.read_text(encoding="utf-8")
        assert "source: Remarkable/Work/Finance/Budget 2026" in content

    def test_publish_mirror_folders_false_flat_mode(self, tmp_path):
        """When mirror_folders is False, notes are placed flat with prefixed names."""
        dest = ObsidianDestination(
            vault_path=str(tmp_path),
            root_folder="All Notes",
            mirror_folders=False,
        )
        success = dest.publish(
            notebook_name="Budget",
            text_content="Numbers",
            image_paths=[],
            sub_folder="Work/Finance",
        )

        assert success is True
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
            notebook_name="Sketches",
            text_content="Handwritten notes",
            image_paths=[img1, img2],
            sub_folder="Personal",
        )

        assert success is True
        note_file = vault / "Living Ink" / "Personal" / "Sketches.md"
        assert note_file.exists()
        content = note_file.read_text(encoding="utf-8")

        # Check WikiLinks
        assert "## Original Pages" in content
        assert "- [[Living Ink/_attachments/Personal/Sketches/page-1.png|Page 1]]" in content
        assert "- [[Living Ink/_attachments/Personal/Sketches/page-2.png|Page 2]]" in content

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
            notebook_name="Roadmap",
            text_content="Notes",
            image_paths=[img],
            sub_folder="Work/Projects/2026",
        )
        assert success is True
        note_file = vault / "Living Ink" / "Work" / "Projects" / "2026" / "Roadmap.md"
        assert note_file.exists()

        attach_dir = (
            vault / "Living Ink" / "_attachments" / "Work" / "Projects" / "2026" / "Roadmap"
        )
        assert (attach_dir / "page-1.png").exists()

        content = note_file.read_text(encoding="utf-8")
        assert (
            "- [[Living Ink/_attachments/Work/Projects/2026/Roadmap/page-1.png|Page 1]]" in content
        )

    def test_missing_attachment_skipped_gracefully(self, tmp_path):
        """Missing image files do not crash the publication."""
        dest = ObsidianDestination(vault_path=str(tmp_path))
        missing_img = tmp_path / "nonexistent.png"

        success = dest.publish("Note", "Content", [missing_img])
        assert success is True
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
        success = dest.publish("DirectNote", "Text", [img], sub_folder="Sub")

        assert success is True
        target_dir = tmp_path / "Sub"
        assert (target_dir / "DirectNote.md").exists()
        assert (target_dir / "DirectNote_page-1.png").exists()

    def test_frontmatter_format(self, tmp_path):
        """YAML frontmatter includes created, source, and tags."""
        dest = ObsidianDestination(vault_path=str(tmp_path))
        dest.publish("TagTest", "Body", [])

        note_file = tmp_path / "TagTest.md"
        content = note_file.read_text(encoding="utf-8")
        assert content.startswith("---\n")
        assert "tags:\n  - remarkable\n  - handwritten" in content
        assert "source: Remarkable/TagTest" in content


# =========================================================================
# AppleNotesDestination
# =========================================================================


class TestAppleNotesDestination:
    """Tests for AppleNotesDestination."""

    def test_convert_to_html(self):
        """Converts plain text to Apple Notes HTML div format."""
        dest = AppleNotesDestination(folder_name="Living Ink")
        html_out = dest._convert_to_html("Line 1\n\nLine 2")
        assert "<div>Line 1</div>" in html_out
        assert "<div><br></div>" in html_out
        assert "<div>Line 2</div>" in html_out

    def test_convert_to_html_escapes_special_chars(self):
        """HTML special characters are escaped."""
        dest = AppleNotesDestination()
        html_out = dest._convert_to_html("5 < 10 & 20 > 15")
        assert "5 &lt; 10 &amp; 20 &gt; 15" in html_out

    def test_create_opaque_image(self, tmp_path):
        """Transparent image is composited onto white RGB."""
        dest = AppleNotesDestination()

        # Create transparent RGBA image
        img_path = tmp_path / "test_transparent.png"
        img = Image.new("RGBA", (50, 50), (255, 0, 0, 128))
        img.save(img_path)

        opaque = dest._create_opaque_image(img_path)
        assert opaque.exists()
        assert opaque.name == "opaque_test_transparent.png"

        result_img = Image.open(opaque)
        assert result_img.mode == "RGB"

    @patch("living_ink.destinations.subprocess.run")
    def test_publish_executes_applescript_with_top_level_folder(self, mock_run):
        """Extracts top-level subfolder when nested path is provided."""
        mock_run.return_value = MagicMock(return_code=0, returncode=0, stderr="")

        dest = AppleNotesDestination(folder_name="Living Ink")
        success = dest.publish(
            notebook_name="Work / Projects / Q1 / Plan",
            text_content="Plan text",
            image_paths=[],
            sub_folder="Projects/Q1",
        )

        assert success is True
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        script = cmd[2]  # osascript -e <script>

        # Verify root folder
        assert '"Living Ink"' in script
        # Verify only the top-level segment of sub_folder is passed
        assert '"Projects"' in script
        assert "Projects/Q1" not in script

    @patch("living_ink.destinations.subprocess.run")
    def test_publish_handles_applescript_failure(self, mock_run):
        """Raises DestinationUnavailable after exhausting retries."""
        mock_run.return_value = MagicMock(returncode=1, stderr="AppleScript Error")

        dest = AppleNotesDestination()
        with patch("living_ink.destinations.time.sleep"):
            with pytest.raises(DestinationUnavailable) as exc_info:
                dest.publish("Failed Note", "Content", [])

        assert "AppleScript Error" in str(exc_info.value)
        assert mock_run.call_count == 3

    @patch("living_ink.destinations.subprocess.run")
    def test_publish_reports_missing_osascript(self, mock_run):
        """A non-macOS host is reported as unavailable, not as a generic failure."""
        mock_run.side_effect = FileNotFoundError("osascript")

        with pytest.raises(DestinationUnavailable, match="osascript not found"):
            AppleNotesDestination().publish("Note", "Content", [])


class TestObsidianFailureReporting:
    """An unwritable vault must be reported as a DestinationError."""

    def test_unwritable_vault_raises_destination_error(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        dest = ObsidianDestination(vault_path=str(vault), root_folder="Living Ink")

        with patch("living_ink.destinations.Path.mkdir", side_effect=PermissionError("denied")):
            with pytest.raises(DestinationError, match="Could not write"):
                dest.publish("Note", "Content", [])

    def test_destination_unavailable_is_a_destination_error(self):
        """Callers can catch the base class and handle both cases."""
        assert issubclass(DestinationUnavailable, DestinationError)


# =========================================================================
# State File Management & Migration
# =========================================================================


class TestStateLocation:
    """Sync state lives in the data directory, and older layouts are absorbed."""

    def _at(self, tmp_path, monkeypatch):
        """Point the state layer at a temp directory and drop any cached store."""
        from living_ink import pipeline

        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path / "data")
        monkeypatch.setattr(pipeline, "ROOT", tmp_path)
        monkeypatch.setattr(pipeline, "ensure_runtime_dirs", lambda: None)
        pipeline.reset_state_store()
        return pipeline

    def test_the_database_lives_in_the_data_dir(self, tmp_path, monkeypatch):
        pipeline = self._at(tmp_path, monkeypatch)
        try:
            assert pipeline.get_state_db_path() == tmp_path / "data" / "state.db"
        finally:
            pipeline.reset_state_store()

    def test_legacy_state_beside_the_checkout_is_imported(self, tmp_path, monkeypatch):
        """State written before it moved under the data directory still counts."""
        pipeline = self._at(tmp_path, monkeypatch)
        legacy = tmp_path / "processed_notebooks_Obsidian.json"
        legacy.write_text('{"doc1": 1}', encoding="utf-8")
        try:
            assert pipeline.load_processed_log("Obsidian") == {"doc1": "1"}
            assert not legacy.exists()
        finally:
            pipeline.reset_state_store()


# =========================================================================
# ObsidianDestination — durability of the note write
# =========================================================================


class TestObsidianWriteDurability:
    """An interrupted publish must not leave a half-written note in the vault."""

    def test_an_interrupted_write_preserves_the_previous_note(self, tmp_path, monkeypatch):
        dest = ObsidianDestination(vault_path=str(tmp_path))
        assert dest.publish("Meeting Notes", "first version", []) is True
        note = tmp_path / "Meeting Notes.md"
        original = note.read_text(encoding="utf-8")

        def interrupt(*_args, **_kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(safeio.os, "replace", interrupt)
        with pytest.raises(KeyboardInterrupt):
            dest.publish("Meeting Notes", "second version", [])

        assert note.read_text(encoding="utf-8") == original

    def test_no_temporary_file_is_left_in_the_vault(self, tmp_path):
        dest = ObsidianDestination(vault_path=str(tmp_path))
        dest.publish("Meeting Notes", "content", [])
        # The attachments folder is expected; a leftover ".tmp" would not be.
        assert sorted(p.name for p in tmp_path.iterdir()) == ["Meeting Notes.md", "_attachments"]


class TestObsidianPreservesUserEdits:
    """A sync used to replace the whole note, discarding anything you added."""

    def _publish(self, tmp_path, text="Transcript v1"):
        dest = ObsidianDestination(vault_path=str(tmp_path))
        assert dest.publish("Meeting Notes", text, []) is True
        return dest, tmp_path / "Meeting Notes.md"

    def test_notes_below_the_transcript_survive_a_resync(self, tmp_path):
        dest, note = self._publish(tmp_path)
        note.write_text(
            note.read_text(encoding="utf-8") + "\n## My action items\n\n- Email Dana\n",
            encoding="utf-8",
        )

        dest.publish("Meeting Notes", "Transcript v2", [])

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

        dest.publish("Meeting Notes", "Transcript v2", [])
        assert "  - Standup" in note.read_text(encoding="utf-8")

    def test_a_note_written_by_someone_else_is_not_destroyed(self, tmp_path):
        """A title collision used to silently delete an unrelated note."""
        note = tmp_path / "Meeting Notes.md"
        note.write_text("# My own note\n\nDo not delete this.\n", encoding="utf-8")

        ObsidianDestination(vault_path=str(tmp_path)).publish("Meeting Notes", "Transcript", [])

        written = note.read_text(encoding="utf-8")
        assert "Do not delete this." in written
        assert "Transcript" in written

    def test_the_creation_date_stops_meaning_last_synced(self, tmp_path):
        dest, note = self._publish(tmp_path)
        note.write_text(
            re.sub(r"created: .*", "created: 2020-01-01", note.read_text(encoding="utf-8")),
            encoding="utf-8",
        )

        dest.publish("Meeting Notes", "Transcript v2", [])

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
            dest.publish("Meeting Notes", f"Transcript v{version}", [])

        written = note.read_text(encoding="utf-8")
        assert written.count(notemerge.MANAGED_BEGIN) == 1
        assert written.count("Transcript v") == 1


class TestAppleNotesIdentifiesNotesById:
    """Deleting by title destroyed notes the user had written themselves."""

    def _run(self, tmp_path, returncode=0, stdout="x-coredata://Store/ICNote/p7", **kwargs):
        dest = AppleNotesDestination(folder_name="reMarkable")
        with patch("living_ink.destinations.subprocess.run") as run:
            run.return_value = MagicMock(returncode=returncode, stdout=stdout, stderr="")
            result = dest.publish("Meeting Notes", "body", [], **kwargs)
        return dest, result, run.call_args[0][0][2]

    def test_a_first_publish_deletes_nothing(self, tmp_path):
        """With no recorded id, a matching title might be somebody else's note."""
        _, result, script = self._run(tmp_path)

        assert result is True
        assert "delete note" not in script
        assert "delete (every" not in script

    def test_a_recorded_id_is_deleted_by_id(self, tmp_path):
        _, _, script = self._run(tmp_path, existing_id="x-coredata://Store/ICNote/p3")

        assert "delete note id" in script
        assert "x-coredata://Store/ICNote/p3" in script
        assert "whose name is noteName" not in script

    def test_an_id_falls_back_to_searching_the_folder(self, tmp_path):
        """`note id` fails if the note moved; the whose-clause still matches by id."""
        _, _, script = self._run(tmp_path, existing_id="x-coredata://Store/ICNote/p3")

        assert "whose id is" in script

    def test_title_matching_needs_explicit_permission(self, tmp_path):
        """Only granted when sync state proves we published this note before."""
        _, _, script = self._run(tmp_path, adopt_by_name=True)

        assert "whose name is noteName" in script

    def test_an_id_outranks_title_matching(self, tmp_path):
        _, _, script = self._run(
            tmp_path, existing_id="x-coredata://Store/ICNote/p3", adopt_by_name=True
        )

        assert "whose name is noteName" not in script

    def test_the_new_note_id_is_reported_back(self, tmp_path):
        dest, _, script = self._run(tmp_path)

        assert "return id of newNote" in script
        assert dest.last_external_id == "x-coredata://Store/ICNote/p7"

    def test_an_empty_reply_records_no_id(self, tmp_path):
        """Better no id than an empty string that would look like one."""
        dest, _, _ = self._run(tmp_path, stdout="\n")

        assert dest.last_external_id is None

    def test_a_stale_id_is_cleared_before_publishing(self, tmp_path):
        dest = AppleNotesDestination()
        dest.last_external_id = "x-coredata://old"
        with patch("living_ink.destinations.subprocess.run") as run:
            run.return_value = MagicMock(returncode=1, stdout="", stderr="boom")
            with patch("living_ink.destinations.time.sleep"):
                with pytest.raises(DestinationUnavailable):
                    dest.publish("Meeting Notes", "body", [])

        assert dest.last_external_id is None

    def test_an_id_with_a_quote_is_escaped(self, tmp_path):
        """AppleScript is assembled as text, so every value has to be quoted."""
        _, _, script = self._run(tmp_path, existing_id='weird" id')

        assert '\\"' in script


class TestObsidianIgnoresIdentityArguments:
    """Obsidian finds its note by path, so both arguments are accepted and unused."""

    def test_publishing_with_an_id_still_works(self, tmp_path):
        dest = ObsidianDestination(vault_path=str(tmp_path))
        assert dest.publish("Note", "body", [], existing_id="ignored") is True
        assert (tmp_path / "Note.md").exists()

    def test_no_external_id_is_reported(self, tmp_path):
        dest = ObsidianDestination(vault_path=str(tmp_path))
        dest.publish("Note", "body", [])
        assert dest.last_external_id is None


class TestObsidianNoteIdentity:
    """The document id is the identity of a note; the filename only labels it."""

    def _dest(self, tmp_path):
        return ObsidianDestination(vault_path=str(tmp_path))

    def test_the_document_id_is_stamped_into_the_note(self, tmp_path):
        self._dest(tmp_path).publish("Notes", "body", [], doc_id="doc-1")
        assert "living_ink_id: doc-1" in (tmp_path / "Notes.md").read_text(encoding="utf-8")

    def test_the_id_survives_a_resync(self, tmp_path):
        dest = self._dest(tmp_path)
        dest.publish("Notes", "v1", [], doc_id="doc-1")
        dest.publish("Notes", "v2", [], doc_id="doc-1")
        written = (tmp_path / "Notes.md").read_text(encoding="utf-8")
        assert written.count("living_ink_id: doc-1") == 1

    def test_a_note_without_an_id_is_not_stamped(self, tmp_path):
        """Publishing without an id behaves exactly as it did before."""
        self._dest(tmp_path).publish("Notes", "body", [])
        assert "living_ink_id" not in (tmp_path / "Notes.md").read_text(encoding="utf-8")

    def test_a_different_document_with_the_same_title_gets_its_own_note(self, tmp_path):
        """Two notebooks called 'Notes' used to merge into one file."""
        dest = self._dest(tmp_path)
        dest.publish("Notes", "first notebook", [], doc_id="doc-1")
        dest.publish("Notes", "second notebook", [], doc_id="doc-2")

        assert "first notebook" in (tmp_path / "Notes.md").read_text(encoding="utf-8")
        assert "second notebook" in (tmp_path / "Notes (2).md").read_text(encoding="utf-8")

    def test_the_same_document_keeps_its_note_rather_than_multiplying(self, tmp_path):
        dest = self._dest(tmp_path)
        dest.publish("Notes", "v1", [], doc_id="doc-1")
        dest.publish("Notes", "v2", [], doc_id="doc-1")
        assert not (tmp_path / "Notes (2).md").exists()

    def test_a_third_collision_takes_the_next_free_name(self, tmp_path):
        dest = self._dest(tmp_path)
        for index in range(1, 4):
            dest.publish("Notes", f"notebook {index}", [], doc_id=f"doc-{index}")
        assert (tmp_path / "Notes (3).md").exists()

    def test_a_note_predating_ids_is_adopted_not_duplicated(self, tmp_path):
        """Everything synced before this existed carries no id, and is still ours."""
        dest = self._dest(tmp_path)
        dest.publish("Notes", "v1", [])
        dest.publish("Notes", "v2", [], doc_id="doc-1")

        assert not (tmp_path / "Notes (2).md").exists()
        assert "living_ink_id: doc-1" in (tmp_path / "Notes.md").read_text(encoding="utf-8")

    def test_someone_elses_note_is_still_merged_into(self, tmp_path):
        """A hand-written note has no id; refusing to touch it would orphan the sync."""
        note = tmp_path / "Notes.md"
        note.write_text("# Mine\n\nKeep this.\n", encoding="utf-8")

        self._dest(tmp_path).publish("Notes", "Transcript", [], doc_id="doc-1")

        written = note.read_text(encoding="utf-8")
        assert "Keep this." in written
        assert "Transcript" in written

    def test_where_the_note_landed_is_reported(self, tmp_path):
        dest = self._dest(tmp_path)
        dest.publish("Notes", "body", [], sub_folder="Work", doc_id="doc-1")
        assert dest.last_target == "Work/Notes.md"

    def test_the_reported_target_is_the_name_actually_used(self, tmp_path):
        dest = self._dest(tmp_path)
        dest.publish("Notes", "first", [], doc_id="doc-1")
        dest.publish("Notes", "second", [], doc_id="doc-2")
        assert dest.last_target == "Notes (2).md"

    def test_the_attachments_follow_the_note_that_was_written(self, tmp_path, monkeypatch):
        """A renamed note must not point its links at the other document's images."""
        page = tmp_path / "page-1.png"
        page.write_bytes(b"png")

        dest = ObsidianDestination(vault_path=str(tmp_path), attachments_folder="_attachments")
        dest.publish("Notes", "first", [], doc_id="doc-1")
        dest.publish("Notes", "second", [page], doc_id="doc-2")

        assert (tmp_path / "_attachments" / "Notes (2)" / "page-1.png").exists()
