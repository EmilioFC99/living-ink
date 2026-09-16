"""Tests for remarkable_mcp.api module.

Covers get_rmapi client factory under SSH and Cloud modes.
"""

from unittest.mock import MagicMock, patch

from remarkable_mcp.api import get_rmapi


def test_get_rmapi_ssh_mode(monkeypatch):
    """get_rmapi returns SSHClient when REMARKABLE_USE_SSH is set."""
    monkeypatch.setenv("REMARKABLE_USE_SSH", "true")
    monkeypatch.setenv("REMARKABLE_SSH_HOST", "10.11.99.1")

    with patch("remarkable_mcp.ssh.create_ssh_client") as mock_create_ssh:
        mock_client = MagicMock()
        mock_create_ssh.return_value = mock_client

        client = get_rmapi()
        assert client is mock_client
        mock_create_ssh.assert_called_once()


def test_get_rmapi_cloud_mode(monkeypatch, tmp_path):
    """get_rmapi returns Cloud client when REMARKABLE_TOKEN is set."""
    monkeypatch.setenv("REMARKABLE_USE_SSH", "false")
    monkeypatch.setenv("REMARKABLE_TOKEN", "dummy-cloud-token")

    with patch("pathlib.Path.home", return_value=tmp_path):
        with patch("remarkable_mcp.sync.load_client_from_token") as mock_load:
            mock_client = MagicMock()
            mock_load.return_value = mock_client

            client = get_rmapi()
            assert client is mock_client
            mock_load.assert_called_once_with("dummy-cloud-token")
