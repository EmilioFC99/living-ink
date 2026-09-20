"""Tests for living_ink.cli module.

Covers the CLI argument parsing, Command Pattern architecture,
and subcommands (info, setup, sync).
"""

import argparse
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from living_ink import scheduler
from living_ink.cli import (
    BaseCommand,
    InfoCommand,
    LivingInkCLI,
    SetupCommand,
    SyncCommand,
    WatchCommand,
    main,
)
from living_ink.cli.commands.setup import WizardResult
from living_ink.cli.status import _describe_connected_device
from living_ink.config import ConfigurationMissing, find_repo_root, get_config_path
from living_ink.settings import SOURCE_CONFIG, SOURCE_ENV, Settings
from living_ink.state import STATUS_NEW, STATUS_UP_TO_DATE


@contextmanager
def patched_wizard(result):
    """Stand in for the conversation, so a test can check what follows it.

    The class is patched rather than :meth:`Wizard.run`, because how the
    command *builds* the wizard — which root, which bin directory — is as much
    of the contract as what it does with the answer.

    Args:
        result: The :class:`WizardResult` the stand-in returns.

    Yields:
        The patched ``Wizard`` class.
    """
    with patch("living_ink.cli.commands.setup.Wizard") as wizard_cls:
        wizard_cls.return_value.run.return_value = result
        yield wizard_cls


def test_find_repo_root_prefers_cwd(tmp_path):
    """find_repo_root identifies a directory holding pyproject.toml."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='test'")
    with patch("pathlib.Path.cwd", return_value=tmp_path):
        root = find_repo_root()
        assert (root / "pyproject.toml").exists()


def test_find_repo_root_accepts_a_bare_config_dir(tmp_path):
    """A directory with only config/config.yml still counts as a root."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yml").write_text("{}")
    with patch("pathlib.Path.cwd", return_value=tmp_path):
        assert find_repo_root() == tmp_path


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


@patch.object(InfoCommand, "run", return_value=0)
def test_main_info_command(mock_info):
    """'living-ink info' invokes InfoCommand.run."""
    with patch("sys.argv", ["living-ink", "info"]):
        main()
        mock_info.assert_called_once()


@patch.object(SetupCommand, "run", return_value=0)
def test_main_setup_command(mock_setup):
    """'living-ink setup' invokes SetupCommand.run."""
    with (
        patch("sys.argv", ["living-ink", "setup"]),
        patch("living_ink.ui.is_tty", return_value=True),
    ):
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


def test_cmd_info_no_config(tmp_path, capsys):
    """InfoCommand reports cleanly when config is missing."""
    args = MagicMock(json=False)
    InfoCommand(root=tmp_path).run(args)
    captured = capsys.readouterr()
    assert "Not found" in captured.out


@patch.object(SyncCommand, "run", return_value=0)
def test_main_sync_command_with_ssh(mock_sync):
    """'living-ink sync --ssh' reaches SyncCommand.run as its setting."""
    with patch("sys.argv", ["living-ink", "sync", "--ssh"]):
        main()
        mock_sync.assert_called_once()
        args = mock_sync.call_args[0][0]
        assert args.preferred_connection == "ssh"


def sync_namespace(**overrides):
    """Build a sync namespace the way the parser would, with no mock in it.

    A :class:`MagicMock` cannot stand in for a parsed namespace here: every
    generated flag is read with ``getattr``, and a mock answers all of them
    with a truthy attribute, so the pipeline is handed an override for every
    setting in the schema. Real parsing is the point of these tests.

    Args:
        **overrides: Attributes to set, by their ``dest`` name.

    Returns:
        A namespace with ``sync``'s defaults and the overrides applied.
    """
    from living_ink.cli import LivingInkCLI

    args = LivingInkCLI().build_parser().parse_args(["sync"])
    for name, value in overrides.items():
        setattr(args, name, value)
    return args


def _run_sync_capturing_pipeline(args, tmp_path):
    """Run SyncCommand with a stubbed pipeline and return the pipeline it built."""
    built = []

    def capture(self):
        built.append(self)
        return True

    with patch("living_ink.pipeline.SyncPipeline.run", autospec=True, side_effect=capture):
        SyncCommand(root=tmp_path).run(args)

    assert built, "SyncCommand should have built and run a pipeline"
    return built[0]


def test_cmd_sync_ssh_flag_resolves_to_ssh(tmp_path, monkeypatch):
    """SyncCommand resolves --ssh into the pipeline's settings."""
    monkeypatch.delenv("REMARKABLE_USE_SSH", raising=False)
    args = sync_namespace(preferred_connection="ssh")

    pipeline = _run_sync_capturing_pipeline(args, tmp_path)

    assert pipeline.settings.use_ssh is True
    assert pipeline.settings.preferred_connection == "ssh"


@patch(
    "living_ink.cli.status._describe_connected_device", return_value="reMarkable 2 (1404\u00d71872)"
)
@patch("living_ink.setup_wizard.verify_remarkable_ssh", return_value=(True, "Connected"))
@patch("living_ink.setup_wizard.verify_ai_provider", return_value=(True, "OK"))
def test_cmd_info_ssh_mode(mock_verify_ai, mock_verify_ssh, mock_device, tmp_path, capsys):
    """InfoCommand verifies SSH when remarkable.use_ssh is true."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "config.yml").write_text(
        "remarkable:\n  preferred_connection: 'ssh'\n  use_ssh: true\n  ssh_host: '10.11.99.1'\nai:\n  provider: 'none'\n"
    )
    args = MagicMock(json=False)
    InfoCommand(root=tmp_path).run(args)
    captured = capsys.readouterr()
    assert "Connected" in captured.out
    assert "USB SSH — Preferred" in captured.out
    assert "Device:        reMarkable 2 (1404\u00d71872)" in captured.out
    mock_verify_ssh.assert_called_once()


@patch.object(SyncCommand, "run", return_value=0)
def test_main_sync_command_with_cloud(mock_sync):
    """'living-ink sync --cloud' reaches SyncCommand.run as its setting."""
    with patch("sys.argv", ["living-ink", "sync", "--cloud"]):
        main()
        mock_sync.assert_called_once()
        args = mock_sync.call_args[0][0]
        assert args.preferred_connection == "cloud"


def test_cmd_sync_cloud_flag_resolves_to_cloud(tmp_path, monkeypatch):
    """SyncCommand resolves --cloud into the pipeline's settings."""
    monkeypatch.delenv("REMARKABLE_PREFERRED_CONNECTION", raising=False)
    args = sync_namespace(preferred_connection="cloud")

    pipeline = _run_sync_capturing_pipeline(args, tmp_path)

    assert pipeline.settings.preferred_connection == "cloud"
    assert pipeline.settings.use_ssh is False


def test_cmd_sync_does_not_write_settings_into_the_environment(tmp_path, monkeypatch):
    """Resolved settings stay on the pipeline instead of leaking into os.environ."""
    for var in ("REMARKABLE_USE_SSH", "REMARKABLE_PREFERRED_CONNECTION", "APPLE_NOTES_FOLDER"):
        monkeypatch.delenv(var, raising=False)
    args = sync_namespace(preferred_connection="ssh")

    _run_sync_capturing_pipeline(args, tmp_path)

    for var in ("REMARKABLE_USE_SSH", "REMARKABLE_PREFERRED_CONNECTION", "APPLE_NOTES_FOLDER"):
        assert var not in os.environ


@patch("living_ink.setup_wizard.verify_remarkable_token", return_value=(True, "Connected"))
@patch("living_ink.setup_wizard.verify_remarkable_ssh", return_value=(False, "Unplugged"))
@patch("living_ink.setup_wizard.verify_ai_provider", return_value=(True, "OK"))
def test_cmd_info_ssh_unplugged_cloud_backup(
    mock_verify_ai, mock_verify_ssh, mock_verify_cloud, tmp_path, capsys
):
    """InfoCommand reports Cloud backup active when preferred SSH is unplugged."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "config.yml").write_text(
        "remarkable:\n  preferred_connection: 'ssh'\n  use_ssh: true\n  device_token: 'tok'\nai:\n  provider: 'none'\n"
    )
    args = MagicMock(json=False)
    InfoCommand(root=tmp_path).run(args)
    captured = capsys.readouterr()
    assert "Connected" in captured.out
    assert "Cloud backup active" in captured.out


@patch("living_ink.setup_wizard.verify_remarkable_token", return_value=(True, "Connected"))
@patch("living_ink.setup_wizard.verify_remarkable_ssh", return_value=(False, "Unplugged"))
@patch("living_ink.setup_wizard.verify_ai_provider", return_value=(True, "OK"))
def test_cmd_info_finds_a_token_registration_left_in_rmapi(
    mock_verify_ai, mock_verify_ssh, mock_verify_cloud, tmp_path, isolated_home, capsys
):
    """Registration stores the token in ~/.rmapi and leaves device_token empty.

    Reading only the config key reported "Disconnected" for a setup that was
    syncing from the Cloud perfectly well.
    """
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "config.yml").write_text(
        "remarkable:\n  preferred_connection: 'cloud'\n  device_token: ''\nai:\n  provider: 'none'\n"
    )
    (isolated_home / ".rmapi").write_text("registered-token", encoding="utf-8")

    InfoCommand(root=tmp_path).run(MagicMock(json=False))

    assert "Disconnected" not in capsys.readouterr().out
    mock_verify_cloud.assert_called_once_with("registered-token")


@patch("living_ink.setup_wizard.verify_remarkable_token", return_value=(True, "Connected"))
@patch("living_ink.setup_wizard.verify_remarkable_ssh", return_value=(False, "Unplugged"))
@patch("living_ink.setup_wizard.verify_ai_provider", return_value=(True, "OK"))
def test_cmd_info_reads_the_token_beside_the_config_it_resolved(
    mock_verify_ai, mock_verify_ssh, mock_verify_cloud, tmp_path, capsys
):
    """A second profile reports on its own account, not the default one."""
    from living_ink.config import credentials

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    cfg_file = cfg_dir / "config.yml"
    cfg_file.write_text(
        "remarkable:\n  preferred_connection: 'cloud'\n  device_token: ''\nai:\n  provider: 'none'\n"
    )
    credentials.write_secret(credentials.CLOUD_TOKEN, "default-profile-token")
    credentials.write_secret(credentials.CLOUD_TOKEN, "this-profile-token", config_path=cfg_file)

    InfoCommand(root=tmp_path).run(MagicMock(json=False))

    mock_verify_cloud.assert_called_once_with("this-profile-token")


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
        max_notebooks_per_run=5,
        preferred_connection="ssh",
        sync_types=["pdf"],
        keep_temp=True,
    )
    with patch("living_ink.pipeline.SyncPipeline.__init__", return_value=None) as mock_init:
        with patch("living_ink.pipeline.SyncPipeline.run", return_value=True) as mock_run:
            code = cmd.run(args)
            assert code == 0
            mock_init.assert_called_once()
            opts = mock_init.call_args.kwargs
            assert opts["notebook"] == "MyNotes"
            assert opts["keep_temp"] is True
            assert opts["flags"] == {
                "max_notebooks_per_run": 5,
                "preferred_connection": "ssh",
                "sync_types": ["pdf"],
            }
            # A flag nobody gave is absent, not empty: an empty list would
            # overrule a config that names the types it wants.
            assert "sync_tags" not in opts["flags"]
            mock_run.assert_called_once()


def test_setup_command_execution(tmp_path):
    """SetupCommand runs the wizard, built with the command's own root."""
    cmd = SetupCommand(root=tmp_path)
    args = argparse.Namespace()
    with patched_wizard(WizardResult(saved=True)) as wizard_cls:
        code = cmd.run(args)
    assert code == 0
    wizard_cls.assert_called_once_with(root=tmp_path, bin_dir=None)


def test_setup_command_reports_a_declined_save(tmp_path):
    """Saying no at the summary is a failure: setup produced no config."""
    with patched_wizard(WizardResult(saved=False)):
        assert SetupCommand(root=tmp_path).run(argparse.Namespace()) == 1


class TestWizardSyncHandoff:
    """The CLI, not the wizard or the pipeline, decides what runs next."""

    def test_setup_runs_sync_when_the_user_asks(self, tmp_path):
        """A wizard that reports run_sync_requested hands off to SyncCommand."""
        result = WizardResult(saved=True, run_sync_requested=True)
        with patched_wizard(result):
            with patch.object(SyncCommand, "run", return_value=0) as mock_sync:
                assert SetupCommand(root=tmp_path).run(argparse.Namespace()) == 0
                mock_sync.assert_called_once()

    def test_setup_skips_sync_when_the_user_declines(self, tmp_path):
        """Declining the first sync leaves SyncCommand untouched."""
        result = WizardResult(saved=True, run_sync_requested=False)
        with patched_wizard(result):
            with patch.object(SyncCommand, "run", return_value=0) as mock_sync:
                assert SetupCommand(root=tmp_path).run(argparse.Namespace()) == 0
                mock_sync.assert_not_called()

    def test_missing_config_offers_the_wizard_when_interactive(self, tmp_path):
        """An unusable config prompts for setup rather than exiting silently."""
        cmd = SyncCommand(root=tmp_path)
        args = argparse.Namespace()
        with patch("living_ink.pipeline.SyncPipeline.__init__", return_value=None):
            with patch(
                "living_ink.pipeline.SyncPipeline.run",
                side_effect=ConfigurationMissing("no provider", hint="run: living-ink setup"),
            ):
                with patch("sys.stdin.isatty", return_value=True):
                    with patch("builtins.input", return_value="y"):
                        with patch.object(SetupCommand, "run", return_value=0) as mock_setup:
                            assert cmd.run(args) == 0
                            mock_setup.assert_called_once()

    def test_missing_config_exits_1_when_not_interactive(self, tmp_path, capsys):
        """Non-interactive runs report the hint and fail without prompting."""
        cmd = SyncCommand(root=tmp_path)
        args = argparse.Namespace()
        with patch("living_ink.pipeline.SyncPipeline.__init__", return_value=None):
            with patch(
                "living_ink.pipeline.SyncPipeline.run",
                side_effect=ConfigurationMissing("no provider", hint="run: living-ink setup"),
            ):
                with patch("sys.stdin.isatty", return_value=False):
                    assert cmd.run(args) == 1
        # stderr: a `--json` run that never got as far as a report still has
        # to say why, and stdout is reserved for the document.
        assert "run: living-ink setup" in capsys.readouterr().err

    def test_sync_launched_by_the_wizard_does_not_reoffer_it(self, tmp_path, capsys):
        """A still-broken config after setup reports the problem, it does not loop."""
        result = WizardResult(saved=True, run_sync_requested=True)
        with patched_wizard(result):
            with patch("living_ink.pipeline.SyncPipeline.__init__", return_value=None):
                with patch(
                    "living_ink.pipeline.SyncPipeline.run",
                    side_effect=ConfigurationMissing("still broken"),
                ):
                    with patch("sys.stdin.isatty", return_value=True):
                        with patch("builtins.input") as mock_input:
                            code = SetupCommand(root=tmp_path).run(argparse.Namespace())
                            assert code == 1
                            mock_input.assert_not_called()


def test_info_command_json_output(tmp_path, capsys):
    """InfoCommand with --json outputs structured JSON."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "config.yml").write_text("ai:\n  provider: 'none'\n")

    cmd = InfoCommand(root=tmp_path)
    args = argparse.Namespace(json=True)
    code = cmd.run(args)
    assert code == 0

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["config"]["found"] is True
    assert "ai" in data


def test_info_command_json_missing_config(tmp_path, capsys):
    """InfoCommand with --json returns 1 when config is missing."""
    cmd = InfoCommand(root=tmp_path)
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
        with patch("living_ink.ui.is_tty", return_value=True):
            code = cli.run([])
        assert code == 0
        mock_setup_run.assert_called_once()


class TestTheTerminalIsAPrecondition:
    """An interactive command is refused before it can half-configure anything."""

    def test_setup_without_a_terminal_is_a_usage_error(self, tmp_path, capsys):
        """No TTY exits 2 and never reaches the command."""
        with patch.object(SetupCommand, "run", return_value=0) as mock_setup_run:
            with patch("living_ink.ui.is_tty", return_value=False):
                assert LivingInkCLI(root=tmp_path).run(["setup"]) == 2
        mock_setup_run.assert_not_called()
        assert "interactive" in capsys.readouterr().err

    def test_a_non_interactive_command_is_never_refused(self, tmp_path):
        """``sync`` runs with stdin closed, which is how a cron job runs it."""
        with patch.object(SyncCommand, "run", return_value=0) as mock_sync_run:
            with patch("living_ink.ui.is_tty", return_value=False):
                assert LivingInkCLI(root=tmp_path).run(["sync"]) == 0
        mock_sync_run.assert_called_once()


def test_cmd_sync_the_rehearsal_reaches_the_pipeline(tmp_path):
    """Publishing nothing is an option on the run, not a setting on disk.

    ``--keep-temp`` is asserted off on purpose: the rehearsal used to turn it
    on behind the user's back, which left artifacts a real sync would purge.
    """
    args = argparse.Namespace(
        notebook=None,
        limit=0,
        folder=None,
        ssh=False,
        cloud=False,
        keep_temp=False,
        preview=True,
        transcribe=True,
    )
    pipeline_obj = _run_sync_capturing_pipeline(args, tmp_path)

    assert pipeline_obj.dry_run is True
    assert pipeline_obj.keep_temp is False


def test_cmd_sync_prune_flag_reaches_the_pipeline(tmp_path):
    """Deleting notes is opt-in on the run, and off unless it is asked for."""
    args = argparse.Namespace(
        notebook=None,
        limit=0,
        folder=None,
        ssh=False,
        cloud=False,
        keep_temp=False,
        dry_run=False,
        prune=True,
    )
    assert _run_sync_capturing_pipeline(args, tmp_path).prune is True


def test_cmd_sync_does_not_prune_by_default(tmp_path):
    args = argparse.Namespace(
        notebook=None,
        limit=0,
        folder=None,
        ssh=False,
        cloud=False,
        keep_temp=False,
        dry_run=False,
    )
    assert _run_sync_capturing_pipeline(args, tmp_path).prune is False


class TestInfoSettingsReport:
    """`info` reports the effective settings, not just connectivity."""

    def _report(self, config_text, env=None):
        """Collect a health report for a config file, with probing stubbed out."""
        import tempfile
        from pathlib import Path as _Path

        from living_ink.cli import collect_status

        tmp = _Path(tempfile.mkdtemp()) / "config.yml"
        tmp.write_text(config_text)
        with (
            patch("living_ink.setup_wizard.verify_remarkable_ssh", return_value=(False, "no")),
            patch("living_ink.setup_wizard.verify_remarkable_token", return_value=(False, "no")),
            patch("living_ink.setup_wizard.verify_ai_provider", return_value=(False, "no")),
            patch.dict(os.environ, env or {}, clear=False),
        ):
            return collect_status(tmp)

    def test_an_unusable_vault_is_reported_by_the_destinations_own_check(self, tmp_path):
        """One implementation of "is this vault usable", not two that drift."""
        report = self._report(f"obsidian:\n  enabled: true\n  vault_path: {tmp_path / 'gone'}\n")

        assert report.obsidian_valid is False
        assert "does not exist" in report.obsidian_problem
        assert report.to_dict()["obsidian"]["problem"] == report.obsidian_problem

    def test_a_good_vault_reports_no_problem(self, tmp_path):
        report = self._report(f"obsidian:\n  enabled: true\n  vault_path: {tmp_path}\n")

        assert report.obsidian_valid is True
        assert report.obsidian_problem == ""

    def test_an_enabled_obsidian_with_no_vault_says_so(self):
        report = self._report("obsidian:\n  enabled: true\n")

        assert report.obsidian_valid is False
        assert "vault_path" in report.obsidian_problem

    def test_the_report_carries_resolved_settings(self):
        report = self._report("sync:\n  ocr_concurrency: 7\n")

        origins = {o.name: o for o in report.settings}
        assert origins["ocr_concurrency"].value == 7
        assert origins["ocr_concurrency"].source == SOURCE_CONFIG

    def test_json_output_lists_settings_with_their_source(self):
        report = self._report("sync: {}\n", env={"SYNC_OCR_CONCURRENCY": "3"})

        entries = {s["name"]: s for s in report.to_dict()["settings"]}
        assert entries["ocr_concurrency"] == {
            "name": "ocr_concurrency",
            "value": "3",
            "source": SOURCE_ENV,
            "env_var": "SYNC_OCR_CONCURRENCY",
        }

    def test_json_output_masks_the_device_token(self):
        report = self._report("remarkable:\n  device_token: sekrit\n")

        entries = {s["name"]: s for s in report.to_dict()["settings"]}
        assert entries["remarkable_token"]["value"] == "••••••••"
        assert "sekrit" not in json.dumps(report.to_dict())

    def test_a_missing_config_reports_no_settings(self):
        from pathlib import Path as _Path

        from living_ink.cli import collect_status

        report = collect_status(_Path("/nonexistent/living-ink/config.yml"))

        assert report.settings == []
        assert report.to_dict()["settings"] == []

    def test_console_output_names_the_overriding_variable(self, capsys):
        report = self._report("sync: {}\n", env={"SYNC_OCR_CONCURRENCY": "3"})

        InfoCommand._render_settings(report)

        out = capsys.readouterr().out
        assert "ocr_concurrency" in out
        assert "SYNC_OCR_CONCURRENCY" in out


class TestInfoChecksStoredCredentials:
    """`info` verifies the provider with the key it will actually use."""

    def _collect(self, tmp_path, config_text, secrets=(), verify=None):
        """Collect a health report against an isolated config directory.

        Args:
            tmp_path: Pytest temporary directory.
            config_text: Contents of ``config.yml``.
            secrets: Pairs of (credential name, value) to store first.
            verify: Replacement for ``verify_ai_provider``, or None for a stub.

        Returns:
            The collected StatusReport.
        """
        from living_ink.cli import collect_status
        from living_ink.config import credentials

        cfg = tmp_path / "config.yml"
        cfg.write_text(config_text)
        cfg.chmod(0o600)
        for name, value in secrets:
            credentials.write_secret(name, value, config_path=cfg)

        with (
            patch("living_ink.setup_wizard.verify_remarkable_ssh", return_value=(False, "no")),
            patch("living_ink.setup_wizard.verify_remarkable_token", return_value=(False, "no")),
            patch("living_ink.api.resolve_stored_token", return_value=""),
            patch(
                "living_ink.setup_wizard.verify_ai_provider",
                side_effect=verify or (lambda *a, **kw: (False, "no")),
            ),
        ):
            return collect_status(cfg)

    def test_the_stored_key_is_the_one_verified(self, tmp_path):
        """A config with no key must not report the provider as unconfigured."""
        seen = []

        def _verify(provider, api_key="", model=""):
            seen.append(api_key)
            return True, "OK"

        self._collect(
            tmp_path,
            "ai:\n  provider: gemini\n",
            secrets=[("ai.api_key.gemini", "AIza-stored")],
            verify=_verify,
        )

        assert seen == ["AIza-stored"]

    def test_a_key_still_in_the_config_is_the_fallback(self, tmp_path):
        """`info` may run before the first sync, which is what migrates it."""
        seen = []

        def _verify(provider, api_key="", model=""):
            seen.append(api_key)
            return True, "OK"

        self._collect(tmp_path, "ai:\n  provider: gemini\n  api_key: AIza-legacy\n", verify=_verify)

        assert seen == ["AIza-legacy"]

    def test_a_loose_credential_is_reported(self, tmp_path):
        from living_ink.config import credentials

        self._collect(
            tmp_path, "ai:\n  provider: none\n", secrets=[(credentials.CLOUD_TOKEN, "tok")]
        )
        loose = tmp_path / "credentials" / credentials.CLOUD_TOKEN
        loose.chmod(0o644)

        report = self._collect(tmp_path, "ai:\n  provider: none\n")

        assert report.loose_credentials == [str(loose)]
        assert report.to_dict()["credentials"]["insecure"] == [str(loose)]

    def test_owner_only_credentials_are_not_reported(self, tmp_path):
        from living_ink.config import credentials

        report = self._collect(
            tmp_path, "ai:\n  provider: none\n", secrets=[(credentials.CLOUD_TOKEN, "tok")]
        )

        assert report.loose_credentials == []

    def test_the_console_names_the_file_and_the_fix(self, tmp_path, capsys):
        from living_ink.config import credentials

        self._collect(
            tmp_path, "ai:\n  provider: none\n", secrets=[(credentials.CLOUD_TOKEN, "tok")]
        )
        loose = tmp_path / "credentials" / credentials.CLOUD_TOKEN
        loose.chmod(0o644)
        report = self._collect(tmp_path, "ai:\n  provider: none\n")
        capsys.readouterr()

        InfoCommand._render_console(report)

        out = capsys.readouterr().out
        assert str(loose) in out
        assert "chmod 600" in out


class TestWatchCommand:
    """`watch` runs the configured cron schedule and survives a bad night.

    Every test here drives a real :class:`~living_ink.scheduler.Schedule` whose
    clock only moves when it sleeps, so a "daily at 09:00" loop runs in
    microseconds and the tick order under test is the shipped one. What is
    stubbed is the sync itself and the two reads that would need a config file.
    """

    @pytest.fixture
    def data_dir(self, tmp_path, monkeypatch):
        """Give the watcher its own state database and lock directory.

        Args:
            tmp_path: Pytest's temporary directory.
            monkeypatch: Pytest's patcher.

        Yields:
            The directory standing in for ``DATA_DIR``.
        """
        from living_ink import pipeline

        monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path)
        pipeline.reset_state_store()
        yield tmp_path
        pipeline.reset_state_store()

    @staticmethod
    def _settings(**watch):
        """Resolve settings with the watch section filled in.

        Args:
            **watch: Overrides for the ``watch:`` section.

        Returns:
            The resolved settings.
        """
        section = {"enabled": True, "schedule": "* * * * *", "timezone": "UTC"}
        section.update(watch)
        return Settings.resolve({"watch": section})

    @staticmethod
    def _schedule(expression="* * * * *"):
        """Build a schedule whose clock only advances when it sleeps.

        Starting at 08:59:30 with a fire at every minute means the first thing
        the loop does is a catch-up for 08:59:00, so a test gets its first tick
        without waiting and the catch-up path is exercised by default.

        Args:
            expression: The cron expression.

        Returns:
            A schedule driven by a fake clock.
        """
        tz = ZoneInfo("UTC")
        clock = {"at": datetime(2026, 9, 20, 8, 59, 30, tzinfo=tz)}

        def now():
            return clock["at"]

        def sleep(seconds):
            clock["at"] = clock["at"] + timedelta(seconds=seconds)

        return scheduler.Schedule(expression, tz, now=now, sleep=sleep)

    def _run(self, side_effects, *, settings=None, schedule=None):
        """Run the watcher until the stubbed sync interrupts it.

        Args:
            side_effects: What successive ``execute_sync`` calls do; the last
                must interrupt or return an exit code, or the loop never ends.
            settings: Settings to hand the command, resolved by default.
            schedule: Schedule to run, or a per-call side effect list.

        Returns:
            (exit code, execute_sync mock). The interrupt is turned into 130
            here exactly the way ``main`` turns it into 130.
        """
        build = (
            {"side_effect": schedule}
            if isinstance(schedule, list)
            else {"return_value": schedule or self._schedule()}
        )
        with (
            patch.object(WatchCommand, "_read_settings", return_value=settings or self._settings()),
            patch.object(WatchCommand, "_build_schedule", **build),
            patch.object(SyncCommand, "execute_sync", side_effect=side_effects) as sync,
        ):
            try:
                code = WatchCommand().run(argparse.Namespace())
            except KeyboardInterrupt:
                code = 130
        return code, sync

    # --- refusing to start ------------------------------------------------

    def test_watching_off_says_so_and_exits_without_syncing(self, data_dir, capsys):
        """Off has one meaning, and idling would be a second one."""
        with patch.object(SyncCommand, "execute_sync", side_effect=AssertionError("must not run")):
            code = self._run([], settings=self._settings(enabled=False))[0]

        assert code == 0
        assert "Watching is off" in capsys.readouterr().out

    def test_watching_on_with_no_schedule_is_a_configuration_error(self, data_dir, capsys):
        with patch.object(
            WatchCommand, "_read_settings", return_value=self._settings(schedule=None)
        ):
            code = WatchCommand().run(argparse.Namespace())

        assert code == 1
        assert "watch.schedule" in capsys.readouterr().err

    def test_an_invalid_expression_names_the_field_it_could_not_read(self, data_dir, capsys):
        with patch.object(
            WatchCommand, "_read_settings", return_value=self._settings(schedule="0 9 * * funday")
        ):
            code = WatchCommand().run(argparse.Namespace())

        assert code == 1
        assert "day of week" in capsys.readouterr().err

    def test_an_unknown_timezone_is_refused_rather_than_read_as_utc(self, data_dir, capsys):
        with patch.object(
            WatchCommand, "_read_settings", return_value=self._settings(timezone="Europe/Madroid")
        ):
            code = WatchCommand().run(argparse.Namespace())

        assert code == 1
        assert "Europe/Mad" in capsys.readouterr().err

    def test_a_second_watcher_refuses_rather_than_racing_the_first(self, data_dir, capsys):
        """Two schedulers on one machine could double-write the same note."""
        with (
            patch.object(scheduler.RunLock, "acquire", return_value=False),
            patch.object(SyncCommand, "execute_sync", side_effect=AssertionError("must not run")),
        ):
            code = self._run([])[0]

        assert code == 1
        assert "already running" in capsys.readouterr().err

    # --- the loop ---------------------------------------------------------

    def test_it_syncs_once_per_fire_until_interrupted(self, data_dir):
        code, sync = self._run([True, True, KeyboardInterrupt()])

        assert code == 130
        assert sync.call_count == 3

    def test_every_tick_says_it_was_the_schedule_that_asked(self, data_dir):
        """The run row is what ``info``'s staleness banner reads back."""
        _, sync = self._run([KeyboardInterrupt()])

        kwargs = sync.call_args.kwargs
        assert kwargs["trigger"] == "scheduled"
        assert kwargs["scheduled_fire_time"] == "2026-09-20T08:59:00+00:00"

    def test_a_missed_fire_produces_one_catch_up_and_not_a_backlog(self, data_dir):
        """A laptop closed for five nights is still one useful sync."""
        _, sync = self._run([KeyboardInterrupt()], schedule=self._schedule("0 9 * * *"))

        # 08:59:30 on the 20th, so yesterday's 09:00 is the overdue one.
        assert sync.call_args.kwargs["scheduled_fire_time"] == "2026-09-19T09:00:00+00:00"

    def test_a_failed_sync_does_not_end_the_watch(self, data_dir):
        """An unplugged tablet is the condition watch exists to ride out."""
        code, sync = self._run([False, KeyboardInterrupt()])

        assert code == 130
        assert sync.call_count == 2

    def test_an_unexpected_error_does_not_end_the_watch(self, data_dir):
        code, sync = self._run([RuntimeError("tablet vanished"), KeyboardInterrupt()])

        assert code == 130
        assert sync.call_count == 2

    def test_a_missing_config_stops_the_watch(self, data_dir):
        """That failure will still be there next tick, so looping is pointless."""
        code, sync = self._run([ConfigurationMissing("no config")])

        assert code == 1
        assert sync.call_count == 1

    def test_the_watch_never_offers_the_setup_wizard(self, data_dir):
        """It runs unattended; blocking on input() would hang a daemon."""
        with patch("builtins.input", side_effect=AssertionError("must not prompt")):
            assert self._run([ConfigurationMissing("nope")])[0] == 1

    def test_a_stopped_watch_is_never_reported_as_a_clean_finish(self, data_dir):
        """Converting Ctrl+C to 0 is what makes a supervised watch unstoppable.

        ``launchd`` with ``KeepAlive`` and systemd with ``Restart=always`` both
        read exit 0 as "the job is done" and start it straight back up, so the
        interrupt has to leave the loop intact for ``main`` to answer 130.
        """
        with (
            patch.object(WatchCommand, "_read_settings", return_value=self._settings()),
            patch.object(WatchCommand, "_build_schedule", return_value=self._schedule()),
            patch.object(SyncCommand, "execute_sync", side_effect=KeyboardInterrupt),
            pytest.raises(KeyboardInterrupt),
        ):
            WatchCommand().run(argparse.Namespace())

    def test_each_tick_drops_the_config_and_destination_caches(self, data_dir):
        """Trap 1: a daemon that never re-reads config runs last month's."""
        from living_ink import pipeline

        with patch.object(pipeline, "reset_caches") as reset:
            self._run([True, KeyboardInterrupt()])

        assert reset.call_count == 2
        # The open database is not one of them: _ADDED_COLUMNS probes on every
        # open, and nothing in config.yml can move the file.
        assert all(call.kwargs == {"keep_state_store": True} for call in reset.call_args_list)

    def test_a_tick_that_cannot_take_the_lock_is_recorded_as_skipped(self, data_dir):
        """Skipped, never queued: the run already going will see the same tablet."""
        from contextlib import contextmanager

        from living_ink import pipeline

        @contextmanager
        def busy(_lock):
            yield False

        with (
            patch.object(scheduler, "optional_lock", busy),
            patch.object(SyncCommand, "execute_sync", side_effect=AssertionError("must not run")),
            patch.object(WatchCommand, "_read_settings", return_value=self._settings()),
            patch.object(WatchCommand, "_build_schedule", return_value=self._schedule()),
            patch.object(WatchCommand, "_reread", side_effect=[None, KeyboardInterrupt()]),
            pytest.raises(KeyboardInterrupt),
        ):
            WatchCommand().run(argparse.Namespace())

        rows = pipeline.get_state_store().recent_runs(5)
        assert [row["outcome"] for row in rows] == ["skipped_overlapping"] * 2
        # Newest first, so the catch-up for the fire time already past is last.
        assert rows[-1]["scheduled_fire_time"] == "2026-09-20T08:59:00+00:00"

    def test_turning_watching_off_stops_the_loop_cleanly(self, data_dir, capsys):
        with patch.object(
            WatchCommand,
            "_reread",
            return_value=self._settings(enabled=False),
        ):
            code, sync = self._run([True])

        assert code == 0
        assert sync.call_count == 1
        assert "turned off" in capsys.readouterr().out

    def test_a_changed_schedule_is_picked_up_without_a_restart(self, data_dir, capsys):
        """The same re-read that catches a new model catches a new schedule."""
        first, second = self._schedule("* * * * *"), self._schedule("0 9 * * *")

        code, sync = self._run(
            [True, KeyboardInterrupt()],
            schedule=[first, second, second],
        )

        assert code == 130
        assert sync.call_count == 2
        assert "Schedule changed" in capsys.readouterr().out

    # --- what it prints ---------------------------------------------------

    def test_it_opens_with_the_whole_status_of_the_watcher(self, data_dir, capsys):
        """ "Is this thing working?" is answered without a second command."""
        self._run([KeyboardInterrupt()])

        out = capsys.readouterr().out
        assert "Living Ink · watching" in out
        assert "every minute" in out or "* * * * *" in out
        assert "Last run     never" in out
        assert "Next run" in out
        assert "Ctrl+C to stop." in out

    def test_json_prints_no_panel(self, data_dir, capsys):
        """``--json`` promises stdout holds objects and nothing else."""
        self._run([KeyboardInterrupt()], settings=self._settings(), schedule=None)
        capsys.readouterr()

        self._run(
            [KeyboardInterrupt()],
            settings=Settings.resolve(
                {"watch": {"enabled": True, "schedule": "* * * * *"}, "output": {"json": True}}
            ),
        )

        assert "Living Ink · watching" not in capsys.readouterr().out

    def test_a_finished_tick_is_reported_in_one_line(self, data_dir, capsys):
        from living_ink import pipeline

        store = pipeline.get_state_store()
        run_id = store.start_run(trigger="scheduled", scheduled_fire_time="2026-09-20T08:59:00")
        store.finish_run(run_id, outcome="success", seen=3, published=3)

        self._run([KeyboardInterrupt()])

        assert "synced 3 documents" in capsys.readouterr().out

    # --- the parser -------------------------------------------------------

    def test_watch_takes_no_behaviour_flags(self):
        """A supervisor restarts without them, so they would stop applying."""
        with pytest.raises(SystemExit):
            LivingInkCLI().build_parser().parse_args(["watch", "--notebook", "Foo"])

    def test_watch_takes_the_output_flags(self):
        args = LivingInkCLI().build_parser().parse_args(["watch", "--json", "--verbose"])

        assert (args.command, args.output_json, args.verbosity) == ("watch", True, "verbose")


class TestVerbosityFlags:
    """--verbose and --quiet are accepted on either side of the subcommand.

    Both set one value, ``output.verbosity``, because they are two answers to
    one question rather than two switches: a config file that says ``quiet``
    and a command line that says ``--verbose`` have to be comparable, and two
    independent booleans give no answer for the pair that are both on.
    """

    def _parse(self, argv):
        return LivingInkCLI().build_parser().parse_args(argv)

    def test_verbose_after_the_subcommand(self):
        assert self._parse(["sync", "--verbose"]).verbosity == "verbose"

    def test_verbose_before_the_subcommand(self):
        """A subparser default would silently overwrite the flag given here."""
        assert self._parse(["--verbose", "sync"]).verbosity == "verbose"

    def test_quiet_after_the_subcommand(self):
        assert self._parse(["sync", "-q"]).verbosity == "quiet"

    def test_neither_flag_leaves_the_value_unset(self):
        """Absent, not "normal": an unset flag must defer to the config file."""
        assert getattr(self._parse(["sync"]), "verbosity", None) is None

    def test_the_later_flag_wins(self):
        """One dest, so a contradictory pair resolves by position, not by luck."""
        assert self._parse(["--verbose", "sync", "--quiet"]).verbosity == "quiet"
        assert self._parse(["--quiet", "sync", "--verbose"]).verbosity == "verbose"

    def test_dispatch_configures_logging(self, tmp_path):
        from living_ink import logs

        args = argparse.Namespace(command="info", config=None, verbosity="verbose")
        with (
            patch("living_ink.logs.LOG_PATH", tmp_path / "pipeline.log"),
            patch.object(InfoCommand, "run", return_value=0),
        ):
            try:
                LivingInkCLI().dispatch(args)
                assert logs.console_mode() is logs.ConsoleMode.VERBOSE
            finally:
                logs.reset_handlers()
                logs._console_mode = logs.ConsoleMode.PLAIN


class TestVerbosityResolvesLikeEverySetting:
    """``output.verbosity`` is a setting, so the file and the environment count.

    ``--quiet`` used to be the only thing the console ever read, which made the
    config key inert: declared in the schema, listed by ``info``, documented,
    and ignored. A user who wrote ``verbosity: quiet`` once and expected every
    run to be quiet got a full run every time, with nothing to say why.
    """

    def _configure(self, tmp_path, argv, config_text=None, **env):
        """Configure logging the way ``dispatch`` does and return the mode."""
        from living_ink import logs
        from living_ink.cli.app import configure_logging

        config = tmp_path / "config.yml"
        if config_text is not None:
            config.write_text(config_text, encoding="utf-8")
        args = LivingInkCLI().build_parser().parse_args(argv)
        with (
            patch("living_ink.logs.LOG_PATH", tmp_path / "pipeline.log"),
            patch.dict(os.environ, {"LIVING_INK_CONFIG": str(config), **env}, clear=False),
        ):
            try:
                configure_logging(args)
                return logs.console_mode()
            finally:
                logs.reset_handlers()
                logs._console_mode = logs.ConsoleMode.PLAIN

    def test_the_config_file_alone_makes_a_run_quiet(self, tmp_path):
        mode = self._configure(tmp_path, ["sync"], "output:\n  verbosity: quiet\n")
        assert mode.name == "QUIET"

    def test_the_environment_alone_makes_a_run_verbose(self, tmp_path):
        mode = self._configure(tmp_path, ["sync"], LIVING_INK_VERBOSITY="verbose")
        assert mode.name == "VERBOSE"

    def test_the_flag_beats_the_file(self, tmp_path):
        mode = self._configure(tmp_path, ["sync", "--verbose"], "output:\n  verbosity: quiet\n")
        assert mode.name == "VERBOSE"

    def test_an_unparseable_config_still_honours_the_flag(self, tmp_path):
        """The command about to run reports the parse error; this one cannot.

        Configuring logging is the first thing that happens, so raising here
        would replace a readable "check your indentation" with a traceback
        from the logging setup.
        """
        mode = self._configure(tmp_path, ["sync", "--quiet"], "output:\n\tverbosity: quiet\n")
        assert mode.name == "QUIET"

    def test_json_keeps_stdout_clean_from_the_config_file_too(self, tmp_path):
        """``output.json`` is a setting as well, and it is orthogonal to volume."""
        mode = self._configure(tmp_path, ["sync"], "output:\n  json: true\n")
        assert mode.name == "JSON"


class TestJsonKeepsStdoutToOneDocument:
    """``--json`` promises stdout holds one JSON value and nothing else.

    It is a promise about the *stream*, not about the summary: the report was
    always valid JSON, and the run still printed a deprecation warning, a
    permissions repair and a failover notice around it with a bare ``print``.
    Anything piping the output got a parse error on line one, which is the
    single failure ``--json`` exists to prevent.
    """

    @pytest.fixture
    def _sandbox(self, tmp_path, monkeypatch):
        """Point every path this run would touch at a temp directory."""
        from living_ink import pipeline as pipeline_module

        monkeypatch.setattr(pipeline_module, "DATA_DIR", tmp_path / "data")
        monkeypatch.setattr(pipeline_module, "ensure_runtime_dirs", lambda: None)
        monkeypatch.setattr(pipeline_module, "validate_environment", lambda: None)
        monkeypatch.setattr(pipeline_module, "cleanup_temp_artifacts", lambda **kw: None)
        monkeypatch.setattr(pipeline_module, "register_temp_cleanup", lambda **kw: None)
        # The config is read once per process, so a test that ran earlier has
        # already spent the warnings this one is about.
        pipeline_module.reset_caches()
        yield
        pipeline_module.reset_caches()

    def _run(self, tmp_path, monkeypatch, argv, config_text):
        """Run the real CLI over an empty tablet and return what it printed."""
        from types import SimpleNamespace

        from living_ink import logs
        from living_ink.pipeline import SyncPipeline

        config = tmp_path / "config.yml"
        config.write_text(config_text, encoding="utf-8")
        monkeypatch.setenv("LIVING_INK_CONFIG", str(config))
        monkeypatch.setattr(logs, "LOG_PATH", tmp_path / "pipeline.log")
        monkeypatch.setattr(
            SyncPipeline, "connect", lambda self: SimpleNamespace(get_meta_items=lambda: [])
        )
        monkeypatch.setattr(SyncPipeline, "preflight_destinations", lambda self: None)
        monkeypatch.setattr(SyncPipeline, "_learn_device", lambda self, client: None)
        try:
            LivingInkCLI().run(argv)
        finally:
            logs.reset_handlers()
            logs._console_mode = logs.ConsoleMode.PLAIN

    def test_a_deprecated_config_key_does_not_break_the_document(
        self, tmp_path, monkeypatch, capsys, _sandbox
    ):
        """The exact leak: five ``⚠️ config.yml — …`` lines ahead of the JSON."""
        self._run(
            tmp_path,
            monkeypatch,
            ["sync", "--json", "--verbose"],
            "use_ssh: true\nopenai:\n  model: gpt-4o\n",
        )
        captured = capsys.readouterr()
        assert json.loads(captured.out)
        # Not merely absent from stdout — the user still has to be told.
        assert "config.yml" in captured.err

    def test_verbose_page_lines_go_to_stderr(self, tmp_path, monkeypatch, capsys, _sandbox):
        """§15.4: verbosity picks the volume, ``--json`` picks the stream."""
        self._run(tmp_path, monkeypatch, ["sync", "--json", "--verbose"], "sync: {}\n")
        captured = capsys.readouterr()
        assert json.loads(captured.out)
        assert "Pipeline started" in captured.err

    def test_quiet_and_json_still_print_the_document(self, tmp_path, monkeypatch, capsys, _sandbox):
        """``--quiet`` silences the human stream, never the report itself."""
        self._run(tmp_path, monkeypatch, ["sync", "--json", "--quiet"], "sync: {}\n")
        assert json.loads(capsys.readouterr().out)


class TestNoticeIsNotProgress:
    """A problem is not chatter, so no verbosity may swallow it."""

    @pytest.fixture(autouse=True)
    def _restore(self):
        from living_ink import logs

        yield
        logs._console_mode = logs.ConsoleMode.PLAIN

    @pytest.mark.parametrize("mode", ["PLAIN", "QUIET", "VERBOSE", "JSON"])
    def test_every_mode_says_it_on_stderr(self, mode, capsys):
        from living_ink import logs

        logs._console_mode = logs.ConsoleMode[mode]
        logs.notice("deprecated key")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "deprecated key" in captured.err

    def test_a_secret_is_redacted_on_the_way_out(self, capsys):
        from living_ink import logs
        from living_ink.redact import register_secret

        register_secret("AIzaSyTOPSECRETVALUE")
        logs.notice("key AIzaSyTOPSECRETVALUE is deprecated")
        assert "TOPSECRET" not in capsys.readouterr().err


class TestReconfiguringKeepsTheMode:
    """``ensure_configured`` carries every mode forward, JSON included."""

    @pytest.mark.parametrize("mode", ["QUIET", "VERBOSE", "JSON"])
    def test_a_moved_log_file_does_not_reset_the_console(self, mode, tmp_path):
        from living_ink import logs

        try:
            logs.configure(
                tmp_path / "first.log",
                quiet=mode == "QUIET",
                verbose=mode == "VERBOSE",
                json_output=mode == "JSON",
            )
            logs.ensure_configured(tmp_path / "second.log")
            assert logs.console_mode() is logs.ConsoleMode[mode]
        finally:
            logs.reset_handlers()
            logs._console_mode = logs.ConsoleMode.PLAIN


class TestDestinationLabels:
    """Class names are an implementation detail; printed names are not."""

    def test_the_suffix_is_dropped_and_words_separated(self):
        from living_ink.cli import short_destination

        assert short_destination("FakeApiDestination") == "Fake Api"

    def test_a_single_word_is_left_alone(self):
        from living_ink.cli import short_destination

        assert short_destination("ObsidianDestination") == "Obsidian"


class TestInfoDefersTheDocumentQuestion:
    """`info` reports the setup; the tablet is asked about separately."""

    def _report(self):
        """Collect a report with every probe stubbed out."""
        import tempfile
        from pathlib import Path as _Path

        from living_ink.cli import collect_status

        tmp = _Path(tempfile.mkdtemp()) / "config.yml"
        tmp.write_text("sync: {}\n")
        with (
            patch("living_ink.setup_wizard.verify_remarkable_ssh", return_value=(False, "no")),
            patch("living_ink.setup_wizard.verify_remarkable_token", return_value=(False, "no")),
            patch("living_ink.setup_wizard.verify_ai_provider", return_value=(False, "no")),
        ):
            return collect_status(tmp)

    def test_no_document_tally_is_collected(self):
        """A count from the database alone would be a guess about the tablet."""
        assert "documents" not in self._report().to_dict()

    def test_the_console_says_nothing_about_documents(self, capsys):
        """Not even a pointer: a line about documents that reports no documents
        reads as an answer, and the answer is somewhere else."""
        InfoCommand._render_console(self._report())
        assert "Documents" not in capsys.readouterr().out

    def test_list_is_gone(self):
        assert "list" not in LivingInkCLI().commands

    def test_the_preview_flags_parse(self):
        args = LivingInkCLI().build_parser().parse_args(["sync", "--preview", "--all", "--json"])
        assert (args.command, args.preview, args.all, args.output_json) == (
            "sync",
            True,
            True,
            True,
        )


class TestInfoReportsTheStores:
    """The two files a sync depends on, reported where `state` and `cache` were.

    Both commands are gone; what they said about the database and the caches is
    now a block of one `info` report, so these exercise the reader and the
    renderer rather than a command each.
    """

    @pytest.fixture
    def store(self, tmp_path, monkeypatch):
        """A real store on a throwaway database, wired into the status reader.

        Both `caches.state_db_path` and `pipeline.get_state_store` read
        `pipeline.DATA_DIR` when they are called, so redirecting it is enough to
        keep the developer's own `state.db` out of the test.
        """
        from living_ink import pipeline

        monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path)
        monkeypatch.setattr(pipeline, "ensure_runtime_dirs", lambda: None)
        pipeline.reset_state_store()
        opened = pipeline.get_state_store()
        opened.record_document("id-1", name="Journal", folder="Personal", version="v1")
        opened.record_publication("id-1", "ObsidianDestination", "v1", recipe="")
        yield opened
        pipeline.reset_state_store()

    def _report(self, tmp_path):
        """Collect the state half of a report, which needs no probes stubbed."""
        from living_ink.cli import status as status_module

        report = status_module.StatusReport(config_path=tmp_path / "config.yml")
        status_module._collect_state(report)
        return report

    def test_the_summary_counts_the_tables(self, store, tmp_path):
        report = self._report(tmp_path)

        assert report.state_exists
        assert report.state_counts["documents"] == 1
        assert report.state_counts["publications"] == 1

    def test_the_schema_version_comes_from_the_file(self, store, tmp_path):
        """The constant says what this build writes; the pragma says what is there."""
        from living_ink import state

        assert self._report(tmp_path).state_schema == state.SCHEMA_VERSION

    def test_a_missing_database_is_reported_not_created(self, tmp_path, monkeypatch):
        from living_ink import pipeline

        empty = tmp_path / "empty"
        monkeypatch.setattr(pipeline, "DATA_DIR", empty)
        report = self._report(tmp_path)

        assert not report.state_exists
        assert report.state_path is not None
        assert not empty.exists()

    def test_an_unreadable_database_does_not_take_the_rest_down(self, store, tmp_path, monkeypatch):
        """A corrupt state file is the condition a health check exists to find."""
        from living_ink import pipeline

        monkeypatch.setattr(
            pipeline,
            "get_state_store",
            MagicMock(side_effect=sqlite3.DatabaseError("file is not a database")),
        )
        report = self._report(tmp_path)

        assert not report.state_exists
        assert report.state_counts == {}

    def test_the_console_names_what_is_recorded(self, store, tmp_path, capsys):
        InfoCommand._render_state(self._report(tmp_path))
        out = capsys.readouterr().out

        assert "1 document(s)" in out
        assert "1 publication(s)" in out

    def test_a_fresh_install_says_so_rather_than_printing_zeros(
        self, tmp_path, monkeypatch, capsys
    ):
        from living_ink import pipeline

        monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path / "empty")
        InfoCommand._render_state(self._report(tmp_path))

        assert "living-ink sync" in capsys.readouterr().out

    def test_the_last_run_is_printed_with_its_outcome(self, store, tmp_path, capsys):
        store.finish_run(store.start_run(), outcome="interrupted")

        InfoCommand._render_state(self._report(tmp_path))

        assert "interrupted" in capsys.readouterr().out

    def test_the_rows_are_the_dump_that_state_used_to_print(self, store):
        from living_ink.cli import state_rows

        assert state_rows()["documents"][0]["name"] == "Journal"

    def test_the_rows_are_empty_without_a_database(self, tmp_path, monkeypatch):
        from living_ink import pipeline
        from living_ink.cli import state_rows

        monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path / "empty")

        assert state_rows() == {}

    def test_json_carries_the_summary_and_the_rows(self, store, tmp_path, capsys):
        """One `--json`, where `status`, `state` and `cache` had three."""
        config = tmp_path / "config.yml"
        config.write_text("sync: {}\n")
        with (
            patch("living_ink.cli.commands.info.get_config_path", return_value=config),
            patch("living_ink.setup_wizard.verify_remarkable_ssh", return_value=(False, "no")),
            patch("living_ink.setup_wizard.verify_remarkable_token", return_value=(False, "no")),
            patch("living_ink.setup_wizard.verify_ai_provider", return_value=(False, "no")),
        ):
            InfoCommand().run(MagicMock(json=True))

        payload = json.loads(capsys.readouterr().out)["state"]
        assert payload["counts"]["documents"] == 1
        assert payload["rows"]["documents"][0]["name"] == "Journal"

    def test_the_cache_line_totals_every_cache(self, tmp_path, monkeypatch, capsys):
        """`cache` reported the caches and `status` measured them; now one does."""
        from living_ink.cache import RenderCache, TranscriptCache
        from living_ink.cli import caches as caches_module
        from living_ink.cli import status as status_module

        transcripts = TranscriptCache(tmp_path / "transcripts")
        transcripts.put("aa", "text")
        monkeypatch.setattr(
            caches_module,
            "all_caches",
            lambda: [transcripts, RenderCache(tmp_path / "renders")],
        )
        monkeypatch.setattr(caches_module, "state_db_path", lambda: tmp_path / "state.db")
        config = tmp_path / "config.yml"
        config.write_text("sync: {}\n")
        with (
            patch("living_ink.setup_wizard.verify_remarkable_ssh", return_value=(False, "no")),
            patch("living_ink.setup_wizard.verify_remarkable_token", return_value=(False, "no")),
            patch("living_ink.setup_wizard.verify_ai_provider", return_value=(False, "no")),
        ):
            report = status_module.collect_status(config)

        assert report.cache_entries == 1
        InfoCommand._render_console(report)
        assert "1 page(s)" in capsys.readouterr().out


class TestInfoWatchPanel:
    """Whether automatic syncing is working, in two lines and one banner.

    The cron expression is deliberately not among them: a user reading `info`
    is asking whether the thing works, and `0 9 * * 1` does not answer that.
    The panel and the `--json` payload are decided in one place — a health
    check whose two views disagree is worse than one view.
    """

    @pytest.fixture
    def store(self, tmp_path, monkeypatch):
        """A real store on a throwaway database, wired into the status reader."""
        from living_ink import pipeline

        monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path)
        monkeypatch.setattr(pipeline, "ensure_runtime_dirs", lambda: None)
        pipeline.reset_state_store()
        yield pipeline.get_state_store()
        pipeline.reset_state_store()

    def _report(self, tmp_path, **watch):
        """Collect the watch half of a report from a config fragment."""
        from living_ink.cli import status as status_module

        report = status_module.StatusReport(config_path=tmp_path / "config.yml")
        status_module._collect_watch(report, {"watch": watch})
        return report

    # -- reading the configuration -----------------------------------------

    def test_watching_off_is_not_a_problem(self, tmp_path):
        """An unset schedule is not a broken one."""
        report = self._report(tmp_path, enabled=False)

        assert (report.watch_enabled, report.watch_problem, report.watch_next_run) == (
            False,
            "",
            None,
        )

    def test_the_zone_is_named_even_when_watching_is_off(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TZ", "Asia/Tokyo")
        assert self._report(tmp_path, enabled=False).watch_timezone == "Asia/Tokyo"

    def test_watching_on_with_no_schedule_is_a_problem(self, tmp_path):
        report = self._report(tmp_path, enabled=True)

        assert "no schedule is set" in report.watch_problem
        assert report.watch_next_run is None

    def test_an_invalid_expression_names_the_field(self, tmp_path):
        report = self._report(tmp_path, enabled=True, schedule="0 9 * * funday")

        assert "day of week" in report.watch_problem
        assert report.watch_next_run is None

    def test_an_unknown_timezone_is_refused_rather_than_read_as_utc(self, tmp_path):
        """An hour or two off, all year, with nothing in the output admitting it."""
        report = self._report(
            tmp_path, enabled=True, schedule="0 9 * * *", timezone="Europe/Madridd"
        )

        assert "not a known timezone" in report.watch_problem
        assert report.watch_next_run is None

    def test_a_valid_schedule_reports_when_it_next_fires(self, store, tmp_path):
        report = self._report(
            tmp_path, enabled=True, schedule="0 9 * * *", timezone="Europe/Madrid"
        )
        from living_ink import scheduler

        upcoming = scheduler.parse_stored(report.watch_next_run)
        assert report.watch_problem == ""
        assert upcoming is not None and upcoming > datetime.now(timezone.utc)

    def test_it_reads_the_last_scheduled_run_and_not_the_last_manual_one(self, store, tmp_path):
        from living_ink import state as state_module

        scheduled = store.start_run(trigger=state_module.TRIGGER_SCHEDULED)
        store.finish_run(scheduled, outcome=state_module.OUTCOME_SUCCESS, published=2)
        store.start_run()

        report = self._report(tmp_path, enabled=True, schedule="* * * * *", timezone="UTC")

        assert report.last_scheduled_run["id"] == scheduled

    def test_an_unreadable_database_does_not_take_the_report_down(self, tmp_path, monkeypatch):
        from living_ink import pipeline

        monkeypatch.setattr(
            pipeline,
            "get_state_store",
            MagicMock(side_effect=sqlite3.DatabaseError("file is not a database")),
        )
        report = self._report(tmp_path, enabled=True, schedule="0 9 * * *", timezone="UTC")

        assert report.watch_problem == ""
        assert report.watch_alert == ""

    # -- deciding on the banner --------------------------------------------

    def _alert(self, last, *, minutes_late=60, schedule="0 9 * * *"):
        """Run the banner decision against a fixed clock.

        Args:
            last: The last scheduled run row, or None.
            minutes_late: How long ago the previous fire time was.
            schedule: The expression the report carries.

        Returns:
            The filled-in report.
        """
        from pathlib import Path as _Path

        from living_ink.cli import status as status_module

        tz = ZoneInfo("Europe/Madrid")
        due = datetime(2026, 9, 19, 9, 0, tzinfo=tz)
        now = due + timedelta(minutes=minutes_late)
        report = status_module.StatusReport(config_path=_Path("config.yml"))
        report.watch_schedule = schedule
        report.last_scheduled_run = last
        # The fixed clock is only honest if the schedule really did fire then.
        assert scheduler.previous_fire(schedule, now, tz) == due
        status_module._raise_watch_alert(report, now, tz)
        return report

    def test_a_failed_scheduled_run_raises_the_banner(self):
        from living_ink import state as state_module

        report = self._alert(
            {
                "outcome": state_module.OUTCOME_ERROR,
                "error": "reMarkable Cloud pairing was revoked",
                "started_at": "2026-09-19T07:00:00+00:00",
            }
        )

        assert "reMarkable Cloud pairing was revoked" in report.watch_alert
        assert "living-ink sync" in report.watch_alert_fix

    def test_a_quiet_night_is_not_a_failure(self):
        """A schedule that fires nightly and finds nothing is working."""
        from living_ink import state as state_module

        report = self._alert(
            {
                "outcome": state_module.OUTCOME_NOTHING_TO_DO,
                "started_at": "2026-09-19T07:00:00+00:00",
            }
        )

        assert report.watch_alert == ""

    def test_a_fire_time_that_nothing_answered_raises_the_banner(self):
        """What a watcher that is not running looks like from the outside:
        there is no error anywhere, because nothing ran to produce one."""
        report = self._alert(None, minutes_late=60 * 20)

        assert "did not run" in report.watch_alert
        assert "living-ink setup" in report.watch_alert_fix

    def test_a_tick_that_has_only_just_come_due_is_not_late(self):
        """A run in flight, or one a minute late because the machine was busy."""
        assert self._alert(None, minutes_late=5).watch_alert == ""

    def test_a_run_that_started_after_the_fire_time_clears_it(self):
        from living_ink import state as state_module

        report = self._alert(
            {
                "outcome": state_module.OUTCOME_SUCCESS,
                "started_at": "2026-09-19T07:02:00+00:00",  # 09:02 in Madrid
            }
        )

        assert report.watch_alert == ""

    def test_a_run_from_before_the_fire_time_does_not(self):
        from living_ink import state as state_module

        report = self._alert(
            {
                "outcome": state_module.OUTCOME_SUCCESS,
                "started_at": "2026-09-18T07:00:00+00:00",  # yesterday's
            }
        )

        assert "did not run" in report.watch_alert

    # -- what it prints ----------------------------------------------------

    def test_the_panel_says_how_to_turn_watching_on(self, tmp_path, capsys):
        InfoCommand._render_watch(self._report(tmp_path, enabled=False))

        assert "living-ink config" in capsys.readouterr().out

    def test_an_unusable_schedule_is_reported_in_the_panel(self, tmp_path, capsys):
        InfoCommand._render_watch(self._report(tmp_path, enabled=True))
        out = capsys.readouterr().out

        assert "Not usable" in out
        assert "no schedule is set" in out

    def test_a_schedule_that_never_ran_says_so(self, store, tmp_path, capsys):
        InfoCommand._render_watch(
            self._report(tmp_path, enabled=True, schedule="0 9 * * *", timezone="UTC")
        )
        out = capsys.readouterr().out

        assert "last run" in out and "never" in out
        assert "next run   in" in out

    def test_the_last_run_is_worded_the_way_watch_words_it(self, store, tmp_path, capsys):
        from living_ink import state as state_module

        run_id = store.start_run(trigger=state_module.TRIGGER_SCHEDULED)
        store.finish_run(run_id, outcome=state_module.OUTCOME_SUCCESS, published=3)

        InfoCommand._render_watch(
            self._report(tmp_path, enabled=True, schedule="0 9 * * *", timezone="UTC")
        )
        out = capsys.readouterr().out

        assert "synced 3 documents" in out
        assert "just now" in out

    def test_the_panel_never_prints_the_cron_expression(self, store, tmp_path, capsys):
        InfoCommand._render_watch(
            self._report(tmp_path, enabled=True, schedule="0 9 * * 1", timezone="UTC")
        )

        assert "0 9 * * 1" not in capsys.readouterr().out

    def test_the_banner_is_above_everything_else(self, tmp_path, capsys):
        """A schedule that stopped working produces no other symptom until
        somebody notices a notebook missing, so it cannot be halfway down."""
        from living_ink.cli import status as status_module

        report = status_module.StatusReport(config_path=tmp_path / "config.yml")
        report.watch_alert = "A scheduled sync was due 2 days ago and did not run."
        report.watch_alert_fix = "Check that the background job is running."

        InfoCommand._render_console(report)
        out = capsys.readouterr().out

        assert report.watch_alert in out
        assert out.index(report.watch_alert) < out.index("Living Ink")

    def test_a_healthy_report_shows_no_banner(self, store, tmp_path, capsys):
        report = self._report(tmp_path, enabled=True, schedule="* * * * *", timezone="UTC")
        InfoCommand._render_console(report)

        assert "⚠" not in capsys.readouterr().out

    def test_json_carries_the_whole_panel_and_the_job_under_it(self, tmp_path):
        """The supervisor's two booleans moved under `watch.job`: they no
        longer decide when anything runs, only whether the watcher is alive."""
        report = self._report(tmp_path, enabled=True, schedule="0 9 * * *", timezone="UTC")
        report.config_found = True
        report.auto_sync_installed = True
        payload = report.to_dict()["watch"]

        assert payload["enabled"] is True
        assert payload["schedule"] == "0 9 * * *"
        assert payload["timezone"] == "UTC"
        assert payload["next_run"] == report.watch_next_run
        assert payload["job"] == {"installed": True, "active": False}


class TestInterruptExitCode:
    """Ctrl+C ends a sync; it does not crash it."""

    def test_no_traceback_and_the_conventional_sigint_code(self):
        with patch("living_ink.cli.LivingInkCLI.run", side_effect=KeyboardInterrupt):
            with pytest.raises(SystemExit) as exit_info:
                main([])

        assert exit_info.value.code == 130


class TestDeviceLineInInfo:
    """Naming the tablet is useful; failing to name it must not break info."""

    @pytest.fixture
    def empty_store(self, tmp_path):
        """Point the device memory at a throwaway database.

        Without this the health probe would read — and a USB reading would
        write — the developer's own ``state.db``.

        Args:
            tmp_path: Pytest temporary directory.

        Yields:
            The open StateStore backing the memory for this test.
        """
        from living_ink.state import StateStore

        store = StateStore(tmp_path / "state.db")
        with patch("living_ink.pipeline.get_state_store", return_value=store):
            yield store

    def test_an_unreachable_tablet_falls_back_to_the_named_default(self, empty_store):
        """A guess is fine as long as it says it is one."""
        with patch("living_ink.ssh.create_ssh_client") as mock_create:
            mock_create.return_value.get_device_info.side_effect = RuntimeError("no route")
            described = _describe_connected_device("10.11.99.1", 22, "root")

        assert described == "reMarkable 2 (1404×1872) (assumed — connect over USB to confirm)"

    def test_a_transport_that_cannot_see_hardware_falls_back_too(self, empty_store):
        from living_ink.transport import UnsupportedOperation

        with patch("living_ink.ssh.create_ssh_client") as mock_create:
            mock_create.return_value.get_device_info.side_effect = UnsupportedOperation("no")
            described = _describe_connected_device("10.11.99.1", 22, "root")

        assert "assumed" in described

    def test_a_usb_reading_is_remembered_for_the_next_cloud_only_run(self, empty_store):
        """The whole point: one USB session teaches every later run."""
        from living_ink.transport import DeviceInfo

        with patch("living_ink.ssh.create_ssh_client") as mock_create:
            mock_create.return_value.get_device_info.return_value = DeviceInfo(
                "reMarkable Paper Pro", "3.20.0", (1620, 2160), color=True
            )
            _describe_connected_device("10.11.99.1", 22, "root")

        # Second call, no cable: the answer survives, and says where it is from.
        described = _describe_connected_device("10.11.99.1", 22, "root", live=False)
        assert described.startswith("reMarkable Paper Pro firmware 3.20.0 (1620×2160)")
        assert "remembered from USB" in described

    def test_a_reachable_tablet_is_described_in_one_line(self, empty_store):
        from living_ink.transport import DeviceInfo

        with patch("living_ink.ssh.create_ssh_client") as mock_create:
            mock_create.return_value.get_device_info.return_value = DeviceInfo(
                "reMarkable 2", "3.5.2", (1404, 1872)
            )
            described = _describe_connected_device("10.11.99.1", 22, "root")

        assert described == "reMarkable 2 firmware 3.5.2 (1404×1872)"

    def test_a_cloud_only_setup_names_the_assumption(self, tmp_path, capsys, empty_store):
        """A guessed model is fine on screen; an unlabelled guess is not."""
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        (cfg_dir / "config.yml").write_text(
            "remarkable:\n  preferred_connection: 'cloud'\nai:\n  provider: 'none'\n"
        )
        with patch("living_ink.setup_wizard.verify_ai_provider", return_value=(True, "OK")):
            InfoCommand(root=tmp_path).run(MagicMock(json=False))

        out = capsys.readouterr().out
        assert "Device:" in out
        assert "assumed" in out


class TestSyncPreviewFlag:
    """`sync --preview` previews a sync instead of running one."""

    def _rows(self, count, status=None):
        """Build `count` comparison rows, all in the same state."""

        return [
            {
                "id": f"doc-{n:04d}-aaaa",
                "name": f"Note {n}",
                "folder": "Work",
                "doc_type": "notebook",
                "status": status or STATUS_NEW,
                "pending": ["ObsidianDestination"],
                "published": {},
                "last_error": None,
            }
            for n in range(count)
        ]

    def _show(self, capsys, rows, orphans=None, **flags):
        """Run the command against a stubbed comparison and return its output."""
        from living_ink.cli import SyncCommand

        defaults = {"preview": True, "all": False, "output_json": False}
        args = argparse.Namespace(**{**defaults, **flags})
        with patch(
            "living_ink.cli.inventory.compare_with_device", return_value=(rows, orphans or [], None)
        ):
            code = SyncCommand().run(args)
        return code, capsys.readouterr().out

    def test_the_row_is_id_name_type_and_status(self):
        from living_ink.cli import format_comparison_row

        line = format_comparison_row(self._rows(1)[0])
        assert line.startswith("doc-0000  Note 0")
        assert ".notebook" in line

    def test_a_long_name_is_truncated_with_an_ellipsis(self):
        from living_ink.cli import format_comparison_row

        row = {**self._rows(1)[0], "name": "A notebook with a very long title"}
        line = format_comparison_row(row)
        assert "A notebook wit…" in line
        assert "very long title" not in line

    def test_the_summary_counts_each_status(self, capsys):
        rows = self._rows(2) + self._rows(1, status=STATUS_UP_TO_DATE)
        _, out = self._show(capsys, rows)
        assert "2  new" in out
        assert "1  up to date" in out

    def test_outstanding_work_is_listed_before_settled_work(self, capsys):
        settled = self._rows(1, status=STATUS_UP_TO_DATE)
        settled[0]["name"] = "Settled"
        _, out = self._show(capsys, settled + self._rows(1))
        assert out.index("Note 0") < out.index("Settled")

    def test_only_ten_rows_are_shown_by_default(self, capsys):
        _, out = self._show(capsys, self._rows(12))
        assert "Note 9" in out
        assert "Note 10" not in out
        assert "10 of 12 shown · 2 more — use --all" in out

    def test_all_shows_every_row(self, capsys):
        """And it prints them straight through — there is no pager to stop at.

        ``--all`` used to pause every ten rows on a terminal and wait for a
        keypress, which made a read-only question something a script could
        hang on.
        """
        _, out = self._show(capsys, self._rows(12), all=True)
        assert "Note 11" in out
        assert "use --all" not in out

    def test_documents_gone_from_the_tablet_are_counted(self, capsys):
        orphans = [{"id": "doc-old", "name": "Deleted"}]
        _, out = self._show(capsys, self._rows(1), orphans=orphans)
        assert "1  no longer on the tablet" in out

    def test_an_empty_tablet_says_so(self, capsys):
        code, out = self._show(capsys, [])
        assert code == 0
        assert "Nothing on the tablet" in out

    def test_nothing_pending_says_everything_is_up_to_date(self, capsys):
        _, out = self._show(capsys, self._rows(2, status=STATUS_UP_TO_DATE))
        assert "Everything is up to date." in out

    def test_json_keys_documents_by_the_stable_status_key(self, capsys):
        orphans = [{"id": "doc-old", "name": "Deleted"}]
        _, out = self._show(capsys, self._rows(1), orphans=orphans, output_json=True)
        payload = json.loads(out)
        assert payload["counts"] == {"failed": 0, "new": 1, "changed": 0, "up_to_date": 0}
        assert payload["documents"][0]["status"] == "new"
        assert payload["orphans"] == ["doc-old"]

    def test_the_preview_flag_never_runs_a_sync(self, capsys):
        from living_ink.cli import SyncCommand

        args = argparse.Namespace(preview=True, all=False, json=False)
        with patch("living_ink.cli.inventory.compare_with_device", return_value=([], [], None)):
            with patch.object(SyncCommand, "execute_sync") as mock_sync:
                SyncCommand().run(args)
        mock_sync.assert_not_called()

    def test_missing_configuration_does_not_launch_the_wizard(self, capsys):
        from living_ink.cli import SyncCommand
        from living_ink.config import ConfigurationMissing

        args = argparse.Namespace(preview=True, all=False, json=False)
        with patch(
            "living_ink.cli.inventory.compare_with_device",
            side_effect=ConfigurationMissing("no config"),
        ):
            with patch.object(SyncCommand, "_handle_missing_config") as mock_wizard:
                code = SyncCommand().run(args)

        mock_wizard.assert_not_called()
        assert code == 1
        assert "living-ink setup" in capsys.readouterr().err


class TestThePreviewNarrowsExactlyLikeTheRun:
    """``--preview`` answers "what would *this exact command* do".

    It used to answer a different question — "where does everything stand" —
    and build its own :class:`SelectionCriteria` holding nothing but the
    configured exclusions. So ``sync --preview --pdf`` previewed notebooks,
    ``--limit 1`` previewed forty documents, ``--tag`` was ignored outright,
    and ``--ssh`` was read from a ``dest`` the generated parser had stopped
    using, so a forced transport silently did nothing. Each of those is a
    preview predicting something the run does not do, in the one feature whose
    entire purpose is to say what will happen.
    """

    def _probe(self, monkeypatch, tmp_path, argv, config=None):
        """Run the real comparison against stubbed seams and report what it asked.

        Args:
            monkeypatch: Pytest's patcher.
            tmp_path: Throwaway directory for the state database.
            argv: The command line, without the program name.
            config: The config file's contents, if any.

        Returns:
            ``(criteria, settings, client, passed_client)`` — what the
            classifier was handed.
        """
        from living_ink import api as api_module
        from living_ink import pipeline as pipeline_module
        from living_ink.cli import LivingInkCLI, inventory
        from living_ink.core import selection as selection_module
        from living_ink.core.selection import Selection
        from living_ink.state import StateStore

        client = MagicMock()
        client.get_meta_items.return_value = []
        client.get_device_info.return_value = None

        seen = {}

        def _select(collection, criteria, store, destinations, *, settings, client=None):
            seen.update(criteria=criteria, settings=settings, client=client)
            return Selection()

        store = StateStore(tmp_path / "state.db")
        monkeypatch.setattr(selection_module, "select", _select)
        monkeypatch.setattr(api_module, "get_rmapi", lambda settings: client)
        monkeypatch.setattr(inventory, "get_config_path", lambda root=None: tmp_path / "none.yml")
        monkeypatch.setattr(pipeline_module, "get_default_config", lambda: config or {})
        monkeypatch.setattr(pipeline_module, "get_default_destinations", lambda: [])
        monkeypatch.setattr(pipeline_module, "get_state_store", lambda: store)

        args = LivingInkCLI().build_parser().parse_args(argv)
        try:
            inventory.compare_with_device(args)
        finally:
            store.close()
        return seen["criteria"], seen["settings"], client, seen["client"]

    def _run_criteria(self, tmp_path, argv, config=None):
        """Return the criteria the run itself would build for the same argv."""
        from living_ink.cli import LivingInkCLI
        from living_ink.cli.commands.sync import sync_arguments
        from living_ink.pipeline import SyncPipeline

        args = LivingInkCLI().build_parser().parse_args(argv)
        pipe = SyncPipeline(**sync_arguments(args), destinations=[])
        return pipe._criteria()

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["sync", "--preview"], id="bare"),
            pytest.param(["sync", "--preview", "--pdf"], id="type"),
            pytest.param(["sync", "--preview", "--limit", "3"], id="limit"),
            pytest.param(["sync", "--preview", "--tag", "work"], id="tag"),
            pytest.param(["sync", "--preview", "--source-path", "Journal/"], id="path"),
            pytest.param(["sync", "--preview", "--source-regex", r"^Work/"], id="regex"),
            pytest.param(["sync", "--preview", "--notebook", "Standup"], id="target"),
            pytest.param(["sync", "--preview", "--force"], id="force"),
            pytest.param(["sync", "--preview", "--exclude", "Templates"], id="exclude"),
        ],
    )
    def test_it_builds_the_criteria_the_run_would_build(self, monkeypatch, tmp_path, argv):
        """The strongest form of the promise: the two objects are equal.

        Compared whole rather than field by field, so a criterion added later
        is covered here the day it lands instead of the day somebody
        remembers to extend a list.
        """
        criteria, _, _, _ = self._probe(monkeypatch, tmp_path, argv)

        assert criteria == self._run_criteria(tmp_path, argv)

    def test_the_configured_answer_still_shows_through(self, monkeypatch, tmp_path):
        """A preview with no flags previews the config, not the schema defaults."""
        config = {"sync": {"types": ["pdf"], "limit": 4, "tags": ["work"]}}

        criteria, _, _, _ = self._probe(monkeypatch, tmp_path, ["sync", "--preview"], config)

        assert criteria.types == frozenset({"pdf"})
        assert criteria.limit == 4
        assert criteria.tags == frozenset({"work"})

    def test_a_forced_transport_reaches_the_preview(self, monkeypatch, tmp_path):
        """``--ssh`` was looked for under a ``dest`` that no longer exists.

        The flag arrives as ``preferred_connection`` — the settings field the
        generated parser names — so reading ``args.ssh`` found nothing and the
        preview quietly used the configured preference instead.
        """
        config = {"remarkable": {"preferred_connection": "cloud"}}

        _, settings, _, _ = self._probe(
            monkeypatch, tmp_path, ["sync", "--preview", "--ssh"], config
        )

        assert settings.preferred_connection == "ssh"

        _, unflagged, _, _ = self._probe(monkeypatch, tmp_path, ["sync", "--preview"], config)

        assert unflagged.preferred_connection == "cloud"

    def test_the_classifier_is_given_the_transport(self, monkeypatch, tmp_path):
        """Without it the type is guessed from the title and no tag can be read.

        ``_keep_tagged`` drops nothing when it has no client — a filter that
        cannot run must not silently exclude everything — so a clientless
        preview would list documents ``--tag`` excludes from the run.
        """
        _, _, client, passed = self._probe(monkeypatch, tmp_path, ["sync", "--preview"])

        assert passed is client
