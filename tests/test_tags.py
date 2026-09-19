"""Tests for reMarkable tag extraction and destination publishing."""

import json
import zipfile
from unittest.mock import MagicMock

from living_ink.api import FallbackClient, get_document_tags
from living_ink.destinations import AppleNotesDestination, ObsidianDestination
from living_ink.extract import (
    extract_tags_from_dict,
    extract_tags_from_zip,
    normalize_tag,
)
from living_ink.ssh import Document as SSHDocument
from living_ink.ssh import SSHClient
from tests.builders import make_both


class TestTagNormalization:
    """Test normalize_tag helper."""

    def test_strip_hash(self):
        assert normalize_tag("#data-engineering") == "data-engineering"
        assert normalize_tag("###notes") == "notes"

    def test_replaces_spaces(self):
        assert normalize_tag("data engineering") == "data-engineering"
        assert normalize_tag("machine  learning  notes") == "machine-learning-notes"

    def test_strips_special_characters(self):
        assert normalize_tag("tag!@$%^&*()") == "tag"
        assert normalize_tag("valid-tag_1/nested") == "valid-tag_1/nested"


class TestExtractTagsFromDict:
    """Test extract_tags_from_dict parsing logic."""

    def test_document_level_string_tags(self):
        data = {"tags": ["work", "project-x"]}
        assert extract_tags_from_dict(data) == ["work", "project-x"]

    def test_document_level_object_tags(self):
        data = {"tags": [{"name": "work"}, {"name": "project-x"}]}
        assert extract_tags_from_dict(data) == ["work", "project-x"]

    def test_page_level_tags(self):
        data = {
            "pageTags": [
                {"name": "data-engineering", "pageId": "uuid-1", "timestamp": 12345},
                {"name": "data-engineering", "pageId": "uuid-2", "timestamp": 12346},
            ]
        }
        assert extract_tags_from_dict(data) == ["data-engineering"]

    def test_combined_document_and_page_tags(self):
        data = {
            "tags": [{"name": "reference"}],
            "pageTags": [
                {"name": "data-engineering", "pageId": "uuid-1"},
                {"name": "reference", "pageId": "uuid-2"},  # Duplicate
            ],
        }
        assert extract_tags_from_dict(data) == ["reference", "data-engineering"]

    def test_empty_tags(self):
        assert extract_tags_from_dict({}) == []
        assert extract_tags_from_dict({"tags": [], "pageTags": []}) == []


class TestExtractTagsFromZip:
    """Test extract_tags_from_zip extraction from zip archive."""

    def test_extract_from_zip_with_content_file(self, tmp_path):
        zip_path = tmp_path / "test.zip"
        content_json = {
            "fileType": "pdf",
            "pageTags": [
                {"name": "data-engineering", "pageId": "p1"},
                {"name": "cloud", "pageId": "p2"},
            ],
            "tags": [],
        }

        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("doc-123.content", json.dumps(content_json))

        tags = extract_tags_from_zip(zip_path)
        assert tags == ["data-engineering", "cloud"]

    def test_extract_from_empty_zip(self, tmp_path):
        zip_path = tmp_path / "empty.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("dummy.txt", "hello")

        tags = extract_tags_from_zip(zip_path)
        assert tags == []


class TestObsidianFrontmatterTags:
    """Test tags in Obsidian YAML frontmatter."""

    def test_pdf_with_tags(self, tmp_path):
        dest = ObsidianDestination(vault_path=str(tmp_path))
        dummy_pdf = tmp_path / "doc.pdf"
        dummy_pdf.write_bytes(b"%PDF-1.4")

        dest.publish(
            *make_both(
                "Data Systems",
                "Some content",
                source="pdf",
                source_file=dummy_pdf,
                tags=["data-engineering", "databases"],
            )
        )

        note_file = tmp_path / "Data Systems.md"
        content = note_file.read_text(encoding="utf-8")
        assert "tags:\n  - remarkable\n  - pdf\n  - data-engineering\n  - databases" in content

    def test_handwritten_with_tags(self, tmp_path):
        dest = ObsidianDestination(vault_path=str(tmp_path))
        dest.publish(*make_both("Meeting", "Notes", tags=["standup", "sprint-42"]))

        note_file = tmp_path / "Meeting.md"
        content = note_file.read_text(encoding="utf-8")
        assert "tags:\n  - remarkable\n  - handwritten\n  - standup\n  - sprint-42" in content

    def test_deduplicates_standard_tags(self, tmp_path):
        dest = ObsidianDestination(vault_path=str(tmp_path))
        dest.publish(*make_both("Note", "Notes", tags=["remarkable", "handwritten", "custom"]))

        note_file = tmp_path / "Note.md"
        content = note_file.read_text(encoding="utf-8")
        assert "tags:\n  - remarkable\n  - handwritten\n  - custom" in content


class TestAppleNotesTags:
    """Test tag badge handling in AppleNotesDestination."""

    def test_tags_appended_as_hashtags(self, tmp_path, monkeypatch):
        dest = AppleNotesDestination(folder_name="Living Ink")
        captured_script = []

        def mock_run(*args, **kwargs):
            captured_script.append(args[0][2])
            res = MagicMock()
            res.returncode = 0
            return res

        import subprocess

        monkeypatch.setattr(subprocess, "run", mock_run)

        dest.publish(*make_both("Test Note", "Body text", tags=["data-engineering", "python"]))

        assert len(captured_script) == 1
        script = captured_script[0]
        assert "#data-engineering" in script
        assert "#python" in script


class TestClientTagHelpers:
    """Test SSHClient and FallbackClient tag retrieval."""

    def test_ssh_client_get_tags(self):
        client = SSHClient.__new__(SSHClient)
        content_json = {
            "pageTags": [{"name": "data-engineering", "pageId": "123"}],
            "tags": [],
        }
        client._scp_download = MagicMock(return_value=json.dumps(content_json).encode("utf-8"))

        doc = SSHDocument(
            id="doc-123",
            hash="h1",
            name="Book",
            doc_type="DocumentType",
        )
        tags = client.get_tags(doc)
        assert tags == ["data-engineering"]
        assert doc.tags == ["data-engineering"]

    def test_fallback_client_get_tags(self):
        mock_active = MagicMock()
        mock_active.get_tags.return_value = ["ai", "machine-learning"]

        fb = FallbackClient(primary_client=mock_active, backup_client=None, primary_name="Primary")
        tags = fb.get_tags("doc")
        assert tags == ["ai", "machine-learning"]

    def test_get_document_tags_helper(self):
        client = MagicMock()
        client.get_tags.return_value = ["tag-1"]
        doc = MagicMock()
        assert get_document_tags(client, doc) == ["tag-1"]

        # Fallback to doc.tags if client doesn't have get_tags
        client_no_tags = object()
        doc.tags = ["tag-2"]
        assert get_document_tags(client_no_tags, doc) == ["tag-2"]
