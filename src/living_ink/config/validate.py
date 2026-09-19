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

#: What a ``config.yml`` may contain.
#:
#: A mapping means a section and lists the keys that section accepts; a marker
#: means a bare top-level key. Anything not named here is rejected, so this
#: table has to stay complete: a key some module reads but this forgets is a
#: working config that no longer loads. Every legacy spelling still honoured
#: elsewhere in the package therefore appears here too. Sections belonging to a
#: registered destination are exempt from key checking entirely — see the
#: ``extra_sections`` argument to :func:`validate_config`.
CONFIG_SCHEMA: Dict[str, Union[str, Dict[str, str]]] = {
    "schema_version": WHOLE,
    # The pre-``remarkable:`` spelling, still read by Settings._config_values.
    "use_ssh": FLAG,
    "ai": {
        "provider": TEXT,
        "api_key": TEXT,
        "model": TEXT,
        "base_url": TEXT,
        "temperature": NUMBER,
    },
    "openai": {
        "api_key": TEXT,
        "model": TEXT,
    },
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
    "google_vision": {
        "credentials_path": TEXT,
        "credentials_json": TEXT,
    },
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
    # are whichever destination it names.
    "destination": {},
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


def _check_section(name: str, section: Any, allowed: Dict[str, str]) -> List[ConfigProblem]:
    """Validate one section of a config against the keys it accepts.

    Args:
        name: The section's name, used to build the dotted path in a problem.
        section: The parsed value found under that name.
        allowed: Key name to kind marker. An empty mapping means the section is
            free-form and only its shape is checked.

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
    if not allowed:
        return problems

    for key, value in section.items():
        path = f"{name}.{key}"
        kind = allowed.get(str(key))
        if kind is None:
            problems.append(
                ConfigProblem(ERROR, path, "unknown key", _did_you_mean(str(key), allowed))
            )
        elif value is not None and not _reads_as(value, kind):
            problems.append(ConfigProblem(ERROR, path, f"expected {kind}, found {value!r}"))
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
        elif isinstance(schema, dict):
            problems.extend(_check_section(key, value, schema))
        elif value is not None and not _reads_as(value, schema):
            problems.append(ConfigProblem(ERROR, key, f"expected {schema}, found {value!r}"))

    return problems


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
