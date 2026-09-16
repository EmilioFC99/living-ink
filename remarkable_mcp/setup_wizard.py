"""Interactive setup wizard for Living Ink.

Provides a guided, user-friendly terminal walkthrough to:
1. Pair or verify reMarkable tablet connection.
2. Select and verify AI handwriting OCR provider (Gemini, OpenAI, Ollama, etc.).
3. Auto-detect Obsidian vaults, choose existing or new destination folders.
4. Optionally configure Apple Notes and automated background sync (LaunchAgent).
5. Validate all credentials live and save config/config.yml.

Example:
    >>> from remarkable_mcp.setup_wizard import run_wizard
    >>> run_wizard()
"""

import json
import logging
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

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
    except Exception as e:
        logger.debug("Failed to parse obsidian.json: %s", e)
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
    except Exception as e:
        logger.debug("Error listing vault folders: %s", e)

    return folders


# ---------------------------------------------------------------------------
# reMarkable Pairing & Verification Helpers
# ---------------------------------------------------------------------------


def get_existing_remarkable_token() -> Optional[str]:
    """Check for an existing reMarkable device token in ~/.rmapi or config.

    Returns:
        The device token string if found, or None.
    """
    rmapi_file = Path.home() / ".rmapi"
    if rmapi_file.exists():
        try:
            token = rmapi_file.read_text(encoding="utf-8").strip()
            if token and "YOUR" not in token:
                return token
        except Exception:
            pass

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
        from remarkable_mcp.sync import load_client_from_token

        client = load_client_from_token(token)
        items = client.get_meta_items()
        docs = [it for it in items if getattr(it, "Type", "") == "DocumentType"]
        return True, f"Connected to reMarkable Cloud ({len(docs)} notebooks found)"
    except Exception as e:
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
        from remarkable_mcp.api import register_and_get_token

        token = register_and_get_token(code)
        return True, token, "Successfully paired with reMarkable Cloud!"
    except Exception as e:
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
        from remarkable_mcp.ssh import SSHClient

        client = SSHClient(host=host, user=user, port=port)
        if client.check_connection():
            items = client.get_meta_items()
            docs = [it for it in items if getattr(it, "Type", "") == "DocumentType"]
            return True, f"Connected via USB SSH ({len(docs)} notebooks found)"
        return (
            False,
            "Could not establish passwordless SSH connection. Is the tablet connected via USB and authorized?",
        )
    except Exception as e:
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

    from remarkable_mcp.providers import get_provider

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
    """Install and load a macOS LaunchAgent to sync notes automatically.

    Args:
        repo_dir: Path to the living-ink repository root.
        interval_seconds: Sync frequency in seconds (default: 3600 / 1 hour).

    Returns:
        Tuple of (success_bool, message_str).
    """
    if platform.system() != "Darwin":
        return False, "LaunchAgent background sync is only supported on macOS."

    if repo_dir is None:
        repo_dir = Path(__file__).parent.parent.resolve()

    uv_path = find_uv_path()
    data_env = os.environ.get("LIVING_INK_DATA_DIR")
    if data_env:
        logs_dir = Path(data_env) / "logs"
    else:
        logs_dir = repo_dir / "data" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    out_log = logs_dir / "launchagent.log"
    err_log = logs_dir / "launchagent.error.log"
    script_path = repo_dir / "scripts" / "process_notebook.py"

    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCH_AGENT_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{uv_path}</string>
        <string>run</string>
        <string>python</string>
        <string>{script_path}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{repo_dir}</string>
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
    except Exception as e:
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
    except Exception as e:
        return False, f"Failed to uninstall LaunchAgent: {e}"


def install_cli_command(repo_dir: Path, bin_dir: Optional[Path] = None) -> Tuple[bool, str]:
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
    script = f"""#!/usr/bin/env bash
export PATH="$HOME/.local/bin:$PATH"
exec uv run --directory "{repo_dir.resolve()}" living-ink "$@"
"""
    try:
        wrapper.write_text(script, encoding="utf-8")
        wrapper.chmod(0o755)
        return True, f"Global command 'living-ink' installed to {wrapper}"
    except Exception as e:
        return False, f"Could not create global command wrapper: {e}"


# ---------------------------------------------------------------------------
# Config File Generation
# ---------------------------------------------------------------------------


def generate_config_yaml(
    ai_provider: str,
    ai_api_key: str,
    ai_model: str,
    remarkable_token: str = "",
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

    Args:
        ai_provider: AI provider preset name.
        ai_api_key: API key for the AI provider.
        ai_model: Model name for the AI provider.
        remarkable_token: reMarkable Cloud device token (empty if using SSH only).
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
        YAML string ready to be written to config/config.yml.
    """
    # Quote path if it contains spaces or special characters
    clean_vault = obsidian_vault_path.strip()

    return f"""# Living Ink Configuration
# Generated by Setup Wizard

# 1. AI Handwriting OCR & Text Cleanup
ai:
  provider: "{ai_provider}"
  api_key: "{ai_api_key}"
  model: "{ai_model}"

# 2. reMarkable Tablet Connection
remarkable:
  preferred_connection: "{preferred_connection}"
  use_ssh: {"true" if use_ssh else "false"}
  ssh_host: "{ssh_host}"
  ssh_port: {ssh_port}
  device_token: "{remarkable_token}"

# 3. Google Cloud Vision (OPTIONAL — Not needed when using Gemini or OpenAI)
google_vision:
  credentials_path: ""

# 4. Sync Settings
sync:
  max_notebooks_per_run: {max_notebooks_per_run}

# 5. Obsidian Destination
obsidian:
  enabled: {"true" if obsidian_enabled else "false"}
  vault_path: "{clean_vault}"
  root_folder: "{obsidian_root_folder}"
  mirror_folders: {"true" if obsidian_mirror_folders else "false"}
  attachments_folder: "attachments"

# 6. Apple Notes Destination
apple_notes:
  enabled: {"true" if apple_notes_enabled else "false"}
  folder_name: "{apple_notes_folder}"
"""


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
) -> bool:
    """Run the interactive setup walkthrough.

    Args:
        input_func: Function for getting user input (default: built-in input).
        print_func: Function for printing output (default: built-in print).
        repo_dir: Optional root repository directory path.

    Returns:
        True if configuration was successfully created, False if aborted.
    """
    if repo_dir is None:
        repo_dir = Path(__file__).parent.parent.resolve()

    env_config_dir = os.environ.get("LIVING_INK_CONFIG_DIR")
    if env_config_dir:
        config_dir = Path(env_config_dir)
    else:
        config_dir = repo_dir / "config"
    config_file = config_dir / "config.yml"

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
        print_func("To use USB SSH:")
        print_func("  1. Connect your reMarkable to this computer via USB-C cable.")
        print_func(
            "  2. Living Ink uses passwordless SSH keys (BatchMode=yes) — no passwords are ever stored."
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
            print_func(f"  To authorize this computer, run: {bold(f'ssh-copy-id root@{ssh_host}')}")
            retry = (
                input_func(bold("Continue anyway (you can run ssh-copy-id later)? [Y/n]: "))
                .strip()
                .lower()
            )
            if retry not in ("", "y", "yes"):
                print_func(red("Setup aborted."))
                return False

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
            return False

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
        ai_api_key=ai_key,
        ai_model=ai_model,
        remarkable_token=remarkable_token,
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
    config_file.write_text(yaml_content, encoding="utf-8")
    print_func(green(f"✓ Configuration saved to {bold(str(config_file))}"))

    # Install/update global CLI launcher in ~/.local/bin
    ok_cli, msg_cli = install_cli_command(repo_dir=repo_dir)
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
    if run_first in ("", "y", "yes"):
        print_func()
        print_func(cyan("Starting sync pipeline..."))
        print_func()
        proc_script = repo_dir / "scripts" / "process_notebook.py"
        uv_cmd = find_uv_path()
        try:
            if shutil.which("living-ink"):
                subprocess.run(["living-ink", "sync"], check=False)
            elif shutil.which(uv_cmd):
                subprocess.run([uv_cmd, "run", "python", str(proc_script)], check=False)
            else:
                subprocess.run([sys.executable, str(proc_script)], check=False)
        except Exception as e:
            print_func(red(f"Error running sync: {e}"))

    return True


if __name__ == "__main__":
    run_wizard()
