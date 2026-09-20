"""The behaviour matrix: what the user types is exactly what runs.

Every other test file in this suite asks "is this unit correct?". This one asks
a different question, and it is the one a user actually cares about:

    Given this command line, does precisely the requested work happen —
    and **nothing else**?

Those are not the same question. A parser can map every flag correctly and the
command can still connect to a tablet it was never asked to reach, or open a
database it has no business opening. Unit tests do not see that, because the
thing they would have to observe is an *absence*.

So the matrix is built from four tables, and the tables are the specification:

=============================  ===========================================
Table                          What a failure means
=============================  ===========================================
``SYNC_BOOLEAN_FLAGS``         A flag stopped meaning what it meant.
``COMMAND_SURFACE``            A flag appeared or vanished unannounced.
``FLAG_COMPATIBILITY``         A combination started or stopped being legal.
``COMMAND_REACH``              A command grew a side effect.
=============================  ===========================================

Two design choices are worth defending, because both look like shortcuts.

**Total equality, not field-by-field assertions.** Every sync case compares the
whole keyword mapping :func:`~living_ink.cli.sync_arguments` produces against an
expected one. That is what makes "and nothing else" testable at all: asserting
``dry_run is True`` proves the flag arrived, while asserting the whole mapping
proves no *other* argument moved with it. A new argument defaults into every
expectation for free; a new argument that some flag secretly sets fails every
case at once, which is the correct amount of noise.

**The exhaustive pass runs at the parse layer.** :func:`intent` is
``parse_args`` followed by ``sync_arguments`` — the two steps that turn an argv
into an instruction, and nothing after them. All 512 boolean combinations go
through it, which would be slow through ``main()`` and would prove no more.
What licenses the shortcut is :class:`TestTheShortcutMatchesTheRealThing`: it
runs the real entry point and checks the pipeline was handed the same arguments
:func:`intent` predicts. If that one test passes, the 512 are statements about
the real CLI.

Nothing here reaches the network, a tablet, a vault, or a real config. The
``cli`` fixture replaces the four seams that face outward and *fails the test*
if anything opens a socket or a subprocess anyway.
"""

from __future__ import annotations

import argparse
import ast
import inspect
import itertools
import json
import os
import platform
import socket
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from living_ink.cli import LivingInkCLI, sync_arguments
from living_ink.cli.flags import flaggable
from living_ink.config.schema import CHOICE, FLAG, LIST, NUMBER, WHOLE, Setting
from living_ink.settings import Settings

# ---------------------------------------------------------------------------
# Table 1 — what each sync flag means
# ---------------------------------------------------------------------------

#: Every boolean flag on ``sync``, and the single thing it is allowed to
#: change, written as a dotted path into the pipeline's keyword arguments. One
#: flag, one effect: the exhaustive pass below turns that into a property — the
#: arguments for any set of flags must be exactly the union of their individual
#: effects, with no interaction between them.
#:
#: A path with a dot lands inside ``flags``, the mapping every schema-declared
#: setting travels in, and a path without one is a named keyword. Which side a
#: flag falls on is itself part of the contract: ``--prune`` names a setting a
#: config file can also set, ``--keep-temp`` shapes one run and has no
#: persisted form, and a flag moving between the two changes whether the config
#: file can override it.
SYNC_BOOLEAN_FLAGS: dict[str, tuple[str, Any]] = {
    "--ssh": ("flags.preferred_connection", "ssh"),
    "--cloud": ("flags.preferred_connection", "cloud"),
    "--force": ("force", True),
    "--keep-temp": ("keep_temp", True),
    "--prune": ("flags.prune", True),
    "--json": ("flags.output_json", True),
}

#: What ``living-ink sync`` with no flags at all resolves to.
#:
#: ``flags`` is empty, and that emptiness is the whole point of generating the
#: parser from the schema: every generated flag defaults to None and
#: :func:`~living_ink.cli.flags.flag_values` drops a None, so a setting the user
#: did not mention never reaches :meth:`Settings.resolve` and therefore cannot
#: overrule the config file or the environment. A ``"prune": False`` in here
#: would mean "the user explicitly said no", which is a different instruction.
#:
#: Spelled out rather than derived from the function under test, because a
#: mapping built by calling ``sync_arguments([])`` would agree with it by
#: construction and prove nothing.
BARE_SYNC: dict[str, Any] = {
    "notebook": None,
    "source_path": None,
    "source_regex": None,
    "force": False,
    "keep_temp": False,
    "dry_run": False,
    "flags": {},
}

#: Boolean flags that ``sync`` accepts but that never reach the pipeline.
#:
#: ``--preview`` selects a different code path entirely and ``--all`` only
#: qualifies it, so both are tested by
#: :class:`TestPreviewIsADifferentCommandInDisguise` instead. ``--transcribe``
#: is not in the table above either, because it is the one flag with no effect
#: of its own — it needs ``--preview`` to mean anything, which is exactly what
#: :class:`TestPreviewAndItsExpensiveVariant` is for. ``--verbose`` and
#: ``--quiet`` are not sync flags at all — they are generated onto every parser
#: from ``output.verbosity`` — and they are covered by
#: :class:`TestVerbosityIsAcceptedOnBothSides`.
SYNC_FLAGS_OUTSIDE_THE_OPTIONS = (
    "--preview",
    "--transcribe",
    "--all",
    "--verbose",
    "-q",
    "--quiet",
)

#: One representative command-line value per setting kind, and what parsing it
#: must yield. Written out rather than taken from the generator's own ``_READERS``
#: table, so that a type reader silently changing — ``--limit`` starting to
#: arrive as the string ``"7"`` — fails here instead of agreeing by construction.
SAMPLE_VALUES: dict[str, tuple[str, Any]] = {
    WHOLE: ("7", 7),
    NUMBER: ("0.25", 0.25),
    LIST: ("one,two", ("one", "two")),
}

#: What a setting with no entry in :data:`SAMPLE_VALUES` is given: text and path
#: settings both carry the word through untouched.
DEFAULT_SAMPLE: tuple[str, Any] = ("sample", "sample")


def spellings_of(setting: Setting) -> list[tuple[list[str], Any]]:
    """Return every way one setting can be given on the command line.

    Args:
        setting: A schema entry that :func:`flaggable` returned.

    Returns:
        ``(words, value)`` pairs — the argv fragment to type, and the value the
        setting must end up holding. A switch with a negated form yields two
        pairs, and a choice with per-value flags yields one pair per flag.
    """
    if setting.kind == FLAG:
        pairs: list[tuple[list[str], Any]] = []
        if setting.flag:
            pairs.append(([setting.flag], True))
        if setting.negated:
            pairs.append(([setting.negated], False))
        return pairs
    dedicated = [choice for choice in setting.choices if choice.flag]
    if dedicated:
        # A choice takes one of its values and a list takes several, so the
        # same flag spelling yields a bare value for the one and a list of
        # exactly what was typed for the other.
        if setting.kind == LIST:
            return [([choice.flag], [choice.value]) for choice in dedicated]
        return [([choice.flag], choice.value) for choice in dedicated]
    if setting.kind == CHOICE:
        first = setting.choices[0].value
        return [([setting.flag, first], first)]
    raw, parsed = SAMPLE_VALUES.get(setting.kind, DEFAULT_SAMPLE)
    return [([setting.flag, raw], parsed)]


#: Every option string ``sync`` gets from the settings schema rather than by
#: hand, in all its spellings.
GENERATED_SYNC_FLAGS: set[str] = {
    spelling
    for setting in flaggable("sync")
    for spelling in [setting.flag, setting.negated, *(c.flag for c in setting.choices)]
    if spelling
}

#: Sets of sync flags the parser refuses to see together.
#:
#: The exhaustive sweep below reads this to decide which of its 512 subsets
#: must be a usage error instead of an instruction, so an exclusion added to
#: the parser and not to this set fails the sweep rather than silently
#: shrinking it.
MUTUALLY_EXCLUSIVE_SYNC_FLAGS: tuple[frozenset[str], ...] = (frozenset({"--ssh", "--cloud"}),)


def is_rejected(flags: tuple[str, ...]) -> bool:
    """Say whether a set of flags trips one of the parser's exclusions.

    Args:
        flags: Flag strings, each a key of :data:`SYNC_BOOLEAN_FLAGS`.

    Returns:
        True if the flags contain every member of an exclusive group.
    """
    return any(group <= set(flags) for group in MUTUALLY_EXCLUSIVE_SYNC_FLAGS)


def intent(argv: list[str]) -> dict[str, Any]:
    """Turn a command line into the instruction it encodes.

    This is the whole of the CLI's decision-making for ``sync``: argparse
    produces a namespace and :func:`~living_ink.cli.sync_arguments` — the
    single place that knows CLI flag names — turns it into the keyword
    arguments the pipeline is constructed from. Everything downstream reads
    those, so two argvs that produce equal mappings are the same instruction.

    Args:
        argv: Arguments as the user would type them, without the program name.

    Returns:
        The arguments a real run would be driven by.
    """
    args = LivingInkCLI().build_parser().parse_args(argv)
    return sync_arguments(args)


def with_flags(**settings: Any) -> dict[str, Any]:
    """Return the bare sync arguments carrying some settings overrides.

    Args:
        **settings: Entries for the ``flags`` mapping, keyed by
            :class:`~living_ink.settings.Settings` field name.

    Returns:
        :data:`BARE_SYNC` with ``flags`` replaced, leaving the original intact.
    """
    return {**BARE_SYNC, "flags": {**BARE_SYNC["flags"], **settings}}


def expected_for(flags: tuple[str, ...]) -> dict[str, Any]:
    """Predict the pipeline arguments for a set of boolean flags.

    Args:
        flags: Flag strings, each a key of :data:`SYNC_BOOLEAN_FLAGS`.

    Returns:
        :data:`BARE_SYNC` with one entry changed per flag, and nothing else.
        A flag whose declared path is dotted changes an entry inside ``flags``;
        the rest change a top-level keyword.
    """
    expected = with_flags()
    for flag in flags:
        path, value = SYNC_BOOLEAN_FLAGS[flag]
        head, dot, leaf = path.partition(".")
        if dot:
            expected[head][leaf] = value
        else:
            expected[head] = value
    return expected


# ---------------------------------------------------------------------------
# Table 2 — the surface itself
# ---------------------------------------------------------------------------

#: Every command, and every option string it accepts. A snapshot, deliberately.
#:
#: The other tables only cover flags they know about, so without this one a
#: newly added flag would be untested *and* silently so — the matrix would
#: still be green while no longer being a matrix. Adding a flag must break this
#: table, which forces whoever adds it to say what it does.
#:
#: ``--verbose`` / ``-q`` are on every parser because ``output.verbosity`` is a
#: program-wide setting, registered at the top level and on each subparser, so
#: that both ``living-ink --verbose sync`` and ``living-ink sync --verbose``
#: work.
#:
#: Most of ``sync``'s list is generated from the settings schema rather than
#: hand-registered, which makes this table more valuable, not less: the schema
#: declares what a setting is *called*, and this says what the program
#: *accepts*. Adding a ``flag=`` to a setting now changes the command line, and
#: it has to break something visible when it does.
SYNC_SURFACE: set[str] = {
    "-h",
    "--help",
    "--verbose",
    "-q",
    "--quiet",
    # Hand-registered: one run's shape, with no persisted form.
    "--notebook",
    "--source-path",
    "--source-regex",
    "--force",
    "--keep-temp",
    "--preview",
    "--transcribe",
    "--all",
    # Generated from the settings schema, in schema order.
    "--ssh",
    "--cloud",
    "--ssh-host",
    "--ssh-user",
    "--ssh-port",
    "--ai-provider",
    "--ai-model",
    "--ai-base-url",
    "--ai-temperature",
    "--ai-language",
    "--ai-prompt-dir",
    "--ocr-concurrency",
    "--notebooks",
    "--pdf",
    "--epub",
    "--tag",
    "--exclude",
    "--skip-empty",
    "--limit",
    "--prune",
    "--destination",
    "--destination-folder",
    "--mirror-folders",
    "--no-mirror-folders",
    "--attachments-folder",
    "--embed-images",
    "--no-embed-images",
    "--no-transcript-cache",
    "--no-render-cache",
    "--data-dir",
    "--json",
}

COMMAND_SURFACE: dict[str, set[str]] = {
    "": {"-h", "--help", "-v", "--version", "-c", "--config", "--verbose", "-q", "--quiet"},
    "sync": SYNC_SURFACE,
    # Watch delegates ``register_args`` to sync, so the two lists can only ever
    # differ by watch's own flag — which is the point of spelling it this way.
    "watch": SYNC_SURFACE | {"--interval"},
    "setup": {"-h", "--help", "--verbose", "-q", "--quiet"},
    # One read-only surface with one ``--json``, where ``status``, ``state``
    # and ``cache`` used to be three commands with three of them. Everything
    # those three could *do* rather than show — clear, prune, repair, forget —
    # is either automatic, destructive and therefore ``config → Advanced``, or
    # ``sync --force``.
    "info": {"-h", "--help", "--verbose", "-q", "--quiet", "--json"},
}

#: Commands §7.1 of the 1.0 design specifies but that have not been built. They
#: must still be rejected as usage errors rather than half-working, and this
#: list is what makes their absence a stated fact instead of an oversight.
UNBUILT_COMMANDS = ("config", "uninstall")

#: Commands that shipped in 0.x and are gone. Listed rather than deleted,
#: because a retired command has to fail the same clean way an unbuilt one
#: does: a script still calling ``living-ink state --forget`` must be told the
#: word is not a command, not left to a traceback or, worse, a partial run.
RETIRED_COMMANDS = ("status", "state", "cache")


# ---------------------------------------------------------------------------
# Table 5 — the setup conversation
# ---------------------------------------------------------------------------

#: Answered by the fixture with the vault it created, because the path is only
#: known per test and the table is module-level.
THE_DETECTED_VAULT = "\x00vault"

#: A full Cloud walkthrough, as (fragment of the question, answer), in order.
#:
#: Keyed on what the wizard *asks* rather than on position, because the
#: alternative — a bare list of twelve answers — cannot tell "the wizard grew a
#: step" apart from "the wizard reordered two" apart from "the answers slipped
#: by one", and all three produce a config that looks plausible. Matching the
#: question makes each answer answer a named one.
#:
#: The answers are widget return values, not keystrokes: a ``select`` hands
#: back the chosen ``Choice.value`` and a ``confirm`` hands back a bool. What a
#: keystroke does to a widget is :mod:`tests.test_ui`'s subject, through a real
#: ``prompt_toolkit`` pipe.
CLOUD_WALKTHROUGH: tuple[tuple[str, object], ...] = (
    ("How should Living Ink reach your reMarkable?", "cloud"),
    ("Reuse the reMarkable pairing", True),
    ("Also set up the USB cable", False),
    ("Which AI provider?", "gemini"),
    ("Model", "gemini-2.0-flash"),
    ("API key", "AIzaTestKey"),
    ("Publish to Obsidian?", True),
    ("Which vault?", THE_DETECTED_VAULT),
    ("Which folder inside the vault?", "Living Ink"),
    ("Mirror the tablet's folder structure", True),
    ("Sync automatically every hour?", False),
    ("Save this configuration?", True),
    ("Run your first sync now?", False),
)

#: Steps the wizard only offers on macOS, because there is nothing behind them
#: anywhere else: background sync installs a launchd plist. Linux therefore
#: gets a walkthrough that is genuinely one question shorter — which CI
#: discovered, because the first version of this table assumed everyone was on
#: a Mac.
MACOS_ONLY_STEPS = frozenset({"Sync automatically every hour?"})


def walkthrough_for_this_platform() -> list[tuple[str, object]]:
    """Return the conversation the wizard actually holds on this machine.

    Returns:
        :data:`CLOUD_WALKTHROUGH` with the macOS-only steps removed when not
        running on macOS.
    """
    on_macos = platform.system() == "Darwin"
    return [step for step in CLOUD_WALKTHROUGH if on_macos or step[0] not in MACOS_ONLY_STEPS]


# ---------------------------------------------------------------------------
# Table 3 — which combinations are legal
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Combination:
    """One entry in the compatibility matrix.

    Attributes:
        argv: The command line, without the program name.
        legal: Whether the parser is expected to accept it.
        why: The reason, printed when the expectation fails.
    """

    argv: tuple[str, ...]
    legal: bool
    why: str


#: Every combination whose legality is a deliberate decision rather than an
#: accident of having two unrelated flags.
#:
#: Read the ``legal=False`` rows as the contract they are: argparse rejects
#: these with exit code 2, and any change to that is a change users can see.
#: Read the ``legal=True`` rows the same way in reverse — ``--notebook`` with
#: ``--preview`` is a combination people rely on, and making it an error later
#: would break scripts.
#:
FLAG_COMPATIBILITY: tuple[Combination, ...] = (
    # sync — the transport pair is the only exclusion.
    Combination(("sync", "--notebook", "Work/Notes"), True, "scoping a sync to one notebook"),
    Combination(("sync", "--notebook", "Foo", "--preview"), True, "preview one notebook"),
    Combination(("sync", "--notebook", "Foo", "--limit", "3"), True, "both narrow the run"),
    Combination(("sync", "--ssh", "--cloud"), False, "a transport is a choice, not an order"),
    Combination(("sync", "--cloud", "--ssh"), False, "and the typing order does not rescue it"),
    Combination(("sync", "--ssh"), True, "one transport is the point of the flag"),
    Combination(("sync", "--cloud"), True, "and so is the other"),
    Combination(("sync", "--ssh", "--preview"), True, "the exclusion is to --cloud alone"),
    Combination(("sync", "--preview", "--prune"), True, "a preview of what pruning would remove"),
    Combination(("sync", "--notebooks", "--pdf"), True, "the type flags accumulate"),
    Combination(("sync", "--preview", "--all"), True, "--all qualifies --preview"),
    Combination(("sync", "--preview", "--json"), True, "the comparison has a JSON form"),
    Combination(("sync", "--all"), True, "accepted, but inert without --preview"),
    Combination(("sync", "--preview", "--transcribe"), True, "the expensive rehearsal"),
    # Parsed, then refused at dispatch with exit code 2 rather than by
    # argparse: "B requires A" is not something a parser can express, and
    # guessing which half was meant is worse than saying so.
    Combination(("sync", "--transcribe"), True, "the parser takes it; run() refuses it"),
    Combination(("sync", "--preview", "--json"), True, "a machine-readable preview"),
    Combination(("sync", "--limit", "0"), True, "0 is a real limit, and the parser takes it"),
    Combination(("sync", "--limit", "-1"), True, "negative is accepted; the pipeline ignores it"),
    # sync — malformed input.
    Combination(("sync", "--limit", "many"), False, "--limit is typed int"),
    Combination(("sync", "--notebook"), False, "--notebook needs a value"),
    Combination(("sync", "--nonsense"), False, "unknown flags are a usage error"),
    Combination(("sync", "--prev"), True, "argparse accepts unambiguous abbreviations"),
    Combination(("sync", "--s"), False, "--ssh, --ssh-host and --skip-empty all match"),
    # watch — every sync flag, plus its own.
    Combination(("watch", "--interval", "600"), True, "the documented usage"),
    Combination(("watch", "--notebook", "Foo", "--preview"), True, "watch takes every sync option"),
    Combination(("watch", "--ssh", "--cloud"), False, "including sync's exclusions"),
    Combination(("watch", "--interval", "fast"), False, "--interval is typed int"),
    # info and setup take almost nothing, and that is the point.
    Combination(("info",), True, "a bare info prints the report"),
    Combination(("info", "--json"), True, "the only flag info has"),
    Combination(("info", "--preview"), False, "a sync flag on a read-only command"),
    # The retired commands' own flags, refused now as unknown words rather
    # than as flags: the subcommand goes first, so the message names the
    # command, which is the part a caller has to change.
    Combination(("state", "--dump"), False, "state is retired"),
    Combination(("cache", "--clear"), False, "cache is retired"),
    Combination(("status", "--json"), False, "status is now info"),
    Combination(("setup",), True, "the wizard takes no behaviour flags"),
    Combination(("setup", "--json"), False, "the wizard has no machine-readable mode"),
    # Verbosity is accepted on both sides of the subcommand, everywhere.
    Combination(("--verbose", "sync"), True, "before the subcommand"),
    Combination(("sync", "--verbose"), True, "after the subcommand"),
    Combination(("-q", "info"), True, "the short form, before"),
    Combination(("info", "--quiet"), True, "the long form, after"),
    Combination(("--verbose", "sync", "--quiet"), True, "contradictory, but not a usage error"),
)


# ---------------------------------------------------------------------------
# Table 4 — how far each command is allowed to reach
# ---------------------------------------------------------------------------

#: The subsystems a command may set in motion, keyed by command line.
#:
#: This is the "nothing else" half of the matrix, and it is the half that
#: cannot be written as a normal assertion: the interesting fact is which of
#: these did *not* happen. ``info`` must not sync. ``sync --preview`` must not
#: download anything. Each of those is one missing label here, and the test
#: fails on any label that fires and is not listed.
#:
#: Labels are the recorded names in :class:`_Recorder`.
COMMAND_REACH: dict[tuple[str, ...], set[str]] = {
    ("sync",): {"pipeline.construct", "pipeline.run"},
    ("sync", "--preview", "--transcribe"): {"pipeline.construct", "pipeline.run"},
    ("sync", "--notebook", "Foo"): {"pipeline.construct", "pipeline.run"},
    ("sync", "--preview"): {"compare_with_device"},
    ("sync", "--preview", "--json"): {"compare_with_device"},
    ("watch", "--interval", "60"): {"pipeline.construct", "pipeline.run"},
    ("info",): {"collect_status"},
    ("info", "--json"): {"collect_status"},
    ("setup",): {"run_wizard"},
}


class _Recorder:
    """Notes which subsystems a command reached, and stands in for four of them.

    Args:
        state_db: A file to report as the state database, so a command that
            reads it finds one without a real sync having run — and, more to
            the point, never finds the developer's own.
    """

    def __init__(self, state_db: Path) -> None:
        """Start with an empty log."""
        self.calls: list[str] = []
        self.options: list[dict[str, Any]] = []
        self.state_db = state_db
        self.pipeline_error: BaseException | None = None

    def note(self, label: str) -> None:
        """Record that a subsystem was entered.

        Args:
            label: The subsystem's name in :data:`COMMAND_REACH`.
        """
        self.calls.append(label)


@dataclass
class Invocation:
    """What one run of the CLI did.

    Attributes:
        argv: The arguments it was given.
        exit_code: The process status it would have produced.
        stdout: Everything printed to standard output.
        stderr: Everything printed to standard error.
        calls: Subsystem labels, in the order they fired.
        options: The keyword arguments each constructed pipeline was given.
    """

    argv: list[str]
    exit_code: int
    stdout: str
    stderr: str
    calls: list[str] = field(default_factory=list)
    options: list[dict[str, Any]] = field(default_factory=list)


@pytest.fixture(autouse=True)
def restore_environment():
    """Undo environment changes the CLI makes without going through monkeypatch.

    ``dispatch`` exports ``LIVING_INK_CONFIG`` for ``-c`` and ``execute_sync``
    setdefaults ``LIVING_INK_CONFIG_DIR``; both write ``os.environ`` directly,
    so nothing restores them and they outlive the test. That is not a
    hypothetical: a leaked ``LIVING_INK_CONFIG`` overrides the ``repo_dir``
    that ``tests/test_setup_wizard.py`` passes, and four of its tests failed —
    but only when this file ran first.

    Autouse rather than part of the ``cli`` fixture, because a test that calls
    ``main`` directly leaks in exactly the same way.

    Yields:
        None. The teardown is the point.
    """
    saved = os.environ.copy()
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture
def cli(monkeypatch, capsys, tmp_path):
    """Run the real CLI entry point with the outside world disconnected.

    Four seams face outward — the pipeline, the wizard, the status probe and
    the tablet comparison — and all four are replaced, because each of them
    would otherwise reach a tablet, a vault or a terminal prompt. The state
    store and the caches are *not* replaced: they are already redirected to a
    throwaway directory by ``conftest``, so the real ones are safe and a real
    one is a better test than a fake.

    A guard fails the test if anything opens a socket or spawns a process. That
    is the load-bearing half of "nothing else": without it, a command could do
    unrequested work through a path this fixture never thought to patch.

    Args:
        monkeypatch: Pytest's patcher.
        capsys: Pytest's output capture.
        tmp_path: Pytest's per-test directory.

    Returns:
        A callable taking argv strings and returning an :class:`Invocation`.
    """
    from living_ink import cli as cli_module
    from living_ink import pipeline as pipeline_module
    from living_ink import ui as ui_module
    from living_ink.cli import caches as caches_module
    from living_ink.cli import inventory as inventory_module
    from living_ink.cli import status as status_module
    from living_ink.cli.commands import setup as setup_module
    from living_ink.cli.commands import watch as watch_module

    state_db = tmp_path / "state.db"
    state_db.write_bytes(b"")
    recorder = _Recorder(state_db)

    class _RecordingPipeline:
        """Stands in for the pipeline, remembering what it was told to do."""

        def __init__(self, *, config_path=None, data_dir=None, destinations=None, **options):
            """Record the instruction without acting on it.

            The three the front end supplies itself are named so that the rest
            can be collected: what a test asserts on is the instruction the
            flags encode, not where the config happened to live.
            """
            recorder.note("pipeline.construct")
            recorder.options.append(options)

        def run(self) -> bool:
            """Report success without doing any work.

            Returns:
                True, unless the test asked the run to fail.

            Raises:
                BaseException: Whatever ``fails_with`` was set to, so that a
                    test can drive the front end's handling of a failure the
                    pipeline reports by raising.
            """
            recorder.note("pipeline.run")
            if recorder.pipeline_error is not None:
                raise recorder.pipeline_error
            return True

    class _RecordingWizard:
        """Stand in for the interactive wizard, without asking anything."""

        def __init__(self, root=None, bin_dir=None):
            """Accept the same construction the command performs.

            Args:
                root: Ignored.
                bin_dir: Ignored.
            """

        def run(self):
            """Report a saved configuration and no request to sync.

            Returns:
                A result the command turns into exit 0.
            """
            recorder.note("run_wizard")
            return setup_module.WizardResult(saved=True, run_sync_requested=False)

    def _collect_status(config_path):
        """Stand in for the probing status collector.

        Args:
            config_path: The config that would have been read.

        Returns:
            An unpopulated report, which renders as "not configured".
        """
        recorder.note("collect_status")
        return cli_module.StatusReport(config_path=Path(config_path))

    def _compare_with_device(args, root=None):
        """Stand in for the tablet-versus-notes comparison.

        Args:
            args: Parsed arguments.
            root: Ignored.

        Returns:
            An empty comparison.
        """
        recorder.note("compare_with_device")
        return [], [], None

    monkeypatch.setattr(pipeline_module, "SyncPipeline", _RecordingPipeline)
    monkeypatch.setattr(setup_module, "Wizard", _RecordingWizard)
    # Every command in the matrix is asked to run as if a person were
    # watching; the refusal without a terminal is its own test, and leaving
    # it live here would silently turn every `setup` row into an exit 2.
    monkeypatch.setattr(ui_module, "is_tty", lambda: True)
    monkeypatch.setattr(status_module, "collect_status", _collect_status)
    monkeypatch.setattr(inventory_module, "compare_with_device", _compare_with_device)

    def _spy(module, name: str, label: str) -> None:
        """Record entry into a subsystem, then let the real one run.

        Args:
            module: Module holding the callable.
            name: Attribute name.
            label: The name used in :data:`COMMAND_REACH`.
        """
        original = getattr(module, name)

        def wrapper(*args, **kwargs):
            recorder.note(label)
            return original(*args, **kwargs)

        monkeypatch.setattr(module, name, wrapper)

    _spy(caches_module, "all_caches", "all_caches")
    _spy(pipeline_module, "get_state_store", "get_state_store")

    def _state_db_path() -> Path:
        """Point every state reader at a database that exists but holds nothing.

        Returns:
            The throwaway database path.
        """
        recorder.note("state_db_path")
        return recorder.state_db

    monkeypatch.setattr(caches_module, "state_db_path", _state_db_path)

    def _no_network(*args, **kwargs):
        """Fail the test rather than let a command reach the network."""
        raise AssertionError(
            "A command opened a socket. Blackbox behaviour tests must not "
            "touch the network; if a new code path legitimately needs one, "
            "give it a seam this fixture can replace."
        )

    def _no_subprocess(*args, **kwargs):
        """Fail the test rather than let a command spawn a process."""
        raise AssertionError(
            f"A command spawned a process: {args[0] if args else '?'}. "
            "Blackbox behaviour tests must not shell out."
        )

    def _no_waiting(seconds):
        """End a timed loop after one cycle instead of sleeping.

        ``watch`` runs forever by design, and its only exit is Ctrl+C — so a
        test that calls it either waits for the heat death of the suite or
        supplies the interrupt. Raising it from the sleep is the honest place:
        it is exactly what a user pressing Ctrl+C during the wait produces, and
        ``WatchCommand`` already catches it there.

        Args:
            seconds: Ignored.

        Raises:
            KeyboardInterrupt: Always.
        """
        raise KeyboardInterrupt

    monkeypatch.setattr(socket, "socket", _no_network)
    monkeypatch.setattr(subprocess, "run", _no_subprocess)
    monkeypatch.setattr(subprocess, "Popen", _no_subprocess)
    monkeypatch.setattr(watch_module.time, "sleep", _no_waiting)

    def _run(*argv: str, fails_with: BaseException | None = None) -> Invocation:
        """Run the CLI once and describe what happened.

        Args:
            *argv: Arguments as the user would type them.
            fails_with: An exception for the pipeline to raise instead of
                succeeding, for testing how the front end reports it.

        Returns:
            An :class:`Invocation` describing the run.
        """
        recorder.pipeline_error = fails_with
        recorder.calls.clear()
        recorder.options.clear()
        capsys.readouterr()
        try:
            code = cli_module.main(list(argv))
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
        captured = capsys.readouterr()
        return Invocation(
            argv=list(argv),
            exit_code=code or 0,
            stdout=captured.out,
            stderr=captured.err,
            calls=list(recorder.calls),
            options=list(recorder.options),
        )

    return _run


# ---------------------------------------------------------------------------
# Sync: argv in, instruction out
# ---------------------------------------------------------------------------


class TestSyncFlagsMapExactly:
    """Each named command line produces one exact instruction.

    Every assertion compares the whole argument mapping, so these also prove the
    negative: a flag that quietly set a second field would fail here.
    """

    @pytest.mark.parametrize(
        "argv,expected",
        [
            pytest.param(["sync"], BARE_SYNC, id="bare"),
            pytest.param(
                ["sync", "--notebook", "Work/Notes"],
                {**BARE_SYNC, "notebook": "Work/Notes"},
                id="notebook-path",
            ),
            pytest.param(
                ["sync", "--notebook", "abc-123"],
                {**BARE_SYNC, "notebook": "abc-123"},
                id="notebook-id",
            ),
            pytest.param(["sync", "--limit", "5"], with_flags(max_notebooks_per_run=5), id="limit"),
            pytest.param(
                ["sync", "--limit", "0"], with_flags(max_notebooks_per_run=0), id="limit-zero"
            ),
            pytest.param(
                ["sync", "--preview", "--transcribe"],
                {**BARE_SYNC, "dry_run": True},
                id="the-expensive-rehearsal",
            ),
            pytest.param(["sync", "--preview"], BARE_SYNC, id="a-bare-preview-builds-nothing"),
            pytest.param(["sync", "--prune"], with_flags(prune=True), id="prune"),
            pytest.param(["sync", "--ssh"], with_flags(preferred_connection="ssh"), id="ssh"),
            pytest.param(["sync", "--cloud"], with_flags(preferred_connection="cloud"), id="cloud"),
            pytest.param(["sync", "--json"], with_flags(output_json=True), id="json"),
            pytest.param(["sync", "--keep-temp"], {**BARE_SYNC, "keep_temp": True}, id="keep-temp"),
            pytest.param(["sync", "--pdf"], with_flags(sync_types=["pdf"]), id="pdf"),
            pytest.param(
                ["sync", "--notebooks", "--epub"],
                with_flags(sync_types=["notebook", "epub"]),
                id="two-types",
            ),
            pytest.param(
                ["sync", "--notebook", "Foo", "--preview", "--transcribe", "--keep-temp"],
                {**BARE_SYNC, "notebook": "Foo", "dry_run": True, "keep_temp": True},
                id="scoped-rehearsal",
            ),
            pytest.param(
                ["sync", "--pdf", "--limit", "2", "--json"],
                with_flags(sync_types=["pdf"], max_notebooks_per_run=2, output_json=True),
                id="pdfs-two-of-them-as-json",
            ),
            pytest.param(
                ["sync", "--tag", "work", "--tag", "ideas,urgent"],
                with_flags(sync_tags=("work", "ideas", "urgent")),
                id="a-repeated-list-flag-accumulates",
            ),
            pytest.param(
                ["sync", "--no-embed-images"],
                with_flags(obsidian_embed_images=False),
                id="a-negated-switch-arrives-as-false",
            ),
        ],
    )
    def test_the_command_line_means_exactly_this(self, argv, expected):
        """The parsed instruction equals the expectation in every field."""
        assert intent(argv) == expected

    def test_an_omitted_flag_is_absent_from_the_overrides(self):
        """A flag nobody typed leaves no trace in ``flags`` at all.

        This is the whole reason every generated flag defaults to None.
        :meth:`Settings._pick` tests ``is not None`` and nothing else, so a
        a ``prune: False`` here would not read as "unset" — it would read as
        "the user said no" and overrule a config file that has pruning switched
        on, turning an omitted flag into an instruction never given.
        """
        assert intent(["sync"])["flags"] == {}
        assert "prune" not in intent(["sync", "--epub"])["flags"]

    def test_a_namespace_missing_a_flag_reads_as_the_flag_being_unset(self):
        """A partial namespace is a missing flag, not an AttributeError.

        ``watch`` borrows the sync parser and a caller can construct one by
        hand, so every read is a ``getattr`` with the same default the parser
        declares. A flag added to the parser and forgotten here therefore
        degrades to "not given" rather than crashing the run.
        """
        assert sync_arguments(argparse.Namespace()) == BARE_SYNC
        assert sync_arguments(argparse.Namespace(preview=True, transcribe=True))["dry_run"] is True

    def test_a_transport_flag_arrives_as_the_setting_it_sets(self):
        """``--ssh`` travels as ``preferred_connection``, and only as that.

        The translation lives in the schema — the flag is declared on the
        *choice* — so there is exactly one statement anywhere that ``--ssh``
        means SSH. It used to be made twice, once by the parser and again by
        the pipeline, which is two places to keep in step for a fact with one
        source.
        """
        assert intent(["sync", "--ssh"])["flags"] == {"preferred_connection": "ssh"}
        assert intent(["sync", "--cloud"])["flags"] == {"preferred_connection": "cloud"}


class TestEveryFlagCombination:
    """All 512 subsets of the boolean flags, checked against the same rule.

    The rule has two halves. A subset that trips an exclusion is a usage error;
    every other subset is independent, meaning its arguments are exactly the
    union of what each flag does alone. That is a stronger claim than any list
    of hand-picked cases, and it is the claim a user makes when they combine
    two flags and expect both to apply — the exclusions are then the complete,
    enumerated list of places where that expectation does not hold.
    """

    @pytest.mark.parametrize(
        "flags",
        [
            pytest.param(combo, id="+".join(f.lstrip("-") for f in combo) or "none")
            for size in range(len(SYNC_BOOLEAN_FLAGS) + 1)
            for combo in itertools.combinations(sorted(SYNC_BOOLEAN_FLAGS), size)
            if not is_rejected(combo)
        ],
    )
    def test_flags_do_not_interact(self, flags):
        """Combining flags changes exactly the fields those flags own."""
        assert intent(["sync", *flags]) == expected_for(flags)

    def test_the_two_halves_cover_every_subset(self):
        """Accepted plus rejected is all 512, with nothing dropped.

        The split is computed, so this is the guard against it quietly
        narrowing: widen an exclusion by mistake and the sweep would test
        fewer combinations while still reporting green.
        """
        subsets = [
            combo
            for size in range(len(SYNC_BOOLEAN_FLAGS) + 1)
            for combo in itertools.combinations(sorted(SYNC_BOOLEAN_FLAGS), size)
        ]
        assert len(subsets) == 2 ** len(SYNC_BOOLEAN_FLAGS)
        assert sum(1 for combo in subsets if is_rejected(combo)) == 2 ** (
            len(SYNC_BOOLEAN_FLAGS) - 2
        )

    @pytest.mark.parametrize(
        "flags",
        [
            pytest.param(combo, id="+".join(f.lstrip("-") for f in combo))
            for size in range(len(SYNC_BOOLEAN_FLAGS) + 1)
            for combo in itertools.combinations(sorted(SYNC_BOOLEAN_FLAGS), size)
            if is_rejected(combo)
        ],
    )
    def test_an_excluded_combination_is_a_usage_error(self, flags):
        """An exclusion holds no matter what else is on the command line.

        The pair is rejected on its own; this says the other flags cannot
        smuggle it past — an exclusion that only fired for the bare pair would
        be a gap a real command line walks straight through.
        """
        with pytest.raises(SystemExit) as exit_info:
            intent(["sync", *flags])
        assert exit_info.value.code == 2


class TestFlagOrderIsIrrelevant:
    """The same flags in a different order are the same instruction."""

    @pytest.mark.parametrize(
        "first,second",
        [
            (
                ["sync", "--preview", "--transcribe", "--notebook", "Foo"],
                ["sync", "--notebook", "Foo", "--transcribe", "--preview"],
            ),
            (["sync", "--ssh", "--json"], ["sync", "--json", "--ssh"]),
            (
                ["sync", "--limit", "3", "--prune", "--keep-temp"],
                ["sync", "--keep-temp", "--limit", "3", "--prune"],
            ),
        ],
    )
    def test_reordering_changes_nothing(self, first, second):
        """Two spellings of one request resolve identically."""
        assert intent(first) == intent(second)


class TestValueFlagsTakeTheValueGiven:
    """A value flag carries the user's string through untouched."""

    @pytest.mark.parametrize(
        "value",
        [
            "Foo",
            "Work/Notes",
            "Deeply/Nested/Folder/Note",
            "abc-123-def",
            "name with spaces",
            "#tag",
        ],
    )
    def test_the_notebook_string_is_not_rewritten(self, value):
        """Whatever was typed is what the pipeline is asked to find.

        Normalisation belongs to the pipeline's notebook matcher, which has its
        own tests; the CLI's job is to not get in the way of it.
        """
        assert intent(["sync", "--notebook", value])["notebook"] == value

    def test_a_negative_limit_is_carried_not_rejected(self):
        """The parser accepts it and the pipeline decides what it means.

        Pinned because it is a real edge: ``SyncPipeline`` treats anything that
        is not greater than zero as "no override", so a negative limit is inert
        rather than an error, and moving that decision into the parser would
        change the exit code a script sees.
        """
        assert intent(["sync", "--limit", "-1"])["flags"]["max_notebooks_per_run"] == -1


class TestEveryGeneratedFlagSetsItsOwnSetting:
    """Each schema-declared flag reaches its own setting, carrying its own value.

    :data:`SYNC_BOOLEAN_FLAGS` is a hand-written table and can only ever cover
    the flags somebody remembered to add to it. This one is driven from
    :func:`~living_ink.cli.flags.flaggable`, so declaring a new ``flag=`` in
    the schema puts it under test the same day — which is the only way a
    generated parser stays worth generating.

    Three things are asserted at once, and the third is the one a per-flag test
    would miss: the flag lands on its setting, the value survives its type
    reader, and **nothing else in the instruction moves**. A ``dest`` typo
    would fail the first, ``--limit`` losing its ``type=int`` the second, and a
    flag with a real default instead of None the third — that last one is the
    subtle failure, because the override it invents looks like an ordinary
    value all the way down to the config file it silently beats.
    """

    @pytest.mark.parametrize("setting", flaggable("sync"), ids=lambda s: s.field)
    def test_a_flag_sets_its_setting_and_leaves_the_rest_alone(self, setting):
        """Every spelling of one setting produces exactly one override."""
        for words, value in spellings_of(setting):
            parsed = intent(["sync", *words])
            assert parsed["flags"] == {setting.field: value}, words
            assert {k: v for k, v in parsed.items() if k != "flags"} == {
                k: v for k, v in BARE_SYNC.items() if k != "flags"
            }, words

    def test_no_setting_declares_a_flag_it_cannot_be_given_by(self):
        """A declared flag is a typeable flag, on the parser, right now.

        Before the generator the schema declared twenty-eight flags and the
        parser registered ten, so eighteen settings named a spelling argparse
        rejected. The schema was documentation that disagreed with the program.
        """
        surface = _option_strings(_subparsers(LivingInkCLI().build_parser())["sync"])
        assert GENERATED_SYNC_FLAGS <= surface

    def test_a_credential_never_gets_a_flag(self):
        """No secret is typeable, whatever the schema says about it.

        A key on the command line is in the shell history and in the process
        list of every user on the machine. :func:`flaggable` filters these out
        rather than trusting each declaration, and this is what says so.
        """
        assert [s.field for s in flaggable("sync") if s.secret] == []


class TestTheTypeFlagsReplaceRatherThanAdd:
    """``--pdf`` means PDFs, not "PDFs as well as whatever the file says".

    The three used to be ``--pdf`` / ``--epub`` / ``--all-types``, each a
    boolean setting of its own, and each strictly additive: a config syncing
    notebooks could not be narrowed to PDFs by any command line. They are one
    list setting now, and replacement is not special-cased anywhere — it falls
    out of the flags being one layer of :meth:`Settings._pick`, which takes the
    first layer that has an answer and never merges two.
    """

    def test_one_flag_names_one_type(self):
        assert intent(["sync", "--pdf"])["flags"]["sync_types"] == ["pdf"]

    def test_the_flags_accumulate_among_themselves(self):
        """They are one answer being built, not three answers competing."""
        assert intent(["sync", "--pdf", "--epub"])["flags"]["sync_types"] == ["pdf", "epub"]
        assert intent(["sync", "--notebooks", "--pdf", "--epub"])["flags"]["sync_types"] == [
            "notebook",
            "pdf",
            "epub",
        ]

    def test_typing_none_of_them_leaves_the_configured_answer_alone(self):
        """The reason every generated flag defaults to None, in its sharpest form."""
        assert "sync_types" not in intent(["sync"])["flags"]

    def test_a_flag_overrules_the_configured_list_instead_of_extending_it(self):
        """End to end through the resolver, because that is where it happens."""
        config = {"sync": {"types": ["notebook", "epub"]}}
        assert Settings.resolve(config, env={}).sync_types == ("notebook", "epub")

        flags = intent(["sync", "--pdf"])["flags"]
        assert Settings.resolve(config, env={}, flags=flags).sync_types == ("pdf",)


class TestNarrowingOneRunByPath:
    """``--source-path`` and ``--source-regex`` filter, and refuse each other.

    Neither has a config key, on purpose: a permanent substring filter is a
    mistake waiting to be forgotten, and a permanent regex is the same with
    sharper edges. So they travel as named keywords rather than through the
    ``flags`` mapping, which is what stops a config file from being able to
    set them at all.
    """

    def test_a_path_filter_reaches_the_pipeline(self):
        assert intent(["sync", "--source-path", "Work/"])["source_path"] == "Work/"

    def test_a_regex_filter_reaches_the_pipeline(self):
        assert intent(["sync", "--source-regex", r"^Journal/"])["source_regex"] == r"^Journal/"

    def test_neither_is_a_setting(self):
        """They stay out of ``flags``, which is the layer a config can answer."""
        given = intent(["sync", "--source-path", "Work/"])
        assert "source_path" not in given["flags"]

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["sync", "--source-path", "a", "--source-regex", "b"], id="path-first"),
            pytest.param(["sync", "--source-regex", "b", "--source-path", "a"], id="regex-first"),
            pytest.param(["watch", "--source-path", "a", "--source-regex", "b"], id="watch"),
        ],
    )
    def test_the_two_together_are_a_usage_error(self, argv):
        """Same field, two rules: a precedence between them would hide a typo."""
        with pytest.raises(SystemExit) as exit_info:
            intent(argv)
        assert exit_info.value.code == 2

    def test_the_clash_names_both_flags(self, cli):
        run = cli("sync", "--source-path", "a", "--source-regex", "b")
        assert run.exit_code == 2
        assert "--source-path" in run.stderr and "--source-regex" in run.stderr
        assert run.calls == []

    def test_a_pattern_that_will_not_compile_is_refused_at_the_parser(self, cli):
        """Before the transport, before the listing, before a page is rendered.

        A regex is checked by ``type=`` rather than when the selection runs,
        so the cost of a typo is a usage error and not a traceback out of the
        middle of a sync that has already downloaded something.
        """
        run = cli("sync", "--source-regex", "Work(")
        assert run.exit_code == 2
        assert "Work(" in run.stderr
        assert run.calls == []

    def test_a_valid_pattern_survives_unchanged(self, tmp_path):
        """Handed on as typed — it is compiled again where it is used."""
        from living_ink.pipeline import SyncPipeline

        pipe = SyncPipeline(
            **intent(["sync", "--source-regex", r"^Journal/diary-\d+"]),
            data_dir=tmp_path,
            destinations=[],
        )
        assert pipe._criteria().source_regex == r"^Journal/diary-\d+"


class TestForcingARepublish:
    """``--force`` is the non-destructive replacement for ``state --forget``.

    Forcing a re-sync never needed to *delete* anything: ``--forget`` mutated
    the database and hoped the next run repaired it, leaving a "forgot it but
    then the sync failed" state that simply does not exist here.
    """

    def test_it_reaches_the_criteria(self, tmp_path):
        from living_ink.pipeline import SyncPipeline

        pipe = SyncPipeline(**intent(["sync", "--force"]), data_dir=tmp_path, destinations=[])
        assert pipe._criteria().force is True

    def test_an_ordinary_run_does_not_force(self, tmp_path):
        from living_ink.pipeline import SyncPipeline

        pipe = SyncPipeline(**intent(["sync"]), data_dir=tmp_path, destinations=[])
        assert pipe._criteria().force is False

    def test_naming_a_notebook_still_forces_without_the_flag(self, tmp_path):
        """The one thing that behaved like ``--force`` before it existed.

        Naming a document is asking for that document, whether or not the
        comparison thinks it is current, and growing a real flag must not
        quietly take that away.
        """
        from living_ink.pipeline import SyncPipeline

        pipe = SyncPipeline(
            **intent(["sync", "--notebook", "Standup"]), data_dir=tmp_path, destinations=[]
        )
        assert pipe._criteria().force is True


class TestForcingBothTransports:
    """``--ssh --cloud`` is refused; each one alone selects its transport.

    A transport is a choice, not a preference order. Silently picking one of
    the two would sync from a source the user did not ask for — over USB when
    they meant the Cloud, or the other way round — and the two do not
    necessarily hold the same documents. Refusing at the parser makes that a
    visible usage error before anything connects.
    """

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["sync", "--ssh", "--cloud"], id="ssh-first"),
            pytest.param(["sync", "--cloud", "--ssh"], id="cloud-first"),
            pytest.param(["watch", "--ssh", "--cloud"], id="watch"),
            pytest.param(["sync", "--ssh", "--keep-temp", "--cloud"], id="separated"),
        ],
    )
    def test_the_parser_refuses_both(self, argv):
        """Exit code 2, whatever the order or what sits between them."""
        with pytest.raises(SystemExit) as exit_info:
            intent(argv)
        assert exit_info.value.code == 2

    def test_the_error_names_the_flags_that_clash(self, cli):
        """The message points at the pair, not just at "usage".

        A user who typed both needs to know which two to choose between, and
        argparse only says so if the flags are in one exclusive group.
        """
        run = cli("sync", "--ssh", "--cloud")
        assert run.exit_code == 2
        assert "--ssh" in run.stderr and "--cloud" in run.stderr
        assert run.calls == []

    def test_ssh_alone_selects_ssh(self, tmp_path):
        """One flag, one transport, read back from the resolved settings."""
        from living_ink.pipeline import SyncPipeline

        pipe = SyncPipeline(**intent(["sync", "--ssh"]), data_dir=tmp_path, destinations=[])
        assert pipe.settings.preferred_connection == "ssh"
        assert pipe.settings.use_ssh is True

    def test_cloud_alone_selects_cloud(self, tmp_path):
        """And the other one, so the exclusion did not disable a flag."""
        from living_ink.pipeline import SyncPipeline

        pipe = SyncPipeline(**intent(["sync", "--cloud"]), data_dir=tmp_path, destinations=[])
        assert pipe.settings.preferred_connection == "cloud"
        assert pipe.settings.use_ssh is False


class TestAnUnreachableTabletIsReportedNotRaised:
    """The end of the fallback ladder is a sentence, not a stack trace.

    Choosing a transport and reaching one are different questions. The parser
    settles the first: ``--ssh`` and ``--cloud`` cannot both be given, so the
    preference is never ambiguous. The transport layer settles the second, and
    it falls back — a preference that cannot be honoured is served by the other
    route rather than refused, which is what makes ``--ssh`` safe to keep in a
    script that sometimes runs with the cable out. ``tests/test_api.py`` owns
    that ladder rung by rung.

    What is left is the bottom rung, where neither route answers. That is an
    ordinary state — an unplugged cable, a Cloud that was never configured —
    and it is the front end's job to say so.
    """

    def test_it_exits_one_with_the_reason_and_no_traceback(self, cli):
        """The message survives; the frames do not.

        A traceback here would claim the tool is broken when the tablet is
        merely unplugged, and would bury the one line telling the user what to
        plug in under frames they cannot act on.
        """
        from living_ink.transport import TransportUnavailable

        run = cli(
            "sync",
            fails_with=TransportUnavailable("Could not connect to reMarkable tablet via USB SSH."),
        )
        assert run.exit_code == 1
        assert "Could not connect to reMarkable tablet via USB SSH." in run.stderr
        assert "Traceback" not in run.stderr

    def test_it_is_reported_the_same_way_for_a_forced_transport(self, cli):
        """``--ssh`` and ``--cloud`` do not get a different failure shape."""
        from living_ink.transport import TransportUnavailable

        for flag in ("--ssh", "--cloud"):
            run = cli("sync", flag, fails_with=TransportUnavailable("no route"))
            assert run.exit_code == 1, flag
            assert "no route" in run.stderr, flag
            assert "Traceback" not in run.stderr, flag

    def test_nothing_is_published_when_there_was_no_route(self, cli):
        """A failed connection ends the run rather than half-running it."""
        from living_ink.transport import TransportUnavailable

        run = cli("sync", fails_with=TransportUnavailable("no route"))
        assert run.calls == ["pipeline.construct", "pipeline.run"]

    def test_watch_survives_a_cycle_with_no_route(self, cli):
        """One unreachable cycle is not the end of the daemon.

        ``watch`` is what the compose service runs, and a tablet is unplugged
        far more often than it is broken; exiting on the first missed cycle
        would mean a daemon that stops the first time someone takes the cable
        to another room.
        """
        from living_ink.transport import TransportUnavailable

        run = cli("watch", "--interval", "1", fails_with=TransportUnavailable("no route"))
        # 130, not 0: the loop ended because the harness pressed Ctrl+C during
        # the wait, and a watch has no other way out. The missed cycle is
        # visible as a completed ``pipeline.run``, not as the exit code.
        assert run.exit_code == 130
        assert "pipeline.run" in run.calls


class TestTheFourExitCodes:
    """0, 1, 2 and 130 are the interface a script branches on.

    They were not four before: a command called ``sys.exit`` directly, ``watch``
    answered a Ctrl+C with 0, and an interrupt at a prompt was reported as an
    ordinary failure. Anything that ends a run has to land on one of these four,
    and each one has to mean only what it says.
    """

    def test_a_completed_sync_is_zero(self, cli):
        assert cli("sync").exit_code == 0

    def test_a_sync_that_could_not_finish_is_one(self, cli):
        from living_ink.transport import TransportUnavailable

        assert cli("sync", fails_with=TransportUnavailable("no route")).exit_code == 1

    def test_a_usage_error_is_two(self, cli):
        assert cli("sync", "--limit", "half").exit_code == 2

    def test_an_interrupt_anywhere_is_a_hundred_and_thirty(self, cli):
        """Not 1, and above all not 0 — the user stopped it, nothing failed."""
        assert cli("sync", fails_with=KeyboardInterrupt()).exit_code == 130

    @pytest.mark.parametrize("command", ["sync", "watch", "setup", "info"])
    def test_a_command_never_exits_the_process_itself(self, command):
        """``watch`` runs ``sync`` in a loop, and a process exit cannot be caught.

        ``SyncCommand.run`` used to call ``sys.exit(1)`` on a failed sync,
        which would have ended the daemon on the first bad cycle the moment
        the loop called anything but ``execute_sync``. ``main`` is the one
        place that exits.
        """
        tree = ast.parse(inspect.getsource(LivingInkCLI().commands[command]))
        called = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}

        assert not called & {"sys.exit", "exit", "quit", "os._exit"}

    def test_help_says_what_they_mean(self, cli):
        """A code nobody documented is one a script cannot safely branch on."""
        run = cli("--help")

        for code in ("0", "1", "2", "130"):
            assert f"  {code}" in run.stdout, code


class TestPreviewIsADifferentCommandInDisguise:
    """``sync --preview`` answers a question instead of doing the work."""

    def test_the_preview_flag_is_not_part_of_the_instruction(self):
        """It never reaches the pipeline, because no sync is run."""
        assert not hasattr(intent(["sync", "--preview"]), "preview")

    def test_all_without_preview_is_accepted_and_inert(self, cli):
        """``sync --all`` parses, and runs an ordinary sync.

        ``--all`` is only read by the comparison renderer, so on its own it
        changes nothing. Pinned because "accepted but does nothing" is exactly
        the kind of behaviour that gets accidentally fixed into an error.
        """
        run = cli("sync", "--all")
        assert run.options == [BARE_SYNC]
        assert "compare_with_device" not in run.calls


class TestPreviewAndItsExpensiveVariant:
    """Two flags, three instructions, and only one of them costs money.

    ``--preview`` used to be two flags: ``--status`` asked the cheap question
    and ``--dry-run`` did the whole run and threw the publish away. Naming them
    as unrelated things hid that they answer the same question at two prices,
    so they are one flag and a modifier now. What that costs is a rule argparse
    cannot express — ``--transcribe`` means nothing alone — which is why the
    refusal is tested here rather than assumed from the parser.
    """

    def test_a_bare_preview_never_builds_a_pipeline(self, cli):
        """The cheap question: metadata only, no download, no OCR, no API call.

        Asserted as the *whole* call list, because the failure this guards
        against is a preview that quietly grew a pipeline and started paying
        for the answer it promised was free.
        """
        run = cli("sync", "--preview")
        assert run.exit_code == 0
        assert run.calls == ["compare_with_device"]

    def test_the_expensive_variant_runs_the_pipeline_with_publishing_off(self, cli):
        """``--preview --transcribe`` is the old ``--dry-run``, spelled out."""
        run = cli("sync", "--preview", "--transcribe")
        assert run.exit_code == 0
        assert run.calls == ["pipeline.construct", "pipeline.run"]
        assert run.options[0]["dry_run"] is True

    def test_transcribe_alone_is_refused_before_anything_connects(self, cli):
        """A rehearsal nobody asked to watch is a sync that wastes its work.

        Exit 2 rather than 1: it is a usage error, the same class of mistake as
        a misspelled flag, and the parser cannot state "B requires A" itself.
        """
        run = cli("sync", "--transcribe")
        assert run.exit_code == 2
        assert run.calls == []
        assert "--transcribe only means something with --preview." in run.stderr

    def test_a_rehearsal_no_longer_implies_keeping_the_temp_files(self, cli):
        """One flag turning on another is a third concept nobody asked for.

        ``--dry-run`` used to set ``keep_temp`` behind the user's back, so a
        rehearsal left artifacts a real sync would have purged. Wanting them is
        a separate request with a flag of its own.
        """
        run = cli("sync", "--preview", "--transcribe")
        assert run.options[0]["keep_temp"] is False

    def test_the_temp_files_are_kept_when_they_are_asked_for(self, cli):
        """The other half of the pair: explicit still works."""
        run = cli("sync", "--preview", "--transcribe", "--keep-temp")
        assert run.options[0]["keep_temp"] is True


# ---------------------------------------------------------------------------
# The shortcut is honest
# ---------------------------------------------------------------------------


class TestTheShortcutMatchesTheRealThing:
    """What :func:`intent` predicts is what the real entry point delivers.

    Everything above runs at the parse layer for speed. This is the test that
    makes those results statements about ``living-ink`` rather than about a
    helper: it drives ``cli.main`` and compares the arguments the pipeline was
    actually constructed with.
    """

    @pytest.mark.parametrize(
        "argv",
        [
            ["sync"],
            ["sync", "--preview", "--transcribe"],
            ["sync", "--notebook", "Work/Notes", "--limit", "2"],
            ["sync", "--pdf", "--keep-temp", "--json"],
            ["sync", "--ssh", "--prune"],
            ["sync", "--pdf", "--epub"],
            ["sync", "--ai-model", "gemini-2.5-flash", "--tag", "work,ideas"],
        ],
    )
    def test_the_pipeline_receives_the_predicted_arguments(self, cli, argv):
        """One pipeline, built with exactly the predicted instruction."""
        run = cli(*argv)
        assert run.options == [intent(argv)]


# ---------------------------------------------------------------------------
# Compatibility
# ---------------------------------------------------------------------------


class TestFlagCompatibility:
    """Legal combinations stay legal; illegal ones stay illegal."""

    @pytest.mark.parametrize(
        "case",
        [pytest.param(c, id=" ".join(c.argv).replace("--", "")) for c in FLAG_COMPATIBILITY],
    )
    def test_the_parser_agrees_with_the_matrix(self, case):
        """Parsing succeeds or exits 2, exactly as the table says."""
        parser = LivingInkCLI().build_parser()
        if case.legal:
            parser.parse_args(list(case.argv))
            return
        with pytest.raises(SystemExit) as exit_info:
            parser.parse_args(list(case.argv))
        assert exit_info.value.code == 2, case.why

    def test_a_rejected_combination_exits_two_through_the_real_entry_point(self, cli):
        """Usage errors reach the shell as 2, not 1.

        Worth asserting separately: ``main`` re-raises non-zero codes through
        ``sys.exit``, and a command that returned 1 for a usage error would be
        indistinguishable from a failed sync in a script.
        """
        run = cli("sync", "--ssh", "--cloud")
        assert run.exit_code == 2
        assert "not allowed with" in run.stderr

    def test_the_declared_exclusions_are_the_only_ones(self):
        """Exactly the pairs in the table are refused, and no others.

        Stated as a sweep rather than a list so that adding a flag extends the
        claim automatically: a new exclusive group fails here until it is
        written into :data:`MUTUALLY_EXCLUSIVE_SYNC_FLAGS`, and an exclusion
        that silently disappears fails here too.
        """
        parser = LivingInkCLI().build_parser()
        for pair in itertools.combinations(sorted(SYNC_BOOLEAN_FLAGS), 2):
            if is_rejected(pair):
                with pytest.raises(SystemExit):
                    parser.parse_args(["sync", *pair])
            else:
                parser.parse_args(["sync", *pair])


class TestAbbreviationsAreAccepted:
    """Any unambiguous prefix of a flag works, because argparse allows it.

    This is not a decision anyone made — ``allow_abbrev`` defaults to True — and
    it is pinned here because it is a compatibility surface with teeth. Every
    accepted abbreviation is a spelling some script now depends on, and adding
    a flag that shares a prefix silently turns that script's argument into an
    "ambiguous option" error. ``--prev`` breaks the day ``--preview-only`` ships.

    Turning ``allow_abbrev`` off is a defensible fix; doing it by accident is
    not, which is what this test prevents.
    """

    @pytest.mark.parametrize(
        "abbreviated,full",
        [
            (["sync", "--prev"], ["sync", "--preview"]),
            (["sync", "--keep"], ["sync", "--keep-temp"]),
            (["sync", "--pru"], ["sync", "--prune"]),
            (["sync", "--li", "4"], ["sync", "--limit", "4"]),
        ],
    )
    def test_a_prefix_means_the_flag_it_abbreviates(self, abbreviated, full):
        """The shortened form resolves to the identical instruction."""
        assert intent(abbreviated) == intent(full)

    @pytest.mark.parametrize("prefix", ["--s", "--ssh-", "--note", "--ai"])
    def test_an_ambiguous_prefix_is_a_usage_error(self, prefix):
        """Several flags match, so argparse refuses to guess.

        ``--ai`` is the one to watch: generating the parser from the schema
        turned one ``--ai-model`` into six ``--ai-*`` flags, so a prefix that
        would have been unambiguous with a hand-written parser is not, and the
        only warning a user gets is this error rather than the wrong setting.
        ``--note`` is the cost of the two spellings the product wants: one
        document is ``--notebook Foo`` and one *type* is ``--notebooks``, and
        nothing shorter than either can tell them apart.
        """
        parser = LivingInkCLI().build_parser()
        with pytest.raises(SystemExit) as exit_info:
            parser.parse_args(["sync", prefix])
        assert exit_info.value.code == 2

    def test_an_exact_match_beats_a_longer_flag_it_prefixes(self):
        """``--notebook`` is ``--notebook``, not a prefix of ``--notebooks``.

        Without argparse's exact-match rule this would be ambiguous, and the
        two mean entirely different things: one names a document, the other
        names a type.
        """
        assert intent(["sync", "--notebook", "Foo"]) == {**BARE_SYNC, "notebook": "Foo"}
        assert intent(["sync", "--notebooks"])["flags"]["sync_types"] == ["notebook"]


class TestVerbosityIsAcceptedOnBothSides:
    """``--verbose`` before or after the subcommand means the same thing.

    ``test_cli.py`` already checks the parsed values; what this adds is that
    the *placement* is free on every command, which is the part that breaks
    when a new subparser forgets ``add_verbosity_args``.
    """

    @pytest.mark.parametrize("command", sorted(set(COMMAND_SURFACE) - {""}))
    @pytest.mark.parametrize("flag", ["--verbose", "--quiet", "-q"])
    def test_both_placements_parse_for_every_command(self, command, flag):
        """Neither ordering is a usage error."""
        parser = LivingInkCLI().build_parser()
        parser.parse_args([flag, command])
        parser.parse_args([command, flag])


# ---------------------------------------------------------------------------
# Reach
# ---------------------------------------------------------------------------


class TestCommandsDoOnlyTheirOwnWork:
    """Each command sets in motion exactly the subsystems it needs.

    The interesting half of every row is what is *missing* from it. ``info``
    has no pipeline, ``sync --preview`` has no pipeline either, and ``setup``
    stops at the wizard. A command that grows an extra step fails here even if
    the step works perfectly, which is the point.
    """

    @pytest.mark.parametrize(
        "argv,allowed",
        [pytest.param(a, s, id=" ".join(a).replace("--", "")) for a, s in COMMAND_REACH.items()],
    )
    def test_nothing_outside_the_allowance_is_touched(self, cli, argv, allowed):
        """No subsystem fires that the table does not list."""
        run = cli(*argv)
        assert set(run.calls) == allowed, f"{argv} reached {sorted(set(run.calls))}"

    def test_the_rehearsal_still_builds_the_pipeline_and_nothing_more(self, cli):
        """``--preview --transcribe`` is a pipeline mode, not a second path.

        A bare ``--preview`` short-circuits in the CLI and never connects; the
        expensive variant is the opposite, and if it were short-circuited too
        the transcripts it exists to produce would never be written.
        """
        run = cli("sync", "--preview", "--transcribe")
        assert run.calls == ["pipeline.construct", "pipeline.run"]
        assert run.options[0]["dry_run"] is True

    def test_the_read_only_command_never_opens_a_transport(self, cli):
        """``info`` reports the setup; it does not go and use it.

        The stores it does read are inside ``collect_status``, which the
        fixture replaces — what this pins is the outer boundary: no pipeline,
        no comparison, no wizard, under either form of the command.
        """
        for argv in (("info",), ("info", "--json")):
            assert cli(*argv).calls == ["collect_status"]

    def test_setup_runs_the_wizard_and_stops_there(self, cli):
        """A wizard that declines the offered sync does not sync.

        The offer is real — ``SetupCommand`` chains into ``SyncCommand`` when
        the user says yes — so "no" has to actually mean no.
        """
        run = cli("setup")
        assert run.calls == ["run_wizard"]
        assert run.options == []


class TestCommandRouting:
    """The command word chooses the command, and the bare form has a rule."""

    @pytest.mark.parametrize(
        "argv,expected",
        [
            (["sync"], "pipeline.construct"),
            (["watch", "--interval", "60"], "pipeline.construct"),
            (["setup"], "run_wizard"),
            (["info"], "collect_status"),
        ],
    )
    def test_the_word_picks_the_command(self, cli, argv, expected):
        """Each subcommand enters its own subsystem first."""
        assert cli(*argv).calls[0] == expected

    def test_a_bare_invocation_syncs_when_configured(self, cli, tmp_path, monkeypatch):
        """``living-ink`` with a config present runs a sync."""
        config = tmp_path / "config.yml"
        config.write_text("remarkable:\n  preferred_connection: ssh\n", encoding="utf-8")
        monkeypatch.setenv("LIVING_INK_CONFIG", str(config))

        assert "pipeline.construct" in cli().calls

    def test_a_bare_invocation_sets_up_when_not_configured(self, cli, tmp_path, monkeypatch):
        """``living-ink`` with no config runs the wizard instead.

        This is the first thing a new user experiences, and it is decided by a
        single ``exists()`` check in ``dispatch`` that nothing else guards.
        """
        monkeypatch.setenv("LIVING_INK_CONFIG", str(tmp_path / "absent.yml"))

        assert cli().calls == ["run_wizard"]

    def test_the_config_flag_redirects_where_config_is_read_from(self, cli, tmp_path):
        """``-c`` points the whole run at another file.

        Asserted through the environment variable because that is the mechanism
        — ``dispatch`` exports ``LIVING_INK_CONFIG`` — and every later reader,
        including ones with no access to the parsed arguments, goes through it.
        """
        config = tmp_path / "elsewhere.yml"
        config.write_text("destinations: {}\n", encoding="utf-8")

        cli("-c", str(config), "info")
        assert os.environ["LIVING_INK_CONFIG"] == str(config.resolve())


# ---------------------------------------------------------------------------
# Output reflects input
# ---------------------------------------------------------------------------


class TestInfoReportsWhatItWasGiven:
    """``info --json`` echoes the configuration it read, not a default.

    The probes are real here — this is the one place the network stubs are
    replaced by verification stubs instead of removed — because the question is
    whether the *config* survives the trip to the output, and a report that
    quietly substituted a default would still look healthy.
    """

    @pytest.fixture
    def configured(self, tmp_path, monkeypatch):
        """Write a config with values nothing would produce by accident.

        Args:
            tmp_path: Pytest's per-test directory.
            monkeypatch: Pytest's patcher.

        Returns:
            A tuple of the config path and the values written.
        """
        from living_ink import setup_wizard as wizard_module
        from living_ink.cli import status as status_module

        vault = tmp_path / "MyVault"
        (vault / ".obsidian").mkdir(parents=True)

        values = {
            "vault": str(vault),
            "root_folder": "reMarkable Imports",
            "provider": "groq",
            "model": "a-very-specific-model",
            "ssh_host": "10.11.99.7",
            "attachments_folder": "Scribbles",
        }
        # The section names are the ones the wizard writes and the schema
        # declares: a destination is a top-level key, not nested under a
        # ``destinations:`` map.
        config = tmp_path / "config.yml"
        config.write_text(
            "remarkable:\n"
            "  preferred_connection: ssh\n"
            "  use_ssh: true\n"
            f"  ssh_host: {values['ssh_host']}\n"
            "ai:\n"
            f"  provider: {values['provider']}\n"
            f"  model: {values['model']}\n"
            "obsidian:\n"
            "  enabled: true\n"
            f"  vault_path: {values['vault']}\n"
            f"  root_folder: {values['root_folder']}\n"
            f"  attachments_folder: {values['attachments_folder']}\n",
            encoding="utf-8",
        )

        # Only the outward-facing probes are stubbed; ``collect_status`` itself
        # runs for real, so the config → report mapping under test is the
        # production one rather than a restatement of the fixture.
        from living_ink import api as api_module

        monkeypatch.setattr(
            wizard_module, "verify_remarkable_ssh", lambda **kw: (True, "connected")
        )
        monkeypatch.setattr(wizard_module, "verify_remarkable_token", lambda token: (True, "ok"))
        monkeypatch.setattr(wizard_module, "verify_ai_provider", lambda *a, **kw: (True, "ok"))
        monkeypatch.setattr(status_module, "_describe_connected_device", lambda *a, **kw: "")
        monkeypatch.setattr(api_module, "resolve_stored_token", lambda **kwargs: "")

        return config, values

    def _report(self, configured, capsys) -> dict:
        """Run ``info --json`` against the written config.

        Args:
            configured: The fixture's config path and values.
            capsys: Pytest's output capture.

        Returns:
            The parsed JSON report.
        """
        from living_ink.cli import main

        config, _ = configured
        capsys.readouterr()
        try:
            main(["-c", str(config), "info", "--json"])
        except SystemExit:
            pass
        return json.loads(capsys.readouterr().out)

    def test_the_vault_in_the_output_is_the_vault_in_the_config(self, configured, capsys):
        """A path the tool could not have guessed comes back unchanged."""
        _, values = configured
        assert values["vault"] in json.dumps(self._report(configured, capsys))

    def test_the_provider_and_model_survive_the_round_trip(self, configured, capsys):
        """The AI section is reported, not re-derived from defaults."""
        _, values = configured
        body = json.dumps(self._report(configured, capsys))
        assert values["provider"] in body
        assert values["model"] in body

    def test_every_configured_value_appears_somewhere_in_the_output(self, configured, capsys):
        """Nothing the user wrote is dropped on the way to the report.

        Stated as a sweep over the whole fixture rather than one assertion per
        key, so that a value added to the config has to be accounted for.
        """
        _, values = configured
        body = json.dumps(self._report(configured, capsys))
        missing = [name for name, value in values.items() if value not in body]
        assert not missing, f"info did not report: {missing}"

    def test_the_configured_transport_preference_is_what_is_reported(self, configured, capsys):
        """``preferred_connection: ssh`` reads back as ssh.

        The default is cloud, so a report that lost the config would say the
        opposite of what the user asked for.
        """
        body = json.dumps(self._report(configured, capsys))
        assert '"ssh"' in body or "'ssh'" in body

    def test_the_config_path_reported_is_the_one_that_was_passed(self, configured, capsys):
        """``-c`` is honoured all the way to the output."""
        config, _ = configured
        assert str(config) in json.dumps(self._report(configured, capsys))

    def test_the_json_output_is_the_only_thing_printed(self, configured, capsys):
        """Nothing else lands on stdout, so the output stays parseable.

        A stray print — a library warning, a progress line — makes ``--json``
        unusable in a pipeline, and that failure is invisible to any test that
        does not parse the whole stream.
        """
        self._report(configured, capsys)  # would raise if stdout held anything else


# ---------------------------------------------------------------------------
# The surface itself
# ---------------------------------------------------------------------------


def _option_strings(parser: argparse.ArgumentParser) -> set[str]:
    """Collect every option string a parser accepts.

    Reads ``_actions`` because argparse offers no public equivalent, and the
    alternative — parsing ``--help`` text — is worse.

    Args:
        parser: The parser to inspect.

    Returns:
        Every long and short option string, including ``-h``.
    """
    return {opt for action in parser._actions for opt in action.option_strings}


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    """Return the subcommand parsers by name.

    Args:
        parser: The top-level parser.

    Returns:
        A mapping of command name to its parser.
    """
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


class TestTheSurfaceIsPinned:
    """The set of commands and flags is a snapshot that must be updated on purpose.

    Without this the matrix silently stops being exhaustive: a new flag would
    be untested, and every other test here would still pass. Breaking this test
    is the intended way to find out that a flag needs a row in the tables above.
    """

    def test_the_commands_are_exactly_these(self):
        """No command appeared or disappeared."""
        parser = LivingInkCLI().build_parser()
        assert set(_subparsers(parser)) == set(COMMAND_SURFACE) - {""}

    def test_the_top_level_flags_are_exactly_these(self):
        """The bare ``living-ink`` surface is unchanged."""
        assert _option_strings(LivingInkCLI().build_parser()) == COMMAND_SURFACE[""]

    @pytest.mark.parametrize("command", sorted(set(COMMAND_SURFACE) - {""}))
    def test_each_command_accepts_exactly_these_flags(self, command):
        """Per-command flags match the table, in both directions."""
        assert (
            _option_strings(_subparsers(LivingInkCLI().build_parser())[command])
            == (COMMAND_SURFACE[command])
        )

    def test_every_sync_boolean_flag_has_a_declared_meaning(self):
        """No hand-registered boolean sync flag escapes the tables.

        The exhaustive combination pass only covers flags
        :data:`SYNC_BOOLEAN_FLAGS` names, so a store_true flag missing from it
        would be excluded from the sweep without anything saying so.

        The generated flags are subtracted because
        :class:`TestEveryGeneratedFlagSetsItsOwnSetting` already covers all of
        them, from the schema, with no table to fall out of date. What is left
        is the handful somebody wrote by hand, which is exactly the set that
        needs a human to say what it means.
        """
        parser = _subparsers(LivingInkCLI().build_parser())["sync"]
        booleans = {
            opt
            for action in parser._actions
            if isinstance(action, argparse._StoreTrueAction)
            for opt in action.option_strings
        }
        undeclared = (
            booleans
            - set(SYNC_BOOLEAN_FLAGS)
            - set(SYNC_FLAGS_OUTSIDE_THE_OPTIONS)
            - GENERATED_SYNC_FLAGS
        )
        assert not undeclared, f"add these to SYNC_BOOLEAN_FLAGS or explain them: {undeclared}"

    def test_watch_accepts_every_sync_flag(self):
        """A watch can be scoped exactly the way a single sync can.

        The design intends to take this away — 1.0 gives ``watch`` no behaviour
        flags at all — so this records what is true now and will fail loudly on
        the day that changes, which is when the tables above need rewriting.
        """
        parsers = _subparsers(LivingInkCLI().build_parser())
        assert _option_strings(parsers["sync"]) <= _option_strings(parsers["watch"])


class TestSetupWritesOnlyWhatItWasTold:
    """The wizard's answers become the config, and nothing else happens.

    ``tests/test_setup_wizard.py`` already checks that each flow writes the
    right keys, so that is not repeated here. What it does not check is the
    other half of the same question, and it is the half this file exists for:
    the wizard must not write a second file, must not reach the network or a
    subprocess, and must not ask a question that is not one of its steps.

    The conversation is drivable because every question is a widget in
    :mod:`living_ink.ui` — the fixture replaces the six of them, so there is no
    TTY, no keystroke and no patching of builtins.
    """

    @pytest.fixture
    def wizard(self, tmp_path, monkeypatch):
        """Drive the wizard from a scripted list of answers.

        Args:
            tmp_path: Pytest's per-test directory.
            monkeypatch: Pytest's patcher.

        Returns:
            A callable taking answers and returning a record of the run.
        """
        from living_ink import setup_wizard as wizard_module
        from living_ink import ui as ui_module
        from living_ink.cli.commands import setup as setup_module

        vault = tmp_path / "MyVault"
        (vault / "Living Ink").mkdir(parents=True)

        monkeypatch.setattr(
            wizard_module,
            "detect_obsidian_vaults",
            lambda: [{"name": "MyVault", "path": str(vault)}],
        )
        monkeypatch.setattr(wizard_module, "verify_remarkable_token", lambda token: (True, "OK"))
        monkeypatch.setattr(wizard_module, "verify_remarkable_ssh", lambda **kw: (True, "OK"))
        monkeypatch.setattr(wizard_module, "verify_ai_provider", lambda *a, **kw: (True, "OK"))
        monkeypatch.setattr(
            wizard_module, "get_existing_remarkable_token", lambda: "existing-token"
        )
        # The closing estimate is a real ``--preview`` against the tablet, which
        # is the one thing in the flow that legitimately reaches the network.
        # Its own behaviour is tested in ``tests/test_wizard.py``.
        monkeypatch.setattr(setup_module.Wizard, "estimate", lambda self: None)

        def _run(script: list[tuple[str, object]] | None = None):
            """Run the wizard against an expected conversation.

            Args:
                script: Pairs of (expected fragment of the question, answer).
                    Defaults to the walkthrough for this platform.

            Returns:
                A tuple of the wizard result, the questions it asked, and the
                paths it left behind under ``tmp_path``.
            """
            steps = iter(script if script is not None else walkthrough_for_this_platform())
            asked: list[str] = []

            def _answer(message: str, *_args, **_kwargs):
                """Answer one question, checking it is the one expected next.

                Args:
                    message: What the wizard asked.

                Returns:
                    The scripted answer.

                Raises:
                    AssertionError: If the wizard asked something unscripted.
                """
                asked.append(message)
                try:
                    fragment, reply = next(steps)
                except StopIteration:
                    raise AssertionError(
                        f"The wizard asked a question the script does not cover: {message!r}. "
                        "Add it to CLOUD_WALKTHROUGH."
                    ) from None
                assert fragment in message, (
                    f"expected a question about {fragment!r}, got {message!r}"
                )
                return str(vault) if reply == THE_DETECTED_VAULT else reply

            for widget in ("select", "checkbox", "confirm", "text", "password", "path"):
                monkeypatch.setattr(ui_module, widget, _answer)

            result = setup_module.Wizard(root=tmp_path, bin_dir=tmp_path / "bin").run()
            written = sorted(
                p.relative_to(tmp_path).as_posix()
                for p in tmp_path.rglob("*")
                if p.is_file() and "MyVault" not in p.parts
            )
            return result, asked, written

        return _run

    def test_it_leaves_exactly_the_files_it_should(self, wizard):
        """A config, two credentials, and a wrapper the user was never asked about.

        The wrapper is the interesting one. ``install_cli_command`` runs as part
        of the walkthrough rather than behind a prompt, so a full setup puts a
        file on the user's PATH without that being one of the four steps it
        announces. That is defensible — a CLI you cannot invoke is not set up —
        but it is not free: it is a second thing ``uninstall`` has to know
        about, and an unlisted artefact is how a tool stops being removable.

        Pinned as a list rather than "config exists" so an extra artefact cannot
        appear unnoticed — and so a secret reappearing inside ``config.yml``
        instead of beside it would show up here as a missing file.
        """
        _, _, written = wizard()
        assert written == [
            "bin/living-ink",
            "config/config.yml",
            "config/credentials/ai.api_key.gemini",
            "config/credentials/remarkable.cloud_token",
        ]

    def test_nothing_it_writes_is_readable_by_another_account(self, wizard, tmp_path):
        """Every credential lands at 0600, not just the ones written last.

        The mode is set by the writer, so this is really a test that setup
        stores secrets *through* the credentials module rather than by opening
        a file itself.
        """
        wizard()
        stored = sorted((tmp_path / "config" / "credentials").iterdir())
        assert stored, "setup stored no credentials at all"
        for path in stored:
            assert stat.S_IMODE(path.stat().st_mode) == 0o600, path

    def test_everything_it_writes_lives_under_one_removable_root(self, wizard, tmp_path):
        """Nothing is scattered outside the directories setup was pointed at.

        This is the testable half of "uninstall leaves nothing behind" while
        there is no uninstall: whatever it eventually removes, the set of
        things to remove has to be bounded and known, and a wizard that wrote
        to a fifth place would make that impossible before the command is even
        written.
        """
        _, _, written = wizard()
        assert all(path.split("/")[0] in {"config", "bin"} for path in written), written

    def test_it_asks_exactly_the_questions_the_table_lists(self, wizard):
        """Every prompt is a known step, in the listed order, and no others.

        Three failures in one assertion, which is why the script matches on
        prompt text: an added step hits the "question the script does not
        cover" error, a reordered one fails the fragment check inside the
        harness, and a removed one is caught by the count here.
        """
        _, asked, _ = wizard()
        expected = walkthrough_for_this_platform()

        assert len(asked) == len(expected), asked
        for prompt, (fragment, _reply) in zip(asked, expected):
            assert fragment in prompt

    @pytest.mark.skipif(platform.system() != "Darwin", reason="launchd is macOS")
    def test_the_macos_only_steps_are_offered_on_macos(self, wizard):
        """Every platform-specific question is asked here."""
        _, asked, _ = wizard()
        for fragment in MACOS_ONLY_STEPS:
            assert any(fragment in prompt for prompt in asked), fragment

    @pytest.mark.skipif(platform.system() == "Darwin", reason="checks the non-macOS walkthrough")
    def test_the_macos_only_steps_are_skipped_elsewhere(self, wizard):
        """Neither is asked, rather than asked and then quietly ignored.

        Offering to install a launchd agent on Linux would be a question whose
        answer cannot be honoured, and a wizard that asks those trains people
        to distrust the ones that matter.
        """
        _, asked, _ = wizard()
        for fragment in MACOS_ONLY_STEPS:
            assert not any(fragment in prompt for prompt in asked), fragment

    def test_declining_every_extra_still_saves(self, wizard):
        """Saying no to background sync and to the first sync works."""
        result, _, _ = wizard()
        assert result.saved is True
        assert result.run_sync_requested is False

    def test_the_wizard_never_shells_out_or_opens_a_socket(self, wizard, monkeypatch):
        """Choosing no background sync means no ``launchctl``, no network.

        The verification steps are stubbed, so anything left reaching outward
        is unrequested work — which is precisely what must not happen.
        """
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: pytest.fail(f"wizard shelled out: {a}")
        )
        monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("wizard opened a socket"))

        wizard()

    def test_a_declined_destination_is_recorded_as_disabled_not_omitted(self, wizard, tmp_path):
        """Saying no writes ``enabled: false`` rather than leaving the key out.

        The difference matters on the next run: an absent section reads as "not
        configured yet" and a false one reads as "asked and answered", and only
        the second stops the tool nagging.

        Obsidian is the destination 1.0 ships, so declining it is what this has
        to drive: the follow-up questions about the vault are not asked, and
        the section still has to appear.
        """
        import yaml

        #: The vault questions only follow a "yes".
        skipped = {
            "Which vault?",
            "Which folder inside the vault?",
            "Mirror the tablet's folder structure",
        }
        declined = [
            (fragment, False if fragment == "Publish to Obsidian?" else reply)
            for fragment, reply in walkthrough_for_this_platform()
            if fragment not in skipped
        ]

        wizard(declined)
        cfg = yaml.safe_load((tmp_path / "config" / "config.yml").read_text(encoding="utf-8"))
        assert cfg["obsidian"]["enabled"] is False

    def test_what_the_wizard_writes_is_what_info_reads_back(self, wizard, tmp_path, monkeypatch):
        """The two halves of the round trip agree on the section names.

        This is the seam that a behaviour matrix is for. The wizard writes
        the config and ``collect_status`` reads it, and they are different
        functions in different modules that happen to share a layout by
        convention. If one moves a section, every unit test on either side
        still passes.
        """
        from living_ink import api as api_module
        from living_ink import setup_wizard as wizard_module
        from living_ink.cli import status as status_module

        wizard()
        monkeypatch.setattr(status_module, "_describe_connected_device", lambda *a, **kw: "")
        monkeypatch.setattr(api_module, "resolve_stored_token", lambda **kwargs: "")
        monkeypatch.setattr(wizard_module, "verify_remarkable_token", lambda token: (True, "OK"))
        monkeypatch.setattr(wizard_module, "verify_ai_provider", lambda *a, **kw: (True, "OK"))

        report = status_module.collect_status(tmp_path / "config" / "config.yml")

        assert report.config_found is True
        assert report.config_error is None
        assert report.preferred == "cloud"
        assert report.ai_provider == "gemini"
        assert report.obsidian_enabled is True
        assert report.obsidian_vault.endswith("MyVault")


class TestCommandsThatDoNotExistYet:
    """The two commands 1.0 specifies but that have not been built.

    Recorded rather than skipped: an unimplemented command must be a clean
    usage error, not a traceback and not a command that half-works. When one
    ships, this fails and its own rows join the tables above.
    """

    @pytest.mark.parametrize("command", UNBUILT_COMMANDS)
    def test_it_is_rejected_as_a_usage_error(self, cli, command):
        """Exit 2 and a message naming the valid choices."""
        run = cli(command)
        assert run.exit_code == 2
        assert "invalid choice" in run.stderr

    def test_uninstall_cannot_run_at_all(self, cli):
        """Nothing is removed, because nothing can be invoked.

        The strongest statement available about "uninstall leaves nothing
        behind" while there is no uninstall: no subsystem is reached, so there
        is no partial removal to recover from.
        """
        run = cli("uninstall")
        assert run.calls == []
        assert run.exit_code == 2


class TestCommandsThatUsedToExist:
    """``status``, ``state`` and ``cache`` are words the CLI no longer knows.

    A retired command is only retired if invoking it *fails*. The danger is
    not the error — it is the near miss: ``state`` still parsing because
    something forgot to unregister it, or ``cache --clear`` reaching a store
    through a command that was supposed to be gone.
    """

    @pytest.mark.parametrize("command", RETIRED_COMMANDS)
    def test_it_is_rejected_the_same_way_an_unknown_word_is(self, cli, command):
        """Exit 2, naming the choices, with nothing set in motion."""
        run = cli(command)
        assert run.exit_code == 2
        assert "invalid choice" in run.stderr
        assert run.calls == []

    @pytest.mark.parametrize(
        "argv",
        [
            ["state", "--forget", "Notes"],
            ["state", "--repair"],
            ["cache", "--clear"],
            ["cache", "--prune", "7"],
        ],
    )
    def test_the_operations_they_carried_reach_nothing(self, cli, argv):
        """The four destructive ones in particular.

        Each has a 1.0 home — ``sync --force`` for the first and
        ``config → Advanced`` for the rest — and none of them may still be
        reachable by the old spelling in the meantime.
        """
        run = cli(*argv)
        assert run.exit_code == 2
        assert run.calls == []
