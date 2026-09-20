"""What the tool knows about its own health, gathered once.

Every reader of this — ``info``, the wizard's closing summary, the preflight —
asks the same questions of the same objects, so they are asked here rather than
three times with three answers.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# The module, never the symbol: these are the seams the behaviour tests
# replace, and a ``from ... import`` binds a copy the patch cannot reach —
# silently, with the test still passing against the real thing.
from living_ink.cli import caches as caches_api
from living_ink.config import credentials
from living_ink.settings import SettingOrigin, Settings

logger = logging.getLogger(__name__)


@dataclass
class StatusReport:
    """A snapshot of the system's health, independent of how it is displayed.

    ``living-ink info`` has two renderers — a styled console view and
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
    #: Why the vault is unusable, straight from ``ObsidianDestination.check()``
    #: so status and preflight cannot disagree about it. Empty when it is fine.
    obsidian_problem: str = ""

    #: The supervisor job — launchd or systemd — whose only remaining task is
    #: keeping ``living-ink watch`` alive. *When* a sync happens is the cron
    #: expression below; these two say whether anything is there to run it.
    auto_sync_installed: bool = False
    auto_sync_active: bool = False

    watch_enabled: bool = False
    watch_schedule: str = ""
    watch_timezone: str = ""
    #: Why the schedule cannot be read, from the same validators the watcher
    #: refuses to start on. Empty when it is fine, and empty when watching is
    #: off — an unset schedule is not a broken one.
    watch_problem: str = ""
    #: When the schedule next fires, ISO-8601. None when watching is off or the
    #: expression will not parse.
    watch_next_run: Optional[str] = None
    #: The most recent run the *scheduler* started, which is a different
    #: question from :attr:`last_run`: somebody who syncs by hand every morning
    #: would otherwise keep resetting the clock that watches the daemon.
    last_scheduled_run: Optional[dict[str, Any]] = None
    #: One line, shown above everything else, when the schedule is configured
    #: and not working. Empty the rest of the time.
    watch_alert: str = ""
    #: The single thing to do about :attr:`watch_alert`.
    watch_alert_fix: str = ""

    cache_entries: int = 0
    cache_bytes: int = 0

    #: Where the sync state lives, whether or not it is there yet. Always set
    #: once the config parsed, because "no database" is itself the answer on a
    #: fresh install and a path the user can look at beats the word "none".
    state_path: Optional[Path] = None
    state_exists: bool = False
    state_bytes: int = 0
    state_schema: int = 0
    state_counts: dict[str, int] = field(default_factory=dict)
    #: The most recent run's row, or None before the first sync.
    last_run: Optional[dict[str, Any]] = None

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
        against ``living-ink info --json``; change them only deliberately.
        1.0 drops the ``apple_notes`` object, which is such a change: the
        destination it described no longer exists, and reporting it as
        permanently disabled would be a fiction a script could still branch on.

        Returns:
            A JSON-serialisable dict. When the config is missing or unparsable,
            every section other than ``config`` is an empty object, matching
            the behaviour scripts already rely on.
        """
        empty: dict[str, Any] = {
            "remarkable": {},
            "ai": {},
            "obsidian": {},
            "watch": {},
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
                "problem": self.obsidian_problem,
            },
            # Was ``auto_sync``, and renamed with the thing it describes: a
            # scheduled sync is now a cron expression the watcher reads, and
            # the job the two booleans describe no longer decides when anything
            # runs — only whether the watcher is alive to read it.
            "watch": {
                "enabled": self.watch_enabled,
                "schedule": self.watch_schedule,
                "timezone": self.watch_timezone,
                "problem": self.watch_problem,
                "next_run": self.watch_next_run,
                "last_scheduled_run": self.last_scheduled_run,
                "alert": self.watch_alert,
                "job": {
                    "installed": self.auto_sync_installed,
                    "active": self.auto_sync_active,
                },
            },
            "cache": {
                "entries": self.cache_entries,
                "size_bytes": self.cache_bytes,
            },
            # What ``state`` used to print as its summary. It is here rather
            # than under its own command because one health surface that
            # disagrees with nothing beats three that can.
            "state": {
                "path": str(self.state_path) if self.state_path else None,
                "exists": self.state_exists,
                "size_bytes": self.state_bytes,
                "schema_version": self.state_schema,
                "counts": dict(self.state_counts),
                "last_run": self.last_run,
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

    from living_ink.destinations import ObsidianDestination
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
    report.obsidian_enabled = obs_cfg.get("enabled", True)
    vault = Path(obs_cfg.get("vault_path", ""))
    report.obsidian_vault = str(vault)
    report.obsidian_root_folder = obs_cfg.get("root_folder", "")
    if report.obsidian_enabled and obs_cfg.get("vault_path"):
        # The destination's own check, not a second copy of it: a status that
        # says the vault is fine while the sync refuses it is worse than no
        # status at all, and that is what two implementations drift into.
        vault_status = ObsidianDestination(vault_path=str(vault)).check()
        report.obsidian_valid = vault_status.ok
        report.obsidian_problem = "" if vault_status.ok else vault_status.detail
    else:
        report.obsidian_valid = False
        report.obsidian_problem = "" if not report.obsidian_enabled else "No vault_path is set."

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
        for cache in caches_api.all_caches():
            entries, total = cache.stats()
            report.cache_entries += entries
            report.cache_bytes += total
    except OSError:
        logger.debug("Could not measure the caches", exc_info=True)

    _collect_state(report)
    _collect_watch(report, cfg)

    return report


#: How late a scheduled sync has to be before ``info`` calls it missing. Long
#: enough that a tick running right now, or one that started a minute late
#: because the machine was busy, is not reported as a failure; short enough
#: that a daemon that died overnight is flagged the next morning.
WATCH_GRACE_SECONDS = 15 * 60


def _collect_watch(report: StatusReport, cfg: dict[str, Any]) -> None:
    """Fill in the schedule, the next fire and whether the last one worked.

    The whole Watch panel is decided here rather than in the renderer, so the
    console view and ``--json`` cannot disagree about whether a schedule is
    healthy — which is the failure this module exists to prevent.

    Args:
        report: The report to fill in.
        cfg: The parsed config, for the ``watch:`` section.
    """
    from living_ink import scheduler

    watch_cfg = cfg.get("watch", {}) or {}
    settings = Settings.resolve(cfg, config_path=report.config_path)
    report.watch_enabled = bool(settings.watch_enabled)
    report.watch_schedule = (settings.watch_schedule or "").strip()

    try:
        tz, tz_name = scheduler.resolve_timezone(settings.watch_timezone)
    except ValueError as e:
        report.watch_timezone = str(watch_cfg.get("timezone", "") or "")
        report.watch_problem = str(e)
        return
    report.watch_timezone = tz_name

    if not report.watch_enabled:
        return

    if not report.watch_schedule:
        report.watch_problem = "Watching is on but no schedule is set (watch.schedule)."
        return

    problem = scheduler.validate_expression(report.watch_schedule)
    if problem:
        report.watch_problem = problem
        return

    now = datetime.now(tz)
    report.watch_next_run = scheduler.next_fire(report.watch_schedule, now, tz).isoformat()

    try:
        from living_ink.pipeline import get_state_store

        report.last_scheduled_run = get_state_store().last_scheduled_run()
    except Exception:
        # Same reason as _collect_state: an unreadable database must not take
        # the rest of the health report down with it.
        logger.debug("Could not read the last scheduled run", exc_info=True)
        return

    _raise_watch_alert(report, now, tz)


def _raise_watch_alert(report: StatusReport, now: datetime, tz: Any) -> None:
    """Decide whether the schedule deserves a banner, and what it should say.

    Two triggers, and only two. The last scheduled run failed — which is the
    case a user finds out about by noticing a notebook is missing, days later.
    Or a fire time has come and gone with nothing recorded against it, which is
    what a watcher that is not running looks like from the outside: there is no
    error anywhere, because nothing ran to produce one.

    Args:
        report: The report to fill in; read for the last scheduled run.
        now: The current time, in the schedule's zone.
        tz: The schedule's zone.
    """
    from living_ink import scheduler, state

    last = report.last_scheduled_run
    if last is not None and last.get("outcome") not in state.HEALTHY_OUTCOMES:
        report.watch_alert = f"The last scheduled sync {scheduler.describe_run(last)}."
        report.watch_alert_fix = "Run 'living-ink sync' to retry, or check living-ink.log."
        return

    due = scheduler.previous_fire(report.watch_schedule, now, tz)
    if (now - due).total_seconds() <= WATCH_GRACE_SECONDS:
        return

    started = scheduler.parse_stored(last.get("started_at")) if last else None
    if started is not None and started >= due:
        return

    ago = scheduler.humanize_ago((now - due).total_seconds())
    report.watch_alert = f"A scheduled sync was due {ago} and did not run."
    report.watch_alert_fix = (
        "Check that the background job is running: 'living-ink setup' reinstalls it."
    )


def _collect_state(report: StatusReport) -> None:
    """Fill in what the sync state database holds, if it is there.

    Counts and a last-run row, never the rows themselves: this runs on the
    console path too, and a health check that reads every page of every
    document to print four numbers is one nobody runs twice. The full dump is
    :func:`state_rows`, asked for only by ``info --json``.

    Args:
        report: The report to fill in. Left at its defaults when there is no
            database yet, which is the honest answer before the first sync.
    """
    path = caches_api.state_db_path()
    report.state_path = path
    if not path.exists():
        return

    try:
        from living_ink.pipeline import get_state_store

        store = get_state_store()
        report.state_exists = True
        report.state_bytes = path.stat().st_size
        report.state_schema = store.schema_version()
        report.state_counts = store.counts()
        report.last_run = store.last_run()
    except Exception:
        # Broad on purpose: an unreadable, locked or corrupt database is
        # precisely the condition a health check is run to discover, and
        # raising here would take the AI, vault and connection lines down
        # with it — the ones that would have said what to do next.
        logger.debug("Could not read the sync state at %s", path, exc_info=True)


def state_rows() -> dict[str, Any]:
    """Return every row in the sync state database.

    The machine surface that replaced ``state --dump``. Kept out of
    :func:`collect_status` because it is unbounded — one row per page of every
    document ever synced — and only a caller that asked for JSON wants it.

    Returns:
        Mapping of table name to its rows, or an empty mapping when there is
        no database yet or it cannot be read.
    """
    path = caches_api.state_db_path()
    if not path.exists():
        return {}
    try:
        from living_ink.pipeline import get_state_store

        return get_state_store().dump()
    except Exception:
        logger.debug("Could not dump the sync state at %s", path, exc_info=True)
        return {}


def short_destination(class_name: str) -> str:
    """Turn a destination class name into something worth printing.

    Args:
        class_name: e.g. ``FakeApiDestination``.

    Returns:
        e.g. ``Fake Api``.
    """
    import re

    trimmed = re.sub(r"Destination$", "", class_name)
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", trimmed) or class_name
