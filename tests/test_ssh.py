"""Tests for the USB/SSH transport client."""

import json
from unittest.mock import MagicMock

from living_ink.ssh import Document, SSHClient


def _doc(doc_id: str) -> Document:
    return Document(id=doc_id, hash="h", name=doc_id, doc_type="DocumentType")


class TestFileTypeCache:
    """get_file_type() must not issue one SSH round-trip per lookup."""

    def test_batch_load_serves_subsequent_lookups(self):
        client = SSHClient()
        batch_output = (
            '===FILE===doc-a\n{"fileType": "pdf"}\n'
            '===FILE===doc-b\n{"fileType": "epub"}\n'
            '===FILE===doc-c\n{"fileType": ""}\n'
        )
        client._ssh_command = MagicMock(return_value=batch_output)
        client._scp_download = MagicMock(side_effect=AssertionError("must not probe per document"))

        assert client.get_file_type(_doc("doc-a")) == "pdf"
        assert client.get_file_type(_doc("doc-b")) == "epub"
        assert client.get_file_type(_doc("doc-c")) == ""

        # One batch read total, regardless of how many documents were asked about.
        assert client._ssh_command.call_count == 1

    def test_repeated_lookup_is_memoised(self):
        client = SSHClient()
        client._ssh_command = MagicMock(return_value="")
        client._scp_download = MagicMock(
            return_value=json.dumps({"fileType": "pdf"}).encode("utf-8")
        )

        doc = _doc("doc-a")
        assert client.get_file_type(doc) == "pdf"
        assert client.get_file_type(doc) == "pdf"
        assert client.get_file_type(doc) == "pdf"

        # The per-document fallback probe runs once, then the cache serves it.
        assert client._scp_download.call_count == 1

    def test_missing_content_file_is_cached_as_none(self):
        client = SSHClient()
        client._ssh_command = MagicMock(return_value="")
        client._scp_download = MagicMock(side_effect=RuntimeError("no such file"))

        doc = _doc("notebook-1")
        assert client.get_file_type(doc) is None
        assert client.get_file_type(doc) is None
        assert client._scp_download.call_count == 1

    def test_batch_failure_falls_back_to_single_probe(self):
        client = SSHClient()
        client._ssh_command = MagicMock(side_effect=RuntimeError("ssh down"))
        client._scp_download = MagicMock(
            return_value=json.dumps({"fileType": "epub"}).encode("utf-8")
        )

        assert client.get_file_type(_doc("doc-a")) == "epub"
        # The failed batch read is not retried for the next document.
        assert client.get_file_type(_doc("doc-b")) == "epub"
        assert client._ssh_command.call_count == 1
