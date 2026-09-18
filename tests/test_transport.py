"""Tests for the living_ink.transport seam."""

from unittest.mock import MagicMock

import pytest

from living_ink.api import FallbackClient
from living_ink.models import Document
from living_ink.ssh import SSHClient
from living_ink.sync import RemarkableClient
from living_ink.transport import (
    DeviceInfo,
    RemarkableTransport,
    UnsupportedOperation,
    require_document,
)


@pytest.mark.parametrize("cls", [SSHClient, RemarkableClient, FallbackClient])
def test_shipped_clients_satisfy_the_protocol(cls):
    """Every shipped transport implements the full RemarkableTransport surface."""
    required = [name for name in vars(RemarkableTransport) if not name.startswith("_")]
    assert required, "the protocol should declare at least one method"
    missing = [name for name in required if not callable(getattr(cls, name, None))]
    assert missing == [], f"{cls.__name__} is missing {missing}"


def test_unsupported_operation_is_a_not_implemented_error():
    """Callers can catch UnsupportedOperation as the stdlib error it specialises."""
    assert issubclass(UnsupportedOperation, NotImplementedError)


class TestDeviceInfo:
    """Asking the tablet what it is, across the transports that can answer."""

    def test_it_describes_itself_for_a_bug_report(self):
        """The whole point is that a user can paste one line into an issue."""
        info = DeviceInfo(model="reMarkable 2", firmware="3.5.2.1807", screen=(1404, 1872))
        assert info.describe() == "reMarkable 2 firmware 3.5.2.1807 (1404×1872)"

    def test_an_unreadable_firmware_does_not_leave_a_dangling_word(self):
        info = DeviceInfo(model="unknown", firmware="", screen=(1404, 1872))
        assert info.describe() == "unknown (1404×1872)"

    def test_the_cloud_refuses_because_it_serves_documents_not_hardware(self):
        client = RemarkableClient.__new__(RemarkableClient)
        with pytest.raises(UnsupportedOperation, match="Connect over USB"):
            client.get_device_info()


class TestSSHDeviceInfo:
    """SSH is the only transport that can see the hardware."""

    def _client(self, output):
        client = SSHClient.__new__(SSHClient)
        client._device_info = None
        client._ssh_command = MagicMock(return_value=output)
        return client

    def test_it_reads_the_model_and_firmware_in_one_round_trip(self):
        client = self._client("reMarkable 2.0\n===\nREMARKABLE_RELEASE_VERSION=3.5.2.1807\n")
        info = client.get_device_info()

        assert (info.model, info.firmware) == ("reMarkable 2", "3.5.2.1807")
        assert info.screen == (1404, 1872)
        client._ssh_command.assert_called_once()

    def test_a_missing_firmware_file_still_yields_the_model(self):
        """Half an answer beats an exception when the model is the useful half."""
        info = self._client("reMarkable 2.0\n===\n").get_device_info()
        assert (info.model, info.firmware) == ("reMarkable 2", "")

    def test_a_device_that_says_nothing_is_reported_as_unknown(self):
        info = self._client("\n===\n").get_device_info()
        assert info.model == "unknown"
        assert info.screen == (1404, 1872)

    def test_the_answer_is_cached_because_the_device_does_not_change(self):
        client = self._client("reMarkable 2.0\n===\nREMARKABLE_RELEASE_VERSION=3.5\n")
        assert client.get_device_info() is client.get_device_info()
        client._ssh_command.assert_called_once()

    def test_an_unreachable_tablet_raises_rather_than_inventing_a_device(self):
        client = SSHClient.__new__(SSHClient)
        client._device_info = None
        client._ssh_command = MagicMock(side_effect=RuntimeError("no route to host"))
        with pytest.raises(RuntimeError, match="Could not read device info"):
            client.get_device_info()


class TestFallbackDeviceInfo:
    """The one capability gap that is not uniform across transports."""

    def _pair(self):
        cloud = MagicMock()
        cloud.get_device_info.side_effect = UnsupportedOperation("cloud cannot")
        ssh = MagicMock()
        ssh.get_device_info.return_value = DeviceInfo("reMarkable 2", "3.5", (1404, 1872))
        return cloud, ssh

    def test_a_cloud_primary_falls_through_to_ssh(self):
        """Every other operation refuses to retry UnsupportedOperation; this one must."""
        cloud, ssh = self._pair()
        client = FallbackClient(primary_client=cloud, backup_client=ssh)

        assert client.get_device_info().model == "reMarkable 2"

    def test_asking_the_device_does_not_change_which_transport_syncs(self):
        """The preference was about fetching documents, and still holds."""
        cloud, ssh = self._pair()
        client = FallbackClient(primary_client=cloud, backup_client=ssh)
        client.get_device_info()

        assert client.active is cloud

    def test_it_raises_when_neither_transport_can_see_the_device(self):
        cloud, _ = self._pair()
        other_cloud = MagicMock()
        other_cloud.get_device_info.side_effect = UnsupportedOperation("nor can this")
        client = FallbackClient(primary_client=cloud, backup_client=other_cloud)

        with pytest.raises(UnsupportedOperation):
            client.get_device_info()

    def test_a_lone_cloud_client_raises_instead_of_looking_for_a_backup(self):
        cloud, _ = self._pair()
        with pytest.raises(UnsupportedOperation):
            FallbackClient(primary_client=cloud, backup_client=None).get_device_info()


class TestDocumentGuard:
    """A document id is not a document, and the error has to say so."""

    def _doc(self):
        return Document(id="abc", hash="h", name="Notes", doc_type="DocumentType")

    def test_a_document_passes_straight_through(self):
        doc = self._doc()
        assert require_document(doc, "get_file_type") is doc

    def test_an_id_names_the_method_and_the_fix(self):
        with pytest.raises(TypeError) as excinfo:
            require_document("c25c3353", "get_file_type")

        message = str(excinfo.value)
        assert "get_file_type() takes a Document, not str" in message
        assert "get_doc('c25c3353')" in message

    def test_some_other_type_still_names_the_method(self):
        with pytest.raises(TypeError, match=r"get_file_type\(\) takes a Document, not int"):
            require_document(7, "get_file_type")

    @pytest.mark.parametrize("client_cls", [SSHClient, RemarkableClient])
    def test_every_client_rejects_an_id(self, client_cls):
        """The guard belongs to the Protocol, so no transport may skip it."""
        client = client_cls.__new__(client_cls)

        with pytest.raises(TypeError, match="takes a Document"):
            client.get_file_type("c25c3353-ce5d-48bd-931b-7a9244afe64d")

    def test_a_caller_error_does_not_look_like_a_transport_failure(self):
        """Failing over would print "the Cloud failed" and hide the mistake."""
        cloud, ssh = MagicMock(), MagicMock()
        cloud.get_file_type.side_effect = TypeError("get_file_type() takes a Document, not str")
        client = FallbackClient(primary_client=cloud, backup_client=ssh)

        with pytest.raises(TypeError):
            client.get_file_type("c25c3353")

        ssh.get_file_type.assert_not_called()
        assert client.active is cloud
