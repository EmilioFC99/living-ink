"""What a ``config.yml`` may contain, and what to do when it does not.

Holds the declarative schema every config is checked against on load
(:data:`CONFIG_SCHEMA`, :func:`validate_config`). A misspelled key used to
parse cleanly and be ignored, which surfaced much later as "0 notebooks
published" and no reason given.
"""

import difflib
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union


class ConfigurationMissing(Exception):
    """Raised when the pipeline cannot run because configuration is absent or invalid.

    Carries the remedy rather than acting on it: deciding whether to prompt the
    user, launch the wizard or just exit is the CLI's job, so the pipeline
    signals the problem and stays out of the interaction.

    Attributes:
        hint: Human-readable next step, e.g. "run: living-ink setup".
    """

    def __init__(self, message: str, hint: str = "run: living-ink setup"):
        """Initialize the error.

        Args:
            message: What is wrong with the configuration.
            hint: The suggested remedy, shown to the user by the CLI.
        """
        super().__init__(message)
        self.hint = hint


# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------

#: Version of the ``config.yml`` shape this build understands.
#:
#: Written into every config the setup wizard generates. It is not a
#: requirement — a file without one is assumed to be version 1, because every
#: config written before this existed is one — but it is the hook a future
#: breaking change branches on, and it lets an old build refuse a file written
#: by a newer one instead of silently ignoring half of it.
SCHEMA_VERSION = 1

#: Marker for "any scalar is fine here", used for free-text values.
TEXT = "text"
#: Marker for "must read as a whole number".
WHOLE = "whole number"
#: Marker for "must read as true or false".
FLAG = "true/false"
#: Marker for "must read as a number, decimals allowed".
NUMBER = "number"

_TRUTHY = {"1", "true", "yes", "on"}
_FALSEY = {"0", "false", "no", "off"}

#: The setting is current. Validate it, read it, say nothing.
ACTIVE = "active"
#: The setting still works but is on its way out.
#:
#: Warned about by name, and — when it has a key-for-key ``replacement`` —
#: copied onto that replacement in memory at load time, so the rest of the
#: package only ever has to know the current spelling.
DEPRECATED = "deprecated"
#: The setting is gone. Warned about, then dropped before anything reads it.
#:
#: This is the difference between "your config does nothing" and "your config
#: does not load". An unrecognised section is a hard :data:`ERROR` and aborts
#: the run, so a section cannot simply be deleted from the schema on the day
#: its code is deleted — every config still naming it would stop working. It
#: is marked ``removed`` instead, and disappears from the schema a release
#: later, once the warning has had time to be seen.
REMOVED = "removed"


@dataclass(frozen=True)
class Key:
    """One config key that is not simply active.

    Active keys are written as a bare kind marker; this wrapper exists for the
    ones carrying a status, so the schema stays readable at a glance and the
    exceptions are the only thing that looks exceptional.

    Attributes:
        kind: One of :data:`TEXT`, :data:`WHOLE`, :data:`FLAG`, :data:`NUMBER`.
        status: :data:`ACTIVE`, :data:`DEPRECATED` or :data:`REMOVED`.
        replacement: Dotted path of the key that supersedes this one, when the
            mapping is key-for-key. ``None`` when there is no single successor.
        note: One clause appended to the warning, for anything the status and
            the replacement do not already say.
    """

    kind: str
    status: str = ACTIVE
    replacement: Optional[str] = None
    note: str = ""


@dataclass(frozen=True)
class Section:
    """One config section that is not simply active.

    As with :class:`Key`, an active section is written as a bare mapping and
    only the ones carrying a status are wrapped.

    Attributes:
        keys: Key name to kind marker or :class:`Key`. Empty means the section
            is free-form and only its shape is checked.
        status: :data:`ACTIVE`, :data:`DEPRECATED` or :data:`REMOVED`.
        replacement: Name of the section that supersedes this one, or ``None``.
        note: One clause appended to the warning.
    """

    keys: Dict[str, Union[str, Key]]
    status: str = ACTIVE
    replacement: Optional[str] = None
    note: str = ""


#: What a ``config.yml`` may contain.
#:
#: A mapping means a section and lists the keys that section accepts; a marker
#: means a bare top-level key. Anything not named here is rejected, so this
#: table has to stay complete: a key some module reads but this forgets is a
#: working config that no longer loads. Every legacy spelling still honoured
#: elsewhere in the package therefore appears here too. Sections belonging to a
#: registered destination are exempt from key checking entirely — see the
#: ``extra_sections`` argument to :func:`validate_config`.
#:
#: A section or key on its way out is wrapped in :class:`Section` or
#: :class:`Key` and carries a status; everything written bare is
#: :data:`ACTIVE`. That wrapper is what lets a removal ship as a warning
#: instead of an abort — see :data:`REMOVED` and :func:`apply_status`.
CONFIG_SCHEMA: Dict[str, Union[str, Key, Dict[str, Union[str, Key]], Section]] = {
    "schema_version": WHOLE,
    "use_ssh": Key(FLAG, DEPRECATED, replacement="remarkable.use_ssh"),
    "ai": {
        "provider": TEXT,
        "api_key": TEXT,
        "model": TEXT,
        "base_url": TEXT,
        "temperature": NUMBER,
    },
    # The pre-``ai:`` spelling. Still read, and still the place a 0.1 install
    # keeps its key, so it maps forward rather than being refused.
    "openai": Section(
        {
            "api_key": Key(TEXT, DEPRECATED, replacement="ai.api_key"),
            "model": Key(TEXT, DEPRECATED, replacement="ai.model"),
        },
        status=DEPRECATED,
        replacement="ai",
    ),
    "remarkable": {
        "device_token": TEXT,
        "preferred_connection": TEXT,
        "use_ssh": FLAG,
        "ssh_host": TEXT,
        "ssh_user": TEXT,
        "ssh_port": WHOLE,
    },
    "sync": {
        "sync_pdfs": FLAG,
        "sync_epubs": FLAG,
        "max_notebooks_per_run": WHOLE,
        "ocr_concurrency": WHOLE,
        "transcript_cache": FLAG,
        "render_cache": FLAG,
        "cache_max_age_days": WHOLE,
    },
    # There is one OCR backend, and it is the configured AI provider. This
    # section is kept only so the configs that still name it keep loading.
    "google_vision": Section(
        {
            "credentials_path": TEXT,
            "credentials_json": TEXT,
        },
        status=REMOVED,
        note="pages are read by the provider named in 'ai:'",
    ),
    "obsidian": {
        "enabled": FLAG,
        "vault_path": TEXT,
        "root_folder": TEXT,
        "mirror_folders": FLAG,
        "attachments_folder": TEXT,
    },
    "apple_notes": {
        "enabled": FLAG,
        "folder_name": TEXT,
    },
    # The single-destination era's block, normalised away by
    # destinations._apply_legacy_destination. Free-form on purpose: its keys
    # are whichever destination it names, which is also why it has no
    # key-for-key replacement to map onto — the successor is a section named
    # after the destination.
    "destination": Section(
        {},
        status=DEPRECATED,
        note="name the destination's own section instead, e.g. 'obsidian:'",
    ),
}

#: The run cannot proceed: the config does not say what its author meant.
ERROR = "error"
#: The config is understood, but names something on its way out.
#:
#: Reserved for deprecations — a key that is still read and still works, but
#: will stop being read in a future release. It is deliberately *not* what an
#: unrecognised key gets: an unrecognised key is not understood at all, and
#: there is no honest way to both warn about it and act on it.
WARNING = "warning"


@dataclass(frozen=True)
class ConfigProblem:
    """One thing wrong with a ``config.yml``.

    Attributes:
        level: :data:`ERROR` (the run cannot proceed) or :data:`WARNING`
            (the setting still works but is on its way out).
        path: Dotted location of the offending key, e.g. ``obsidian.vault_path``.
        message: What is wrong, in one sentence.
        hint: The suggested fix, or an empty string when there is nothing
            better to say than the message itself.
    """

    level: str
    path: str
    message: str
    hint: str = ""

    def describe(self) -> str:
        """Render the problem as a single line for a console or a log.

        Returns:
            ``"obsidian.vault_pat: unknown key (did you mean vault_path?)"``.
        """
        text = f"{self.path}: {self.message}"
        return f"{text} ({self.hint})" if self.hint else text


def _reads_as(value: Any, kind: str) -> bool:
    """Report whether a YAML value can be used as the declared kind.

    Deliberately permissive about spelling and strict about meaning. YAML gives
    no way to say "this 22 is a string", and Settings already coerces, so
    ``ssh_port: "22"`` is accepted. ``max_notebooks_per_run: many`` is not,
    because nothing downstream can turn that into a number and the run would
    quietly fall back to the default instead.

    Args:
        value: The parsed YAML value.
        kind: One of :data:`TEXT`, :data:`WHOLE`, :data:`FLAG`, :data:`NUMBER`.

    Returns:
        True when the value is usable as that kind.
    """
    if isinstance(value, (dict, list)):
        return False

    if kind == TEXT:
        return True

    if kind == FLAG:
        if isinstance(value, bool):
            return True
        return str(value).strip().lower() in _TRUTHY | _FALSEY

    # A bool is an int in Python, but ``ssh_port: true`` is a mistake, not a
    # port, so it is rejected before the numeric checks see it.
    if isinstance(value, bool):
        return False

    if kind == WHOLE:
        if isinstance(value, int):
            return True
        try:
            int(str(value).strip())
        except (TypeError, ValueError):
            return False
        return True

    if kind == NUMBER:
        if isinstance(value, (int, float)):
            return True
        try:
            float(str(value).strip())
        except (TypeError, ValueError):
            return False
        return True

    return True


def _status_problem(path: str, entry: Union[Key, Section]) -> Optional[ConfigProblem]:
    """Report that a section or key carries a status worth mentioning.

    Args:
        path: Dotted location of the section or key, as written in the file.
        entry: The schema entry found for it.

    Returns:
        A :data:`WARNING` problem for a deprecated or removed entry, or None
        when the entry is active and there is nothing to say.
    """
    if entry.status == DEPRECATED:
        message = "deprecated"
        hint = f"use {entry.replacement}" if entry.replacement else ""
    elif entry.status == REMOVED:
        message = "no longer used, and ignored"
        hint = f"delete it; {entry.note}" if entry.note else "delete it"
    else:
        return None

    if entry.status == DEPRECATED and entry.note:
        hint = f"{hint}; {entry.note}" if hint else entry.note

    return ConfigProblem(WARNING, path, message, hint)


def _did_you_mean(key: str, candidates: Iterable[str]) -> str:
    """Suggest the schema key a misspelling was probably reaching for.

    Args:
        key: The unrecognised key as written.
        candidates: The keys that would have been accepted in its place.

    Returns:
        ``"did you mean obsidian?"``, or an empty string when nothing is close
        enough that guessing would help more than it misleads.
    """
    matches = difflib.get_close_matches(key, list(candidates), n=1, cutoff=0.7)
    return f"did you mean {matches[0]}?" if matches else ""


def _check_section(
    name: str,
    section: Any,
    allowed: Dict[str, Union[str, Key]],
    check_keys: bool = True,
) -> List[ConfigProblem]:
    """Validate one section of a config against the keys it accepts.

    Args:
        name: The section's name, used to build the dotted path in a problem.
        section: The parsed value found under that name.
        allowed: Key name to kind marker or :class:`Key`. An empty mapping
            means the section is free-form and only its shape is checked.
        check_keys: Whether to police the contents at all. False for a
            :data:`REMOVED` section, whose keys are about to be dropped: naming
            them one by one adds noise to a warning that already says the whole
            section is ignored, and a stray key inside a dead section must not
            be the thing that stops the run.

    Returns:
        Every problem found inside this section, in file order.
    """
    problems: List[ConfigProblem] = []

    if section is None:
        return problems
    if not isinstance(section, dict):
        return [
            ConfigProblem(
                ERROR,
                name,
                f"expected a section of settings, found {type(section).__name__}",
                "indent its settings underneath it",
            )
        ]
    if not allowed or not check_keys:
        return problems

    for key, value in section.items():
        path = f"{name}.{key}"
        entry = allowed.get(str(key))
        if entry is None:
            problems.append(
                ConfigProblem(ERROR, path, "unknown key", _did_you_mean(str(key), allowed))
            )
            continue

        kind = entry.kind if isinstance(entry, Key) else entry
        if value is not None and not _reads_as(value, kind):
            problems.append(ConfigProblem(ERROR, path, f"expected {kind}, found {value!r}"))
            continue

        # Only after the value is known to be readable: telling someone a key
        # is deprecated and then not saying it is also unparseable would send
        # them to rename it and hit the same wall again.
        if isinstance(entry, Key):
            status = _status_problem(path, entry)
            if status is not None:
                problems.append(status)
    return problems


def validate_config(
    config: Optional[Dict[str, Any]], extra_sections: Sequence[str] = ()
) -> List[ConfigProblem]:
    """Check a parsed ``config.yml`` against :data:`CONFIG_SCHEMA`.

    Anything the schema does not recognise is an error, the way ``gcloud``
    rejects ``--quyery`` rather than running without it. A config is a
    statement of intent, and a key Living Ink cannot read is intent it cannot
    honour; continuing would mean doing something other than what the file
    says, then reporting success. The sympathetic-looking alternative — warn
    and carry on — is how ``obsidain:`` turns into "0 notebooks published" with
    the reason ten thousand log lines back, which is the failure this exists to
    end.

    Being strict obliges the schema to be complete, so the two ways a config
    can legitimately hold a key this build does not define both have an
    explicit route through: ``extra_sections`` for a destination registered at
    runtime, and ``schema_version`` for a file written by a newer Living Ink,
    which is refused as a version rather than misread as a pile of typos.

    Args:
        config: Parsed config contents. ``None`` and ``{}`` are valid — every
            setting has a default.
        extra_sections: Section names that exist but whose keys this function
            must not police, such as the config section of a destination
            registered at runtime. Their shape is still checked.

    Returns:
        Every problem found, in file order. An empty list means the config is
        usable as written.
    """
    if not config:
        return []
    if not isinstance(config, dict):
        return [
            ConfigProblem(
                ERROR,
                "config.yml",
                f"expected a mapping of sections, found {type(config).__name__}",
            )
        ]

    problems: List[ConfigProblem] = []
    known = set(CONFIG_SCHEMA) | set(extra_sections)

    declared = config.get("schema_version")
    if declared is not None and _reads_as(declared, WHOLE) and int(declared) > SCHEMA_VERSION:
        problems.append(
            ConfigProblem(
                ERROR,
                "schema_version",
                f"config is version {int(declared)}, this build understands {SCHEMA_VERSION}",
                "upgrade Living Ink",
            )
        )

    for name, value in config.items():
        key = str(name)
        schema = CONFIG_SCHEMA.get(key)

        if schema is None:
            if key in extra_sections:
                problems.extend(_check_section(key, value, {}))
            else:
                problems.append(
                    ConfigProblem(ERROR, key, "unknown section", _did_you_mean(key, known))
                )
        elif isinstance(schema, Section):
            problems.extend(
                _check_section(key, value, schema.keys, check_keys=schema.status != REMOVED)
            )
            status = _status_problem(key, schema)
            if status is not None:
                problems.append(status)
        elif isinstance(schema, dict):
            problems.extend(_check_section(key, value, schema))
        elif isinstance(schema, Key):
            if value is not None and not _reads_as(value, schema.kind):
                problems.append(
                    ConfigProblem(ERROR, key, f"expected {schema.kind}, found {value!r}")
                )
            else:
                status = _status_problem(key, schema)
                if status is not None:
                    problems.append(status)
        elif value is not None and not _reads_as(value, schema):
            problems.append(ConfigProblem(ERROR, key, f"expected {schema}, found {value!r}"))

    return problems


def _adopt(config: Dict[str, Any], path: str, value: Any) -> None:
    """Copy a deprecated key's value onto its current spelling, if free.

    Additive on purpose: the old key is left exactly where the user wrote it,
    and the new one is only filled when nothing already occupies it. A config
    naming both is stating a preference, and the current spelling is the one it
    means.

    Args:
        config: The config being rewritten, modified in place.
        path: Dotted path of the replacement, e.g. ``remarkable.use_ssh``.
        value: The value found under the deprecated key.
    """
    section_name, _, key = path.partition(".")
    if not key:
        return

    existing = config.get(section_name)
    if existing is not None and not isinstance(existing, dict):
        return

    section = dict(existing) if isinstance(existing, dict) else {}
    if section.get(key) is not None:
        return

    section[key] = value
    config[section_name] = section


def apply_status(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return the config the rest of the package should read.

    The schema's statuses are declarations; this is where they take effect.
    :data:`REMOVED` sections and keys are dropped, so nothing downstream has to
    remember that a dead setting might still be sitting in the file, and
    :data:`DEPRECATED` keys are copied onto their current spelling, so nothing
    downstream has to know the old one. Both happen in memory.

    **No config file is ever rewritten.** The alternative — migrating the file
    in place behind a backup — loses the user's comments and ordering, and a
    backup is not a mitigation for that. The file stays byte-for-byte as it was
    written; ``living-ink setup`` is what rewrites it, when the user asks.

    Warnings are not produced here. :func:`validate_config` has already seen
    the file as written and reported every status it found; doing it again from
    the transformed copy would mean either warning twice or warning about
    something that is no longer there.

    Args:
        config: Parsed ``config.yml`` contents, left unmodified.

    Returns:
        A new dictionary. Sections are copied only where something changed.
    """
    if not isinstance(config, dict):
        return {}

    result: Dict[str, Any] = dict(config)

    for name in list(result):
        schema = CONFIG_SCHEMA.get(str(name))

        if isinstance(schema, Key):
            if schema.status == REMOVED:
                del result[name]
            elif schema.status == DEPRECATED and schema.replacement:
                _adopt(result, schema.replacement, result[name])
            continue

        if not isinstance(schema, Section):
            continue

        if schema.status == REMOVED:
            del result[name]
            continue

        section = result.get(name)
        if not isinstance(section, dict):
            continue

        updated = dict(section)
        changed = False
        for key in list(updated):
            entry = schema.keys.get(str(key))
            if not isinstance(entry, Key):
                continue
            if entry.status == REMOVED:
                del updated[key]
                changed = True
            elif entry.status == DEPRECATED and entry.replacement:
                _adopt(result, entry.replacement, updated[key])
        if changed:
            result[name] = updated

    return result


def split_problems(
    problems: Sequence[ConfigProblem],
) -> Tuple[List[ConfigProblem], List[ConfigProblem]]:
    """Separate the problems that stop a run from the ones that do not.

    Args:
        problems: The output of :func:`validate_config`.

    Returns:
        ``(errors, warnings)``, each in the order they were reported.
    """
    return (
        [p for p in problems if p.level == ERROR],
        [p for p in problems if p.level == WARNING],
    )
