"""Interactive setup wizard for Living Ink.

Provides a guided, user-friendly terminal walkthrough to:
1. Pair or verify reMarkable tablet connection.
2. Select and verify AI handwriting OCR provider (Gemini, OpenAI, Ollama, etc.).
3. Auto-detect Obsidian vaults, choose existing or new destination folders.
4. Optionally configure Apple Notes and automated background sync (LaunchAgent).
5. Validate all credentials live and save ~/.config/living-ink/config.yml.

Example:
    >>> from living_ink.setup_wizard import run_wizard
    >>> run_wizard()
"""

import json
import logging
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

from living_ink import safeio
from living_ink.config import SCHEMA_VERSION, credentials

logger = logging.getLogger(__name__)


@dataclass
class WizardResult:
    """What the setup wizard achieved, and what the user asked for next.

    The wizard used to run the first sync itself, which meant the onboarding UI
    imported the orchestrator and the orchestrator imported the onboarding UI.
    Returning the request instead leaves the decision to the CLI, the one layer
    that legitimately knows about both.

    Attributes:
        saved: Whether a config file was written.
        run_sync_requested: Whether the user asked to sync immediately.
    """

    saved: bool
    run_sync_requested: bool = False

    def __bool__(self) -> bool:
        """Report success, so existing truthiness checks keep working."""
        return self.saved


# ANSI styling helpers (disabled when stdout is not a TTY)
IS_TTY = sys.stdout.isatty()


def _c(text: str, code: str) -> str:
    """Format text with ANSI color code if stdout is a TTY."""
    return f"\033[{code}m{text}\033[0m" if IS_TTY else text


def bold(text: str) -> str:
    """Make text bold."""
    return _c(text, "1")


def green(text: str) -> str:
    """Format text in green."""
    return _c(text, "92")


def yellow(text: str) -> str:
    """Format text in yellow."""
    return _c(text, "93")


def cyan(text: str) -> str:
    """Format text in cyan."""
    return _c(text, "96")


def red(text: str) -> str:
    """Format text in red."""
    return _c(text, "91")


def dim(text: str) -> str:
    """Format text in dim gray."""
    return _c(text, "2")


# ---------------------------------------------------------------------------
# Obsidian Detection Helpers
# ---------------------------------------------------------------------------


def get_obsidian_config_path() -> Optional[Path]:
    """Get the path to Obsidian's application config file based on the OS.

    Returns:
        Path to obsidian.json if found, or None.
    """
    system = platform.system()
    if system == "Darwin":
        path = Path.home() / "Library" / "Application Support" / "obsidian" / "obsidian.json"
    elif system == "Linux":
        path = Path.home() / ".config" / "obsidian" / "obsidian.json"
    elif system == "Windows":
        app_data = os.environ.get("APPDATA")
        path = Path(app_data) / "obsidian" / "obsidian.json" if app_data else None
    else:
        path = None

    return path if path and path.exists() else None


def detect_obsidian_vaults() -> List[Dict[str, str]]:
    """Auto-detect Obsidian vaults registered on this computer.

    Reads Obsidian's internal `obsidian.json` configuration to find all
    active vault directories without requiring manual path entry.

    Returns:
        List of dicts with 'name' and 'path' keys for each existing vault.
    """
    config_path = get_obsidian_config_path()
    if not config_path:
        return []

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        vaults = []
        raw_vaults = data.get("vaults", {})
        for _, vinfo in raw_vaults.items():
            raw_path = vinfo.get("path", "")
            if not raw_path:
                continue
            vpath = Path(raw_path)
            if vpath.exists() and vpath.is_dir():
                vaults.append({"name": vpath.name, "path": str(vpath)})

        # Sort alphabetically by vault name
        vaults.sort(key=lambda x: x["name"].lower())
        return vaults
    except (OSError, ValueError, TypeError) as e:
        # An unreadable, malformed, or unexpectedly shaped obsidian.json. The
        # wizard falls back to asking for the path, so this stays quiet.
        logger.debug("Failed to parse obsidian.json: %s", e, exc_info=True)
        return []


def list_vault_folders(vault_path: Path) -> List[str]:
    """List non-hidden top-level directories inside an Obsidian vault.

    Args:
        vault_path: Path to the Obsidian vault root.

    Returns:
        Sorted list of subdirectory names (excluding hidden folders).
    """
    if not vault_path.exists() or not vault_path.is_dir():
        return []

    folders = []
    try:
        for item in vault_path.iterdir():
            if item.is_dir() and not item.name.startswith("."):
                folders.append(item.name)
        folders.sort(key=lambda x: x.lower())
    except OSError as e:
        logger.debug("Error listing vault folders: %s", e, exc_info=True)

    return folders


# ---------------------------------------------------------------------------
# reMarkable Pairing & Verification Helpers
# ---------------------------------------------------------------------------


def get_existing_remarkable_token() -> Optional[str]:
    """Check for an existing reMarkable device token.

    The credentials directory first, since that is where this build stores it,
    then ``~/.rmapi`` for a pairing that predates it. Finding either is what
    lets the wizard offer "use the existing pairing" instead of sending someone
    back to the website for a code they do not need.

    Returns:
        The device token string if found, or None.
    """
    stored = credentials.read_secret(credentials.CLOUD_TOKEN)
    if stored and "YOUR" not in stored:
        return stored

    rmapi_file = Path.home() / ".rmapi"
    if rmapi_file.exists():
        try:
            token = rmapi_file.read_text(encoding="utf-8").strip()
            if token and "YOUR" not in token:
                return token
        except (OSError, UnicodeDecodeError) as e:
            # No readable token file is the same answer as no token file.
            logger.debug("Could not read %s: %s", rmapi_file, e, exc_info=True)

    return None


def verify_remarkable_token(token: str) -> Tuple[bool, str]:
    """Verify that a reMarkable device token can connect to the Cloud API.

    Args:
        token: reMarkable device token.

    Returns:
        Tuple of (success_bool, message_str).
    """
    if not token or "YOUR" in token:
        return False, "Token is empty or unconfigured."

    try:
        from living_ink.sync import load_client_from_token

        client = load_client_from_token(token)
        items = client.get_meta_items()
        docs = [it for it in items if getattr(it, "Type", "") == "DocumentType"]
        return True, f"Connected to reMarkable Cloud ({len(docs)} notebooks found)"
    except Exception as e:
        # Broad by design: this function exists to turn any failure into one
        # sentence a person can act on. A wizard that raises is worse than a
        # wizard that reports.
        logger.debug("Token verification failed", exc_info=True)
        return False, f"Could not connect with token: {e}"


def pair_remarkable_device(one_time_code: str) -> Tuple[bool, str, str]:
    """Exchange an 8-letter reMarkable one-time pairing code for a device token.

    Args:
        one_time_code: 8-letter code from my.remarkable.com.

    Returns:
        Tuple of (success_bool, device_token, message_str).
    """
    code = one_time_code.strip()
    if len(code) != 8:
        return False, "", "Pairing code must be exactly 8 letters."

    try:
        from living_ink.api import register_and_get_token

        token = register_and_get_token(code)
        return True, token, "Successfully paired with reMarkable Cloud!"
    except Exception as e:
        # Same contract as verify_remarkable_token: report, never raise.
        logger.debug("Pairing failed", exc_info=True)
        return False, "", f"Pairing failed: {e}"


def verify_remarkable_ssh(
    host: str = "10.11.99.1",
    user: str = "root",
    port: int = 22,
) -> Tuple[bool, str]:
    """Test passwordless SSH connection to reMarkable tablet over USB.

    Args:
        host: SSH host address (default: 10.11.99.1 for USB).
        user: SSH user (default: root).
        port: SSH port (default: 22).

    Returns:
        Tuple of (success_bool, message_str).
    """
    try:
        from living_ink.ssh import SSHClient

        client = SSHClient(host=host, user=user, port=port)
        if client.check_connection():
            items = client.get_meta_items()
            docs = [it for it in items if getattr(it, "Type", "") == "DocumentType"]
            return True, f"Connected via USB SSH ({len(docs)} notebooks found)"

        # Perform targeted network diagnostics to provide precise guidance
        import socket

        web_interface_active = False
        ssh_port_open = False

        try:
            with socket.create_connection((host, 80), timeout=0.6):
                web_interface_active = True
        except OSError:
            # A closed port is one of the answers this probe is looking for.
            pass

        try:
            with socket.create_connection((host, port), timeout=0.6):
                ssh_port_open = True
        except OSError:
            pass

        if web_interface_active and not ssh_port_open:
            return (
                False,
                "Could not establish passwordless SSH connection: USB Web Interface is active, but SSH is disabled. Please ensure Developer Mode is enabled on your tablet (Settings → General → Software → Advanced → Developer mode).",
            )
        elif web_interface_active and ssh_port_open:
            return (
                False,
                f"Could not establish passwordless SSH connection: Tablet reached, but SSH key authentication failed. Run 'ssh-copy-id {user}@{host}' to authorize this computer.",
            )
        else:
            return (
                False,
                "Could not establish passwordless SSH connection. Is the tablet connected via USB, awake, and 'USB web interface' toggled ON under Settings → Storage?",
            )
    except Exception as e:
        # Broad: the diagnosis above is best-effort, and a failure to diagnose
        # must still leave the user with a message rather than a traceback.
        logger.debug("SSH verification failed", exc_info=True)
        return False, f"SSH connection failed: {e}"


# ---------------------------------------------------------------------------
# AI Provider Verification Helpers
# ---------------------------------------------------------------------------


def verify_ai_provider(
    provider_name: str,
    api_key: str = "",
    model: str = "",
) -> Tuple[bool, str]:
    """Test connection and authentication for an AI provider.

    Args:
        provider_name: Provider preset ('gemini', 'openai', 'ollama', etc.).
        api_key: API key for the provider.
        model: Model name override (optional).

    Returns:
        Tuple of (success_bool, message_str).
    """
    provider_clean = provider_name.strip().lower()

    if provider_clean == "none":
        return True, "AI cleanup disabled (raw OCR text will be used)."

    from living_ink.providers import get_provider

    config = {
        "ai": {
            "provider": provider_clean,
            "api_key": api_key,
        }
    }
    if model:
        config["ai"]["model"] = model

    try:
        provider = get_provider(config)
        test_prompt = "Reply with exactly: READY"
        response = provider.repair_text("Test", test_prompt)

        if response and response.strip():
            return True, f"Verified {provider.name} — connection successful!"
        return False, f"Provider {provider.name} returned an empty response. Check your API key."
    except Exception as e:
        # Broad: nine providers, each with its own idea of an error, and the
        # caller wants one line of prose either way.
        logger.debug("Provider verification failed", exc_info=True)
        return False, f"Verification failed: {e}"


# ---------------------------------------------------------------------------
# macOS LaunchAgent (Background Sync) Helpers
# ---------------------------------------------------------------------------

LAUNCH_AGENT_LABEL = "com.livingink.sync"
LAUNCH_AGENT_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def find_uv_path() -> str:
    """Find the full path to the uv executable."""
    found = shutil.which("uv")
    if found:
        return found
    local_uv = Path.home() / ".local" / "bin" / "uv"
    if local_uv.exists():
        return str(local_uv)
    cargo_uv = Path.home() / ".cargo" / "bin" / "uv"
    if cargo_uv.exists():
        return str(cargo_uv)
    return "uv"


def install_launch_agent(
    repo_dir: Optional[Path] = None,
    interval_seconds: int = 3600,
) -> Tuple[bool, str]:
    """Install macOS LaunchAgent plist for periodic background note sync.

    Args:
        repo_dir: Root repository directory path.
        interval_seconds: Sync interval in seconds (default: 3600 / 1 hour).

    Returns:
        Tuple of (success_bool, message_str).
    """
    if platform.system() != "Darwin":
        return False, "LaunchAgent background sync is only supported on macOS."

    from living_ink.config import get_logs_dir

    logs_dir = get_logs_dir(repo_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)
    out_log = logs_dir / "launchagent.log"
    err_log = logs_dir / "launchagent.error.log"

    cli_path = shutil.which("living-ink") or str(Path.home() / ".local" / "bin" / "living-ink")
    if Path(cli_path).exists():
        args_xml = f"""        <string>{cli_path}</string>
        <string>sync</string>"""
    else:
        uv_path = find_uv_path()
        args_xml = f"""        <string>{uv_path}</string>
        <string>run</string>
        <string>python</string>
        <string>-m</string>
        <string>living_ink</string>
        <string>sync</string>"""

    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCH_AGENT_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{args_xml}
    </array>
    <key>StartInterval</key>
    <integer>{interval_seconds}</integer>
    <key>StandardOutPath</key>
    <string>{out_log}</string>
    <key>StandardErrorPath</key>
    <string>{err_log}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{Path.home()}/.local/bin:{Path.home()}/.cargo/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
</dict>
</plist>
"""
    try:
        LAUNCH_AGENT_PLIST.parent.mkdir(parents=True, exist_ok=True)

        # Unload if already running
        if LAUNCH_AGENT_PLIST.exists():
            subprocess.run(
                ["launchctl", "unload", str(LAUNCH_AGENT_PLIST)],
                capture_output=True,
                check=False,
            )

        LAUNCH_AGENT_PLIST.write_text(plist_content, encoding="utf-8")

        # Load agent
        res = subprocess.run(
            ["launchctl", "load", str(LAUNCH_AGENT_PLIST)],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode != 0:
            return False, f"Failed to load LaunchAgent: {res.stderr.strip()}"

        return True, f"Installed background sync (runs every {interval_seconds // 60} minutes)"
    except OSError as e:
        return False, f"Could not create LaunchAgent: {e}"


def uninstall_launch_agent() -> Tuple[bool, str]:
    """Unload and remove the Living Ink macOS LaunchAgent.

    Returns:
        Tuple of (success_bool, message_str).
    """
    if not LAUNCH_AGENT_PLIST.exists():
        return True, "LaunchAgent was not installed."

    try:
        subprocess.run(
            ["launchctl", "unload", str(LAUNCH_AGENT_PLIST)],
            capture_output=True,
            check=False,
        )
        LAUNCH_AGENT_PLIST.unlink(missing_ok=True)
        return True, "Background sync LaunchAgent removed."
    except OSError as e:
        return False, f"Failed to uninstall LaunchAgent: {e}"


def install_cli_command(
    repo_dir: Optional[Path] = None, bin_dir: Optional[Path] = None
) -> Tuple[bool, str]:
    """Install or update the global 'living-ink' CLI command wrapper in ~/.local/bin.

    Args:
        repo_dir: Absolute path to the Living Ink repository root.
        bin_dir: Target directory for the executable (default: ~/.local/bin).

    Returns:
        Tuple of (success_bool, message_str).
    """
    if bin_dir is None:
        bin_dir = Path.home() / ".local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    wrapper = bin_dir / "living-ink"

    # Don't overwrite if living-ink is already managed by uv tool
    if wrapper.is_symlink() and bin_dir == Path.home() / ".local" / "bin":
        return True, f"Global command 'living-ink' is active at {wrapper} (managed by uv tool)"

    if repo_dir is None:
        if shutil.which("living-ink"):
            return True, "Global command 'living-ink' is already active in PATH"
        return False, "No repository directory provided to create CLI wrapper"

    script = f"""#!/usr/bin/env bash
VENV_BIN="{repo_dir.resolve()}/.venv/bin/living-ink"
if [ -x "$VENV_BIN" ]; then
    exec "$VENV_BIN" "$@"
else
    echo "Error: Living Ink virtualenv not found at {repo_dir.resolve()}/.venv" >&2
    echo "Please run: cd {repo_dir.resolve()} && uv sync" >&2
    exit 1
fi
"""
    try:
        wrapper.write_text(script, encoding="utf-8")
        wrapper.chmod(0o755)
        return True, f"Global command 'living-ink' installed to {wrapper}"
    except OSError as e:
        return False, f"Could not create global command wrapper: {e}"


# ---------------------------------------------------------------------------
# Config File Generation
# ---------------------------------------------------------------------------


def generate_config_yaml(
    ai_provider: str,
    ai_model: str,
    preferred_connection: str = "ssh",
    use_ssh: bool = True,
    ssh_host: str = "10.11.99.1",
    ssh_port: int = 22,
    obsidian_enabled: bool = False,
    obsidian_vault_path: str = "",
    obsidian_root_folder: str = "Living Ink",
    obsidian_mirror_folders: bool = True,
    apple_notes_enabled: bool = False,
    apple_notes_folder: str = "Living Ink",
    max_notebooks_per_run: int = 5,
) -> str:
    """Generate clean, commented config.yml content.

    No secret appears in the result. The API key and the device token are
    stored by :mod:`living_ink.config.credentials` instead, so this file stays
    something a user can paste into an issue, copy between machines or keep in
    a dotfiles repo without thinking about it first.

    Args:
        ai_provider: AI provider preset name.
        ai_model: Model name for the AI provider.
        preferred_connection: Preferred method ('ssh' or 'cloud').
        use_ssh: Whether USB SSH connection is enabled.
        ssh_host: SSH host address.
        ssh_port: SSH port number.
        obsidian_enabled: Whether Obsidian destination is enabled.
        obsidian_vault_path: Absolute path to Obsidian vault.
        obsidian_root_folder: Root folder inside the vault.
        obsidian_mirror_folders: Whether to mirror reMarkable folder hierarchy.
        apple_notes_enabled: Whether Apple Notes destination is enabled.
        apple_notes_folder: Folder name in Apple Notes.
        max_notebooks_per_run: Maximum notebooks to process per sync run.

    Returns:
        YAML string ready to be written to config.yml.

    Note:
        Values are emitted through PyYAML rather than string interpolation, so
        vault paths and folder names containing quotes, backslashes or colons
        round-trip correctly.
    """
    sections: List[Tuple[str, Dict[str, Any]]] = [
        (
            "Config format version — do not edit",
            {"schema_version": SCHEMA_VERSION},
        ),
        (
            "1. AI Handwriting OCR & Text Cleanup (the API key is stored separately)",
            {
                "ai": {
                    "provider": ai_provider,
                    "model": ai_model,
                }
            },
        ),
        (
            "2. reMarkable Tablet Connection (the device token is stored separately)",
            {
                "remarkable": {
                    "preferred_connection": preferred_connection,
                    "use_ssh": use_ssh,
                    "ssh_host": ssh_host,
                    "ssh_port": ssh_port,
                }
            },
        ),
        (
            "3. Sync Settings",
            {"sync": {"max_notebooks_per_run": max_notebooks_per_run}},
        ),
        (
            "4. Obsidian Destination",
            {
                "obsidian": {
                    "enabled": obsidian_enabled,
                    "vault_path": obsidian_vault_path.strip(),
                    "root_folder": obsidian_root_folder,
                    "mirror_folders": obsidian_mirror_folders,
                    "attachments_folder": "_attachments",
                }
            },
        ),
        (
            "5. Apple Notes Destination",
            {
                "apple_notes": {
                    "enabled": apple_notes_enabled,
                    "folder_name": apple_notes_folder,
                }
            },
        ),
    ]

    parts = ["# Living Ink Configuration", "# Generated by Setup Wizard", ""]
    for comment, section in sections:
        parts.append(f"# {comment}")
        parts.append(
            yaml.safe_dump(
                section,
                sort_keys=False,
                allow_unicode=True,
                default_flow_style=False,
            ).rstrip()
        )
        parts.append("")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Interactive Setup Wizard CLI
# ---------------------------------------------------------------------------


def _prompt_cloud_pairing(
    input_func: Callable[[str], str],
    print_func: Callable[..., None],
) -> str:
    """Helper to prompt user for reMarkable Cloud pairing or token."""
    existing_token = get_existing_remarkable_token()

    if existing_token:
        print_func(green("Found existing reMarkable pairing token on this computer."))
        choice = input_func(bold("Use existing reMarkable pairing? [Y/n]: ")).strip().lower()
        if choice in ("", "y", "yes"):
            print_func(dim("  Verifying token with reMarkable Cloud..."))
            ok, msg = verify_remarkable_token(existing_token)
            if ok:
                print_func(green(f"  ✓ {msg}"))
                return existing_token
            else:
                print_func(yellow(f"  ⚠️ Existing token could not connect: {msg}"))

    while True:
        print_func()
        print_func("To pair with reMarkable Cloud:")
        print_func(cyan("  1. Visit: ") + bold("https://my.remarkable.com/device/desktop/connect"))
        print_func("  2. Sign in and copy the 8-letter code.")
        print_func()
        code_or_token = input_func(
            bold("Enter your 8-letter code (or paste token, or press Enter to skip): ")
        ).strip()

        if not code_or_token:
            return ""

        if len(code_or_token) == 8:
            print_func(dim("  Pairing device with reMarkable Cloud..."))
            ok, token, msg = pair_remarkable_device(code_or_token)
            if ok:
                print_func(green(f"  ✓ {msg}"))
                return token
            else:
                print_func(red(f"  ✗ {msg}"))
        else:
            print_func(dim("  Verifying token..."))
            ok, msg = verify_remarkable_token(code_or_token)
            if ok:
                print_func(green(f"  ✓ {msg}"))
                return code_or_token
            else:
                print_func(red(f"  ✗ {msg}"))


def run_wizard(
    input_func: Callable[[str], str] = input,
    print_func: Callable[..., None] = print,
    repo_dir: Optional[Path] = None,
    bin_dir: Optional[Path] = None,
) -> WizardResult:
    """Run the interactive setup walkthrough.

    Args:
        input_func: Function for getting user input (default: built-in input).
        print_func: Function for printing output (default: built-in print).
        repo_dir: Optional root repository directory path.
        bin_dir: Optional custom bin directory for CLI wrapper installation.

    Returns:
        A WizardResult recording whether config was saved and whether the user
        asked to sync straight away. Running that sync is the caller's job.
    """
    from living_ink.config import get_config_path

    config_file = get_config_path(repo_dir)
    config_dir = config_file.parent

    print_func()
    print_func(bold(cyan("============================================================")))
    print_func(bold(cyan("              🖋️   Welcome to Living Ink Setup  🖋️            ")))
    print_func(bold(cyan("   Sync your reMarkable notebooks to Obsidian & Apple Notes ")))
    print_func(bold(cyan("============================================================")))
    print_func()

    # -----------------------------------------------------------------------
    # Step 1: reMarkable Tablet Connection
    # -----------------------------------------------------------------------
    print_func(bold("[Step 1 of 4] reMarkable Tablet Connection"))
    print_func(dim("-" * 60))
    print_func("Choose your preferred connection method:")
    print_func(
        f"  {bold('[1]')} USB SSH {green('(Recommended — Free, fast, works offline, Cloud backup)')}"
    )
    print_func(f"  {bold('[2]')} reMarkable Cloud (Wireless sync, USB SSH backup)")

    remarkable_token = ""
    preferred_connection = "ssh"
    use_ssh = True
    ssh_host = "10.11.99.1"
    ssh_port = 22

    conn_choice = input_func(bold("Select preferred connection [1-2] (default: 1): ")).strip()
    if conn_choice in ("", "1"):
        preferred_connection = "ssh"
        use_ssh = True
        print_func()
        print_func(bold("How to set up USB SSH on your reMarkable:"))
        print_func("  1. Connect your tablet to this computer via USB-C cable.")
        print_func(
            dim("     (Tip: If using a MacBook, try the other USB-C port if it doesn't connect)")
        )
        print_func("  2. Turn on the USB interface on the tablet:")
        print_func(
            f"     → Open {bold('Settings → Storage')} and toggle {bold('USB web interface')} to {green('ON')}."
        )
        print_func("  3. Make sure Developer Mode / SSH is enabled on your tablet:")
        print_func(
            f"     → Paper Pro / Pure: {bold('Settings → General → Software → Advanced → Developer mode')}"
        )
        print_func(
            f"     → reMarkable 2: {bold('Settings → General → Help → About → Copyrights & licenses')}"
        )
        print_func(
            "  4. Living Ink uses passwordless SSH keys (BatchMode=yes) — no passwords are ever stored."
        )
        print_func()

        host_input = input_func(bold("SSH host [10.11.99.1]: ")).strip()
        if host_input:
            ssh_host = host_input

        print_func(dim("  Verifying passwordless SSH connection..."))
        ok, msg = verify_remarkable_ssh(host=ssh_host, port=ssh_port)
        if ok:
            print_func(green(f"  ✓ {msg}"))
        else:
            print_func(yellow(f"  ⚠️ {msg}"))
            print_func()
            print_func(cyan("  Troubleshooting tips:"))
            print_func("    • Make sure the tablet is awake and screen is unlocked.")
            print_func(
                f"    • Check that {bold('USB web interface')} is toggled {green('ON')} under {bold('Settings → Storage')}."
            )
            print_func(
                f"    • Authorize this computer with: {bold(f'ssh-copy-id root@{ssh_host}')}"
            )
            print_func()
            retry = (
                input_func(bold("Continue anyway (you can finish setting up SSH later)? [Y/n]: "))
                .strip()
                .lower()
            )
            if retry not in ("", "y", "yes"):
                print_func(red("Setup aborted."))
                return WizardResult(saved=False)

        # Offer Cloud as automatic backup
        print_func()
        cloud_backup = (
            input_func(
                bold("Configure reMarkable Cloud as an automatic backup (when unplugged)? [y/N]: ")
            )
            .strip()
            .lower()
        )
        if cloud_backup in ("y", "yes"):
            remarkable_token = _prompt_cloud_pairing(input_func, print_func)
    else:
        preferred_connection = "cloud"
        remarkable_token = _prompt_cloud_pairing(input_func, print_func)
        if not remarkable_token:
            print_func(red("reMarkable Cloud pairing is required for Cloud mode. Setup aborted."))
            return WizardResult(saved=False)

        # Offer USB SSH as automatic backup
        print_func()
        ssh_backup = (
            input_func(bold("Configure USB SSH as an automatic backup (when plugged in)? [y/N]: "))
            .strip()
            .lower()
        )
        if ssh_backup in ("y", "yes"):
            use_ssh = True
            host_input = input_func(bold("SSH host [10.11.99.1]: ")).strip()
            if host_input:
                ssh_host = host_input
            print_func(dim("  Verifying passwordless SSH connection..."))
            ok, msg = verify_remarkable_ssh(host=ssh_host, port=ssh_port)
            if ok:
                print_func(green(f"  ✓ {msg}"))
            else:
                print_func(
                    yellow(
                        f"  ⚠️ {msg} (Saved as backup; run ssh-copy-id root@{ssh_host} to enable)"
                    )
                )
        else:
            use_ssh = False

    # -----------------------------------------------------------------------
    # Step 2: AI Handwriting OCR Provider
    # -----------------------------------------------------------------------
    print_func()
    print_func(bold("[Step 2 of 4] AI Handwriting OCR & Cleanup"))
    print_func(dim("-" * 60))
    print_func("Choose your AI provider for handwriting recognition and formatting:")
    print_func(
        f"  {bold('[1]')} Google Gemini {green('(Recommended — Free, fast, high accuracy)')}"
    )
    print_func(f"  {bold('[2]')} OpenAI (GPT-4o / GPT-4o-mini)")
    print_func(f"  {bold('[3]')} Ollama (100% local, free & private)")
    print_func(f"  {bold('[4]')} Other (Groq, OpenRouter, Mistral, Together, Custom)")
    print_func(f"  {bold('[5]')} None (Raw text only, no AI cleanup)")

    ai_provider = "gemini"
    ai_model = "gemini-flash-latest"
    ai_key = ""

    provider_choice = input_func(bold("Select provider [1-5] (default: 1): ")).strip()
    if provider_choice in ("", "1"):
        ai_provider = "gemini"
        ai_model = "gemini-flash-latest"
        print_func()
        print_func(f"Model: {cyan(ai_model)} (Default)")
        print_func(f"Get a free API key at: {bold('https://aistudio.google.com/apikey')}")
    elif provider_choice == "2":
        ai_provider = "openai"
        ai_model = "gpt-4o-mini"
        print_func()
        print_func(f"Model: {cyan(ai_model)} (Default)")
        print_func(f"Get your API key at: {bold('https://platform.openai.com/api-keys')}")
    elif provider_choice == "3":
        ai_provider = "ollama"
        ai_model = "llama3.2"
        print_func(green("Local Ollama selected — no API key needed!"))
    elif provider_choice == "4":
        p_name = (
            input_func(
                bold("Enter provider name (groq / openrouter / mistral / together / custom): ")
            )
            .strip()
            .lower()
        )
        ai_provider = p_name or "custom"
        ai_model = input_func(bold("Enter model name (or leave empty for default): ")).strip()
    elif provider_choice == "5":
        ai_provider = "none"
        ai_model = ""

    # Prompt for API key if needed
    if ai_provider not in ("ollama", "none"):
        while not ai_key:
            key_input = input_func(bold(f"Enter your {ai_provider.capitalize()} API key: ")).strip()
            if not key_input:
                print_func(red("API key cannot be empty."))
                continue

            print_func(dim("  Verifying API key..."))
            ok, msg = verify_ai_provider(ai_provider, key_input, ai_model)
            if ok:
                print_func(green(f"  ✓ {msg}"))
                ai_key = key_input
            else:
                print_func(yellow(f"  ⚠️ {msg}"))
                retry = input_func(bold("Save this key anyway? [y/N]: ")).strip().lower()
                if retry in ("y", "yes"):
                    ai_key = key_input

    # -----------------------------------------------------------------------
    # Step 3: Destinations (Obsidian & Apple Notes)
    # -----------------------------------------------------------------------
    print_func()
    print_func(bold("[Step 3 of 4] Notes Destinations"))
    print_func(dim("-" * 60))

    # Obsidian Setup
    obsidian_enabled = True
    obsidian_vault_path = ""
    obsidian_root_folder = "Living Ink"
    obsidian_mirror_folders = True

    obs_choice = input_func(bold("Enable Obsidian sync? [Y/n]: ")).strip().lower()
    if obs_choice in ("", "y", "yes"):
        obsidian_enabled = True
        detected_vaults = detect_obsidian_vaults()

        if detected_vaults:
            print_func()
            print_func(
                green(f"🔍 Found {len(detected_vaults)} Obsidian Vault(s) on your computer:")
            )
            for idx, v in enumerate(detected_vaults, 1):
                print_func(f"  {bold(f'[{idx}]')} {v['name']} {dim('(' + v['path'] + ')')}")
            print_func(f"  {bold(f'[{len(detected_vaults) + 1}]')} Enter a custom path manually")

            v_choice = input_func(
                bold(f"Select vault [1-{len(detected_vaults) + 1}] (default: 1): ")
            ).strip()

            try:
                v_idx = int(v_choice) if v_choice else 1
                if 1 <= v_idx <= len(detected_vaults):
                    chosen_vault = detected_vaults[v_idx - 1]
                    obsidian_vault_path = chosen_vault["path"]
                else:
                    obsidian_vault_path = input_func(
                        bold("Enter path to your Obsidian vault: ")
                    ).strip()
            except ValueError:
                obsidian_vault_path = input_func(
                    bold("Enter path to your Obsidian vault: ")
                ).strip()
        else:
            obsidian_vault_path = input_func(
                bold("Enter absolute path to your Obsidian vault: ")
            ).strip()

        # Clean quotes/escapes if user dragged-and-dropped folder in terminal
        obsidian_vault_path = obsidian_vault_path.strip("'\"").replace("\\ ", " ")

        # Ask for destination folder (Existing or New)
        vault_p = Path(obsidian_vault_path)
        existing_folders = list_vault_folders(vault_p)

        print_func()
        print_func(bold(f"Where inside '{vault_p.name}' should your notes be saved?"))
        folder_options = []

        # If 'Living Ink' already exists, put it first
        if "Living Ink" in existing_folders:
            folder_options.append("Living Ink")

        for f in existing_folders:
            if f != "Living Ink":
                folder_options.append(f)

        for idx, f in enumerate(folder_options, 1):
            print_func(f"  {bold(f'[{idx}]')} {f} {dim('(Existing folder)')}")

        new_opt_idx = len(folder_options) + 1
        root_opt_idx = len(folder_options) + 2
        print_func(f"  {bold(f'[{new_opt_idx}]')} Create a new folder")
        print_func(f"  {bold(f'[{root_opt_idx}]')} Vault root directly {dim('(no subfolder)')}")

        f_choice = input_func(bold(f"Choose folder [1-{root_opt_idx}] (default: 1): ")).strip()

        try:
            f_num = int(f_choice) if f_choice else 1
            if 1 <= f_num <= len(folder_options):
                obsidian_root_folder = folder_options[f_num - 1]
            elif f_num == new_opt_idx:
                new_name = input_func(bold("Enter new folder name [Living Ink]: ")).strip()
                obsidian_root_folder = new_name or "Living Ink"
            elif f_num == root_opt_idx:
                obsidian_root_folder = ""
            else:
                obsidian_root_folder = "Living Ink"
        except ValueError:
            obsidian_root_folder = "Living Ink"

        mirror_choice = (
            input_func(bold("Mirror complete nested reMarkable folder hierarchy? [Y/n]: "))
            .strip()
            .lower()
        )
        obsidian_mirror_folders = mirror_choice in ("", "y", "yes")

    else:
        obsidian_enabled = False

    # Apple Notes Setup
    print_func()
    apple_notes_enabled = False
    apple_notes_folder = "Living Ink"
    if platform.system() == "Darwin":
        an_choice = input_func(bold("Enable Apple Notes sync? [y/N]: ")).strip().lower()
        if an_choice in ("y", "yes"):
            apple_notes_enabled = True
            an_folder = input_func(bold("Apple Notes folder name [Living Ink]: ")).strip()
            apple_notes_folder = an_folder or "Living Ink"

    # -----------------------------------------------------------------------
    # Step 4: Final Validation & Save
    # -----------------------------------------------------------------------
    print_func()
    print_func(bold("[Step 4 of 4] Saving Configuration"))
    print_func(dim("-" * 60))

    yaml_content = generate_config_yaml(
        ai_provider=ai_provider,
        ai_model=ai_model,
        preferred_connection=preferred_connection,
        use_ssh=use_ssh,
        ssh_host=ssh_host,
        ssh_port=ssh_port,
        obsidian_enabled=obsidian_enabled,
        obsidian_vault_path=obsidian_vault_path,
        obsidian_root_folder=obsidian_root_folder,
        obsidian_mirror_folders=obsidian_mirror_folders,
        apple_notes_enabled=apple_notes_enabled,
        apple_notes_folder=apple_notes_folder,
    )

    config_dir.mkdir(parents=True, exist_ok=True)
    # Still owner-only and still written in one step. The secrets have moved
    # out, but a config names a vault path and a tablet, and a half-written one
    # would lose the answers the user just gave.
    safeio.write_secret_atomic(config_file, yaml_content)
    print_func(green(f"✓ Configuration saved to {bold(str(config_file))}"))

    # The two secrets go beside it, one file each, never into it. Written after
    # the config so that the directory they are derived from exists, and
    # reported by name and mask so the user can see which key landed without
    # the key itself reaching the scrollback. Each is stored independently:
    # failing to store the API key must not also cost the pairing the user just
    # completed, since that one needs a trip to the website to redo.
    for label, secret in (("ai", ai_key), ("remarkable", remarkable_token)):
        if not secret:
            continue
        try:
            name = (
                credentials.ai_key_name(ai_provider) if label == "ai" else credentials.CLOUD_TOKEN
            )
            credentials.write_secret(name, secret, config_path=config_file)
        except (OSError, ValueError) as e:
            print_func(yellow(f"  ⚠️  Could not store the {label} credential: {e}"))
            continue
        print_func(green(f"  ✓ Stored {name} ({credentials.mask(secret)})"))

    # Install/update global CLI launcher in ~/.local/bin
    ok_cli, msg_cli = install_cli_command(repo_dir=repo_dir, bin_dir=bin_dir)
    if ok_cli:
        print_func(green(f"  ✓ {msg_cli}"))

    # Optional: macOS Background Sync Setup
    if platform.system() == "Darwin":
        print_func()
        bg_choice = (
            input_func(bold("Automatically sync notes in background every hour? [y/N]: "))
            .strip()
            .lower()
        )
        if bg_choice in ("y", "yes"):
            ok, msg = install_launch_agent(repo_dir=repo_dir, interval_seconds=3600)
            if ok:
                print_func(green(f"  ✓ {msg}"))
            else:
                print_func(yellow(f"  ⚠️ {msg}"))

    # Offer to run first sync now
    print_func()
    print_func(bold(green("🎉 Setup Complete!")))
    run_first = (
        input_func(bold("Would you like to run your first sync now? [Y/n]: ")).strip().lower()
    )

    return WizardResult(saved=True, run_sync_requested=run_first in ("", "y", "yes"))
