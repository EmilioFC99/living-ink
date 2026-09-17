"""Unified command-line interface for Living Ink.

Commands:
    living-ink               Run sync (or setup if unconfigured)
    living-ink sync          Sync notes from reMarkable to Obsidian/Apple Notes
    living-ink setup         Launch interactive configuration walkthrough
    living-ink status        Display connection, vault, and sync service status
"""

import argparse
import os
import sys
from pathlib import Path
from typing import Optional


def get_root() -> Optional[Path]:
    """Find the Living Ink root directory if running from a repo checkout."""
    # Check current working directory first (e.g. testing or invoked in project root)
    cwd = Path.cwd()
    if (
        (cwd / "pyproject.toml").exists()
        or (cwd / "config" / "config.yml").exists()
        or (cwd / "config.yml").exists()
    ):
        return cwd
    # Check parent hierarchy of this file
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return None


def get_config_path(root: Optional[Path] = None) -> Path:
    """Find the config.yml file, checking LIVING_INK_CONFIG_DIR first."""
    from living_ink.config import get_config_path as _get_config_path

    return _get_config_path(root)


def cmd_setup(args, root: Optional[Path] = None):
    """Run the interactive setup wizard."""
    from living_ink.setup_wizard import run_wizard

    run_wizard(repo_dir=root)


def cmd_sync(args, root: Optional[Path] = None):
    """Run the notebook sync pipeline."""
    # Ensure root is in sys.path if running from a repo checkout
    if root and str(root) not in sys.path:
        sys.path.insert(0, str(root))

    # Handle connection overrides
    if hasattr(args, "ssh") and args.ssh:
        os.environ["REMARKABLE_PREFERRED_CONNECTION"] = "ssh"
        os.environ["REMARKABLE_USE_SSH"] = "true"
    elif hasattr(args, "cloud") and args.cloud:
        os.environ["REMARKABLE_PREFERRED_CONNECTION"] = "cloud"
        os.environ["REMARKABLE_USE_SSH"] = "false"

    cfg_path = get_config_path(root)
    if cfg_path.exists():
        os.environ.setdefault("LIVING_INK_CONFIG_DIR", str(cfg_path.parent))

    try:
        from scripts.process_notebook import main as sync_main
    except ImportError:
        from living_ink.pipeline import main as sync_main

    # Forward any options to process_notebook
    sys.argv = [sys.argv[0]]
    if hasattr(args, "notebook") and args.notebook:
        sys.argv.extend(["--notebook", args.notebook])
    if hasattr(args, "limit") and args.limit:
        sys.argv.extend(["--limit", str(args.limit)])
    if hasattr(args, "folder") and args.folder:
        sys.argv.extend(["--folder", args.folder])
    if hasattr(args, "ssh") and args.ssh:
        sys.argv.extend(["--ssh"])
    if hasattr(args, "cloud") and args.cloud:
        sys.argv.extend(["--cloud"])
    if hasattr(args, "sync_pdfs") and args.sync_pdfs:
        sys.argv.extend(["--sync-pdfs"])
    if hasattr(args, "sync_epubs") and args.sync_epubs:
        sys.argv.extend(["--sync-epubs"])
    if hasattr(args, "all_types") and args.all_types:
        sys.argv.extend(["--all-types"])

    sync_main()


def cmd_status(args, root: Optional[Path] = None):
    """Display system and connection status."""
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
    config_file = get_config_path(root)

    if not config_file.exists():
        print(f"Configuration: {red('Not found')}")
        print("Run 'living-ink setup' to configure.")
        return

    print(f"Configuration: {green('Found')} ({dim(str(config_file))})")

    import yaml

    try:
        with open(config_file, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as e:
        print(f"Configuration: {red(f'Syntax Error: {e}')}")
        return

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
            # Check if USB is physically plugged in and reachable
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


def main():
    """Main CLI entry point."""
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
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # sync command
    sync_parser = subparsers.add_parser("sync", help="Run the sync pipeline")
    sync_parser.add_argument(
        "--notebook",
        help="Sync a specific notebook by name, folder path (e.g. 'Work/Notes'), or document ID",
    )
    sync_parser.add_argument("--limit", type=int, default=0, help="Max notebooks to process")
    sync_parser.add_argument("--folder", help="Apple Notes folder override")
    sync_parser.add_argument(
        "--ssh", action="store_true", help="Force sync via USB SSH instead of Cloud"
    )
    sync_parser.add_argument(
        "--cloud", action="store_true", help="Force sync via reMarkable Cloud instead of SSH"
    )
    sync_parser.add_argument(
        "--sync-pdfs", action="store_true", help="Sync PDF documents and annotations"
    )
    sync_parser.add_argument(
        "--sync-epubs", action="store_true", help="Sync EPUB ebooks and annotations"
    )
    sync_parser.add_argument(
        "--all-types",
        action="store_true",
        help="Sync all document types (notebooks, PDFs, and EPUBs)",
    )

    # setup command
    subparsers.add_parser("setup", help="Launch the interactive setup wizard")

    # status command
    subparsers.add_parser("status", help="Show system, tablet, and vault status")

    parser.add_argument(
        "-c",
        "--config",
        help="Path to custom config.yml file",
    )

    args = parser.parse_args()

    if args.config:
        os.environ["LIVING_INK_CONFIG"] = str(Path(args.config).resolve())

    if args.command is None:
        # Default behavior: if config exists, sync; otherwise setup
        config_file = get_config_path()

        if config_file.exists():
            cmd_sync(args)
        else:
            cmd_setup(args)
    elif args.command == "sync":
        cmd_sync(args)
    elif args.command == "setup":
        cmd_setup(args)
    elif args.command == "status":
        cmd_status(args)


if __name__ == "__main__":
    main()
