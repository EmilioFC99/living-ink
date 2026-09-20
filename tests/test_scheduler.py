"""Tests for the cron arithmetic, the lock and the tick loop.

No pipeline, no tablet and no waiting: :class:`~living_ink.scheduler.Schedule`
takes its clock and its sleep as arguments precisely so a year of ticks costs a
millisecond, and the DST cases — the ones worth testing — are a statement about
a calendar rather than about a sync.
"""

import os
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from living_ink import scheduler, state

MADRID = ZoneInfo("Europe/Madrid")


@pytest.fixture(autouse=True)
def forget_held_locks():
    """Clear the process-wide reentrancy counter between tests.

    It is module state by design — two acquisitions in one process are one
    sync — so a test that leaves a count behind makes the next one pass for
    the wrong reason.
    """
    yield
    scheduler._HELD.clear()


class TestNamingTheZone:
    """An unknown zone is refused, never quietly read as UTC."""

    def test_a_valid_name_has_no_complaint(self):
        assert scheduler.validate_timezone("Europe/Madrid") is None

    def test_a_blank_name_is_refused(self):
        assert scheduler.validate_timezone("  ") == "A timezone name cannot be blank."

    def test_a_typo_is_told_what_it_nearly_said(self):
        problem = scheduler.validate_timezone("Europe/Madridd")
        assert "not a known timezone" in problem
        assert "Europe/Madrid" in problem

    def test_resolving_a_name_returns_the_zone_and_the_name(self):
        tz, name = scheduler.resolve_timezone("Asia/Tokyo")
        assert (tz.key, name) == ("Asia/Tokyo", "Asia/Tokyo")

    def test_no_name_follows_the_host(self, monkeypatch):
        monkeypatch.setenv("TZ", "America/Chicago")
        assert scheduler.resolve_timezone(None) == (ZoneInfo("America/Chicago"), "America/Chicago")

    def test_an_unknown_name_raises_rather_than_waiting_for_the_first_fire(self):
        with pytest.raises(ValueError, match="not a known timezone"):
            scheduler.resolve_timezone("Mars/Olympus_Mons")

    def test_the_host_zone_is_never_an_abbreviation(self, monkeypatch):
        """``CEST`` is neither unique nor loadable, so it is never returned."""
        monkeypatch.setenv("TZ", "CEST")
        assert ZoneInfo(scheduler.host_timezone_name())

    def test_an_unset_host_zone_falls_back_to_utc(self, monkeypatch, tmp_path):
        monkeypatch.delenv("TZ", raising=False)
        monkeypatch.setattr(scheduler, "Path", lambda _: tmp_path / "missing")
        assert scheduler.host_timezone_name() == "UTC"


class TestReadingAnExpression:
    """A refusal names the field at fault, because croniter does not."""

    def test_a_valid_expression_has_no_complaint(self):
        assert scheduler.validate_expression("0 9 * * 1") is None

    def test_a_blank_expression_is_refused(self):
        assert scheduler.validate_expression("") == "A schedule cannot be blank."

    def test_the_wrong_number_of_fields_says_how_many_there_are(self):
        problem = scheduler.validate_expression("0 9 * *")
        assert "has 4" in problem and "5 fields" in problem

    def test_the_offending_field_is_named(self):
        problem = scheduler.validate_expression("0 9 * * funday")
        assert "field 5 (day of week)" in problem
        assert "funday" in problem

    def test_an_out_of_range_value_is_named_too(self):
        assert "field 2 (hour)" in scheduler.validate_expression("0 99 * * *")

    def test_every_preset_is_valid(self):
        for _label, expression in scheduler.SCHEDULE_PRESETS:
            if expression is not None:
                assert scheduler.validate_expression(expression) is None

    def test_exactly_one_preset_means_off(self):
        assert [e for _label, e in scheduler.SCHEDULE_PRESETS].count(None) == 1


class TestWhenItFires:
    """The arithmetic is done on a wall clock, which is what DST needs."""

    def test_the_next_fire_is_strictly_after_the_moment_asked_about(self):
        nine = datetime(2026, 9, 21, 9, 0, tzinfo=MADRID)
        assert scheduler.next_fire("0 9 * * *", nine, MADRID) == nine + timedelta(days=1)

    def test_a_naive_moment_is_read_as_the_schedule_s_own_zone(self):
        naive = datetime(2026, 9, 21, 8, 0)
        assert scheduler.next_fire("0 9 * * *", naive, MADRID) == datetime(
            2026, 9, 21, 9, 0, tzinfo=MADRID
        )

    def test_a_moment_in_another_zone_is_converted_first(self):
        eight_utc = datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc)  # 10:00 in Madrid
        assert scheduler.next_fire("0 9 * * *", eight_utc, MADRID) == datetime(
            2026, 9, 22, 9, 0, tzinfo=MADRID
        )

    def test_nine_is_nine_on_the_day_the_clocks_go_forward(self):
        """The bug this guards: a timezone-aware base makes croniter yield
        08:00 on the spring day — an hour early, once a year, silently."""
        before = datetime(2026, 3, 28, 12, 0, tzinfo=MADRID)
        fire = scheduler.next_fire("0 9 * * *", before, MADRID)
        assert (fire.hour, fire.utcoffset()) == (9, timedelta(hours=2))

    def test_a_time_that_does_not_exist_fires_when_the_gap_ends(self):
        before = datetime(2026, 3, 28, 12, 0, tzinfo=MADRID)
        assert scheduler.next_fire("0 2 * * *", before, MADRID) == datetime(
            2026, 3, 29, 3, 0, tzinfo=MADRID
        )

    def test_a_time_that_happens_twice_fires_on_the_first_one(self):
        before = datetime(2026, 10, 24, 12, 0, tzinfo=MADRID)
        fire = scheduler.next_fire("0 2 * * *", before, MADRID)
        assert (fire.hour, fire.utcoffset()) == (2, timedelta(hours=2))

    def test_the_repeated_hour_is_not_fired_twice(self):
        first = scheduler.next_fire(
            "0 2 * * *", datetime(2026, 10, 24, 12, 0, tzinfo=MADRID), MADRID
        )
        assert scheduler.next_fire("0 2 * * *", first, MADRID).day == 26

    def test_the_previous_fire_is_the_one_that_has_already_passed(self):
        assert scheduler.previous_fire(
            "0 9 * * *", datetime(2026, 9, 19, 14, 0, tzinfo=MADRID), MADRID
        ) == datetime(2026, 9, 19, 9, 0, tzinfo=MADRID)

    def test_asking_exactly_on_a_fire_time_returns_that_fire(self):
        """The tick that has just come due is the one the caller means."""
        nine = datetime(2026, 9, 19, 9, 0, tzinfo=MADRID)
        assert scheduler.previous_fire("0 9 * * *", nine, MADRID) == nine

    def test_the_next_three_fires_are_ascending(self):
        fires = scheduler.next_fires(
            "0 9 * * 1", datetime(2026, 9, 19, 14, 0, tzinfo=MADRID), MADRID, count=3
        )
        assert [f.strftime("%a %d") for f in fires] == ["Mon 21", "Mon 28", "Mon 05"]

    def test_asking_for_none_returns_none(self):
        assert scheduler.next_fires("0 9 * * *", datetime.now(MADRID), MADRID, count=0) == []

    def test_an_invalid_expression_raises_rather_than_guessing(self):
        with pytest.raises(ValueError, match="day of week"):
            scheduler.next_fire("0 9 * * funday", datetime.now(MADRID), MADRID)


class TestSayingItInEnglish:
    """Only the shapes a person plausibly chose are put into words."""

    @pytest.mark.parametrize(
        "expression, expected",
        [
            ("0 9 * * *", "every day at 09:00"),
            ("0 9,18 * * *", "every day at 09:00, 18:00"),
            ("0 * * * *", "every hour"),
            ("30 * * * *", "every hour at :30"),
            ("0 9 * * 1", "every Monday at 09:00"),
            ("0 9 * * 0", "every Sunday at 09:00"),
        ],
    )
    def test_the_shapes_it_knows(self, expression, expected):
        assert scheduler.describe_expression(expression) == expected

    def test_the_zone_is_named_when_there_is_one(self):
        assert scheduler.describe_expression("0 9 * * *", MADRID).endswith(" (Europe/Madrid)")

    @pytest.mark.parametrize("expression", ["*/7 3-5 * * 2-4", "0 9 1 * *", "0 9 * 3 *"])
    def test_anything_else_is_returned_as_itself(self, expression):
        """A describer that guesses and gets it subtly wrong is worse than one
        that admits the expression is the clearest statement available."""
        assert scheduler.describe_expression(expression) == expression

    def test_an_invalid_expression_is_returned_rather_than_raising(self):
        assert scheduler.describe_expression("nonsense") == "nonsense"


class TestSayingHowLong:
    """One vocabulary for durations, shared by ``watch`` and ``info``."""

    @pytest.mark.parametrize(
        "seconds, expected",
        [
            (0.4, "0.4s"),
            (-5, "0.0s"),
            (42, "42s"),
            (75, "1m 15s"),
            (3700, "1h 1m"),
            (200000, "2d 7h"),
        ],
    )
    def test_durations(self, seconds, expected):
        assert scheduler.humanize_duration(seconds) == expected

    @pytest.mark.parametrize(
        "seconds, expected",
        [
            (0, "just now"),
            (89, "just now"),
            (120, "2 minutes ago"),
            (3600, "1 hour ago"),
            (259200, "3 days ago"),
        ],
    )
    def test_how_long_ago(self, seconds, expected):
        assert scheduler.humanize_ago(seconds) == expected

    def test_a_moment_without_its_year(self):
        assert (
            scheduler.format_moment(datetime(2026, 9, 19, 9, 0, tzinfo=MADRID))
            == "Sat 19 Sep 09:00"
        )

    def test_a_moment_with_its_year(self):
        """The wizard's confirmation shows it: a rule that may not fire for
        months is not confirmed by a weekday and a day number."""
        assert (
            scheduler.format_moment(datetime(2026, 9, 19, 9, 0, tzinfo=MADRID), year=True)
            == "Sat 19 Sep 2026, 09:00"
        )

    def test_a_moment_is_shown_in_the_zone_asked_for(self):
        utc_nine = datetime(2026, 9, 19, 9, 0, tzinfo=timezone.utc)
        assert scheduler.format_moment(utc_nine, MADRID) == "Sat 19 Sep 11:00"


class TestSayingWhatARunDid:
    """One vocabulary for outcomes, so two panels cannot disagree."""

    @pytest.mark.parametrize(
        "row, expected",
        [
            ({"outcome": None}, "did not finish"),
            ({"outcome": state.OUTCOME_NOTHING_TO_DO}, "nothing to do"),
            (
                {"outcome": state.OUTCOME_SKIPPED_OVERLAPPING},
                "skipped · a sync was already running",
            ),
            ({"outcome": state.OUTCOME_SUCCESS, "documents_published": 3}, "synced 3 documents"),
            ({"outcome": state.OUTCOME_SUCCESS, "documents_published": 1}, "synced 1 document"),
            ({"outcome": state.OUTCOME_SUCCESS}, "nothing to do"),
            (
                {"outcome": state.OUTCOME_INTERRUPTED, "documents_published": 2},
                "interrupted after 2 documents",
            ),
            ({"outcome": state.OUTCOME_INTERRUPTED}, "interrupted"),
            (
                {"outcome": state.OUTCOME_ERROR, "error": "tablet unreachable"},
                "failed · tablet unreachable",
            ),
            ({"outcome": state.OUTCOME_ERROR}, "failed"),
        ],
    )
    def test_each_outcome_reads_as_itself(self, row, expected):
        assert scheduler.describe_run(row) == expected

    def test_a_partial_run_says_both_halves(self):
        row = {
            "outcome": state.OUTCOME_PARTIAL,
            "documents_published": 4,
            "documents_failed": 1,
            "error": "Notes: rate limited",
        }
        assert scheduler.describe_run(row) == "synced 4 documents, 1 failed · Notes: rate limited"

    def test_a_finished_run_always_says_something(self):
        assert scheduler.describe_run({}) == "did not finish"


class TestTimestampsInTheDatabase:
    """One spelling for an instant, so a comparison is a string comparison."""

    def test_an_instant_round_trips(self):
        moment = datetime(2026, 9, 19, 9, 0, tzinfo=MADRID)
        assert scheduler.parse_stored(scheduler.to_utc_iso(moment)) == moment

    def test_it_is_stored_in_utc_whatever_zone_it_came_from(self):
        assert (
            scheduler.to_utc_iso(datetime(2026, 9, 19, 9, 0, tzinfo=MADRID))
            == "2026-09-19T07:00:00+00:00"
        )

    def test_nothing_stored_reads_back_as_nothing(self):
        assert scheduler.parse_stored(None) is None
        assert scheduler.parse_stored("") is None

    def test_an_unreadable_timestamp_is_absent_rather_than_fatal(self):
        """One malformed row must not take down the health report that found it."""
        assert scheduler.parse_stored("last tuesday") is None

    def test_a_naive_timestamp_is_read_as_utc(self):
        """That is what this module writes, so that is how it is read back."""
        assert scheduler.parse_stored("2026-09-19T07:00:00") == datetime(
            2026, 9, 19, 7, 0, tzinfo=timezone.utc
        )


class TestTheLock:
    """One sync per machine, and a scheduler that does not deadlock itself."""

    def test_acquiring_a_free_lock_works(self, tmp_path):
        lock = scheduler.RunLock(tmp_path / "sync.lock")
        assert lock.acquire() is True
        lock.release()

    def test_the_lock_file_is_created_with_its_directory(self, tmp_path):
        lock = scheduler.RunLock(tmp_path / "deep" / "sync.lock")
        lock.acquire()
        try:
            assert lock.path.exists()
        finally:
            lock.release()

    def test_it_records_the_pid_that_holds_it(self, tmp_path):
        lock = scheduler.RunLock(tmp_path / "sync.lock")
        with lock:
            assert lock.owner() == os.getpid()

    def test_an_absent_lock_file_has_no_owner(self, tmp_path):
        assert scheduler.RunLock(tmp_path / "sync.lock").owner() is None

    def test_holding_it_twice_in_one_process_is_one_sync(self, tmp_path):
        """A watch tick runs its sync in-process, and the sync takes the same
        lock; a second flock on a second descriptor would deadlock the daemon
        against itself."""
        first = scheduler.RunLock(tmp_path / "sync.lock")
        second = scheduler.RunLock(tmp_path / "sync.lock")
        with first:
            assert second.acquire() is True
            second.release()
            assert scheduler._HELD[str(first.path)] == 1

    def test_releasing_the_inner_hold_does_not_free_the_lock(self, tmp_path):
        outer = scheduler.RunLock(tmp_path / "sync.lock")
        inner = scheduler.RunLock(tmp_path / "sync.lock")
        outer.acquire()
        inner.acquire()
        inner.release()
        try:
            assert scheduler._HELD[str(outer.path)] == 1
        finally:
            outer.release()
        assert str(outer.path) not in scheduler._HELD

    def test_another_process_cannot_take_it(self, tmp_path):
        """The one thing the lock is for, and the one thing a same-process
        test cannot show: reentrancy would report success either way."""
        path = tmp_path / "sync.lock"
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                textwrap.dedent(
                    f"""
                    import sys
                    from pathlib import Path
                    from living_ink.scheduler import RunLock
                    lock = RunLock(Path({str(path)!r}))
                    assert lock.acquire()
                    print("held", flush=True)
                    sys.stdin.readline()
                    lock.release()
                    """
                ),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert child.stdout.readline().strip() == "held"
            assert scheduler.RunLock(path).acquire() is False
            with pytest.raises(scheduler.LockBusy, match=f"pid {child.pid}"):
                with scheduler.RunLock(path):
                    pass
        finally:
            child.stdin.write("\n")
            child.stdin.close()
            child.wait(timeout=10)

        assert scheduler.RunLock(path).acquire() is True

    def test_the_optional_lock_says_it_did_not_get_it(self, tmp_path):
        """The scheduler's answer to a held lock is to record the tick as
        skipped and carry on, not to raise."""
        path = tmp_path / "sync.lock"
        holder = scheduler.RunLock(path)
        holder.acquire()
        # A held count the contender cannot see, which is what another process
        # looks like from here.
        scheduler._HELD.clear()
        try:
            with scheduler.optional_lock(scheduler.RunLock(path)) as mine:
                assert mine is False
        finally:
            scheduler._HELD[str(path)] = 1
            holder.release()

    def test_the_optional_lock_releases_what_it_took(self, tmp_path):
        path = tmp_path / "sync.lock"
        with scheduler.optional_lock(scheduler.RunLock(path)) as mine:
            assert mine is True
        assert str(path) not in scheduler._HELD

    def test_it_is_released_even_when_the_block_raises(self, tmp_path):
        path = tmp_path / "sync.lock"
        with pytest.raises(RuntimeError):
            with scheduler.RunLock(path):
                raise RuntimeError("boom")
        assert scheduler.RunLock(path).acquire() is True


class Clock:
    """A wall clock a test moves by hand.

    Attributes:
        now: The current instant.
        slept: Every delay that was waited on, in order.
    """

    def __init__(self, start: datetime):
        """Start the clock.

        Args:
            start: The instant it reads before anything sleeps.
        """
        self.moment = start
        self.slept: list = []

    def __call__(self) -> datetime:
        """Read the clock.

        Returns:
            The current instant.
        """
        return self.moment

    def sleep(self, seconds: float) -> None:
        """Wait, instantly.

        Args:
            seconds: How long the caller believes it waited.
        """
        self.slept.append(seconds)
        self.moment += timedelta(seconds=seconds)


def take(iterator, count: int) -> list:
    """Pull ``count`` items out of an endless iterator.

    Args:
        iterator: The iterator to drain.
        count: How many to take.

    Returns:
        The items taken.
    """
    return [next(iterator) for _ in range(count)]


class TestTheTickLoop:
    """It yields fire times and sleeps; the work belongs to the caller."""

    def schedule(self, expression="0 9 * * *", start=datetime(2026, 9, 19, 8, 0, tzinfo=MADRID)):
        """Build a schedule on a clock this test drives.

        Args:
            expression: The cron expression.
            start: What the clock reads before the first sleep.

        Returns:
            The schedule and its clock.
        """
        clock = Clock(start)
        return (
            scheduler.Schedule(expression, MADRID, now=clock, sleep=clock.sleep),
            clock,
        )

    def test_an_invalid_expression_is_refused_on_construction(self):
        with pytest.raises(ValueError, match="day of week"):
            scheduler.Schedule("0 9 * * funday", MADRID)

    def test_it_describes_itself_with_its_zone(self):
        schedule, _clock = self.schedule()
        assert schedule.describe() == "every day at 09:00 (Europe/Madrid)"

    def test_the_schedule_reads_its_own_clock(self):
        schedule, clock = self.schedule()
        assert schedule.now() == clock.moment

    def test_it_sleeps_until_the_fire_time_and_yields_it(self):
        schedule, clock = self.schedule()
        ticks = take(schedule.ticks(lambda _fire: True), 1)

        assert ticks[0].fire_time == datetime(2026, 9, 19, 9, 0, tzinfo=MADRID)
        assert clock.slept == [3600]

    def test_it_keeps_firing_once_a_day(self):
        schedule, _clock = self.schedule()
        ticks = take(schedule.ticks(lambda _fire: True), 3)

        assert [tick.fire_time.day for tick in ticks] == [19, 20, 21]

    def test_a_tick_that_finished_within_the_second_is_not_served_twice(self):
        """The clock has not moved past the fire time, so searching from `now`
        alone would hand the loop its own fire time straight back."""
        schedule, clock = self.schedule()
        ticks = iter(schedule.ticks(lambda _fire: True))
        first = next(ticks)
        clock.moment = first.fire_time  # the sync took no time at all
        assert next(ticks).fire_time == first.fire_time + timedelta(days=1)

    def test_a_missed_fire_produces_one_catch_up_first(self):
        schedule, _clock = self.schedule(start=datetime(2026, 9, 19, 14, 0, tzinfo=MADRID))
        ticks = take(schedule.ticks(lambda _fire: False), 1)

        assert ticks[0] == scheduler.Tick(
            fire_time=datetime(2026, 9, 19, 9, 0, tzinfo=MADRID), catch_up=True
        )

    def test_five_missed_days_are_still_one_catch_up(self):
        """A sync reconciles what is on the tablet now; it does not replay
        history, so five missed dailies are one useful run."""
        schedule, _clock = self.schedule(start=datetime(2026, 9, 24, 14, 0, tzinfo=MADRID))
        ticks = take(schedule.ticks(lambda _fire: False), 3)

        assert [tick.catch_up for tick in ticks] == [True, False, False]
        assert [tick.fire_time.day for tick in ticks] == [24, 25, 26]

    def test_a_covered_fire_time_produces_no_catch_up(self):
        schedule, _clock = self.schedule(start=datetime(2026, 9, 19, 14, 0, tzinfo=MADRID))
        ticks = take(schedule.ticks(lambda _fire: True), 1)

        assert ticks[0].catch_up is False
        assert ticks[0].fire_time.day == 20

    def test_the_catch_up_question_is_asked_once(self):
        """Asked again per tick, a caller that records nothing would spin on
        the same instant forever."""
        asked = []

        def satisfied(fire):
            asked.append(fire)
            return True

        schedule, _clock = self.schedule()
        take(schedule.ticks(satisfied), 3)
        assert len(asked) == 1

    def test_the_missed_fire_is_reported_on_its_own(self):
        schedule, _clock = self.schedule(start=datetime(2026, 9, 19, 14, 0, tzinfo=MADRID))
        assert schedule.missed_fire(lambda _fire: False) == datetime(
            2026, 9, 19, 9, 0, tzinfo=MADRID
        )
        assert schedule.missed_fire(lambda _fire: True) is None

    def test_it_does_not_drift_across_the_spring_transition(self):
        """An interval loop is an hour out for six months after this day, and
        nothing in its output admits it."""
        schedule, _clock = self.schedule(start=datetime(2026, 3, 27, 12, 0, tzinfo=MADRID))
        ticks = take(schedule.ticks(lambda _fire: True), 2)

        assert [tick.fire_time.hour for tick in ticks] == [9, 9]
        assert [tick.fire_time.utcoffset() for tick in ticks] == [
            timedelta(hours=1),
            timedelta(hours=2),
        ]
