"""Unified command-line interface for Living Ink.

Provides an extensible Command Pattern architecture for CLI execution.

Commands:
    living-ink               Run sync (or setup if unconfigured)
    living-ink sync          Sync notes from reMarkable to Obsidian/Apple Notes
    living-ink watch         Sync repeatedly on a timer until interrupted
    living-ink setup         Launch interactive configuration walkthrough
    living-ink status        Display connection, vault, and sync service status
    living-ink list          Show which documents are synced, pending, or failing
"""

import argparse
import json
import os
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Type

from living_ink.config import ConfigurationMissing, get_config_path
from living_ink.settings import SOURCE_ENV, SettingOrigin, Settings


class BaseCommand(ABC):
    """Abstract base class for all Living Ink CLI commands.

    Subclasses encapsulate argument definition (`register_args`) and execution
    logic (`run`) for a specific CLI subcommand.

    Attributes:
        name: Subcommand name used on the command line.
        help: Short one-line summary for CLI help listings.
        description: Extended description for command-specific help.
        root: Optional repository root path.
    """

    name: str = ""
    help: str = ""
    description: Optional[str] = None

    def __init__(self, root: Optional[Path] = None) -> None:
        """Initialize command with an optional project root path.

        Args:
            root: Root path of the project or repository.
        """
        self.root = root

    @classmethod
    @abstractmethod
    def register_args(cls, parser: argparse.ArgumentParser) -> None:
        """Register command-specific CLI flags and options.

        Args:
            parser: Subparser to attach arguments to.
        """
        pass

    @abstractmethod
    def run(self, args: argparse.Namespace) -> int:
        """Execute the command logic.

        Args:
            args: Parsed command-line arguments namespace.

        Returns:
            Exit code (0 for success, non-zero for error).
        """
        pass


class SyncCommand(BaseCommand):
    """Execute the reMarkable notebook sync pipeline.

    Attributes:
        offer_setup_on_missing_config: Whether an unusable config should prompt
            the user to run the wizard. Set to False when the wizard is already
            what invoked this command, to avoid bouncing between the two.
    """

    name = "sync"
    help = "Run the sync pipeline"
    description = "Sync notes and documents from reMarkable to Obsidian/Apple Notes."

    offer_setup_on_missing_config = True

    @classmethod
    def register_args(cls, parser: argparse.ArgumentParser) -> None:
        """Register arguments for the sync pipeline.

        Args:
            parser: Subparser to attach arguments to.
        """
        parser.add_argument(
            "--notebook",
            help="Sync a specific notebook by name, folder path (e.g. 'Work/Notes'), or document ID",
        )
        parser.add_argument("--limit", type=int, default=0, help="Max notebooks to process")
        parser.add_argument("--folder", help="Apple Notes folder override")
        parser.add_argument(
            "--ssh", action="store_true", help="Force sync via USB SSH instead of Cloud"
        )
        parser.add_argument(
            "--cloud", action="store_true", help="Force sync via reMarkable Cloud instead of SSH"
        )
        parser.add_argument(
            "--sync-pdfs", action="store_true", help="Sync PDF documents and annotations"
        )
        parser.add_argument(
            "--sync-epubs", action="store_true", help="Sync EPUB ebooks and annotations"
        )
        parser.add_argument(
            "--all-types",
            action="store_true",
            help="Sync all document types (notebooks, PDFs, and EPUBs)",
        )
        parser.add_argument(
            "--keep-temp",
            action="store_true",
            help="Preserve temporary rendered images, OCR transcripts, and downloaded documents after sync",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Transcribe as usual but publish nothing; prints where each transcript was written",
        )

    def run(self, args: argparse.Namespace) -> int:
        """Run the notebook sync pipeline.

        Args:
            args: Parsed arguments for sync.

        Returns:
            0 on success, or exits with 1 on failure.
        """
        try:
            success = self.execute_sync(args)
        except ConfigurationMissing as e:
            return self._handle_missing_config(e, args)
        if not success:
            sys.exit(1)
        return 0

    def execute_sync(self, args: argparse.Namespace) -> bool:
        """Run one sync and report whether it worked.

        Separate from :meth:`run` because turning a failure into a process exit
        is a decision only the one-shot command gets to make; a caller that
        syncs repeatedly — :class:`WatchCommand` — needs the verdict, not a
        dead process.

        Args:
            args: Parsed arguments for sync.

        Returns:
            True if the pipeline reported success.

        Raises:
            ConfigurationMissing: If configuration is absent or unusable.
        """
        if self.root and str(self.root) not in sys.path:
            sys.path.insert(0, str(self.root))

        cfg_path = get_config_path(self.root)
        if cfg_path.exists():
            os.environ.setdefault("LIVING_INK_CONFIG_DIR", str(cfg_path.parent))

        from living_ink.pipeline import SyncOptions, SyncPipeline

        pipeline = SyncPipeline(
            options=SyncOptions.from_args(args),
            config_path=cfg_path if cfg_path.exists() else None,
        )
        return pipeline.run()

    def _handle_missing_config(self, error: "ConfigurationMissing", args) -> int:
        """Report a configuration problem and, if interactive, offer the wizard.

        The pipeline only reports that configuration is unusable; whether to
        interrupt the user and walk them through setup is a front-end decision,
        so it is made here.

        Args:
            error: The configuration problem the pipeline reported.
            args: Parsed arguments, reused if the sync is retried after setup.

        Returns:
            0 if setup ran and the retried sync succeeded, 1 otherwise.
        """
        print("\n" + "=" * 60)
        print("CONFIGURATION ERROR")
        print("=" * 60)
        print(str(error))
        print("-" * 60)
        print(error.hint)
        print("=" * 60 + "\n")

        if not self.offer_setup_on_missing_config or not sys.stdin.isatty():
            return 1

        try:
            choice = input("Would you like to run the interactive setup wizard now? [Y/n]: ")
        except (KeyboardInterrupt, EOFError):
            return 1

        if choice.strip().lower() not in ("", "y", "yes"):
            return 1

        return SetupCommand(root=self.root).run(args)


class WatchCommand(BaseCommand):
    """Sync on a fixed interval until interrupted."""

    name = "watch"
    help = "Sync repeatedly on a timer"
    description = (
        "Run the sync pipeline every --interval seconds until interrupted. "
        "Accepts every sync option, so a watch can be scoped the same way a single sync can."
    )

    #: Half an hour is frequent enough that notes land while they are still on
    #: the user's mind, and rare enough to stay well inside free API tiers.
    DEFAULT_INTERVAL = 1800

    #: Below this the tablet is polled faster than a sync typically finishes.
    MIN_INTERVAL = 30

    @classmethod
    def register_args(cls, parser: argparse.ArgumentParser) -> None:
        """Register the watch interval on top of every sync option.

        Args:
            parser: Subparser to attach arguments to.
        """
        SyncCommand.register_args(parser)
        parser.add_argument(
            "--interval",
            type=int,
            default=cls.DEFAULT_INTERVAL,
            help=f"Seconds between syncs (default: {cls.DEFAULT_INTERVAL}, minimum: {cls.MIN_INTERVAL})",
        )

    def run(self, args: argparse.Namespace) -> int:
        """Sync in a loop until the user interrupts it.

        A sync that fails does not end the watch: an unplugged tablet, a closed
        laptop lid, or a rate-limited provider are the ordinary conditions this
        command exists to ride out. Only a configuration problem stops it,
        because that one will still be there on the next tick.

        Args:
            args: Parsed arguments — every sync option, plus ``--interval``.

        Returns:
            0 when interrupted, 1 if configuration is missing or unusable.
        """
        interval = max(self.MIN_INTERVAL, getattr(args, "interval", self.DEFAULT_INTERVAL))
        syncer = SyncCommand(root=self.root)
        # A watch is unattended by definition, so never stop to offer the wizard.
        syncer.offer_setup_on_missing_config = False

        print(f"Watching reMarkable — syncing every {interval}s. Press Ctrl+C to stop.")
        while True:
            try:
                if syncer.execute_sync(args):
                    print("Sync complete.")
                else:
                    print("Sync finished with errors; retrying next cycle.")
            except ConfigurationMissing as e:
                print(f"\nConfiguration error: {e}")
                print(e.hint)
                return 1
            except KeyboardInterrupt:
                return self._stopped()
            except Exception as e:
                print(f"Sync failed: {e}; retrying next cycle.")

            try:
                print(f"Next sync in {interval}s.")
                time.sleep(interval)
            except KeyboardInterrupt:
                return self._stopped()

    @staticmethod
    def _stopped() -> int:
        """Report a clean shutdown after Ctrl+C.

        Returns:
            0 — an interrupted watch is the intended way to end one, not a
            failure, so systemd and launchd should not treat it as a crash.
        """
        print("\nStopped watching.")
        return 0


class SetupCommand(BaseCommand):
    """Launch the interactive onboarding setup wizard."""

    name = "setup"
    help = "Launch the interactive setup wizard"
    description = "Configure reMarkable connection, AI provider, and note destinations."

    @classmethod
    def register_args(cls, parser: argparse.ArgumentParser) -> None:
        """Register arguments for the setup wizard.

        Args:
            parser: Subparser to attach arguments to.
        """
        pass

    def run(self, args: argparse.Namespace) -> int:
        """Run the interactive setup wizard, then optionally the first sync.

        Args:
            args: Parsed arguments for setup.

        Returns:
            0 on completion, or the sync's exit code if the user asked to sync.
        """
        from living_ink.setup_wizard import run_wizard

        result = run_wizard(repo_dir=self.root)
        if result.run_sync_requested:
            print("\nStarting sync pipeline...\n")
            sync = SyncCommand(root=self.root)
            # Config was just written; if it is still unusable, reporting the
            # problem beats looping back into the wizard that produced it.
            sync.offer_setup_on_missing_config = False
            return sync.run(args)
        return 0


@dataclass
class StatusReport:
    """A snapshot of the system's health, independent of how it is displayed.

    ``living-ink status`` has two renderers — a styled console view and
    ``--json`` — and they used to probe the tablet, the AI provider and the
    LaunchAgent separately, which meant the two could disagree and a new check
    had to be written twice. This dataclass is the single collected result;
    :func:`collect_status` fills it and the renderers only format it.

    Fields are deliberately flat rather than nested, because the nesting the
    JSON output needs is a presentation detail and lives in :meth:`to_dict`.
    """

    config_path: Path
    config_found: bool = False
    config_error: Optional[str] = None

    preferred: str = "cloud"
    ssh_host: str = "10.11.99.1"
    ssh_ok: bool = False
    ssh_msg: str = ""
    cloud_ok: bool = False
    cloud_msg: str = ""

    ai_provider: str = "none"
    ai_model: str = "default"
    ai_ok: bool = False
    ai_msg: str = ""

    obsidian_enabled: bool = False
    obsidian_vault: str = ""
    obsidian_root_folder: str = ""
    obsidian_valid: bool = False

    apple_notes_enabled: bool = False
    apple_notes_folder: str = "Living Ink"

    auto_sync_installed: bool = False
    auto_sync_active: bool = False

    documents_known: bool = False
    documents_synced: int = 0
    documents_pending: int = 0
    documents_failing: int = 0

    settings: list[SettingOrigin] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        """Whether a config was found and parsed; drives the process exit code."""
        return self.config_found and self.config_error is None

    def to_dict(self) -> dict[str, Any]:
        """Render the report in the documented ``--json`` shape.

        The key names and nesting are a stable contract for anyone scripting
        against ``living-ink status --json``; change them only deliberately.

        Returns:
            A JSON-serialisable dict. When the config is missing or unparsable,
            every section other than ``config`` is an empty object, matching
            the behaviour scripts already rely on.
        """
        empty: dict[str, Any] = {
            "remarkable": {},
            "ai": {},
            "obsidian": {},
            "apple_notes": {},
            "auto_sync": {},
            "documents": {},
            "settings": [],
        }

        if not self.config_found:
            return {"config": {"found": False, "path": None}, **empty}

        config: dict[str, Any] = {"found": True, "path": str(self.config_path)}
        if self.config_error:
            config["error"] = self.config_error
            return {"config": config, **empty}

        return {
            "config": config,
            "remarkable": {
                "preferred": self.preferred,
                "ssh": {"connected": self.ssh_ok, "message": self.ssh_msg},
                "cloud": {"connected": self.cloud_ok, "message": self.cloud_msg},
            },
            "ai": {
                "provider": self.ai_provider,
                "model": self.ai_model,
                "valid": self.ai_ok,
                "message": self.ai_msg,
            },
            "obsidian": {
                "enabled": self.obsidian_enabled,
                "vault_path": self.obsidian_vault,
                "valid": self.obsidian_valid,
            },
            "apple_notes": {
                "enabled": self.apple_notes_enabled,
                "folder": self.apple_notes_folder,
            },
            "auto_sync": {
                "installed": self.auto_sync_installed,
                "active": self.auto_sync_active,
            },
            "documents": {
                "known": self.documents_known,
                "synced": self.documents_synced,
                "pending": self.documents_pending,
                "failing": self.documents_failing,
            },
            "settings": [
                {
                    "name": origin.name,
                    "value": origin.display(),
                    "source": origin.source,
                    "env_var": origin.env_var,
                }
                for origin in self.settings
            ],
        }


def collect_status(config_path: Path) -> StatusReport:
    """Probe every subsystem once and return the result.

    Network and subprocess work happens here and nowhere else, so both
    renderers are guaranteed to describe the same moment in time.

    Args:
        config_path: Config file to read. Need not exist.

    Returns:
        A populated StatusReport. Probing stops early — leaving the remaining
        fields at their defaults — if the config is missing or unparsable.
    """
    import yaml

    from living_ink.setup_wizard import (
        LAUNCH_AGENT_PLIST,
        verify_ai_provider,
        verify_remarkable_ssh,
        verify_remarkable_token,
    )

    report = StatusReport(config_path=config_path)
    if not config_path.exists():
        return report

    report.config_found = True
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as e:
        report.config_error = str(e)
        return report

    # reMarkable transport. Both are probed when configured, because the
    # non-preferred one is the fallback and its health is worth reporting.
    rm_cfg = cfg.get("remarkable", {})
    has_ssh = rm_cfg.get("use_ssh", False) or cfg.get("use_ssh", False)
    report.preferred = rm_cfg.get("preferred_connection", "").strip().lower() or (
        "ssh" if has_ssh else "cloud"
    )
    report.ssh_host = rm_cfg.get("ssh_host", "10.11.99.1")

    if has_ssh or report.preferred == "ssh":
        report.ssh_ok, report.ssh_msg = verify_remarkable_ssh(
            host=report.ssh_host, port=rm_cfg.get("ssh_port", 22)
        )

    token = rm_cfg.get("device_token", "")
    if token:
        report.cloud_ok, report.cloud_msg = verify_remarkable_token(token)

    # AI provider
    ai_cfg = cfg.get("ai", {})
    report.ai_provider = ai_cfg.get("provider", "none")
    model = ai_cfg.get("model", "")
    report.ai_model = model or "default"
    report.ai_ok, report.ai_msg = verify_ai_provider(
        report.ai_provider, ai_cfg.get("api_key", ""), model
    )

    # Obsidian
    obs_cfg = cfg.get("obsidian", {})
    report.obsidian_enabled = obs_cfg.get("enabled", False)
    vault = Path(obs_cfg.get("vault_path", ""))
    report.obsidian_vault = str(vault)
    report.obsidian_root_folder = obs_cfg.get("root_folder", "")
    report.obsidian_valid = bool(report.obsidian_enabled and vault.exists() and vault.is_dir())

    # Apple Notes
    an_cfg = cfg.get("apple_notes", {})
    report.apple_notes_enabled = an_cfg.get("enabled", False)
    report.apple_notes_folder = an_cfg.get("folder_name", "Living Ink")

    # Effective settings, resolved exactly as a sync would resolve them.
    report.settings = Settings.explain(cfg)

    # Background sync
    report.auto_sync_installed = LAUNCH_AGENT_PLIST.exists()
    if report.auto_sync_installed:
        import subprocess

        res = subprocess.run(
            ["launchctl", "list", "com.livingink.sync"],
            capture_output=True,
            text=True,
            check=False,
        )
        report.auto_sync_active = res.returncode == 0

    # Sync inventory. A status check must survive a missing or damaged state
    # database — the rest of the report is exactly what someone would be
    # reading to diagnose that.
    try:
        inventory = collect_inventory()
    except Exception:
        inventory = []
    if inventory:
        from living_ink.state import STATUS_FAILING, STATUS_PENDING, STATUS_SYNCED

        counts = count_by_status(inventory)
        report.documents_known = True
        report.documents_synced = counts[STATUS_SYNCED]
        report.documents_pending = counts[STATUS_PENDING]
        report.documents_failing = counts[STATUS_FAILING]

    return report


def short_destination(class_name: str) -> str:
    """Turn a destination class name into something worth printing.

    Args:
        class_name: e.g. ``AppleNotesDestination``.

    Returns:
        e.g. ``Apple Notes``.
    """
    import re

    trimmed = re.sub(r"Destination$", "", class_name)
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", trimmed) or class_name


def collect_inventory() -> list[dict[str, Any]]:
    """Describe every document the state database knows about.

    Args:
        None.

    Returns:
        The rows :meth:`living_ink.state.StateStore.sync_overview` returns,
        judged against the destinations the current config enables. Empty when
        nothing has ever been synced, which is also what a fresh install looks
        like.
    """
    from living_ink import state
    from living_ink.pipeline import DATA_DIR, get_default_destinations, get_state_store

    # Reading the inventory must not create it. Opening the store would make
    # an empty database, and a command that only reports has no business
    # leaving a file behind.
    if not (DATA_DIR / state.DB_FILENAME).exists():
        return []

    names = [type(dest).__name__ for dest in get_default_destinations()]
    return get_state_store().sync_overview(names)


def count_by_status(inventory: list[dict[str, Any]]) -> dict[str, int]:
    """Total the inventory by sync status.

    Args:
        inventory: Rows from :func:`collect_inventory`.

    Returns:
        Mapping of each of the three statuses to its count, zeros included so
        callers can format without checking for missing keys.
    """
    from living_ink.state import STATUS_FAILING, STATUS_PENDING, STATUS_SYNCED

    counts = {STATUS_SYNCED: 0, STATUS_PENDING: 0, STATUS_FAILING: 0}
    for row in inventory:
        if row["status"] in counts:
            counts[row["status"]] += 1
    return counts


class ListCommand(BaseCommand):
    """Show what is synced, what is waiting, and what is broken.

    Defaults to the exception view. On a healthy library the interesting
    answer is short — usually nothing — and printing forty untouched notebooks
    to say so buries it. ``--all`` asks for the full inventory.
    """

    name = "list"
    help = "Show which documents are synced, pending, or failing"
    description = (
        "List documents Living Ink knows about. Shows only pending and failing "
        "ones unless --all is given."
    )

    @classmethod
    def register_args(cls, parser: argparse.ArgumentParser) -> None:
        """Register arguments for the list command.

        Args:
            parser: Subparser to attach arguments to.
        """
        parser.add_argument(
            "--all",
            action="store_true",
            help="Include documents that are already up to date",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Output the inventory in JSON format",
        )

    def run(self, args: argparse.Namespace) -> int:
        """Print the inventory.

        Args:
            args: Parsed arguments for list.

        Returns:
            0 always. A pending document is a normal state of affairs, not an
            error, and a script that treats it as one would break every time
            somebody wrote a new page.
        """
        inventory = collect_inventory()

        if getattr(args, "json", False):
            print(
                json.dumps({"documents": inventory, "counts": count_by_status(inventory)}, indent=2)
            )
            return 0

        self._render_console(inventory, show_all=getattr(args, "all", False))
        return 0

    @staticmethod
    def _render_console(inventory: list[dict[str, Any]], *, show_all: bool) -> None:
        """Print the inventory grouped by status.

        Args:
            inventory: Rows from :func:`collect_inventory`.
            show_all: Whether to include the up-to-date documents.
        """
        from living_ink.setup_wizard import bold, dim, green, red, yellow
        from living_ink.state import STATUS_FAILING, STATUS_PENDING, STATUS_SYNCED

        if not inventory:
            print()
            print(dim("No documents on record yet. Run 'living-ink sync' first."))
            print()
            return

        groups = {
            STATUS_FAILING: ("Failing", red),
            STATUS_PENDING: ("Pending", yellow),
            STATUS_SYNCED: ("Synced", green),
        }
        wanted = list(groups) if show_all else [STATUS_FAILING, STATUS_PENDING]

        width = min(40, max(len(row["name"] or row["id"]) for row in inventory))
        printed = 0

        print()
        for status in wanted:
            rows = [row for row in inventory if row["status"] == status]
            if not rows:
                continue
            label, colour = groups[status]
            print(bold(colour(f"{label} ({len(rows)})")))
            for row in rows:
                print(f"  {ListCommand._format_row(row, width)}")
            print()
            printed += len(rows)

        counts = count_by_status(inventory)
        if not show_all:
            if printed == 0:
                print(green("Everything is up to date."))
            print(dim(f"{counts[STATUS_SYNCED]} synced (not shown; use --all)"))
            print()

    @staticmethod
    def _format_row(row: dict[str, Any], width: int) -> str:
        """Render one document as a single line.

        Args:
            row: One entry from :func:`collect_inventory`.
            width: Column width for the document name.

        Returns:
            The line to print, without its leading indent.
        """
        from living_ink.setup_wizard import dim

        name = (row["name"] or row["id"])[:width].ljust(width)
        folder = row.get("folder") or "—"

        if row.get("last_error"):
            detail = str(row["last_error"]).splitlines()[0][:60]
        elif row["pending"]:
            detail = "waiting for " + ", ".join(short_destination(d) for d in row["pending"])
        else:
            detail = ""

        return f"{name}  {dim(folder.ljust(20))}  {detail}".rstrip()


class StatusCommand(BaseCommand):
    """Display connection, vault, and sync service status."""

    name = "status"
    help = "Show system, tablet, and vault status"
    description = "Check and display system configuration, tablet connectivity, and vault targets."

    @classmethod
    def register_args(cls, parser: argparse.ArgumentParser) -> None:
        """Register arguments for the status command.

        Args:
            parser: Subparser to attach arguments to.
        """
        parser.add_argument(
            "--json",
            action="store_true",
            help="Output status details in JSON format",
        )

    def run(self, args: argparse.Namespace) -> int:
        """Display system and connection status.

        Args:
            args: Parsed arguments for status.

        Returns:
            0 on completion, 1 if configuration is missing or invalid.
        """
        report = collect_status(get_config_path(self.root))
        if getattr(args, "json", False) is True:
            print(json.dumps(report.to_dict(), indent=2))
        else:
            self._render_console(report)
        return 0 if report.usable else 1

    @staticmethod
    def _render_console(report: StatusReport) -> None:
        """Print a styled, human-readable summary of an already-collected report.

        Args:
            report: Snapshot produced by collect_status().
        """
        from living_ink.setup_wizard import bold, cyan, dim, green, red, yellow

        print()
        print(bold(cyan("============================================================")))
        print(bold(cyan("                 Living Ink Status Check                    ")))
        print(bold(cyan("============================================================")))
        print()

        if not report.config_found:
            print(f"Configuration: {red('Not found')}")
            print("Run 'living-ink setup' to configure.")
            return

        print(f"Configuration: {green('Found')} ({dim(str(report.config_path))})")
        if report.config_error:
            print(f"Configuration: {red(f'Syntax Error: {report.config_error}')}")
            return

        # reMarkable — report the preferred transport first, then whether the
        # other one is standing by, since that is what the user can act on.
        if report.preferred == "ssh":
            if report.ssh_ok:
                backup = f" {dim('(Cloud backup ready)')}" if report.cloud_ok else ""
                print(f"reMarkable:    {green('Connected')} (USB SSH — Preferred){backup}")
            elif report.cloud_ok:
                unauthorized = (
                    "Tablet reached" in report.ssh_msg or "unauthorized" in report.ssh_msg.lower()
                )
                if unauthorized:
                    print(
                        f"reMarkable:    {yellow('Connected')} (Cloud backup active — USB plugged in but SSH key unauthorized)"
                    )
                    print(
                        f"               {dim(f'→ Run: ssh-copy-id root@{report.ssh_host} to enable USB SSH')}"
                    )
                else:
                    print(
                        f"reMarkable:    {yellow('Connected')} (Cloud backup active — USB SSH unplugged)"
                    )
            else:
                print(f"reMarkable:    {red('Disconnected')} (USB SSH: {report.ssh_msg})")
        else:
            if report.cloud_ok:
                backup = f" {dim('(USB SSH backup ready)')}" if report.ssh_ok else ""
                print(f"reMarkable:    {green('Connected')} (Cloud — Preferred){backup}")
            elif report.ssh_ok:
                print(
                    f"reMarkable:    {yellow('Connected')} (USB SSH backup active — Cloud unavailable)"
                )
            else:
                print(f"reMarkable:    {red('Disconnected')} (Cloud: {report.cloud_msg})")

        # AI provider
        label = f"{report.ai_provider} ({report.ai_model})"
        if report.ai_ok:
            print(f"AI Provider:   {green(label)} — {report.ai_msg}")
        else:
            print(f"AI Provider:   {yellow(report.ai_provider)} — {report.ai_msg}")

        # Obsidian
        if report.obsidian_enabled:
            if report.obsidian_valid:
                vault = Path(report.obsidian_vault)
                target = (
                    vault / report.obsidian_root_folder if report.obsidian_root_folder else vault
                )
                print(f"Obsidian:      {green('Enabled')} -> {target}")
            else:
                print(f"Obsidian:      {red('Vault path not found')} ({report.obsidian_vault})")
        else:
            print(f"Obsidian:      {dim('Disabled')}")

        # Apple Notes
        if report.apple_notes_enabled:
            print(f"Apple Notes:   {green('Enabled')} (Folder: {report.apple_notes_folder})")
        else:
            print(f"Apple Notes:   {dim('Disabled')}")

        # Background sync
        if not report.auto_sync_installed:
            print(f"Auto-Sync:     {dim('Not installed (run living-ink setup to enable)')}")
        elif report.auto_sync_active:
            print(f"Auto-Sync:     {green('Active (runs hourly in background)')}")
        else:
            print(f"Auto-Sync:     {yellow('Installed but not currently loaded')}")

        # Documents — the one line that answers "did my notes make it?".
        if report.documents_known:
            parts = [green(f"{report.documents_synced} synced")]
            if report.documents_pending:
                parts.append(yellow(f"{report.documents_pending} pending"))
            if report.documents_failing:
                parts.append(red(f"{report.documents_failing} failing"))
            print(f"Documents:     {' · '.join(parts)}")
            if report.documents_pending or report.documents_failing:
                print(f"               {dim('→ Run: living-ink list')}")
        else:
            print(f"Documents:     {dim('None synced yet')}")

        StatusCommand._render_settings(report)
        print()

    @staticmethod
    def _render_settings(report: StatusReport) -> None:
        """Print the effective settings and the layer each one came from.

        A value can come from the config file, an environment variable, or a
        built-in default, and only the first of those is visible by reading
        ``config.yml``. Printing the winning layer next to each value turns
        "why is it doing that?" into something answerable without a debugger.

        Args:
            report: Snapshot produced by collect_status().
        """
        from living_ink.setup_wizard import bold, cyan, dim, yellow

        if not report.settings:
            return

        print()
        print(bold(cyan("Effective settings")))
        width = max(len(origin.name) for origin in report.settings)
        for origin in report.settings:
            # An environment override is the surprising case, so it is the one
            # that gets colour; config and defaults are expected and stay quiet.
            if origin.source == SOURCE_ENV:
                note = yellow(f"{origin.source} ({origin.env_var})")
            else:
                note = dim(origin.source)
            print(f"  {origin.name.ljust(width)}  {origin.display()}  {note}")


def configure_logging(args: argparse.Namespace) -> None:
    """Install the package log handlers for this invocation.

    Done once here rather than per command, so the modules that know most
    about a failure — providers, transports, the renderer — reach the log file
    no matter which subcommand is running.

    Args:
        args: Parsed arguments; ``--verbose`` and ``--quiet`` are read off it.
    """
    from living_ink import logs
    from living_ink.pipeline import LOG_PATH

    logs.configure(
        LOG_PATH,
        verbose=getattr(args, "verbose", False),
        quiet=getattr(args, "quiet", False),
    )


def add_verbosity_args(parser: argparse.ArgumentParser) -> None:
    """Add the ``--verbose`` / ``--quiet`` pair to a parser.

    Both default to ``argparse.SUPPRESS``: the same flags are declared on the
    top-level parser and on every subparser, and a real default on the
    subparser would overwrite a flag given before the subcommand name.

    Args:
        parser: Parser or subparser to extend.
    """
    group = parser.add_argument_group("output")
    group.add_argument(
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Show every log record, including internal detail, on stderr",
    )
    group.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Suppress progress output; the log file is still written in full",
    )


class LivingInkCLI:
    """Unified command-line interface orchestrator for Living Ink.

    Manages command registration, argument parsing, configuration path
    resolution, and subcommand dispatching.

    Attributes:
        root: Project root directory Path.
        commands: Dictionary mapping command names to BaseCommand classes.
    """

    DEFAULT_COMMANDS: list[Type[BaseCommand]] = [
        SyncCommand,
        WatchCommand,
        SetupCommand,
        StatusCommand,
        ListCommand,
    ]

    def __init__(
        self,
        root: Optional[Path] = None,
        commands: Optional[list[Type[BaseCommand]]] = None,
    ) -> None:
        """Initialize CLI with optional root path and command classes.

        Args:
            root: Project root path. Leave None to let ``get_config_path()``
                run its full XDG resolution; passing a root pins config lookup
                to that directory, which is mainly useful in tests. Use
                :func:`living_ink.config.find_repo_root` to discover one.
            commands: Optional list of BaseCommand subclasses to register.
        """
        self.root = root
        self.commands: dict[str, Type[BaseCommand]] = {}
        for cmd_cls in commands or self.DEFAULT_COMMANDS:
            self.register_command(cmd_cls)

    def register_command(self, cmd_cls: Type[BaseCommand]) -> None:
        """Register a new command subclass.

        Args:
            cmd_cls: BaseCommand subclass to register.

        Raises:
            TypeError: If cmd_cls does not subclass BaseCommand.
        """
        if not issubclass(cmd_cls, BaseCommand):
            raise TypeError(f"{cmd_cls} must subclass BaseCommand")
        self.commands[cmd_cls.name] = cmd_cls

    def build_parser(self) -> argparse.ArgumentParser:
        """Construct the CLI argument parser with all registered subcommands.

        Returns:
            Configured argparse.ArgumentParser instance.
        """
        from living_ink import __version__

        parser = argparse.ArgumentParser(
            prog="living-ink",
            description="Sync handwritten reMarkable notebooks to Obsidian and Apple Notes.",
        )
        parser.add_argument(
            "-v",
            "--version",
            action="version",
            version=f"%(prog)s {__version__}",
        )
        parser.add_argument(
            "-c",
            "--config",
            help="Path to custom config.yml file",
        )

        # Attached to the top-level parser *and* to every subparser, so both
        # `living-ink --verbose sync` and `living-ink sync --verbose` work;
        # people reach for the second form and argparse does not allow it
        # otherwise.
        add_verbosity_args(parser)

        subparsers = parser.add_subparsers(dest="command", help="Available commands")

        for cmd_name, cmd_cls in self.commands.items():
            subparser = subparsers.add_parser(
                cmd_name,
                help=cmd_cls.help,
                description=cmd_cls.description or cmd_cls.help,
            )
            add_verbosity_args(subparser)
            cmd_cls.register_args(subparser)

        return parser

    def dispatch(self, args: argparse.Namespace) -> int:
        """Dispatch parsed arguments to the appropriate command handler.

        Args:
            args: Parsed command-line arguments.

        Returns:
            Exit code integer.
        """
        if getattr(args, "config", None):
            os.environ["LIVING_INK_CONFIG"] = str(Path(args.config).resolve())

        configure_logging(args)

        if args.command is None:
            # Default behavior: if config exists, sync; otherwise setup
            config_file = get_config_path(self.root)
            cmd_cls = (
                self.commands.get("sync", SyncCommand)
                if config_file.exists()
                else self.commands.get("setup", SetupCommand)
            )
            return cmd_cls(root=self.root).run(args)

        cmd_cls = self.commands.get(args.command)
        if cmd_cls:
            return cmd_cls(root=self.root).run(args)

        return 1

    def run(self, argv: Optional[list[str]] = None) -> int:
        """Parse arguments and run the corresponding command.

        Args:
            argv: Optional list of CLI argument strings (defaults to sys.argv[1:]).

        Returns:
            Exit code integer.
        """
        parser = self.build_parser()
        args = parser.parse_args(argv)
        return self.dispatch(args)


def main(argv: Optional[list[str]] = None) -> int:
    """Main CLI entry point.

    Args:
        argv: Optional list of CLI arguments (defaults to sys.argv[1:]).

    Returns:
        Exit code integer.
    """
    cli = LivingInkCLI()
    code = cli.run(argv)
    if isinstance(code, int) and code != 0:
        sys.exit(code)
    return code or 0


if __name__ == "__main__":
    main()
