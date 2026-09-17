"""Tests for living_ink.cli module.

Covers the CLI argument parsing, Command Pattern architecture,
and subcommands (status, setup, sync).
"""

import argparse
import json
from unittest.mock import MagicMock, patch

import pytest

from living_ink.cli import (
    BaseCommand,
    LivingInkCLI,
    SetupCommand,
    StatusCommand,
    SyncCommand,
    get_config_path,
    get_root,
    main,
)


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


@patch.object(StatusCommand, "run", return_value=0)
def test_main_status_command(mock_status):
    """'living-ink status' invokes StatusCommand.run."""
    with patch("sys.argv", ["living-ink", "status"]):
        main()
        mock_status.assert_called_once()


@patch.object(SetupCommand, "run", return_value=0)
def test_main_setup_command(mock_setup):
    """'living-ink setup' invokes SetupCommand.run."""
    with patch("sys.argv", ["living-ink", "setup"]):
        main()
        mock_setup.assert_called_once()


@patch.object(SyncCommand, "run", return_value=0)
def test_main_sync_command(mock_sync):
    """'living-ink sync' invokes SyncCommand.run."""
    with patch("sys.argv", ["living-ink", "sync", "--notebook", "TestBook"]):
        main()
        mock_sync.assert_called_once()
        args = mock_sync.call_args[0][0]
        assert args.notebook == "TestBook"


def test_cmd_status_no_config(tmp_path, capsys):
    """StatusCommand reports cleanly when config is missing."""
    args = MagicMock(json=False)
    StatusCommand(root=tmp_path).run(args)
    captured = capsys.readouterr()
    assert "Not found" in captured.out


@patch.object(SyncCommand, "run", return_value=0)
def test_main_sync_command_with_ssh(mock_sync):
    """'living-ink sync --ssh' passes ssh flag to SyncCommand.run."""
    with patch("sys.argv", ["living-ink", "sync", "--ssh"]):
        main()
        mock_sync.assert_called_once()
        args = mock_sync.call_args[0][0]
        assert args.ssh is True


def test_cmd_sync_sets_ssh_env(tmp_path, monkeypatch):
    """SyncCommand sets REMARKABLE_USE_SSH when --ssh is passed."""
    import os

    monkeypatch.delenv("REMARKABLE_USE_SSH", raising=False)
    args = MagicMock(ssh=True, notebook=None, limit=0, folder=None, json=False)
    with patch("living_ink.pipeline.SyncPipeline.run", return_value=True):
        SyncCommand(root=tmp_path).run(args)
        assert os.environ.get("REMARKABLE_USE_SSH") == "true"


@patch("living_ink.setup_wizard.verify_remarkable_ssh", return_value=(True, "Connected"))
@patch("living_ink.setup_wizard.verify_ai_provider", return_value=(True, "OK"))
def test_cmd_status_ssh_mode(mock_verify_ai, mock_verify_ssh, tmp_path, capsys):
    """StatusCommand verifies SSH when remarkable.use_ssh is true."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "config.yml").write_text(
        "remarkable:\n  preferred_connection: 'ssh'\n  use_ssh: true\n  ssh_host: '10.11.99.1'\nai:\n  provider: 'none'\n"
    )
    args = MagicMock(json=False)
    StatusCommand(root=tmp_path).run(args)
    captured = capsys.readouterr()
    assert "Connected" in captured.out
    assert "USB SSH — Preferred" in captured.out
    mock_verify_ssh.assert_called_once()


@patch.object(SyncCommand, "run", return_value=0)
def test_main_sync_command_with_cloud(mock_sync):
    """'living-ink sync --cloud' passes cloud flag to SyncCommand.run."""
    with patch("sys.argv", ["living-ink", "sync", "--cloud"]):
        main()
        mock_sync.assert_called_once()
        args = mock_sync.call_args[0][0]
        assert args.cloud is True


def test_cmd_sync_sets_cloud_env(tmp_path, monkeypatch):
    """SyncCommand sets REMARKABLE_PREFERRED_CONNECTION=cloud when --cloud is passed."""
    import os

    monkeypatch.delenv("REMARKABLE_PREFERRED_CONNECTION", raising=False)
    args = MagicMock(ssh=False, cloud=True, notebook=None, limit=0, folder=None, json=False)
    with patch("living_ink.pipeline.SyncPipeline.run", return_value=True):
        SyncCommand(root=tmp_path).run(args)
        assert os.environ.get("REMARKABLE_PREFERRED_CONNECTION") == "cloud"


@patch("living_ink.setup_wizard.verify_remarkable_token", return_value=(True, "Connected"))
@patch("living_ink.setup_wizard.verify_remarkable_ssh", return_value=(False, "Unplugged"))
@patch("living_ink.setup_wizard.verify_ai_provider", return_value=(True, "OK"))
def test_cmd_status_ssh_unplugged_cloud_backup(
    mock_verify_ai, mock_verify_ssh, mock_verify_cloud, tmp_path, capsys
):
    """StatusCommand reports Cloud backup active when preferred SSH is unplugged."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "config.yml").write_text(
        "remarkable:\n  preferred_connection: 'ssh'\n  use_ssh: true\n  device_token: 'tok'\nai:\n  provider: 'none'\n"
    )
    args = MagicMock(json=False)
    StatusCommand(root=tmp_path).run(args)
    captured = capsys.readouterr()
    assert "Connected" in captured.out
    assert "Cloud backup active" in captured.out


def test_main_version_flag(capsys):
    """'living-ink --version' outputs version."""
    with patch("sys.argv", ["living-ink", "--version"]):
        try:
            main()
        except SystemExit:
            pass
    captured = capsys.readouterr()
    assert "living-ink 0.2.0" in captured.out


# ---------------------------------------------------------------------------
# Command Pattern Architecture Tests
# ---------------------------------------------------------------------------


def test_base_command_cannot_be_instantiated():
    """BaseCommand ABC cannot be instantiated directly."""
    with pytest.raises(TypeError):
        BaseCommand()


def test_custom_command_registration():
    """Custom command subclassing BaseCommand registers and runs properly."""

    class DummyCommand(BaseCommand):
        name = "dummy"
        help = "Dummy test command"

        @classmethod
        def register_args(cls, parser):
            parser.add_argument("--message", default="hello")

        def run(self, args):
            return 42

    cli = LivingInkCLI()
    cli.register_command(DummyCommand)
    assert "dummy" in cli.commands

    args = cli.build_parser().parse_args(["dummy", "--message", "world"])
    assert args.message == "world"
    assert cli.dispatch(args) == 42


def test_cli_register_invalid_class():
    """Registering a non-BaseCommand class raises TypeError."""
    cli = LivingInkCLI()
    with pytest.raises(TypeError):
        cli.register_command(dict)


def test_sync_command_execution(tmp_path):
    """SyncCommand initializes SyncPipeline with expected arguments and runs."""
    cmd = SyncCommand(root=tmp_path)
    args = argparse.Namespace(
        notebook="MyNotes",
        limit=5,
        folder="TestFolder",
        ssh=True,
        cloud=False,
        sync_pdfs=True,
        sync_epubs=False,
        all_types=False,
        keep_temp=True,
    )
    with patch("living_ink.pipeline.SyncPipeline.__init__", return_value=None) as mock_init:
        with patch("living_ink.pipeline.SyncPipeline.run", return_value=True) as mock_run:
            code = cmd.run(args)
            assert code == 0
            mock_init.assert_called_once()
            assert mock_init.call_args.kwargs["notebook"] == "MyNotes"
            assert mock_init.call_args.kwargs["limit"] == 5
            assert mock_init.call_args.kwargs["folder"] == "TestFolder"
            assert mock_init.call_args.kwargs["ssh"] is True
            assert mock_init.call_args.kwargs["keep_temp"] is True
            mock_run.assert_called_once()


def test_setup_command_execution(tmp_path):
    """SetupCommand invokes run_wizard with root directory."""
    cmd = SetupCommand(root=tmp_path)
    args = argparse.Namespace()
    with patch("living_ink.setup_wizard.run_wizard") as mock_wizard:
        code = cmd.run(args)
        assert code == 0
        mock_wizard.assert_called_once_with(repo_dir=tmp_path)


def test_status_command_json_output(tmp_path, capsys):
    """StatusCommand with --json outputs structured JSON."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "config.yml").write_text("ai:\n  provider: 'none'\n")

    cmd = StatusCommand(root=tmp_path)
    args = argparse.Namespace(json=True)
    code = cmd.run(args)
    assert code == 0

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["config"]["found"] is True
    assert "ai" in data


def test_status_command_json_missing_config(tmp_path, capsys):
    """StatusCommand with --json returns 1 when config is missing."""
    cmd = StatusCommand(root=tmp_path)
    args = argparse.Namespace(json=True)
    code = cmd.run(args)
    assert code == 1

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["config"]["found"] is False


def test_cli_default_routing_to_sync(tmp_path):
    """When no command is given and config exists, CLI routes to sync."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "config.yml").write_text("ai:\n  provider: 'none'\n")

    cli = LivingInkCLI(root=tmp_path)
    with patch.object(SyncCommand, "run", return_value=0) as mock_sync_run:
        code = cli.run([])
        assert code == 0
        mock_sync_run.assert_called_once()


def test_cli_default_routing_to_setup(tmp_path):
    """When no command is given and config is missing, CLI routes to setup."""
    cli = LivingInkCLI(root=tmp_path)
    with patch.object(SetupCommand, "run", return_value=0) as mock_setup_run:
        code = cli.run([])
        assert code == 0
        mock_setup_run.assert_called_once()
