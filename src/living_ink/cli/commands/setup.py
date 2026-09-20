"""``living-ink setup`` — first run, from nothing to a working sync."""

import argparse
import logging

from living_ink.cli.base import BaseCommand
from living_ink.cli.commands.sync import SyncCommand

logger = logging.getLogger(__name__)


class SetupCommand(BaseCommand):
    """Launch the interactive onboarding setup wizard."""

    name = "setup"
    help = "Launch the interactive setup wizard"
    description = "Configure reMarkable connection, AI provider, and note destinations."

    @classmethod
    def register_args(cls, parser: argparse.ArgumentParser) -> None:
        """Register arguments for the setup wizard.

        Args:
            parser: Subparser to attach arguments to.
        """
        pass

    def run(self, args: argparse.Namespace) -> int:
        """Run the interactive setup wizard, then optionally the first sync.

        Args:
            args: Parsed arguments for setup.

        Returns:
            0 on completion, or the sync's exit code if the user asked to sync.
        """
        from living_ink.setup_wizard import run_wizard

        result = run_wizard(repo_dir=self.root)
        if result.run_sync_requested:
            print("\nStarting sync pipeline...\n")
            sync = SyncCommand(root=self.root)
            # Config was just written; if it is still unusable, reporting the
            # problem beats looping back into the wizard that produced it.
            sync.offer_setup_on_missing_config = False
            return sync.run(args)
        return 0
