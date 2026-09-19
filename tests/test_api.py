"""Tests for living_ink.api module.

Covers get_rmapi client factory under SSH and Cloud modes with automatic fallback,
and FallbackClient failover behavior.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from living_ink.api import (
    FallbackClient,
    download_raw_file,
    get_document_tags,
    get_file_type,
    get_rmapi,
    register_and_get_token,
    resolve_stored_token,
)
from living_ink.config.credentials import CLOUD_TOKEN, read_secret, write_secret
from living_ink.transport import TransportUnavailable, UnsupportedOperation


def test_get_rmapi_ssh_preferred_connected(monkeypatch, tmp_path):
    """get_rmapi returns SSH client when connected over USB and no cloud token exists."""
    monkeypatch.setenv("REMARKABLE_PREFERRED_CONNECTION", "ssh")
    monkeypatch.setenv("REMARKABLE_USE_SSH", "true")
    monkeypatch.delenv("REMARKABLE_TOKEN", raising=False)

    with patch("pathlib.Path.home", return_value=tmp_path):
        with patch("living_ink.ssh.create_ssh_client") as mock_create_ssh:
            mock_ssh = MagicMock()
            mock_ssh.check_connection.return_value = True
            mock_create_ssh.return_value = mock_ssh

            client = get_rmapi()
            assert client is mock_ssh


def test_get_rmapi_ssh_preferred_with_cloud_backup(monkeypatch):
    """get_rmapi returns FallbackClient when both SSH and Cloud are configured."""
    monkeypatch.setenv("REMARKABLE_PREFERRED_CONNECTION", "ssh")
    monkeypatch.setenv("REMARKABLE_USE_SSH", "true")
    monkeypatch.setenv("REMARKABLE_TOKEN", "cloud-token")

    with patch("living_ink.ssh.create_ssh_client") as mock_create_ssh:
        with patch("living_ink.sync.load_client_from_token") as mock_load_cloud:
            mock_ssh = MagicMock()
            mock_ssh.check_connection.return_value = True
            mock_create_ssh.return_value = mock_ssh

            mock_cloud = MagicMock()
            mock_load_cloud.return_value = mock_cloud

            client = get_rmapi()
            assert isinstance(client, FallbackClient)
            assert client.active is mock_ssh
            assert client.backup is mock_cloud


def test_get_rmapi_ssh_unplugged_falls_back_to_cloud(monkeypatch):
    """get_rmapi automatically falls back to Cloud when USB SSH is not connected."""
    monkeypatch.setenv("REMARKABLE_PREFERRED_CONNECTION", "ssh")
    monkeypatch.setenv("REMARKABLE_USE_SSH", "true")
    monkeypatch.setenv("REMARKABLE_TOKEN", "cloud-token")

    with patch("living_ink.ssh.create_ssh_client") as mock_create_ssh:
        with patch("living_ink.sync.load_client_from_token") as mock_load_cloud:
            mock_ssh = MagicMock()
            mock_ssh.check_connection.return_value = False
            mock_create_ssh.return_value = mock_ssh

            mock_cloud = MagicMock()
            mock_load_cloud.return_value = mock_cloud

            client = get_rmapi()
            assert isinstance(client, FallbackClient)
            assert client.active is mock_cloud


def test_get_rmapi_never_writes_the_stored_token(monkeypatch, isolated_home):
    """Building a client must not touch ~/.rmapi.

    It used to write the token it had just resolved back to ~/.rmapi, so simply
    constructing a client with REMARKABLE_TOKEN set overwrote whatever real
    credential was already stored there.
    """
    monkeypatch.setenv("REMARKABLE_PREFERRED_CONNECTION", "cloud")
    monkeypatch.setenv("REMARKABLE_TOKEN", "from-the-environment")
    rmapi = isolated_home / ".rmapi"
    rmapi.write_text("the-real-token", encoding="utf-8")

    with patch("living_ink.ssh.create_ssh_client") as mock_create_ssh:
        with patch("living_ink.sync.load_client_from_token") as mock_load_cloud:
            mock_create_ssh.return_value = MagicMock()
            mock_load_cloud.return_value = MagicMock()

            get_rmapi()

    assert rmapi.read_text(encoding="utf-8") == "the-real-token"


def test_get_rmapi_cloud_preferred_with_ssh_backup(monkeypatch):
    """get_rmapi returns FallbackClient with Cloud as primary when preferred."""
    monkeypatch.setenv("REMARKABLE_PREFERRED_CONNECTION", "cloud")
    monkeypatch.setenv("REMARKABLE_TOKEN", "cloud-token")

    with patch("living_ink.ssh.create_ssh_client") as mock_create_ssh:
        with patch("living_ink.sync.load_client_from_token") as mock_load_cloud:
            mock_ssh = MagicMock()
            mock_create_ssh.return_value = mock_ssh

            mock_cloud = MagicMock()
            mock_load_cloud.return_value = mock_cloud

            client = get_rmapi()
            assert isinstance(client, FallbackClient)
            assert client.active is mock_cloud
            assert client.backup is mock_ssh


def test_get_rmapi_ssh_unplugged_and_no_token_is_transport_unavailable(monkeypatch, isolated_home):
    """The bottom of the ladder names itself, so the CLI can print it.

    A plain RuntimeError here is indistinguishable from a bug, and the front
    end would have to choose between swallowing real failures and showing a
    traceback for an unplugged cable.
    """
    monkeypatch.setenv("REMARKABLE_PREFERRED_CONNECTION", "ssh")
    monkeypatch.delenv("REMARKABLE_TOKEN", raising=False)

    with patch("living_ink.ssh.create_ssh_client") as mock_create_ssh:
        mock_ssh = MagicMock()
        mock_ssh.check_connection.return_value = False
        mock_create_ssh.return_value = mock_ssh

        with pytest.raises(TransportUnavailable, match="USB SSH"):
            get_rmapi()


def test_get_rmapi_cloud_unconfigured_and_ssh_down_is_transport_unavailable(
    monkeypatch, isolated_home
):
    """Same failure from the other preference, same exception type."""
    monkeypatch.setenv("REMARKABLE_PREFERRED_CONNECTION", "cloud")
    monkeypatch.delenv("REMARKABLE_TOKEN", raising=False)

    with patch("living_ink.ssh.create_ssh_client") as mock_create_ssh:
        mock_ssh = MagicMock()
        mock_ssh.check_connection.return_value = False
        mock_create_ssh.return_value = mock_ssh

        with pytest.raises(TransportUnavailable, match="No reMarkable token"):
            get_rmapi()


def test_transport_unavailable_is_still_a_runtime_error():
    """Existing broad handlers keep working.

    ``WatchCommand`` catches ``Exception`` per cycle and anything already
    written against ``RuntimeError`` must keep treating this as a failed
    attempt rather than stop recognising it.
    """
    assert issubclass(TransportUnavailable, RuntimeError)


def test_fallback_client_get_meta_items_failover():
    """FallbackClient switches to backup client when primary get_meta_items fails."""
    primary = MagicMock()
    primary.get_meta_items.side_effect = RuntimeError("SSH connection lost")

    backup = MagicMock()
    backup_doc = MagicMock()
    backup.get_meta_items.return_value = [backup_doc]

    client = FallbackClient(primary_client=primary, backup_client=backup)
    items = client.get_meta_items()

    assert items == [backup_doc]
    assert client.active is backup
    backup.get_meta_items.assert_called_once()


def test_fallback_client_download_failover():
    """FallbackClient switches to backup client when primary download fails."""
    primary = MagicMock()
    primary.download.side_effect = RuntimeError("Download error")

    backup = MagicMock()
    backup.download.return_value = b"zip-content"
    backup.get_doc.return_value = MagicMock(id="doc-123")

    doc = MagicMock(id="doc-123")
    client = FallbackClient(primary_client=primary, backup_client=backup)
    content = client.download(doc)

    assert content == b"zip-content"
    assert client.active is backup


class TestFallbackIsUniform:
    """Every transport operation, not just two, retries on the backup client."""

    FAILOVER_CALLS = [
        ("check_connection", (), True),
        ("get_doc", ("doc-1",), "a-doc"),
        ("get_file_type", (MagicMock(),), "pdf"),
        ("download_raw_file", (MagicMock(), "pdf"), b"%PDF"),
        ("get_tags", (MagicMock(),), ["work"]),
    ]

    @pytest.mark.parametrize("method,args,expected", FAILOVER_CALLS)
    def test_failover(self, method, args, expected):
        """A primary failure is retried on the backup for each operation."""
        primary = MagicMock()
        getattr(primary, method).side_effect = RuntimeError("primary down")
        backup = MagicMock()
        getattr(backup, method).return_value = expected

        client = FallbackClient(primary_client=primary, backup_client=backup)

        assert getattr(client, method)(*args) == expected
        assert client.active is backup

    @pytest.mark.parametrize("method,args,expected", FAILOVER_CALLS)
    def test_raises_when_there_is_no_backup(self, method, args, expected):
        """Without a backup the original error surfaces instead of being masked."""
        primary = MagicMock()
        getattr(primary, method).side_effect = RuntimeError("primary down")

        client = FallbackClient(primary_client=primary, backup_client=None)

        with pytest.raises(RuntimeError, match="primary down"):
            getattr(client, method)(*args)

    def test_unsupported_operation_is_not_retried(self):
        """A permanent capability gap propagates without touching the backup."""
        primary = MagicMock()
        primary.get_tags.side_effect = UnsupportedOperation("not on this transport")
        backup = MagicMock()

        client = FallbackClient(primary_client=primary, backup_client=backup)

        with pytest.raises(UnsupportedOperation):
            client.get_tags(MagicMock())
        backup.get_tags.assert_not_called()
        assert client.active is primary


class TestTransportHelpers:
    """The module-level helpers degrade when a transport cannot answer."""

    def test_get_file_type_falls_back_to_the_document_name(self):
        """An unsupported transport leaves the name as the only signal."""
        client = MagicMock()
        client.get_file_type.side_effect = UnsupportedOperation("no")
        doc = MagicMock(VissibleName="Contract.pdf")

        assert get_file_type(client, doc) == "pdf"

    def test_get_file_type_defaults_to_notebook(self):
        """A plain name with no extension is a notebook."""
        client = MagicMock()
        client.get_file_type.return_value = None
        doc = MagicMock(VissibleName="Meeting Notes")

        assert get_file_type(client, doc) == "notebook"

    def test_download_raw_file_returns_none_when_unsupported(self):
        """An unsupported raw download is a None, not an exception."""
        client = MagicMock()
        client.download_raw_file.side_effect = UnsupportedOperation("no")

        assert download_raw_file(client, MagicMock(), "pdf") is None

    def test_get_document_tags_falls_back_to_the_document(self):
        """Tags already on the document are used when the transport fails."""
        client = MagicMock()
        client.get_tags.side_effect = RuntimeError("offline")
        doc = MagicMock(tags=["ideas"])

        assert get_document_tags(client, doc) == ["ideas"]


class TestWhereTheCloudTokenComesFrom:
    """Three routes to one token, and what each one leaves behind."""

    @pytest.fixture(autouse=True)
    def _isolated(self, tmp_path, monkeypatch):
        """Redirect both the config directory and the home directory.

        Args:
            tmp_path: Pytest temporary directory.
            monkeypatch: Pytest monkeypatch fixture.
        """
        monkeypatch.setenv("LIVING_INK_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.delenv("REMARKABLE_TOKEN", raising=False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        self.home = tmp_path

    def test_no_token_anywhere_is_none(self):
        assert resolve_stored_token() is None

    def test_the_stored_credential_is_used(self):
        write_secret(CLOUD_TOKEN, "stored-token")

        assert resolve_stored_token() == "stored-token"

    def test_a_token_in_the_environment_wins(self, monkeypatch):
        write_secret(CLOUD_TOKEN, "stored-token")
        monkeypatch.setenv("REMARKABLE_TOKEN", "env-token")

        assert resolve_stored_token() == "env-token"

    def test_a_legacy_rmapi_file_is_still_read(self):
        (self.home / ".rmapi").write_text("legacy-token", encoding="utf-8")

        assert resolve_stored_token() == "legacy-token"

    def test_a_legacy_token_moves_itself_into_the_credentials_directory(self):
        """An existing install upgrades without anyone retyping a pairing code."""
        (self.home / ".rmapi").write_text("legacy-token", encoding="utf-8")

        resolve_stored_token()

        assert read_secret(CLOUD_TOKEN) == "legacy-token"

    def test_the_legacy_file_is_left_where_it_was(self):
        """Copy, not move: downgrading must not mean re-pairing."""
        legacy = self.home / ".rmapi"
        legacy.write_text("legacy-token", encoding="utf-8")

        resolve_stored_token()

        assert legacy.read_text(encoding="utf-8") == "legacy-token"

    def test_the_stored_credential_beats_a_stale_rmapi_file(self):
        write_secret(CLOUD_TOKEN, "current-token")
        (self.home / ".rmapi").write_text("stale-token", encoding="utf-8")

        assert resolve_stored_token() == "current-token"

    def test_an_unreadable_legacy_file_is_not_a_crash(self):
        (self.home / ".rmapi").write_bytes(b"\xff\xfe\x00bad")

        assert resolve_stored_token() is None

    def test_a_caller_with_a_config_in_hand_reads_that_profile(self, tmp_path):
        """``living-ink status --root`` must report on the config it resolved.

        Reading the default profile instead reported the connection state of an
        account the run was never going to use.
        """
        other = tmp_path / "other" / "config.yml"
        write_secret(CLOUD_TOKEN, "default-profile-token")
        write_secret(CLOUD_TOKEN, "other-profile-token", config_path=other)

        assert resolve_stored_token(config_path=other) == "other-profile-token"

    def test_migration_lands_in_the_profile_that_asked_for_it(self, tmp_path):
        other = tmp_path / "other" / "config.yml"
        (self.home / ".rmapi").write_text("legacy-token", encoding="utf-8")

        resolve_stored_token(config_path=other)

        assert read_secret(CLOUD_TOKEN, config_path=other) == "legacy-token"
        assert read_secret(CLOUD_TOKEN) is None

    def test_pairing_writes_the_token_to_the_credentials_directory(self, monkeypatch):
        """The whole point: a fresh pairing never touches ``~/.rmapi``."""
        monkeypatch.setattr(
            "living_ink.sync.register_device", lambda code: {"devicetoken": "fresh"}
        )

        returned = register_and_get_token("abcdefgh")

        assert read_secret(CLOUD_TOKEN) == returned
        assert "fresh" in returned
        assert not (self.home / ".rmapi").exists()
