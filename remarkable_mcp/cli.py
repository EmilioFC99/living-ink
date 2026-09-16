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


# Find project root or current working directory
def get_root() -> Path:
    """Find the Living Ink root directory."""
    pkg_dir = Path(__file__).parent.parent.resolve()
    if (pkg_dir / "pyproject.toml").exists():
        return pkg_dir
    # Check current working directory
    cwd = Path.cwd()
    if (cwd / "config" / "config.yml").exists() or (cwd / "config.yml").exists():
        return cwd
    return pkg_dir


def get_config_path(root: Path) -> Path:
    """Find the config.yml file, checking LIVING_INK_CONFIG_DIR first."""
    env_dir = os.environ.get("LIVING_INK_CONFIG_DIR")
    if env_dir:
        return Path(env_dir) / "config.yml"
    cfg = root / "config" / "config.yml"
    if cfg.exists():
        return cfg
    if (root / "config.yml").exists():
        return root / "config.yml"
    return cfg


def cmd_setup(args, root: Path):
    """Run the interactive setup wizard."""
    from remarkable_mcp.setup_wizard import run_wizard

    run_wizard(repo_dir=root)


def cmd_sync(args, root: Path):
    """Run the notebook sync pipeline."""
    # Ensure root is in sys.path
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    # Handle connection overrides
    if hasattr(args, "ssh") and args.ssh:
        os.environ["REMARKABLE_PREFERRED_CONNECTION"] = "ssh"
        os.environ["REMARKABLE_USE_SSH"] = "true"
    elif hasattr(args, "cloud") and args.cloud:
        os.environ["REMARKABLE_PREFERRED_CONNECTION"] = "cloud"
        os.environ["REMARKABLE_USE_SSH"] = "false"

    from scripts.process_notebook import main as sync_main

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

    sync_main()


def cmd_status(args, root: Path):
    """Display system and connection status."""
    from remarkable_mcp.setup_wizard import (
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
    has_ssh = (
        rm_cfg.get("use_ssh", False)
        or cfg.get("use_ssh", False)
        or bool(rm_cfg.get("ssh_password"))
    )
    token = rm_cfg.get("device_token", "")

    if not preferred:
        preferred = "ssh" if has_ssh else "cloud"

    from remarkable_mcp.setup_wizard import verify_remarkable_ssh

    ssh_ok, ssh_msg = False, ""
    if has_ssh or preferred == "ssh":
        ssh_host = rm_cfg.get("ssh_host", "10.11.99.1")
        ssh_port = rm_cfg.get("ssh_port", 22)
        ssh_password = rm_cfg.get("ssh_password", "") or None
        ssh_ok, ssh_msg = verify_remarkable_ssh(host=ssh_host, port=ssh_port, password=ssh_password)

    cloud_ok, cloud_msg = False, ""
    if token:
        cloud_ok, cloud_msg = verify_remarkable_token(token)

    # Format status output
    if preferred == "ssh":
        if ssh_ok:
            backup_note = f" {dim('(Cloud backup ready)')}" if cloud_ok else ""
            print(f"reMarkable:    {green('Connected')} (USB SSH — Preferred){backup_note}")
        elif cloud_ok:
            print(f"reMarkable:    {yellow('Connected')} (Cloud backup active — USB SSH unplugged)")
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
    root = get_root()

    parser = argparse.ArgumentParser(
        prog="living-ink",
        description="Sync handwritten reMarkable notebooks to Obsidian and Apple Notes.",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # sync command
    sync_parser = subparsers.add_parser("sync", help="Run the sync pipeline")
    sync_parser.add_argument("--notebook", help="Sync a specific notebook by name")
    sync_parser.add_argument("--limit", type=int, default=0, help="Max notebooks to process")
    sync_parser.add_argument("--folder", help="Apple Notes folder override")
    sync_parser.add_argument(
        "--ssh", action="store_true", help="Force sync via USB SSH instead of Cloud"
    )
    sync_parser.add_argument(
        "--cloud", action="store_true", help="Force sync via reMarkable Cloud instead of SSH"
    )

    # setup command
    subparsers.add_parser("setup", help="Launch the interactive setup wizard")

    # status command
    subparsers.add_parser("status", help="Show system, tablet, and vault status")

    args = parser.parse_args()

    if args.command is None:
        # Default behavior: if config exists, sync; otherwise setup
        config_file = get_config_path(root)

        if config_file.exists():
            cmd_sync(args, root)
        else:
            cmd_setup(args, root)
    elif args.command == "sync":
        cmd_sync(args, root)
    elif args.command == "setup":
        cmd_setup(args, root)
    elif args.command == "status":
        cmd_status(args, root)


if __name__ == "__main__":
    main()
