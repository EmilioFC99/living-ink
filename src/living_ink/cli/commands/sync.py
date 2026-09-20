"""``living-ink sync`` — do it now.

Never interactive, not even for ``--notebook``: a command a scheduler runs must
never be able to block on a prompt.
"""

import argparse
import json
import logging
import os
import sys
from typing import Any

# The module, never the symbol: these are the seams the behaviour tests
# replace, and a ``from ... import`` binds a copy the patch cannot reach —
# silently, with the test still passing against the real thing.
from living_ink.cli import inventory as inventory_api
from living_ink.cli.base import BaseCommand
from living_ink.cli.flags import flag_values, register_settings_flags
from living_ink.config import ConfigurationMissing, get_config_path
from living_ink.transport import TransportUnavailable

logger = logging.getLogger(__name__)


def sync_arguments(args: argparse.Namespace) -> dict[str, Any]:
    """Translate a parsed ``sync`` namespace into pipeline keyword arguments.

    This is the single place that knows CLI flag names, so adding a flag means
    touching the parser and this function — never the pipeline internals. It
    lives in the front end rather than on the pipeline because ``dest=`` names
    are an argparse fact: a library caller constructs the pipeline directly and
    should not have to know that ``--json`` arrives as ``args.output_json``.

    Note:
        Everything the settings schema declares travels in one ``flags``
        mapping rather than as a keyword each, so registering a new flag never
        means widening the pipeline's signature. What is left as a named
        keyword is the handful of choices that shape one run and have no
        persisted form to be resolved against.

    Args:
        args: Namespace produced by the sync subparser. Read with ``getattr``
            defaults throughout, because ``watch`` reuses the same parser and a
            test may hand over a bare namespace.

    Returns:
        Keyword arguments for :class:`~living_ink.pipeline.SyncPipeline`.
    """
    return {
        "notebook": getattr(args, "notebook", None),
        "all_types": getattr(args, "all_types", False),
        "keep_temp": getattr(args, "keep_temp", False),
        "dry_run": getattr(args, "dry_run", False),
        "flags": flag_values(args, SyncCommand.name),
    }


class SyncCommand(BaseCommand):
    """Execute the reMarkable notebook sync pipeline.

    Attributes:
        offer_setup_on_missing_config: Whether an unusable config should prompt
            the user to run the wizard. Set to False when the wizard is already
            what invoked this command, to avoid bouncing between the two.
    """

    name = "sync"
    help = "Run the sync pipeline"
    description = "Sync notes and documents from reMarkable to an Obsidian vault."

    offer_setup_on_missing_config = True

    @classmethod
    def register_args(cls, parser: argparse.ArgumentParser) -> None:
        """Register arguments for the sync pipeline.

        Args:
            parser: Subparser to attach arguments to.
        """
        # Everything the schema declares a flag for, registered from the
        # declaration. Only the switches below are hand-written, because they
        # shape one run and have no persisted form to declare.
        register_settings_flags(parser, cls.name)
        parser.add_argument(
            "--notebook",
            default=None,
            help="Sync a specific notebook by name, folder path (e.g. 'Work/Notes'), or document ID",
        )
        parser.add_argument(
            "--all-types",
            action="store_true",
            help="Sync all document types (notebooks, PDFs, and EPUBs)",
        )
        parser.add_argument(
            "--keep-temp",
            action="store_true",
            help="Preserve temporary rendered images, OCR transcripts, and downloaded documents after sync",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Transcribe as usual but publish nothing; prints where each transcript was written",
        )
        parser.add_argument(
            "--status",
            action="store_true",
            help="Show what a sync would do — compare the tablet against your notes — and exit",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="With --status, list every document instead of the first ten",
        )

    def run(self, args: argparse.Namespace) -> int:
        """Run the notebook sync pipeline.

        Args:
            args: Parsed arguments for sync.

        Returns:
            0 on success, or exits with 1 on failure.
        """
        if getattr(args, "status", False):
            try:
                return self.show_status(args)
            except ConfigurationMissing as e:
                # Not routed through _handle_missing_config: that offers the
                # wizard and then retries the *sync*, which is not what someone
                # asking a read-only question wanted to set in motion.
                print(f"Configuration problem: {e}", file=sys.stderr)
                # The error's own hint, not a fixed line: a config that is
                # merely absent wants the wizard, one with an unusable value
                # wants an editor, and only the raiser knows which it is.
                print(f"Fix: {e.hint}", file=sys.stderr)
                return 1
            except TransportUnavailable as e:
                return self._report_unreachable(e)

        try:
            success = self.execute_sync(args)
        except ConfigurationMissing as e:
            return self._handle_missing_config(e, args)
        except TransportUnavailable as e:
            return self._report_unreachable(e)
        if not success:
            sys.exit(1)
        return 0

    def _report_unreachable(self, error: TransportUnavailable) -> int:
        """Report that there was no route to the tablet, without a traceback.

        An unplugged cable and an unconfigured Cloud are the two most ordinary
        states a tablet can be in, and the exception already carries the lines
        that say which one to fix. Printing it as a stack trace would suggest a
        bug in the tool and bury the instruction in frames the user cannot act
        on.

        Args:
            error: The failure reported by the transport layer.

        Returns:
            1, the same failure code an unsuccessful sync returns.
        """
        print(f"Cannot reach the reMarkable: {error}", file=sys.stderr)
        return 1

    def show_status(self, args: argparse.Namespace) -> int:
        """Report what a sync would do, without doing any of it.

        Lists the tablet over whichever transport the settings resolve to and
        joins that against the state database. Metadata only: no download, no
        render, no OCR, no API call.

        Args:
            args: Parsed arguments for sync; ``--ssh`` / ``--cloud``, ``--all``
                and ``--json`` are honoured.

        Returns:
            0 when the comparison succeeded, 1 when the tablet was unreachable.

        Raises:
            ConfigurationMissing: If configuration is absent or unusable.
        """
        rows, orphans, device = inventory_api.compare_with_device(args, root=self.root)

        if getattr(args, "output_json", False):
            payload = inventory_api.inventory_as_json(rows)
            payload["orphans"] = [row["id"] for row in orphans]
            payload["device"] = device.describe() if device else None
            print(json.dumps(payload, indent=2))
            return 0

        inventory_api.render_comparison(
            rows,
            orphans,
            device,
            show_all=getattr(args, "all", False),
        )
        return 0

    def execute_sync(self, args: argparse.Namespace) -> bool:
        """Run one sync and report whether it worked.

        Separate from :meth:`run` because turning a failure into a process exit
        is a decision only the one-shot command gets to make; a caller that
        syncs repeatedly — :class:`WatchCommand` — needs the verdict, not a
        dead process.

        Args:
            args: Parsed arguments for sync.

        Returns:
            True if the pipeline reported success.

        Raises:
            ConfigurationMissing: If configuration is absent or unusable.
        """
        if self.root and str(self.root) not in sys.path:
            sys.path.insert(0, str(self.root))

        cfg_path = get_config_path(self.root)
        if cfg_path.exists():
            os.environ.setdefault("LIVING_INK_CONFIG_DIR", str(cfg_path.parent))

        from living_ink.pipeline import SyncPipeline

        pipeline = SyncPipeline(
            **sync_arguments(args),
            config_path=cfg_path if cfg_path.exists() else None,
        )
        return pipeline.run()

    def _handle_missing_config(self, error: "ConfigurationMissing", args) -> int:
        """Report a configuration problem and, if interactive, offer the wizard.

        The pipeline only reports that configuration is unusable; whether to
        interrupt the user and walk them through setup is a front-end decision,
        so it is made here.

        Args:
            error: The configuration problem the pipeline reported.
            args: Parsed arguments, reused if the sync is retried after setup.

        Returns:
            0 if setup ran and the retried sync succeeded, 1 otherwise.
        """
        print("\n" + "=" * 60)
        print("CONFIGURATION ERROR")
        print("=" * 60)
        print(str(error))
        print("-" * 60)
        print(error.hint)
        print("=" * 60 + "\n")

        if not self.offer_setup_on_missing_config or not sys.stdin.isatty():
            return 1

        try:
            choice = input("Would you like to run the interactive setup wizard now? [Y/n]: ")
        except (KeyboardInterrupt, EOFError):
            return 1

        if choice.strip().lower() not in ("", "y", "yes"):
            return 1

        # Imported here, not at module level: the wizard's command offers to
        # run a sync when it finishes, so the two commands name each other and
        # one of the two edges has to be deferred.
        from living_ink.cli.commands.setup import SetupCommand

        return SetupCommand(root=self.root).run(args)
