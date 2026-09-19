"""Typed, resolved configuration for a single run.

Configuration arrives from four places — per-run CLI options, environment
variables, the YAML config file and the credentials directory — and used to be
merged by writing YAML values *into* ``os.environ`` so that far-away modules
could read them back out as strings. That round trip made the precedence rules
invisible and left every consumer re-parsing ``"true"`` for itself.

:class:`Settings` is that merge, done once and typed. Build it with
:meth:`Settings.resolve` and pass it to whoever needs it;
:meth:`Settings.from_env` covers callers that have no config in hand.

Precedence, highest first:

1. Explicit CLI options.
2. Environment variables — a deliberate override, e.g. in Docker or CI.
3. The YAML config file, or the credentials directory for a secret.
4. The defaults in :mod:`living_ink.config.schema`.

**There is no table of fields in this module.** Every field, key, environment
variable and default comes from :data:`living_ink.config.schema.SETTINGS`, and
:meth:`resolve` and :meth:`explain` walk it. A parallel list is how ``explain``
came to be blind to two settings that shape every run; the parity test in
``tests/test_settings.py`` is what keeps the two from drifting again.

Environment variables stay meaningful for third-party SDKs that read them
directly (``OPENAI_API_KEY``); those are still exported by
:func:`living_ink.pipeline.load_yaml_config`. What no longer happens is Living
Ink talking to *itself* through the environment.
"""

import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from living_ink.config.schema import (
    CHOICE,
    DEFAULT_AI_LANGUAGE,
    DEFAULT_AI_TEMPERATURE,
    DEFAULT_APPLE_NOTES_FOLDER,
    DEFAULT_ATTACHMENTS_FOLDER,
    DEFAULT_CACHE_MAX_AGE_DAYS,
    DEFAULT_MAX_NOTEBOOKS_PER_RUN,
    DEFAULT_OCR_CONCURRENCY,
    DEFAULT_PREFERRED_CONNECTION,
    DEFAULT_RENDER_BACKGROUND,
    DEFAULT_RENDER_CACHE,
    DEFAULT_SSH_HOST,
    DEFAULT_SSH_PORT,
    DEFAULT_SSH_USER,
    DEFAULT_SYNC_EXCLUDE,
    DEFAULT_TRANSCRIPT_CACHE,
    DEFAULT_VERBOSITY,
    FLAG,
    LIST,
    NUMBER,
    PATH,
    SETTINGS,
    STORE_CREDENTIALS,
    TEXT,
    WHOLE,
    Setting,
)
from living_ink.redact import register_secret

__all__ = [
    "DEFAULT_APPLE_NOTES_FOLDER",
    "DEFAULT_CACHE_MAX_AGE_DAYS",
    "DEFAULT_MAX_NOTEBOOKS_PER_RUN",
    "DEFAULT_OCR_CONCURRENCY",
    "DEFAULT_PREFERRED_CONNECTION",
    "DEFAULT_RENDER_CACHE",
    "DEFAULT_SSH_HOST",
    "DEFAULT_SSH_PORT",
    "DEFAULT_SSH_USER",
    "DEFAULT_TRANSCRIPT_CACHE",
    "SOURCE_CONFIG",
    "SOURCE_CREDENTIALS",
    "SOURCE_DEFAULT",
    "SOURCE_ENV",
    "SOURCE_FLAG",
    "SettingOrigin",
    "Settings",
    "as_bool",
    "as_int",
    "as_list",
    "as_str",
]

_TRUTHY = ("1", "true", "yes", "on")

SOURCE_FLAG = "flag"
SOURCE_ENV = "environment"
SOURCE_CONFIG = "config file"
SOURCE_CREDENTIALS = "credentials"
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
        source: One of :data:`SOURCE_FLAG`, :data:`SOURCE_ENV`,
            :data:`SOURCE_CONFIG`, :data:`SOURCE_CREDENTIALS`,
            :data:`SOURCE_DEFAULT`.
        env_var: The environment variable that overrides this setting, or an
            empty string when it has none.
        secret: Whether the value must be masked before display.
        origin_detail: Which *particular* thing supplied it — the flag name,
            the config key, the credential name. "from a flag" is not an
            answer a user can act on; "from ``--ai-model``" is.
        label: The config key, or the field name when there is no key. What a
            report prints in the left column.
    """

    name: str
    value: Any
    source: str
    env_var: str
    secret: bool = False
    origin_detail: str = ""
    label: str = ""

    def display(self) -> str:
        """Return the value as text, masked when it is a secret.

        Returns:
            A short printable rendering. A secret goes through
            :func:`living_ink.config.credentials.mask`, which keeps the first
            and last few characters: enough for a user to tell *which* token
            they are looking at — the only reason to print one — without it
            surviving a screen share or a pasted log.
        """
        if self.secret:
            from living_ink.config.credentials import mask

            return mask(self.value if isinstance(self.value, str) else None)
        if self.value is None:
            return "not set"
        if isinstance(self.value, bool):
            return "true" if self.value else "false"
        if isinstance(self.value, tuple):
            return ", ".join(str(item) for item in self.value) or "not set"
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


def as_float(value: Any, default: float) -> float:
    """Interpret a config or environment value as a floating-point number.

    Args:
        value: A number, a numeric string, or None.
        default: Returned when ``value`` is None or unparseable.

    Returns:
        The float interpretation of ``value``.
    """
    if value is None:
        return default
    try:
        return float(str(value).strip())
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


def as_list(value: Any, default: Tuple[str, ...] = ()) -> Tuple[str, ...]:
    """Interpret a config or environment value as a tuple of strings.

    A YAML list is the natural spelling. An environment variable has no way to
    say "several", so a comma-separated string reads as one too — which also
    means a single bare string is a one-element list rather than an error.

    Args:
        value: A list, a comma-separated string, or None.
        default: Returned when ``value`` is None.

    Returns:
        The items, stripped, with blanks dropped.
    """
    if value is None:
        return default
    items = value if isinstance(value, (list, tuple)) else str(value).split(",")
    return tuple(text for text in (str(item).strip() for item in items) if text)


@dataclass(frozen=True)
class Settings:
    """Every setting the pipeline reads, already resolved and typed.

    Frozen because a run's configuration should not drift underneath the code
    using it. Per-run CLI overrides are applied by building a new instance with
    :func:`dataclasses.replace`, not by mutation.

    The fields below must match :data:`living_ink.config.schema.SETTINGS`
    exactly, one for one. That is asserted by a test rather than maintained by
    hand: a field with no schema entry cannot be resolved from anything, and a
    schema entry with no field is invisible to every reader at once.

    Attributes:
        preferred_connection: ``"ssh"`` or ``"cloud"`` — which transport to try first.
        use_ssh: Whether SSH is usable at all.
        ssh_host: Tablet address, ``10.11.99.1`` over USB.
        ssh_user: SSH user on the tablet.
        ssh_port: SSH port on the tablet.
        ssh_password: The tablet's screen password, if one is stored.
        remarkable_token: reMarkable Cloud device token, if one is configured.
        ai_provider: Name of the AI provider, or None for no cleanup.
        ai_model: Model name, or None for the provider's default.
        ai_api_key: API key for the selected provider, if one is stored.
        ai_base_url: Endpoint for a custom or self-hosted provider.
        ai_temperature: Sampling temperature for transcription.
        ai_language: Language to transcribe in, or ``"auto"``.
        ai_prompt_dir: Directory of user-owned prompt overrides, if any.
        ocr_concurrency: How many pages to transcribe at once. 1 is serial.
        sync_pdfs: Whether annotated PDFs are synced alongside notebooks.
        sync_epubs: Whether annotated EPUBs are synced alongside notebooks.
        sync_tags: Only sync documents carrying one of these tablet tags.
        sync_exclude: Tablet folders never synced.
        skip_empty: Whether a document that transcribes to nothing is skipped.
        max_notebooks_per_run: Cap on documents processed in one run.
        prune: Whether published notes of deleted documents are removed.
        obsidian_enabled: Whether the Obsidian destination is active.
        obsidian_vault_path: Path to the vault.
        obsidian_root_folder: Folder inside the vault everything lands under.
        obsidian_mirror_folders: Whether the tablet's folder tree is mirrored.
        obsidian_attachments_folder: Subfolder page images land in.
        obsidian_embed_images: Whether page images are embedded in the note.
        apple_notes_enabled: Whether the Apple Notes destination is active.
        apple_notes_folder: Destination folder name in Apple Notes.
        watch_enabled: Whether a scheduled sync is installed.
        watch_schedule: Cron expression for the scheduled sync.
        watch_timezone: Timezone the schedule is read in.
        transcript_cache: Whether transcriptions are reused across runs.
        render_cache: Whether rendered page images are reused across runs.
        cache_max_age_days: Idle age at which a cached page is pruned.
        data_dir: Override for the runtime artifact directory.
        verbosity: ``"quiet"``, ``"normal"`` or ``"verbose"``.
        output_json: Whether the run report is printed as JSON.
        repair_enabled: Whether the AI cleanup pass runs at all.
        render_background: Paper colour behind a rendered page.
    """

    preferred_connection: str = DEFAULT_PREFERRED_CONNECTION
    use_ssh: bool = True
    ssh_host: str = DEFAULT_SSH_HOST
    ssh_user: str = DEFAULT_SSH_USER
    ssh_port: int = DEFAULT_SSH_PORT
    ssh_password: Optional[str] = None
    remarkable_token: Optional[str] = None

    ai_provider: Optional[str] = None
    ai_model: Optional[str] = None
    ai_api_key: Optional[str] = None
    ai_base_url: Optional[str] = None
    ai_temperature: float = DEFAULT_AI_TEMPERATURE
    ai_language: str = DEFAULT_AI_LANGUAGE
    ai_prompt_dir: Optional[str] = None
    ocr_concurrency: int = DEFAULT_OCR_CONCURRENCY

    sync_pdfs: bool = False
    sync_epubs: bool = False
    sync_tags: Tuple[str, ...] = ()
    sync_exclude: Tuple[str, ...] = DEFAULT_SYNC_EXCLUDE
    skip_empty: bool = False
    max_notebooks_per_run: int = DEFAULT_MAX_NOTEBOOKS_PER_RUN
    prune: bool = False

    obsidian_enabled: bool = False
    obsidian_vault_path: Optional[str] = None
    obsidian_root_folder: Optional[str] = None
    obsidian_mirror_folders: bool = True
    obsidian_attachments_folder: str = DEFAULT_ATTACHMENTS_FOLDER
    obsidian_embed_images: bool = True
    apple_notes_enabled: bool = False
    apple_notes_folder: str = DEFAULT_APPLE_NOTES_FOLDER

    watch_enabled: bool = False
    watch_schedule: Optional[str] = None
    watch_timezone: Optional[str] = None

    transcript_cache: bool = DEFAULT_TRANSCRIPT_CACHE
    render_cache: bool = DEFAULT_RENDER_CACHE
    cache_max_age_days: int = DEFAULT_CACHE_MAX_AGE_DAYS
    data_dir: Optional[str] = None
    verbosity: str = DEFAULT_VERBOSITY
    output_json: bool = False

    repair_enabled: bool = True
    render_background: str = DEFAULT_RENDER_BACKGROUND

    @classmethod
    def resolve(
        cls,
        config: Optional[Dict[str, Any]] = None,
        env: Optional[Mapping[str, str]] = None,
        flags: Optional[Mapping[str, Any]] = None,
        config_path: Optional[Path] = None,
    ) -> "Settings":
        """Merge flags, the environment, a YAML config and stored credentials.

        Args:
            config: Parsed ``config.yml`` contents. An empty or missing config
                is fine — every field has a default.
            env: Environment mapping to read overrides from. Defaults to
                ``os.environ``; pass an explicit mapping in tests.
            flags: Field name to value for options given on the command line.
                A ``None`` value means the flag was absent, not that it was
                given as empty.
            config_path: Config file whose sibling credentials directory holds
                the secrets. Defaults to the resolved config path.

        Returns:
            The resolved settings for this run.
        """
        values = {
            field: value for field, value, _, _ in cls._layers(config, env, flags, config_path)
        }

        # Registered the moment it is known, so nothing downstream has to
        # remember that these particular strings must not reach a log file.
        for name in ("remarkable_token", "ai_api_key", "ssh_password"):
            register_secret(values.get(name))

        return cls(**values)

    @classmethod
    def _layers(
        cls,
        config: Optional[Dict[str, Any]],
        env: Optional[Mapping[str, str]],
        flags: Optional[Mapping[str, Any]],
        config_path: Optional[Path],
    ) -> List[Tuple[str, Any, str, str]]:
        """Resolve every setting and record which layer supplied it.

        The one merge point. :meth:`resolve` throws the provenance away and
        :meth:`explain` keeps it, but neither re-implements the precedence,
        because a second implementation is how ``info`` starts reporting an
        origin the run did not actually use.

        Args:
            config: Parsed ``config.yml`` contents, or None.
            env: Environment mapping, or None for ``os.environ``.
            flags: Field name to command-line value, or None.
            config_path: Config file the credentials directory derives from.

        Returns:
            One ``(field, value, source, origin_detail)`` per setting, in
            schema order.
        """
        environ = os.environ if env is None else env
        given = flags or {}
        raw = config or {}

        resolved: List[Tuple[str, Any, str, str]] = []
        seen: Dict[str, Any] = {}

        for setting in SETTINGS:
            value, source, detail = cls._pick(setting, raw, environ, given, config_path, seen)
            value = _coerce(setting, value, seen)
            seen[setting.field] = value
            resolved.append((setting.field, value, source, detail))

        return resolved

    @staticmethod
    def _pick(
        setting: Setting,
        config: Dict[str, Any],
        environ: Mapping[str, str],
        flags: Mapping[str, Any],
        config_path: Optional[Path],
        seen: Mapping[str, Any],
    ) -> Tuple[Any, str, str]:
        """Return the first value any layer supplies for one setting.

        Args:
            setting: The schema entry being resolved.
            config: Parsed config contents.
            environ: Environment mapping.
            flags: Field name to command-line value.
            config_path: Config file the credentials directory derives from.
            seen: Fields resolved so far, for the two settings whose lookup
                depends on an earlier one.

        Returns:
            ``(raw value, source, origin_detail)``. The value is None when
            nothing supplied one, and the source is :data:`SOURCE_DEFAULT`.
        """
        if setting.field in flags and flags[setting.field] is not None:
            return flags[setting.field], SOURCE_FLAG, setting.flag or setting.field

        if setting.env and environ.get(setting.env) not in (None, ""):
            return environ[setting.env], SOURCE_ENV, setting.env

        if setting.store == STORE_CREDENTIALS:
            name = _credential_name(setting, seen)
            if name:
                from living_ink.config.credentials import read_secret

                stored = read_secret(name, config_path=config_path)
                if stored:
                    return stored, SOURCE_CREDENTIALS, name

        for key in _config_paths(setting):
            value = _lookup(config, key)
            if value is not None:
                return value, SOURCE_CONFIG, key

        return None, SOURCE_DEFAULT, ""

    @classmethod
    def explain(
        cls,
        config: Optional[Dict[str, Any]] = None,
        env: Optional[Mapping[str, str]] = None,
        flags: Optional[Mapping[str, Any]] = None,
        config_path: Optional[Path] = None,
    ) -> List[SettingOrigin]:
        """Resolve settings and say where each value came from.

        Precedence is invisible in the resolved object alone, which makes a
        stray environment variable in a shell profile or a Docker file very
        hard to spot. This reports the same merge :meth:`resolve` performs —
        literally the same call — annotated with the layer that supplied each
        value.

        Args:
            config: Parsed ``config.yml`` contents, or None.
            env: Environment mapping. Defaults to ``os.environ``.
            flags: Field name to command-line value, or None.
            config_path: Config file the credentials directory derives from.

        Returns:
            One :class:`SettingOrigin` per setting, in schema order.
        """
        layers = cls._layers(config, env, flags, config_path)
        by_field = {setting.field: setting for setting in SETTINGS}

        origins = []
        for name, value, source, detail in layers:
            setting = by_field[name]
            origins.append(
                SettingOrigin(
                    name=name,
                    value=value,
                    source=source,
                    env_var=setting.env or "",
                    secret=setting.secret,
                    origin_detail=detail,
                    label=setting.key or name,
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


def _config_paths(setting: Setting) -> List[str]:
    """List the config keys a setting reads, current spelling first.

    A credential has no current key but may still have legacy ones, which is
    how a 0.1 config with an API key in it keeps working.

    Args:
        setting: The schema entry.

    Returns:
        Dotted paths, in the order they should be tried.
    """
    paths = [] if setting.key is None else [setting.key]
    paths.extend(setting.legacy_keys)
    return paths


def _lookup(config: Dict[str, Any], path: str) -> Any:
    """Read a dotted path out of a parsed config.

    Args:
        config: Parsed config contents.
        path: Dotted path, e.g. ``remarkable.ssh_host``.

    Returns:
        The value found, or None when any level of the path is absent.
    """
    head, dot, tail = path.partition(".")
    if not dot:
        return config.get(head)
    section = config.get(head)
    return section.get(tail) if isinstance(section, dict) else None


def _credential_name(setting: Setting, seen: Mapping[str, Any]) -> Optional[str]:
    """Return the credentials-directory name holding this setting's value.

    Most credentials have a fixed name. The AI key does not: it is stored per
    provider (``ai.api_key.gemini``), so that trying OpenAI for an afternoon
    and going back does not destroy the Gemini key. That name is only knowable
    once the provider is resolved, which is why ``ai_provider`` is declared
    before ``ai_api_key`` in the schema.

    Args:
        setting: The schema entry.
        seen: Fields resolved so far.

    Returns:
        The credential name, or None when there is nothing to look up.
    """
    if setting.credential:
        return setting.credential
    if setting.field != "ai_api_key":
        return None

    provider = seen.get("ai_provider")
    if not provider or str(provider).strip().lower() == "none":
        return None

    from living_ink.config.credentials import ai_key_name

    try:
        return ai_key_name(str(provider))
    except ValueError:
        # A provider name that cannot be a credential name is a config error,
        # and get_provider is about to report it far better than this could.
        return None


def _coerce(setting: Setting, value: Any, seen: Mapping[str, Any]) -> Any:
    """Turn a raw value from any layer into the type the field declares.

    Args:
        setting: The schema entry.
        value: The raw value, or None when no layer supplied one.
        seen: Fields resolved so far, for the one default that depends on
            another setting.

    Returns:
        The typed value.
    """
    default = _default_for(setting, seen)

    if setting.kind == FLAG:
        return as_bool(value, bool(default))
    if setting.kind == WHOLE:
        resolved = as_int(value, int(default))
        # One page at a time is serial; zero would be no pages at all.
        return max(1, resolved) if setting.field == "ocr_concurrency" else resolved
    if setting.kind == NUMBER:
        return as_float(value, float(default))
    if setting.kind == LIST:
        return as_list(value, default or ())
    if setting.kind == CHOICE:
        text = as_str(value, default)
        return text.lower() if isinstance(text, str) else text
    if setting.kind in (TEXT, PATH) and value is not None and not str(value).strip():
        # An explicit blank is a value, not an omission: an empty
        # ``obsidian.attachments_folder`` means "beside the note". A blank
        # environment variable never reaches here — :meth:`Settings._pick`
        # reads that as unset, which is what a shell means by it — so a blank
        # arriving here was written in the file or typed on the command line.
        return ""
    return as_str(value, default)


def _default_for(setting: Setting, seen: Mapping[str, Any]) -> Any:
    """Return a setting's default, resolving the one that is not a constant.

    ``use_ssh`` is a derived view rather than a product setting: when nothing
    states it, a stated preference of ``cloud`` must not be silently re-enabled
    by a default of True.

    Args:
        setting: The schema entry.
        seen: Fields resolved so far.

    Returns:
        The default value for this run.
    """
    if setting.field == "use_ssh":
        return seen.get("preferred_connection") == "ssh"
    return setting.default


def _assert_parity() -> None:
    """Fail at import if the schema and the dataclass have drifted.

    Raises:
        RuntimeError: If either side names a field the other does not. The
            test suite asserts the same thing with a readable message; this is
            the belt, so that a mismatch cannot survive even an unrun test.
    """
    declared = {setting.field for setting in SETTINGS}
    present = {f.name for f in fields(Settings)}
    if declared != present:
        missing = ", ".join(sorted(declared - present)) or "none"
        extra = ", ".join(sorted(present - declared)) or "none"
        raise RuntimeError(
            f"Settings and CONFIG schema disagree. In the schema only: {missing}. "
            f"On Settings only: {extra}."
        )


_assert_parity()
