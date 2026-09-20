"""``living-ink watch`` — do it on a schedule, forever."""

import argparse
import logging
import time

from living_ink.cli.base import BaseCommand
from living_ink.cli.commands.sync import SyncCommand
from living_ink.config import ConfigurationMissing

logger = logging.getLogger(__name__)


class WatchCommand(BaseCommand):
    """Sync on a fixed interval until interrupted."""

    name = "watch"
    help = "Sync repeatedly on a timer"
    description = (
        "Run the sync pipeline every --interval seconds until interrupted. "
        "Accepts every sync option, so a watch can be scoped the same way a single sync can."
    )

    #: Half an hour is frequent enough that notes land while they are still on
    #: the user's mind, and rare enough to stay well inside free API tiers.
    DEFAULT_INTERVAL = 1800

    #: Below this the tablet is polled faster than a sync typically finishes.
    MIN_INTERVAL = 30

    @classmethod
    def register_args(cls, parser: argparse.ArgumentParser) -> None:
        """Register the watch interval on top of every sync option.

        Args:
            parser: Subparser to attach arguments to.
        """
        SyncCommand.register_args(parser)
        parser.add_argument(
            "--interval",
            type=int,
            default=cls.DEFAULT_INTERVAL,
            help=f"Seconds between syncs (default: {cls.DEFAULT_INTERVAL}, minimum: {cls.MIN_INTERVAL})",
        )

    def run(self, args: argparse.Namespace) -> int:
        """Sync in a loop until the user interrupts it.

        A sync that fails does not end the watch: an unplugged tablet, a closed
        laptop lid, or a rate-limited provider are the ordinary conditions this
        command exists to ride out. Only a configuration problem stops it,
        because that one will still be there on the next tick.

        Args:
            args: Parsed arguments — every sync option, plus ``--interval``.

        Returns:
            0 when interrupted, 1 if configuration is missing or unusable.
        """
        interval = max(self.MIN_INTERVAL, getattr(args, "interval", self.DEFAULT_INTERVAL))
        syncer = SyncCommand(root=self.root)
        # A watch is unattended by definition, so never stop to offer the wizard.
        syncer.offer_setup_on_missing_config = False

        print(f"Watching reMarkable — syncing every {interval}s. Press Ctrl+C to stop.")
        while True:
            try:
                if syncer.execute_sync(args):
                    print("Sync complete.")
                else:
                    print("Sync finished with errors; retrying next cycle.")
            except ConfigurationMissing as e:
                print(f"\nConfiguration error: {e}")
                print(e.hint)
                return 1
            except KeyboardInterrupt:
                return self._stopped()
            except Exception as e:
                # Deliberately broad. A daemon that exits on the first
                # unexpected error is a daemon that is not running when the
                # user needs it; the traceback goes to the log file instead.
                logger.warning("Sync cycle failed", exc_info=True)
                print(f"Sync failed: {e}; retrying next cycle.")

            try:
                print(f"Next sync in {interval}s.")
                time.sleep(interval)
            except KeyboardInterrupt:
                return self._stopped()

    @staticmethod
    def _stopped() -> int:
        """Report a clean shutdown after Ctrl+C.

        Returns:
            0 — an interrupted watch is the intended way to end one, not a
            failure, so systemd and launchd should not treat it as a crash.
        """
        print("\nStopped watching.")
        return 0
