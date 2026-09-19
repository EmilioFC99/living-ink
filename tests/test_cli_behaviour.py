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
whole :class:`~living_ink.pipeline.SyncOptions` against an expected value. That
is what makes "and nothing else" testable at all: asserting ``dry_run is True``
proves the flag arrived, while asserting the whole object proves no *other*
field moved with it. A new field defaults into every expectation for free; a
new field that some flag secretly sets fails every case at once, which is the
correct amount of noise.

**The exhaustive pass runs at the parse layer.** :func:`intent` is
``parse_args`` followed by ``SyncOptions.from_args`` — the two steps that turn
an argv into an instruction, and nothing after them. All 512 boolean
combinations go through it, which would be slow through ``main()`` and would
prove no more. What licenses the shortcut is
:class:`TestTheShortcutMatchesTheRealThing`: it runs the real entry point and
checks the pipeline was handed the same object :func:`intent` predicts. If that
one test passes, the 512 are statements about the real CLI.

Nothing here reaches the network, a tablet, a vault, or a real config. The
``cli`` fixture replaces the four seams that face outward and *fails the test*
if anything opens a socket or a subprocess anyway.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import platform
import socket
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

from living_ink.cli import LivingInkCLI
from living_ink.pipeline import SyncOptions

# ---------------------------------------------------------------------------
# Table 1 — what each sync flag means
# ---------------------------------------------------------------------------

#: Every boolean flag on ``sync``, and the single ``SyncOptions`` field it is
#: allowed to change. One flag, one field: the exhaustive pass below turns that
#: into a property — the options for any set of flags must be exactly the union
#: of their individual effects, with no interaction between them.
SYNC_BOOLEAN_FLAGS: dict[str, tuple[str, Any]] = {
    "--ssh": ("ssh", True),
    "--cloud": ("cloud", True),
    "--sync-pdfs": ("sync_pdfs", True),
    "--sync-epubs": ("sync_epubs", True),
    "--all-types": ("all_types", True),
    "--keep-temp": ("keep_temp", True),
    "--dry-run": ("dry_run", True),
    "--prune": ("prune", True),
    "--json": ("json_output", True),
}

#: What ``living-ink sync`` with no flags at all resolves to.
#:
#: ``limit`` is 0 rather than None because the parser declares ``default=0``,
#: and the pipeline reads 0 as "no override, use the configured maximum". The
#: distinction matters: a real 0 here would mean "process no notebooks".
BARE_SYNC = SyncOptions(limit=0)

#: Boolean flags that ``sync`` accepts but that never reach ``SyncOptions``.
#:
#: ``--status`` selects a different code path entirely and ``--all`` only
#: qualifies it, so both are tested by
#: :class:`TestStatusIsADifferentCommandInDisguise` instead. ``--verbose`` and
#: ``--quiet`` are not sync flags at all — ``add_verbosity_args`` puts them on
#: every parser — and they are covered by
#: :class:`TestVerbosityIsAcceptedOnBothSides`.
SYNC_FLAGS_OUTSIDE_THE_OPTIONS = ("--status", "--all", "--verbose", "-q", "--quiet")


def intent(argv: list[str]) -> SyncOptions:
    """Turn a command line into the instruction it encodes.

    This is the whole of the CLI's decision-making for ``sync``: argparse
    produces a namespace and ``SyncOptions.from_args`` — which the pipeline
    documents as "the single place that knows CLI flag names" — turns it into
    the object the pipeline obeys. Everything downstream reads that object, so
    two argvs that produce equal options are the same instruction.

    Args:
        argv: Arguments as the user would type them, without the program name.

    Returns:
        The options a real run would be driven by.
    """
    args = LivingInkCLI().build_parser().parse_args(argv)
    return SyncOptions.from_args(args)


def expected_for(flags: tuple[str, ...]) -> SyncOptions:
    """Predict the options for a set of boolean flags.

    Args:
        flags: Flag strings, each a key of :data:`SYNC_BOOLEAN_FLAGS`.

    Returns:
        :data:`BARE_SYNC` with one field changed per flag, and nothing else.
    """
    return replace(BARE_SYNC, **{SYNC_BOOLEAN_FLAGS[f][0]: SYNC_BOOLEAN_FLAGS[f][1] for f in flags})


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
#: ``--verbose`` / ``-q`` are on every parser because ``add_verbosity_args`` is
#: applied to the top level and to each subparser, so that both
#: ``living-ink --verbose sync`` and ``living-ink sync --verbose`` work.
COMMAND_SURFACE: dict[str, set[str]] = {
    "": {"-h", "--help", "-v", "--version", "-c", "--config", "--verbose", "-q", "--quiet"},
    "sync": {
        "-h",
        "--help",
        "--verbose",
        "-q",
        "--quiet",
        "--notebook",
        "--limit",
        "--folder",
        "--ssh",
        "--cloud",
        "--sync-pdfs",
        "--sync-epubs",
        "--all-types",
        "--keep-temp",
        "--dry-run",
        "--prune",
        "--status",
        "--all",
        "--json",
    },
    "watch": {
        "-h",
        "--help",
        "--verbose",
        "-q",
        "--quiet",
        "--notebook",
        "--limit",
        "--folder",
        "--ssh",
        "--cloud",
        "--sync-pdfs",
        "--sync-epubs",
        "--all-types",
        "--keep-temp",
        "--dry-run",
        "--prune",
        "--status",
        "--all",
        "--json",
        "--interval",
    },
    "setup": {"-h", "--help", "--verbose", "-q", "--quiet"},
    "status": {"-h", "--help", "--verbose", "-q", "--quiet", "--json"},
    "state": {
        "-h",
        "--help",
        "--verbose",
        "-q",
        "--quiet",
        "--dump",
        "--forget",
        "--repair",
        "--destination",
        "--json",
    },
    "cache": {"-h", "--help", "--verbose", "-q", "--quiet", "--clear", "--prune", "--json"},
}

#: Commands §7.1 of the 1.0 design specifies but that have not been built. They
#: must still be rejected as usage errors rather than half-working, and this
#: list is what makes their absence a stated fact instead of an oversight.
UNBUILT_COMMANDS = ("info", "config", "uninstall")


# ---------------------------------------------------------------------------
# Table 5 — the setup conversation
# ---------------------------------------------------------------------------

#: A full Cloud walkthrough, as (fragment of the prompt, reply), in order.
#:
#: Keyed on what the wizard *asks* rather than on position, because the
#: alternative — a bare list of twelve answers — cannot tell "the wizard grew a
#: step" apart from "the wizard reordered two" apart from "the answers slipped
#: by one", and all three produce a config that looks plausible. Matching the
#: prompt makes each reply answer a named question.
CLOUD_WALKTHROUGH: tuple[tuple[str, str], ...] = (
    ("Select preferred connection", "2"),
    ("Use existing reMarkable pairing", "y"),
    ("Configure USB SSH as an automatic backup", "n"),
    ("Select provider", "1"),
    ("Gemini API key", "AIzaTestKey"),
    ("Enable Obsidian sync", "y"),
    ("Select vault", "1"),
    ("Choose folder", "1"),
    ("Mirror complete nested", "y"),
    ("Enable Apple Notes sync", "n"),
    ("Automatically sync notes in background", "n"),
    ("run your first sync now", "n"),
)

#: Steps the wizard only offers on macOS, because there is nothing behind them
#: anywhere else: Apple Notes is driven through ``osascript`` and background
#: sync installs a launchd plist. Linux therefore gets a walkthrough that is
#: genuinely two questions shorter — which CI discovered, because the first
#: version of this table assumed everyone was on a Mac.
MACOS_ONLY_STEPS = frozenset({"Enable Apple Notes sync", "Automatically sync notes in background"})


def walkthrough_for_this_platform() -> list[tuple[str, str]]:
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
#: ``--dry-run`` is a combination people rely on, and making it an error later
#: would break scripts.
#:
#: ``--ssh --cloud`` is legal today, which is not obviously what anyone expects.
#: It resolves by precedence rather than by exclusion — see
#: :class:`TestForcingBothTransports` for what it actually does.
FLAG_COMPATIBILITY: tuple[Combination, ...] = (
    # sync — nothing on this command is mutually exclusive.
    Combination(("sync", "--notebook", "Work/Notes"), True, "scoping a sync to one notebook"),
    Combination(("sync", "--notebook", "Foo", "--dry-run"), True, "preview one notebook"),
    Combination(("sync", "--notebook", "Foo", "--limit", "3"), True, "both narrow the run"),
    Combination(("sync", "--ssh", "--cloud"), True, "precedence, not exclusion"),
    Combination(("sync", "--dry-run", "--prune"), True, "a preview of what pruning would remove"),
    Combination(("sync", "--all-types", "--sync-pdfs"), True, "--all-types simply subsumes it"),
    Combination(("sync", "--status", "--all"), True, "--all qualifies --status"),
    Combination(("sync", "--status", "--json"), True, "the comparison has a JSON form"),
    Combination(("sync", "--all"), True, "accepted, but inert without --status"),
    Combination(("sync", "--dry-run", "--json"), True, "a machine-readable preview"),
    Combination(("sync", "--limit", "0"), True, "0 means 'use the configured maximum'"),
    Combination(("sync", "--limit", "-1"), True, "negative is accepted; the pipeline ignores it"),
    # sync — malformed input.
    Combination(("sync", "--limit", "many"), False, "--limit is typed int"),
    Combination(("sync", "--notebook"), False, "--notebook needs a value"),
    Combination(("sync", "--nonsense"), False, "unknown flags are a usage error"),
    Combination(("sync", "--dry"), True, "argparse accepts unambiguous abbreviations"),
    Combination(("sync", "--s"), False, "--ssh, --sync-pdfs, --sync-epubs, --status all match"),
    # watch — every sync flag, plus its own.
    Combination(("watch", "--interval", "600"), True, "the documented usage"),
    Combination(("watch", "--notebook", "Foo", "--dry-run"), True, "watch takes every sync option"),
    Combination(("watch", "--interval", "fast"), False, "--interval is typed int"),
    # state — the three actions genuinely exclude one another.
    Combination(("state",), True, "a bare state prints a summary"),
    Combination(("state", "--dump"), True, "one action"),
    Combination(("state", "--forget", "Foo", "--destination", "obsidian"), True, "scoped forget"),
    Combination(("state", "--dump", "--json"), True, "--json is not an action"),
    Combination(("state", "--dump", "--repair"), False, "two actions at once"),
    Combination(("state", "--dump", "--forget", "Foo"), False, "two actions at once"),
    Combination(("state", "--forget", "Foo", "--repair"), False, "two actions at once"),
    # cache — same shape, two actions.
    Combination(("cache",), True, "a bare cache prints a summary"),
    Combination(("cache", "--prune"), True, "--prune's day count is optional"),
    Combination(("cache", "--prune", "7"), True, "an explicit age"),
    Combination(("cache", "--clear", "--json"), True, "--json is not an action"),
    Combination(("cache", "--clear", "--prune"), False, "two actions at once"),
    Combination(("cache", "--clear", "--prune", "7"), False, "two actions at once"),
    # status and setup take almost nothing, and that is the point.
    Combination(("status", "--json"), True, "the only flag status has"),
    Combination(("status", "--dry-run"), False, "a sync flag on a read-only command"),
    Combination(("setup",), True, "the wizard takes no behaviour flags"),
    Combination(("setup", "--json"), False, "the wizard has no machine-readable mode"),
    # Verbosity is accepted on both sides of the subcommand, everywhere.
    Combination(("--verbose", "sync"), True, "before the subcommand"),
    Combination(("sync", "--verbose"), True, "after the subcommand"),
    Combination(("-q", "status"), True, "the short form, before"),
    Combination(("status", "--quiet"), True, "the long form, after"),
    Combination(("--verbose", "sync", "--quiet"), True, "contradictory, but not a usage error"),
)


# ---------------------------------------------------------------------------
# Table 4 — how far each command is allowed to reach
# ---------------------------------------------------------------------------

#: The subsystems a command may set in motion, keyed by command line.
#:
#: This is the "nothing else" half of the matrix, and it is the half that
#: cannot be written as a normal assertion: the interesting fact is which of
#: these did *not* happen. ``status`` must not sync. ``cache`` must not open
#: the state database. ``sync --status`` must not download anything. Each of
#: those is one missing label here, and the test fails on any label that fires
#: and is not listed.
#:
#: Labels are the recorded names in :class:`_Recorder`.
COMMAND_REACH: dict[tuple[str, ...], set[str]] = {
    ("sync",): {"pipeline.construct", "pipeline.run"},
    ("sync", "--dry-run"): {"pipeline.construct", "pipeline.run"},
    ("sync", "--notebook", "Foo"): {"pipeline.construct", "pipeline.run"},
    ("sync", "--status"): {"compare_with_device"},
    ("sync", "--status", "--json"): {"compare_with_device"},
    ("watch", "--interval", "60"): {"pipeline.construct", "pipeline.run"},
    ("status",): {"collect_status"},
    ("status", "--json"): {"collect_status"},
    ("setup",): {"run_wizard"},
    ("state",): {"state_db_path", "get_state_store"},
    ("state", "--json"): {"state_db_path", "get_state_store"},
    ("cache",): {"all_caches"},
    ("cache", "--json"): {"all_caches"},
}


class _Recorder:
    """Notes which subsystems a command reached, and stands in for four of them.

    Args:
        state_db: A file to report as the state database, so ``state`` gets
            past its existence check without a real sync having run.
    """

    def __init__(self, state_db: Path) -> None:
        """Start with an empty log."""
        self.calls: list[str] = []
        self.options: list[SyncOptions] = []
        self.state_db = state_db

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
        options: The options handed to each pipeline that was constructed.
    """

    argv: list[str]
    exit_code: int
    stdout: str
    stderr: str
    calls: list[str] = field(default_factory=list)
    options: list[SyncOptions] = field(default_factory=list)


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
    from living_ink import setup_wizard as wizard_module

    state_db = tmp_path / "state.db"
    state_db.write_bytes(b"")
    recorder = _Recorder(state_db)

    class _RecordingPipeline:
        """Stands in for the pipeline, remembering what it was told to do."""

        def __init__(self, options=None, config_path=None, data_dir=None, destinations=None):
            """Record the instruction without acting on it."""
            recorder.note("pipeline.construct")
            recorder.options.append(options)

        def run(self) -> bool:
            """Report success without doing any work.

            Returns:
                True, always.
            """
            recorder.note("pipeline.run")
            return True

    class _WizardResult:
        """The one field :class:`SetupCommand` reads off a wizard run."""

        run_sync_requested = False

    def _run_wizard(repo_dir=None):
        """Stand in for the interactive wizard.

        Args:
            repo_dir: Ignored.

        Returns:
            A result declining the offered first sync.
        """
        recorder.note("run_wizard")
        return _WizardResult()

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
    monkeypatch.setattr(wizard_module, "run_wizard", _run_wizard)
    monkeypatch.setattr(cli_module, "collect_status", _collect_status)
    monkeypatch.setattr(cli_module, "compare_with_device", _compare_with_device)

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

    _spy(cli_module, "all_caches", "all_caches")
    _spy(pipeline_module, "get_state_store", "get_state_store")

    def _state_db_path() -> Path:
        """Point ``state`` at a database that exists but holds nothing.

        Returns:
            The throwaway database path.
        """
        recorder.note("state_db_path")
        return recorder.state_db

    monkeypatch.setattr(cli_module, "state_db_path", _state_db_path)

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
    monkeypatch.setattr(cli_module.time, "sleep", _no_waiting)

    def _run(*argv: str) -> Invocation:
        """Run the CLI once and describe what happened.

        Args:
            *argv: Arguments as the user would type them.

        Returns:
            An :class:`Invocation` describing the run.
        """
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

    Every assertion compares the whole options object, so these also prove the
    negative: a flag that quietly set a second field would fail here.
    """

    @pytest.mark.parametrize(
        "argv,expected",
        [
            pytest.param(["sync"], BARE_SYNC, id="bare"),
            pytest.param(
                ["sync", "--notebook", "Work/Notes"],
                replace(BARE_SYNC, notebook="Work/Notes"),
                id="notebook-path",
            ),
            pytest.param(
                ["sync", "--notebook", "abc-123"],
                replace(BARE_SYNC, notebook="abc-123"),
                id="notebook-id",
            ),
            pytest.param(["sync", "--limit", "5"], replace(BARE_SYNC, limit=5), id="limit"),
            pytest.param(["sync", "--limit", "0"], replace(BARE_SYNC, limit=0), id="limit-zero"),
            pytest.param(
                ["sync", "--folder", "Inbox"], replace(BARE_SYNC, folder="Inbox"), id="folder"
            ),
            pytest.param(["sync", "--dry-run"], replace(BARE_SYNC, dry_run=True), id="dry-run"),
            pytest.param(["sync", "--prune"], replace(BARE_SYNC, prune=True), id="prune"),
            pytest.param(["sync", "--ssh"], replace(BARE_SYNC, ssh=True), id="ssh"),
            pytest.param(["sync", "--cloud"], replace(BARE_SYNC, cloud=True), id="cloud"),
            pytest.param(["sync", "--json"], replace(BARE_SYNC, json_output=True), id="json"),
            pytest.param(
                ["sync", "--keep-temp"], replace(BARE_SYNC, keep_temp=True), id="keep-temp"
            ),
            pytest.param(
                ["sync", "--all-types"], replace(BARE_SYNC, all_types=True), id="all-types"
            ),
            pytest.param(
                ["sync", "--sync-pdfs"], replace(BARE_SYNC, sync_pdfs=True), id="sync-pdfs"
            ),
            pytest.param(
                ["sync", "--sync-epubs"], replace(BARE_SYNC, sync_epubs=True), id="sync-epubs"
            ),
            pytest.param(
                ["sync", "--notebook", "Foo", "--dry-run", "--keep-temp"],
                replace(BARE_SYNC, notebook="Foo", dry_run=True, keep_temp=True),
                id="scoped-preview",
            ),
            pytest.param(
                ["sync", "--all-types", "--limit", "2", "--json"],
                replace(BARE_SYNC, all_types=True, limit=2, json_output=True),
                id="everything-two-of-them-as-json",
            ),
        ],
    )
    def test_the_command_line_means_exactly_this(self, argv, expected):
        """The parsed instruction equals the expectation in every field."""
        assert intent(argv) == expected

    def test_an_unset_document_type_defers_to_config(self):
        """``--sync-pdfs`` absent means None, not False.

        The difference is the whole reason ``from_args`` maps with ``or None``:
        False would override a config that has PDFs switched on, turning an
        omitted flag into an instruction the user never gave.
        """
        options = intent(["sync"])
        assert options.sync_pdfs is None
        assert options.sync_epubs is None

    def test_no_flag_ever_sets_the_connection_preference(self):
        """``preferred_connection`` is a config field, not a CLI one.

        ``--ssh`` and ``--cloud`` set their own booleans and the pipeline reads
        those; a flag writing this field instead would change precedence.
        """
        for flag in SYNC_BOOLEAN_FLAGS:
            assert intent(["sync", flag]).preferred_connection is None, flag


class TestEveryFlagCombination:
    """All 512 subsets of the boolean flags, checked against the same rule.

    The rule is independence: the options for a set of flags are exactly the
    union of what each flag does alone. That is a stronger claim than any list
    of hand-picked cases, and it is the claim a user makes when they combine
    two flags and expect both to apply.
    """

    @pytest.mark.parametrize(
        "flags",
        [
            pytest.param(combo, id="+".join(f.lstrip("-") for f in combo) or "none")
            for size in range(len(SYNC_BOOLEAN_FLAGS) + 1)
            for combo in itertools.combinations(sorted(SYNC_BOOLEAN_FLAGS), size)
        ],
    )
    def test_flags_do_not_interact(self, flags):
        """Combining flags changes exactly the fields those flags own."""
        assert intent(["sync", *flags]) == expected_for(flags)


class TestFlagOrderIsIrrelevant:
    """The same flags in a different order are the same instruction."""

    @pytest.mark.parametrize(
        "first,second",
        [
            (
                ["sync", "--dry-run", "--notebook", "Foo"],
                ["sync", "--notebook", "Foo", "--dry-run"],
            ),
            (["sync", "--ssh", "--json"], ["sync", "--json", "--ssh"]),
            (
                ["sync", "--limit", "3", "--all-types", "--keep-temp"],
                ["sync", "--keep-temp", "--limit", "3", "--all-types"],
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
        assert intent(["sync", "--notebook", value]).notebook == value

    def test_a_negative_limit_is_carried_not_rejected(self):
        """The parser accepts it and the pipeline decides what it means.

        Pinned because it is a real edge: ``SyncPipeline`` treats anything that
        is not greater than zero as "no override", so a negative limit is inert
        rather than an error, and moving that decision into the parser would
        change the exit code a script sees.
        """
        assert intent(["sync", "--limit", "-1"]).limit == -1


class TestForcingBothTransports:
    """``--ssh --cloud`` together: legal, and resolved by precedence.

    This is the combination the matrix exists to pin. It is *not* rejected —
    neither by argparse nor by the pipeline — and what actually happens is that
    ``--ssh`` wins, because the pipeline tests the flags in order rather than
    treating them as exclusive. Whether it should reject instead is a decision
    for the generated-flags work; until then, this is the behaviour, and
    changing it means changing this test on purpose.
    """

    def test_the_parser_accepts_both(self):
        """No usage error, no exclusive group."""
        options = intent(["sync", "--ssh", "--cloud"])
        assert options.ssh is True
        assert options.cloud is True

    def test_ssh_wins_regardless_of_the_order_they_were_typed(self, tmp_path, monkeypatch):
        """The resolved transport is SSH either way.

        Reading the resolved settings rather than the flags is the point: the
        flags are only an input, and what a user experiences is which transport
        the run actually prefers.
        """
        from living_ink.pipeline import SyncPipeline

        for argv in (["sync", "--ssh", "--cloud"], ["sync", "--cloud", "--ssh"]):
            pipe = SyncPipeline(
                options=intent(argv),
                data_dir=tmp_path,
                destinations=[],
            )
            assert pipe.settings.preferred_connection == "ssh", argv
            assert pipe.settings.use_ssh is True, argv

    def test_cloud_alone_still_selects_cloud(self, tmp_path):
        """The precedence rule does not make ``--cloud`` unusable."""
        from living_ink.pipeline import SyncPipeline

        pipe = SyncPipeline(options=intent(["sync", "--cloud"]), data_dir=tmp_path, destinations=[])
        assert pipe.settings.preferred_connection == "cloud"
        assert pipe.settings.use_ssh is False


class TestStatusIsADifferentCommandInDisguise:
    """``sync --status`` answers a question instead of doing the work."""

    def test_the_status_flag_is_not_part_of_the_instruction(self):
        """It never reaches ``SyncOptions``, because no sync is run."""
        assert not hasattr(intent(["sync", "--status"]), "status")

    def test_all_without_status_is_accepted_and_inert(self, cli):
        """``sync --all`` parses, and runs an ordinary sync.

        ``--all`` is only read by the comparison renderer, so on its own it
        changes nothing. Pinned because "accepted but does nothing" is exactly
        the kind of behaviour that gets accidentally fixed into an error.
        """
        run = cli("sync", "--all")
        assert run.options == [BARE_SYNC]
        assert "compare_with_device" not in run.calls


# ---------------------------------------------------------------------------
# The shortcut is honest
# ---------------------------------------------------------------------------


class TestTheShortcutMatchesTheRealThing:
    """What :func:`intent` predicts is what the real entry point delivers.

    Everything above runs at the parse layer for speed. This is the test that
    makes those results statements about ``living-ink`` rather than about a
    helper: it drives ``cli.main`` and compares the options the pipeline was
    actually constructed with.
    """

    @pytest.mark.parametrize(
        "argv",
        [
            ["sync"],
            ["sync", "--dry-run"],
            ["sync", "--notebook", "Work/Notes", "--limit", "2"],
            ["sync", "--all-types", "--keep-temp", "--json"],
            ["sync", "--ssh", "--prune", "--folder", "Inbox"],
            ["sync", "--sync-pdfs", "--sync-epubs"],
        ],
    )
    def test_the_pipeline_receives_the_predicted_options(self, cli, argv):
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
        run = cli("state", "--dump", "--repair")
        assert run.exit_code == 2
        assert "not allowed with" in run.stderr

    def test_every_pair_of_sync_booleans_is_accepted(self):
        """No two sync flags exclude one another, and none ever have.

        Stated as a sweep rather than a list so that adding a flag extends the
        claim automatically: the day one of them needs an exclusive group,
        this fails and the decision gets written down.
        """
        parser = LivingInkCLI().build_parser()
        for left, right in itertools.combinations(sorted(SYNC_BOOLEAN_FLAGS), 2):
            parser.parse_args(["sync", left, right])


class TestAbbreviationsAreAccepted:
    """Any unambiguous prefix of a flag works, because argparse allows it.

    This is not a decision anyone made — ``allow_abbrev`` defaults to True — and
    it is pinned here because it is a compatibility surface with teeth. Every
    accepted abbreviation is a spelling some script now depends on, and adding
    a flag that shares a prefix silently turns that script's argument into an
    "ambiguous option" error. ``--dry`` breaks the day ``--dry-clean`` ships.

    Turning ``allow_abbrev`` off is a defensible fix; doing it by accident is
    not, which is what this test prevents.
    """

    @pytest.mark.parametrize(
        "abbreviated,full",
        [
            (["sync", "--dry"], ["sync", "--dry-run"]),
            (["sync", "--keep"], ["sync", "--keep-temp"]),
            (["sync", "--pru"], ["sync", "--prune"]),
            (["sync", "--note", "Foo"], ["sync", "--notebook", "Foo"]),
            (["sync", "--li", "4"], ["sync", "--limit", "4"]),
        ],
    )
    def test_a_prefix_means_the_flag_it_abbreviates(self, abbreviated, full):
        """The shortened form resolves to the identical instruction."""
        assert intent(abbreviated) == intent(full)

    @pytest.mark.parametrize("prefix", ["--s", "--sync", "--al"])
    def test_an_ambiguous_prefix_is_a_usage_error(self, prefix):
        """Several flags match, so argparse refuses to guess.

        ``--sync`` is the interesting one: it is a prefix of both
        ``--sync-pdfs`` and ``--sync-epubs``, so the most natural abbreviation
        of the pair is the one that does not work.
        """
        parser = LivingInkCLI().build_parser()
        with pytest.raises(SystemExit) as exit_info:
            parser.parse_args(["sync", prefix])
        assert exit_info.value.code == 2

    def test_an_exact_match_beats_a_longer_flag_it_prefixes(self):
        """``--all`` is ``--all``, not an abbreviation of ``--all-types``.

        Without the exact-match rule this would be ambiguous, and the two flags
        mean entirely different things.
        """
        assert intent(["sync", "--all"]) == BARE_SYNC
        assert intent(["sync", "--all-types"]).all_types is True


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

    The interesting half of every row is what is *missing* from it. ``status``
    has no pipeline, ``cache`` has no state store, ``sync --status`` has no
    pipeline either. A command that grows an extra step fails here even if the
    step works perfectly, which is the point.
    """

    @pytest.mark.parametrize(
        "argv,allowed",
        [pytest.param(a, s, id=" ".join(a).replace("--", "")) for a, s in COMMAND_REACH.items()],
    )
    def test_nothing_outside_the_allowance_is_touched(self, cli, argv, allowed):
        """No subsystem fires that the table does not list."""
        run = cli(*argv)
        assert set(run.calls) == allowed, f"{argv} reached {sorted(set(run.calls))}"

    def test_a_dry_run_still_builds_the_pipeline_and_nothing_more(self, cli):
        """``--dry-run`` is a pipeline mode, not a separate code path.

        If it were handled by short-circuiting in the CLI, the transcripts a
        dry run exists to produce would never be written.
        """
        run = cli("sync", "--dry-run")
        assert run.calls == ["pipeline.construct", "pipeline.run"]
        assert run.options[0].dry_run is True

    def test_reading_the_cache_never_opens_the_state_database(self, cli):
        """Two independent stores, and the commands stay independent."""
        assert "get_state_store" not in cli("cache").calls
        assert "state_db_path" not in cli("cache", "--json").calls

    def test_inspecting_state_never_touches_the_caches(self, cli):
        """The reverse direction, which is just as easy to break."""
        assert "all_caches" not in cli("state").calls

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
            (["status"], "collect_status"),
            (["cache"], "all_caches"),
            (["state"], "state_db_path"),
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

        cli("-c", str(config), "status")
        assert os.environ["LIVING_INK_CONFIG"] == str(config.resolve())


# ---------------------------------------------------------------------------
# Output reflects input
# ---------------------------------------------------------------------------


class TestStatusReportsWhatItWasGiven:
    """``status --json`` echoes the configuration it read, not a default.

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
        from living_ink import cli as cli_module
        from living_ink import setup_wizard as wizard_module

        vault = tmp_path / "MyVault"
        (vault / ".obsidian").mkdir(parents=True)

        values = {
            "vault": str(vault),
            "root_folder": "reMarkable Imports",
            "provider": "groq",
            "model": "a-very-specific-model",
            "ssh_host": "10.11.99.7",
            "apple_notes_folder": "Scribbles",
        }
        # The section names are the ones the wizard writes and the schema
        # declares: destinations are top-level keys, not nested under a
        # ``destinations:`` map, and Apple Notes' key is ``folder_name``.
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
            "apple_notes:\n"
            "  enabled: true\n"
            f"  folder_name: {values['apple_notes_folder']}\n",
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
        monkeypatch.setattr(cli_module, "_describe_connected_device", lambda *a, **kw: "")
        monkeypatch.setattr(api_module, "resolve_stored_token", lambda: "")

        return config, values

    def _report(self, configured, capsys) -> dict:
        """Run ``status --json`` against the written config.

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
            main(["-c", str(config), "status", "--json"])
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
        assert not missing, f"status did not report: {missing}"

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
        """No boolean sync flag escapes :data:`SYNC_BOOLEAN_FLAGS`.

        The exhaustive combination pass only covers flags this table names, so
        a store_true flag missing from it would be excluded from the sweep
        without anything saying so.
        """
        parser = _subparsers(LivingInkCLI().build_parser())["sync"]
        booleans = {
            opt
            for action in parser._actions
            if isinstance(action, argparse._StoreTrueAction)
            for opt in action.option_strings
        }
        undeclared = booleans - set(SYNC_BOOLEAN_FLAGS) - set(SYNC_FLAGS_OUTSIDE_THE_OPTIONS)
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

    ``run_wizard`` takes ``input_func`` and ``print_func``, so the whole
    conversation is drivable from a list — no TTY, no patching of builtins.
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

        def _run(script: list[tuple[str, str]] | None = None):
            """Run the wizard against an expected conversation.

            Args:
                script: Pairs of (expected fragment of the prompt, reply).
                    Defaults to the walkthrough for this platform.

            Returns:
                A tuple of the wizard result, the prompts it asked, and the
                paths it left behind under ``tmp_path``.
            """
            steps = iter(script if script is not None else walkthrough_for_this_platform())
            asked: list[str] = []

            def _input(prompt: str = "") -> str:
                """Answer one prompt, checking it is the one expected next.

                Args:
                    prompt: What the wizard asked.

                Returns:
                    The scripted reply.

                Raises:
                    AssertionError: If the wizard asked something unscripted.
                """
                asked.append(prompt)
                try:
                    fragment, reply = next(steps)
                except StopIteration:
                    raise AssertionError(
                        f"The wizard asked a question the script does not cover: {prompt!r}. "
                        "Add it to CLOUD_WALKTHROUGH."
                    ) from None
                assert fragment in prompt, f"expected a question about {fragment!r}, got {prompt!r}"
                return reply

            result = wizard_module.run_wizard(
                input_func=_input,
                print_func=lambda *a: None,
                repo_dir=tmp_path,
                bin_dir=tmp_path / "bin",
            )
            written = sorted(
                p.relative_to(tmp_path).as_posix()
                for p in tmp_path.rglob("*")
                if p.is_file() and "MyVault" not in p.parts
            )
            return result, asked, written

        return _run

    def test_it_leaves_exactly_two_files_behind(self, wizard):
        """The config, and an executable wrapper the user was never asked about.

        The wrapper is the interesting one. ``install_cli_command`` runs as part
        of the walkthrough rather than behind a prompt, so a full setup puts a
        file on the user's PATH without that being one of the four steps it
        announces. That is defensible — a CLI you cannot invoke is not set up —
        but it is not free: it is a second thing ``uninstall`` has to know
        about, and an unlisted artefact is how a tool stops being removable.

        Pinned as a list rather than "config exists" so a third artefact cannot
        appear unnoticed.
        """
        _, _, written = wizard()
        assert written == ["bin/living-ink", "config/config.yml"]

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

    @pytest.mark.skipif(platform.system() != "Darwin", reason="launchd and Apple Notes are macOS")
    def test_the_macos_only_steps_are_offered_on_macos(self, wizard):
        """Both platform-specific questions are asked here."""
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
        """Saying no to Apple Notes, background sync and the first sync works."""
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
        """
        import yaml

        wizard()
        cfg = yaml.safe_load((tmp_path / "config" / "config.yml").read_text(encoding="utf-8"))
        assert cfg["apple_notes"]["enabled"] is False

    def test_what_the_wizard_writes_is_what_status_reads_back(self, wizard, tmp_path, monkeypatch):
        """The two halves of the round trip agree on the section names.

        This is the seam that a behaviour matrix is for. ``run_wizard`` writes
        the config and ``collect_status`` reads it, and they are different
        functions in different modules that happen to share a layout by
        convention. If one moves a section, every unit test on either side
        still passes.
        """
        from living_ink import api as api_module
        from living_ink import cli as cli_module
        from living_ink import setup_wizard as wizard_module

        wizard()
        monkeypatch.setattr(cli_module, "_describe_connected_device", lambda *a, **kw: "")
        monkeypatch.setattr(api_module, "resolve_stored_token", lambda: "")
        monkeypatch.setattr(wizard_module, "verify_remarkable_token", lambda token: (True, "OK"))
        monkeypatch.setattr(wizard_module, "verify_ai_provider", lambda *a, **kw: (True, "OK"))

        report = cli_module.collect_status(tmp_path / "config" / "config.yml")

        assert report.config_found is True
        assert report.config_error is None
        assert report.preferred == "cloud"
        assert report.ai_provider == "gemini"
        assert report.obsidian_enabled is True
        assert report.obsidian_vault.endswith("MyVault")


class TestCommandsThatDoNotExistYet:
    """The three commands 1.0 specifies but that have not been built.

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
