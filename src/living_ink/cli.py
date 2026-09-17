"""Unified command-line interface for Living Ink.

Provides an extensible Command Pattern architecture for CLI execution.

Commands:
    living-ink               Run sync (or setup if unconfigured)
    living-ink sync          Sync notes from reMarkable to Obsidian/Apple Notes
    living-ink setup         Launch interactive configuration walkthrough
    living-ink status        Display connection, vault, and sync service status
"""

import argparse
import json
import os
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional, Type

from living_ink.config import get_config_path


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
    """Execute the reMarkable notebook sync pipeline."""

    name = "sync"
    help = "Run the sync pipeline"
    description = "Sync notes and documents from reMarkable to Obsidian/Apple Notes."

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

    def run(self, args: argparse.Namespace) -> int:
        """Run the notebook sync pipeline.

        Args:
            args: Parsed arguments for sync.

        Returns:
            0 on success, or exits with 1 on failure.
        """
        if self.root and str(self.root) not in sys.path:
            sys.path.insert(0, str(self.root))

        cfg_path = get_config_path(self.root)
        if cfg_path.exists():
            os.environ.setdefault("LIVING_INK_CONFIG_DIR", str(cfg_path.parent))

        from living_ink.pipeline import SyncPipeline

        pipeline = SyncPipeline(
            config_path=cfg_path if cfg_path.exists() else None,
            notebook=getattr(args, "notebook", None),
            limit=getattr(args, "limit", None),
            folder=getattr(args, "folder", None),
            ssh=getattr(args, "ssh", False),
            cloud=getattr(args, "cloud", False),
            sync_pdfs=getattr(args, "sync_pdfs", False)
            if hasattr(args, "sync_pdfs") and args.sync_pdfs
            else None,
            sync_epubs=getattr(args, "sync_epubs", False)
            if hasattr(args, "sync_epubs") and args.sync_epubs
            else None,
            all_types=getattr(args, "all_types", False),
            keep_temp=getattr(args, "keep_temp", False),
        )
        success = pipeline.run()
        if not success:
            sys.exit(1)
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
        """Run the interactive setup wizard.

        Args:
            args: Parsed arguments for setup.

        Returns:
            0 on completion.
        """
        from living_ink.setup_wizard import run_wizard

        run_wizard(repo_dir=self.root)
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
        if getattr(args, "json", False) is True:
            return self._run_json()
        return self._run_console()

    def _run_console(self) -> int:
        """Render status in styled terminal format.

        Returns:
            0 on success, 1 on missing or invalid configuration.
        """
        from living_ink.setup_wizard import (
            LAUNCH_AGENT_PLIST,
            bold,
            cyan,
            dim,
            green,
            red,
            verify_ai_provider,
            verify_remarkable_token,
            yellow,
        )

        print()
        print(bold(cyan("============================================================")))
        print(bold(cyan("                 Living Ink Status Check                    ")))
        print(bold(cyan("============================================================")))
        print()

        # 1. Config file
        config_file = get_config_path(self.root)

        if not config_file.exists():
            print(f"Configuration: {red('Not found')}")
            print("Run 'living-ink setup' to configure.")
            return 1

        print(f"Configuration: {green('Found')} ({dim(str(config_file))})")

        import yaml

        try:
            with open(config_file, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        except Exception as e:
            print(f"Configuration: {red(f'Syntax Error: {e}')}")
            return 1

        # 2. reMarkable Tablet
        rm_cfg = cfg.get("remarkable", {})
        preferred = rm_cfg.get("preferred_connection", "").strip().lower()
        has_ssh = rm_cfg.get("use_ssh", False) or cfg.get("use_ssh", False)
        token = rm_cfg.get("device_token", "")

        if not preferred:
            preferred = "ssh" if has_ssh else "cloud"

        from living_ink.setup_wizard import verify_remarkable_ssh

        ssh_ok, ssh_msg = False, ""
        if has_ssh or preferred == "ssh":
            ssh_host = rm_cfg.get("ssh_host", "10.11.99.1")
            ssh_port = rm_cfg.get("ssh_port", 22)
            ssh_ok, ssh_msg = verify_remarkable_ssh(host=ssh_host, port=ssh_port)

        cloud_ok, cloud_msg = False, ""
        if token:
            cloud_ok, cloud_msg = verify_remarkable_token(token)

        # Format status output
        if preferred == "ssh":
            if ssh_ok:
                backup_note = f" {dim('(Cloud backup ready)')}" if cloud_ok else ""
                print(f"reMarkable:    {green('Connected')} (USB SSH — Preferred){backup_note}")
            elif cloud_ok:
                if "Tablet reached" in ssh_msg or "unauthorized" in ssh_msg.lower():
                    print(
                        f"reMarkable:    {yellow('Connected')} (Cloud backup active — USB plugged in but SSH key unauthorized)"
                    )
                    print(
                        f"               {dim(f'→ Run: ssh-copy-id root@{ssh_host} to enable USB SSH')}"
                    )
                else:
                    print(
                        f"reMarkable:    {yellow('Connected')} (Cloud backup active — USB SSH unplugged)"
                    )
            else:
                print(f"reMarkable:    {red('Disconnected')} (USB SSH: {ssh_msg})")
        else:  # preferred == "cloud"
            if cloud_ok:
                backup_note = f" {dim('(USB SSH backup ready)')}" if ssh_ok else ""
                print(f"reMarkable:    {green('Connected')} (Cloud — Preferred){backup_note}")
            elif ssh_ok:
                print(
                    f"reMarkable:    {yellow('Connected')} (USB SSH backup active — Cloud unavailable)"
                )
            else:
                print(f"reMarkable:    {red('Disconnected')} (Cloud: {cloud_msg})")

        # 3. AI Provider
        ai_cfg = cfg.get("ai", {})
        provider = ai_cfg.get("provider", "none")
        key = ai_cfg.get("api_key", "")
        model = ai_cfg.get("model", "")
        ok, msg = verify_ai_provider(provider, key, model)
        model_label = model if model else "default"
        if ok:
            print(f"AI Provider:   {green(f'{provider} ({model_label})')} — {msg}")
        else:
            print(f"AI Provider:   {yellow(f'{provider}')} — {msg}")

        # 4. Obsidian
        obs_cfg = cfg.get("obsidian", {})
        if obs_cfg.get("enabled", False):
            vp = Path(obs_cfg.get("vault_path", ""))
            root_f = obs_cfg.get("root_folder", "")
            if vp.exists() and vp.is_dir():
                target = vp / root_f if root_f else vp
                print(f"Obsidian:      {green('Enabled')} -> {target}")
            else:
                print(f"Obsidian:      {red('Vault path not found')} ({vp})")
        else:
            print(f"Obsidian:      {dim('Disabled')}")

        # 5. Apple Notes
        an_cfg = cfg.get("apple_notes", {})
        if an_cfg.get("enabled", False):
            print(
                f"Apple Notes:   {green('Enabled')} (Folder: {an_cfg.get('folder_name', 'Living Ink')})"
            )
        else:
            print(f"Apple Notes:   {dim('Disabled')}")

        # 6. LaunchAgent background sync
        if LAUNCH_AGENT_PLIST.exists():
            import subprocess

            res = subprocess.run(
                ["launchctl", "list", "com.livingink.sync"],
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode == 0:
                print(f"Auto-Sync:     {green('Active (runs hourly in background)')}")
            else:
                print(f"Auto-Sync:     {yellow('Installed but not currently loaded')}")
        else:
            print(f"Auto-Sync:     {dim('Not installed (run living-ink setup to enable)')}")

        print()
        return 0

    def _run_json(self) -> int:
        """Collect and output status as structured JSON.

        Returns:
            0 on success, 1 on missing or invalid configuration.
        """
        status_data: dict[str, Any] = {
            "config": {"found": False, "path": None},
            "remarkable": {},
            "ai": {},
            "obsidian": {},
            "apple_notes": {},
            "auto_sync": {},
        }
        config_file = get_config_path(self.root)
        if not config_file.exists():
            print(json.dumps(status_data, indent=2))
            return 1

        status_data["config"] = {"found": True, "path": str(config_file)}
        import yaml

        try:
            with open(config_file, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        except Exception as e:
            status_data["config"]["error"] = str(e)
            print(json.dumps(status_data, indent=2))
            return 1

        # reMarkable Tablet
        rm_cfg = cfg.get("remarkable", {})
        preferred = rm_cfg.get("preferred_connection", "").strip().lower()
        has_ssh = rm_cfg.get("use_ssh", False) or cfg.get("use_ssh", False)
        token = rm_cfg.get("device_token", "")
        if not preferred:
            preferred = "ssh" if has_ssh else "cloud"

        from living_ink.setup_wizard import verify_remarkable_ssh, verify_remarkable_token

        ssh_ok, ssh_msg = False, ""
        if has_ssh or preferred == "ssh":
            ssh_host = rm_cfg.get("ssh_host", "10.11.99.1")
            ssh_port = rm_cfg.get("ssh_port", 22)
            ssh_ok, ssh_msg = verify_remarkable_ssh(host=ssh_host, port=ssh_port)

        cloud_ok, cloud_msg = False, ""
        if token:
            cloud_ok, cloud_msg = verify_remarkable_token(token)

        status_data["remarkable"] = {
            "preferred": preferred,
            "ssh": {"connected": ssh_ok, "message": ssh_msg},
            "cloud": {"connected": cloud_ok, "message": cloud_msg},
        }

        # AI Provider
        from living_ink.setup_wizard import verify_ai_provider

        ai_cfg = cfg.get("ai", {})
        provider = ai_cfg.get("provider", "none")
        key = ai_cfg.get("api_key", "")
        model = ai_cfg.get("model", "")
        ok, msg = verify_ai_provider(provider, key, model)
        status_data["ai"] = {
            "provider": provider,
            "model": model or "default",
            "valid": ok,
            "message": msg,
        }

        # Obsidian
        obs_cfg = cfg.get("obsidian", {})
        obs_enabled = obs_cfg.get("enabled", False)
        vp = Path(obs_cfg.get("vault_path", ""))
        status_data["obsidian"] = {
            "enabled": obs_enabled,
            "vault_path": str(vp),
            "valid": vp.exists() and vp.is_dir() if obs_enabled else False,
        }

        # Apple Notes
        an_cfg = cfg.get("apple_notes", {})
        status_data["apple_notes"] = {
            "enabled": an_cfg.get("enabled", False),
            "folder": an_cfg.get("folder_name", "Living Ink"),
        }

        # Auto Sync
        from living_ink.setup_wizard import LAUNCH_AGENT_PLIST

        if LAUNCH_AGENT_PLIST.exists():
            import subprocess

            res = subprocess.run(
                ["launchctl", "list", "com.livingink.sync"],
                capture_output=True,
                text=True,
                check=False,
            )
            status_data["auto_sync"] = {
                "installed": True,
                "active": res.returncode == 0,
            }
        else:
            status_data["auto_sync"] = {"installed": False, "active": False}

        print(json.dumps(status_data, indent=2))
        return 0


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
        SetupCommand,
        StatusCommand,
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

        subparsers = parser.add_subparsers(dest="command", help="Available commands")

        for cmd_name, cmd_cls in self.commands.items():
            subparser = subparsers.add_parser(
                cmd_name,
                help=cmd_cls.help,
                description=cmd_cls.description or cmd_cls.help,
            )
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
