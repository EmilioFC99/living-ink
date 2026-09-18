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
from typing import Any, Dict, List, Mapping, Optional

from living_ink.redact import register_secret

# Defaults for a USB-attached tablet.
DEFAULT_SSH_HOST = "10.11.99.1"
DEFAULT_SSH_USER = "root"
DEFAULT_SSH_PORT = 22

DEFAULT_PREFERRED_CONNECTION = "ssh"
DEFAULT_APPLE_NOTES_FOLDER = "Living Ink"
DEFAULT_MAX_NOTEBOOKS_PER_RUN = 1

# Pages are transcribed by one network call each, so a handful in flight is a
# large speedup. The ceiling is the provider's rate limit, not local CPU.
DEFAULT_OCR_CONCURRENCY = 4

# Transcribing is the only step that costs money, and it is pure with respect
# to the page image, so caching is on unless a user turns it off.
DEFAULT_TRANSCRIPT_CACHE = True
DEFAULT_CACHE_MAX_AGE_DAYS = 90

_TRUTHY = ("1", "true", "yes", "on")

# Every setting is overridable by exactly one environment variable. Keeping the
# pairing in one table means :meth:`Settings.resolve` and :meth:`Settings.explain`
# cannot drift: the same map decides what wins and what gets reported as having won.
FIELD_ENV_VARS: Dict[str, str] = {
    "remarkable_token": "REMARKABLE_TOKEN",
    "preferred_connection": "REMARKABLE_PREFERRED_CONNECTION",
    "use_ssh": "REMARKABLE_USE_SSH",
    "ssh_host": "REMARKABLE_SSH_HOST",
    "ssh_user": "REMARKABLE_SSH_USER",
    "ssh_port": "REMARKABLE_SSH_PORT",
    "sync_pdfs": "SYNC_PDFS",
    "sync_epubs": "SYNC_EPUBS",
    "max_notebooks_per_run": "SYNC_MAX_NOTEBOOKS",
    "ocr_concurrency": "SYNC_OCR_CONCURRENCY",
    "transcript_cache": "SYNC_TRANSCRIPT_CACHE",
    "cache_max_age_days": "SYNC_CACHE_MAX_AGE_DAYS",
    "apple_notes_folder": "APPLE_NOTES_FOLDER",
}

# Fields whose value must never be printed in full.
SECRET_FIELDS = frozenset({"remarkable_token"})

SOURCE_ENV = "environment"
SOURCE_CONFIG = "config file"
SOURCE_DEFAULT = "default"


@dataclass(frozen=True)
class SettingOrigin:
    """One resolved setting, with the reason it holds the value it holds.

    "Why is it syncing over the cloud when my config says ssh?" is answerable
    only if the answer names the layer that won. This is that answer, in a
    shape both the console and ``--json`` renderers can format.

    Attributes:
        name: Field name on :class:`Settings`.
        value: The resolved value.
        source: One of ``SOURCE_ENV``, ``SOURCE_CONFIG``, ``SOURCE_DEFAULT``.
        env_var: The environment variable that overrides this setting.
        secret: Whether the value must be masked before display.
    """

    name: str
    value: Any
    source: str
    env_var: str
    secret: bool = False

    def display(self) -> str:
        """Return the value as text, masked when it is a secret.

        Returns:
            A short printable rendering; secrets collapse to ``"set"`` or
            ``"not set"`` so a token never reaches a log or a screen share.
        """
        if self.secret:
            return "set" if self.value else "not set"
        if self.value is None:
            return "not set"
        if isinstance(self.value, bool):
            return "true" if self.value else "false"
        return str(self.value)


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
        ocr_concurrency: How many pages to transcribe at once. 1 is serial.
        transcript_cache: Whether transcriptions are reused across runs.
        cache_max_age_days: Idle age at which a cached transcription is pruned.
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
    ocr_concurrency: int = DEFAULT_OCR_CONCURRENCY
    transcript_cache: bool = DEFAULT_TRANSCRIPT_CACHE
    cache_max_age_days: int = DEFAULT_CACHE_MAX_AGE_DAYS

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
        environ = os.environ if env is None else env
        from_config = cls._config_values(config)

        def pick(field: str) -> Any:
            """Return the environment value for a field, else its config value."""
            env_key = FIELD_ENV_VARS[field]
            if environ.get(env_key) not in (None, ""):
                return environ[env_key]
            return from_config.get(field)

        preferred = (
            as_str(pick("preferred_connection"), DEFAULT_PREFERRED_CONNECTION)
            or DEFAULT_PREFERRED_CONNECTION
        ).lower()

        # ``use_ssh`` is only an independent switch when nothing states it; a
        # stated preference of "cloud" would otherwise be silently re-enabled.
        use_ssh = as_bool(pick("use_ssh"), preferred == "ssh")

        # Registered the moment it is known, so nothing downstream has to
        # remember that this particular string must not reach a log file.
        register_secret(as_str(pick("remarkable_token")))

        return cls(
            remarkable_token=as_str(pick("remarkable_token")),
            preferred_connection=preferred,
            use_ssh=use_ssh,
            ssh_host=as_str(pick("ssh_host"), DEFAULT_SSH_HOST) or DEFAULT_SSH_HOST,
            ssh_user=as_str(pick("ssh_user"), DEFAULT_SSH_USER) or DEFAULT_SSH_USER,
            ssh_port=as_int(pick("ssh_port"), DEFAULT_SSH_PORT),
            sync_pdfs=as_bool(pick("sync_pdfs"), False),
            sync_epubs=as_bool(pick("sync_epubs"), False),
            max_notebooks_per_run=as_int(
                pick("max_notebooks_per_run"), DEFAULT_MAX_NOTEBOOKS_PER_RUN
            ),
            ocr_concurrency=max(1, as_int(pick("ocr_concurrency"), DEFAULT_OCR_CONCURRENCY)),
            transcript_cache=as_bool(pick("transcript_cache"), DEFAULT_TRANSCRIPT_CACHE),
            cache_max_age_days=as_int(pick("cache_max_age_days"), DEFAULT_CACHE_MAX_AGE_DAYS),
            apple_notes_folder=as_str(pick("apple_notes_folder"), DEFAULT_APPLE_NOTES_FOLDER)
            or DEFAULT_APPLE_NOTES_FOLDER,
        )

    @staticmethod
    def _config_values(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Flatten a YAML config into ``{field name: raw value}``.

        The YAML nests by subsystem while :class:`Settings` is flat, and both
        resolution and reporting need the same translation. A key absent from
        the returned mapping means the config file said nothing about it.

        Args:
            config: Parsed ``config.yml`` contents, or None.

        Returns:
            Field name to raw, unconverted config value, omitting anything the
            config did not set.
        """
        cfg = config or {}
        rm = cfg.get("remarkable") or {}
        sync = cfg.get("sync") or {}
        notes = cfg.get("apple_notes") or {}

        values = {
            "remarkable_token": rm.get("device_token"),
            "preferred_connection": rm.get("preferred_connection"),
            # The bare top-level ``use_ssh`` is the pre-``remarkable:`` spelling.
            "use_ssh": rm.get("use_ssh") if rm.get("use_ssh") is not None else cfg.get("use_ssh"),
            "ssh_host": rm.get("ssh_host"),
            "ssh_user": rm.get("ssh_user"),
            "ssh_port": rm.get("ssh_port"),
            "sync_pdfs": sync.get("sync_pdfs"),
            "sync_epubs": sync.get("sync_epubs"),
            "max_notebooks_per_run": sync.get("max_notebooks_per_run"),
            "ocr_concurrency": sync.get("ocr_concurrency"),
            "transcript_cache": sync.get("transcript_cache"),
            "cache_max_age_days": sync.get("cache_max_age_days"),
            "apple_notes_folder": notes.get("folder_name"),
        }
        return {k: v for k, v in values.items() if v is not None}

    @classmethod
    def explain(
        cls,
        config: Optional[Dict[str, Any]] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> List[SettingOrigin]:
        """Resolve settings and say where each value came from.

        Precedence is invisible in the resolved object alone, which makes a
        stray environment variable in a shell profile or a Docker file very
        hard to spot. This reports the same merge :meth:`resolve` performs,
        annotated with the layer that supplied each value.

        Args:
            config: Parsed ``config.yml`` contents, or None.
            env: Environment mapping. Defaults to ``os.environ``.

        Returns:
            One :class:`SettingOrigin` per field, in declaration order.
        """
        resolved = cls.resolve(config, env)
        environ = os.environ if env is None else env
        from_config = cls._config_values(config)

        origins = []
        for field_name, env_var in FIELD_ENV_VARS.items():
            if environ.get(env_var) not in (None, ""):
                source = SOURCE_ENV
            elif field_name in from_config:
                source = SOURCE_CONFIG
            else:
                source = SOURCE_DEFAULT
            origins.append(
                SettingOrigin(
                    name=field_name,
                    value=getattr(resolved, field_name),
                    source=source,
                    env_var=env_var,
                    secret=field_name in SECRET_FIELDS,
                )
            )
        return origins

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
