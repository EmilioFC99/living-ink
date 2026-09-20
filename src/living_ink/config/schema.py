"""Every setting Living Ink has, declared once.

This module is the single enumeration the rest of the package derives from.
:data:`SETTINGS` lists one :class:`Setting` per configurable fact and
:data:`SECTIONS` describes the ``config.yml`` sections they group into; between
them they carry the config key, the environment variable, the default, the
type, the help text and the status. Everything else reads that:

* :mod:`living_ink.config.validate` checks a parsed config against it.
* :class:`living_ink.settings.Settings` resolves and explains values from it.

The point is not tidiness. Before this, the same fact was written down in four
places — the schema, ``FIELD_ENV_VARS``, the wizard's hardcoded defaults and
the CLI's hand-registered flags — and they had already drifted. A setting
declared here is a setting all of its readers see; a setting forgotten here is
missing from all of them at once, which is a failure a single test can catch
(``TestSchemaParity`` in ``tests/test_settings.py``).

Nothing in here imports from outside :mod:`living_ink.config`, so the schema
can be read without pulling in the pipeline.
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from living_ink.config.credentials import CLOUD_TOKEN, SSH_PASSWORD

# ---------------------------------------------------------------------------
# Kinds
# ---------------------------------------------------------------------------

#: Marker for "any scalar is fine here", used for free-text values.
TEXT = "text"
#: Marker for "must read as a whole number".
WHOLE = "whole number"
#: Marker for "must read as true or false".
FLAG = "true/false"
#: Marker for "must read as a number, decimals allowed".
NUMBER = "number"
#: Marker for a filesystem location. Read like :data:`TEXT`, rendered as a path.
PATH = "path"
#: Marker for "one of a fixed set", enumerated by :attr:`Setting.choices`.
CHOICE = "choice"
#: Marker for a sequence. A YAML list, or a comma-separated string from an
#: environment variable, which has no other way to say "several".
LIST = "list"
#: Marker for a credential. Never written to ``config.yml``, never displayed
#: unmasked, and resolved from the credentials directory rather than the file.
SECRET = "secret"

# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

#: The setting is current. Validate it, read it, say nothing.
ACTIVE = "active"
#: The setting still works but is on its way out.
#:
#: Warned about by name and — for a key with a current spelling — copied onto
#: that spelling in memory at load time, so the rest of the package only ever
#: has to know the current one. Declared from the *new* setting's end, as
#: :attr:`Setting.legacy_keys`, so the pairing cannot be half-written.
DEPRECATED = "deprecated"
#: The setting is gone. Warned about, then dropped before anything reads it.
#:
#: This is the difference between "your config does nothing" and "your config
#: does not load". An unrecognised section is a hard error and aborts the run,
#: so a section cannot simply be deleted from the schema on the day its code is
#: deleted — every config still naming it would stop working. It is marked
#: ``removed`` instead, and disappears from the schema a release later, once
#: the warning has had time to be seen.
REMOVED = "removed"

#: Where a resolved value is persisted between runs.
#:
#: ``config`` is the ordinary case: the value has a key in ``config.yml``.
#: ``credentials`` values are secrets and live one file per credential in the
#: credentials directory (:mod:`living_ink.config.credentials`); they never
#: enter ``config.yml`` and never get a command-line flag, because a flag puts
#: them in the shell history and the process list. ``env_only`` values have no
#: persisted form at all — they exist here so that ``info`` can report them
#: rather than leaving a setting that shapes the run invisible.
STORE_CONFIG = "config"
STORE_CREDENTIALS = "credentials"
STORE_ENV_ONLY = "env_only"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

# Defaults for a USB-attached tablet.
DEFAULT_SSH_HOST = "10.11.99.1"
DEFAULT_SSH_USER = "root"
DEFAULT_SSH_PORT = 22

DEFAULT_PREFERRED_CONNECTION = "ssh"
DEFAULT_MAX_NOTEBOOKS_PER_RUN = 1

# Pages are transcribed by one network call each, so a handful in flight is a
# large speedup. The ceiling is the provider's rate limit, not local CPU.
DEFAULT_OCR_CONCURRENCY = 4

# Low, because transcription is not a creative task: the same page should come
# back the same way twice, and an invented word is worse than an illegible one.
DEFAULT_AI_TEMPERATURE = 0.3

#: Let the model infer the language rather than constraining it.
DEFAULT_AI_LANGUAGE = "auto"

# Transcribing is the only step that costs money, and it is pure with respect
# to the page image, so caching is on unless a user turns it off.
DEFAULT_TRANSCRIPT_CACHE = True
DEFAULT_RENDER_CACHE = True
DEFAULT_CACHE_MAX_AGE_DAYS = 90

DEFAULT_ATTACHMENTS_FOLDER = "_attachments"

# The folders every tablet has and nobody wants synced.
DEFAULT_SYNC_EXCLUDE: Tuple[str, ...] = ("Trash", "Templates", "Quick sheets")

#: Standard reMarkable paper colour — a light cream rather than pure white.
DEFAULT_RENDER_BACKGROUND = "#FBFBFB"

#: Report the same three lines a run has always printed.
DEFAULT_VERBOSITY = "normal"


# ---------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Choice:
    """One allowed value of a :data:`CHOICE` setting.

    Attributes:
        value: The stored value.
        label: What a menu shows for it.
        flag: A dedicated flag that selects this value, e.g. ``--ssh``. Choices
            sharing their setting's :attr:`Setting.exclusive_group` are mutually
            exclusive on the command line.
    """

    value: str
    label: str
    flag: Optional[str] = None


@dataclass(frozen=True)
class Setting:
    """One configurable fact, declared once.

    Attributes:
        field: The :class:`living_ink.settings.Settings` attribute name. **This
            is the identity**, not :attr:`key`: settings exist that have no
            config key at all, and the parity test joins on this.
        key: Dotted path in ``config.yml``, or None when the value is not
            stored there — every credential, and everything ``env_only``.
        kind: How the value is read and rendered.
        default: The value when nothing supplies one.
        help: One line, reused by the menu, the generated config and ``info``.
        env: Environment variable that overrides it, or None.
        flag: Long flag, or None for a setting with no command-line form.
        negated: The ``--no-x`` form for a boolean, or None.
        choices: Allowed values when :attr:`kind` is :data:`CHOICE`.
        exclusive_group: Flags sharing a group become one mutually exclusive
            argparse group.
        store: :data:`STORE_CONFIG`, :data:`STORE_CREDENTIALS` or
            :data:`STORE_ENV_ONLY`.
        credential: Credential name for a :data:`STORE_CREDENTIALS` setting
            whose name is fixed. None when the name is composed at resolve time
            — the AI key is stored per provider, so its name is not knowable
            until the provider is.
        status: :data:`ACTIVE`, :data:`DEPRECATED` or :data:`REMOVED`.
        legacy_keys: Older dotted paths that still read, and are copied onto
            :attr:`key` in memory at load time. Declaring the pairing here, on
            the surviving setting, is what keeps a rename from being written
            down in two places that can disagree.
        commands: Which commands get the flag. ``watch`` gets none: a
            supervised process is restarted without its arguments, so a flag
            would stop applying without saying so.
        secret: Mask this value everywhere it is displayed.
    """

    field: str
    key: Optional[str]
    kind: str
    default: Any
    help: str
    env: Optional[str] = None
    flag: Optional[str] = None
    negated: Optional[str] = None
    choices: Tuple[Choice, ...] = ()
    exclusive_group: Optional[str] = None
    store: str = STORE_CONFIG
    credential: Optional[str] = None
    status: str = ACTIVE
    legacy_keys: Tuple[str, ...] = ()
    commands: Tuple[str, ...] = ("sync",)
    secret: bool = False

    @property
    def section(self) -> Optional[str]:
        """Return the config section this setting's key lives in, if any.

        Returns:
            The part of :attr:`key` before the dot, ``""`` for a bare top-level
            key, or None when the setting has no config key.
        """
        if self.key is None:
            return None
        head, dot, _ = self.key.partition(".")
        return head if dot else ""

    @property
    def leaf(self) -> Optional[str]:
        """Return the setting's name within its section.

        Returns:
            The part of :attr:`key` after the dot, or the whole key for a bare
            top-level key, or None when the setting has no config key.
        """
        if self.key is None:
            return None
        _, dot, tail = self.key.partition(".")
        return tail if dot else self.key


@dataclass(frozen=True)
class Section:
    """One ``config.yml`` section, and what has become of it.

    The keys a section accepts are not listed here: they are derived from
    :data:`SETTINGS`, which is the point of the whole module. A section exists
    in this table to carry its status, its help text, and — for the sections
    that outlived their settings — the fact that it is on its way out.

    Attributes:
        help: One line describing what the section configures.
        status: :data:`ACTIVE`, :data:`DEPRECATED` or :data:`REMOVED`.
        replacement: Name of the section that supersedes this one, or None.
        note: One clause appended to the warning, for anything the status and
            the replacement do not already say.
        free_form: Whether the section's keys belong to something other than
            this schema and must not be policed.
    """

    help: str = ""
    status: str = ACTIVE
    replacement: Optional[str] = None
    note: str = ""
    free_form: bool = False


#: Every ``config.yml`` section, in the order a generated file writes them.
SECTIONS: Dict[str, Section] = {
    "ai": Section("Which model reads your handwriting."),
    "ocr": Section("How pages are read."),
    "remarkable": Section("How to reach the tablet."),
    "sync": Section("What gets synced, and what gets skipped."),
    "obsidian": Section("Where notes are published."),
    "watch": Section("The scheduled sync."),
    "cache": Section("What is reused between runs."),
    "paths": Section("Where runtime artifacts live."),
    "output": Section("What a run prints."),
    # The pre-``ai:`` spelling. Still read, and still the place a 0.1 install
    # keeps its key, so it maps forward rather than being refused.
    "openai": Section("", status=DEPRECATED, replacement="ai"),
    # The second destination, deleted in 1.0. Kept here so a config that still
    # names it loads with a warning instead of refusing to start.
    "apple_notes": Section(
        "",
        status=REMOVED,
        note="Apple Notes is no longer a destination; notes go to 'obsidian:'",
    ),
    # There is one OCR backend, and it is the configured AI provider. This
    # section is kept only so the configs that still name it keep loading.
    "google_vision": Section(
        "",
        status=REMOVED,
        note="pages are read by the provider named in 'ai:'",
    ),
    # The single-destination era's block, normalised away by
    # destinations._apply_legacy_destination. Free-form on purpose: its keys
    # are whichever destination it names, which is also why it has no
    # key-for-key replacement to map onto — the successor is a section named
    # after the destination.
    "destination": Section(
        "",
        status=DEPRECATED,
        note="name the destination's own section instead, e.g. 'obsidian:'",
        free_form=True,
    ),
}


#: Every setting, grouped the way ``config.yml`` groups them.
#:
#: Order matters only for display: a generated file and the ``info`` table both
#: walk this tuple. Resolution does not depend on it, with one documented
#: exception — see :meth:`living_ink.settings.Settings.resolve`, where
#: ``use_ssh`` takes its default from the already-resolved
#: ``preferred_connection`` and the AI key's credential name is composed from
#: the already-resolved provider.
SETTINGS: Tuple[Setting, ...] = (
    # ── Connection ─────────────────────────────────────────────────────────
    Setting(
        field="preferred_connection",
        key="remarkable.preferred_connection",
        kind=CHOICE,
        default=DEFAULT_PREFERRED_CONNECTION,
        help="Which route to the tablet to try first.",
        env="REMARKABLE_PREFERRED_CONNECTION",
        choices=(
            Choice("ssh", "USB cable", flag="--ssh"),
            Choice("cloud", "reMarkable Cloud", flag="--cloud"),
        ),
        exclusive_group="connection",
    ),
    Setting(
        field="use_ssh",
        key="remarkable.use_ssh",
        kind=FLAG,
        default=True,
        help="Whether the USB route may be used at all.",
        env="REMARKABLE_USE_SSH",
        legacy_keys=("use_ssh",),
    ),
    Setting(
        field="ssh_host",
        key="remarkable.ssh_host",
        kind=TEXT,
        default=DEFAULT_SSH_HOST,
        help="Tablet address over USB.",
        env="REMARKABLE_SSH_HOST",
        flag="--ssh-host",
    ),
    Setting(
        field="ssh_user",
        key="remarkable.ssh_user",
        kind=TEXT,
        default=DEFAULT_SSH_USER,
        help="SSH user on the tablet.",
        env="REMARKABLE_SSH_USER",
        flag="--ssh-user",
    ),
    Setting(
        field="ssh_port",
        key="remarkable.ssh_port",
        kind=WHOLE,
        default=DEFAULT_SSH_PORT,
        help="SSH port on the tablet.",
        env="REMARKABLE_SSH_PORT",
        flag="--ssh-port",
    ),
    Setting(
        field="ssh_password",
        key=None,
        kind=SECRET,
        default=None,
        help="The tablet's screen password, shown under Settings → Help.",
        env="REMARKABLE_SSH_PASSWORD",
        store=STORE_CREDENTIALS,
        credential=SSH_PASSWORD,
        secret=True,
    ),
    Setting(
        field="remarkable_token",
        key=None,
        kind=SECRET,
        default=None,
        help="reMarkable Cloud device token, written by pairing rather than typed.",
        env="REMARKABLE_TOKEN",
        store=STORE_CREDENTIALS,
        credential=CLOUD_TOKEN,
        legacy_keys=("remarkable.device_token",),
        secret=True,
    ),
    # ── AI and OCR ─────────────────────────────────────────────────────────
    Setting(
        field="ai_provider",
        key="ai.provider",
        kind=TEXT,
        default=None,
        help="gemini, openai, ollama, groq, openrouter, mistral, together, custom, or none.",
        env="LIVING_INK_AI_PROVIDER",
        flag="--ai-provider",
    ),
    Setting(
        field="ai_model",
        key="ai.model",
        kind=TEXT,
        default=None,
        help="Model name. Empty means the provider's default.",
        env="LIVING_INK_AI_MODEL",
        flag="--ai-model",
        legacy_keys=("openai.model",),
    ),
    Setting(
        field="ai_api_key",
        key=None,
        kind=SECRET,
        default=None,
        help="API key for the selected provider. One is kept per provider.",
        env="LIVING_INK_AI_API_KEY",
        store=STORE_CREDENTIALS,
        # Composed at resolve time from the provider: the key is stored as
        # ``ai.api_key.<provider>`` so switching provider and back does not
        # destroy the first one.
        credential=None,
        legacy_keys=("ai.api_key", "openai.api_key"),
        secret=True,
    ),
    Setting(
        field="ai_base_url",
        key="ai.base_url",
        kind=TEXT,
        default=None,
        help="Endpoint for a self-hosted or otherwise custom provider.",
        env="LIVING_INK_AI_BASE_URL",
        flag="--ai-base-url",
    ),
    Setting(
        field="ai_temperature",
        key="ai.temperature",
        kind=NUMBER,
        default=DEFAULT_AI_TEMPERATURE,
        help="Sampling temperature. Low, because transcription is not a creative task.",
        env="LIVING_INK_AI_TEMPERATURE",
        flag="--ai-temperature",
    ),
    Setting(
        field="ai_language",
        key="ai.language",
        kind=TEXT,
        default=DEFAULT_AI_LANGUAGE,
        help="Language to transcribe in, or 'auto' to let the model infer it.",
        env="LIVING_INK_AI_LANGUAGE",
        flag="--ai-language",
    ),
    Setting(
        field="ai_prompt_dir",
        key="ai.prompt_dir",
        kind=PATH,
        default=None,
        help="Directory holding your own copies of the OCR and cleanup prompts.",
        env="LIVING_INK_AI_PROMPT_DIR",
        flag="--ai-prompt-dir",
    ),
    Setting(
        field="ocr_concurrency",
        key="ocr.concurrency",
        kind=WHOLE,
        default=DEFAULT_OCR_CONCURRENCY,
        help="Pages read in parallel. This is the rate-limit knob.",
        env="SYNC_OCR_CONCURRENCY",
        flag="--ocr-concurrency",
        legacy_keys=("sync.ocr_concurrency",),
    ),
    # ── What to sync ───────────────────────────────────────────────────────
    Setting(
        field="sync_pdfs",
        key="sync.sync_pdfs",
        kind=FLAG,
        default=False,
        help="Sync annotated PDFs alongside notebooks.",
        env="SYNC_PDFS",
        flag="--pdf",
    ),
    Setting(
        field="sync_epubs",
        key="sync.sync_epubs",
        kind=FLAG,
        default=False,
        help="Sync annotated EPUBs alongside notebooks.",
        env="SYNC_EPUBS",
        flag="--epub",
    ),
    Setting(
        field="sync_tags",
        key="sync.tags",
        kind=LIST,
        default=(),
        help="Only sync documents carrying one of these tablet tags.",
        env="SYNC_TAGS",
        flag="--tag",
    ),
    Setting(
        field="sync_exclude",
        key="sync.exclude",
        kind=LIST,
        default=DEFAULT_SYNC_EXCLUDE,
        help="Tablet folders never synced.",
        env="SYNC_EXCLUDE",
        flag="--exclude",
    ),
    Setting(
        field="skip_empty",
        key="sync.skip_empty",
        kind=FLAG,
        default=False,
        help="Do not publish a document whose pages all transcribe to nothing.",
        env="SYNC_SKIP_EMPTY",
        flag="--skip-empty",
    ),
    Setting(
        field="max_notebooks_per_run",
        key="sync.limit",
        kind=WHOLE,
        default=DEFAULT_MAX_NOTEBOOKS_PER_RUN,
        help="Most documents to process in one run.",
        env="SYNC_MAX_NOTEBOOKS",
        flag="--limit",
        legacy_keys=("sync.max_notebooks_per_run",),
    ),
    Setting(
        field="prune",
        key="sync.prune",
        kind=FLAG,
        default=False,
        help="Delete published notes whose document is gone from the tablet.",
        env="SYNC_PRUNE",
        flag="--prune",
    ),
    # ── Destinations ───────────────────────────────────────────────────────
    #
    # The legacy dict form of the retired ``destination:`` key both selected a
    # destination and configured it (``destination: {type: obsidian,
    # vault_path: …}``), so each setting it could carry names that spelling as
    # a legacy key. Otherwise the destination would have to read the raw
    # section to find a value the settings never saw.
    Setting(
        field="obsidian_enabled",
        key="obsidian.enabled",
        kind=FLAG,
        default=True,
        help="Publish to an Obsidian vault.",
        env="LIVING_INK_OBSIDIAN_ENABLED",
    ),
    Setting(
        field="obsidian_vault_path",
        key="obsidian.vault_path",
        kind=PATH,
        default=None,
        help="Path to the vault this run publishes into.",
        env="LIVING_INK_OBSIDIAN_VAULT_PATH",
        flag="--destination",
        legacy_keys=("destination.vault_path",),
    ),
    Setting(
        field="obsidian_root_folder",
        key="obsidian.root_folder",
        kind=TEXT,
        default=None,
        help="Folder inside the vault that everything lands under.",
        env="LIVING_INK_OBSIDIAN_ROOT_FOLDER",
        flag="--destination-folder",
        legacy_keys=("destination.root_folder",),
    ),
    Setting(
        field="obsidian_mirror_folders",
        key="obsidian.mirror_folders",
        kind=FLAG,
        default=True,
        help="Reproduce the tablet's folder tree inside the vault.",
        env="LIVING_INK_OBSIDIAN_MIRROR_FOLDERS",
        flag="--mirror-folders",
        negated="--no-mirror-folders",
        legacy_keys=("destination.mirror_folders",),
    ),
    Setting(
        field="obsidian_attachments_folder",
        key="obsidian.attachments_folder",
        kind=TEXT,
        default=DEFAULT_ATTACHMENTS_FOLDER,
        help="Subfolder page images land in. Empty means beside the note.",
        env="LIVING_INK_OBSIDIAN_ATTACHMENTS_FOLDER",
        flag="--attachments-folder",
        legacy_keys=("destination.attachments_folder",),
    ),
    Setting(
        field="obsidian_embed_images",
        key="obsidian.embed_images",
        kind=FLAG,
        default=True,
        help="Embed the page images alongside the text, rather than text only.",
        env="LIVING_INK_OBSIDIAN_EMBED_IMAGES",
        flag="--embed-images",
        negated="--no-embed-images",
    ),
    # ── Watch ──────────────────────────────────────────────────────────────
    #
    # No flags, and that is the one exception to "every setting has one": a
    # supervised process is restarted without its arguments, so a flag would
    # stop applying without saying so. Scheduled runs are configured; one-off
    # runs are ``sync``.
    Setting(
        field="watch_enabled",
        key="watch.enabled",
        kind=FLAG,
        default=False,
        help="Whether a scheduled sync is installed.",
        env="LIVING_INK_WATCH_ENABLED",
        commands=(),
    ),
    Setting(
        field="watch_schedule",
        key="watch.schedule",
        kind=TEXT,
        default=None,
        help="When the scheduled sync runs, as a cron expression.",
        env="LIVING_INK_WATCH_SCHEDULE",
        commands=(),
    ),
    Setting(
        field="watch_timezone",
        key="watch.timezone",
        kind=TEXT,
        default=None,
        help="Timezone the schedule is read in. The host's zone when unset.",
        env="LIVING_INK_WATCH_TIMEZONE",
        commands=(),
    ),
    # ── Advanced ───────────────────────────────────────────────────────────
    Setting(
        field="transcript_cache",
        key="cache.transcripts",
        kind=FLAG,
        default=DEFAULT_TRANSCRIPT_CACHE,
        help="Reuse transcriptions across runs. This is what makes a repeat sync free.",
        env="SYNC_TRANSCRIPT_CACHE",
        negated="--no-transcript-cache",
        legacy_keys=("sync.transcript_cache",),
    ),
    Setting(
        field="render_cache",
        key="cache.renders",
        kind=FLAG,
        default=DEFAULT_RENDER_CACHE,
        help="Reuse rendered page images across runs.",
        env="SYNC_RENDER_CACHE",
        negated="--no-render-cache",
        legacy_keys=("sync.render_cache",),
    ),
    Setting(
        field="cache_max_age_days",
        key="cache.max_age_days",
        kind=WHOLE,
        default=DEFAULT_CACHE_MAX_AGE_DAYS,
        help="Idle age at which a cached page is pruned.",
        env="SYNC_CACHE_MAX_AGE_DAYS",
        legacy_keys=("sync.cache_max_age_days",),
    ),
    Setting(
        field="data_dir",
        key="paths.data_dir",
        kind=PATH,
        default=None,
        help="Where runtime artifacts live. Defaults to ~/.local/share/living-ink.",
        env="LIVING_INK_DATA_DIR",
        flag="--data-dir",
    ),
    Setting(
        field="verbosity",
        key="output.verbosity",
        kind=CHOICE,
        default=DEFAULT_VERBOSITY,
        help="How much a run prints.",
        env="LIVING_INK_VERBOSITY",
        choices=(
            Choice("quiet", "The run report and nothing else", flag="--quiet"),
            Choice("normal", "Three lines per document"),
            Choice("verbose", "A line per page", flag="--verbose"),
        ),
        exclusive_group="verbosity",
    ),
    Setting(
        field="output_json",
        key="output.json",
        kind=FLAG,
        default=False,
        help="Print the run report as one JSON document instead of a table.",
        env="LIVING_INK_OUTPUT_JSON",
        flag="--json",
    ),
    # ── No config key ──────────────────────────────────────────────────────
    #
    # Both shape a run and neither has ever been reachable from config.yml.
    # They are declared so that ``info`` reports them: a setting that changes
    # what gets rendered or whether text is cleaned, and that nothing can
    # print, is one nobody can debug.
    Setting(
        field="repair_enabled",
        key=None,
        kind=FLAG,
        default=True,
        help="Run the AI cleanup pass. Off is equivalent to ai.provider = none.",
        env="ENABLE_REPAIR",
        store=STORE_ENV_ONLY,
    ),
    Setting(
        field="render_background",
        key=None,
        kind=TEXT,
        default=DEFAULT_RENDER_BACKGROUND,
        help="Paper colour behind a rendered page. Part of the render cache key.",
        env="REMARKABLE_BACKGROUND_COLOR",
        store=STORE_ENV_ONLY,
    ),
)


# ---------------------------------------------------------------------------
# Derived lookups
# ---------------------------------------------------------------------------


def _by_field() -> Dict[str, Setting]:
    """Index :data:`SETTINGS` by field name, refusing a duplicate.

    Returns:
        Field name to setting.

    Raises:
        ValueError: If two settings claim the same field, which would make one
            of them silently unresolvable.
    """
    index: Dict[str, Setting] = {}
    for setting in SETTINGS:
        if setting.field in index:
            raise ValueError(f"Duplicate setting field: {setting.field}")
        index[setting.field] = setting
    return index


#: Every setting by its :class:`Settings` attribute name.
BY_FIELD: Dict[str, Setting] = _by_field()


def _section_keys() -> Dict[str, Dict[str, Setting]]:
    """Group settings by config section, including every legacy spelling.

    A legacy path is indexed under the section it is *written* in, not the one
    its successor lives in, because that is how it appears in a user's file and
    therefore how the validator meets it.

    Returns:
        Section name to leaf key to the setting that owns it. Bare top-level
        keys are grouped under ``""``.
    """
    grouped: Dict[str, Dict[str, Setting]] = {name: {} for name in SECTIONS}
    grouped.setdefault("", {})

    for setting in SETTINGS:
        paths = list(setting.legacy_keys)
        if setting.key is not None:
            paths.append(setting.key)
        for path in paths:
            head, dot, tail = path.partition(".")
            section, leaf = (head, tail) if dot else ("", head)
            grouped.setdefault(section, {})[leaf] = setting
    return grouped


#: Section name to the keys it accepts, current and legacy alike.
SECTION_KEYS: Dict[str, Dict[str, Setting]] = _section_keys()

#: Dotted legacy path to the setting that superseded it.
LEGACY_KEYS: Dict[str, Setting] = {
    path: setting for setting in SETTINGS for path in setting.legacy_keys
}


def section_status(name: str) -> Section:
    """Return the declared section, or an active placeholder for an implied one.

    A section can exist purely because a setting names it, without an entry in
    :data:`SECTIONS`; that is not an error, it just has no help text.

    Args:
        name: Section name as written in the config.

    Returns:
        The :class:`Section` for that name.
    """
    return SECTIONS.get(name, Section())
