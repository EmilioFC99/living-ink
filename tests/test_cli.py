"""Tests for remarkable_mcp.cli module.

Covers the CLI argument parsing and subcommands (status, setup, sync).
"""

from unittest.mock import MagicMock, patch

from remarkable_mcp.cli import cmd_status, get_config_path, get_root, main


def test_get_root(tmp_path):
    """get_root identifies directory with pyproject.toml."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='test'")
    with patch("pathlib.Path.cwd", return_value=tmp_path):
        root = get_root()
        assert (root / "pyproject.toml").exists()


def test_get_config_path(tmp_path, monkeypatch):
    """get_config_path respects LIVING_INK_CONFIG_DIR and relative paths."""
    # Default without env
    monkeypatch.delenv("LIVING_INK_CONFIG_DIR", raising=False)
    p = get_config_path(tmp_path)
    assert p == tmp_path / "config" / "config.yml"

    # With LIVING_INK_CONFIG_DIR
    custom_dir = tmp_path / "custom_config"
    custom_dir.mkdir()
    monkeypatch.setenv("LIVING_INK_CONFIG_DIR", str(custom_dir))
    p = get_config_path(tmp_path)
    assert p == custom_dir / "config.yml"


@patch("remarkable_mcp.cli.cmd_status")
def test_main_status_command(mock_status):
    """'living-ink status' invokes cmd_status."""
    with patch("sys.argv", ["living-ink", "status"]):
        main()
        mock_status.assert_called_once()


@patch("remarkable_mcp.cli.cmd_setup")
def test_main_setup_command(mock_setup):
    """'living-ink setup' invokes cmd_setup."""
    with patch("sys.argv", ["living-ink", "setup"]):
        main()
        mock_setup.assert_called_once()


@patch("remarkable_mcp.cli.cmd_sync")
def test_main_sync_command(mock_sync):
    """'living-ink sync' invokes cmd_sync."""
    with patch("sys.argv", ["living-ink", "sync", "--notebook", "TestBook"]):
        main()
        mock_sync.assert_called_once()
        args = mock_sync.call_args[0][0]
        assert args.notebook == "TestBook"


def test_cmd_status_no_config(tmp_path, capsys):
    """cmd_status reports cleanly when config is missing."""
    args = MagicMock()
    cmd_status(args, root=tmp_path)
    captured = capsys.readouterr()
    assert "Not found" in captured.out


@patch("remarkable_mcp.cli.cmd_sync")
def test_main_sync_command_with_ssh(mock_sync):
    """'living-ink sync --ssh' passes ssh flag to cmd_sync."""
    with patch("sys.argv", ["living-ink", "sync", "--ssh"]):
        main()
        mock_sync.assert_called_once()
        args = mock_sync.call_args[0][0]
        assert args.ssh is True


def test_cmd_sync_sets_ssh_env(tmp_path, monkeypatch):
    """cmd_sync sets REMARKABLE_USE_SSH when --ssh is passed."""
    import os

    from remarkable_mcp.cli import cmd_sync

    monkeypatch.delenv("REMARKABLE_USE_SSH", raising=False)
    args = MagicMock(ssh=True, notebook=None, limit=0, folder=None)
    with patch("scripts.process_notebook.main"):
        cmd_sync(args, root=tmp_path)
        assert os.environ.get("REMARKABLE_USE_SSH") == "true"


@patch("remarkable_mcp.setup_wizard.verify_remarkable_ssh", return_value=(True, "Connected"))
@patch("remarkable_mcp.setup_wizard.verify_ai_provider", return_value=(True, "OK"))
def test_cmd_status_ssh_mode(mock_verify_ai, mock_verify_ssh, tmp_path, capsys):
    """cmd_status verifies SSH when remarkable.use_ssh is true."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "config.yml").write_text(
        "remarkable:\n  preferred_connection: 'ssh'\n  use_ssh: true\n  ssh_host: '10.11.99.1'\nai:\n  provider: 'none'\n"
    )
    args = MagicMock()
    cmd_status(args, root=tmp_path)
    captured = capsys.readouterr()
    assert "Connected" in captured.out
    assert "USB SSH — Preferred" in captured.out
    mock_verify_ssh.assert_called_once()


@patch("remarkable_mcp.cli.cmd_sync")
def test_main_sync_command_with_cloud(mock_sync):
    """'living-ink sync --cloud' passes cloud flag to cmd_sync."""
    with patch("sys.argv", ["living-ink", "sync", "--cloud"]):
        main()
        mock_sync.assert_called_once()
        args = mock_sync.call_args[0][0]
        assert args.cloud is True


def test_cmd_sync_sets_cloud_env(tmp_path, monkeypatch):
    """cmd_sync sets REMARKABLE_PREFERRED_CONNECTION=cloud when --cloud is passed."""
    import os

    from remarkable_mcp.cli import cmd_sync

    monkeypatch.delenv("REMARKABLE_PREFERRED_CONNECTION", raising=False)
    args = MagicMock(ssh=False, cloud=True, notebook=None, limit=0, folder=None)
    with patch("scripts.process_notebook.main"):
        cmd_sync(args, root=tmp_path)
        assert os.environ.get("REMARKABLE_PREFERRED_CONNECTION") == "cloud"


@patch("remarkable_mcp.setup_wizard.verify_remarkable_token", return_value=(True, "Connected"))
@patch("remarkable_mcp.setup_wizard.verify_remarkable_ssh", return_value=(False, "Unplugged"))
@patch("remarkable_mcp.setup_wizard.verify_ai_provider", return_value=(True, "OK"))
def test_cmd_status_ssh_unplugged_cloud_backup(
    mock_verify_ai, mock_verify_ssh, mock_verify_cloud, tmp_path, capsys
):
    """cmd_status reports Cloud backup active when preferred SSH is unplugged."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "config.yml").write_text(
        "remarkable:\n  preferred_connection: 'ssh'\n  use_ssh: true\n  device_token: 'tok'\nai:\n  provider: 'none'\n"
    )
    args = MagicMock()
    cmd_status(args, root=tmp_path)
    captured = capsys.readouterr()
    assert "Connected" in captured.out
    assert "Cloud backup active" in captured.out
