"""Command-line flags, generated from the settings schema.

Every flag ``living-ink`` accepts for a configurable value is declared once, in
:data:`living_ink.config.schema.SETTINGS`, and built from that declaration here.
Before this the parser hand-registered ten of them and the schema declared
twenty-eight, so eighteen settings had a ``flag`` nobody could type: the schema
said ``--ai-model`` existed, ``--help`` did not list it, and argparse rejected
it. A generated parser cannot drift from the thing it is generated from.

Two rules make the generated flags safe to merge with the other layers.

**Every flag defaults to None, without exception.** :meth:`Settings._pick`
tests ``flags[field] is not None`` and nothing else, so a boolean flag that
defaults to False does not mean "the user left it off" — it means "the user
explicitly said no", and it silently overrules the config file and the
environment. ``store_true`` with ``default=None`` is the whole fix, and it is
why no ``add_argument`` call below omits ``default``.

**Every flag names its setting with ``dest``.** The namespace attribute is the
:class:`~living_ink.config.schema.Setting` field, not a spelling derived from
the flag, so :func:`flag_values` can hand the namespace straight to
:meth:`Settings.resolve` as its ``flags`` layer without a translation table in
between. That is also why ``--json`` arrives as ``args.output_json``.
"""

import argparse
from typing import Any, Dict, List, Optional, Tuple

from living_ink.config.schema import (
    ACTIVE,
    CHOICE,
    FLAG,
    LIST,
    NUMBER,
    SECRET,
    SETTINGS,
    STORE_CREDENTIALS,
    WHOLE,
    Setting,
)


def _split_list(raw: str) -> Tuple[str, ...]:
    """Read a :data:`LIST` setting from one command-line word.

    A shell has no way to say "several" in a single argument, so the comma is
    the separator here exactly as it is for the environment variable form.

    Args:
        raw: The flag's value, e.g. ``"Trash,Templates"``.

    Returns:
        The trimmed, non-empty parts, in the order given.
    """
    return tuple(part.strip() for part in raw.split(",") if part.strip())


#: How each :class:`Setting` kind reads one command-line word.
_READERS = {WHOLE: int, NUMBER: float}


class _CollectList(argparse.Action):
    """Accumulate a :data:`LIST` setting across repeats and commas.

    ``--tag work --tag ideas`` and ``--tag work,ideas`` are the same request
    spelled two ways, and a user reaches for whichever their shell makes easy.
    Supporting only the comma makes a repeated flag silently discard every
    occurrence but the last, which is the failure mode worth designing out.
    """

    def __call__(self, parser, namespace, values, option_string=None):
        """Extend the destination with the parts this occurrence named.

        Args:
            parser: The parser, unused.
            namespace: The namespace being filled.
            values: The raw word given after the flag.
            option_string: The spelling used, unused.
        """
        current: Tuple[str, ...] = getattr(namespace, self.dest, None) or ()
        setattr(namespace, self.dest, current + _split_list(values))


#: Fields whose flags belong to the program rather than to one command.
#:
#: Verbosity is the one thing a user reaches for without having decided which
#: subcommand they are running yet, so it is accepted on either side of the
#: subcommand name. Everything else belongs to the command that acts on it.
GLOBAL_FIELDS = frozenset({"verbosity"})


def _has_a_flag(setting: Setting) -> bool:
    """Return whether a setting declares any command-line spelling at all.

    Args:
        setting: The schema entry to test.

    Returns:
        True if the setting names a flag, a negated form, or a choice with a
        dedicated flag.
    """
    return bool(setting.flag or setting.negated or any(c.flag for c in setting.choices))


def _is_typeable(setting: Setting) -> bool:
    """Return whether a setting may appear on the command line.

    Args:
        setting: The schema entry to test.

    Returns:
        True for an active, non-secret setting with a flag form.

    Note:
        A credential never gets a flag however it is declared. A secret passed
        on the command line is in the shell history and the process list of
        every user on the machine, so the exclusion is enforced here rather
        than trusted to whoever adds the next one.
    """
    return (
        setting.status == ACTIVE
        and setting.store != STORE_CREDENTIALS
        and setting.kind != SECRET
        and _has_a_flag(setting)
    )


#: The settings registered on every parser, in schema order.
GLOBAL_SETTINGS: List[Setting] = [
    setting for setting in SETTINGS if _is_typeable(setting) and setting.field in GLOBAL_FIELDS
]


def flaggable(command: str) -> List[Setting]:
    """Return the settings that get a flag on one command's own parser.

    Args:
        command: The command name, as the user types it.

    Returns:
        Every active, non-secret setting declaring a flag form for ``command``,
        in schema order, excluding the program-wide ones in
        :data:`GLOBAL_SETTINGS` — those are registered on this parser too, by
        :func:`register_global_flags`, and adding them twice is an argparse
        conflict rather than a duplicate.
    """
    return [
        setting
        for setting in SETTINGS
        if command in setting.commands
        and setting.field not in GLOBAL_FIELDS
        and _is_typeable(setting)
    ]


def _metavar(setting: Setting) -> str:
    """Return the placeholder shown after a value-taking flag.

    Args:
        setting: The setting the flag stands for.

    Returns:
        The flag's own name, upper-cased and undashed — ``--ai-model`` shows
        ``AI_MODEL`` — which reads as the thing being set rather than as
        argparse's default, the ``dest``, which is an internal name.
    """
    assert setting.flag is not None
    return setting.flag.lstrip("-").replace("-", "_").upper()


def _add_switch(target: Any, setting: Setting, default: Any) -> None:
    """Register a boolean setting's on and off flags.

    Args:
        target: Parser or argument group to register on.
        setting: A :data:`FLAG` setting, with a positive form, a negated form,
            or both.
        default: What the namespace holds when the flag is absent.
    """
    if setting.flag:
        target.add_argument(
            setting.flag,
            dest=setting.field,
            action="store_true",
            default=default,
            help=setting.help,
        )
    if setting.negated:
        # Same dest as the positive form, so the later flag on the command
        # line wins and neither needs to know the other exists.
        target.add_argument(
            setting.negated,
            dest=setting.field,
            action="store_false",
            default=default,
            help=f"Do not {setting.help[0].lower()}{setting.help[1:]}",
        )


def _add_choice(target: Any, setting: Setting, default: Any) -> None:
    """Register a choice setting, as dedicated flags or as one valued flag.

    Args:
        target: Parser or argument group to register on.
        setting: A :data:`CHOICE` setting.
        default: What the namespace holds when no choice was named.

    Note:
        A choice carrying per-value flags (``--ssh`` / ``--cloud``) gets those
        and not a ``--preferred-connection SSH`` as well: two spellings of one
        answer is two things to keep in step, and the schema already says which
        spelling was meant by putting the flag on the choice.
    """
    dedicated = [choice for choice in setting.choices if choice.flag]
    if not dedicated:
        target.add_argument(
            setting.flag,
            dest=setting.field,
            default=default,
            choices=[choice.value for choice in setting.choices],
            metavar=_metavar(setting),
            help=setting.help,
        )
        return
    for choice in dedicated:
        assert choice.flag is not None
        spellings = [choice.short, choice.flag] if choice.short else [choice.flag]
        target.add_argument(
            *spellings,
            dest=setting.field,
            action="store_const",
            const=choice.value,
            default=default,
            help=choice.label,
        )


def _add_list(target: Any, setting: Setting, default: Any) -> None:
    """Register a list setting, as one valued flag or as a flag per value.

    Args:
        target: Parser or argument group to register on.
        setting: A :data:`LIST` setting.
        default: What the namespace holds when no flag named a value.

    Note:
        The per-value form **replaces** the configured list rather than adding
        to it, and it does so by being an ordinary flags layer: ``--pdf``
        alone means PDFs and nothing else, because
        :meth:`~living_ink.settings.Settings._pick` takes the first layer that
        has an answer and never merges two. The flags accumulate among
        themselves — ``--pdf --epub`` is both — which is what ``append_const``
        buys, and it is why the default stays None: an empty list here would
        read as "the user asked for nothing" and overrule the file.
    """
    dedicated = [choice for choice in setting.choices if choice.flag]
    if not dedicated:
        target.add_argument(
            setting.flag,
            dest=setting.field,
            default=default,
            action=_CollectList,
            metavar=_metavar(setting),
            help=f"{setting.help} Repeatable, or comma-separated.",
        )
        return
    for choice in dedicated:
        assert choice.flag is not None
        spellings = [choice.short, choice.flag] if choice.short else [choice.flag]
        target.add_argument(
            *spellings,
            dest=setting.field,
            action="append_const",
            const=choice.value,
            default=default,
            help=choice.label,
        )


def _register(parser: argparse.ArgumentParser, settings: List[Setting], default: Any) -> None:
    """Add one list of settings' flags to a parser.

    Settings sharing an :attr:`~living_ink.config.schema.Setting.exclusive_group`
    become one argparse mutually exclusive group, which is what makes
    ``--ssh --cloud`` a parse error rather than a silent pick. A transport is a
    choice and not a preference order: asking for both says nothing about which
    was meant, and syncing from the wrong source is worse than being told to
    decide.

    Args:
        parser: The parser or subparser to extend.
        settings: The settings to register, in the order they appear in help.
        default: What each flag's ``dest`` holds when the flag is absent.
    """
    groups: Dict[str, Any] = {}

    def target_for(setting: Setting) -> Any:
        """Return the parser or the exclusive group a setting belongs on."""
        if not setting.exclusive_group:
            return parser
        if setting.exclusive_group not in groups:
            groups[setting.exclusive_group] = parser.add_mutually_exclusive_group()
        return groups[setting.exclusive_group]

    for setting in settings:
        target = target_for(setting)
        if setting.kind == FLAG:
            _add_switch(target, setting, default)
        elif setting.kind == CHOICE:
            _add_choice(target, setting, default)
        elif setting.kind == LIST:
            _add_list(target, setting, default)
        else:
            target.add_argument(
                setting.flag,
                dest=setting.field,
                default=default,
                type=_READERS.get(setting.kind, str),
                metavar=_metavar(setting),
                help=setting.help,
            )


def register_settings_flags(parser: argparse.ArgumentParser, command: str) -> None:
    """Add every schema-declared flag for one command to its parser.

    Args:
        parser: The command's parser or subparser.
        command: The command name, used to select the settings that apply.
    """
    _register(parser, flaggable(command), default=None)


def register_global_flags(parser: argparse.ArgumentParser) -> None:
    """Add the flags that work before and after the subcommand name.

    ``living-ink --quiet sync`` and ``living-ink sync --quiet`` are the same
    request, so the pair is registered on the top-level parser *and* on every
    subparser. That is also why these alone default to ``argparse.SUPPRESS``
    rather than to None: a real default on the subparser would land in the
    namespace second and overwrite a flag given before the subcommand name,
    turning ``--quiet sync`` into an ordinary run.

    Args:
        parser: Parser or subparser to extend.
    """
    _register(parser, GLOBAL_SETTINGS, default=argparse.SUPPRESS)


def flag_values(args: argparse.Namespace, command: str = "sync") -> Dict[str, Any]:
    """Collect what the user actually typed, keyed by settings field.

    Args:
        args: The parsed namespace.
        command: Which command's flags to look for. Read with ``getattr``
            defaults, because ``watch`` reuses ``sync``'s parser and a test may
            hand over a bare namespace.

    Returns:
        A mapping fit for :meth:`Settings.resolve`'s ``flags`` layer, holding
        only the settings a flag supplied. An omitted flag is left out rather
        than passed as None, so the dictionary reads as "what was asked for"
        at the call site as well as at the resolver.
    """
    values: Dict[str, Any] = {}
    for setting in GLOBAL_SETTINGS + flaggable(command):
        given: Optional[Any] = getattr(args, setting.field, None)
        if given is not None:
            values[setting.field] = given
    return values
