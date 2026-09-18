"""Tests for living_ink.cli module.

Covers the CLI argument parsing, Command Pattern architecture,
and subcommands (status, setup, sync).
"""

import argparse
import json
import os
from unittest.mock import MagicMock, patch

import pytest

from living_ink.cli import (
    BaseCommand,
    LivingInkCLI,
    SetupCommand,
    StatusCommand,
    SyncCommand,
    WatchCommand,
    main,
)
from living_ink.config import ConfigurationMissing, find_repo_root, get_config_path
from living_ink.settings import SOURCE_CONFIG, SOURCE_ENV
from living_ink.setup_wizard import WizardResult


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
    args = MagicMock(ssh=True, notebook=None, limit=0, folder=None, json=False)

    pipeline = _run_sync_capturing_pipeline(args, tmp_path)

    assert pipeline.settings.use_ssh is True
    assert pipeline.settings.preferred_connection == "ssh"


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


def test_cmd_sync_cloud_flag_resolves_to_cloud(tmp_path, monkeypatch):
    """SyncCommand resolves --cloud into the pipeline's settings."""
    monkeypatch.delenv("REMARKABLE_PREFERRED_CONNECTION", raising=False)
    args = MagicMock(ssh=False, cloud=True, notebook=None, limit=0, folder=None, json=False)

    pipeline = _run_sync_capturing_pipeline(args, tmp_path)

    assert pipeline.settings.preferred_connection == "cloud"
    assert pipeline.settings.use_ssh is False


def test_cmd_sync_does_not_write_settings_into_the_environment(tmp_path, monkeypatch):
    """Resolved settings stay on the pipeline instead of leaking into os.environ."""
    for var in ("REMARKABLE_USE_SSH", "REMARKABLE_PREFERRED_CONNECTION", "APPLE_NOTES_FOLDER"):
        monkeypatch.delenv(var, raising=False)
    args = MagicMock(ssh=True, cloud=False, notebook=None, limit=0, folder=None, json=False)

    _run_sync_capturing_pipeline(args, tmp_path)

    for var in ("REMARKABLE_USE_SSH", "REMARKABLE_PREFERRED_CONNECTION", "APPLE_NOTES_FOLDER"):
        assert var not in os.environ


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
            opts = mock_init.call_args.kwargs["options"]
            assert opts.notebook == "MyNotes"
            assert opts.limit == 5
            assert opts.folder == "TestFolder"
            assert opts.ssh is True
            assert opts.sync_pdfs is True
            # An unset store-true flag must defer to config, not force False.
            assert opts.sync_epubs is None
            assert opts.keep_temp is True
            mock_run.assert_called_once()


def test_setup_command_execution(tmp_path):
    """SetupCommand invokes run_wizard with root directory."""
    cmd = SetupCommand(root=tmp_path)
    args = argparse.Namespace()
    with patch(
        "living_ink.setup_wizard.run_wizard", return_value=WizardResult(saved=True)
    ) as mock_wizard:
        code = cmd.run(args)
        assert code == 0
        mock_wizard.assert_called_once_with(repo_dir=tmp_path)


class TestWizardSyncHandoff:
    """The CLI, not the wizard or the pipeline, decides what runs next."""

    def test_setup_runs_sync_when_the_user_asks(self, tmp_path):
        """A wizard that reports run_sync_requested hands off to SyncCommand."""
        result = WizardResult(saved=True, run_sync_requested=True)
        with patch("living_ink.setup_wizard.run_wizard", return_value=result):
            with patch.object(SyncCommand, "run", return_value=0) as mock_sync:
                assert SetupCommand(root=tmp_path).run(argparse.Namespace()) == 0
                mock_sync.assert_called_once()

    def test_setup_skips_sync_when_the_user_declines(self, tmp_path):
        """Declining the first sync leaves SyncCommand untouched."""
        result = WizardResult(saved=True, run_sync_requested=False)
        with patch("living_ink.setup_wizard.run_wizard", return_value=result):
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
        assert "run: living-ink setup" in capsys.readouterr().out

    def test_sync_launched_by_the_wizard_does_not_reoffer_it(self, tmp_path, capsys):
        """A still-broken config after setup reports the problem, it does not loop."""
        result = WizardResult(saved=True, run_sync_requested=True)
        with patch("living_ink.setup_wizard.run_wizard", return_value=result):
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


def test_cmd_sync_dry_run_flag_reaches_the_pipeline(tmp_path):
    """--dry-run is an option on the run, not a setting on disk."""
    args = argparse.Namespace(
        notebook=None,
        limit=0,
        folder=None,
        ssh=False,
        cloud=False,
        sync_pdfs=False,
        sync_epubs=False,
        all_types=False,
        keep_temp=False,
        dry_run=True,
    )
    pipeline_obj = _run_sync_capturing_pipeline(args, tmp_path)

    assert pipeline_obj.dry_run is True
    assert pipeline_obj.keep_temp is True


class TestStatusSettingsReport:
    """`status` reports the effective settings, not just connectivity."""

    def _report(self, config_text, env=None):
        """Collect a status report for a config file, with probing stubbed out."""
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
        assert entries["remarkable_token"]["value"] == "set"
        assert "sekrit" not in json.dumps(report.to_dict())

    def test_a_missing_config_reports_no_settings(self):
        from pathlib import Path as _Path

        from living_ink.cli import collect_status

        report = collect_status(_Path("/nonexistent/living-ink/config.yml"))

        assert report.settings == []
        assert report.to_dict()["settings"] == []

    def test_console_output_names_the_overriding_variable(self, capsys):
        report = self._report("sync: {}\n", env={"SYNC_OCR_CONCURRENCY": "3"})

        StatusCommand._render_settings(report)

        out = capsys.readouterr().out
        assert "ocr_concurrency" in out
        assert "SYNC_OCR_CONCURRENCY" in out


class TestWatchCommand:
    """`watch` syncs on a timer and survives everything but a bad config."""

    def _watch(self, side_effects, interval=30):
        """Run the loop until the stubbed sync raises KeyboardInterrupt.

        Args:
            side_effects: What successive execute_sync calls do; the last one
                must interrupt, or the loop would never end.
            interval: Value for --interval.

        Returns:
            (exit code, execute_sync mock, sleep mock).
        """
        args = argparse.Namespace(interval=interval)
        with (
            patch.object(SyncCommand, "execute_sync", side_effect=side_effects) as sync,
            patch("living_ink.cli.time.sleep") as sleep,
        ):
            code = WatchCommand().run(args)
        return code, sync, sleep

    def test_syncs_repeatedly_until_interrupted(self):
        code, sync, sleep = self._watch([True, True, KeyboardInterrupt()])

        assert code == 0
        assert sync.call_count == 3
        assert sleep.call_count == 2

    def test_a_failed_sync_does_not_end_the_watch(self):
        """An unplugged tablet is the condition watch exists to ride out."""
        code, sync, _ = self._watch([False, KeyboardInterrupt()])

        assert code == 0
        assert sync.call_count == 2

    def test_an_unexpected_error_does_not_end_the_watch(self):
        code, sync, _ = self._watch([RuntimeError("tablet vanished"), KeyboardInterrupt()])

        assert code == 0
        assert sync.call_count == 2

    def test_a_missing_config_stops_the_watch(self):
        """That failure will still be there next tick, so looping is pointless."""
        code, sync, _ = self._watch([ConfigurationMissing("no config")])

        assert code == 1
        assert sync.call_count == 1

    def test_the_watch_never_offers_the_setup_wizard(self):
        """It runs unattended; blocking on input() would hang a daemon."""
        args = argparse.Namespace(interval=30)
        with (
            patch.object(SyncCommand, "execute_sync", side_effect=ConfigurationMissing("nope")),
            patch("builtins.input", side_effect=AssertionError("must not prompt")),
            patch("living_ink.cli.time.sleep"),
        ):
            assert WatchCommand().run(args) == 1

    def test_the_interval_is_floored(self):
        """Polling faster than a sync finishes just stacks runs on each other."""
        _, _, sleep = self._watch([True, KeyboardInterrupt()], interval=1)

        sleep.assert_called_once_with(WatchCommand.MIN_INTERVAL)

    def test_interrupting_the_wait_stops_cleanly(self):
        args = argparse.Namespace(interval=30)
        with (
            patch.object(SyncCommand, "execute_sync", return_value=True),
            patch("living_ink.cli.time.sleep", side_effect=KeyboardInterrupt),
        ):
            assert WatchCommand().run(args) == 0

    def test_watch_is_registered_and_takes_every_sync_option(self):
        parser = LivingInkCLI().build_parser()

        args = parser.parse_args(["watch", "--interval", "60", "--notebook", "Foo", "--cloud"])

        assert (args.command, args.interval, args.notebook, args.cloud) == (
            "watch",
            60,
            "Foo",
            True,
        )

    def test_watch_interval_defaults_to_half_an_hour(self):
        args = LivingInkCLI().build_parser().parse_args(["watch"])

        assert args.interval == WatchCommand.DEFAULT_INTERVAL


class TestVerbosityFlags:
    """--verbose and --quiet are accepted on either side of the subcommand."""

    def _parse(self, argv):
        return LivingInkCLI().build_parser().parse_args(argv)

    def test_verbose_after_the_subcommand(self):
        assert self._parse(["sync", "--verbose"]).verbose is True

    def test_verbose_before_the_subcommand(self):
        """A subparser default would silently overwrite the flag given here."""
        assert self._parse(["--verbose", "sync"]).verbose is True

    def test_quiet_after_the_subcommand(self):
        assert self._parse(["sync", "-q"]).quiet is True

    def test_neither_flag_leaves_both_unset(self):
        args = self._parse(["sync"])
        assert getattr(args, "verbose", False) is False
        assert getattr(args, "quiet", False) is False

    def test_dispatch_configures_logging(self, tmp_path):
        from living_ink import logs

        args = argparse.Namespace(command="status", config=None, verbose=True, quiet=False)
        with (
            patch("living_ink.pipeline.LOG_PATH", tmp_path / "pipeline.log"),
            patch.object(StatusCommand, "run", return_value=0),
        ):
            try:
                LivingInkCLI().dispatch(args)
                assert logs.console_mode() is logs.ConsoleMode.VERBOSE
            finally:
                logs.reset_handlers()
                logs._console_mode = logs.ConsoleMode.PLAIN


class TestListCommand:
    """`list` answers "did my notes make it?" without a full inventory dump."""

    ROWS = [
        {
            "id": "doc-1",
            "name": "Meeting Notes",
            "folder": "Work",
            "status": "pending",
            "pending": ["ObsidianDestination"],
            "published": {},
            "last_error": None,
        },
        {
            "id": "doc-2",
            "name": "Sketchbook",
            "folder": "Art",
            "status": "failing",
            "pending": ["ObsidianDestination"],
            "published": {},
            "last_error": "Download timed out",
        },
        {
            "id": "doc-3",
            "name": "Journal",
            "folder": None,
            "status": "synced",
            "pending": [],
            "published": {"ObsidianDestination": "v1"},
            "last_error": None,
        },
    ]

    def _run(self, capsys, rows=None, **flags):
        """Run the command against a stubbed inventory and return its output."""
        from living_ink.cli import ListCommand

        defaults = {"all": False, "json": False}
        args = argparse.Namespace(**{**defaults, **flags})
        with patch(
            "living_ink.cli.collect_inventory", return_value=self.ROWS if rows is None else rows
        ):
            code = ListCommand().run(args)
        return code, capsys.readouterr().out

    def test_pending_and_failing_are_shown_by_default(self, capsys):
        _, out = self._run(capsys)
        assert "Meeting Notes" in out
        assert "Sketchbook" in out

    def test_synced_documents_are_hidden_by_default(self, capsys):
        _, out = self._run(capsys)
        assert "Journal" not in out
        assert "1 synced" in out

    def test_all_shows_everything(self, capsys):
        _, out = self._run(capsys, all=True)
        assert "Journal" in out

    def test_the_failure_reason_is_printed(self, capsys):
        _, out = self._run(capsys)
        assert "Download timed out" in out

    def test_a_pending_document_names_the_destination_it_owes(self, capsys):
        _, out = self._run(capsys)
        assert "Obsidian" in out

    def test_a_clean_library_says_so(self, capsys):
        clean = [dict(self.ROWS[2])]
        _, out = self._run(capsys, rows=clean)
        assert "Everything is up to date" in out

    def test_an_empty_database_points_at_sync(self, capsys):
        _, out = self._run(capsys, rows=[])
        assert "living-ink sync" in out

    def test_json_output_carries_rows_and_counts(self, capsys):
        _, out = self._run(capsys, json=True)
        payload = json.loads(out)
        assert len(payload["documents"]) == 3
        assert payload["counts"] == {"synced": 1, "pending": 1, "failing": 1}

    def test_a_pending_document_is_not_an_error_exit(self, capsys):
        """Scripts must not treat "someone wrote a new page" as a failure."""
        code, _ = self._run(capsys)
        assert code == 0

    def test_the_command_is_registered(self):
        assert "list" in LivingInkCLI().commands

    def test_the_parser_accepts_the_flags(self):
        args = LivingInkCLI().build_parser().parse_args(["list", "--all", "--json"])
        assert (args.command, args.all, args.json) == ("list", True, True)


class TestDestinationLabels:
    """Class names are an implementation detail; printed names are not."""

    def test_the_suffix_is_dropped_and_words_separated(self):
        from living_ink.cli import short_destination

        assert short_destination("AppleNotesDestination") == "Apple Notes"

    def test_a_single_word_is_left_alone(self):
        from living_ink.cli import short_destination

        assert short_destination("ObsidianDestination") == "Obsidian"


class TestStatusDocumentCounts:
    """`status` summarises the inventory in one line."""

    def _report(self, rows):
        """Collect a report against a stubbed inventory, probing disabled."""
        import tempfile
        from pathlib import Path as _Path

        from living_ink.cli import collect_status

        tmp = _Path(tempfile.mkdtemp()) / "config.yml"
        tmp.write_text("sync: {}\n")
        with (
            patch("living_ink.setup_wizard.verify_remarkable_ssh", return_value=(False, "no")),
            patch("living_ink.setup_wizard.verify_remarkable_token", return_value=(False, "no")),
            patch("living_ink.setup_wizard.verify_ai_provider", return_value=(False, "no")),
            patch("living_ink.cli.collect_inventory", return_value=rows),
        ):
            return collect_status(tmp)

    def test_the_counts_are_collected(self):
        report = self._report(TestListCommand.ROWS)
        assert (report.documents_synced, report.documents_pending, report.documents_failing) == (
            1,
            1,
            1,
        )

    def test_an_empty_inventory_is_reported_as_unknown(self):
        report = self._report([])
        assert report.documents_known is False

    def test_an_unreadable_database_does_not_break_the_report(self):
        import tempfile
        from pathlib import Path as _Path

        from living_ink.cli import collect_status

        tmp = _Path(tempfile.mkdtemp()) / "config.yml"
        tmp.write_text("sync: {}\n")
        with (
            patch("living_ink.setup_wizard.verify_remarkable_ssh", return_value=(False, "no")),
            patch("living_ink.setup_wizard.verify_remarkable_token", return_value=(False, "no")),
            patch("living_ink.setup_wizard.verify_ai_provider", return_value=(False, "no")),
            patch("living_ink.cli.collect_inventory", side_effect=OSError("disk is gone")),
        ):
            report = collect_status(tmp)

        assert report.documents_known is False
        assert report.usable is True

    def test_json_output_includes_the_counts(self):
        report = self._report(TestListCommand.ROWS)
        assert report.to_dict()["documents"] == {
            "known": True,
            "synced": 1,
            "pending": 1,
            "failing": 1,
        }

    def test_the_console_line_points_at_list_when_work_is_outstanding(self, capsys):
        report = self._report(TestListCommand.ROWS)
        StatusCommand._render_console(report)

        out = capsys.readouterr().out
        assert "1 pending" in out
        assert "living-ink list" in out


class TestStateCommand:
    """`state` is the hand tool for the file everything else depends on."""

    @pytest.fixture
    def store(self, tmp_path, monkeypatch):
        """A real store on a throwaway database, wired into the command."""
        from living_ink import pipeline

        monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path)
        monkeypatch.setattr(pipeline, "ROOT", tmp_path)
        monkeypatch.setattr(pipeline, "ensure_runtime_dirs", lambda: None)
        pipeline.reset_state_store()
        opened = pipeline.get_state_store()
        opened.record_document("id-1", name="Journal", folder="Personal", version="v1")
        opened.record_publication("id-1", "ObsidianDestination", "v1")
        yield opened
        pipeline.reset_state_store()

    def _run(self, capsys, **flags):
        """Run the command and return its exit code and output."""
        from living_ink.cli import StateCommand

        defaults = {
            "dump": False,
            "forget": None,
            "repair": False,
            "destination": None,
            "json": False,
        }
        code = StateCommand().run(argparse.Namespace(**{**defaults, **flags}))
        return code, capsys.readouterr().out

    def test_the_summary_counts_the_tables(self, store, capsys):
        _, out = self._run(capsys)
        assert "documents" in out
        assert "schema" in out

    def test_a_missing_database_is_reported_not_created(self, tmp_path, monkeypatch, capsys):
        from living_ink import pipeline

        monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path / "empty")
        code, out = self._run(capsys)

        assert code == 1
        assert "living-ink sync" in out
        assert not (tmp_path / "empty").exists()

    def test_dump_prints_the_rows(self, store, capsys):
        _, out = self._run(capsys, dump=True)
        assert "Journal" in out
        assert "[publications]" in out

    def test_dump_json_is_parseable(self, store, capsys):
        _, out = self._run(capsys, dump=True, json=True)
        assert json.loads(out)["documents"][0]["name"] == "Journal"

    def test_forget_drops_the_publication(self, store, capsys):
        code, out = self._run(capsys, forget="Journal")

        assert code == 0
        assert store.published_versions("ObsidianDestination") == {}
        assert "next sync" in out

    def test_forget_accepts_a_folder_path(self, store, capsys):
        code, _ = self._run(capsys, forget="Personal/Journal")
        assert code == 0

    def test_forget_can_target_one_destination(self, store, capsys):
        store.record_publication("id-1", "AppleNotesDestination", "v1")

        self._run(capsys, forget="id-1", destination="ObsidianDestination")

        assert store.published_versions("AppleNotesDestination") == {"id-1": "v1"}

    def test_forget_refuses_an_unknown_document(self, store, capsys):
        code, out = self._run(capsys, forget="nope")
        assert code == 1
        assert "No document matches" in out

    def test_forget_refuses_to_guess_between_two_matches(self, store, capsys):
        """Picking one would silently re-OCR the wrong notebook."""
        store.record_document("id-2", name="Journal", folder="Work", version="v1")

        code, out = self._run(capsys, forget="Journal")

        assert code == 1
        assert "id-1" in out and "id-2" in out
        assert store.published_versions("ObsidianDestination") == {"id-1": "v1"}

    def test_repair_reports_a_healthy_database(self, store, capsys):
        code, out = self._run(capsys, repair=True)
        assert code == 0
        assert "ok" in out

    def test_repair_reports_damage_without_deleting_anything(self, store, capsys, monkeypatch):
        monkeypatch.setattr(store, "integrity_check", lambda: "page 4 is never used")

        code, out = self._run(capsys, repair=True)

        assert code == 1
        assert "page 4 is never used" in out
        assert store.path.exists()

    def test_the_command_is_registered(self):
        assert "state" in LivingInkCLI().commands

    def test_the_actions_are_mutually_exclusive(self):
        parser = LivingInkCLI().build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["state", "--dump", "--repair"])


class TestCacheCommand:
    """`cache` is the hand tool for the thing that saves the money."""

    @pytest.fixture
    def cache(self, tmp_path, monkeypatch):
        """A real cache in a throwaway directory, wired into the command."""
        from living_ink import cli as cli_module
        from living_ink.cache import TranscriptCache

        built = TranscriptCache(tmp_path / "transcripts")
        monkeypatch.setattr(cli_module, "transcript_cache", lambda: built)
        return built

    def _run(self, capsys, **flags):
        """Run the command and return its exit code and output."""
        from living_ink.cli import CacheCommand

        defaults = {"clear": False, "prune": None, "json": False}
        code = CacheCommand().run(argparse.Namespace(**{**defaults, **flags}))
        return code, capsys.readouterr().out

    def test_an_empty_cache_is_not_an_error(self, cache, capsys):
        code, out = self._run(capsys)
        assert code == 0
        assert "0 page(s)" in out

    def test_the_summary_counts_the_entries(self, cache, capsys):
        cache.put("aa", "raw", "clean")
        cache.put("bb", "raw", "clean")
        _, out = self._run(capsys)
        assert "2 page(s)" in out

    def test_the_summary_says_where_it_lives(self, cache, capsys):
        _, out = self._run(capsys)
        assert str(cache.root) in out

    def test_a_disabled_cache_says_so(self, tmp_path, monkeypatch, capsys):
        from living_ink import cli as cli_module
        from living_ink.cache import TranscriptCache

        off = TranscriptCache(tmp_path / "t", enabled=False)
        monkeypatch.setattr(cli_module, "transcript_cache", lambda: off)
        _, out = self._run(capsys)
        assert "disabled" in out

    def test_json_reports_the_same_numbers(self, cache, capsys):
        cache.put("aa", "raw", "clean")
        _, out = self._run(capsys, json=True)
        payload = json.loads(out)
        assert payload["entries"] == 1
        assert payload["size_bytes"] > 0

    def test_clearing_removes_everything(self, cache, capsys):
        cache.put("aa", "raw", "clean")
        code, out = self._run(capsys, clear=True)
        assert code == 0
        assert "1 cached page(s)" in out
        assert cache.stats() == (0, 0)

    def test_clearing_warns_that_the_pages_will_be_paid_for_again(self, cache, capsys):
        cache.put("aa", "raw", "clean")
        _, out = self._run(capsys, clear=True)
        assert "paid for" in out

    def test_pruning_keeps_fresh_entries(self, cache, capsys):
        cache.put("aa", "raw", "clean")
        _, out = self._run(capsys, prune=30)
        assert "0 cached page(s)" in out
        assert cache.get("aa") is not None

    def test_pruning_drops_stale_entries(self, cache, capsys):
        import os
        import time

        cache.put("aa", "raw", "clean")
        old = time.time() - 200 * 86400
        os.utime(cache._path_for("aa"), (old, old))

        _, out = self._run(capsys, prune=90)
        assert "1 cached page(s)" in out
        assert cache.get("aa") is None

    def test_a_bare_prune_uses_the_configured_age(self, cache, capsys):
        _, out = self._run(capsys, prune=-1, json=True)
        assert json.loads(out)["max_age_days"] == cache.max_age_days

    def test_reading_the_cache_does_not_create_it(self, cache, capsys):
        self._run(capsys)
        assert not cache.root.exists()
