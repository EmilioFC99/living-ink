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


class TestCtrlDIsAlsoACancel:
    """Ctrl+D leaves a prompt, and it has to leave it the same way Ctrl+C does.

    ``questionary`` catches only ``KeyboardInterrupt``; ``prompt_toolkit``
    raises ``EOFError`` on Ctrl+D at an empty buffer, and nothing in the library
    catches that. So the four text-entry widgets used to end the run in a
    traceback while the two list widgets swallowed the key — two answers to one
    keystroke, and the traceback was the one a user leaving a wizard got.

    The list widgets are here too. They absorb the key rather than acting on
    it, which makes them *look* fine, but the assertion worth pinning is that
    all six agree, not that four of them were fixed.
    """

    #: The end-of-transmission byte a terminal sends for Ctrl+D.
    CTRL_D = "\x04"

    @pytest.mark.parametrize(
        "name,ask",
        [
            ("confirm", lambda: ui.confirm("Sync now?")),
            ("password", lambda: ui.password("API key")),
            ("text", lambda: ui.text("Model")),
            ("path", lambda: ui.path("Vault")),
        ],
    )
    def test_a_text_widget_treats_it_as_a_cancel(self, keyboard, name, ask):
        assert keyboard.answer(self.CTRL_D, ask) is None

    def test_it_reaches_the_caller_as_the_same_cancelled_as_ctrl_c(self, keyboard):
        """The point of the fix: one ``except KeyboardInterrupt`` covers both."""
        with pytest.raises(ui.Cancelled):
            ui.required(keyboard.answer(self.CTRL_D, lambda: ui.text("Model")))
        with pytest.raises(ui.Cancelled):
            ui.required(keyboard.answer(CTRL_C, lambda: ui.text("Model")))


class TestALongListStillAsksTheQuestion:
    """A select with more rows than there are shortcut keys must not crash.

    ``questionary`` assigns shortcuts from a fixed table of ten digits and
    twenty-six letters, and past it raises ``ValueError`` — at construction,
    before ``ask()``, so it does not even arrive as a cancel. The config menu is
    exactly the list that finds this: one row per setting is already close to
    the limit and the schema only grows.
    """

    @staticmethod
    def many(count: int) -> tuple:
        """Return ``count`` distinct choices.

        Args:
            count: How many to build.

        Returns:
            A tuple of choices whose values and labels differ.
        """
        return tuple(ui.Choice(f"v{i}", f"Row number {i}") for i in range(count))

    def test_the_cap_is_read_from_the_library_rather_than_hardcoded(self):
        """A library bump that changes the table must not need an edit here."""
        assert ui.SHORTCUT_LIMIT == 36

    @pytest.mark.parametrize("count", [1, 36, 37, 80])
    def test_a_list_of_any_length_answers(self, keyboard, count):
        answer = keyboard.answer(ENTER, lambda: ui.select("Pick", self.many(count)))
        assert answer == "v0"

    def test_the_arrow_keys_still_work_past_the_cap(self, keyboard):
        """Shortcuts are what is dropped; navigation is not."""
        answer = keyboard.answer(DOWN + ENTER, lambda: ui.select("Pick", self.many(40)))
        assert answer == "v1"

    def test_a_default_past_the_cap_still_places_the_pointer(self, keyboard):
        answer = keyboard.answer(ENTER, lambda: ui.select("Pick", self.many(40), default="v25"))
        assert answer == "v25"


class TestTheAdvertisedShortcutIsTheOneThatFires:
    """Typing the letter a row is labelled with picks that row.

    ``j`` and ``k`` are the twentieth and twenty-first shortcuts, and
    ``questionary`` registers its vi navigation *after* the shortcut bindings.
    ``prompt_toolkit`` fires the last binding that matches, so on a list that
    long the two keys moved the cursor instead of choosing the rows the screen
    was visibly offering under them — a menu that lies about its own labels.
    """

    def test_a_digit_picks_the_row_it_labels(self, keyboard):
        assert keyboard.answer("3" + ENTER, lambda: ui.select("Pick", CHOICES)) == "gamma"

    def test_the_twentieth_row_is_reachable_by_its_letter(self, keyboard):
        rows = TestALongListStillAsksTheQuestion.many(30)
        # Ten digits come first, so the twentieth row is labelled ``j``.
        assert keyboard.answer("j" + ENTER, lambda: ui.select("Pick", rows)) == "v19"

    def test_the_twenty_first_row_is_reachable_by_its_letter(self, keyboard):
        rows = TestALongListStillAsksTheQuestion.many(30)
        assert keyboard.answer("k" + ENTER, lambda: ui.select("Pick", rows)) == "v20"

    def test_vi_navigation_comes_back_once_there_are_no_shortcuts_to_steal(self, keyboard):
        """Past the cap the labels are gone, so ``j`` is free to mean "down"."""
        rows = TestALongListStillAsksTheQuestion.many(40)
        assert keyboard.answer("j" + ENTER, lambda: ui.select("Pick", rows)) == "v1"


class TestSelectedOverridesTheChoicesOwnFlags:
    """``selected=`` is an override, and ``()`` is a different answer from ``None``.

    The two were ``or``-ed together, so a caller handing back "these are the
    ones currently on" could never turn a choice that declared itself enabled
    *off*. That is precisely the config-menu case: the set comes from the
    config file, and the choice's own flag is the shipped default it is meant
    to replace.
    """

    ONE_ON = (ui.Choice("alpha", "First"), ui.Choice("beta", "Second", enabled=True))

    def test_no_opinion_leaves_the_choices_own_flags_alone(self, keyboard):
        assert keyboard.answer(ENTER, lambda: ui.checkbox("Pick", self.ONE_ON)) == ("beta",)

    def test_an_empty_override_turns_everything_off(self, keyboard):
        answer = keyboard.answer(ENTER, lambda: ui.checkbox("Pick", self.ONE_ON, selected=()))
        assert answer == ()

    def test_an_override_replaces_the_flags_rather_than_adding_to_them(self, keyboard):
        answer = keyboard.answer(
            ENTER, lambda: ui.checkbox("Pick", self.ONE_ON, selected=("alpha",))
        )
        assert answer == ("alpha",)


class TestColourNeverReachesAWidget:
    """A widget renders its text literally, so an escape in it is visible.

    ``console()`` writes to a terminal that reads ANSI; ``questionary`` hands a
    plain-string title to ``prompt_toolkit``, which puts it on the screen as
    text. The config menu's value column used ``ui.dim`` for the provenance
    note and arrived as ``^[[2m(config file)^[[0m``. Styling inside a widget
    belongs to :data:`ui.THEME`, so the colour is stripped at the boundary
    rather than left to each caller to remember.

    **The rendered assertions look for ``^[``, not ``\\x1b``, and that is the
    whole reason they catch anything.** ``prompt_toolkit`` renders a control
    character in caret notation, so a leaked escape reaches the screen as the
    two printable characters the user actually sees — searching the render for
    a raw ``\\x1b`` finds nothing whether the bug is present or not, which is
    how the first version of these tests passed against the unfixed code.
    """

    @pytest.mark.parametrize(
        "styled",
        [
            lambda: ui.dim("(default)"),
            lambda: ui.cyan("  • edited"),
            lambda: ui.yellow("(env)"),
            lambda: f"{ui.bold('name')}  {ui.dim('(config file)')}{ui.cyan(' • edited')}",
        ],
    )
    def test_every_helper_is_stripped(self, styled, monkeypatch):
        """Including a label composed of several, which is the real shape."""
        monkeypatch.setattr(ui, "colour_enabled", lambda: True)
        text = styled()
        assert "\033[" in text, "the helper did not colour, so this proves nothing"

        assert "\033" not in ui.plain(text)

    def test_text_with_no_colour_is_returned_unchanged(self):
        """Stripping is not reformatting: spacing a menu column relies on it."""
        assert ui.plain("true  (config file)  • edited") == "true  (config file)  • edited"

    def test_a_coloured_label_renders_clean(self, keyboard, monkeypatch):
        """End to end: the escape is gone from what the terminal is shown.

        The unit test above proves the function; this proves it is *called*,
        which is the half that regressed.
        """
        monkeypatch.setattr(ui, "colour_enabled", lambda: True)
        rows = [ui.Choice("a", f"watch_enabled  true  {ui.dim('(config file)')}")]

        keyboard.answer(ENTER, lambda: ui.select("Watch", rows), capture=True)

        assert "(config file)" in keyboard.rendered
        assert "^[" not in keyboard.rendered

    def test_a_coloured_question_renders_clean(self, keyboard, monkeypatch):
        """The message is text too, and takes the same route."""
        monkeypatch.setattr(ui, "colour_enabled", lambda: True)

        keyboard.answer("y\r", lambda: ui.confirm(f"Remove {ui.bold('everything')}?"), capture=True)

        assert "everything" in keyboard.rendered
        assert "^[" not in keyboard.rendered

    def test_a_coloured_description_renders_clean(self, keyboard, monkeypatch):
        """The second line is dimmed by the theme, not by the caller."""
        monkeypatch.setattr(ui, "colour_enabled", lambda: True)
        rows = [ui.Choice("a", "First", description=ui.yellow("shadowed by an env var"))]

        keyboard.answer(ENTER, lambda: ui.select("Pick", rows), capture=True)

        assert "shadowed by an env var" in keyboard.rendered
        assert "^[" not in keyboard.rendered

    def test_the_shortcut_numbers_survive(self, keyboard, monkeypatch):
        """Why the colour is dropped rather than translated.

        ``questionary`` accepts a list of style/text pairs as a title, which
        would keep the colour — but the branch rendering one skips the
        shortcut prefix and the highlight class, so the ``1)`` numbers and the
        cursor would go instead. Losing the styling is the cheaper trade, and
        this is the thing that trade was made to protect.
        """
        monkeypatch.setattr(ui, "colour_enabled", lambda: True)
        rows = [
            ui.Choice("a", ui.dim("First")),
            ui.Choice("b", ui.dim("Second")),
        ]

        keyboard.answer(ENTER, lambda: ui.select("Pick", rows), capture=True)

        assert "1)" in keyboard.rendered
        assert "2)" in keyboard.rendered
