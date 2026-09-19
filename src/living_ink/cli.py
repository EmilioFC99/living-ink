"""Unified command-line interface for Living Ink.

Provides an extensible Command Pattern architecture for CLI execution.

Commands:
    living-ink               Run sync (or setup if unconfigured)
    living-ink sync          Sync notes from reMarkable to Obsidian/Apple Notes
    living-ink watch         Sync repeatedly on a timer until interrupted
    living-ink setup         Launch interactive configuration walkthrough
    living-ink status        Display connection, vault, and sync service status
    living-ink state         Inspect, reset, or check the sync state database
    living-ink cache         Show, prune, or clear the transcription and render caches
"""

import argparse
import dataclasses
import json
import logging
import os
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Type

from living_ink.config import ConfigurationMissing, credentials, get_config_path
from living_ink.settings import SOURCE_ENV, SettingOrigin, Settings
from living_ink.transport import TransportUnavailable

if TYPE_CHECKING:  # pragma: no cover - annotation only; state is imported lazily
    from living_ink.state import SyncStatus

logger = logging.getLogger(__name__)


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
        # A transport is a choice, not a preference order: asking for both
        # says nothing about which one was meant, so argparse rejects the
        # pair rather than silently picking SSH and syncing from a source
        # the user may not have intended.
        transport = parser.add_mutually_exclusive_group()
        transport.add_argument(
            "--ssh", action="store_true", help="Force sync via USB SSH instead of Cloud"
        )
        transport.add_argument(
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
        parser.add_argument(
            "--prune",
            action="store_true",
            help="Delete notes whose notebook is gone from the tablet (reported, not deleted, by default)",
        )
        parser.add_argument(
            "--status",
            action="store_true",
            help="Show what a sync would do — compare the tablet against your notes — and exit",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="With --status, list every document instead of the first ten",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Print the run summary as JSON instead of a table",
        )

    def run(self, args: argparse.Namespace) -> int:
        """Run the notebook sync pipeline.

        Args:
            args: Parsed arguments for sync.

        Returns:
            0 on success, or exits with 1 on failure.
        """
        if getattr(args, "status", False):
            try:
                return self.show_status(args)
            except ConfigurationMissing as e:
                # Not routed through _handle_missing_config: that offers the
                # wizard and then retries the *sync*, which is not what someone
                # asking a read-only question wanted to set in motion.
                print(f"Configuration problem: {e}", file=sys.stderr)
                # The error's own hint, not a fixed line: a config that is
                # merely absent wants the wizard, one with an unusable value
                # wants an editor, and only the raiser knows which it is.
                print(f"Fix: {e.hint}", file=sys.stderr)
                return 1
            except TransportUnavailable as e:
                return self._report_unreachable(e)

        try:
            success = self.execute_sync(args)
        except ConfigurationMissing as e:
            return self._handle_missing_config(e, args)
        except TransportUnavailable as e:
            return self._report_unreachable(e)
        if not success:
            sys.exit(1)
        return 0

    def _report_unreachable(self, error: TransportUnavailable) -> int:
        """Report that there was no route to the tablet, without a traceback.

        An unplugged cable and an unconfigured Cloud are the two most ordinary
        states a tablet can be in, and the exception already carries the lines
        that say which one to fix. Printing it as a stack trace would suggest a
        bug in the tool and bury the instruction in frames the user cannot act
        on.

        Args:
            error: The failure reported by the transport layer.

        Returns:
            1, the same failure code an unsuccessful sync returns.
        """
        print(f"Cannot reach the reMarkable: {error}", file=sys.stderr)
        return 1

    def show_status(self, args: argparse.Namespace) -> int:
        """Report what a sync would do, without doing any of it.

        Lists the tablet over whichever transport the settings resolve to and
        joins that against the state database. Metadata only: no download, no
        render, no OCR, no API call.

        Args:
            args: Parsed arguments for sync; ``--ssh`` / ``--cloud``, ``--all``
                and ``--json`` are honoured.

        Returns:
            0 when the comparison succeeded, 1 when the tablet was unreachable.

        Raises:
            ConfigurationMissing: If configuration is absent or unusable.
        """
        rows, orphans, device = compare_with_device(args, root=self.root)

        if getattr(args, "json", False):
            payload = inventory_as_json(rows)
            payload["orphans"] = [row["id"] for row in orphans]
            payload["device"] = device.describe() if device else None
            print(json.dumps(payload, indent=2))
            return 0

        render_comparison(
            rows,
            orphans,
            device,
            show_all=getattr(args, "all", False),
        )
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
                # Deliberately broad. A daemon that exits on the first
                # unexpected error is a daemon that is not running when the
                # user needs it; the traceback goes to the log file instead.
                logger.warning("Sync cycle failed", exc_info=True)
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
    #: One-line device description, empty when no transport could see the
    #: tablet. Only USB SSH can; the Cloud serves documents, not hardware.
    device: str = ""

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

    cache_entries: int = 0
    cache_bytes: int = 0

    #: Stored credentials any other account on this machine can read. Almost
    #: always empty — this module writes 0600 — but a file restored from a
    #: backup or copied with ``cp`` arrives with whatever mode it had, and the
    #: user is the only one who can fix it.
    loose_credentials: list[str] = field(default_factory=list)

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
                "device": self.device,
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
                # The console renderer builds the target path from this
                # (see _render_console), so omitting it here made the two
                # renderers describe different things — the exact divergence
                # this dataclass was introduced to remove.
                "root_folder": self.obsidian_root_folder,
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
            "cache": {
                "entries": self.cache_entries,
                "size_bytes": self.cache_bytes,
            },
            "credentials": {"insecure": list(self.loose_credentials)},
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


def _describe_connected_device(host: str, port: int, user: str, *, live: bool = True) -> str:
    """Say which tablet this installation syncs with, for the status line.

    A live USB reading wins, and is written to the state store on the way past
    so a later Cloud-only ``status`` can still name the device. When USB is not
    there, the answer comes from that memory, and only then from the default
    profile.

    Args:
        host: SSH host the tablet answers on.
        port: SSH port.
        user: SSH user.
        live: Whether SSH looked reachable. False skips the probe entirely
            rather than spending its timeout on a cable that is not plugged in.

    Returns:
        A one-line description, or an empty string if nothing at all could be
        said. A health check that cannot name the model is still a useful
        health check, so every failure here degrades to silence rather than
        turning ``status`` itself into an error.
    """
    from living_ink.devices import resolve_device

    transport = None
    if live:
        from living_ink.ssh import create_ssh_client

        transport = create_ssh_client(host=host, user=user, port=port)

    try:
        from living_ink.pipeline import get_state_store

        store = get_state_store()
    except (OSError, RuntimeError) as e:
        logger.debug("No state store for the device memory: %s", e)
        store = None

    try:
        return resolve_device(transport, store).describe()
    except (RuntimeError, OSError) as e:
        logger.debug("Could not identify the device: %s", e, exc_info=True)
        return ""


def _status_ai_key(provider: str, ai_cfg: dict[str, Any], config_path: Path) -> str:
    """Find the API key ``status`` should verify the provider with.

    The stored credential is the real answer; a key left in ``config.yml`` by
    an older install is the fallback, so a health check run before the first
    sync — which is what migrates it — still reports the truth.

    Args:
        provider: The configured provider name.
        ai_cfg: The ``ai:`` section of the config.
        config_path: The config file, which locates the credentials beside it.

    Returns:
        The key, or an empty string when there is none to find.
    """
    try:
        stored = credentials.read_secret(credentials.ai_key_name(provider), config_path=config_path)
    except ValueError:
        stored = None
    return stored or str(ai_cfg.get("api_key", "") or "")


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
    except (OSError, yaml.YAMLError) as e:
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

    # Asked even when SSH is down: the memory of a past USB session is still
    # the best answer available, and saying nothing would hide it.
    report.device = _describe_connected_device(
        report.ssh_host,
        rm_cfg.get("ssh_port", 22),
        rm_cfg.get("ssh_user", "root"),
        live=report.ssh_ok,
    )

    # Not rm_cfg["device_token"] alone: registration stores the token in
    # the credentials directory and leaves the config key empty, so reading
    # only the config reported "Disconnected" for a setup that syncs perfectly
    # well.
    from living_ink.api import resolve_stored_token

    token = rm_cfg.get("device_token", "") or resolve_stored_token(config_path=config_path)
    if token:
        report.cloud_ok, report.cloud_msg = verify_remarkable_token(token)

    # AI provider
    ai_cfg = cfg.get("ai", {})
    report.ai_provider = ai_cfg.get("provider", "none")
    model = ai_cfg.get("model", "")
    report.ai_model = model or "default"
    report.ai_ok, report.ai_msg = verify_ai_provider(
        report.ai_provider, _status_ai_key(report.ai_provider, ai_cfg, config_path), model
    )

    report.loose_credentials = [
        str(path) for path in credentials.insecure_credentials(config_path=config_path)
    ]

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

    try:
        for cache in all_caches():
            entries, total = cache.stats()
            report.cache_entries += entries
            report.cache_bytes += total
    except OSError:
        logger.debug("Could not measure the caches", exc_info=True)

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


def count_by_status(inventory: list[dict[str, Any]]) -> dict["SyncStatus", int]:
    """Total the inventory by sync status.

    Args:
        inventory: Rows carrying a ``status``, as
            :meth:`~living_ink.state.StateStore.compare_with_listing` returns.

    Returns:
        Mapping of every :data:`~living_ink.state.SYNC_STATUSES` entry to its
        count, zeros included so callers can format without checking for
        missing keys. Iterating the registry rather than naming statuses is
        what lets a new status be one entry in ``state.py`` and nothing here.
    """
    from living_ink.state import SYNC_STATUSES

    counts = {status: 0 for status in SYNC_STATUSES}
    for row in inventory:
        if row["status"] in counts:
            counts[row["status"]] += 1
    return counts


def compare_with_device(args: argparse.Namespace, root: Optional[Path] = None):
    """List the tablet and judge it against what has been published.

    Metadata only. ``get_meta_items()`` is one listing call; the document type
    is taken from the state database or from the listing's own file index
    rather than probed per document, because a probe is a round trip each and
    a preview that costs as much as a sync defeats its own purpose.

    Args:
        args: Parsed sync arguments; ``--ssh`` / ``--cloud`` select the
            transport, otherwise the configured preference wins.
        root: Optional repository root, for locating the config.

    Returns:
        ``(rows, orphans, device)`` — the comparison rows, documents published
        but no longer on the tablet, and what the transport says it is talking
        to, or None when it cannot say.

    Raises:
        ConfigurationMissing: If configuration is absent or unusable.
    """
    from living_ink.api import get_rmapi
    from living_ink.pipeline import (
        get_default_config,
        get_default_destinations,
        get_notebook_path,
        get_state_store,
        get_val,
    )

    cfg_path = get_config_path(root)
    if cfg_path.exists():
        os.environ.setdefault("LIVING_INK_CONFIG_DIR", str(cfg_path.parent))

    settings = Settings.resolve(get_default_config())
    if getattr(args, "ssh", False):
        settings = dataclasses.replace(settings, preferred_connection="ssh")
    elif getattr(args, "cloud", False):
        settings = dataclasses.replace(settings, preferred_connection="cloud")

    client = get_rmapi(settings)

    try:
        device = client.get_device_info()
    except Exception:
        # Broad on purpose, and it covers UnsupportedOperation: the comparison
        # needs the device's listing, not its identity. Saying nothing about
        # which tablet beats refusing to answer the question asked.
        logger.debug("Transport could not identify the device", exc_info=True)
        device = None

    collection = client.get_meta_items()
    id_map = {get_val(item, "ID"): item for item in collection}

    listing = []
    for item in collection:
        if get_val(item, "Type") != "DocumentType":
            continue
        name = get_val(item, "VissibleName") or get_val(item, "VisibleName")
        if not name:
            continue
        folder = get_notebook_path(item, id_map)
        if folder.startswith("[TRASH]"):
            continue
        listing.append(
            {
                "id": get_val(item, "ID"),
                "name": name,
                "folder": folder or None,
                "doc_type": document_type_from_metadata(item),
                "version": version_of(item),
            }
        )

    names = [dest.state_key for dest in get_default_destinations()]
    rows, orphans = get_state_store().compare_with_listing(listing, names)
    return rows, orphans, device


def version_of(item: Any) -> str:
    """Return the version string a sync would compare against.

    Thin wrapper over :func:`living_ink.pipeline.document_version`, which is
    also what the run itself uses: a preview and the run it predicts must not
    disagree about whether a document changed.

    Args:
        item: A document from the transport's listing.

    Returns:
        The content hash, or the version number as a string, or ``"1"``.
    """
    from living_ink.pipeline import document_version

    return document_version(item)


def document_type_from_metadata(item: Any) -> Optional[str]:
    """Guess a document's type without asking the transport.

    Args:
        item: A document from the transport's listing.

    Returns:
        ``"pdf"``, ``"epub"``, ``"notebook"``, or None when the metadata does
        not say — in which case the state database's remembered type is used
        instead.
    """
    from living_ink.pipeline import get_document_type

    # No client: get_document_type falls back to the file index and the
    # filename, both of which are already in hand.
    return get_document_type(item) or None


def inventory_as_json(inventory: list[dict[str, Any]]) -> dict[str, Any]:
    """Render the inventory as JSON-safe data.

    A row's ``status`` is a :class:`~living_ink.state.SyncStatus`, which is not
    serialisable and whose ``label`` is free to be reworded. Both the rows and
    the counts are keyed by the stable ``key`` instead, so a script reading
    this output survives a rename in the UI.

    Args:
        inventory: Rows from :func:`collect_inventory`.

    Returns:
        A dict with ``documents`` and ``counts``, ready for :func:`json.dumps`.
    """
    documents = [{**row, "status": row["status"].key} for row in inventory]
    counts = {status.key: count for status, count in count_by_status(inventory).items()}
    return {"documents": documents, "counts": counts}


#: How many rows a page of the comparison shows.
PAGE_SIZE = 10

#: Width of the truncated document name column.
NAME_WIDTH = 15


def format_comparison_row(row: dict[str, Any]) -> str:
    """Render one document as `<id> <name>.<type> <status>`.

    Args:
        row: One entry from :meth:`~living_ink.state.StateStore.compare_with_listing`.

    Returns:
        The line to print, without a leading indent and without colour — the
        caller colours the status so that the columns stay aligned whether or
        not escape codes are in play.
    """
    doc_id = str(row.get("id") or "")[:8].ljust(8)
    name = str(row.get("name") or row.get("id") or "")
    # Truncated with an ellipsis rather than hard-cut, so a row that lost
    # characters admits it instead of quietly reading as a different note.
    if len(name) > NAME_WIDTH:
        name = name[: NAME_WIDTH - 1] + "…"
    doc_type = row.get("doc_type") or "notebook"
    return f"{doc_id}  {name.ljust(NAME_WIDTH)}.{doc_type.ljust(8)}  "


def render_comparison(
    rows: list[dict[str, Any]],
    orphans: list[dict[str, Any]],
    device: Any,
    *,
    show_all: bool,
) -> None:
    """Print the summary and as much of the list as was asked for.

    Args:
        rows: Comparison rows, in listing order.
        orphans: Documents published but no longer on the tablet.
        device: What the transport is talking to, or None.
        show_all: Whether to page through everything rather than show the
            first :data:`PAGE_SIZE` rows.
    """
    from living_ink.setup_wizard import bold, dim, green
    from living_ink.state import SYNC_STATUSES

    print()
    against = device.describe() if device else "your reMarkable"
    print(bold(f"Comparing {against} against your notes"))
    print()

    if not rows:
        print(dim("Nothing on the tablet to compare."))
        print()
        return

    counts = count_by_status(rows)
    for status in SYNC_STATUSES:
        count = counts[status]
        if not count:
            continue
        colour = tone_colour(status.tone)
        print(f"  {colour(str(count).rjust(4))}  {status.label}")
    if orphans:
        print(f"  {dim(str(len(orphans)).rjust(4))}  no longer on the tablet")
    print()

    # Outstanding work first: a list that opens with forty up-to-date notes
    # buries the three that need attention.
    ordered = sorted(rows, key=lambda row: SYNC_STATUSES.index(row["status"]))

    if not any(row["status"].needs_sync for row in rows):
        print(green("Everything is up to date."))
        print()
        if not show_all:
            return

    if show_all:
        _print_paged(ordered)
    else:
        for row in ordered[:PAGE_SIZE]:
            _print_comparison_row(row)
        remaining = len(ordered) - PAGE_SIZE
        if remaining > 0:
            print()
            print(dim(f"{PAGE_SIZE} of {len(ordered)} shown · {remaining} more — use --all"))
    print()


def _print_comparison_row(row: dict[str, Any]) -> None:
    """Print one row with its status coloured.

    Args:
        row: One comparison row.
    """
    status = row["status"]
    print(f"  {format_comparison_row(row)}{tone_colour(status.tone)(status.label)}")


def _print_paged(rows: list[dict[str, Any]]) -> None:
    """Print every row, pausing each page when someone is watching.

    Args:
        rows: Comparison rows, already ordered.
    """
    from living_ink.setup_wizard import dim

    interactive = sys.stdout.isatty() and sys.stdin.isatty()

    for start in range(0, len(rows), PAGE_SIZE):
        for row in rows[start : start + PAGE_SIZE]:
            _print_comparison_row(row)

        shown = min(start + PAGE_SIZE, len(rows))
        if not interactive or shown >= len(rows):
            continue

        print()
        try:
            answer = input(dim(f"  {shown} of {len(rows)} — Enter for more, q to stop: "))
        except (EOFError, KeyboardInterrupt):
            # Piped into `head`, or the user gave up. Neither is an error.
            print()
            return
        if answer.strip().lower().startswith("q"):
            return


def tone_colour(tone: str):
    """Map a status tone onto the colour helper that renders it.

    Statuses name a tone rather than carrying a colour function so that
    :mod:`living_ink.state` stays free of presentation imports. This is the
    one place that translation happens.

    Args:
        tone: ``"good"``, ``"warn"``, ``"bad"`` or anything else.

    Returns:
        A callable taking a string and returning it wrapped in escape codes.
        An unrecognised tone renders dim rather than raising — a new status
        should never be able to crash the renderer.
    """
    from living_ink.setup_wizard import dim, green, red, yellow

    return {"good": green, "warn": yellow, "bad": red}.get(tone, dim)


def state_db_path() -> Path:
    """Return where the state database lives, without creating it.

    Returns:
        Path to ``state.db`` inside the data directory. May not exist.
    """
    from living_ink import state
    from living_ink.pipeline import DATA_DIR

    return DATA_DIR / state.DB_FILENAME


def transcript_cache():
    """Return the transcription cache the configured settings describe.

    Built from the resolved settings rather than defaults so that ``cache``
    reports on the same cache a sync would use, including a disabled one.

    Returns:
        A :class:`living_ink.cache.TranscriptCache`, whose directory may not
        exist yet. Reading the cache must not create it.
    """
    from living_ink.cache import TranscriptCache
    from living_ink.pipeline import TRANSCRIPT_CACHE_DIR, get_default_config
    from living_ink.settings import Settings

    settings = Settings.resolve(get_default_config())
    return TranscriptCache(
        TRANSCRIPT_CACHE_DIR,
        enabled=settings.transcript_cache,
        max_age_days=settings.cache_max_age_days,
    )


def render_cache():
    """Return the render cache the configured settings describe.

    Returns:
        A :class:`living_ink.cache.RenderCache`, whose directory may not exist
        yet. Reading the cache must not create it.
    """
    from living_ink.cache import RenderCache
    from living_ink.pipeline import RENDER_CACHE_DIR, get_default_config
    from living_ink.settings import Settings

    settings = Settings.resolve(get_default_config())
    return RenderCache(
        RENDER_CACHE_DIR,
        enabled=settings.render_cache,
        max_age_days=settings.cache_max_age_days,
    )


def all_caches():
    """Return every cache ``living-ink cache`` reports on, in printing order.

    Returns:
        A list of :class:`living_ink.cache.FileCache` instances.
    """
    return [transcript_cache(), render_cache()]


class StateCommand(BaseCommand):
    """Inspect and repair the sync state database.

    Everything Living Ink remembers between runs lives in one SQLite file, and
    when it is wrong the symptom is indirect: a notebook that will not re-sync,
    or one that re-syncs every time. These are the three things worth doing to
    it by hand — look at it, forget one document, and check it is not corrupt.
    """

    name = "state"
    help = "Inspect, reset, or check the sync state database"
    description = (
        "Show what Living Ink remembers between runs. With no options, prints a "
        "summary of the state database."
    )

    @classmethod
    def register_args(cls, parser: argparse.ArgumentParser) -> None:
        """Register arguments for the state command.

        Args:
            parser: Subparser to attach arguments to.
        """
        action = parser.add_mutually_exclusive_group()
        action.add_argument(
            "--dump",
            action="store_true",
            help="Print every row in the database",
        )
        action.add_argument(
            "--forget",
            metavar="DOCUMENT",
            help="Forget a document by id, name, or 'Folder/Name' so it syncs again",
        )
        action.add_argument(
            "--repair",
            action="store_true",
            help="Check the database for damage and compact it",
        )
        parser.add_argument(
            "--destination",
            help="Limit --forget to one destination (e.g. ObsidianDestination)",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Output in JSON format",
        )

    def run(self, args: argparse.Namespace) -> int:
        """Inspect or modify the state database.

        Args:
            args: Parsed arguments for state.

        Returns:
            0 on success; 1 when the database is missing, the named document
            is unknown or ambiguous, or the integrity check fails.
        """
        path = state_db_path()
        if not path.exists():
            print(f"No state database yet ({path}). Run 'living-ink sync' first.")
            return 1

        from living_ink.pipeline import get_state_store

        store = get_state_store()
        as_json = getattr(args, "json", False)

        if getattr(args, "dump", False):
            return self._dump(store, as_json)
        if getattr(args, "forget", None):
            return self._forget(store, args.forget, getattr(args, "destination", None), as_json)
        if getattr(args, "repair", False):
            return self._repair(store, path, as_json)
        return self._summary(store, path, as_json)

    @staticmethod
    def _summary(store, path: Path, as_json: bool) -> int:
        """Print what the database holds and when it was last written.

        Args:
            store: Open state store.
            path: Database file.
            as_json: Whether to print JSON instead of a console summary.

        Returns:
            0.
        """
        counts = store.counts()
        last_run = store.last_run()
        payload = {
            "path": str(path),
            "size_bytes": path.stat().st_size,
            "schema_version": store._conn.execute("PRAGMA user_version").fetchone()[0],
            "counts": counts,
            "last_run": last_run,
        }

        if as_json:
            print(json.dumps(payload, indent=2))
            return 0

        from living_ink.setup_wizard import bold, cyan, dim

        print()
        print(bold(cyan("Sync state")))
        print(f"  {dim(str(path))}  ({payload['size_bytes'] / 1024:.1f} KB)")
        print(f"  schema {payload['schema_version']}")
        print()
        for table, count in counts.items():
            print(f"  {table.ljust(14)} {count}")
        if last_run:
            print()
            outcome = last_run["outcome"] or "unfinished"
            print(f"  last run       {last_run['started_at']} ({outcome})")
        print()
        return 0

    @staticmethod
    def _dump(store, as_json: bool) -> int:
        """Print every row, for when the summary is not enough.

        Args:
            store: Open state store.
            as_json: Whether to print JSON instead of a console listing.

        Returns:
            0.
        """
        data = store.dump()
        if as_json:
            print(json.dumps(data, indent=2))
            return 0

        for table, rows in data.items():
            print(f"\n[{table}] {len(rows)} row(s)")
            for row in rows:
                print("  " + "  ".join(f"{key}={value!r}" for key, value in row.items()))
        print()
        return 0

    @staticmethod
    def _forget(store, query: str, destination: Optional[str], as_json: bool) -> int:
        """Drop a document's publication records so the next sync redoes it.

        Refuses to guess when a name matches more than one document: picking
        one would silently re-OCR the wrong notebook, and the user can see the
        ids and say which they meant.

        Args:
            store: Open state store.
            query: Id, name, or ``Folder/Name``.
            destination: Limit to one destination, or None for all of them.
            as_json: Whether to print JSON instead of a console message.

        Returns:
            0 when one document was forgotten, 1 otherwise.
        """
        matches = store.find_documents(query)

        if not matches:
            print(f"No document matches {query!r}. Try 'living-ink sync --status --all'.")
            return 1
        if len(matches) > 1:
            print(f"{query!r} matches {len(matches)} documents. Use an id:")
            for row in matches:
                print(f"  {row['id']}  {row['name']}  ({row['folder'] or '—'})")
            return 1

        document = matches[0]
        removed = store.forget(document["id"], destination)

        if as_json:
            print(
                json.dumps(
                    {
                        "id": document["id"],
                        "name": document["name"],
                        "publications_removed": removed,
                    }
                )
            )
            return 0

        where = f" for {short_destination(destination)}" if destination else ""
        print(f"Forgot {document['name'] or document['id']}{where} ({removed} record(s) removed).")
        print("It will be transcribed and published again on the next sync.")
        return 0

    @staticmethod
    def _repair(store, path: Path, as_json: bool) -> int:
        """Check the database for damage and reclaim space.

        A damaged file is reported, never deleted. Rebuilding it costs a full
        re-OCR of every notebook, which is real money, so that decision stays
        with the user.

        Args:
            store: Open state store.
            path: Database file.
            as_json: Whether to print JSON instead of a console message.

        Returns:
            0 when the database is intact, 1 when it is not.
        """
        result = store.integrity_check()
        healthy = result == "ok"
        before = path.stat().st_size

        if healthy:
            store.vacuum()
        after = path.stat().st_size

        if as_json:
            print(
                json.dumps(
                    {
                        "integrity": result,
                        "healthy": healthy,
                        "bytes_before": before,
                        "bytes_after": after,
                    }
                )
            )
            return 0 if healthy else 1

        from living_ink.setup_wizard import green, red

        if healthy:
            print(f"Integrity: {green('ok')}")
            print(f"Compacted {before / 1024:.1f} KB -> {after / 1024:.1f} KB")
            return 0

        print(f"Integrity: {red('damaged')}")
        print(result)
        print()
        print(f"Move {path} aside and run 'living-ink sync' to rebuild it.")
        print("Everything will be transcribed again, which costs another full OCR pass.")
        return 1


class CacheCommand(BaseCommand):
    """Inspect and maintain the caches.

    Two of them: transcribed pages, which cost money, and rendered pages, which
    cost time. Together they are what make re-syncing an unchanged notebook
    free, so the only things worth doing by hand are seeing how big they have
    grown, dropping what nobody has used in months, and — when the output is
    wrong and a prompt edit is not the fix — throwing it all away.
    """

    name = "cache"
    help = "Show, prune, or clear the caches"
    description = "Show how much is cached. With no options, prints a summary."

    @classmethod
    def register_args(cls, parser: argparse.ArgumentParser) -> None:
        """Register arguments for the cache command.

        Args:
            parser: Subparser to attach arguments to.
        """
        action = parser.add_mutually_exclusive_group()
        action.add_argument(
            "--clear",
            action="store_true",
            help="Delete every cached page",
        )
        action.add_argument(
            "--prune",
            nargs="?",
            const=-1,
            type=int,
            metavar="DAYS",
            help="Delete entries unused for DAYS days (default: the configured age)",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Output in JSON format",
        )

    def run(self, args: argparse.Namespace) -> int:
        """Show the cache, or prune or clear it.

        Args:
            args: Parsed arguments for cache.

        Returns:
            0. An empty cache is a normal state, not an error.
        """
        caches = all_caches()
        as_json = getattr(args, "json", False)

        if getattr(args, "clear", False):
            removed = {c.noun: c.clear() for c in caches}
            return self._report_removal(removed, "cleared", as_json)

        days = getattr(args, "prune", None)
        if days is not None:
            # A bare --prune means "the configured age", which each cache
            # carries for itself.
            removed = {c.noun: c.prune(None if days < 0 else days) for c in caches}
            limit = days if days >= 0 else caches[0].max_age_days
            return self._report_removal(removed, "pruned", as_json, days=limit)

        return self._summary(caches, as_json)

    @staticmethod
    def _summary(caches, as_json: bool) -> int:
        """Print what each cache is holding.

        Args:
            caches: The caches to report on.
            as_json: Whether to print JSON instead of a console summary.

        Returns:
            0.
        """
        from living_ink.cache import format_size

        payload = {}
        for cache in caches:
            entries, total = cache.stats()
            payload[cache.noun] = {
                "path": str(cache.root),
                "enabled": cache.enabled,
                "entries": entries,
                "size_bytes": total,
                "max_age_days": cache.max_age_days,
            }

        if as_json:
            print(json.dumps(payload, indent=2))
            return 0

        from living_ink.setup_wizard import bold, cyan, dim

        print()
        print(bold(cyan("Caches")))
        for cache in caches:
            entry = payload[cache.noun]
            print(f"  {cache.noun}s")
            print(f"    {dim(entry['path'])}")
            if not entry["enabled"]:
                print("    disabled — every page is done afresh")
            print(f"    {entry['entries']} page(s), {format_size(entry['size_bytes'])}")
            print(f"    pruned after {entry['max_age_days']} day(s) unused")
        print()
        return 0

    @staticmethod
    def _report_removal(removed: dict, verb: str, as_json: bool, days: int = 0) -> int:
        """Report how many entries an operation removed from each cache.

        Args:
            removed: How many entries went, keyed by what the cache calls one.
            verb: Past-tense description of what happened.
            as_json: Whether to print JSON instead of a console message.
            days: The age limit used, for a prune.

        Returns:
            0.
        """
        if as_json:
            print(json.dumps({"action": verb, "removed": removed, "max_age_days": days}))
            return 0

        for noun, count in removed.items():
            print(f"{verb.capitalize()} {count} cached {noun}(s).")
        if removed.get("transcribed page"):
            print("Those pages will be transcribed again, and paid for, on the next sync.")
        return 0


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

        # Only USB SSH can see the hardware, so this line is absent on a
        # Cloud-only setup rather than guessing at a model.
        if report.device:
            print(f"Device:        {report.device}")

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

        # No document tally here on purpose. This command reports the setup;
        # what is and is not synced is a live question about the tablet, and
        # `living-ink sync --status` is the one that goes and asks it.
        print(f"Documents:     {dim('→ Run: living-ink sync --status')}")

        # Caches — how much of the next sync is already paid for.
        if report.cache_entries:
            from living_ink.cache import format_size

            print(
                f"Cache:         {report.cache_entries} page(s), {format_size(report.cache_bytes)}"
            )

        # Named one by one rather than counted: the fix is a chmod on a
        # specific file, so a count would just send the user looking for them.
        for path in report.loose_credentials:
            print(f"Credentials:   {yellow('Readable by other accounts')} ({path})")
            print(f"               {dim(f'→ Run: chmod 600 {path}')}")

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
        json_output=getattr(args, "json", False),
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
        StateCommand,
        CacheCommand,
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
    try:
        code = cli.run(argv)
    except KeyboardInterrupt:
        # Ctrl+C is how a user ends a long sync. A traceback would suggest
        # something broke; 130 is what a shell expects from SIGINT.
        sys.exit(130)
    if isinstance(code, int) and code != 0:
        sys.exit(code)
    return code or 0


if __name__ == "__main__":
    main()
