"""``living-ink sync`` — do it now.

Never interactive, not even for ``--notebook``: a command a scheduler runs must
never be able to block on a prompt.
"""

import argparse
import json
import logging
import os
import re
import sys
from typing import Any

# The module, never the symbol: these are the seams the behaviour tests
# replace, and a ``from ... import`` binds a copy the patch cannot reach —
# silently, with the test still passing against the real thing.
from living_ink.cli import inventory as inventory_api
from living_ink.cli.base import BaseCommand
from living_ink.cli.flags import flag_values, register_settings_flags
from living_ink.config import ConfigurationMissing, get_config_path
from living_ink.scheduler import RUN_LOCK_NAME, LockBusy, RunLock
from living_ink.state import TRIGGER_MANUAL
from living_ink.transport import TransportUnavailable

logger = logging.getLogger(__name__)


def _pattern(raw: str) -> str:
    """Accept a regular expression, rejecting one that will not compile.

    Checked at parse time rather than when the selection runs, so a typo costs
    a usage error before a transport is opened, an inventory is read or a page
    is rendered. The message is the position and the reason ``re`` reports,
    never a traceback.

    Args:
        raw: The pattern as typed.

    Returns:
        The pattern, unchanged — it is compiled again where it is used, and
        handing back the string keeps the namespace printable.

    Raises:
        argparse.ArgumentTypeError: If the pattern is not a valid regex.
    """
    try:
        re.compile(raw)
    except re.error as exc:
        raise argparse.ArgumentTypeError(f"invalid pattern {raw!r}: {exc}") from exc
    return raw


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
        "source_path": getattr(args, "source_path", None),
        "source_regex": getattr(args, "source_regex", None),
        "force": getattr(args, "force", False),
        "keep_temp": getattr(args, "keep_temp", False),
        # The pipeline's own name for "do the work, publish nothing", which is
        # what ``--preview --transcribe`` asks for. ``--preview`` on its own
        # never builds a pipeline at all.
        "dry_run": getattr(args, "preview", False) and getattr(args, "transcribe", False),
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
        # One mutually exclusive group, so asking for both is a usage error the
        # parser reports before the tablet is contacted. They filter the same
        # field by different rules, and defining a precedence between them
        # would only hide the mistake.
        where = parser.add_mutually_exclusive_group()
        where.add_argument(
            "--source-path",
            default=None,
            metavar="TEXT",
            help="Only documents whose full path contains this text (case-insensitive)",
        )
        where.add_argument(
            "--source-regex",
            default=None,
            type=_pattern,
            metavar="PATTERN",
            help="Only documents whose full path matches this pattern (case-sensitive)",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Publish every selected document, even one the comparison calls unchanged",
        )
        parser.add_argument(
            "--keep-temp",
            action="store_true",
            help="Preserve temporary rendered images, OCR transcripts, and downloaded documents after sync",
        )
        parser.add_argument(
            "--preview",
            action="store_true",
            help="Show what this exact command would do and exit; no download, no OCR, no API call",
        )
        parser.add_argument(
            "--transcribe",
            action="store_true",
            help="With --preview, also transcribe — the expensive rehearsal: real OCR, nothing published",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="With --preview, list every document instead of the first ten",
        )

    def run(self, args: argparse.Namespace) -> int:
        """Run the notebook sync pipeline.

        Args:
            args: Parsed arguments for sync.

        Returns:
            0 on success, 1 on a failure the user has to fix, 2 on a usage
            error. Never exits: the code travels back to ``main``, which is
            the only place that calls :func:`sys.exit`, so a library caller —
            and ``watch``, which runs this in a loop — can decide what a
            failure means.
        """
        if getattr(args, "transcribe", False) and not getattr(args, "preview", False):
            # A rehearsal nobody asked to watch is just a sync that throws the
            # work away, so refusing beats guessing which half was meant.
            print("--transcribe only means something with --preview.", file=sys.stderr)
            return 2

        if getattr(args, "preview", False) and not getattr(args, "transcribe", False):
            try:
                return self.show_preview(args)
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
        except LockBusy as e:
            # Two syncs writing one vault is the failure the lock exists to
            # prevent, and the second one arriving is ordinary — a watcher is
            # mid-tick. Say so in a line, not a traceback.
            print(str(e), file=sys.stderr)
            print("Fix: wait for it to finish, or stop it and try again.", file=sys.stderr)
            return 1
        return 0 if success else 1

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

    def show_preview(self, args: argparse.Namespace) -> int:
        """Report what a sync would do, without doing any of it.

        Lists the tablet over whichever transport the settings resolve to and
        joins that against the state database. Metadata only: no download, no
        render, no OCR, no API call.

        Args:
            args: Parsed arguments for sync. Every flag ``sync`` takes is
                honoured, because a flag that changed the selection but not
                the preview would make the preview a lie.

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

    def execute_sync(
        self,
        args: argparse.Namespace,
        *,
        trigger: str = TRIGGER_MANUAL,
        scheduled_fire_time: str | None = None,
    ) -> bool:
        """Run one sync and report whether it worked.

        Separate from :meth:`run` because turning a failure into a process exit
        is a decision only the one-shot command gets to make; a caller that
        syncs repeatedly — :class:`WatchCommand` — needs the verdict, not a
        dead process.

        The whole-machine run lock is taken here rather than in the pipeline,
        because it guards *the vault*, not the object: a manual sync and a
        scheduled fire in two processes would otherwise write the same note at
        once. It is reentrant within a process, so a watch tick that already
        holds it passes straight through.

        Args:
            args: Parsed arguments for sync.
            trigger: What asked for this run — ``manual`` or ``scheduled``.
            scheduled_fire_time: The UTC ISO instant this run was due, when a
                schedule asked for it. None for a manual run.

        Returns:
            True if the pipeline reported success.

        Raises:
            ConfigurationMissing: If configuration is absent or unusable.
            LockBusy: If another process is already syncing.
        """
        if self.root and str(self.root) not in sys.path:
            sys.path.insert(0, str(self.root))

        cfg_path = get_config_path(self.root)
        if cfg_path.exists():
            os.environ.setdefault("LIVING_INK_CONFIG_DIR", str(cfg_path.parent))

        from living_ink.pipeline import DATA_DIR, SyncPipeline

        pipeline = SyncPipeline(
            **sync_arguments(args),
            config_path=cfg_path if cfg_path.exists() else None,
            trigger=trigger,
            scheduled_fire_time=scheduled_fire_time,
        )
        with RunLock(DATA_DIR / RUN_LOCK_NAME):
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
        # stderr, like every other problem this command reports: ``--json``
        # promises stdout holds one document and nothing else, and a run that
        # never got as far as building the report still has to say why.
        banner = "=" * 60
        for line in (
            "",
            banner,
            "CONFIGURATION ERROR",
            banner,
            str(error),
            "-" * 60,
            error.hint,
            banner,
            "",
        ):
            print(line, file=sys.stderr)

        if not self.offer_setup_on_missing_config or not sys.stdin.isatty():
            return 1

        try:
            choice = input("Would you like to run the interactive setup wizard now? [Y/n]: ")
        except EOFError:
            # No answer available: the config problem stands, unfixed.
            return 1
        except KeyboardInterrupt:
            # Declining the wizard is "n". Ctrl+C is the user leaving, and it
            # exits 130 like every other interrupt rather than looking like a
            # configuration failure they chose not to fix.
            raise

        if choice.strip().lower() not in ("", "y", "yes"):
            return 1

        # Imported here, not at module level: the wizard's command offers to
        # run a sync when it finishes, so the two commands name each other and
        # one of the two edges has to be deferred.
        from living_ink.cli.commands.setup import SetupCommand

        return SetupCommand(root=self.root).run(args)
