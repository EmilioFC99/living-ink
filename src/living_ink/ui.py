"""The interactive layer: six widgets, one theme, the colours and the TTY check.

Every question Living Ink asks a human goes through this module. The flows live
elsewhere — the wizard in :mod:`living_ink.cli.commands.setup`, the menu in
:mod:`living_ink.cli.commands.config` — and this holds only the primitives, so
that the answer to "how does this tool ask a question" is one file rather than
121 call sites passing ``input_func`` and ``print_func`` to each other.

``questionary`` is imported here and **nowhere else**. That is what makes the
widget the seam: a test drives the real widgets through a pipe
(:func:`driven_by`) rather than substituting a fake ``input``, so what the test
exercises is what the user sees.

Three rules the callers depend on:

* **A widget returns ``None`` when the user cancels**, never a default and
  never a partial value. ``questionary`` swallows Ctrl+C into ``None``, so a
  ``None`` that falls through as a value is a silent misconfiguration —
  :func:`required` is the one-line way to refuse it.
* **:class:`Cancelled` is a :class:`KeyboardInterrupt`.** A cancel at question
  seven of a wizard is the user leaving, and it has to reach ``main`` as the
  same exit 130 as a Ctrl+C during a sync. Subclassing means every existing
  ``except KeyboardInterrupt`` already handles it.
* **:func:`is_tty` is a function, never a cached constant.** The old
  ``IS_TTY = sys.stdout.isatty()`` froze the answer at import while other call
  sites re-checked live, which is two answers to one question.

The ANSI helpers live here too, and they answer a *different* question from
:func:`is_tty`: see :func:`colour_enabled`. They were in ``setup_wizard``, so
every module that wanted a green tick imported the onboarding flow to get one.
"""

import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Sequence, Union

import questionary
from prompt_toolkit.styles import Style

#: What a validator may return: ``True`` for fine, or the message to show.
Validator = Callable[[str], Union[bool, str]]

#: Shown when an interactive command is run without a terminal. Names the
#: non-interactive alternative, because a user who hit this is scripting and
#: telling them only "run it in a terminal" leaves them nowhere to go.
NO_TTY_MESSAGE = (
    "This command is interactive; run it in a terminal. "
    "To configure Living Ink without one, set the matching environment "
    "variables (see 'living-ink info') and run 'living-ink sync'."
)

#: One theme for every widget. Cyan for the question, green for the answer the
#: user settled on, and dim for the hints — the same three the console report
#: has always used, so the wizard and the run do not look like two products.
THEME = Style(
    [
        ("qmark", "fg:#00afaf bold"),
        ("question", "bold"),
        ("answer", "fg:#5faf5f bold"),
        ("pointer", "fg:#00afaf bold"),
        ("highlighted", "fg:#00afaf bold"),
        ("selected", "fg:#5faf5f"),
        ("separator", "fg:#6c6c6c"),
        ("instruction", "fg:#6c6c6c"),
        ("text", ""),
        ("disabled", "fg:#6c6c6c italic"),
    ]
)


class Cancelled(KeyboardInterrupt):
    """The user pressed Ctrl+C or Escape at a prompt.

    A :class:`KeyboardInterrupt` on purpose: leaving a wizard halfway is the
    same act as interrupting a sync, and it must exit 130 rather than looking
    like a step that returned nothing.
    """


@dataclass(frozen=True)
class Choice:
    """One option in a :func:`select` or :func:`checkbox`.

    Attributes:
        value: What the caller gets back. Never shown.
        label: What the user reads.
        description: An optional second line, dimmed, for a choice whose label
            cannot carry the whole answer.
        enabled: Whether a checkbox starts this one ticked.
    """

    value: str
    label: str
    description: Optional[str] = None
    enabled: bool = False


#: Keyword arguments forwarded to every widget, empty outside tests. The test
#: harness (§17.6) fills it with a ``prompt_toolkit`` pipe so the real widgets
#: can be driven by scripted keystrokes; production never touches it.
_DRIVER: dict[str, Any] = {}


@contextmanager
def driven_by(pipe_input: Any, output: Any) -> Iterator[None]:
    """Route every widget through a scripted input and a captured output.

    The whole interactive layer is untestable without this, and an untested
    interactive layer is most of the product.

    Args:
        pipe_input: A ``prompt_toolkit`` pipe input to read keystrokes from.
        output: A ``prompt_toolkit`` output to render into.

    Yields:
        None, for the duration of the redirection.
    """
    previous = dict(_DRIVER)
    _DRIVER.clear()
    _DRIVER.update(input=pipe_input, output=output)
    try:
        yield
    finally:
        _DRIVER.clear()
        _DRIVER.update(previous)


def is_tty() -> bool:
    """Report whether this process can hold a conversation.

    Both ends have to be a terminal: a piped stdin cannot answer, and a
    redirected stdout cannot show the question being answered.

    Returns:
        True when stdin and stdout are both terminals.
    """
    if _DRIVER:
        # Under the test harness the pipe *is* the terminal, and asking the
        # real streams would make every widget test unreachable.
        return True
    return bool(sys.stdin.isatty() and sys.stdout.isatty())


def colour_enabled() -> bool:
    """Report whether ANSI escapes will be read by a terminal.

    Deliberately *not* :func:`is_tty`: colouring is a property of the output
    stream alone. A ``living-ink sync < /dev/null`` in a terminal still has a
    human watching it, and refusing to colour that run because stdin is a pipe
    is the kind of answer that makes people think the tool is broken.

    Returns:
        True when stdout is a terminal and ``NO_COLOR`` is unset.
    """
    if os.environ.get("NO_COLOR"):
        return False
    return bool(sys.stdout.isatty())


def _c(value: str, code: str) -> str:
    """Wrap text in an ANSI escape, or leave it alone.

    Args:
        value: The text to style.
        code: The SGR parameter, without the escape or the terminator.

    Returns:
        The styled text on a terminal, otherwise ``value`` unchanged.
    """
    return f"\033[{code}m{value}\033[0m" if colour_enabled() else value


def bold(value: str) -> str:
    """Make text bold.

    Args:
        value: The text to style.

    Returns:
        The styled text.
    """
    return _c(value, "1")


def green(value: str) -> str:
    """Format text in green — something worked.

    Args:
        value: The text to style.

    Returns:
        The styled text.
    """
    return _c(value, "92")


def yellow(value: str) -> str:
    """Format text in yellow — something needs attention.

    Args:
        value: The text to style.

    Returns:
        The styled text.
    """
    return _c(value, "93")


def cyan(value: str) -> str:
    """Format text in cyan — a heading or a prompt.

    Args:
        value: The text to style.

    Returns:
        The styled text.
    """
    return _c(value, "96")


def red(value: str) -> str:
    """Format text in red — something failed.

    Args:
        value: The text to style.

    Returns:
        The styled text.
    """
    return _c(value, "91")


def dim(value: str) -> str:
    """Format text dimly — a hint, a path, a provenance note.

    Args:
        value: The text to style.

    Returns:
        The styled text.
    """
    return _c(value, "2")


def required(value: Optional[Any]) -> Any:
    """Return an answer, or refuse to continue without one.

    Args:
        value: Whatever a widget returned.

    Returns:
        The value, unchanged.

    Raises:
        Cancelled: If the value is None, i.e. the user cancelled.
    """
    if value is None:
        raise Cancelled()
    return value


def select(
    message: str,
    choices: Sequence[Choice],
    *,
    default: Optional[str] = None,
) -> Optional[str]:
    """Ask for one of several options.

    Args:
        message: The question.
        choices: The options, in the order they should appear.
        default: The value to start highlighted on.

    Returns:
        The chosen ``Choice.value``, or None if the user cancelled.
    """
    options = [_as_questionary_choice(choice) for choice in choices]
    selected = next((option for option in options if option.value == default), None)
    return questionary.select(
        message,
        choices=options,
        default=selected,
        style=THEME,
        # Typing the number still works, so muscle memory and the arrow keys
        # both land — which is the whole reason the shortcuts are on.
        use_shortcuts=True,
        **_DRIVER,
    ).ask()


def checkbox(
    message: str,
    choices: Sequence[Choice],
    *,
    selected: Sequence[str] = (),
) -> Optional[tuple]:
    """Ask for any number of several options.

    Args:
        message: The question.
        choices: The options, in the order they should appear.
        selected: Values to start ticked, overriding ``Choice.enabled``.

    Returns:
        The chosen values as a tuple — empty when the user ticked nothing,
        which is a real answer — or None if they cancelled.
    """
    options = [
        _as_questionary_choice(choice, checked=choice.value in selected or choice.enabled)
        for choice in choices
    ]
    answer = questionary.checkbox(message, choices=options, style=THEME, **_DRIVER).ask()
    return None if answer is None else tuple(answer)


def confirm(message: str, *, default: bool = False) -> Optional[bool]:
    """Ask a yes/no question.

    Args:
        message: The question.
        default: What Enter alone means.

    Returns:
        The answer, or None if the user cancelled. ``False`` and ``None`` are
        not the same: one declined, the other left.
    """
    return questionary.confirm(message, default=default, style=THEME, **_DRIVER).ask()


def text(
    message: str,
    *,
    default: str = "",
    validate: Optional[Validator] = None,
) -> Optional[str]:
    """Ask for a free-text value.

    Args:
        message: The question.
        default: Pre-filled, editable, and what Enter alone accepts.
        validate: Called on each keystroke; return True or the problem.

    Returns:
        The entered text, or None if the user cancelled.
    """
    return questionary.text(
        message,
        default=default,
        validate=validate,
        style=THEME,
        **_DRIVER,
    ).ask()


def password(message: str, *, validate: Optional[Validator] = None) -> Optional[str]:
    """Ask for a secret, without echoing it.

    Masking is not cosmetic: a key typed in clear text survives in the
    scrollback, in ``script`` logs and in a screen share, which is the terminal
    half of the disclosure problem the 0600 credential files solve on disk. The
    value goes straight to :mod:`living_ink.config.credentials` and is never
    logged.

    Args:
        message: The question.
        validate: Called on each keystroke; return True or the problem.

    Returns:
        The entered secret, or None if the user cancelled.
    """
    return questionary.password(message, validate=validate, style=THEME, **_DRIVER).ask()


def path(message: str, *, default: str = "", must_exist: bool = False) -> Optional[str]:
    """Ask for a filesystem path, with completion.

    Args:
        message: The question.
        default: Pre-filled, editable, and what Enter alone accepts.
        must_exist: Refuse a path that is not there. Off by default, because a
            destination folder is usually one Living Ink is about to create.

    Returns:
        The entered path with ``~`` left as typed, or None if cancelled.
    """

    def _validate(answer: str) -> Union[bool, str]:
        """Check the path exists, when the caller asked for that.

        Args:
            answer: What has been typed so far.

        Returns:
            True when acceptable, otherwise the problem to show.
        """
        if not answer.strip():
            return "Enter a path."
        if must_exist and not Path(answer).expanduser().exists():
            return "That path does not exist."
        return True

    return questionary.path(
        message,
        default=default,
        validate=_validate,
        style=THEME,
        **_DRIVER,
    ).ask()


def _as_questionary_choice(choice: Choice, *, checked: Optional[bool] = None) -> questionary.Choice:
    """Translate a Living Ink choice into the widget library's own.

    Kept private so that ``questionary`` stays inside this module: a caller
    that had to build a ``questionary.Choice`` would have imported the library,
    and the seam would be back where it was.

    Args:
        choice: The option to translate.
        checked: Initial tick state, for checkboxes only.

    Returns:
        The equivalent ``questionary.Choice``.
    """
    return questionary.Choice(
        title=choice.label,
        value=choice.value,
        description=choice.description,
        checked=checked,
    )
