"""``living-ink watch`` — sync on the schedule, forever.

The schedule is a cron expression in ``config.yml``; this command does not
configure it and does not manage a daemon, because it *is* the thing being
managed. :mod:`living_ink.scheduler` owns the arithmetic and the locks, the
pipeline owns the work, and what is left here is the order the two happen in
and what the user sees while they do.
"""

import argparse
import json
import logging
import sys
from datetime import datetime, tzinfo
from typing import Callable, Optional

from living_ink import scheduler, state
from living_ink.cli.base import BaseCommand
from living_ink.cli.commands.sync import SyncCommand
from living_ink.cli.flags import flag_values, register_settings_flags
from living_ink.config import ConfigurationMissing, get_config_path
from living_ink.settings import Settings

logger = logging.getLogger(__name__)


class WatchCommand(BaseCommand):
    """Run the sync pipeline on the configured schedule until stopped."""

    name = "watch"
    help = "Sync on the configured schedule"
    description = (
        "Sync on the schedule set in config.yml, until stopped. "
        "Takes no behaviour flags: a supervised process is restarted without its "
        "arguments, so a flag would stop applying without saying so. Use "
        "'living-ink config' to change what a scheduled run does."
    )

    @classmethod
    def register_args(cls, parser: argparse.ArgumentParser) -> None:
        """Register the output flags, and nothing else.

        Args:
            parser: Subparser to attach arguments to.
        """
        register_settings_flags(parser, cls.name)

    def run(self, args: argparse.Namespace) -> int:
        """Print the watcher's status, then sync on the schedule until stopped.

        Args:
            args: Parsed arguments — the output flags, and nothing else.

        Returns:
            0 if watching is off (nothing to do is not a failure), 1 if the
            schedule is unusable or a second watcher is already running. There
            is no successful return from the loop itself: the only way out is
            Ctrl+C.

        Raises:
            KeyboardInterrupt: When the user stops the watch, so ``main`` exits
                130. A supervisor reads 0 as "the job finished" and starts the
                daemon straight back up, which is what would make stopping a
                watch impossible.
        """
        try:
            settings = self._read_settings(args)
        except ConfigurationMissing as e:
            return self._report_config_problem(e, e.hint)

        if not settings.watch_enabled:
            print()
            print("  Watching is off. Turn it on with: living-ink config → Watch")
            print()
            return 0

        try:
            schedule = self._build_schedule(settings)
        except ValueError as e:
            return self._report_config_problem(
                e, "Run 'living-ink config' → Watch to set a valid schedule."
            )

        from living_ink.pipeline import DATA_DIR

        guard = scheduler.RunLock(DATA_DIR / scheduler.WATCH_LOCK_NAME)
        if not guard.acquire():
            owner = guard.owner()
            where = f" (pid {owner})" if owner else ""
            print(f"Another watcher is already running{where}.", file=sys.stderr)
            print("Fix: stop that one first — there is one watcher per machine.", file=sys.stderr)
            return 1
        try:
            return self._watch(args, settings, schedule)
        finally:
            guard.release()

    # --- setting up -------------------------------------------------------

    def _read_settings(self, args: argparse.Namespace) -> Settings:
        """Resolve this run's settings from the config file and the flags.

        Read again after every tick, not once at startup: a daemon that ran
        for six weeks on the config it saw at login is the trap ``reset_caches``
        exists to avoid, and the schedule itself is part of what can change.

        Args:
            args: Parsed arguments, supplying the flags layer.

        Returns:
            The resolved settings.

        Raises:
            ConfigurationMissing: If the config file cannot be loaded.
        """
        from living_ink.pipeline import load_yaml_config

        cfg_path = get_config_path(self.root)
        raw = load_yaml_config(cfg_path if cfg_path.exists() else None)
        return Settings.resolve(raw, flags=flag_values(args, self.name), config_path=cfg_path)

    @staticmethod
    def _build_schedule(settings: Settings) -> scheduler.Schedule:
        """Turn the two watch settings into a schedule.

        Args:
            settings: The resolved settings.

        Returns:
            The schedule to run.

        Raises:
            ValueError: If the expression is missing or invalid, or the
                timezone is not a name ``zoneinfo`` knows.
        """
        expression = (settings.watch_schedule or "").strip()
        if not expression:
            raise ValueError("Watching is on but no schedule is set (watch.schedule).")
        tz, _name = scheduler.resolve_timezone(settings.watch_timezone)
        return scheduler.Schedule(expression, tz)

    @staticmethod
    def _report_config_problem(error: Exception, hint: str) -> int:
        """Say that the watcher cannot start, and what would fix it.

        Args:
            error: What went wrong.
            hint: The one thing to do about it.

        Returns:
            1, always.
        """
        print(f"Configuration problem: {error}", file=sys.stderr)
        print(f"Fix: {hint}", file=sys.stderr)
        return 1

    # --- the loop ---------------------------------------------------------

    def _watch(
        self,
        args: argparse.Namespace,
        settings: Settings,
        schedule: scheduler.Schedule,
    ) -> int:
        """Tick until interrupted, reloading the schedule when it changes.

        The state store is opened once and held: ``_ADDED_COLUMNS`` runs on
        every database open, so reopening it per tick would pay an
        ALTER-TABLE probe every night for nothing.

        Args:
            args: Parsed arguments.
            settings: The settings the schedule was built from.
            schedule: The schedule to run.

        Returns:
            1 if a configuration failure ended the watch, 0 if watching was
            turned off while it ran.

        Raises:
            KeyboardInterrupt: Straight out of the sleep.
        """
        from living_ink.pipeline import get_state_store

        store = get_state_store()
        syncer = SyncCommand(root=self.root)
        # A watch is unattended by definition, so never stop to offer the wizard.
        syncer.offer_setup_on_missing_config = False
        as_json = bool(getattr(args, "output_json", False)) or settings.output_json

        if not as_json:
            self._print_summary(schedule, store)

        covered = self._coverage(store)
        current = (schedule.expression, str(schedule.tz))
        while True:
            for tick in schedule.ticks(covered):
                failure = self._run_tick(tick, args, syncer, store, as_json)
                if failure is not None:
                    return failure

                settings = self._reread(args) or settings
                if not settings.watch_enabled:
                    if not as_json:
                        print("\n  Watching was turned off. Stopping.\n")
                    return 0
                try:
                    replacement = self._build_schedule(settings)
                except ValueError as e:
                    logger.warning("Keeping the running schedule: %s", e)
                    continue
                if (replacement.expression, str(replacement.tz)) == current:
                    continue
                schedule = replacement
                current = (schedule.expression, str(schedule.tz))
                if not as_json:
                    print(f"\n  Schedule changed — now {schedule.describe()}.\n")
                break

    @staticmethod
    def _coverage(store: "state.StateStore") -> Callable[[datetime], bool]:
        """Build the catch-up question: has anything run since this fire time?

        Any run counts, not only a scheduled one. A manual ``sync`` ten minutes
        ago has already reconciled the tablet, so replaying this morning's
        missed 09:00 on top of it would be a listing and a lock for nothing.

        Args:
            store: The open state store.

        Returns:
            A predicate over fire times.
        """

        def covered(fire: datetime) -> bool:
            return store.runs_since(scheduler.to_utc_iso(fire)) > 0

        return covered

    def _reread(self, args: argparse.Namespace) -> Optional[Settings]:
        """Re-read the settings, treating an unreadable config as no change.

        A config saved half-written, or one the user is mid-edit on, must not
        take the daemon down: the run itself will report the problem properly
        on the next tick, and until then the schedule that was working keeps
        working.

        Args:
            args: Parsed arguments, supplying the flags layer.

        Returns:
            The new settings, or None if they could not be read.
        """
        try:
            return self._read_settings(args)
        except Exception:
            logger.warning("Could not re-read the configuration; keeping the current one.")
            return None

    def _run_tick(
        self,
        tick: scheduler.Tick,
        args: argparse.Namespace,
        syncer: SyncCommand,
        store: "state.StateStore",
        as_json: bool,
    ) -> Optional[int]:
        """Do one firing: take the lock, sync, and report what happened.

        Args:
            tick: The firing that came due.
            args: Parsed arguments, forwarded to the sync.
            syncer: The command that builds and runs the pipeline.
            store: The open state store.
            as_json: Print one JSON object instead of one line.

        Returns:
            None to keep watching, or an exit code to stop. A failed tick — the
            tablet asleep, the network down, the provider rate-limiting — keeps
            watching, because tomorrow's run is likely to succeed. A
            configuration failure stops, because every future tick would fail
            identically.

        Raises:
            KeyboardInterrupt: Propagated, never converted.
        """
        fired_at = scheduler.to_utc_iso(tick.fire_time)

        from living_ink.pipeline import DATA_DIR, reset_caches

        lock = scheduler.RunLock(DATA_DIR / scheduler.RUN_LOCK_NAME)
        with scheduler.optional_lock(lock) as mine:
            if not mine:
                # Skipped, not queued: two syncs writing one vault can
                # double-write a note, and a sync already running will pick up
                # whatever this one would have.
                run_id = store.start_run(
                    trigger=state.TRIGGER_SCHEDULED, scheduled_fire_time=fired_at
                )
                store.finish_run(run_id, outcome=state.OUTCOME_SKIPPED_OVERLAPPING)
                self._report_tick(tick, store, as_json)
                return None

            # Before the run, not after: a config change is only picked up by
            # the run that follows it, and the destinations are mutable objects
            # one tick must not hand to the next. The open database survives —
            # nothing in config.yml moves it.
            reset_caches(keep_state_store=True)

            try:
                syncer.execute_sync(
                    args,
                    trigger=state.TRIGGER_SCHEDULED,
                    scheduled_fire_time=fired_at,
                )
            except ConfigurationMissing as e:
                print(f"\nConfiguration error: {e}", file=sys.stderr)
                print(e.hint, file=sys.stderr)
                return 1
            except KeyboardInterrupt:
                raise
            except Exception as e:
                # Deliberately broad. A daemon that exits on the first
                # unexpected error is a daemon that is not running when the
                # user needs it; the traceback goes to the log file instead.
                logger.warning("Scheduled sync failed", exc_info=True)
                logger.debug("Tick %s failed: %s", fired_at, e)

        self._report_tick(tick, store, as_json)
        return None

    # --- what it prints ---------------------------------------------------

    def _print_summary(self, schedule: scheduler.Schedule, store: "state.StateStore") -> None:
        """Print the whole status of the watcher, then say how to stop it.

        Started once, at the top, so the answer to "is this thing working?" is
        on screen the moment the watcher starts rather than needing a second
        command.

        Args:
            schedule: The schedule about to run.
            store: The open state store.
        """
        now = schedule.now()
        upcoming = schedule.next_fire(now)
        recent = store.recent_runs(5)

        print()
        print("  Living Ink · watching")
        print(f"  Schedule     {schedule.describe()}")

        last = store.last_run()
        if last:
            print(f"  Last run     {self._run_line(last, schedule.tz)}")
        else:
            print("  Last run     never")

        ahead = scheduler.humanize_duration((upcoming - now).total_seconds())
        print(f"  Next run     {scheduler.format_moment(upcoming)}, in {ahead}")

        if recent:
            print()
            print("  Recent runs")
            for row in recent:
                print(f"    {self._run_line(row, schedule.tz)}")

        print()
        print("  Ctrl+C to stop.")
        print()

    def _report_tick(self, tick: scheduler.Tick, store: "state.StateStore", as_json: bool) -> None:
        """Say what the tick that just finished did.

        Args:
            tick: The firing that ran.
            store: The open state store, holding the row the run just wrote.
            as_json: Print one JSON object instead of one line. In JSON mode
                the pipeline has already printed its own report for a tick that
                ran, so only a skipped tick — which never built a pipeline —
                prints anything here.
        """
        recent = store.recent_runs(1)
        row = recent[0] if recent else None
        if row is None:  # pragma: no cover - a run always writes a row
            return

        if as_json:
            if row.get("outcome") == state.OUTCOME_SKIPPED_OVERLAPPING:
                print(
                    json.dumps(
                        {
                            "fire_time": scheduler.to_utc_iso(tick.fire_time),
                            "outcome": row.get("outcome"),
                            "detail": scheduler.describe_run(row),
                        }
                    )
                )
            return

        print(f"  {self._run_line(row, tick.fire_time.tzinfo, fire_time=tick.fire_time)}")

    @staticmethod
    def _run_line(row: dict, tz: Optional[tzinfo], fire_time: Optional[datetime] = None) -> str:
        """Render one run as the summary's columns.

        Args:
            row: A row from the ``runs`` table.
            tz: Show times in this zone — the schedule's, not the host's, so
                every line in the panel is comparable with the schedule above
                it. They are the same zone unless the config overrode it, and
                the override exists precisely because the host's is wrong.
            fire_time: The schedule time this run answered, when the caller
                knows it. Falls back to the row's own, then to when it started
                — a manual run has no fire time at all.

        Returns:
            A line of the form ``Thu 18 Sep 09:00   synced 3 documents   48s``.
        """
        when = fire_time or scheduler.parse_stored(
            row.get("scheduled_fire_time") or row.get("started_at")
        )
        stamp = scheduler.format_moment(when, tz) if when else "unknown time"
        detail = scheduler.describe_run(row)
        took = WatchCommand._duration(row)
        return f"{stamp}   {detail}{took}"

    @staticmethod
    def _duration(row: dict) -> str:
        """Render how long a run took, if it finished.

        Args:
            row: A row from the ``runs`` table.

        Returns:
            Two spaces and a duration, or the empty string when the run is
            still going or the timestamps will not parse.
        """
        started = scheduler.parse_stored(row.get("started_at"))
        finished = scheduler.parse_stored(row.get("finished_at"))
        if not started or not finished:
            return ""
        return f"   {scheduler.humanize_duration((finished - started).total_seconds())}"
