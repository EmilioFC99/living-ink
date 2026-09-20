"""What ``setup`` has to find out about this machine.

The wizard's *questions* live in :mod:`living_ink.cli.commands.setup`; this
module is everything those questions need to know, and everything the answers
turn into: where Obsidian keeps its vaults, whether a token or a key still
works, what a launch agent looks like, and how a ``config.yml`` is written.

The split is not tidiness. ``info`` runs the same three probes the wizard runs
(:mod:`living_ink.cli.status`), and a health check must not have to import a
conversation to ask whether the tablet answers. Nothing in here prompts, prints
a question, or reads stdin.
"""

import json
import logging
import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from living_ink.config import SCHEMA_VERSION, credentials, render_config
from living_ink.config.schema import (
    DEFAULT_ATTACHMENTS_FOLDER,
    DEFAULT_PREFERRED_CONNECTION,
    DEFAULT_SSH_HOST,
    DEFAULT_SSH_PORT,
)

logger = logging.getLogger(__name__)


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
    """Test connection, authentication and vision for an AI provider.

    The probe is a real multimodal request, because that is the only kind of
    request a sync makes: ``clean.ocr_and_repair()`` reads every page by
    sending it as an image, and there is no second OCR backend to degrade to.
    A text-only model answers a text prompt perfectly and then fails on every
    page of every notebook, so verifying with one would be verifying the wrong
    thing.

    Args:
        provider_name: Provider preset ('gemini', 'openai', 'ollama', etc.).
            No provider at all — ``None``, or a name that is only whitespace —
            reads as ``none``: ``config.yml`` can say ``provider:`` with
            nothing after it, and ``info`` calls this with whatever it found.
        api_key: API key for the provider.
        model: Model name override (optional).

    Returns:
        Tuple of (success_bool, message_str).
    """
    provider_clean = (provider_name or "").strip().lower()

    if provider_clean in ("", "none"):
        return True, "AI cleanup disabled (raw OCR text will be used)."

    from living_ink.providers import get_provider
    from living_ink.settings import Settings

    # Built rather than resolved: this verifies the key the user just typed,
    # which is not stored anywhere yet and must not be overridden by whatever
    # the environment or an existing config already holds.
    candidate = Settings(
        ai_provider=provider_clean,
        ai_api_key=api_key,
        ai_model=model or None,
    )

    try:
        provider = get_provider(candidate)
        if not provider.supports_vision:
            return False, (
                f"Provider {provider.name} cannot read images, and every page is read "
                "as one. Choose a provider with vision support."
            )
        response = provider.probe_vision()

        if response and response.strip():
            return True, f"Verified {provider.name} — connection successful!"
        # The provider reports a failed request by returning nothing, so the
        # cause is on the provider rather than in the reply. Say it: a rejected
        # key and an unreachable host both look like silence from here, and
        # only one of them is fixed by checking the key.
        if provider.last_failure:
            return False, (
                f"Provider {provider.name} could not read a page image — {provider.last_failure}."
            )
        return False, (
            f"Provider {provider.name} returned nothing for a page image. Check the "
            "API key, and that the model can read images."
        )
    except Exception as e:
        # Broad: nine providers, each with its own idea of an error, and the
        # caller wants one line of prose either way.
        logger.debug("Provider verification failed", exc_info=True)
        return False, f"Verification failed: {e}"


# ---------------------------------------------------------------------------
# macOS LaunchAgent (keeping the watcher alive)
# ---------------------------------------------------------------------------

LAUNCH_AGENT_LABEL = "com.livingink.sync"
LAUNCH_AGENT_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def install_launch_agent(repo_dir: Optional[Path] = None) -> Tuple[bool, str]:
    """Install the macOS LaunchAgent that keeps ``living-ink watch`` running.

    The job answers one question — is the watcher alive — and nothing else.
    It used to answer two: the plist carried a ``StartInterval``, so *when* a
    sync happened was a number baked into a file in ``~/Library``, invisible
    to ``info``, unreachable from ``config``, and impossible to express as
    "weekdays at nine". The schedule is now a cron expression in ``config.yml``
    that the watcher reads on every tick, which is why this plist has no
    interval and why editing the schedule needs no reinstall.

    ``ProgramArguments`` is the installed ``living-ink`` executable, never
    ``uv run`` from a checkout: a supervised process that resolves its
    interpreter out of a directory the user may rename is a background job
    that dies silently months later. A missing CLI is therefore a refusal
    with a remedy, not a fallback.

    Args:
        repo_dir: Root repository directory, used only to locate the log
            directory the job's stdout and stderr are redirected to.

    Returns:
        Tuple of (success_bool, message_str).
    """
    if platform.system() != "Darwin":
        return False, "LaunchAgent background sync is only supported on macOS."

    cli_path = shutil.which("living-ink") or str(Path.home() / ".local" / "bin" / "living-ink")
    if not Path(cli_path).exists():
        return False, (
            "The 'living-ink' command is not installed, so there is nothing for the "
            "background job to run. Install it first, then run setup again."
        )

    from living_ink.config import get_logs_dir

    logs_dir = get_logs_dir(repo_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)
    out_log = logs_dir / "launchagent.log"
    err_log = logs_dir / "launchagent.error.log"

    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCH_AGENT_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{cli_path}</string>
        <string>watch</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
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

        # Unload first, always: a reinstall that only rewrites the file leaves
        # the old process running against the old plist until the next login.
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

        return True, "Installed the background job that runs your sync schedule"
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
    ai_base_url: str = "",
    preferred_connection: str = DEFAULT_PREFERRED_CONNECTION,
    use_ssh: bool = True,
    ssh_host: str = DEFAULT_SSH_HOST,
    ssh_port: int = DEFAULT_SSH_PORT,
    obsidian_enabled: bool = False,
    obsidian_vault_path: str = "",
    obsidian_root_folder: str = "Living Ink",
    obsidian_mirror_folders: bool = True,
    max_notebooks_per_run: int = 5,
    watch_schedule: str = "",
    watch_timezone: str = "",
) -> str:
    """Generate clean, commented config.yml content.

    No secret appears in the result. The API key and the device token are
    stored by :mod:`living_ink.config.credentials` instead, so this file stays
    something a user can paste into an issue, copy between machines or keep in
    a dotfiles repo without thinking about it first.

    Args:
        ai_provider: AI provider preset name.
        ai_model: Model name for the AI provider.
        ai_base_url: Endpoint for a provider with no preset. Written only when
            given — a preset carries its own URL, and pinning it into the file
            would freeze an endpoint the package is free to correct.
        preferred_connection: Preferred method ('ssh' or 'cloud').
        use_ssh: Whether USB SSH connection is enabled.
        ssh_host: SSH host address.
        ssh_port: SSH port number.
        obsidian_enabled: Whether Obsidian destination is enabled.
        obsidian_vault_path: Absolute path to Obsidian vault.
        obsidian_root_folder: Root folder inside the vault.
        obsidian_mirror_folders: Whether to mirror reMarkable folder hierarchy.
        max_notebooks_per_run: Maximum notebooks to process per sync run.
        watch_schedule: A cron expression, or "" for no automatic syncing.
        watch_timezone: The zone that expression is read in, written only
            alongside a schedule — a timezone with nothing to schedule is a
            line in the file that decides nothing.

    Returns:
        YAML string ready to be written to config.yml.

    Note:
        The file is serialised by :func:`living_ink.config.render_config`, the
        one writer both ``setup`` and ``config`` use, so the section comments
        come from the same schema that validates the result.
    """
    ai: Dict[str, Any] = {"provider": ai_provider, "model": ai_model}
    if ai_base_url:
        ai["base_url"] = ai_base_url

    # Enabled *is* having a schedule. Two keys that can disagree — a schedule
    # with ``enabled: false``, or the reverse — give the user a watcher that
    # runs nothing and a config that says it should.
    watch: Dict[str, Any] = {"enabled": bool(watch_schedule)}
    if watch_schedule:
        watch["schedule"] = watch_schedule
        watch["timezone"] = watch_timezone

    return render_config(
        {
            "schema_version": SCHEMA_VERSION,
            "ai": ai,
            "watch": watch,
            "remarkable": {
                "preferred_connection": preferred_connection,
                "use_ssh": use_ssh,
                "ssh_host": ssh_host,
                "ssh_port": ssh_port,
            },
            "sync": {"limit": max_notebooks_per_run},
            "obsidian": {
                "enabled": obsidian_enabled,
                "vault_path": obsidian_vault_path.strip(),
                "root_folder": obsidian_root_folder,
                "mirror_folders": obsidian_mirror_folders,
                "attachments_folder": DEFAULT_ATTACHMENTS_FOLDER,
            },
        }
    )
