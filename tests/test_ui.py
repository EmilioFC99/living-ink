"""The interactive layer, driven by scripted keystrokes through a real pipe.

Every other way of testing a prompt tests something else. Patching
``questionary.select`` proves only that the module calls the function it calls;
substituting an ``input_func`` proves that a fake answers the way the fake was
written. Neither would have caught a ``default=`` that names no choice, a
checkbox that turns "nothing ticked" into ``None``, or a password echoing into
the scrollback — and those are the failures a user actually meets.

So this file drives the **real** widgets. ``prompt_toolkit`` can be handed a
pipe to read keys from and an output to render into, :func:`living_ink.ui.
driven_by` is the seam that puts those two objects into every widget call, and
the :class:`Keyboard` helper below turns a string of raw keystrokes into an
answer. What the tests exercise is the same key handling, the same validators
and the same rendering the user gets.

Two rules keep that honest, and both are in :meth:`Keyboard.plugged_in`:

* **Every keystroke is sent before the widget runs.** A widget reads its input
  from an event loop it owns, so a test that tried to type *while* the prompt
  was up would be typing into a loop that is not running yet.
* **The write end of the pipe is closed immediately afterwards.** A widget
  waiting for a key the script never sent would otherwise block the whole suite
  for as long as CI allows; against a closed pipe it raises ``EOFError`` in
  under a second. A hanging prompt is always a bug in the test, and this is what
  makes it say so.

Nothing here reaches the network, a tablet, a vault or the user's real
terminal: the only filesystem paths are ``tmp_path``, and the only streams are
a pipe and a string buffer.
"""

import io
import sys
from contextlib import contextmanager
from typing import Any, Callable, Iterator

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.plain_text import PlainTextOutput

from living_ink import ui

#: Raw keystrokes, spelled once. These are the bytes a terminal actually sends,
#: which is why the down arrow is an escape sequence and not a name: the widget
#: parses vt100 input, so the test has to speak vt100.
ENTER = "\r"
DOWN = "\x1b[B"
SPACE = " "
BACKSPACE = "\x7f"
CTRL_C = "\x03"

#: Three options whose labels share no text with their values. Every assertion
#: about "the caller gets the value" would pass by accident if they matched.
CHOICES = (
    ui.Choice("alpha", "First thing"),
    ui.Choice("beta", "Second thing"),
    ui.Choice("gamma", "Third thing"),
)


def erase(text: str) -> str:
    """Return the keystrokes that delete ``text`` a character at a time.

    A test that retypes an answer has to clear the rejected one the way a user
    would, because the buffer still holds it: sending the replacement alone
    would append to it and assert against a string nobody typed.

    Args:
        text: The text currently in the prompt's buffer.

    Returns:
        One backspace per character.
    """
    return BACKSPACE * len(text)


class Keyboard:
    """A scripted user, typing at the real widgets.

    Holds no state between questions on purpose: each answer gets its own pipe,
    so a stray keystroke left over from one widget cannot be read by the next
    and quietly make a later assertion pass.
    """

    def __init__(self) -> None:
        """Start with nothing rendered."""
        self._screen = io.StringIO()

    @property
    def rendered(self) -> str:
        """Return everything the last captured widget drew.

        Empty unless the answer was taken with ``capture=True``; capturing is
        opt-in because only the password test cares what reached the screen,
        and a discarded render is faster and quieter.

        Returns:
            The text of the rendered prompt.
        """
        return self._screen.getvalue()

    @contextmanager
    def plugged_in(self, keys: str, *, capture: bool = False) -> Iterator[None]:
        """Point the widgets at a pipe preloaded with ``keys``.

        Args:
            keys: Every keystroke the widget will be given, in order.
            capture: Render into a readable buffer instead of discarding.

        Yields:
            None, for as long as the widgets are driven by this pipe.
        """
        with create_pipe_input() as pipe:
            pipe.send_text(keys)
            # Closing the write end now is the hang guard: a widget that wants
            # a key nobody sent gets EOF rather than waiting for one forever.
            pipe.close()
            self._screen = io.StringIO()
            output = PlainTextOutput(self._screen) if capture else DummyOutput()
            with ui.driven_by(pipe, output):
                yield

    def answer(self, keys: str, ask: Callable[[], Any], *, capture: bool = False) -> Any:
        """Type ``keys`` at one widget and return what it decided.

        Args:
            keys: Every keystroke the widget will be given, in order.
            ask: A zero-argument call that puts the widget up.
            capture: Render into :attr:`rendered` instead of discarding.

        Returns:
            Whatever the widget returned, cancellation included.
        """
        with self.plugged_in(keys, capture=capture):
            return ask()


@pytest.fixture
def keyboard() -> Keyboard:
    """Return a scripted user for the real widgets.

    Returns:
        A :class:`Keyboard` that can type at any of the six widgets.
    """
    return Keyboard()


class TestSelect:
    """One option out of several, and what leaving without one looks like."""

    def test_enter_takes_the_option_the_pointer_starts_on(self, keyboard):
        assert keyboard.answer(ENTER, lambda: ui.select("Pick", CHOICES)) == "alpha"

    def test_an_arrow_down_then_enter_takes_the_second_option(self, keyboard):
        assert keyboard.answer(DOWN + ENTER, lambda: ui.select("Pick", CHOICES)) == "beta"

    def test_a_default_starts_the_pointer_on_that_value(self, keyboard):
        """The default is where the pointer *starts*, so Enter alone accepts it."""
        answer = keyboard.answer(ENTER, lambda: ui.select("Pick", CHOICES, default="gamma"))
        assert answer == "gamma"

    def test_a_default_is_a_starting_point_and_not_a_lock(self, keyboard):
        """Arrowing away from the default still lands where the user went."""
        answer = keyboard.answer(DOWN + ENTER, lambda: ui.select("Pick", CHOICES, default="alpha"))
        assert answer == "beta"

    def test_a_default_naming_no_choice_is_ignored_rather_than_raising(self, keyboard):
        """A stale config value must not crash the wizard that reads it.

        ``config.yml`` outlives the choices it names — a destination is removed,
        a provider is renamed — and the underlying library raises ``ValueError``
        on a default it cannot find. Falling back to the first option asks the
        question; raising ends the run at the question that was going to fix it.
        """
        answer = keyboard.answer(ENTER, lambda: ui.select("Pick", CHOICES, default="retired"))
        assert answer == "alpha"

    def test_the_user_reads_the_label_and_the_caller_gets_the_value(self, keyboard):
        answer = keyboard.answer(ENTER, lambda: ui.select("Pick", CHOICES), capture=True)
        assert answer == "alpha"
        assert "First thing" in keyboard.rendered

    def test_a_cancelled_select_returns_none(self, keyboard):
        assert keyboard.answer(CTRL_C, lambda: ui.select("Pick", CHOICES)) is None


class TestCheckbox:
    """Any number of options, including none — which is an answer, not a cancel."""

    def test_space_then_enter_ticks_the_option_under_the_pointer(self, keyboard):
        assert keyboard.answer(SPACE + ENTER, lambda: ui.checkbox("Pick", CHOICES)) == ("alpha",)

    def test_two_ticks_come_back_in_the_order_the_choices_were_offered(self, keyboard):
        keys = SPACE + DOWN + SPACE + ENTER
        assert keyboard.answer(keys, lambda: ui.checkbox("Pick", CHOICES)) == ("alpha", "beta")

    def test_enter_alone_returns_an_empty_tuple(self, keyboard):
        """Ticking nothing is a decision the user made, and it has a value."""
        assert keyboard.answer(ENTER, lambda: ui.checkbox("Pick", CHOICES)) == ()

    def test_an_empty_answer_is_not_a_cancelled_one(self, keyboard):
        """The one confusion that would silently re-enable everything.

        A caller that tests the answer for truthiness cannot tell "the user
        turned every destination off" from "the user left", and would treat the
        deliberate empty selection as "no answer, keep the old config". The two
        are different objects here, and every caller must branch on ``is None``.
        """
        empty = keyboard.answer(ENTER, lambda: ui.checkbox("Pick", CHOICES))
        cancelled = keyboard.answer(CTRL_C, lambda: ui.checkbox("Pick", CHOICES))
        assert empty is not None
        assert cancelled is None

    def test_a_value_named_in_selected_starts_ticked(self, keyboard):
        answer = keyboard.answer(ENTER, lambda: ui.checkbox("Pick", CHOICES, selected=("beta",)))
        assert answer == ("beta",)

    def test_a_pre_ticked_value_can_be_unticked(self, keyboard):
        """Pre-ticking has to be a starting state, not a floor."""
        keys = DOWN + SPACE + ENTER
        answer = keyboard.answer(keys, lambda: ui.checkbox("Pick", CHOICES, selected=("beta",)))
        assert answer == ()

    def test_a_choice_that_declares_itself_enabled_starts_ticked(self, keyboard):
        choices = (ui.Choice("alpha", "First thing"), ui.Choice("beta", "Second", enabled=True))
        assert keyboard.answer(ENTER, lambda: ui.checkbox("Pick", choices)) == ("beta",)

    def test_a_cancelled_checkbox_returns_none(self, keyboard):
        assert keyboard.answer(CTRL_C, lambda: ui.checkbox("Pick", CHOICES)) is None


class TestConfirm:
    """Yes, no, and the third answer that is neither."""

    def test_y_is_yes(self, keyboard):
        assert keyboard.answer("y", lambda: ui.confirm("Sync now?")) is True

    def test_n_is_no(self, keyboard):
        assert keyboard.answer("n", lambda: ui.confirm("Sync now?", default=True)) is False

    def test_enter_alone_takes_the_default(self, keyboard):
        assert keyboard.answer(ENTER, lambda: ui.confirm("Sync now?", default=True)) is True

    def test_enter_alone_takes_a_negative_default_too(self, keyboard):
        """The default is whatever the caller declared, not whatever is safest."""
        assert keyboard.answer(ENTER, lambda: ui.confirm("Delete?", default=False)) is False

    def test_a_cancelled_confirm_returns_none_and_none_is_not_false(self, keyboard):
        """Declining and leaving are different instructions.

        ``--prune`` asks before deleting. A cancel read as ``False`` would only
        skip the delete, which is harmless; a cancel read as an *answer* means
        the wizard carries on to question eight with a value the user never
        gave. The identity check is the assertion — ``None == False`` is already
        false, but ``not None`` and ``not False`` are both true, which is how
        the two get confused in the first place.
        """
        answer = keyboard.answer(CTRL_C, lambda: ui.confirm("Sync now?"))
        assert answer is None
        assert answer is not False


class TestText:
    """Free text, its default, and a validator that will not let go."""

    def test_the_typed_text_comes_back(self, keyboard):
        assert keyboard.answer("Field notes" + ENTER, lambda: ui.text("Title")) == "Field notes"

    def test_enter_alone_accepts_the_default(self, keyboard):
        assert keyboard.answer(ENTER, lambda: ui.text("Title", default="Inbox")) == "Inbox"

    def test_a_default_is_editable_rather_than_final(self, keyboard):
        keys = erase("Inbox") + "Archive" + ENTER
        assert keyboard.answer(keys, lambda: ui.text("Title", default="Inbox")) == "Archive"

    def test_a_rejected_answer_keeps_the_prompt_open_until_one_passes(self, keyboard):
        """The refusal has to be a second chance, not a returned bad value.

        Asserted through the answer rather than through the validator's call
        count: what matters is that the rejected text never became the result,
        and a widget that returned it would fail this even if it had called the
        validator exactly as often as expected.
        """

        def not_blank(answer: str):
            """Refuse an empty API key.

            Args:
                answer: What has been typed so far.

            Returns:
                True when acceptable, otherwise the problem to show.
            """
            return bool(answer.strip()) or "Enter something."

        keys = ENTER + "eventually" + ENTER
        assert keyboard.answer(keys, lambda: ui.text("Title", validate=not_blank)) == "eventually"

    def test_a_cancelled_text_returns_none_rather_than_the_default(self, keyboard):
        assert keyboard.answer(CTRL_C, lambda: ui.text("Title", default="Inbox")) is None


class TestPassword:
    """The one widget whose whole job is what it does *not* do."""

    def test_the_typed_secret_comes_back(self, keyboard):
        assert keyboard.answer("AIzaSyD-secret" + ENTER, lambda: ui.password("Key")) == (
            "AIzaSyD-secret"
        )

    def test_the_secret_never_reaches_the_screen(self, keyboard):
        """A key echoed once survives in the scrollback and in every screen share.

        This is the terminal half of the problem the 0600 credential files solve
        on disk, and the reason the wizard uses ``password`` rather than
        ``text`` for the API key. The assertion is on the real rendering, so a
        widget that stopped masking fails here and not in review.
        """
        secret = "AIzaSyD-not-in-the-scrollback"
        answer = keyboard.answer(secret + ENTER, lambda: ui.password("Key"), capture=True)
        assert answer == secret
        assert secret not in keyboard.rendered
        assert "*" in keyboard.rendered


class TestPath:
    """Filesystem answers, where the widget is the only thing checking."""

    def test_a_typed_path_comes_back_verbatim(self, keyboard):
        """``~`` is left as typed, because the config file is allowed to hold it.

        Expanding here would write one machine's absolute home into a file the
        user may sync between machines, and the expansion belongs to whoever
        opens the path anyway.
        """
        assert keyboard.answer("~/Vaults/Ink" + ENTER, lambda: ui.path("Vault")) == "~/Vaults/Ink"

    def test_a_path_that_does_not_exist_yet_is_accepted(self, keyboard, tmp_path):
        """The usual answer names a folder Living Ink is about to create."""
        wanted = str(tmp_path / "not-there-yet")
        assert keyboard.answer(wanted + ENTER, lambda: ui.path("Vault")) == wanted

    def test_must_exist_accepts_a_directory_that_is_there(self, keyboard, tmp_path):
        assert keyboard.answer(
            str(tmp_path) + ENTER, lambda: ui.path("Vault", must_exist=True)
        ) == str(tmp_path)

    def test_must_exist_refuses_a_path_that_is_not_there(self, keyboard, tmp_path):
        """A typo in a vault path is otherwise found three stages into a sync.

        The refusal is asserted through what comes back: the missing path was
        typed and submitted first, so a widget that accepted it would return it
        and never read the correction that follows.
        """
        missing = str(tmp_path / "nowhere")
        keys = missing + ENTER + erase(missing) + str(tmp_path) + ENTER
        assert keyboard.answer(keys, lambda: ui.path("Vault", must_exist=True)) == str(tmp_path)

    def test_an_empty_answer_is_refused(self, keyboard, tmp_path):
        """Enter on an empty prompt is a slip, and "" is a path that resolves."""
        keys = ENTER + str(tmp_path) + ENTER
        assert keyboard.answer(keys, lambda: ui.path("Vault")) == str(tmp_path)

    def test_a_cancelled_path_returns_none(self, keyboard):
        assert keyboard.answer(CTRL_C, lambda: ui.path("Vault")) is None


class TestChoice:
    """What the user reads and what the caller stores are two different facts."""

    def test_the_value_and_the_label_are_separate(self):
        choice = ui.Choice("obsidian", "Obsidian vault")
        assert choice.value == "obsidian"
        assert choice.label == "Obsidian vault"

    def test_the_caller_never_receives_the_label(self, keyboard):
        """The label is prose and will be reworded; the value lands in config.

        A widget returning the label would put "Obsidian vault" in the config
        file, and the next copy edit to that sentence would silently invalidate
        every config written before it.
        """
        choices = (ui.Choice("obsidian", "Obsidian vault"),)
        answer = keyboard.answer(ENTER, lambda: ui.select("Where to?", choices), capture=True)
        assert answer == "obsidian"
        assert "Obsidian vault" in keyboard.rendered

    def test_a_choice_is_not_ticked_unless_it_says_so(self):
        assert ui.Choice("obsidian", "Obsidian vault").enabled is False


class TestRequired:
    """The one-line way to refuse a ``None`` instead of passing it on."""

    def test_an_answer_passes_straight_through(self):
        assert ui.required("obsidian") == "obsidian"

    def test_none_is_refused(self):
        with pytest.raises(ui.Cancelled):
            ui.required(None)

    def test_false_is_an_answer_and_not_a_refusal(self):
        """A declined confirm is a value, and wrapping it must not throw it out."""
        assert ui.required(False) is False

    def test_an_empty_tuple_is_an_answer_and_not_a_refusal(self):
        """The checkbox answer that a truthiness check would have rejected."""
        assert ui.required(()) == ()

    def test_cancelled_is_a_keyboard_interrupt(self):
        assert issubclass(ui.Cancelled, KeyboardInterrupt)

    def test_an_existing_keyboard_interrupt_handler_catches_it(self):
        """This is the whole reason for the subclass, and it is what exits 130.

        ``cli.main`` already turns a ``KeyboardInterrupt`` into exit code 130,
        because Ctrl+C during a sync had to mean that. Leaving a wizard at
        question seven is the same act, so it reaches the same handler without
        a single ``except Cancelled`` being added anywhere.
        """
        try:
            ui.required(None)
        except KeyboardInterrupt as caught:
            assert isinstance(caught, ui.Cancelled)
        else:
            pytest.fail("Cancelled escaped an 'except KeyboardInterrupt'")


class _Stream:
    """A stand-in stream that says whatever a test needs it to about itself.

    Real streams cannot be talked into lying: pytest's own capture has already
    replaced both of them by the time a test runs, and neither a terminal nor a
    pipe can be conjured in-process. The write and flush methods exist only so
    that nothing printing during the test explodes on the substitute.
    """

    def __init__(self, tty: bool) -> None:
        """Record the answer this stream will give.

        Args:
            tty: What ``isatty()`` should report.
        """
        self.tty = tty

    def isatty(self) -> bool:
        """Report whether this stream claims to be a terminal.

        Returns:
            Whatever the test asked for.
        """
        return self.tty

    def write(self, text: str) -> int:
        """Swallow output.

        Args:
            text: Ignored.

        Returns:
            The number of characters nominally written.
        """
        return len(text)

    def flush(self) -> None:
        """Do nothing, successfully."""


class TestIsTty:
    """Whether this process can hold a conversation, asked live every time."""

    def test_two_terminals_can_hold_a_conversation(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", _Stream(True))
        monkeypatch.setattr(sys, "stdout", _Stream(True))
        assert ui.is_tty() is True

    def test_a_piped_stdin_cannot_answer(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", _Stream(False))
        monkeypatch.setattr(sys, "stdout", _Stream(True))
        assert ui.is_tty() is False

    def test_a_redirected_stdout_cannot_show_the_question(self, monkeypatch):
        """Both ends matter: a question nobody can read is not worth asking.

        ``living-ink setup > log.txt`` still has a keyboard attached, and
        checking stdin alone would put the wizard up with its prompts in the
        file and the user staring at a blank screen.
        """
        monkeypatch.setattr(sys, "stdin", _Stream(True))
        monkeypatch.setattr(sys, "stdout", _Stream(False))
        assert ui.is_tty() is False

    def test_the_answer_is_read_live_and_not_frozen_at_import(self, monkeypatch):
        """The bug the function replaced: ``IS_TTY = sys.stdout.isatty()``.

        A constant is computed once, at import, in whatever environment
        happened to be current — which for an embedded caller, a test, or a
        ``patch_stdout`` block is not the environment the question is asked in.
        Two calls with different streams have to give two answers.
        """
        monkeypatch.setattr(sys, "stdin", _Stream(True))
        monkeypatch.setattr(sys, "stdout", _Stream(True))
        assert ui.is_tty() is True

        monkeypatch.setattr(sys, "stdout", _Stream(False))
        assert ui.is_tty() is False

    def test_the_test_harness_counts_as_a_terminal(self, keyboard, monkeypatch):
        """Otherwise every widget test would be unreachable.

        The pipe *is* the terminal for a driven run, and a caller that checks
        :func:`~living_ink.ui.is_tty` before asking — which is what ``dispatch``
        does — would refuse to put the widget up at all.
        """
        monkeypatch.setattr(sys, "stdin", _Stream(False))
        monkeypatch.setattr(sys, "stdout", _Stream(False))
        with keyboard.plugged_in(""):
            assert ui.is_tty() is True


class TestDrivenBy:
    """The harness has to hand the widgets back, on every way out."""

    def test_the_widgets_go_back_to_the_real_streams_afterwards(self, keyboard, monkeypatch):
        """A leaked driver would leave later tests reading a dead pipe.

        :func:`~living_ink.ui.is_tty` is the readable witness: it answers True
        for as long as the driver is installed and falls back to the real
        streams once it is gone.
        """
        monkeypatch.setattr(sys, "stdin", _Stream(False))
        monkeypatch.setattr(sys, "stdout", _Stream(False))
        assert ui.is_tty() is False

        with keyboard.plugged_in(""):
            assert ui.is_tty() is True

        assert ui.is_tty() is False

    def test_a_raising_body_still_hands_the_widgets_back(self, keyboard, monkeypatch):
        """The exit that matters, since a failing test is how the body raises."""
        monkeypatch.setattr(sys, "stdin", _Stream(False))
        monkeypatch.setattr(sys, "stdout", _Stream(False))

        with pytest.raises(ZeroDivisionError):
            with keyboard.plugged_in(""):
                1 / 0

        assert ui.is_tty() is False

    def test_a_nested_block_restores_the_outer_driver_rather_than_clearing_it(self):
        """Restoring the previous state is not the same as clearing it.

        A ``finally`` that only emptied the driver would leave the outer block
        driving nothing, and the widget after it would read the developer's own
        terminal — which in a test run means blocking on a keyboard nobody is
        watching. The proof is that a widget run after the inner block still
        answers from the outer pipe.
        """
        outer = Keyboard()
        inner = Keyboard()

        with outer.plugged_in(DOWN + ENTER):
            assert inner.answer("y", lambda: ui.confirm("Inner?")) is True
            assert ui.select("Outer?", CHOICES) == "beta"
