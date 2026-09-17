"""Tests for living_ink.destinations module.

Covers Destination abstract base class, AppleNotesDestination,
and ObsidianDestination including full folder mirroring, root folder
configuration, attachment handling, and filename sanitization.
"""

from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

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


class TestStateFilePath:
    """Tests for state file storage under data/ and legacy migration."""

    def test_state_file_in_data_dir(self, tmp_path, monkeypatch):
        """get_state_file_path returns path inside DATA_DIR."""
        from living_ink.pipeline import get_state_file_path

        monkeypatch.setattr("living_ink.pipeline.DATA_DIR", tmp_path / "data")
        monkeypatch.setattr("living_ink.pipeline.ROOT", tmp_path)
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)

        path = get_state_file_path("Obsidian")
        assert path == tmp_path / "data" / "processed_notebooks_Obsidian.json"

    def test_legacy_state_file_auto_migration(self, tmp_path, monkeypatch):
        """Legacy state file in ROOT is automatically moved into DATA_DIR."""
        from living_ink.pipeline import get_state_file_path

        legacy = tmp_path / "processed_notebooks_Obsidian.json"
        legacy.write_text('{"doc1": 1}', encoding="utf-8")

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setattr("living_ink.pipeline.DATA_DIR", data_dir)
        monkeypatch.setattr("living_ink.pipeline.ROOT", tmp_path)

        migrated_path = get_state_file_path("Obsidian")
        assert migrated_path == data_dir / "processed_notebooks_Obsidian.json"
        assert migrated_path.exists()
        assert not legacy.exists()
