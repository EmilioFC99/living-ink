"""Shell completion scripts, generated from the parser the CLI builds.

A completion script is a copy of the interface, and a copy goes stale. This
one is read off :meth:`~living_ink.cli.app.LivingInkCLI.build_parser` at the
moment it is printed, so a flag exists in the shell exactly when it exists in
the program: add a setting to :data:`~living_ink.config.schema.SETTINGS`, or a
hand-written switch to a command's ``register_args``, and the completion for it
appears with no second edit. There is no static list of flags in this module
and there must never be one.

Two facts have to survive the trip from argparse to three shells that disagree
about everything else, and :class:`OptionSpec` is where they are recorded:
whether a flag takes a value — offering ``--force`` a filename is noise, but
swallowing the word after ``--destination`` is a wrong completion — and what
that value may be. Only three answers exist. A :data:`PATH` setting completes
filenames, a setting with :class:`~living_ink.config.schema.Choice` values
completes those words, and everything else completes nothing, because a
notebook title is on the tablet and this runs offline.

Nothing secret reaches a completion script, and not by filtering here:
:func:`~living_ink.cli.flags._is_typeable` refuses a credential a flag at all,
so a parser built from the schema has no ``--api-key`` to describe.
"""

import argparse
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from living_ink.config.schema import PATH, SETTINGS

#: The shells ``living-ink completions`` can write, in the order it lists them.
SHELLS: Tuple[str, ...] = ("bash", "zsh", "fish")

#: A flag that takes no value at all.
SWITCH = "switch"
#: A flag whose value is a path on this machine.
FILE = "file"
#: A flag whose value is one of a known set of words.
CHOICES = "choices"
#: A flag that takes a value nothing offline can enumerate.
OPAQUE = "opaque"

#: ``dest`` names whose value is a path.
#:
#: Read from the schema rather than listed, so a new :data:`PATH` setting
#: completes filenames the day it is declared. ``config`` is the one addition:
#: ``-c`` is the top-level parser's own flag and belongs to no setting, being
#: the one thing that has to be answerable *before* a config file is found.
_PATH_DESTS = frozenset(setting.field for setting in SETTINGS if setting.kind == PATH) | {"config"}


@dataclass(frozen=True)
class OptionSpec:
    """One flag, as much of it as a shell can use.

    Attributes:
        spellings: Every way of typing it, long forms first, as they appear on
            the command line — ``("--quiet", "-q")``.
        description: One line, shown by the shells that show one.
        takes: :data:`SWITCH`, :data:`FILE`, :data:`CHOICES` or :data:`OPAQUE`.
        choices: The allowed words, for :data:`CHOICES`, and empty otherwise.
    """

    spellings: Tuple[str, ...]
    description: str
    takes: str
    choices: Tuple[str, ...] = ()

    @property
    def long(self) -> Tuple[str, ...]:
        """Return the ``--long`` spellings."""
        return tuple(name for name in self.spellings if name.startswith("--"))

    @property
    def short(self) -> Tuple[str, ...]:
        """Return the ``-s`` spellings."""
        return tuple(name for name in self.spellings if not name.startswith("--"))


@dataclass(frozen=True)
class CommandSpec:
    """One subcommand and everything typeable after its name.

    Attributes:
        name: The word the user types.
        description: One line, from the command's own help.
        options: Its flags, including the ones every subparser carries.
        words: Values its positional arguments accept, flattened. Empty for a
            command whose positionals take anything, which completes nothing
            rather than guessing.
    """

    name: str
    description: str
    options: Tuple[OptionSpec, ...] = ()
    words: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ProgramSpec:
    """The whole interface, in the one shape three renderers can read.

    Attributes:
        prog: The executable's name, as the parser knows it.
        options: Flags accepted before the subcommand name.
        commands: The subcommands, in the order ``--help`` lists them.
    """

    prog: str
    options: Tuple[OptionSpec, ...] = ()
    commands: Tuple[CommandSpec, ...] = ()

    @property
    def command_names(self) -> Tuple[str, ...]:
        """Return just the subcommand words."""
        return tuple(command.name for command in self.commands)


def _is_subcommands(action: argparse.Action) -> bool:
    """Return whether an action is the one holding the subparsers.

    Args:
        action: An action off a parser.

    Returns:
        True for the sub-parsers action. Recognised by shape rather than by
        ``isinstance(action, argparse._SubParsersAction)``: it is the only
        positional whose choices are parsers, and the shape is the part of
        argparse that is documented.
    """
    choices = getattr(action, "choices", None)
    return (
        not action.option_strings
        and isinstance(choices, dict)
        and all(isinstance(value, argparse.ArgumentParser) for value in choices.values())
    )


def _first_line(text: Optional[str]) -> str:
    """Return the first line of a help string, collapsed to one space.

    Args:
        text: A help or description string, possibly None or multi-line.

    Returns:
        The first non-empty line, stripped. A shell shows one line beside a
        flag, and a description written for ``--help`` may be a paragraph.
    """
    for line in (text or "").splitlines():
        stripped = " ".join(line.split())
        if stripped:
            return stripped
    return ""


def _summary(text: Optional[str]) -> str:
    """Return the one-sentence form of a command's description.

    Args:
        text: The subparser's ``description``, which is a paragraph written
            for ``living-ink <command> --help``.

    Returns:
        Its first sentence. A shell lists commands in a menu one line high, and
        the short ``help=`` argparse was given for exactly that purpose is only
        reachable through a second private attribute — while the first sentence
        of the long form is the same summary, written for the same reader, out
        of a public one.
    """
    sentence, stop, _ = _first_line(text).partition(". ")
    return sentence + stop.strip()


def _takes(action: argparse.Action) -> Tuple[str, Tuple[str, ...]]:
    """Work out what may follow one flag.

    Args:
        action: The option's action.

    Returns:
        The kind and, for :data:`CHOICES`, the allowed words.
    """
    if action.nargs == 0:
        return SWITCH, ()
    if action.choices:
        return CHOICES, tuple(str(choice) for choice in action.choices)
    if action.dest in _PATH_DESTS:
        return FILE, ()
    return OPAQUE, ()


def _options(parser: argparse.ArgumentParser) -> Tuple[OptionSpec, ...]:
    """Describe every flag one parser accepts.

    Actions sharing a ``dest`` are **not** merged: ``--json`` and ``--no-json``
    are one setting and two things to type, and a shell completes spellings.
    A hidden flag (``help=SUPPRESS``) is left out, because the point of hiding
    it is that nobody is meant to find it.

    Args:
        parser: The parser or subparser to read.

    Returns:
        The flags, in the order they were registered.
    """
    found: List[OptionSpec] = []
    for action in _actions(parser):
        if not action.option_strings or action.help == argparse.SUPPRESS:
            continue
        takes, choices = _takes(action)
        spellings = sorted(
            action.option_strings, key=lambda name: (not name.startswith("--"), name)
        )
        found.append(
            OptionSpec(
                spellings=tuple(spellings),
                description=_first_line(action.help),
                takes=takes,
                choices=choices,
            )
        )
    return tuple(found)


def _words(parser: argparse.ArgumentParser) -> Tuple[str, ...]:
    """Collect the values a subparser's positional arguments accept.

    Args:
        parser: The subparser to read.

    Returns:
        Every declared choice, in order. A positional with no ``choices`` adds
        nothing: a notebook title or a file path is not knowable from here, and
        an empty list is how the renderers say "complete nothing".
    """
    words: List[str] = []
    for action in _actions(parser):
        if action.option_strings or _is_subcommands(action):
            continue
        words.extend(str(choice) for choice in action.choices or ())
    return tuple(words)


def _actions(parser: argparse.ArgumentParser) -> List[argparse.Action]:
    """Return a parser's actions.

    Args:
        parser: The parser to read.

    Returns:
        The registered actions, in registration order.

    Note:
        ``_actions`` is argparse's only inventory of what a parser accepts and
        it has no public counterpart, so the private access is confined to this
        one function rather than spread across the module.
    """
    return list(parser._actions)


def describe(parser: argparse.ArgumentParser) -> ProgramSpec:
    """Read a built parser into the shape the renderers consume.

    Args:
        parser: The top-level parser, with its subcommands already registered.

    Returns:
        The program, its global flags and every subcommand. A parser with no
        subcommands describes fine and yields a script that completes only the
        global flags, which is what the tests build to check the walk itself.
    """
    commands: List[CommandSpec] = []
    for action in _actions(parser):
        if not _is_subcommands(action):
            continue
        for name, subparser in action.choices.items():
            commands.append(
                CommandSpec(
                    name=name,
                    description=_summary(subparser.description),
                    options=_options(subparser),
                    words=_words(subparser),
                )
            )
    return ProgramSpec(prog=parser.prog, options=_options(parser), commands=tuple(commands))


def _sh_quote(text: str) -> str:
    """Escape a string for the inside of shell single quotes.

    Args:
        text: Arbitrary help text.

    Returns:
        The text with every single quote closed, escaped and reopened — the
        only sequence that is safe inside ``'...'`` in all three shells.
    """
    return text.replace("'", "'\\''")


def _fish_quote(text: str) -> str:
    """Escape a string for the inside of fish single quotes.

    Args:
        text: Arbitrary help text.

    Returns:
        The text with backslashes and single quotes backslash-escaped. fish
        does honour the backslash inside single quotes, which is exactly why
        it needs different treatment from bash and zsh.
    """
    return text.replace("\\", "\\\\").replace("'", "\\'")


def _zsh_description(text: str) -> str:
    """Escape a description for the inside of a zsh ``[...]`` spec.

    Args:
        text: Arbitrary help text.

    Returns:
        The text with the four characters that end a spec early — the brackets,
        the colon that starts the argument action, and the backslash itself —
        escaped. A help line reading "Sync a notebook by name, folder path
        (e.g. 'Work/Notes'), or ID" carries none of them, and the one that
        eventually does must not silently truncate every flag after it.
    """
    for char in ("\\", "[", "]", ":"):
        text = text.replace(char, "\\" + char)
    return _sh_quote(text)


def _header(shell: str, prog: str, install: str) -> List[str]:
    """Build the comment block every generated script opens with.

    Args:
        shell: The shell the script is for.
        prog: The executable's name.
        install: The line telling the user where to put it.

    Returns:
        The comment lines. Installation instructions go *inside* the script
        rather than beside it on stderr, because the whole point of the output
        is that it can be redirected into a file or eval'd, and a message that
        survives either is a comment.
    """
    return [
        f"# {prog} completions for {shell}.",
        "#",
        f"# Generated by `{prog} completions {shell}` — regenerate it after an upgrade",
        f"# rather than editing it; every line below is read off {prog}'s own parser.",
        "#",
        "# Install:",
        f"#   {install}",
        "",
    ]


def render_bash(spec: ProgramSpec) -> str:
    """Render the bash completion function.

    Args:
        spec: The described program.

    Returns:
        A script defining one function and registering it with ``complete``.

    Note:
        Finding the subcommand means walking the words rather than reading
        ``$1``: ``living-ink --quiet sync`` is a supported spelling, so the
        first word is not reliably the command. The walk skips the word after
        a value-taking global flag, or ``living-ink -c foo.yml`` would complete
        as though ``foo.yml`` were a subcommand.
    """
    prog = spec.prog
    func = "_" + prog.replace("-", "_")
    value_globals = [
        spelling
        for option in spec.options
        if option.takes != SWITCH
        for spelling in option.spellings
    ]

    lines = _header(
        "bash",
        prog,
        f"{prog} completions bash > ~/.local/share/bash-completion/completions/{prog}",
    )
    lines += [
        f"{func}() {{",
        "    local cur prev cmd i",
        "    COMPREPLY=()",
        '    cur="${COMP_WORDS[COMP_CWORD]}"',
        '    prev="${COMP_WORDS[COMP_CWORD-1]}"',
        "",
        '    cmd=""',
        "    for (( i = 1; i < COMP_CWORD; i++ )); do",
        '        case "${COMP_WORDS[i]}" in',
    ]
    if value_globals:
        lines.append(f"            {'|'.join(value_globals)}) i=$((i+1)) ;;")
    lines += [
        "            -*) ;;",
        '            *) cmd="${COMP_WORDS[i]}"; break ;;',
        "        esac",
        "    done",
        "",
        '    case "$cmd" in',
    ]

    for command in spec.commands:
        lines.append(f"        {command.name})")
        lines += _bash_branch(command.options, command.words)
        lines.append("            ;;")

    lines.append("        *)")
    lines += _bash_branch(spec.options, spec.command_names)
    lines += [
        "            ;;",
        "    esac",
        "}",
        "",
        f"complete -F {func} {prog}",
        "",
    ]
    return "\n".join(lines)


def _bash_branch(options: Tuple[OptionSpec, ...], words: Tuple[str, ...]) -> List[str]:
    """Render one ``case`` arm: what to offer at one point in the line.

    Args:
        options: The flags accepted here.
        words: Subcommand names or positional values accepted here.

    Returns:
        The arm's body, indented to sit inside the outer ``case``.
    """
    arms = []
    for option in options:
        if option.takes == SWITCH:
            continue
        spellings = "|".join(option.spellings)
        if option.takes == FILE:
            answer = 'COMPREPLY=( $(compgen -f -- "$cur") ); return'
        elif option.takes == CHOICES:
            offered = " ".join(option.choices)
            answer = f"COMPREPLY=( $(compgen -W '{offered}' -- \"$cur\") ); return"
        else:
            # A value nothing here can list. Returning empty-handed lets bash
            # fall back to nothing, which is right: guessing a filename for
            # --notebook would complete the working directory into a title.
            answer = "return"
        arms.append(f"                {spellings}) {answer} ;;")

    # Omitted entirely when every flag here is a switch: an empty ``case`` is
    # legal bash and reads like a mistake in a file the user is invited to
    # look at.
    body = ['            case "$prev" in', *arms, "            esac"] if arms else []

    offered = " ".join(list(words) + [s for option in options for s in option.spellings])
    body.append(f"            COMPREPLY=( $(compgen -W '{offered}' -- \"$cur\") )")
    body.append("            return")
    return body


def render_zsh(spec: ProgramSpec) -> str:
    """Render the zsh completion function.

    Args:
        spec: The described program.

    Returns:
        A script that works both ways it can be used: dropped into ``$fpath``
        as ``_living-ink``, where ``#compdef`` and the trailing call do the
        work, and ``eval``'d in an interactive shell, where the ``funcstack``
        test sends it to ``compdef`` instead.
    """
    prog = spec.prog
    func = "_" + prog.replace("-", "_")

    lines = _header(
        "zsh",
        prog,
        f"{prog} completions zsh > ~/.zfunc/_{prog}   # with `fpath+=(~/.zfunc)` before `compinit`",
    )
    lines.insert(0, f"#compdef {prog}")
    lines += [
        f"{func}() {{",
        "    local context state state_descr line",
        "    typeset -A opt_args",
        "",
        "    _arguments -C \\",
    ]
    lines += [f"        {spec_line} \\" for spec_line in _zsh_specs(spec.options)]
    lines += [
        "        '1: :->command' \\",
        "        '*:: :->argument'",
        "",
        "    case $state in",
        "        command)",
        "            local -a commands",
        "            commands=(",
    ]
    for command in spec.commands:
        lines.append(f"                '{command.name}:{_zsh_description(command.description)}'")
    lines += [
        "            )",
        f"            _describe -t commands '{prog} command' commands",
        "            ;;",
        "        argument)",
        "            case $words[1] in",
    ]
    for command in spec.commands:
        arm = _zsh_specs(command.options)
        if command.words:
            arm.append(f"'1: :({' '.join(command.words)})'")
        lines.append(f"                {command.name})")
        if arm:
            lines.append("                    _arguments \\")
            lines += [f"                        {spec_line} \\" for spec_line in arm[:-1]]
            lines.append(f"                        {arm[-1]}")
        lines.append("                    ;;")
    lines += [
        "            esac",
        "            ;;",
        "    esac",
        "}",
        "",
        f'if [ "$funcstack[1]" = "{func}" ]; then',
        f'    {func} "$@"',
        "else",
        f"    compdef {func} {prog}",
        "fi",
        "",
    ]
    return "\n".join(lines)


def _zsh_specs(options: Tuple[OptionSpec, ...]) -> List[str]:
    """Render one flag per ``_arguments`` spec string.

    Args:
        options: The flags to describe.

    Returns:
        One quoted spec per spelling. The ``(-q --quiet)`` exclusion prefix
        names every spelling of the same flag, so offering one stops offering
        the others — two spellings of one answer is not two arguments.
    """
    specs: List[str] = []
    for option in options:
        exclusion = f"({' '.join(option.spellings)})" if len(option.spellings) > 1 else ""
        if option.takes == SWITCH:
            argument = ""
        elif option.takes == FILE:
            argument = ":path:_files"
        elif option.takes == CHOICES:
            argument = f":value:({' '.join(option.choices)})"
        else:
            # An empty action: zsh knows a word follows and offers nothing for
            # it, which is what stops it completing the next flag into a value.
            argument = ":value:"
        for spelling in option.spellings:
            description = _zsh_description(option.description)
            specs.append(f"'{exclusion}{spelling}[{description}]{argument}'")
    return specs


def render_fish(spec: ProgramSpec) -> str:
    """Render the fish completions.

    Args:
        spec: The described program.

    Returns:
        A script of ``complete`` calls, one per flag per command.

    Note:
        fish has no notion of a flag belonging to a subcommand, so every line
        carries its own condition. The global flags are gated on *not* having
        seen a subcommand, because ``--version`` after ``sync`` is not a thing
        the parser accepts and a completion that offers it is a lie.
    """
    prog = spec.prog
    names = " ".join(spec.command_names)

    lines = _header(
        "fish",
        prog,
        f"{prog} completions fish > ~/.config/fish/completions/{prog}.fish",
    )
    lines += [
        f"complete -c {prog} -f",
        "",
    ]
    for command in spec.commands:
        description = _fish_quote(command.description)
        lines.append(
            f"complete -c {prog} -n '__fish_use_subcommand' -a '{command.name}' -d '{description}'"
        )
    lines.append("")
    for option in spec.options:
        lines.append(
            f"complete -c {prog} -n 'not __fish_seen_subcommand_from {names}' "
            f"{_fish_option(option)}"
        )
    for command in spec.commands:
        lines.append("")
        condition = f"__fish_seen_subcommand_from {command.name}"
        if command.words:
            offered = " ".join(command.words)
            lines.append(f"complete -c {prog} -n '{condition}' -a '{offered}'")
        for option in command.options:
            lines.append(f"complete -c {prog} -n '{condition}' {_fish_option(option)}")
    lines.append("")
    return "\n".join(lines)


def _fish_option(option: OptionSpec) -> str:
    """Render the flag-specific half of one fish ``complete`` call.

    Args:
        option: The flag to describe.

    Returns:
        The spellings, the value rule and the description, without the
        ``complete -c`` prefix or the condition.
    """
    parts = [f"-l {name.lstrip('-')}" for name in option.long]
    parts += [f"-s {name.lstrip('-')}" for name in option.short]
    if option.takes == FILE:
        # -r says a value is required; -F re-enables the file completion the
        # `complete -c living-ink -f` line at the top turned off everywhere.
        parts.append("-r -F")
    elif option.takes == CHOICES:
        parts.append(f"-x -a '{_fish_quote(' '.join(option.choices))}'")
    elif option.takes == OPAQUE:
        parts.append("-x")
    parts.append(f"-d '{_fish_quote(option.description)}'")
    return " ".join(parts)


#: How each shell is written. A fourth shell is an entry here and a renderer.
RENDERERS: Dict[str, Callable[[ProgramSpec], str]] = {
    "bash": render_bash,
    "zsh": render_zsh,
    "fish": render_fish,
}


def render(parser: argparse.ArgumentParser, shell: str) -> str:
    """Generate the completion script for one shell.

    Args:
        parser: The top-level parser, with subcommands registered.
        shell: One of :data:`SHELLS`.

    Returns:
        The script, ready to be written to a file or eval'd.

    Raises:
        ValueError: If the shell is not one this module writes. The command
            never sees it — argparse rejects an unknown word first — but a
            library caller can.
    """
    try:
        renderer = RENDERERS[shell]
    except KeyError:
        raise ValueError(f"No completions for {shell!r}. Choose one of: {', '.join(SHELLS)}.")
    return renderer(describe(parser))
