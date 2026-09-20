"""The scheduler — one cron expression, one lock, one tick loop.

``watch`` is driven by a **cron expression** in ``config.yml`` plus a timezone.
Not an interval, not a plist ``StartInterval``, not a ``--interval`` flag: one
stored representation, in one place, that the wizard, the config menu, ``info``
and the tick loop all read. A second field to say the same thing is a second
field to drift.

Three things in here are less obvious than they look:

**The cron arithmetic is done on a naive wall clock.** ``0 9 * * *`` means
09:00 local, including on the two days a year the clocks move. Handing
``croniter`` a timezone-aware base makes it yield 08:00 on the spring-forward
day — an hour early, once a year, with nothing in the output admitting it — so
:func:`next_fire` converts to local wall time, does the arithmetic there, and
attaches the zone afterwards. That is also the only place that can answer what
a schedule pointing into a DST gap should do.

**The lock is reentrant within a process and exclusive across machines.** A
manual ``sync`` and a scheduled fire must not overlap, which means ``sync``
takes the same lock the scheduler takes; but the scheduler runs its tick
*in-process*, so a naive second ``flock`` would deadlock the daemon against
itself. Holding it twice in one process is one sync, and that is what the
counter says.

**The tick loop is a generator, and the work is the caller's.** It yields fire
times; running a pipeline, invalidating caches and writing a ``runs`` row all
belong to ``WatchCommand``. That is what keeps this module free of the pipeline
and the destinations, and what lets the schedule be tested without either.
"""

import difflib
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from croniter import croniter

from living_ink import state

try:  # pragma: no cover - Windows has no fcntl, and nothing here ships there
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

#: The five cron fields, in order, with what each one accepts. Used only to
#: name the offending field when an expression is refused: ``croniter`` reports
#: "[0 9 * * funday] is not acceptable", which tells the user nothing they did
#: not already know.
CRON_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("minute", "0-59"),
    ("hour", "0-23"),
    ("day of month", "1-31"),
    ("month", "1-12, JAN-DEC, or *"),
    ("day of week", "0-7, SUN-SAT, or *"),
)

#: The presets the wizard and the config menu offer. Labelled cron strings
#: rather than a parallel vocabulary of names, so the menu can show each one's
#: expression beside it — which quietly teaches the format to the user who
#: later picks Custom. ``None`` is "off"; it is a member rather than a special
#: case because "not scheduled" is one of the choices on that screen.
SCHEDULE_PRESETS: Tuple[Tuple[str, Optional[str]], ...] = (
    ("Off — sync only when I ask", None),
    ("Every day at 09:00", "0 9 * * *"),
    ("Twice a day, 09:00 and 18:00", "0 9,18 * * *"),
    ("Every hour", "0 * * * *"),
    ("Every Monday at 09:00", "0 9 * * 1"),
)

#: Offered in the timezone picker above the free-text escape hatch.
#: ``zoneinfo.available_timezones()`` has about six hundred entries, and six
#: hundred entries in a picker is not a choice, it is a search problem.
COMMON_TIMEZONES: Tuple[str, ...] = (
    "UTC",
    "Europe/Madrid",
    "Europe/London",
    "Europe/Berlin",
    "America/New_York",
    "America/Chicago",
    "America/Los_Angeles",
    "America/Sao_Paulo",
    "Asia/Tokyo",
    "Asia/Shanghai",
    "Asia/Kolkata",
    "Australia/Sydney",
)

_DAY_NAMES = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")

#: Where the whole-machine locks live, relative to the data directory.
RUN_LOCK_NAME = "sync.lock"
WATCH_LOCK_NAME = "watch.lock"

#: Process-wide reentrancy: resolved lock path to how many times this process
#: currently holds it. A scheduler tick runs its sync in-process, and the sync
#: takes the same lock, so the second acquisition has to be a no-op rather than
#: a deadlock against itself.
_HELD: Dict[str, int] = {}


# ---------------------------------------------------------------------------
# Timezones
# ---------------------------------------------------------------------------


def host_timezone_name() -> str:
    """Return the IANA name of the machine's own timezone.

    ``zoneinfo`` has no "give me the local zone" call on Python 3.10, and
    ``datetime.now().astimezone().tzinfo`` yields an abbreviation like ``CEST``
    that is neither unique nor loadable. So this reads the two places the name
    actually lives, and only then falls back to a fixed offset.

    Returns:
        An IANA zone name such as ``Europe/Madrid``, or ``UTC`` if the host
        will not say. Never an abbreviation.
    """
    candidate = os.environ.get("TZ", "").strip()
    if candidate and validate_timezone(candidate) is None:
        return candidate

    localtime = Path("/etc/localtime")
    try:
        if localtime.is_symlink():
            resolved = localtime.resolve()
            parts = resolved.parts
            if "zoneinfo" in parts:
                name = "/".join(parts[parts.index("zoneinfo") + 1 :])
                if name and validate_timezone(name) is None:
                    return name
    except OSError:  # pragma: no cover - unreadable /etc on a locked-down host
        pass

    return "UTC"


def validate_timezone(name: str) -> Optional[str]:
    """Return a human-readable problem with ``name``, or None if it loads.

    An unknown zone is refused rather than silently treated as UTC: a container
    that reports the wrong zone fires the schedule an hour or two off, all year,
    with nothing in the output admitting it.

    Args:
        name: An IANA timezone name.

    Returns:
        A one-line complaint naming near matches, or None when ``name`` is
        valid.
    """
    if not name or not name.strip():
        return "A timezone name cannot be blank."
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        close = difflib.get_close_matches(name, sorted(available_timezones()), n=3)
        hint = f" Did you mean {', '.join(close)}?" if close else ""
        return f'"{name}" is not a known timezone.{hint}'
    return None


def resolve_timezone(name: Optional[str]) -> Tuple[ZoneInfo, str]:
    """Load the zone a schedule is read in.

    Args:
        name: The configured zone, or None to follow this machine.

    Returns:
        The loaded zone and the name it was loaded under, so a caller can say
        which zone it is using without re-deriving it.

    Raises:
        ValueError: If ``name`` is set and does not name a real zone. Refused
            here rather than at the first fire, because a schedule whose
            mistakes only show up days later is the one setting that most needs
            to fail on entry.
    """
    if name:
        problem = validate_timezone(name)
        if problem:
            raise ValueError(problem)
        return ZoneInfo(name), name

    resolved = host_timezone_name()
    return ZoneInfo(resolved), resolved


# ---------------------------------------------------------------------------
# Cron expressions
# ---------------------------------------------------------------------------


def validate_expression(expression: str) -> Optional[str]:
    """Return a human-readable problem with ``expression``, or None if valid.

    Args:
        expression: A five-field cron expression.

    Returns:
        A one-line complaint that names the field at fault, or None.
    """
    if not expression or not expression.strip():
        return "A schedule cannot be blank."

    fields = expression.split()
    if len(fields) != len(CRON_FIELDS):
        names = ", ".join(name for name, _ in CRON_FIELDS)
        return (
            f"A schedule has {len(CRON_FIELDS)} fields ({names}); "
            f'"{expression.strip()}" has {len(fields)}.'
        )

    if croniter.is_valid(expression):
        return None

    # Which field is wrong: substitute each one into an otherwise-blank
    # expression and see which substitution is the one that stops parsing.
    for index, (value, (name, accepts)) in enumerate(zip(fields, CRON_FIELDS)):
        probe = ["*"] * len(CRON_FIELDS)
        probe[index] = value
        if not croniter.is_valid(" ".join(probe)):
            return f'"{value}" — field {index + 1} ({name}) expects {accepts}.'

    # Every field parses alone but the whole does not — a combination croniter
    # rejects. Say so rather than blaming a field that is fine.
    return f'"{expression.strip()}" is not a schedule this build can read.'


def _attach(moment: datetime, tz: ZoneInfo) -> datetime:
    """Turn a local wall clock into a real instant in ``tz``.

    Two of the three cases are ordinary; the third is the reason this exists.
    A time that occurs twice (autumn) resolves to the first occurrence, which
    is what ``fold=0`` already means. A time that does not exist (spring)
    cannot be represented at all, so the first instant after the gap is used —
    found by bisection, because that instant *is* the transition and nothing in
    ``zoneinfo`` will name it.

    Args:
        moment: A naive local wall clock.
        tz: The zone to read it in.

    Returns:
        An aware datetime whose wall-clock fields are real in ``tz``.
    """
    attached = moment.replace(tzinfo=tz)
    # Round-tripping through an instant reveals a gap: a wall clock inside one
    # comes back as a different wall clock.
    normalized = datetime.fromtimestamp(attached.timestamp(), tz)
    if normalized.replace(tzinfo=None) == moment:
        return attached

    # Everything before the gap has a smaller wall clock and everything after
    # it a larger one, so the first instant whose wall clock reaches ``moment``
    # is the transition itself.
    low = int(attached.timestamp()) - 26 * 3600
    high = int(normalized.timestamp()) + 1
    while low < high:
        middle = (low + high) // 2
        if datetime.fromtimestamp(middle, tz).replace(tzinfo=None) >= moment:
            high = middle
        else:
            low = middle + 1
    return datetime.fromtimestamp(low, tz)


def next_fire(expression: str, after: datetime, tz: ZoneInfo) -> datetime:
    """The next time ``expression`` fires strictly after ``after``, in ``tz``.

    DST is why the timezone is explicit and why fire times are computed in
    local time rather than by adding seconds to a timestamp. ``0 7 * * *``
    means 07:00 local on the day the clocks change too; an interval loop drifts
    by an hour twice a year and the user cannot say why.

    A time that does not exist (spring forward) fires at the first instant
    after the gap. A time that occurs twice (fall back) fires once, on the
    first occurrence.

    Args:
        expression: A valid five-field cron expression.
        after: The moment to search from. Naive values are read as ``tz``.
        tz: The zone the expression is written in.

    Returns:
        An aware datetime in ``tz``.

    Raises:
        ValueError: If ``expression`` is not valid.
    """
    problem = validate_expression(expression)
    if problem:
        raise ValueError(problem)

    local = after.astimezone(tz) if after.tzinfo else after.replace(tzinfo=tz)
    cursor = croniter(expression, local.replace(tzinfo=None))
    return _attach(cursor.get_next(datetime), tz)


def previous_fire(expression: str, before: datetime, tz: ZoneInfo) -> datetime:
    """The most recent time ``expression`` fired at or before ``before``.

    This is the catch-up question in one call: a schedule time that has already
    passed is a reason to sync now, not a reason to wait for tomorrow.

    Args:
        expression: A valid five-field cron expression.
        before: The moment to search back from.
        tz: The zone the expression is written in.

    Returns:
        An aware datetime in ``tz``, strictly at or before ``before``.

    Raises:
        ValueError: If ``expression`` is not valid.
    """
    problem = validate_expression(expression)
    if problem:
        raise ValueError(problem)

    local = before.astimezone(tz) if before.tzinfo else before.replace(tzinfo=tz)
    # One second forward, so a query made exactly on a fire time returns that
    # fire rather than the one before it — the tick that has just come due is
    # the one the caller means.
    cursor = croniter(expression, (local + timedelta(seconds=1)).replace(tzinfo=None))
    return _attach(cursor.get_prev(datetime), tz)


def next_fires(expression: str, after: datetime, tz: ZoneInfo, count: int = 3) -> List[datetime]:
    """The next ``count`` fire times, for confirming a schedule before saving.

    Showing them is the only way a user can tell ``0 9 * * 1`` from
    ``0 9 1 * *`` without already reading cron fluently, which is why the menu
    shows them for the presets too and not only for a custom expression.

    Args:
        expression: A valid five-field cron expression.
        after: The moment to search from.
        tz: The zone the expression is written in.
        count: How many to return.

    Returns:
        Aware datetimes in ``tz``, ascending.

    Raises:
        ValueError: If ``expression`` is not valid.
    """
    fires: List[datetime] = []
    cursor = after
    for _ in range(max(0, count)):
        cursor = next_fire(expression, cursor, tz)
        fires.append(cursor)
    return fires


def _numbers(field: str, limit: int) -> Optional[List[int]]:
    """Read a cron field as a plain list of values, or give up.

    Args:
        field: One cron field.
        limit: One past the largest legal value.

    Returns:
        The values in ascending order, or None if the field uses a form this
        describer does not put into words.
    """
    if field == "*":
        return None
    values: List[int] = []
    for part in field.split(","):
        if not part.isdigit():
            return None
        value = int(part)
        if value >= limit:
            return None
        values.append(value)
    return sorted(set(values))


def describe_expression(expression: str, tz: Optional[ZoneInfo] = None) -> str:
    """Render ``expression`` as English.

    Only the shapes a person plausibly chose are put into words; anything else
    is returned as the expression itself. A describer that guesses at
    ``*/7 3-5 * * 2-4`` and gets it subtly wrong is worse than one that admits
    the expression is the clearest statement available.

    Args:
        expression: A cron expression, valid or not.
        tz: The zone to name in the description, if any.

    Returns:
        A phrase such as ``every day at 09:00 (Europe/Madrid)``.
    """
    suffix = f" ({tz.key})" if tz is not None else ""
    if validate_expression(expression):
        return f"{expression.strip()}{suffix}"

    minute_field, hour_field, dom, month, dow = expression.split()
    minutes = _numbers(minute_field, 60)
    hours = _numbers(hour_field, 24)
    days = _numbers(dow, 8)

    if dom != "*" or month != "*" or minutes is None or len(minutes) != 1:
        return f"{expression.strip()}{suffix}"

    minute = minutes[0]
    if hours is None:
        phrase = "every hour" if minute == 0 else f"every hour at :{minute:02d}"
    else:
        times = ", ".join(f"{hour:02d}:{minute:02d}" for hour in hours)
        phrase = f"every day at {times}"

    if days is not None:
        names = ", ".join(_DAY_NAMES[day % 7] for day in days)
        phrase = phrase.replace("every day", f"every {names}").replace(
            "every hour", f"every hour on {names}"
        )

    return f"{phrase}{suffix}"


# ---------------------------------------------------------------------------
# Saying how long
# ---------------------------------------------------------------------------


def humanize_duration(seconds: float) -> str:
    """Render a span of time the way the schedule lines print it.

    Args:
        seconds: A duration. Negative values are read as zero.

    Returns:
        Something like ``14h 22m``, ``1m 12s`` or ``0.4s``.
    """
    total = max(0.0, float(seconds))
    if total < 10:
        return f"{total:.1f}s"
    total = int(total)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m {total % 60}s"
    if total < 86400:
        return f"{total // 3600}h {(total % 3600) // 60}m"
    return f"{total // 86400}d {(total % 86400) // 3600}h"


def humanize_ago(seconds: float) -> str:
    """Render how long ago something happened, in round units.

    ``info`` answers "is this healthy", and "2 hours ago" answers it where
    "2h 14m ago" makes the reader do arithmetic to find out.

    Args:
        seconds: How long ago, in seconds.

    Returns:
        A phrase such as ``3 days ago`` or ``just now``.
    """
    total = max(0.0, float(seconds))
    if total < 90:
        return "just now"
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if total >= size:
            count = int(total // size)
            return f"{count} {unit}{'' if count == 1 else 's'} ago"
    return "just now"  # pragma: no cover - unreachable; total >= 90 hits minutes


def format_moment(moment: datetime, tz: Optional[tzinfo] = None, *, year: bool = False) -> str:
    """Render an instant the way every schedule line prints one.

    Args:
        moment: An aware datetime.
        tz: Show it in this zone. Left alone when None, which is what the
            schedule's own fire times already are.
        year: Include the year. The wizard's "next three runs" does — it is
            confirming a rule that may not fire for months — and the status
            lines do not, because they are all within a day or two.

    Returns:
        Something like ``Fri 19 Sep 09:00``.
    """
    local = moment.astimezone(tz) if tz else moment
    return local.strftime("%a %d %b %Y, %H:%M" if year else "%a %d %b %H:%M")


def describe_run(row: Mapping[str, Any]) -> str:
    """Say in one phrase what a ``runs`` row amounts to.

    One vocabulary for the two places a run is reported — the ``watch`` summary
    and ``info``'s Watch panel — because "failed" meaning two different things
    in two panels of one product is how a user stops trusting either.

    Args:
        row: A row from the ``runs`` table.

    Returns:
        A phrase such as ``synced 3 documents``, ``nothing to do`` or
        ``failed · tablet unreachable``. Never empty.
    """
    outcome = row.get("outcome")
    published = int(row.get("documents_published") or 0)
    failed = int(row.get("documents_failed") or 0)
    error = (row.get("error") or "").strip()

    def counted(count: int) -> str:
        return f"{count} document{'' if count == 1 else 's'}"

    if outcome is None:
        # Started and never finished: the process was killed mid-run, so no
        # code ever ran to record one. Saying "failed" would blame the sync.
        return "did not finish"
    if outcome == state.OUTCOME_SKIPPED_OVERLAPPING:
        return "skipped · a sync was already running"
    if outcome == state.OUTCOME_NOTHING_TO_DO:
        return "nothing to do"
    if outcome == state.OUTCOME_INTERRUPTED:
        return f"interrupted after {counted(published)}" if published else "interrupted"
    if outcome == state.OUTCOME_SUCCESS:
        return f"synced {counted(published)}" if published else "nothing to do"
    if outcome == state.OUTCOME_PARTIAL:
        part = f"synced {counted(published)}, {failed} failed"
        return f"{part} · {error}" if error else part
    return f"failed · {error}" if error else "failed"


def to_utc_iso(moment: datetime) -> str:
    """Render an instant the way :mod:`living_ink.state` stores one.

    Args:
        moment: An aware datetime.

    Returns:
        An ISO-8601 UTC string with second precision, directly comparable to
        the timestamps in the ``runs`` table.
    """
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_stored(value: Optional[str]) -> Optional[datetime]:
    """Read a timestamp back out of the state database.

    Args:
        value: An ISO-8601 string, or None.

    Returns:
        An aware datetime, or None if there was nothing to read or it could not
        be parsed. Unreadable is folded into absent on purpose: a malformed
        timestamp in one row must not take down the health report that found
        it.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# The lock
# ---------------------------------------------------------------------------


class LockBusy(RuntimeError):
    """Raised when a lock is already held, by this machine or another."""


class RunLock:
    """An advisory whole-machine lock, held for as long as the work runs.

    Two of them exist: one around a sync, so a manual run and a scheduled fire
    cannot write the same note at once, and one around the watcher itself, so a
    second ``living-ink watch`` says so instead of starting a rival scheduler.

    Reentrant **within a process** and exclusive outside it. The scheduler runs
    its tick in-process and the sync takes the same lock, so a second ``flock``
    on a second descriptor would have the daemon deadlock against itself; two
    acquisitions in one process are one sync, and the counter says so.
    """

    def __init__(self, path: Path):
        """Prepare a lock without taking it.

        Args:
            path: The lock file. Created on acquisition; never deleted, because
                unlinking a file another process has open drops the lock
                without anybody noticing.
        """
        self.path = path
        self._key = str(path)
        self._fd: Optional[int] = None

    def acquire(self) -> bool:
        """Take the lock if it is free.

        Returns:
            True if this call now holds it (including a reentrant hold), False
            if another process has it.
        """
        if _HELD.get(self._key):
            _HELD[self._key] += 1
            return True

        if fcntl is None:  # pragma: no cover - not reachable on macOS or Linux
            logger.warning("No file locking on this platform; overlapping runs are possible.")
            _HELD[self._key] = 1
            return True

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False

        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        os.fsync(fd)
        self._fd = fd
        _HELD[self._key] = 1
        return True

    def release(self) -> None:
        """Give the lock back, or drop one level of a reentrant hold."""
        held = _HELD.get(self._key, 0)
        if held > 1:
            _HELD[self._key] = held - 1
            return
        _HELD.pop(self._key, None)
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)  # type: ignore[union-attr]
            except OSError:  # pragma: no cover - the descriptor is going away anyway
                pass
            os.close(self._fd)
            self._fd = None

    def owner(self) -> Optional[int]:
        """Read the process id written into the lock file, if any.

        Returns:
            The pid the holder recorded, or None if the file is missing or
            says something that is not a number. Advisory only — the holder may
            have died between the read and the answer.
        """
        try:
            content = self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return int(content) if content.isdigit() else None

    def __enter__(self) -> "RunLock":
        """Take the lock, or refuse.

        Returns:
            This lock.

        Raises:
            LockBusy: If another process holds it.
        """
        if not self.acquire():
            pid = self.owner()
            where = f" (pid {pid})" if pid else ""
            raise LockBusy(f"Another Living Ink process is already running{where}.")
        return self

    def __exit__(self, *exc_info) -> None:
        """Release the lock however the block ended."""
        self.release()


@contextmanager
def optional_lock(lock: RunLock) -> Iterator[bool]:
    """Run a block with ``lock`` if it is free, and say whether it was.

    The scheduler's answer to a held lock is to record the tick as skipped and
    carry on, not to fail; a context manager that raises would make every
    caller write the same try/except around it.

    Args:
        lock: The lock to attempt.

    Yields:
        True if the block owns the lock, False if it was already held.
    """
    acquired = lock.acquire()
    try:
        yield acquired
    finally:
        if acquired:
            lock.release()


# ---------------------------------------------------------------------------
# The tick loop
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Tick:
    """One firing of the schedule.

    Attributes:
        fire_time: When this tick was due, in the schedule's zone.
        catch_up: Whether it was already overdue when the loop reached it — a
            laptop that was closed at 09:00 and opened at 14:00 gets one of
            these, and exactly one, however many fires it slept through.
    """

    fire_time: datetime
    catch_up: bool


class Schedule:
    """A cron expression, a zone, and the loop that waits on them.

    The loop yields fire times and nothing else. Running a pipeline,
    invalidating the module caches and writing a ``runs`` row all belong to the
    command, which is what keeps this module out of the pipeline's import graph
    and lets the schedule be tested with a fake clock and no tablet.
    """

    def __init__(
        self,
        expression: str,
        tz: ZoneInfo,
        *,
        now: Optional[Callable[[], datetime]] = None,
        sleep: Optional[Callable[[float], None]] = None,
    ):
        """Prepare a schedule.

        Args:
            expression: A valid five-field cron expression.
            tz: The zone it is read in.
            now: Reads the current time. Injected so a test can drive years of
                ticks without waiting for them.
            sleep: Waits. Injected for the same reason. Resolved here rather
                than as a default argument, which would bind ``time.sleep`` at
                import and leave a test that patches it patching nothing.

        Raises:
            ValueError: If ``expression`` is not valid.
        """
        problem = validate_expression(expression)
        if problem:
            raise ValueError(problem)
        self.expression = expression
        self.tz = tz
        self._now = now or (lambda: datetime.now(tz))
        self._sleep = sleep or time.sleep

    def describe(self) -> str:
        """Render the schedule as English.

        Returns:
            A phrase such as ``every day at 09:00 (Europe/Madrid)``.
        """
        return describe_expression(self.expression, self.tz)

    def now(self) -> datetime:
        """The current time, in this schedule's zone.

        Exposed because every line the watcher prints is relative to it, and a
        summary that read the wall clock directly would disagree with the loop
        it is describing the moment a test — or a container — moved one of them.

        Returns:
            An aware datetime.
        """
        return self._now()

    def next_fire(self, after: Optional[datetime] = None) -> datetime:
        """When this schedule next fires.

        Args:
            after: The moment to search from; now, by default.

        Returns:
            An aware datetime in this schedule's zone.
        """
        return next_fire(self.expression, after or self._now(), self.tz)

    def missed_fire(self, satisfied: Callable[[datetime], bool]) -> Optional[datetime]:
        """The overdue fire time, if there is one that nothing has covered.

        A missed schedule produces **one** catch-up run, not five: five days of
        missed daily runs are still one useful sync, because a sync reconciles
        what is on the tablet now rather than replaying history. So only the
        most recent fire time is considered, and only when nothing has run
        since it.

        Args:
            satisfied: Answers whether a run has already covered a fire time.

        Returns:
            The fire time to catch up on, or None if the last one is covered.
        """
        previous = previous_fire(self.expression, self._now(), self.tz)
        return None if satisfied(previous) else previous

    def ticks(self, satisfied: Callable[[datetime], bool]) -> Iterator[Tick]:
        """Yield fire times forever, sleeping in between.

        Args:
            satisfied: Answers whether a run has already covered a fire time.
                Consulted once, for the catch-up decision: after that the loop
                is the only thing firing, so asking again would only let a
                caller that records nothing spin on the same instant.

        Yields:
            One :class:`Tick` per firing.

        Raises:
            KeyboardInterrupt: Straight out of the sleep, never converted. A
                supervisor reading exit 0 treats a user-initiated stop as a
                clean finish and starts the daemon straight back up.
        """
        served: Optional[datetime] = None

        missed = self.missed_fire(satisfied)
        if missed is not None:
            served = missed
            yield Tick(fire_time=missed, catch_up=True)

        while True:
            # Searched from whichever is later, now or the fire time already
            # served. Both matter: normally the clock has moved past it, but a
            # tick that finished within the same second would otherwise be
            # handed its own fire time back and run twice.
            moment = self._now()
            reference = served if served is not None and served > moment else moment
            target = self.next_fire(reference)
            delay = (target - self._now()).total_seconds()
            if delay > 0:
                self._sleep(delay)
            served = target
            yield Tick(fire_time=target, catch_up=False)
