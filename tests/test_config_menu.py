"""Tests for ``living-ink config`` — the menu, and above all what it writes.

The widgets are replaced rather than driven through a pipe, for the reason
given in :mod:`tests.test_wizard`: :mod:`tests.test_ui` already proves a real
``questionary`` prompt reads a real keystroke, and re-proving it once per menu
row would be a test of ``prompt_toolkit``.

What is worth pinning here is different from the wizard, though, and it is why
:class:`Menu` below is a *queue* rather than a lookup keyed by question text.
The wizard asks each question once; the menu loops, so "What would you like to
change?" is asked four times in one session and the four answers are only
distinguishable by their order. A driver matching on text would answer the top
menu the same way every time and spin forever.

Three things this file exists to catch:

* A save that materialises defaults into the file, or loses a section the
  schema has never heard of.
* A secret that reaches ``config.yml``, or an API key filed under a name
  nothing reads back.
* A setting that ``info`` prints and the menu cannot reach — the one failure
  that makes "every setting is configurable" untrue without anything breaking.
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
import yaml

from living_ink import logs, ui
from living_ink.cli.commands import config as config_module
from living_ink.cli.commands.config import (
    ADVANCED,
    BACK,
    DISCARD,
    PROMPTS,
    RESET,
    SAVE,
    ConfigMenu,
    coerce,
    editor_command,
    label_for,
    settings_in,
)
from living_ink.config import credentials
from living_ink.config.schema import ACTIVE, LIVE_SETTINGS, SECTIONS, STORE_CREDENTIALS


class Menu:
    """A scripted user working through a looping menu.

    Attributes:
        asked: Every question put to the user, in order, paired with the widget
            that asked it — so a test can assert that a path row got the path
            widget and a secret row got the password one.
    """

    def __init__(self, steps: List[Tuple[str, Any]]) -> None:
        """Prepare the answers, in the order they will be given.

        Args:
            steps: ``(fragment, answer)`` pairs. The fragment is matched
                case-insensitively against the question, which turns a drifted
                flow into a failure naming the question it did not expect
                rather than one that quietly answers it.
        """
        self._steps = list(steps)
        self.asked: List[Tuple[str, str]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> "Menu":
        """Replace every widget in :mod:`living_ink.ui` with this script.

        Args:
            monkeypatch: The fixture that undoes it afterwards.

        Returns:
            This script, for chaining.
        """
        for widget in ("select", "checkbox", "confirm", "text", "password", "path"):
            monkeypatch.setattr(ui, widget, self._replier(widget))
        return self

    def _replier(self, widget: str):
        """Build the stand-in for one widget.

        Args:
            widget: The function name being replaced.

        Returns:
            A callable with the widget's signature shape.
        """

        def reply(message: str, *_args: Any, **_kwargs: Any) -> Any:
            """Answer the next question.

            Args:
                message: The question as the user would read it.

            Returns:
                The scripted answer.

            Raises:
                AssertionError: If the script has run out, or the question is
                    not the one the next step expected.
            """
            self.asked.append((widget, message))
            if not self._steps:
                raise AssertionError(
                    f"The menu asked more than the script answers: {message!r}. "
                    f"Already asked: {[q for _w, q in self.asked]}"
                )
            fragment, answer = self._steps.pop(0)
            assert fragment.lower() in message.lower(), (
                f"Expected a question about {fragment!r}, got {message!r}"
            )
            return answer

        return reply

    @property
    def exhausted(self) -> bool:
        """Report whether every scripted answer was used.

        Returns:
            True when the script ran out exactly, which is what a flow test
            means by "and then it stopped asking".
        """
        return not self._steps

    def widget_for(self, fragment: str) -> str:
        """Return which widget asked the question matching ``fragment``.

        Args:
            fragment: Case-insensitive text from the question.

        Returns:
            The widget's function name.

        Raises:
            AssertionError: If no question matched.
        """
        for widget, message in self.asked:
            if fragment.lower() in message.lower():
                return widget
        raise AssertionError(f"Nothing asked about {fragment!r}; asked {self.asked}")


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the menu at a throwaway config, with no environment leaking in.

    Every variable the schema declares is cleared. The resolver reads the real
    environment, so a developer with ``LIVING_INK_AI_MODEL`` exported would
    otherwise see different origins from CI — and the menu's whole right-hand
    column is origins.

    Args:
        tmp_path: Per-test directory.
        monkeypatch: The fixture that undoes the environment changes.

    Returns:
        The path the menu will read and write.
    """
    for setting in LIVE_SETTINGS:
        if setting.env:
            monkeypatch.delenv(setting.env, raising=False)
    for name in list(os.environ):
        if name.startswith(("LIVING_INK_", "REMARKABLE_", "SYNC_", "OPENAI_")):
            monkeypatch.delenv(name, raising=False)

    path = tmp_path / "config.yml"
    monkeypatch.setenv("LIVING_INK_CONFIG", str(path))
    # ``console`` prints only in PLAIN mode, and the mode is a module global a
    # previous test may have moved.
    monkeypatch.setattr(logs, "_console_mode", logs.ConsoleMode.PLAIN)
    return path


def write(path: Path, values: Dict[str, Any]) -> Path:
    """Write a starting configuration.

    Args:
        path: Where to write it.
        values: The mapping to serialise.

    Returns:
        The path, for chaining.
    """
    path.write_text(yaml.safe_dump(values), encoding="utf-8")
    return path


def saved(path: Path) -> Dict[str, Any]:
    """Read back what the menu wrote.

    Args:
        path: The config file.

    Returns:
        The parsed mapping.
    """
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def drive(steps: List[Tuple[str, Any]], monkeypatch: pytest.MonkeyPatch) -> ConfigMenu:
    """Run one menu session against a script.

    Args:
        steps: The scripted answers, in order.
        monkeypatch: The fixture that installs them.

    Returns:
        The menu afterwards, so a test can inspect its pending edits.
    """
    script = Menu(steps).install(monkeypatch)
    menu = ConfigMenu()
    menu.run()
    assert script.exhausted, "The menu stopped before the script ran out"
    return menu


def edit_steps(section: str, field: str, *answers: Any) -> List[Tuple[str, Any]]:
    """Build the script for "go in, change one thing, come out, discard".

    Args:
        section: The section to enter.
        field: The setting to edit.
        *answers: The answers to that setting's edit prompts, in order. The
            fragment is left empty because *which* question is asked is the
            thing under test in :class:`TestTheWidgetMatchesTheKind`.

    Returns:
        The steps, ending at the main menu's Discard row.
    """
    return [
        ("what would you like to change", section),
        (f"{section} —", field),
        *[("", answer) for answer in answers],
        (f"{section} —", BACK),
        ("what would you like to change", DISCARD),
        ("throw away", True),
    ]


class TestEverySettingIsReachable:
    """The promise the menu exists to keep.

    ``info`` prints one line per live setting. If any of them has no row here,
    the tool tells a user about a value it will not let them change — and the
    failure is invisible: nothing crashes, the menu is just missing a line.
    """

    def test_every_live_setting_is_in_exactly_one_section(self):
        homes = {
            setting.field: [name for name in SECTIONS if setting in settings_in(name)]
            for setting in LIVE_SETTINGS
        }
        assert {field: names for field, names in homes.items() if len(names) != 1} == {}

    def test_every_section_the_menu_files_a_setting_under_is_active(self):
        for setting in LIVE_SETTINGS:
            assert SECTIONS[setting.menu_section].status == ACTIVE, setting.field

    def test_the_menu_calls_a_setting_what_info_calls_it(self):
        """The two surfaces must agree, or the menu is unsearchable.

        ``info`` prints ``SettingOrigin.name``, which is the field name. A user
        arrives at this menu holding a line they read there.
        """
        from living_ink.settings import Settings

        printed = {origin.name for origin in Settings.explain({})}
        assert {label_for(setting) for setting in LIVE_SETTINGS} == printed

    def test_a_credential_is_filed_with_what_it_unlocks(self):
        homes = {s.field: s.menu_section for s in LIVE_SETTINGS if s.store == STORE_CREDENTIALS}
        assert homes == {
            "ssh_password": "remarkable",
            "remarkable_token": "remarkable",
            "ai_api_key": "ai",
        }

    def test_the_sections_offered_cover_every_setting(self, config_file):
        write(config_file, {})
        offered = {choice.value for choice in ConfigMenu().main_choices()}
        assert {s.menu_section for s in LIVE_SETTINGS} <= offered


class TestNothingIsWrittenUntilSave:
    """The rule the whole module is arranged around."""

    def test_discarding_leaves_the_file_exactly_as_it_was(self, config_file, monkeypatch):
        write(config_file, {"ai": {"model": "old-model"}})
        before = config_file.read_text(encoding="utf-8")

        drive(
            [
                ("what would you like to change", "ai"),
                ("ai —", "ai_model"),
                ("new value", "new-model"),
                ("ai —", BACK),
                ("what would you like to change", DISCARD),
                ("throw away 1 unsaved change", True),
            ],
            monkeypatch,
        )
        assert config_file.read_text(encoding="utf-8") == before

    def test_an_edit_is_held_in_memory_under_its_field_name(self, config_file, monkeypatch):
        write(config_file, {"ai": {"model": "old-model"}})
        menu = drive(
            [
                ("what would you like to change", "ai"),
                ("ai —", "ai_model"),
                ("new value", "new-model"),
                ("ai —", BACK),
                ("what would you like to change", DISCARD),
                ("throw away", True),
            ],
            monkeypatch,
        )
        assert menu.edits == {"ai_model": "new-model"}

    def test_declining_the_save_confirmation_writes_nothing(self, config_file, monkeypatch):
        write(config_file, {"ai": {"model": "old-model"}})
        drive(
            [
                ("what would you like to change", "ai"),
                ("ai —", "ai_model"),
                ("new value", "new-model"),
                ("ai —", BACK),
                ("what would you like to change", SAVE),
                ("save?", False),
                ("what would you like to change", DISCARD),
                ("throw away", True),
            ],
            monkeypatch,
        )
        assert saved(config_file) == {"ai": {"model": "old-model"}}

    def test_backing_out_of_a_discard_offers_the_save_instead(self, config_file, monkeypatch):
        """Saying "no, do not throw it away" must not then throw it away."""
        write(config_file, {"ai": {"model": "old-model"}})
        drive(
            [
                ("what would you like to change", "ai"),
                ("ai —", "ai_model"),
                ("new value", "new-model"),
                ("ai —", BACK),
                ("what would you like to change", DISCARD),
                ("throw away", False),
                ("save?", True),
            ],
            monkeypatch,
        )
        assert saved(config_file)["ai"]["model"] == "new-model"

    def test_leaving_an_edit_prompt_records_nothing(self, config_file, monkeypatch):
        """Cancelling one question backs out of that question, not the session."""
        write(config_file, {"ai": {"model": "old-model"}})
        menu = drive(
            [
                ("what would you like to change", "ai"),
                ("ai —", "ai_model"),
                ("new value", None),
                ("ai —", BACK),
                ("what would you like to change", DISCARD),
            ],
            monkeypatch,
        )
        assert menu.edits == {}

    def test_ctrl_c_at_a_menu_leaves_by_raising(self, config_file, monkeypatch):
        """A cancel at a *menu* is the user leaving, and must reach exit 130."""
        write(config_file, {"ai": {"model": "old-model"}})
        Menu([("what would you like to change", None)]).install(monkeypatch)
        with pytest.raises(ui.Cancelled):
            ConfigMenu().run()


class TestWhatASaveWrites:
    """A save is a rewrite, so what it leaves out matters as much as what it puts in."""

    def _set_limit(self, monkeypatch, typed: str = "7") -> None:
        """Change one ordinary numeric setting and save.

        Args:
            monkeypatch: The fixture installing the script.
            typed: What to type into it.
        """
        drive(
            [
                ("what would you like to change", "sync"),
                ("sync —", "max_notebooks_per_run"),
                ("new value", typed),
                ("sync —", BACK),
                ("what would you like to change", SAVE),
                ("save?", True),
            ],
            monkeypatch,
        )

    def test_only_the_edited_key_is_added(self, config_file, monkeypatch):
        write(config_file, {"ai": {"model": "old-model"}})
        self._set_limit(monkeypatch)
        result = saved(config_file)
        assert result["ai"] == {"model": "old-model"}
        assert result["sync"] == {"limit": 7}

    def test_the_defaults_are_not_materialised(self, config_file, monkeypatch):
        """The file stays a list of decisions, not a snapshot of every default.

        Writing every effective value would freeze today's defaults into a
        config meant to track them, so a later release improving one would
        silently not apply.
        """
        write(config_file, {})
        self._set_limit(monkeypatch)
        assert saved(config_file) == {"schema_version": 1, "sync": {"limit": 7}}

    def test_a_whole_number_is_written_as_a_number(self, config_file, monkeypatch):
        """YAML reads ``7`` and ``"7"`` back as different types."""
        write(config_file, {})
        self._set_limit(monkeypatch)
        value = saved(config_file)["sync"]["limit"]
        assert value == 7
        assert isinstance(value, int)

    def test_a_section_the_schema_never_heard_of_survives(self, config_file, monkeypatch):
        """A plugin destination's section is the user's, and a save is not a way to lose it."""
        write(config_file, {"notion": {"token_name": "work"}})
        self._set_limit(monkeypatch)
        assert saved(config_file)["notion"] == {"token_name": "work"}

    def test_an_empty_answer_removes_the_key_rather_than_writing_a_blank(
        self, config_file, monkeypatch
    ):
        write(config_file, {"ai": {"model": "old-model"}, "sync": {"limit": 9}})
        drive(
            [
                ("what would you like to change", "ai"),
                ("ai —", "ai_model"),
                ("new value", "   "),
                ("ai —", BACK),
                ("what would you like to change", SAVE),
                ("save?", True),
            ],
            monkeypatch,
        )
        result = saved(config_file)
        assert "model" not in result.get("ai", {})
        assert result["sync"] == {"limit": 9}

    def test_saving_with_nothing_changed_does_not_rewrite_the_file(self, config_file, monkeypatch):
        """A rewrite costs the user's comments; doing it for no change is a bug."""
        config_file.write_text(
            "# a comment worth keeping\nai:\n  model: old-model\n", encoding="utf-8"
        )
        before = config_file.read_text(encoding="utf-8")
        drive([("what would you like to change", SAVE)], monkeypatch)
        assert config_file.read_text(encoding="utf-8") == before

    def test_undoing_a_section_drops_its_pending_edits(self, config_file, monkeypatch):
        write(config_file, {"ai": {"model": "old-model"}})
        menu = drive(
            [
                ("what would you like to change", "ai"),
                ("ai —", "ai_model"),
                ("new value", "new-model"),
                ("ai —", RESET),
                ("ai —", BACK),
                ("what would you like to change", DISCARD),
            ],
            monkeypatch,
        )
        assert menu.edits == {}


class TestTheWidgetMatchesTheKind:
    """A setting is edited by the widget its declared kind calls for."""

    def test_a_flag_is_a_yes_no_question(self, config_file, monkeypatch):
        write(config_file, {})
        script = Menu(edit_steps("cache", "transcript_cache", False)).install(monkeypatch)
        ConfigMenu().run()
        assert script.widget_for("transcript_cache?") == "confirm"

    def test_a_choice_is_a_list(self, config_file, monkeypatch):
        write(config_file, {})
        script = Menu(edit_steps("remarkable", "preferred_connection", "cloud")).install(
            monkeypatch
        )
        ConfigMenu().run()
        assert script.widget_for("which one?") == "select"

    def test_a_path_gets_the_completing_widget(self, config_file, monkeypatch):
        write(config_file, {})
        script = Menu(edit_steps("obsidian", "obsidian_vault_path", "/tmp/vault")).install(
            monkeypatch
        )
        ConfigMenu().run()
        assert script.widget_for("path (empty") == "path"

    def test_a_list_with_declared_choices_is_a_checkbox(self, config_file, monkeypatch):
        write(config_file, {})
        script = Menu(edit_steps("sync", "sync_types", ("notebook", "pdf"))).install(monkeypatch)
        ConfigMenu().run()
        assert script.widget_for("tick the ones") == "checkbox"

    def test_a_list_without_choices_is_typed(self, config_file, monkeypatch):
        write(config_file, {})
        script = Menu(edit_steps("sync", "sync_exclude", "Trash, Archive")).install(monkeypatch)
        ConfigMenu().run()
        assert script.widget_for("comma-separated") == "text"

    def test_a_number_is_typed(self, config_file, monkeypatch):
        write(config_file, {})
        script = Menu(edit_steps("ocr", "ocr_concurrency", "3")).install(monkeypatch)
        ConfigMenu().run()
        assert script.widget_for("new value") == "text"

    def test_a_typed_list_is_split_on_commas(self, config_file, monkeypatch):
        write(config_file, {})
        menu = drive(edit_steps("sync", "sync_exclude", " Trash , Archive ,, "), monkeypatch)
        assert menu.edits == {"sync_exclude": ("Trash", "Archive")}

    def test_a_ticked_list_is_stored_as_ticked(self, config_file, monkeypatch):
        write(config_file, {})
        menu = drive(edit_steps("sync", "sync_types", ("notebook", "epub")), monkeypatch)
        assert menu.edits == {"sync_types": ("notebook", "epub")}

    def test_choosing_the_default_row_removes_the_key(self, config_file, monkeypatch):
        write(config_file, {"remarkable": {"preferred_connection": "cloud"}})
        drive(
            [
                ("what would you like to change", "remarkable"),
                ("remarkable —", "preferred_connection"),
                ("which one?", RESET),
                ("remarkable —", BACK),
                ("what would you like to change", SAVE),
                ("save?", True),
            ],
            monkeypatch,
        )
        assert "preferred_connection" not in saved(config_file).get("remarkable", {})


class TestTheScheduleRow:
    """``watch.schedule`` is the one row whose answer is a cron expression.

    It is a kind rather than a special case in the menu — ``CRON`` — so what is
    worth pinning here is that the kind buys the three things a bare text box
    would not: presets, the next fire time beside each one, and a refusal to
    store five fields that do not parse. An invalid expression saved is a
    watcher that will not start, and the user finds out the next morning.
    """

    def _schedule_steps(self, *answers: Any) -> List[Tuple[str, Any]]:
        """Script a session that edits the schedule and discards.

        Args:
            *answers: The answers to the schedule's prompts, in order.

        Returns:
            The steps, ending at Discard.
        """
        return edit_steps("watch", "watch_schedule", *answers)

    def test_it_offers_the_presets_rather_than_a_text_box(self, config_file, monkeypatch):
        write(config_file, {})
        script = Menu(self._schedule_steps("0 9 * * *")).install(monkeypatch)
        ConfigMenu().run()
        assert script.widget_for("when?") == "select"

    def test_the_question_names_the_zone_the_times_are_in(self, config_file, monkeypatch):
        write(config_file, {"watch": {"timezone": "Asia/Tokyo"}})
        script = Menu(self._schedule_steps("0 9 * * *")).install(monkeypatch)
        ConfigMenu().run()
        question = next(q for _w, q in script.asked if "when?" in q.lower())
        assert "Asia/Tokyo" in question

    def test_a_preset_is_stored_as_its_expression(self, config_file, monkeypatch):
        write(config_file, {})
        menu = drive(self._schedule_steps("0 9,18 * * *"), monkeypatch)
        assert menu.edits == {"watch_schedule": "0 9,18 * * *"}

    def test_turning_it_off_removes_the_key(self, config_file, monkeypatch):
        write(config_file, {"watch": {"schedule": "0 9 * * *"}})
        drive(
            [
                ("what would you like to change", "watch"),
                ("watch —", "watch_schedule"),
                ("when?", RESET),
                ("watch —", BACK),
                ("what would you like to change", SAVE),
                ("save?", True),
            ],
            monkeypatch,
        )
        assert "schedule" not in saved(config_file).get("watch", {})

    def test_the_custom_row_asks_for_the_five_fields(self, config_file, monkeypatch):
        write(config_file, {})
        script = Menu(self._schedule_steps(config_module.CUSTOM_CRON, "30 7 * * 1-5")).install(
            monkeypatch
        )
        ConfigMenu().run()
        assert script.widget_for("cron expression") == "text"

    def test_a_typed_expression_is_stored_stripped(self, config_file, monkeypatch):
        write(config_file, {})
        menu = drive(
            self._schedule_steps(config_module.CUSTOM_CRON, "  30 7 * * 1-5  "), monkeypatch
        )
        assert menu.edits == {"watch_schedule": "30 7 * * 1-5"}

    def test_typing_nothing_removes_the_key(self, config_file, monkeypatch):
        write(config_file, {"watch": {"schedule": "0 9 * * *"}})
        drive(
            [
                ("what would you like to change", "watch"),
                ("watch —", "watch_schedule"),
                ("when?", config_module.CUSTOM_CRON),
                ("cron expression", "   "),
                ("watch —", BACK),
                ("what would you like to change", SAVE),
                ("save?", True),
            ],
            monkeypatch,
        )
        assert "schedule" not in saved(config_file).get("watch", {})

    def _cancel_steps(self, *answers: Any) -> List[Tuple[str, Any]]:
        """Script a session whose only edit was cancelled.

        Args:
            *answers: The answers to the schedule's prompts, ending in a
                cancel.

        Returns:
            The steps. There is no "throw away" confirmation at the end,
            because the menu only asks it when something is pending — which is
            itself the thing these two tests are checking.
        """
        return [
            ("what would you like to change", "watch"),
            ("watch —", "watch_schedule"),
            *[("", answer) for answer in answers],
            ("watch —", BACK),
            ("what would you like to change", DISCARD),
        ]

    def test_cancelling_the_picker_changes_nothing(self, config_file, monkeypatch):
        write(config_file, {})
        menu = drive(self._cancel_steps(None), monkeypatch)
        assert menu.edits == {}

    def test_cancelling_the_typed_expression_changes_nothing(self, config_file, monkeypatch):
        write(config_file, {})
        menu = drive(self._cancel_steps(config_module.CUSTOM_CRON, None), monkeypatch)
        assert menu.edits == {}


class TestTheScheduleIsShownBeforeItIsChosen:
    """Every option carries when it would actually fire.

    ``0 9 1 * *`` and ``0 9 * * 1`` both parse and both read as a morning
    schedule; one runs twelve times a year. The next fire time beside the row
    is the only thing on the screen that tells them apart.
    """

    def test_each_preset_says_when_it_would_next_fire(self, config_file, monkeypatch):
        from living_ink import scheduler

        write(config_file, {})
        captured: List[Any] = []

        def capture_select(message: str, choices: Any, **_kwargs: Any) -> Any:
            captured.extend(choices)
            return "0 9 * * *"

        script = Menu(self._steps()).install(monkeypatch)
        monkeypatch.setattr(ui, "select", _only_for("when?", capture_select, script))
        ConfigMenu().run()

        described = {c.value: c.description for c in captured if c.description}
        for _label, expression in scheduler.SCHEDULE_PRESETS:
            if expression is not None:
                assert described.get(expression), f"{expression} was offered with no next fire"

    def test_a_typed_expression_is_echoed_back_as_three_fire_times(
        self, config_file, monkeypatch, capsys
    ):
        write(config_file, {})
        drive(
            edit_steps("watch", "watch_schedule", config_module.CUSTOM_CRON, "0 9 * * 1"),
            monkeypatch,
        )
        assert capsys.readouterr().out.count("fires ") == 3

    @staticmethod
    def _steps() -> List[Tuple[str, Any]]:
        """Script a session that opens the schedule picker and discards.

        Returns:
            The steps, ending at Discard. The picker's own answer comes from
            the capturing stand-in rather than the script, so the ``when?``
            step is deliberately absent.
        """
        return [
            ("what would you like to change", "watch"),
            ("watch —", "watch_schedule"),
            ("watch —", BACK),
            ("what would you like to change", DISCARD),
            ("throw away", True),
        ]


def _only_for(fragment: str, replacement: Any, script: Menu) -> Any:
    """Route one question to ``replacement`` and the rest back to the script.

    The scripted :class:`Menu` is a queue, so a test that wants to inspect the
    *choices* of one prompt cannot simply replace ``ui.select`` wholesale — the
    section menu is a select too, and swallowing it would derail the flow.

    Args:
        fragment: Case-insensitive text identifying the question to intercept.
        replacement: What answers that one.
        script: The queue answering everything else.

    Returns:
        A stand-in for :func:`living_ink.ui.select`.
    """
    scripted = script._replier("select")

    def select(message: str, *args: Any, **kwargs: Any) -> Any:
        if fragment.lower() in message.lower():
            return replacement(message, *args, **kwargs)
        return scripted(message, *args, **kwargs)

    return select


class TestTheCronValidator:
    """What the keystroke validator accepts, refuses, and says about it."""

    def test_an_empty_answer_is_allowed(self):
        assert config_module._valid_cron("") is True
        assert config_module._valid_cron("   ") is True

    @pytest.mark.parametrize(
        "expression",
        ["0 9 * * *", "*/15 * * * *", "0 9,18 * * 1-5", "0 0 1 JAN *"],
    )
    def test_an_expression_croniter_reads_is_allowed(self, expression):
        assert config_module._valid_cron(expression) is True

    @pytest.mark.parametrize("expression", ["not a schedule", "0 9 * *", "99 9 * * *"])
    def test_an_expression_it_cannot_read_is_refused_with_a_reason(self, expression):
        problem = config_module._valid_cron(expression)
        assert problem is not True
        assert isinstance(problem, str) and problem


class TestTheZoneTheScheduleIsReadIn:
    """The picker's times follow the timezone being edited, not the saved one."""

    def test_an_unsaved_timezone_edit_is_what_the_times_are_shown_in(
        self, config_file, monkeypatch
    ):
        write(config_file, {"watch": {"timezone": "UTC"}})
        script = Menu(
            [
                ("what would you like to change", "watch"),
                ("watch —", "watch_timezone"),
                ("", "Asia/Tokyo"),
                ("watch —", "watch_schedule"),
                ("when?", "0 9 * * *"),
                ("watch —", BACK),
                ("what would you like to change", DISCARD),
                ("throw away", True),
            ]
        ).install(monkeypatch)
        ConfigMenu().run()
        question = next(q for _w, q in script.asked if "when?" in q.lower())
        assert "Asia/Tokyo" in question

    def test_an_unreadable_zone_falls_back_rather_than_ending_the_session(
        self, config_file, monkeypatch
    ):
        write(config_file, {"watch": {"timezone": "Mars/Olympus_Mons"}})
        menu = drive(edit_steps("watch", "watch_schedule", "0 9 * * *"), monkeypatch)
        assert menu.edits == {"watch_schedule": "0 9 * * *"}


class TestSecrets:
    """A credential never enters the config file, and never comes back out on screen."""

    def _store_key(self, monkeypatch, secret: str, provider_steps=()) -> None:
        """Run a session that sets the AI key and saves.

        Args:
            monkeypatch: The fixture installing the script.
            secret: The key to type.
            provider_steps: Extra steps run before the key is set.
        """
        drive(
            [
                ("what would you like to change", "ai"),
                *provider_steps,
                ("ai —", "ai_api_key"),
                ("what would you like to do", "set"),
                ("ai_api_key", secret),
                ("ai —", BACK),
                ("what would you like to change", SAVE),
                ("save?", True),
            ],
            monkeypatch,
        )

    def test_a_secret_is_asked_for_without_echoing(self, config_file, monkeypatch):
        write(config_file, {"ai": {"provider": "gemini"}})
        script = Menu(
            [
                ("what would you like to change", "ai"),
                ("ai —", "ai_api_key"),
                ("what would you like to do", "set"),
                ("ai_api_key", "AIzaSecret"),
                ("ai —", BACK),
                ("what would you like to change", DISCARD),
                ("throw away", True),
            ]
        ).install(monkeypatch)
        ConfigMenu().run()
        assert script.widget_for("ai_api_key") == "password"

    def test_a_secret_never_reaches_the_config_file(self, config_file, monkeypatch):
        write(config_file, {"ai": {"provider": "gemini"}})
        self._store_key(monkeypatch, "AIzaSecretValue")
        assert "AIzaSecretValue" not in config_file.read_text(encoding="utf-8")

    def test_a_secret_reaches_the_credentials_store(self, config_file, monkeypatch):
        write(config_file, {"ai": {"provider": "gemini"}})
        self._store_key(monkeypatch, "AIzaSecretValue")
        stored = credentials.read_secret("ai.api_key.gemini", config_path=config_file)
        assert stored == "AIzaSecretValue"

    def test_the_key_is_stored_against_the_provider_just_chosen(self, config_file, monkeypatch):
        """Changing the provider and the key in one session must agree.

        The credential name is composed from the provider, so a save reading
        the *old* provider would file the new key where nothing looks for it —
        and the next run would report "no API key" moments after one was set.
        """
        write(config_file, {"ai": {"provider": "gemini"}})
        self._store_key(
            monkeypatch,
            "sk-openai",
            provider_steps=(
                ("ai —", "ai_provider"),
                ("new value", "openai"),
            ),
        )
        assert credentials.read_secret("ai.api_key.openai", config_path=config_file) == "sk-openai"
        assert credentials.read_secret("ai.api_key.gemini", config_path=config_file) is None

    def test_a_key_with_no_provider_is_refused_rather_than_misfiled(
        self, config_file, monkeypatch, capsys
    ):
        """``ai.api_key.none`` is a file the reader never opens.

        The composer returns None when there is no provider, so the menu says
        so instead of storing a key that would look saved and never be found.
        """
        write(config_file, {})
        self._store_key(monkeypatch, "AIzaOrphan")
        assert credentials.read_secret("ai.api_key.none", config_path=config_file) is None
        assert "set ai_provider first" in capsys.readouterr().out.lower()

    def test_a_blank_password_leaves_the_stored_value_alone(self, config_file, monkeypatch):
        """Enter pressed by mistake must not cost a key that took a website visit."""
        write(config_file, {"ai": {"provider": "gemini"}})
        credentials.write_secret("ai.api_key.gemini", "AIzaKeep", config_path=config_file)
        menu = drive(
            [
                ("what would you like to change", "ai"),
                ("ai —", "ai_api_key"),
                ("what would you like to do", "set"),
                ("ai_api_key", "   "),
                ("ai —", BACK),
                ("what would you like to change", DISCARD),
            ],
            monkeypatch,
        )
        assert menu.edits == {}
        assert credentials.read_secret("ai.api_key.gemini", config_path=config_file) == "AIzaKeep"

    def test_leaving_it_alone_records_nothing(self, config_file, monkeypatch):
        write(config_file, {"ai": {"provider": "gemini"}})
        menu = drive(
            [
                ("what would you like to change", "ai"),
                ("ai —", "ai_api_key"),
                ("what would you like to do", BACK),
                ("ai —", BACK),
                ("what would you like to change", DISCARD),
            ],
            monkeypatch,
        )
        assert menu.edits == {}

    def test_removing_a_secret_deletes_the_credential(self, config_file, monkeypatch):
        write(config_file, {"ai": {"provider": "gemini"}})
        credentials.write_secret("ai.api_key.gemini", "AIzaDropMe", config_path=config_file)
        drive(
            [
                ("what would you like to change", "ai"),
                ("ai —", "ai_api_key"),
                ("what would you like to do", RESET),
                ("ai —", BACK),
                ("what would you like to change", SAVE),
                ("save?", True),
            ],
            monkeypatch,
        )
        assert credentials.read_secret("ai.api_key.gemini", config_path=config_file) is None

    def test_the_change_summary_masks_the_new_value(self, config_file):
        write(config_file, {"ai": {"provider": "gemini"}})
        menu = ConfigMenu()
        menu.edits = {"ai_api_key": "AIzaSuperSecretKeyValue"}
        rendered = "\n".join(menu.change_lines())
        assert "AIzaSuperSecretKeyValue" not in rendered
        assert "ai_api_key" in rendered


class TestPrecedenceIsExplained:
    """An edit that will not take effect has to say so at the moment it is made."""

    def test_an_environment_override_is_called_out(self, config_file, monkeypatch, capsys):
        write(config_file, {})
        monkeypatch.setenv("LIVING_INK_AI_MODEL", "from-the-shell")
        drive(
            [
                ("what would you like to change", "ai"),
                ("ai —", "ai_model"),
                ("new value", "from-the-menu"),
                ("ai —", BACK),
                ("what would you like to change", DISCARD),
                ("throw away", True),
            ],
            monkeypatch,
        )
        printed = capsys.readouterr().out
        assert "LIVING_INK_AI_MODEL" in printed
        assert "outranks" in printed

    def test_an_unshadowed_edit_just_reports_the_new_value(self, config_file, monkeypatch, capsys):
        write(config_file, {})
        drive(
            [
                ("what would you like to change", "ai"),
                ("ai —", "ai_model"),
                ("new value", "from-the-menu"),
                ("ai —", BACK),
                ("what would you like to change", DISCARD),
                ("throw away", True),
            ],
            monkeypatch,
        )
        printed = capsys.readouterr().out
        assert "from-the-menu" in printed
        assert "outranks" not in printed

    def test_the_menu_and_info_resolve_a_value_the_same_way(self, config_file, monkeypatch):
        """One resolver, so the two surfaces cannot disagree about a value."""
        from living_ink.settings import Settings

        write(config_file, {"ai": {"model": "from-the-file"}})
        monkeypatch.setenv("SYNC_OCR_CONCURRENCY", "11")
        menu = ConfigMenu()
        expected = {
            o.name: (o.value, o.source) for o in Settings.explain(menu.raw, config_path=config_file)
        }
        actual = {name: (o.value, o.source) for name, o in menu.origins().items()}
        assert actual == expected


class TestPrompts:
    """§6.1: the prompt files are opened, not described."""

    def test_both_prompt_files_ship_with_the_package(self):
        from living_ink import clean

        assert clean.OCR_PROMPT_FILE.exists()
        assert clean.PROMPT_FILE.exists()

    def test_it_opens_the_chosen_file_in_the_editor(self, config_file, monkeypatch):
        from living_ink import clean

        write(config_file, {})
        monkeypatch.setenv("EDITOR", "my-editor --wait")
        seen: Dict[str, Any] = {}

        def _run(command, **_kwargs):
            """Record the command instead of spawning it.

            Args:
                command: The argument list.

            Returns:
                A stand-in completed process.
            """
            seen["command"] = command
            return object()

        monkeypatch.setattr(config_module.subprocess, "run", _run)
        drive(
            [
                ("what would you like to change", PROMPTS),
                ("which prompt", str(clean.OCR_PROMPT_FILE)),
                ("what would you like to change", DISCARD),
            ],
            monkeypatch,
        )
        assert seen["command"] == ["my-editor", "--wait", str(clean.OCR_PROMPT_FILE)]

    def test_a_prompt_dir_is_seeded_and_edited_instead_of_the_packaged_copy(
        self, config_file, monkeypatch, tmp_path, capsys
    ):
        """An edit to the installed package is an edit the next upgrade deletes."""
        from living_ink import clean

        mine = tmp_path / "prompts"
        write(config_file, {"ai": {"prompt_dir": str(mine)}})
        seen: Dict[str, Any] = {}
        monkeypatch.setattr(config_module, "editor_command", lambda: ["my-editor"])
        monkeypatch.setattr(
            config_module.subprocess, "run", lambda command, **_k: seen.setdefault("c", command)
        )
        drive(
            [
                ("what would you like to change", PROMPTS),
                ("which prompt", str(clean.OCR_PROMPT_FILE)),
                ("what would you like to change", DISCARD),
            ],
            monkeypatch,
        )

        copy = mine / "ocr_prompt.txt"
        assert copy.read_text() == clean.OCR_PROMPT_FILE.read_text()
        assert seen["c"] == ["my-editor", str(copy)]

    def test_an_existing_prompt_dir_copy_is_offered_and_not_overwritten(
        self, config_file, monkeypatch, tmp_path
    ):
        from living_ink import clean

        mine = tmp_path / "prompts"
        mine.mkdir()
        (mine / "ocr_prompt.txt").write_text("Mine, edited last week.")
        write(config_file, {"ai": {"prompt_dir": str(mine)}})
        seen: Dict[str, Any] = {}
        monkeypatch.setattr(config_module, "editor_command", lambda: ["my-editor"])
        monkeypatch.setattr(
            config_module.subprocess, "run", lambda command, **_k: seen.setdefault("c", command)
        )
        drive(
            [
                ("what would you like to change", PROMPTS),
                ("which prompt", str(mine / "ocr_prompt.txt")),
                ("what would you like to change", DISCARD),
            ],
            monkeypatch,
        )

        assert (mine / "ocr_prompt.txt").read_text() == "Mine, edited last week."
        assert seen["c"] == ["my-editor", str(mine / "ocr_prompt.txt")]
        assert clean.OCR_PROMPT_FILE.read_text() != "Mine, edited last week."

    def test_no_editor_prints_the_path_rather_than_failing(self, config_file, monkeypatch, capsys):
        from living_ink import clean

        write(config_file, {})
        monkeypatch.setattr(config_module, "editor_command", lambda: None)
        drive(
            [
                ("what would you like to change", PROMPTS),
                ("which prompt", str(clean.PROMPT_FILE)),
                ("what would you like to change", DISCARD),
            ],
            monkeypatch,
        )
        assert str(clean.PROMPT_FILE) in capsys.readouterr().out

    def test_an_edit_warns_that_cached_pages_will_be_read_again(
        self, config_file, monkeypatch, capsys
    ):
        """The prompts are in the transcription fingerprint, so an edit costs API calls."""
        from living_ink import clean

        write(config_file, {})
        monkeypatch.setenv("EDITOR", "true")
        monkeypatch.setattr(config_module.subprocess, "run", lambda *_a, **_k: object())
        drive(
            [
                ("what would you like to change", PROMPTS),
                ("which prompt", str(clean.OCR_PROMPT_FILE)),
                ("what would you like to change", DISCARD),
            ],
            monkeypatch,
        )
        assert "read again" in capsys.readouterr().out

    def test_an_editor_that_fails_says_where_the_file_is(self, config_file, monkeypatch, capsys):
        from living_ink import clean

        write(config_file, {})
        monkeypatch.setenv("EDITOR", "my-editor")

        def _boom(*_args, **_kwargs):
            """Fail the way a missing editor does.

            Raises:
                OSError: Always.
            """
            raise OSError("No such file or directory")

        monkeypatch.setattr(config_module.subprocess, "run", _boom)
        drive(
            [
                ("what would you like to change", PROMPTS),
                ("which prompt", str(clean.PROMPT_FILE)),
                ("what would you like to change", DISCARD),
            ],
            monkeypatch,
        )
        assert str(clean.PROMPT_FILE) in capsys.readouterr().out


class TestEditorResolution:
    """``$VISUAL`` and ``$EDITOR`` conventionally carry arguments."""

    def test_visual_wins_over_editor(self, monkeypatch):
        monkeypatch.setenv("VISUAL", "vis")
        monkeypatch.setenv("EDITOR", "ed")
        assert editor_command() == ["vis"]

    def test_arguments_are_split_rather_than_execed_as_one_name(self, monkeypatch):
        monkeypatch.setenv("VISUAL", "code --wait")
        assert editor_command() == ["code", "--wait"]

    def test_a_quoted_path_survives_the_split(self, monkeypatch):
        monkeypatch.setenv("VISUAL", "'/Applications/My Editor' -w")
        assert editor_command() == ["/Applications/My Editor", "-w"]

    def test_a_blank_variable_is_not_an_editor(self, monkeypatch):
        monkeypatch.setenv("VISUAL", "   ")
        monkeypatch.setenv("EDITOR", "ed")
        assert editor_command() == ["ed"]

    def test_nothing_installed_and_nothing_declared_is_none(self, monkeypatch):
        monkeypatch.delenv("VISUAL", raising=False)
        monkeypatch.delenv("EDITOR", raising=False)
        monkeypatch.setattr(config_module, "FALLBACK_EDITORS", ("definitely-not-installed",))
        assert editor_command() is None


class TestAdvanced:
    """The destructive half, which lives here precisely so ``info`` does not carry it."""

    def _caches(self, monkeypatch) -> Dict[str, int]:
        """Replace the caches with recorders.

        Args:
            monkeypatch: The fixture that undoes it.

        Returns:
            A tally the test can assert on.
        """
        from living_ink.cli import caches as caches_api

        tally = {"clear": 0, "prune": 0}

        class _Cache:
            """A cache that records rather than deletes."""

            noun = "test"

            def clear(self) -> int:
                """Record a clear.

                Returns:
                    A plausible count.
                """
                tally["clear"] += 1
                return 3

            def prune(self) -> int:
                """Record a prune.

                Returns:
                    A plausible count.
                """
                tally["prune"] += 1
                return 1

        monkeypatch.setattr(caches_api, "all_caches", lambda: [_Cache()])
        return tally

    def test_clearing_the_caches_asks_first(self, config_file, monkeypatch):
        write(config_file, {})
        tally = self._caches(monkeypatch)
        drive(
            [
                ("what would you like to change", ADVANCED),
                ("advanced", "clear"),
                ("delete every cached", False),
                ("advanced", BACK),
                ("what would you like to change", DISCARD),
            ],
            monkeypatch,
        )
        assert tally["clear"] == 0

    def test_confirming_clears_them(self, config_file, monkeypatch):
        write(config_file, {})
        tally = self._caches(monkeypatch)
        drive(
            [
                ("what would you like to change", ADVANCED),
                ("advanced", "clear"),
                ("delete every cached", True),
                ("advanced", BACK),
                ("what would you like to change", DISCARD),
            ],
            monkeypatch,
        )
        assert tally["clear"] == 1

    def test_pruning_needs_no_confirmation(self, config_file, monkeypatch):
        """Pruning deletes only what has already expired, which is not a decision."""
        write(config_file, {})
        tally = self._caches(monkeypatch)
        drive(
            [
                ("what would you like to change", ADVANCED),
                ("advanced", "prune"),
                ("advanced", BACK),
                ("what would you like to change", DISCARD),
            ],
            monkeypatch,
        )
        assert tally["prune"] == 1

    def test_checking_a_database_that_is_not_there_says_so(
        self, config_file, monkeypatch, capsys, tmp_path
    ):
        from living_ink.cli import caches as caches_api

        write(config_file, {})
        monkeypatch.setattr(caches_api, "state_db_path", lambda: tmp_path / "absent.db")
        drive(
            [
                ("what would you like to change", ADVANCED),
                ("advanced", "check"),
                ("advanced", BACK),
                ("what would you like to change", DISCARD),
            ],
            monkeypatch,
        )
        assert "No sync database yet" in capsys.readouterr().out

    def test_a_healthy_database_reports_ok(self, config_file, monkeypatch, capsys, tmp_path):
        from living_ink import state
        from living_ink.cli import caches as caches_api

        write(config_file, {})
        db = tmp_path / "state.db"
        state.StateStore(db)
        monkeypatch.setattr(caches_api, "state_db_path", lambda: db)
        drive(
            [
                ("what would you like to change", ADVANCED),
                ("advanced", "check"),
                ("advanced", BACK),
                ("what would you like to change", DISCARD),
            ],
            monkeypatch,
        )
        assert "ok" in capsys.readouterr().out.lower()

    def test_compacting_asks_first(self, config_file, monkeypatch, tmp_path):
        from living_ink import state
        from living_ink.cli import caches as caches_api

        write(config_file, {})
        db = tmp_path / "state.db"
        state.StateStore(db)
        monkeypatch.setattr(caches_api, "state_db_path", lambda: db)
        vacuumed: List[bool] = []
        monkeypatch.setattr(state.StateStore, "vacuum", lambda self: vacuumed.append(True))
        drive(
            [
                ("what would you like to change", ADVANCED),
                ("advanced", "vacuum"),
                ("rebuild state.db", False),
                ("advanced", BACK),
                ("what would you like to change", DISCARD),
            ],
            monkeypatch,
        )
        assert vacuumed == []

    def test_confirming_compacts(self, config_file, monkeypatch, tmp_path):
        from living_ink import state
        from living_ink.cli import caches as caches_api

        write(config_file, {})
        db = tmp_path / "state.db"
        state.StateStore(db)
        monkeypatch.setattr(caches_api, "state_db_path", lambda: db)
        vacuumed: List[bool] = []
        monkeypatch.setattr(state.StateStore, "vacuum", lambda self: vacuumed.append(True))
        drive(
            [
                ("what would you like to change", ADVANCED),
                ("advanced", "vacuum"),
                ("rebuild state.db", True),
                ("advanced", BACK),
                ("what would you like to change", DISCARD),
            ],
            monkeypatch,
        )
        assert vacuumed == [True]


class TestCoerce:
    """Typed text becomes the type the file should hold."""

    @pytest.mark.parametrize(
        "field,typed,expected",
        [
            ("max_notebooks_per_run", "7", 7),
            ("ocr_concurrency", "12", 12),
            ("ai_temperature", "0.5", 0.5),
            ("ai_model", "gemini-2.0-flash", "gemini-2.0-flash"),
            ("obsidian_root_folder", "Notes", "Notes"),
        ],
    )
    def test_the_value_keeps_the_kind_it_was_declared_with(self, field, typed, expected):
        setting = next(s for s in LIVE_SETTINGS if s.field == field)
        result = coerce(setting, typed)
        assert result == expected
        assert type(result) is type(expected)

    def test_unparseable_text_is_stored_rather_than_crashing(self):
        """The validator refuses this; a paste past it must not end the session."""
        setting = next(s for s in LIVE_SETTINGS if s.field == "max_notebooks_per_run")
        assert coerce(setting, "not-a-number") == "not-a-number"


class TestTheCommand:
    """``living-ink config`` as the front end sees it."""

    def test_it_is_interactive_and_therefore_needs_a_terminal(self):
        assert config_module.ConfigCommand.interactive is True

    def test_it_declares_no_flags_of_its_own(self):
        """A flag here would be a third way to set a setting."""
        import argparse

        parser = argparse.ArgumentParser()
        config_module.ConfigCommand.register_args(parser)
        assert [action.dest for action in parser._actions] == ["help"]

    def test_an_unreadable_config_is_reported_rather_than_traced(self, config_file, capsys):
        config_file.write_text("ai: [unclosed\n", encoding="utf-8")
        assert config_module.ConfigCommand().run(_Namespace()) == 1
        assert "Could not read" in capsys.readouterr().out

    def test_leaving_the_menu_is_not_a_failure(self, config_file, monkeypatch):
        write(config_file, {})
        Menu([("what would you like to change", DISCARD)]).install(monkeypatch)
        assert config_module.ConfigCommand().run(_Namespace()) == 0

    def test_a_cancel_carrying_unsaved_edits_warns_on_the_way_out(
        self, config_file, monkeypatch, capsys
    ):
        write(config_file, {})
        Menu(
            [
                ("what would you like to change", "ai"),
                ("ai —", "ai_model"),
                ("new value", "new-model"),
                ("ai —", BACK),
                ("what would you like to change", None),
            ]
        ).install(monkeypatch)
        with pytest.raises(ui.Cancelled):
            config_module.ConfigCommand().run(_Namespace())
        assert "unsaved" in capsys.readouterr().out


class _Namespace:
    """The empty argument namespace ``config`` is handed."""
