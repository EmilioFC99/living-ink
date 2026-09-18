"""Keep credentials out of log files and error output.

The usual way a secret escapes an open-source tool is a user pasting a log into
a bug report. Two paths lead there: ``pipeline.log()`` appends every message to
``pipeline.log`` on disk, and the AI provider logs the full HTTP response body
on an error — bodies that several providers fill with an echo of the request.

Rather than auditing each call site, secrets are registered here once as they
become known, and the values are masked wherever they appear. Registration is
process-global on purpose: the alternative is threading a secrets list through
every function that might one day log something.
"""

import logging
import threading
from typing import Iterable, Optional, Set

#: How the masked value appears. Carries no prefix of the original: at most two
#: secrets are ever in play, so identifying which one matched is not worth
#: leaking bytes of it into a file the user is about to paste into an issue.
MASK = "***redacted***"

#: Values shorter than this are ignored. A three-character "key" would match
#: half the words in a log line and corrupt it into uselessness.
MIN_SECRET_LENGTH = 8

_secrets: Set[str] = set()
_lock = threading.Lock()


def register_secret(value: Optional[str]) -> None:
    """Record a value that must never appear in output.

    Safe to call repeatedly with the same value, and safe to call with None or
    an empty string, so callers do not need to guard.

    Args:
        value: The credential to mask. Ignored when absent or too short to
            distinguish from ordinary text.
    """
    if not value or len(value) < MIN_SECRET_LENGTH:
        return
    with _lock:
        _secrets.add(value)


def registered_secrets() -> Set[str]:
    """Return a copy of the registered secrets.

    Returns:
        The set of values currently being masked.
    """
    with _lock:
        return set(_secrets)


def clear_secrets() -> None:
    """Forget every registered secret.

    Exists for tests, which would otherwise leak registrations between cases.
    """
    with _lock:
        _secrets.clear()


def redact(text: str, extra: Iterable[str] = ()) -> str:
    """Replace every registered secret in a string with :data:`MASK`.

    Longest values are replaced first, so a secret that contains another
    secret as a substring cannot leave a fragment behind.

    Args:
        text: The string to clean. Non-strings are returned unchanged.
        extra: Additional values to mask for this call only.

    Returns:
        The string with all known secrets masked.
    """
    if not isinstance(text, str) or not text:
        return text

    candidates = registered_secrets()
    candidates.update(s for s in extra if s and len(s) >= MIN_SECRET_LENGTH)

    for secret in sorted(candidates, key=len, reverse=True):
        if secret in text:
            text = text.replace(secret, MASK)
    return text


class SecretFilter(logging.Filter):
    """Logging filter that masks registered secrets in every record.

    Attached to handlers rather than loggers: a filter on a logger is not
    consulted for records that propagate up from its children, which is how
    almost every record in this package arrives.

    The record is rewritten in place. That is destructive, but a record is
    formatted at most once per handler and the masked form is the only form any
    handler should emit.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Mask secrets in the record's message and arguments.

        Args:
            record: The record about to be emitted.

        Returns:
            True, always — this filter cleans records rather than dropping them.
        """
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)

        if isinstance(record.args, dict):
            record.args = {
                k: redact(v) if isinstance(v, str) else v for k, v in record.args.items()
            }
        elif record.args:
            record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)

        return True
