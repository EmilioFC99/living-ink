"""Tests for living_ink.sync, the reMarkable Cloud transport."""

import json
from unittest.mock import MagicMock, patch

from living_ink.models import Document
from living_ink.sync import RemarkableClient


def make_doc(files=None) -> Document:
    """Build a Document with a pre-populated blob index."""
    return Document(
        id="doc-1",
        hash="hash-1",
        name="Notes",
        doc_type="DocumentType",
        files=files if files is not None else [],
    )


class TestCheckConnection:
    """RemarkableClient.check_connection probes the sync root."""

    def test_true_on_http_200(self):
        client = RemarkableClient(device_token="tok")
        with patch.object(client, "_request", return_value=MagicMock(status_code=200)):
            assert client.check_connection() is True

    def test_false_on_non_200(self):
        client = RemarkableClient(device_token="tok")
        with patch.object(client, "_request", return_value=MagicMock(status_code=401)):
            assert client.check_connection() is False

    def test_false_when_the_request_raises(self):
        client = RemarkableClient(device_token="tok")
        with patch.object(client, "_request", side_effect=RuntimeError("offline")):
            assert client.check_connection() is False


class TestFileType:
    """RemarkableClient.get_file_type reads the .content blob."""

    def test_reads_filetype_from_content(self):
        client = RemarkableClient(device_token="tok")
        doc = make_doc([{"id": "doc-1.content", "hash": "h-content"}])
        with patch.object(
            client, "_get_file", return_value=json.dumps({"fileType": "pdf"}).encode()
        ):
            assert client.get_file_type(doc) == "pdf"

    def test_none_for_a_plain_notebook(self):
        client = RemarkableClient(device_token="tok")
        doc = make_doc([{"id": "doc-1.content", "hash": "h-content"}])
        with patch.object(client, "_get_file", return_value=json.dumps({"fileType": ""}).encode()):
            assert client.get_file_type(doc) is None

    def test_none_when_there_is_no_content_member(self):
        client = RemarkableClient(device_token="tok")
        doc = make_doc([{"id": "doc-1.metadata", "hash": "h-meta"}])
        assert client.get_file_type(doc) is None


class TestTags:
    """RemarkableClient.get_tags reads the .content blob and caches on the doc."""

    def test_extracts_and_caches_tags(self):
        client = RemarkableClient(device_token="tok")
        doc = make_doc([{"id": "doc-1.content", "hash": "h-content"}])
        payload = json.dumps({"tags": [{"name": "work"}, {"name": "ideas"}]}).encode()
        with patch.object(client, "_get_file", return_value=payload) as mock_get:
            assert client.get_tags(doc) == ["work", "ideas"]
            assert doc.tags == ["work", "ideas"]
            # Second call is served from the document, not the network.
            assert client.get_tags(doc) == ["work", "ideas"]
            assert mock_get.call_count == 1

    def test_empty_when_content_is_unreadable(self):
        client = RemarkableClient(device_token="tok")
        doc = make_doc([{"id": "doc-1.content", "hash": "h-content"}])
        with patch.object(client, "_get_file", side_effect=RuntimeError("404")):
            assert client.get_tags(doc) == []


class TestDownloadRawFile:
    """RemarkableClient.download_raw_file pulls a member out of the blob index."""

    def test_returns_the_matching_member(self):
        client = RemarkableClient(device_token="tok")
        doc = make_doc(
            [
                {"id": "doc-1.content", "hash": "h-content"},
                {"id": "doc-1.pdf", "hash": "h-pdf"},
            ]
        )
        with patch.object(client, "_get_file", return_value=b"%PDF-1.7") as mock_get:
            assert client.download_raw_file(doc, "pdf") == b"%PDF-1.7"
            mock_get.assert_called_once_with("h-pdf", "doc-1.pdf")

    def test_none_when_the_extension_is_absent(self):
        client = RemarkableClient(device_token="tok")
        doc = make_doc([{"id": "doc-1.content", "hash": "h-content"}])
        assert client.download_raw_file(doc, "epub") is None

    def test_none_when_the_download_fails(self):
        client = RemarkableClient(device_token="tok")
        doc = make_doc([{"id": "doc-1.epub", "hash": "h-epub"}])
        with patch.object(client, "_get_file", side_effect=RuntimeError("gone")):
            assert client.download_raw_file(doc, "epub") is None

    def test_fetches_the_index_when_the_doc_has_none(self):
        client = RemarkableClient(device_token="tok")
        doc = make_doc([])
        index = b"3\nh-pdf:0:doc-1.pdf:0:100\n"
        with patch.object(client, "_get_file", side_effect=[index, b"%PDF-1.7"]):
            assert client.download_raw_file(doc, "pdf") == b"%PDF-1.7"
