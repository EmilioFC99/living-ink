"""Tests for remarkable_mcp.api module.

Covers get_rmapi client factory under SSH and Cloud modes with automatic fallback,
and FallbackClient failover behavior.
"""

from unittest.mock import MagicMock, patch

from remarkable_mcp.api import FallbackClient, get_rmapi


def test_get_rmapi_ssh_preferred_connected(monkeypatch, tmp_path):
    """get_rmapi returns SSH client when connected over USB and no cloud token exists."""
    monkeypatch.setenv("REMARKABLE_PREFERRED_CONNECTION", "ssh")
    monkeypatch.setenv("REMARKABLE_USE_SSH", "true")
    monkeypatch.delenv("REMARKABLE_TOKEN", raising=False)

    with patch("pathlib.Path.home", return_value=tmp_path):
        with patch("remarkable_mcp.ssh.create_ssh_client") as mock_create_ssh:
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

    with patch("remarkable_mcp.ssh.create_ssh_client") as mock_create_ssh:
        with patch("remarkable_mcp.sync.load_client_from_token") as mock_load_cloud:
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

    with patch("remarkable_mcp.ssh.create_ssh_client") as mock_create_ssh:
        with patch("remarkable_mcp.sync.load_client_from_token") as mock_load_cloud:
            mock_ssh = MagicMock()
            mock_ssh.check_connection.return_value = False
            mock_create_ssh.return_value = mock_ssh

            mock_cloud = MagicMock()
            mock_load_cloud.return_value = mock_cloud

            client = get_rmapi()
            assert isinstance(client, FallbackClient)
            assert client.active is mock_cloud


def test_get_rmapi_cloud_preferred_with_ssh_backup(monkeypatch):
    """get_rmapi returns FallbackClient with Cloud as primary when preferred."""
    monkeypatch.setenv("REMARKABLE_PREFERRED_CONNECTION", "cloud")
    monkeypatch.setenv("REMARKABLE_TOKEN", "cloud-token")

    with patch("remarkable_mcp.ssh.create_ssh_client") as mock_create_ssh:
        with patch("remarkable_mcp.sync.load_client_from_token") as mock_load_cloud:
            mock_ssh = MagicMock()
            mock_create_ssh.return_value = mock_ssh

            mock_cloud = MagicMock()
            mock_load_cloud.return_value = mock_cloud

            client = get_rmapi()
            assert isinstance(client, FallbackClient)
            assert client.active is mock_cloud
            assert client.backup is mock_ssh


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
