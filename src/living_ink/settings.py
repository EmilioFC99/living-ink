"""Typed, resolved configuration for a single run.

Configuration arrives from three places — the YAML config file, environment
variables, and per-run CLI options — and used to be merged by writing YAML
values *into* ``os.environ`` so that far-away modules could read them back out
as strings. That round trip made the precedence rules invisible and left every
consumer re-parsing ``"true"`` for itself.

:class:`Settings` is that merge, done once and typed. Build it with
:meth:`Settings.resolve` (config file plus environment) and pass it to whoever
needs it; :meth:`Settings.from_env` covers callers that have no config in hand.

Precedence, highest first:

1. Explicit CLI options (applied by the caller, not here).
2. Environment variables — a deliberate override, e.g. in Docker or CI.
3. The YAML config file.
4. The defaults in this module.

Environment variables stay meaningful for third-party SDKs that read them
directly (``OPENAI_API_KEY``, ``GOOGLE_APPLICATION_CREDENTIALS``); those are
still exported by :func:`living_ink.pipeline.load_yaml_config`. What no longer
happens is Living Ink talking to *itself* through the environment.
"""

import os
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

# Defaults for a USB-attached tablet.
DEFAULT_SSH_HOST = "10.11.99.1"
DEFAULT_SSH_USER = "root"
DEFAULT_SSH_PORT = 22

DEFAULT_PREFERRED_CONNECTION = "ssh"
DEFAULT_APPLE_NOTES_FOLDER = "Living Ink"
DEFAULT_MAX_NOTEBOOKS_PER_RUN = 1

_TRUTHY = ("1", "true", "yes", "on")


def as_bool(value: Any, default: bool = False) -> bool:
    """Interpret a config or environment value as a boolean.

    Args:
        value: A bool, a string such as ``"true"``, or None.
        default: Returned when ``value`` is None.

    Returns:
        The boolean interpretation of ``value``.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUTHY


def as_int(value: Any, default: int) -> int:
    """Interpret a config or environment value as an integer.

    Args:
        value: An int, a numeric string, or None.
        default: Returned when ``value`` is None or unparseable.

    Returns:
        The integer interpretation of ``value``.
    """
    if value is None:
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def as_str(value: Any, default: Optional[str] = None) -> Optional[str]:
    """Interpret a config or environment value as a non-empty string.

    Args:
        value: Any value, or None.
        default: Returned when ``value`` is None or blank.

    Returns:
        The stripped string, or ``default`` if there was nothing to strip.
    """
    if value is None:
        return default
    text = str(value).strip()
    return text or default


@dataclass(frozen=True)
class Settings:
    """Every setting the pipeline reads, already resolved and typed.

    Frozen because a run's configuration should not drift underneath the code
    using it. Per-run CLI overrides are applied by building a new instance with
    :func:`dataclasses.replace`, not by mutation.

    Attributes:
        remarkable_token: reMarkable Cloud device token, if one is configured.
        preferred_connection: ``"ssh"`` or ``"cloud"`` — which transport to try first.
        use_ssh: Whether SSH is usable at all.
        ssh_host: Tablet address, ``10.11.99.1`` over USB.
        ssh_user: SSH user on the tablet.
        ssh_port: SSH port on the tablet.
        sync_pdfs: Whether annotated PDFs are synced alongside notebooks.
        sync_epubs: Whether annotated EPUBs are synced alongside notebooks.
        max_notebooks_per_run: Cap on documents processed in one run.
        apple_notes_folder: Destination folder name in Apple Notes.
    """

    remarkable_token: Optional[str] = None
    preferred_connection: str = DEFAULT_PREFERRED_CONNECTION
    use_ssh: bool = True
    ssh_host: str = DEFAULT_SSH_HOST
    ssh_user: str = DEFAULT_SSH_USER
    ssh_port: int = DEFAULT_SSH_PORT

    sync_pdfs: bool = False
    sync_epubs: bool = False
    max_notebooks_per_run: int = DEFAULT_MAX_NOTEBOOKS_PER_RUN

    apple_notes_folder: str = DEFAULT_APPLE_NOTES_FOLDER

    @classmethod
    def resolve(
        cls,
        config: Optional[Dict[str, Any]] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> "Settings":
        """Merge a YAML config and the environment into one typed Settings.

        Args:
            config: Parsed ``config.yml`` contents. An empty or missing config
                is fine — every field has a default.
            env: Environment mapping to read overrides from. Defaults to
                ``os.environ``; pass an explicit mapping in tests.

        Returns:
            The resolved settings for this run.
        """
        cfg = config or {}
        environ = os.environ if env is None else env

        rm = cfg.get("remarkable") or {}
        sync = cfg.get("sync") or {}
        notes = cfg.get("apple_notes") or {}

        def pick(env_key: str, *config_values: Any) -> Any:
            """Return the environment value, else the first config value set."""
            if environ.get(env_key) not in (None, ""):
                return environ[env_key]
            for value in config_values:
                if value is not None:
                    return value
            return None

        preferred = (
            as_str(
                pick("REMARKABLE_PREFERRED_CONNECTION", rm.get("preferred_connection")),
                DEFAULT_PREFERRED_CONNECTION,
            )
            or DEFAULT_PREFERRED_CONNECTION
        ).lower()

        # ``use_ssh`` is only an independent switch when nothing states it; a
        # stated preference of "cloud" would otherwise be silently re-enabled.
        stated_ssh = pick("REMARKABLE_USE_SSH", rm.get("use_ssh"), cfg.get("use_ssh"))
        use_ssh = as_bool(stated_ssh, preferred == "ssh")

        return cls(
            remarkable_token=as_str(pick("REMARKABLE_TOKEN", rm.get("device_token"))),
            preferred_connection=preferred,
            use_ssh=use_ssh,
            ssh_host=as_str(pick("REMARKABLE_SSH_HOST", rm.get("ssh_host")), DEFAULT_SSH_HOST)
            or DEFAULT_SSH_HOST,
            ssh_user=as_str(pick("REMARKABLE_SSH_USER", rm.get("ssh_user")), DEFAULT_SSH_USER)
            or DEFAULT_SSH_USER,
            ssh_port=as_int(pick("REMARKABLE_SSH_PORT", rm.get("ssh_port")), DEFAULT_SSH_PORT),
            sync_pdfs=as_bool(pick("SYNC_PDFS", sync.get("sync_pdfs")), False),
            sync_epubs=as_bool(pick("SYNC_EPUBS", sync.get("sync_epubs")), False),
            max_notebooks_per_run=as_int(
                pick("SYNC_MAX_NOTEBOOKS", sync.get("max_notebooks_per_run")),
                DEFAULT_MAX_NOTEBOOKS_PER_RUN,
            ),
            apple_notes_folder=as_str(
                pick("APPLE_NOTES_FOLDER", notes.get("folder_name")), DEFAULT_APPLE_NOTES_FOLDER
            )
            or DEFAULT_APPLE_NOTES_FOLDER,
        )

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Settings":
        """Resolve settings from the environment alone.

        For callers reached without a config file in hand — a bare
        :func:`living_ink.api.get_rmapi` call, for instance.

        Args:
            env: Environment mapping. Defaults to ``os.environ``.

        Returns:
            The resolved settings.
        """
        return cls.resolve(config=None, env=env)
