"""Tests for the single logging path and the rotating log file."""

import logging

import pytest

from living_ink import logs
from living_ink import redact as redact_mod
from living_ink.logs import ConsoleMode

SECRET = "rm-device-token-abcdef123456"


@pytest.fixture(autouse=True)
def isolated_logging():
    """Keep handlers and the console mode from leaking between tests."""
    logs.reset_handlers()
    logs._console_mode = ConsoleMode.PLAIN
    redact_mod.clear_secrets()
    yield
    logs.reset_handlers()
    logs._console_mode = ConsoleMode.PLAIN
    redact_mod.clear_secrets()


def emit(message, level=logging.INFO, name="living_ink.providers"):
    """Log a record the way a module inside the package would."""
    logging.getLogger(name).log(level, message)


class TestHandlerInstallation:
    """The 53 logger calls scattered across the package had nowhere to go."""

    def test_a_module_logger_reaches_the_file(self, tmp_path):
        log_path = tmp_path / "pipeline.log"
        logs.configure(log_path)
        emit("provider returned 429")
        assert "provider returned 429" in log_path.read_text(encoding="utf-8")

    def test_debug_records_reach_the_file(self, tmp_path):
        """The file is the bug report, so it keeps detail the screen does not."""
        log_path = tmp_path / "pipeline.log"
        logs.configure(log_path)
        emit("request body 1.2 kB", level=logging.DEBUG)
        assert "request body 1.2 kB" in log_path.read_text(encoding="utf-8")

    def test_the_parent_directory_is_created(self, tmp_path):
        log_path = tmp_path / "nested" / "dir" / "pipeline.log"
        logs.configure(log_path)
        assert log_path.exists()

    def test_the_root_logger_is_left_alone(self, tmp_path):
        """An application embedding Living Ink owns its own logging."""
        before = list(logging.getLogger().handlers)
        logs.configure(tmp_path / "pipeline.log")
        assert logging.getLogger().handlers == before
        assert logging.getLogger(logs.PACKAGE_LOGGER).propagate is False

    def test_configuring_twice_does_not_duplicate_records(self, tmp_path):
        log_path = tmp_path / "pipeline.log"
        logs.configure(log_path)
        logs.configure(log_path)
        emit("published Meeting Notes")
        assert log_path.read_text(encoding="utf-8").count("published Meeting Notes") == 1


class TestRunBoundaries:
    """`watch` calls run() every interval; the log used to survive only one."""

    def test_a_second_run_does_not_erase_the_first(self, tmp_path):
        log_path = tmp_path / "pipeline.log"
        logs.configure(log_path)

        logs.mark_run_start()
        emit("connection refused")
        logs.mark_run_start()
        emit("all good")

        written = log_path.read_text(encoding="utf-8")
        # The failure a user wants to report, still present after the retry
        # that followed it succeeded.
        assert "connection refused" in written
        assert "all good" in written

    def test_each_run_is_separated(self, tmp_path):
        log_path = tmp_path / "pipeline.log"
        logs.configure(log_path)
        logs.mark_run_start()
        logs.mark_run_start()
        assert log_path.read_text(encoding="utf-8").count("run started") == 2

    def test_reconfiguring_appends_rather_than_truncating(self, tmp_path):
        log_path = tmp_path / "pipeline.log"
        logs.configure(log_path)
        emit("first process")
        logs.configure(log_path)
        emit("second process")
        written = log_path.read_text(encoding="utf-8")
        assert "first process" in written
        assert "second process" in written


class TestRotation:
    """Appending forever would be an unbounded write into the user's home."""

    def test_the_log_is_bounded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(logs, "MAX_LOG_BYTES", 2_000)
        monkeypatch.setattr(logs, "LOG_BACKUP_COUNT", 1)
        log_path = tmp_path / "pipeline.log"
        logs.configure(log_path)

        for index in range(400):
            emit(f"page {index} transcribed with a reasonably long message")

        total = sum(p.stat().st_size for p in tmp_path.iterdir())
        # Two files of at most ~2 kB each, rather than the ~25 kB written.
        assert total < 6_000

    def test_rotation_keeps_a_backup(self, tmp_path, monkeypatch):
        monkeypatch.setattr(logs, "MAX_LOG_BYTES", 1_000)
        monkeypatch.setattr(logs, "LOG_BACKUP_COUNT", 2)
        log_path = tmp_path / "pipeline.log"
        logs.configure(log_path)
        for index in range(200):
            emit(f"page {index} transcribed with a reasonably long message")
        assert (tmp_path / "pipeline.log.1").exists()


class TestConsoleModes:
    """Three modes, one decision point."""

    def test_plain_prints_progress(self, tmp_path, capsys):
        logs.configure(tmp_path / "pipeline.log")
        logs.console("Publishing Meeting Notes")
        assert "Publishing Meeting Notes" in capsys.readouterr().out

    def test_quiet_suppresses_progress_but_keeps_the_file(self, tmp_path, capsys):
        log_path = tmp_path / "pipeline.log"
        logs.configure(log_path, quiet=True)
        logs.console("Publishing Meeting Notes")
        emit("published Meeting Notes")

        assert capsys.readouterr().out == ""
        assert "published Meeting Notes" in log_path.read_text(encoding="utf-8")

    def test_verbose_sends_every_record_to_stderr(self, tmp_path, capsys):
        logs.configure(tmp_path / "pipeline.log", verbose=True)
        emit("retrying page 3", level=logging.DEBUG)
        assert "retrying page 3" in capsys.readouterr().err

    def test_verbose_does_not_also_print_plain_lines(self, tmp_path, capsys):
        """The stderr handler already emits the same record with more context."""
        logs.configure(tmp_path / "pipeline.log", verbose=True)
        logs.console("Publishing Meeting Notes")
        assert capsys.readouterr().out == ""

    def test_verbose_wins_over_quiet(self, tmp_path):
        logs.configure(tmp_path / "pipeline.log", verbose=True, quiet=True)
        assert logs.console_mode() is ConsoleMode.VERBOSE


class TestSecretsInTheLogFile:
    """The filter from the masking work finally has a handler to sit on."""

    def test_a_registered_secret_never_reaches_the_file(self, tmp_path):
        redact_mod.register_secret(SECRET)
        log_path = tmp_path / "pipeline.log"
        logs.configure(log_path)
        emit(f"authenticating with {SECRET}")

        written = log_path.read_text(encoding="utf-8")
        assert SECRET not in written
        assert "***redacted***" in written

    def test_a_secret_in_an_argument_is_masked(self, tmp_path):
        redact_mod.register_secret(SECRET)
        log_path = tmp_path / "pipeline.log"
        logs.configure(log_path)
        logging.getLogger("living_ink.providers").error("Details: %s", f"echo {SECRET}")
        assert SECRET not in log_path.read_text(encoding="utf-8")

    def test_the_verbose_stream_is_masked_too(self, tmp_path, capsys):
        redact_mod.register_secret(SECRET)
        logs.configure(tmp_path / "pipeline.log", verbose=True)
        emit(f"authenticating with {SECRET}")
        assert SECRET not in capsys.readouterr().err


class TestEnsureConfigured:
    """pipeline.log() must reach a file even with no CLI in the picture."""

    def test_it_configures_when_nothing_has(self, tmp_path):
        log_path = tmp_path / "pipeline.log"
        logs.ensure_configured(log_path)
        assert logs.configured_path() == log_path

    def test_it_follows_a_moved_target(self, tmp_path):
        first = tmp_path / "one.log"
        second = tmp_path / "two.log"
        logs.ensure_configured(first)
        logs.ensure_configured(second)
        emit("after the move")
        assert "after the move" in second.read_text(encoding="utf-8")

    def test_it_preserves_the_chosen_console_mode(self, tmp_path):
        logs.configure(tmp_path / "one.log", quiet=True)
        logs.ensure_configured(tmp_path / "two.log")
        assert logs.console_mode() is ConsoleMode.QUIET

    def test_it_does_not_reinstall_for_the_same_path(self, tmp_path):
        log_path = tmp_path / "pipeline.log"
        logs.configure(log_path)
        handlers = list(logging.getLogger(logs.PACKAGE_LOGGER).handlers)
        logs.ensure_configured(log_path)
        assert logging.getLogger(logs.PACKAGE_LOGGER).handlers == handlers


class TestJsonConsoleMode:
    """``--json`` promises stdout carries a JSON document and nothing else."""

    def test_progress_moves_to_stderr_rather_than_vanishing(self, tmp_path, capsys):
        """A long sync is still worth watching; it just must not corrupt stdout."""
        logs.configure(tmp_path / "log", json_output=True)
        logs.console("Destination added: Obsidian")
        captured = capsys.readouterr()

        assert captured.out == ""
        assert "Destination added: Obsidian" in captured.err

    def test_quiet_still_wins_because_it_asks_for_less(self, tmp_path, capsys):
        logs.configure(tmp_path / "log", quiet=True, json_output=True)
        logs.console("progress")
        captured = capsys.readouterr()

        assert (captured.out, captured.err) == ("", "")

    def test_verbose_still_wins_because_its_handler_already_prints(self, tmp_path):
        logs.configure(tmp_path / "log", verbose=True, json_output=True)
        assert logs.console_mode() is logs.ConsoleMode.VERBOSE

    def test_plain_output_is_unaffected(self, tmp_path, capsys):
        logs.configure(tmp_path / "log")
        logs.console("progress")

        assert capsys.readouterr().out == "progress\n"


class TestLogRedaction:
    """``log()`` writes the file users attach to bug reports."""

    def test_a_registered_secret_never_reaches_the_log_file(self, tmp_path, monkeypatch, capsys):
        redact_mod.register_secret(SECRET)
        log_path = tmp_path / "pipeline.log"
        monkeypatch.setattr(logs, "LOG_PATH", log_path)

        logs.log(f"connecting with {SECRET}")

        written = log_path.read_text(encoding="utf-8")
        assert SECRET not in written
        assert "***redacted***" in written
        # The same masked text is what the user saw on screen.
        assert SECRET not in capsys.readouterr().out

    def test_ordinary_messages_are_untouched(self, tmp_path, monkeypatch):
        log_path = tmp_path / "pipeline.log"
        monkeypatch.setattr(logs, "LOG_PATH", log_path)

        logs.log("Publishing Meeting Notes")

        assert "Publishing Meeting Notes" in log_path.read_text(encoding="utf-8")


class TestLogPersistence:
    """The log used to be truncated at the top of every run()."""

    def test_a_new_run_keeps_the_previous_run(self, tmp_path, monkeypatch):
        """`watch` calls run() every interval; it used to keep only the last."""
        log_path = tmp_path / "pipeline.log"
        monkeypatch.setattr(logs, "LOG_PATH", log_path)

        logs.log("connection refused")
        logs.mark_run_start()
        logs.log("all good")

        written = log_path.read_text(encoding="utf-8")
        assert "connection refused" in written
        assert "all good" in written

    def test_log_is_silent_on_the_console_when_quiet(self, tmp_path, monkeypatch, capsys):
        log_path = tmp_path / "pipeline.log"
        monkeypatch.setattr(logs, "LOG_PATH", log_path)
        logs.configure(log_path, quiet=True)

        logs.log("Publishing Meeting Notes")

        assert capsys.readouterr().out == ""
        assert "Publishing Meeting Notes" in log_path.read_text(encoding="utf-8")


class TestParserWarningsAreCollected:
    """rmscene reports an unreadable block by logging and carrying on.

    The package used to raise both parser loggers to ``ERROR`` at import time,
    so a page that parsed incompletely rendered a partial image and said so
    nowhere. Collecting instead of suppressing keeps the signal without
    letting a third-party log line land in the middle of a ``--json`` run.
    """

    def test_a_warning_is_captured_instead_of_printed(self, capsys):
        with logs.collect_parser_warnings() as collected:
            logging.getLogger("rmscene").warning("Unknown block type 42")

        assert collected == ["Unknown block type 42"]
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_a_child_logger_is_covered_by_its_root(self):
        """Naming ``rmscene`` has to be enough; there are six emitters."""
        with logs.collect_parser_warnings() as collected:
            logging.getLogger("rmscene.text").warning("Unknown formatting code")
            logging.getLogger("rmc.exporters.svg").warning("no bounding box")

        assert collected == ["Unknown formatting code", "no bounding box"]

    def test_the_same_complaint_is_kept_once(self):
        """A 40-page notebook logs one bad block shape 40 times."""
        with logs.collect_parser_warnings() as collected:
            for _ in range(40):
                logging.getLogger("rmscene").warning("Some data has not been read")

        assert collected == ["Some data has not been read"]

    def test_a_mismatched_format_string_does_not_take_the_render_down(self):
        """The library owns its format strings, and one of them is wrong."""
        with logs.collect_parser_warnings() as collected:
            logging.getLogger("rmscene").warning("needs %s and %s", "one")
            logging.getLogger("rmscene").warning("readable")

        assert collected == ["readable"]

    def test_info_is_not_worth_reporting(self):
        with logs.collect_parser_warnings() as collected:
            logging.getLogger("rmscene").info("read 12 blocks")

        assert collected == []

    def test_the_loggers_are_left_exactly_as_they_were(self):
        library = logging.getLogger("rmscene")
        before = (library.level, library.propagate, list(library.handlers))

        with logs.collect_parser_warnings():
            pass

        assert (library.level, library.propagate, list(library.handlers)) == before

    def test_they_are_restored_even_when_the_block_raises(self):
        library = logging.getLogger("rmscene")
        before = (library.level, library.propagate, list(library.handlers))

        with pytest.raises(RuntimeError):
            with logs.collect_parser_warnings():
                raise RuntimeError("render failed")

        assert (library.level, library.propagate, list(library.handlers)) == before

    def test_what_was_collected_before_a_failure_survives(self):
        """A document that stopped early is the one whose warnings explain it."""
        collected = None
        with pytest.raises(RuntimeError):
            with logs.collect_parser_warnings() as messages:
                collected = messages
                logging.getLogger("rmscene").warning("Unknown block type 42")
                raise RuntimeError("render failed")

        assert collected == ["Unknown block type 42"]
