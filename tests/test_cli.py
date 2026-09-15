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
