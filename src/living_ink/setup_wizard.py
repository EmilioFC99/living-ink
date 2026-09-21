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
from dataclasses import dataclass
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


# ---------------------------------------------------------------------------
# Shell completions
# ---------------------------------------------------------------------------

#: What each shell calls the file it loads a completion from.
COMPLETION_FILENAMES = {"zsh": "_living-ink", "bash": "living-ink", "fish": "living-ink.fish"}

#: Wrapped around the lines added to an rc file so removing them later is exact
#: rather than a guess about where the block ended. ``uninstall`` matches on
#: these two strings; changing either orphans the block in every rc file that
#: already has one.
RC_START = "# living-ink tab completion — added by `living-ink setup`"
RC_END = "# end living-ink tab completion"

#: How long to wait for a shell to answer the probe. An interactive shell runs
#: the user's whole rc, which can source a version manager or two.
PROBE_TIMEOUT = 15


@dataclass(frozen=True)
class CompletionTarget:
    """Where one shell's completion script goes, and whether that is enough.

    Attributes:
        shell: The shell this was resolved for.
        path: The file to write.
        searched: Whether the shell reads this directory on its own. False
            means the script sits there inert until a line in an rc file
            points at it.
    """

    shell: str
    path: Path
    searched: bool


def detect_shell() -> Optional[str]:
    """Name the user's shell, if completions can be written for it.

    Returns:
        ``"zsh"``, ``"bash"``, ``"fish"``, or None for anything else —
        including an unset ``$SHELL``, which is what a container gives you.
    """
    name = Path(os.environ.get("SHELL", "")).name
    return name if name in COMPLETION_FILENAMES else None


def completion_dirs(shell: str) -> Tuple[Tuple[Path, bool], ...]:
    """List where a shell's completions may go, best first.

    Computed per call rather than declared at module scope, because every
    entry is relative to ``$HOME`` and a constant would bind whatever it was
    at import.

    Args:
        shell: One of the keys of :data:`COMPLETION_FILENAMES`.

    Returns:
        Pairs of directory and whether the shell searches it unprompted. The
        searched ones come first: an install that edits nothing the user owns
        is the whole point, and only the fallbacks need a line in an rc file.
    """
    home = Path.home()
    return {
        "zsh": (
            (Path("/opt/homebrew/share/zsh/site-functions"), True),
            (Path("/usr/local/share/zsh/site-functions"), True),
            (home / ".zfunc", False),
        ),
        "bash": (
            (Path("/opt/homebrew/etc/bash_completion.d"), True),
            (Path("/usr/local/etc/bash_completion.d"), True),
            (home / ".bash_completion.d", False),
        ),
        # fish has one answer and always reads it, so there is nothing to
        # choose and no rc line this could ever need.
        "fish": ((home / ".config" / "fish" / "completions", True),),
    }.get(shell, ())


def completion_target(shell: str) -> Optional[CompletionTarget]:
    """Choose where this shell's completion script should be written.

    A directory that exists wins over one that would have to be created:
    creating ``/usr/local/share/zsh/site-functions`` on a machine that has no
    such thing is inventing a convention rather than following one. Only a
    directory under ``$HOME`` may be created, because the others belong to a
    package manager.

    Args:
        shell: One of the keys of :data:`COMPLETION_FILENAMES`.

    Returns:
        Where to write, or None if this shell has nowhere writable.
    """
    filename = COMPLETION_FILENAMES.get(shell)
    candidates = completion_dirs(shell)
    if not filename or not candidates:
        return None

    for directory, searched in candidates:
        if directory.is_dir() and os.access(directory, os.W_OK):
            return CompletionTarget(shell, directory / filename, searched)

    home = Path.home()
    for directory, searched in candidates:
        if home == directory or home in directory.parents:
            return CompletionTarget(shell, directory / filename, searched)
    return None


def completion_is_live(shell: str) -> bool:
    """Ask the shell whether it would complete ``living-ink`` right now.

    The only honest test, and the reason this feature is not just a file
    write. Whether the script is on the search path and whether the shell's
    completion system was ever started are two different questions, and macOS
    answers no to the second: neither ``/etc/zshrc`` nor ``/etc/zprofile``
    runs ``compinit``, so a correctly installed script stays inert and Tab
    does nothing — indistinguishable, from the user's side, from a bad
    install.

    Args:
        shell: One of the keys of :data:`COMPLETION_FILENAMES`.

    Returns:
        True if the shell named a completion for ``living-ink``. False for
        anything else, including a shell that could not be run at all: the
        cost of being wrong is offering help that was not needed.
    """
    probes = {
        "zsh": (["zsh", "-i", "-c"], "print -r -- ${_comps[living-ink]:-}"),
        "bash": (["bash", "-i", "-c"], "complete -p living-ink 2>/dev/null"),
        "fish": (["fish", "-c"], "complete -C 'living-ink sy'"),
    }
    if shell not in probes:
        return False

    argv, script = probes[shell]
    try:
        result = subprocess.run(
            [*argv, script],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(result.stdout.strip())


def completion_rc_snippet(target: CompletionTarget) -> str:
    """Return the rc lines that make a written script findable.

    Args:
        target: Where the script was written.

    Returns:
        The block to append, already wrapped in its markers, or "" when the
        shell needs nothing — which is every fish install, and every zsh
        install on a machine whose completion system is already running.
    """
    if target.shell == "zsh":
        lines = [] if target.searched else [f"fpath+=({target.path.parent})"]
        # Unconditional, and after the fpath line: this is called only when the
        # probe said nothing completes, and on macOS the missing `compinit` is
        # the usual reason. Running it twice costs a little startup time and
        # breaks nothing, which is the cheaper way to be wrong.
        lines.append("autoload -Uz compinit && compinit")
    elif target.shell == "bash":
        lines = [f"source {target.path}"]
    else:
        return ""
    return "\n".join([RC_START, *lines, RC_END])


def shell_rc_path(shell: str) -> Optional[Path]:
    """Name the file a shell reads at the start of an interactive session.

    Args:
        shell: One of the keys of :data:`COMPLETION_FILENAMES`.

    Returns:
        The rc file, or None for a shell that needs no edit. macOS starts
        every terminal as a login shell, which is why bash reads
        ``.bash_profile`` there and ``.bashrc`` everywhere else.
    """
    home = Path.home()
    if shell == "zsh":
        return home / ".zshrc"
    if shell == "bash":
        return home / (".bash_profile" if platform.system() == "Darwin" else ".bashrc")
    return None


def install_completions(
    shell: Optional[str] = None, repo_dir: Optional[Path] = None
) -> Tuple[bool, str, Optional[CompletionTarget]]:
    """Write the completion script for this shell.

    This asks nobody. The file is Living Ink's own, in a directory meant for
    exactly it, and ``uninstall`` takes it back unconditionally — the same
    tier as the launch agent and the caches. The one part that needs consent
    is a line in an rc file the user owns, which is
    :func:`enable_completions_in_rc` and a separate decision.

    Args:
        shell: Which shell to install for. None detects it from ``$SHELL``.
        repo_dir: Project root, passed through when building the parser the
            script is generated from.

    Returns:
        Tuple of (success, message, where it went). The target is None when
        there was nowhere to write, so a caller can tell "no completions
        here" from "installed, and now unfindable".
    """
    # Imported here, not at module scope: this module is what the CLI's setup
    # command imports, so reaching back up to the app at import time is a
    # cycle. The completions module gives the same answer for the same reason.
    from living_ink.cli import completions
    from living_ink.cli.app import LivingInkCLI

    shell = shell or detect_shell()
    if not shell:
        return False, "Could not tell which shell you use, so no completions were installed", None

    target = completion_target(shell)
    if target is None:
        return False, f"Found nowhere writable to install {shell} completions", None

    try:
        target.path.parent.mkdir(parents=True, exist_ok=True)
        script = completions.render(LivingInkCLI(root=repo_dir).build_parser(), shell)
        target.path.write_text(script, encoding="utf-8")
    except OSError as e:
        return False, f"Could not write {shell} completions: {e}", None
    return True, f"Tab completion for {shell} installed to {target.path}", target


def enable_completions_in_rc(target: CompletionTarget) -> Tuple[bool, str]:
    """Append the lines that make an installed script take effect.

    The one part of this that touches a file the user owns, so the one part a
    caller has to ask about first. Idempotent: a second run finds its own
    marker and changes nothing, which matters because ``setup`` is a command
    people re-run.

    Args:
        target: Where the script was written.

    Returns:
        Tuple of (success, message).
    """
    snippet = completion_rc_snippet(target)
    rc = shell_rc_path(target.shell)
    if not snippet or rc is None:
        return True, f"{target.shell} needs no changes to find it"

    try:
        existing = rc.read_text(encoding="utf-8") if rc.exists() else ""
        if RC_START in existing:
            return True, f"{rc} already points at it"
        # One blank line above the block so it reads as a block, and none at
        # all when the file is new — a config whose first line is blank looks
        # like something went wrong.
        prefix = ("\n" if existing.endswith("\n") else "\n\n") if existing else ""
        with rc.open("a", encoding="utf-8") as handle:
            handle.write(f"{prefix}{snippet}\n")
    except OSError as e:
        return False, f"Could not update {rc}: {e}"
    return True, f"Added the completion lines to {rc}"


def uninstall_completions(shell: Optional[str] = None) -> List[str]:
    """Remove the completion script, and any rc block that was added for it.

    Both halves are removed, and neither is a question: this only ever undoes
    what :func:`install_completions` and :func:`enable_completions_in_rc` did,
    and the rc block is found by the markers they wrote rather than by
    matching on what the lines look like.

    Args:
        shell: Which shell to clean up. None does every shell that has a file,
            because the shell a user had at ``setup`` is not necessarily the
            one they have now.

    Returns:
        One line per thing removed, for the caller to print. Empty if there
        was nothing installed.
    """
    removed: List[str] = []
    for name in [shell] if shell else list(COMPLETION_FILENAMES):
        for directory, _searched in completion_dirs(name):
            script = directory / COMPLETION_FILENAMES[name]
            try:
                if script.is_file():
                    script.unlink()
                    removed.append(f"Removed {script}")
            except OSError as e:
                removed.append(f"Could not remove {script}: {e}")

        rc = shell_rc_path(name)
        if rc is None or not rc.exists():
            continue
        try:
            text = rc.read_text(encoding="utf-8")
            trimmed = _without_rc_block(text)
            if trimmed != text:
                rc.write_text(trimmed, encoding="utf-8")
                removed.append(f"Removed the completion lines from {rc}")
        except OSError as e:
            removed.append(f"Could not update {rc}: {e}")
    return removed


def _without_rc_block(text: str) -> str:
    """Strip the marked completion block out of an rc file's contents.

    Args:
        text: The whole file.

    Returns:
        The same text with the first ``RC_START``..``RC_END`` block removed,
        along with the one blank line :func:`enable_completions_in_rc` wrote
        above it — otherwise installing and uninstalling a few times leaves a
        growing stack of blank lines in somebody's ``.zshrc``. An unterminated
        block is left alone: the end marker is what proves where Living Ink's
        lines stop, and deleting to the end of a shell config on a guess is
        not a recovery.
    """
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if line.strip() == RC_START), None)
    if start is None:
        return text
    end = next((i for i, line in enumerate(lines[start:], start) if line.strip() == RC_END), None)
    if end is None:
        return text
    if start and not lines[start - 1].strip():
        start -= 1
    return "".join(lines[:start] + lines[end + 1 :])


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
    max_notebooks_per_run: int = 0,
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
        max_notebooks_per_run: Most documents to process per sync run, ``0``
            for no cap. A first run's job is to get the whole tablet into the
            vault; a cap makes that take as many runs as the user has
            notebooks, and one they have to discover a setting to lift. The
            schema's own default stays low, because that one protects a
            *scripted* run from an accidentally enormous one.
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
