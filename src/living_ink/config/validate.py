"""What a ``config.yml`` may contain, and what to do when it does not.

Checks a parsed config against :data:`living_ink.config.schema.SETTINGS`
(:func:`validate_config`) and then hands the rest of the package the version of
it they should read (:func:`apply_status`). A misspelled key used to parse
cleanly and be ignored, which surfaced much later as "0 notebooks published"
and no reason given.

The schema itself lives in :mod:`living_ink.config.schema`. Nothing here
decides what a setting is; it only decides what to do about one.
"""

import difflib
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from living_ink.config.schema import (
    ACTIVE,
    CHOICE,
    DEPRECATED,
    FLAG,
    LEGACY_KEYS,
    LIST,
    NUMBER,
    PATH,
    REMOVED,
    SECRET,
    SECTION_KEYS,
    SECTIONS,
    SETTINGS,
    TEXT,
    WHOLE,
    Section,
    Setting,
    section_status,
)


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


#: Version of the ``config.yml`` shape this build understands.
#:
#: Written into every config the setup wizard generates. It is not a
#: requirement — a file without one is assumed to be version 1, because every
#: config written before this existed is one — but it is the hook a future
#: breaking change branches on, and it lets an old build refuse a file written
#: by a newer one instead of silently ignoring half of it.
SCHEMA_VERSION = 1

_TRUTHY = {"1", "true", "yes", "on"}
_FALSEY = {"0", "false", "no", "off"}

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


def reads_as(value: Any, kind: str) -> bool:
    """Report whether a YAML value can be used as the declared kind.

    Deliberately permissive about spelling and strict about meaning. YAML gives
    no way to say "this 22 is a string", and Settings already coerces, so
    ``ssh_port: "22"`` is accepted. ``limit: many`` is not, because nothing
    downstream can turn that into a number and the run would quietly fall back
    to the default instead.

    Args:
        value: The parsed YAML value.
        kind: A kind marker from :mod:`living_ink.config.schema`.

    Returns:
        True when the value is usable as that kind.
    """
    if kind == LIST:
        # A list is the natural spelling, but an environment variable has no
        # way to say "several", so a comma-separated string reads as one too.
        return isinstance(value, (list, tuple)) or isinstance(value, str)

    if isinstance(value, (dict, list)):
        return False

    if kind in (TEXT, PATH, SECRET, CHOICE):
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


def _value_problem(path: str, setting: Setting, value: Any) -> Optional[ConfigProblem]:
    """Report that a value cannot be used as the setting it was written under.

    Args:
        path: Dotted location of the key, as written in the file.
        setting: The schema entry found for it.
        value: The parsed YAML value. ``None`` means the key was written with
            nothing after it, which is "unset", not "mistyped".

    Returns:
        An :data:`ERROR` problem, or None when the value is readable.
    """
    if value is None:
        return None

    if not reads_as(value, setting.kind):
        return ConfigProblem(ERROR, path, f"expected {setting.kind}, found {value!r}")

    if setting.kind == CHOICE and setting.choices:
        allowed = [choice.value for choice in setting.choices]
        if str(value).strip().lower() not in allowed:
            return ConfigProblem(
                ERROR,
                path,
                f"expected one of {', '.join(allowed)}, found {value!r}",
            )

    return None


def _legacy_problem(path: str, setting: Setting) -> ConfigProblem:
    """Report that a key is spelled the way an older release spelled it.

    Args:
        path: The legacy dotted path, as written in the file.
        setting: The setting that superseded it.

    Returns:
        A :data:`WARNING` naming the current spelling, or — for a credential,
        which has no spelling in ``config.yml`` at all — naming the command
        that stores it properly.
    """
    if setting.key is not None:
        return ConfigProblem(WARNING, path, "deprecated", f"use {setting.key}")
    return ConfigProblem(
        WARNING,
        path,
        "deprecated",
        "secrets are stored outside config.yml; run: living-ink setup",
    )


def _status_problem(path: str, section: Section) -> Optional[ConfigProblem]:
    """Report that a section carries a status worth mentioning.

    Args:
        path: The section's name, as written in the file.
        section: The schema entry found for it.

    Returns:
        A :data:`WARNING` problem for a deprecated or removed section, or None
        when the section is active and there is nothing to say.
    """
    if section.status == DEPRECATED:
        message = "deprecated"
        hint = f"use {section.replacement}" if section.replacement else ""
        if section.note:
            hint = f"{hint}; {section.note}" if hint else section.note
    elif section.status == REMOVED:
        message = "no longer used, and ignored"
        hint = f"delete it; {section.note}" if section.note else "delete it"
    else:
        return None

    return ConfigProblem(WARNING, path, message, hint)


def _retired_problem(path: str, setting: Setting) -> Optional[ConfigProblem]:
    """Report that a key carries a status of its own worth mentioning.

    The section-level counterpart of this, :func:`_status_problem`, can only
    retire a whole section, which is why ``google_vision:`` could go and a
    single key inside a surviving section could not. A key is the smaller unit
    and the more common one: ``sync.types`` replaces two booleans without the
    ``sync:`` section going anywhere.

    Nothing is guessed at. A key that was merely *renamed* never reaches here —
    that is :attr:`Setting.legacy_keys`, and its value is copied forward. This
    is the case where the successor holds a different shape of answer, so the
    only honest thing to do is name it and let the user write it.

    Args:
        path: Dotted location of the key, as written in the file.
        setting: The schema entry found for it.

    Returns:
        A :data:`WARNING` problem for a deprecated or retired key, or None.
    """
    if setting.status == DEPRECATED:
        message = "deprecated"
        hint = f"use {setting.replacement}" if setting.replacement else ""
    elif setting.status == REMOVED:
        message = "no longer used, and ignored"
        hint = f"use {setting.replacement} instead" if setting.replacement else "delete it"
    else:
        return None

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


def _current_leaves(section: str) -> List[str]:
    """List the keys a section accepts under their current spelling.

    A suggestion must not point at a name that is itself deprecated, so the
    legacy spellings this section still reads are excluded.

    Args:
        section: Section name, or ``""`` for bare top-level keys.

    Returns:
        Leaf key names, unordered.
    """
    return [
        leaf
        for leaf, setting in SECTION_KEYS.get(section, {}).items()
        if setting.key is not None and setting.leaf == leaf and setting.section == section
    ]


def _check_section(name: str, value: Any, check_keys: bool = True) -> List[ConfigProblem]:
    """Validate one section of a config against the keys it accepts.

    Args:
        name: The section's name, used to build the dotted path in a problem.
        value: The parsed value found under that name.
        check_keys: Whether to police the contents at all. False for a
            :data:`REMOVED` or free-form section: naming the keys of a section
            that is about to be discarded adds noise to a warning that already
            says the whole section is ignored, and a stray key inside a dead
            section must not be the thing that stops the run.

    Returns:
        Every problem found inside this section, in file order.
    """
    problems: List[ConfigProblem] = []

    if value is None:
        return problems
    if not isinstance(value, dict):
        return [
            ConfigProblem(
                ERROR,
                name,
                f"expected a section of settings, found {type(value).__name__}",
                "indent its settings underneath it",
            )
        ]
    if not check_keys:
        return problems

    accepted = SECTION_KEYS.get(name, {})
    for key, item in value.items():
        path = f"{name}.{key}"
        setting = accepted.get(str(key))
        if setting is None:
            problems.append(
                ConfigProblem(
                    ERROR, path, "unknown key", _did_you_mean(str(key), _current_leaves(name))
                )
            )
            continue

        problem = _value_problem(path, setting, item)
        if problem is not None:
            problems.append(problem)
            continue

        # Only after the value is known to be readable: telling someone a key
        # is deprecated and then not saying it is also unparseable would send
        # them to rename it and hit the same wall again.
        if path in LEGACY_KEYS:
            problems.append(_legacy_problem(path, setting))
            continue

        # A legacy spelling and a retired key are different things, and a path
        # is never both: the first resolves to a setting that survived under
        # another name, the second to one that did not survive at all.
        retired = _retired_problem(path, setting)
        if retired is not None:
            problems.append(retired)

    return problems


def validate_config(
    config: Optional[Dict[str, Any]], extra_sections: Sequence[str] = ()
) -> List[ConfigProblem]:
    """Check a parsed ``config.yml`` against the schema.

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
    top_level = SECTION_KEYS.get("", {})
    known = set(SECTIONS) | set(top_level) | set(extra_sections) | {"schema_version"}

    declared = config.get("schema_version")
    if declared is not None and reads_as(declared, WHOLE) and int(declared) > SCHEMA_VERSION:
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

        if key == "schema_version":
            if value is not None and not reads_as(value, WHOLE):
                problems.append(ConfigProblem(ERROR, key, f"expected {WHOLE}, found {value!r}"))
            continue

        # A bare top-level key, which today means one spelling that predates
        # its section.
        setting = top_level.get(key)
        if setting is not None:
            problem = _value_problem(key, setting, value)
            if problem is not None:
                problems.append(problem)
            elif key in LEGACY_KEYS:
                problems.append(_legacy_problem(key, setting))
            else:
                retired = _retired_problem(key, setting)
                if retired is not None:
                    problems.append(retired)
            continue

        if key in extra_sections and key not in SECTIONS:
            problems.extend(_check_section(key, value, check_keys=False))
            continue

        if key not in SECTIONS and key not in SECTION_KEYS:
            problems.append(ConfigProblem(ERROR, key, "unknown section", _did_you_mean(key, known)))
            continue

        section = section_status(key)
        check_keys = section.status != REMOVED and not section.free_form
        problems.extend(_check_section(key, value, check_keys=check_keys))
        status = _status_problem(key, section)
        if status is not None:
            problems.append(status)

    return problems


def _adopt(config: Dict[str, Any], path: str, value: Any) -> None:
    """Copy a legacy key's value onto its current spelling, if free.

    Additive on purpose: the old key is left exactly where the user wrote it,
    and the new one is only filled when nothing already occupies it. A config
    naming both is stating a preference, and the current spelling is the one it
    means.

    Args:
        config: The config being rewritten, modified in place.
        path: Dotted path of the current spelling, e.g. ``remarkable.use_ssh``.
        value: The value found under the legacy key.
    """
    section_name, dot, key = path.partition(".")
    if not dot:
        if config.get(section_name) is None:
            config[section_name] = value
        return

    existing = config.get(section_name)
    if existing is not None and not isinstance(existing, dict):
        return

    section = dict(existing) if isinstance(existing, dict) else {}
    if section.get(key) is not None:
        return

    section[key] = value
    config[section_name] = section


def _lookup(config: Dict[str, Any], path: str) -> Any:
    """Read a dotted path out of a parsed config.

    Args:
        config: Parsed config contents.
        path: Dotted path, e.g. ``sync.max_notebooks_per_run``.

    Returns:
        The value found, or None when any level of the path is absent.
    """
    section_name, dot, key = path.partition(".")
    if not dot:
        return config.get(section_name)
    section = config.get(section_name)
    return section.get(key) if isinstance(section, dict) else None


def apply_status(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return the config the rest of the package should read.

    The schema's statuses are declarations; this is where they take effect.
    :data:`REMOVED` sections and :data:`REMOVED` keys are dropped, so nothing
    downstream has to remember that a dead setting might still be sitting in
    the file, and legacy keys are copied onto their current spelling, so
    nothing downstream has to know the old one. All of it happens in memory.

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

    result: Dict[str, Any] = {
        name: dict(value) if isinstance(value, dict) else value for name, value in config.items()
    }

    for name in list(result):
        if section_status(str(name)).status == REMOVED:
            del result[name]

    # Then the retired keys inside the sections that survived. Dropped for the
    # same reason a removed section is: nothing downstream should have to
    # remember that a dead key might still be sitting in the file, and a
    # retired setting has no field on Settings to resolve it onto anyway.
    for setting in SETTINGS:
        if setting.status != REMOVED or setting.key is None:
            continue
        if setting.section == "":
            result.pop(setting.leaf, None)
        elif isinstance(result.get(setting.section), dict):
            result[setting.section].pop(setting.leaf, None)

    # Read from the original so that a legacy key inside a section this loop
    # also writes to cannot be seen half-updated.
    for path, setting in LEGACY_KEYS.items():
        if setting.key is None:
            continue
        value = _lookup(config, path)
        if value is None:
            continue
        if section_status(path.partition(".")[0]).status == REMOVED:
            continue
        _adopt(result, setting.key, value)

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


__all__ = [
    "ACTIVE",
    "DEPRECATED",
    "ERROR",
    "REMOVED",
    "SCHEMA_VERSION",
    "SETTINGS",
    "WARNING",
    "ConfigProblem",
    "ConfigurationMissing",
    "apply_status",
    "reads_as",
    "split_problems",
    "validate_config",
]
