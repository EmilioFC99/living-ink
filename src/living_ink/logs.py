"""One logging path for the whole package.

Living Ink grew three parallel output mechanisms: ``print()`` in the CLI,
``pipeline.log()`` which printed *and* appended to a file, and 53 ``logger.*``
calls across eight modules that went nowhere because no handler was ever
configured. The modules that know most about a failure — the providers, the
transports, the renderer — were the silent ones.

This module installs the missing handlers, exactly once, and decides how much
reaches the screen:

===========  ==========================================  =========================
Mode         Console                                     File
===========  ==========================================  =========================
default      friendly progress lines from ``log()``      everything, at DEBUG
``--quiet``  nothing below a warning                     everything, at DEBUG
``--verbose``  every record, formatted, on stderr        everything, at DEBUG
===========  ==========================================  =========================

Handlers go on the ``living_ink`` logger rather than the root logger, so
importing this package never reconfigures logging for an application that
embeds it, and third-party chatter stays out of the file.
"""

import logging
import sys
from datetime import datetime
from enum import Enum
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional

from living_ink.config import get_logs_dir
from living_ink.redact import SecretFilter, redact

#: Logger every module in the package writes through, directly or by propagation.
PACKAGE_LOGGER = "living_ink"

#: Where :func:`log` writes when nothing has configured a path. It lived on
#: ``pipeline`` for as long as ``log()`` did, which meant every module that
#: wanted to say something to the user had to import the pipeline to say it.
LOG_PATH = get_logs_dir() / "pipeline.log"

#: Keeps the log useful for a daemon without letting it grow without bound.
#: Five megabytes and three backups caps the whole thing at roughly 20 MB.
MAX_LOG_BYTES = 5_000_000
LOG_BACKUP_COUNT = 3

_FILE_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_VERBOSE_FORMAT = "%(levelname)-7s %(name)s: %(message)s"


class ConsoleMode(Enum):
    """How much of the log reaches the terminal."""

    #: Friendly progress lines only — what a user running ``sync`` expects.
    PLAIN = "plain"
    #: Nothing but warnings and errors, for cron jobs and daemons.
    QUIET = "quiet"
    #: Every record from every module, formatted, on stderr.
    VERBOSE = "verbose"
    #: Progress on stderr, so stdout carries nothing but the JSON document.
    #: Without this a single "Destination added: ..." line makes the output of
    #: ``--json`` unparseable, which defeats the point of the flag.
    JSON = "json"


_console_mode = ConsoleMode.PLAIN
_configured_path: Optional[Path] = None


def configure(
    log_path: Path,
    *,
    verbose: bool = False,
    quiet: bool = False,
    json_output: bool = False,
) -> None:
    """Install the file and console handlers for the package logger.

    Safe to call more than once: existing Living Ink handlers are removed
    first, so a second call re-points the log rather than doubling every line.
    ``watch`` relies on this, since it runs many syncs in one process.

    Args:
        log_path: File to append to. Rotated rather than truncated, so the
            failure a user wants to report is not erased by the retry that
            followed it.
        verbose: Send every record to stderr as well.
        quiet: Suppress ordinary progress output. Ignored when ``verbose``
            is set, since asking for both is a contradiction and the more
            explicit request wins.
        json_output: Keep stdout free for a JSON document by moving progress
            lines to stderr. Ranks below ``verbose`` and ``quiet``, both of
            which already keep stdout clean.
    """
    global _console_mode, _configured_path

    if verbose:
        _console_mode = ConsoleMode.VERBOSE
    elif quiet:
        _console_mode = ConsoleMode.QUIET
    elif json_output:
        _console_mode = ConsoleMode.JSON
    else:
        _console_mode = ConsoleMode.PLAIN

    logger = logging.getLogger(PACKAGE_LOGGER)
    logger.setLevel(logging.DEBUG)
    # An embedding application owns the root logger; we only own ours.
    logger.propagate = False
    reset_handlers()

    secret_filter = SecretFilter()

    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=MAX_LOG_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(_FILE_FORMAT))
    # Attached to the handler, not the logger: a filter on a logger is not
    # consulted for records propagating up from its children, which is how
    # nearly every record in this package arrives.
    file_handler.addFilter(secret_filter)
    logger.addHandler(file_handler)

    if _console_mode is ConsoleMode.VERBOSE:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setLevel(logging.DEBUG)
        stream_handler.setFormatter(logging.Formatter(_VERBOSE_FORMAT))
        stream_handler.addFilter(secret_filter)
        logger.addHandler(stream_handler)

    _configured_path = log_path


def ensure_configured(log_path: Path) -> None:
    """Configure logging if nothing has yet, or if the target file moved.

    Lets ``pipeline.log()`` keep its promise that a message reaches the log
    file even when the pipeline is driven by something other than the CLI —
    a test, a script, or an embedding application. The console mode already
    chosen is preserved, so this never undoes ``--quiet`` or ``--verbose``.

    Args:
        log_path: Where the log should be written.
    """
    if _configured_path == Path(log_path):
        return
    configure(
        log_path,
        verbose=_console_mode is ConsoleMode.VERBOSE,
        quiet=_console_mode is ConsoleMode.QUIET,
    )


def mark_run_start() -> None:
    """Write a separator so one run can be told from the next.

    The log used to be truncated at the start of every run, which is harmless
    for a one-shot ``sync`` and destructive for ``watch``: the failure a user
    wants to report was erased by the retry that followed it. A marker gives
    the same "where does this run begin" answer without losing history.
    """
    logging.getLogger(PACKAGE_LOGGER).info(
        "─── run started %s ───", datetime.now().isoformat(timespec="seconds")
    )


def reset_handlers() -> None:
    """Remove the handlers this module installed.

    Closing them matters on Windows-style locking filesystems and in tests,
    where an open rotating handler keeps a temp directory alive.
    """
    global _configured_path
    logger = logging.getLogger(PACKAGE_LOGGER)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    _configured_path = None


def console_mode() -> ConsoleMode:
    """Return the current console verbosity.

    Returns:
        The mode selected by the last :func:`configure` call, or
        :attr:`ConsoleMode.PLAIN` if it was never called.
    """
    return _console_mode


def configured_path() -> Optional[Path]:
    """Return the log file currently being written, if any.

    Returns:
        The path passed to the last :func:`configure` call, or None.
    """
    return _configured_path


def console(message: str) -> None:
    """Print a progress line, unless the mode says otherwise.

    Suppressed under ``--quiet`` because it is noise for a daemon, and under
    ``--verbose`` because the stderr handler already emits the same record with
    more context — printing both would duplicate every line. Under ``--json``
    it moves to stderr rather than disappearing: the user still wants to watch
    a long sync, they just need stdout to stay machine-readable.

    Args:
        message: The already-redacted line to show.
    """
    if _console_mode is ConsoleMode.PLAIN:
        print(message)
    elif _console_mode is ConsoleMode.JSON:
        print(message, file=sys.stderr)


def log(message: Any) -> None:
    """Say one line to the user, and put it in the log file.

    The friendly progress line the pipeline and the stages emit, redacted at
    this single choke point rather than at each of the ~90 call sites: the log
    file is what a user attaches to a bug report.

    Console and file are separate decisions: ``--quiet`` silences the first,
    and the second is a rotating handler shared with every other module's
    logger calls, so the file holds more than just these messages.

    Args:
        message: Anything printable. Stringified here.
    """
    text = redact(str(message))
    console(text)
    ensure_configured(LOG_PATH)
    logging.getLogger(PACKAGE_LOGGER).info(text)
