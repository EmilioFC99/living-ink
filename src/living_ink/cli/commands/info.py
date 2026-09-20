"""``living-ink info`` — what the setup is, and whether it is healthy.

The one read-only surface. ``status``, ``state`` and ``cache`` were three
commands answering overlapping halves of one question, each with its own
``--json``, and nothing stopped them disagreeing: ``status`` measured the
caches while ``cache`` reported them, ``state`` opened the database that
``status`` never looked at. They are now one command over one
:class:`~living_ink.cli.status.StatusReport`.

What is *and is not synced* is deliberately absent: that is a live question
about the tablet, and ``sync --preview`` is the command that goes and asks it.
"""

import argparse
import json
import logging
from pathlib import Path

# The module, never the symbol: these are the seams the behaviour tests
# replace, and a ``from ... import`` binds a copy the patch cannot reach —
# silently, with the test still passing against the real thing.
from living_ink.cli import status as status_api
from living_ink.cli.base import BaseCommand
from living_ink.cli.status import StatusReport
from living_ink.config import get_config_path
from living_ink.settings import SOURCE_ENV

logger = logging.getLogger(__name__)


class InfoCommand(BaseCommand):
    """Report the connection, the AI provider, the destination and the stores.

    Read-only under every flag. The two operations the retired commands could
    perform — clearing a cache and repairing the database — are destructive,
    which is why they move to ``config → Advanced`` rather than staying here
    behind a flag: a command a user runs to find out what is wrong should not
    be one keystroke away from making it worse.
    """

    name = "info"
    help = "Show the setup and whether it is healthy"
    description = (
        "Report the connection, AI provider, destination, caches and sync state. "
        "Reads only; changes nothing."
    )

    @classmethod
    def register_args(cls, parser: argparse.ArgumentParser) -> None:
        """Register arguments for the info command.

        Args:
            parser: Subparser to attach arguments to.
        """
        parser.add_argument(
            "--json",
            action="store_true",
            help="Output the full report, including every state row, as JSON",
        )

    def run(self, args: argparse.Namespace) -> int:
        """Collect the health report and print it.

        Args:
            args: Parsed arguments for info.

        Returns:
            0 on completion, 1 if configuration is missing or invalid.
        """
        report = status_api.collect_status(get_config_path(self.root))
        if getattr(args, "json", False) is True:
            payload = report.to_dict()
            # The rows themselves, which no console view could usefully show
            # and which ``state --dump`` used to be. Attached to the state
            # object the summary already occupies rather than beside it, so a
            # reader finds the counts and the rows in one place.
            if report.state_exists:
                payload["state"]["rows"] = status_api.state_rows()
            print(json.dumps(payload, indent=2))
        else:
            self._render_console(report)
        return 0 if report.usable else 1

    @staticmethod
    def _render_console(report: StatusReport) -> None:
        """Print a styled, human-readable summary of an already-collected report.

        Args:
            report: Snapshot produced by collect_status().
        """
        from living_ink.setup_wizard import bold, cyan, dim, green, red, yellow

        print()
        print(bold(cyan("============================================================")))
        print(bold(cyan("                     Living Ink                             ")))
        print(bold(cyan("============================================================")))
        print()

        if not report.config_found:
            print(f"Configuration: {red('Not found')}")
            print("Run 'living-ink setup' to configure.")
            return

        print(f"Configuration: {green('Found')} ({dim(str(report.config_path))})")
        if report.config_error:
            print(f"Configuration: {red(f'Syntax Error: {report.config_error}')}")
            return

        # reMarkable — report the preferred transport first, then whether the
        # other one is standing by, since that is what the user can act on.
        if report.preferred == "ssh":
            if report.ssh_ok:
                backup = f" {dim('(Cloud backup ready)')}" if report.cloud_ok else ""
                print(f"reMarkable:    {green('Connected')} (USB SSH — Preferred){backup}")
            elif report.cloud_ok:
                unauthorized = (
                    "Tablet reached" in report.ssh_msg or "unauthorized" in report.ssh_msg.lower()
                )
                if unauthorized:
                    print(
                        f"reMarkable:    {yellow('Connected')} (Cloud backup active — USB plugged in but SSH key unauthorized)"
                    )
                    print(
                        f"               {dim(f'→ Run: ssh-copy-id root@{report.ssh_host} to enable USB SSH')}"
                    )
                else:
                    print(
                        f"reMarkable:    {yellow('Connected')} (Cloud backup active — USB SSH unplugged)"
                    )
            else:
                print(f"reMarkable:    {red('Disconnected')} (USB SSH: {report.ssh_msg})")
        else:
            if report.cloud_ok:
                backup = f" {dim('(USB SSH backup ready)')}" if report.ssh_ok else ""
                print(f"reMarkable:    {green('Connected')} (Cloud — Preferred){backup}")
            elif report.ssh_ok:
                print(
                    f"reMarkable:    {yellow('Connected')} (USB SSH backup active — Cloud unavailable)"
                )
            else:
                print(f"reMarkable:    {red('Disconnected')} (Cloud: {report.cloud_msg})")

        # Only USB SSH can see the hardware, so this line is absent on a
        # Cloud-only setup rather than guessing at a model.
        if report.device:
            print(f"Device:        {report.device}")

        # AI provider
        label = f"{report.ai_provider} ({report.ai_model})"
        if report.ai_ok:
            print(f"AI Provider:   {green(label)} — {report.ai_msg}")
        else:
            print(f"AI Provider:   {yellow(report.ai_provider)} — {report.ai_msg}")

        # Obsidian
        if report.obsidian_enabled:
            if report.obsidian_valid:
                vault = Path(report.obsidian_vault)
                target = (
                    vault / report.obsidian_root_folder if report.obsidian_root_folder else vault
                )
                print(f"Obsidian:      {green('Enabled')} -> {target}")
            else:
                print(f"Obsidian:      {red('Not usable')} — {report.obsidian_problem}")
        else:
            print(f"Obsidian:      {dim('Disabled')}")

        # Background sync
        if not report.auto_sync_installed:
            print(f"Auto-Sync:     {dim('Not installed (run living-ink setup to enable)')}")
        elif report.auto_sync_active:
            print(f"Auto-Sync:     {green('Active (runs hourly in background)')}")
        else:
            print(f"Auto-Sync:     {yellow('Installed but not currently loaded')}")

        # No document line at all, not even a pointer. This command reports the
        # setup; what is and is not synced is a live question about the tablet,
        # and `sync --preview` is the command that goes and asks it.

        # Caches — how much of the next sync is already paid for.
        if report.cache_entries:
            from living_ink.cache import format_size

            print(
                f"Cache:         {report.cache_entries} page(s), {format_size(report.cache_bytes)}"
            )

        InfoCommand._render_state(report)

        # Named one by one rather than counted: the fix is a chmod on a
        # specific file, so a count would just send the user looking for them.
        for path in report.loose_credentials:
            print(f"Credentials:   {yellow('Readable by other accounts')} ({path})")
            print(f"               {dim(f'→ Run: chmod 600 {path}')}")

        InfoCommand._render_settings(report)
        print()

    @staticmethod
    def _render_state(report: StatusReport) -> None:
        """Print what the sync state database remembers.

        Two facts and the file, which is all the retired ``state`` summary
        actually told anyone: how much is recorded, and whether the last run
        finished. A fresh install says so rather than printing four zeros.

        Args:
            report: Snapshot produced by collect_status().
        """
        from living_ink.setup_wizard import dim, red, yellow

        if not report.state_exists:
            print(f"Sync state:    {dim('Nothing recorded yet — run living-ink sync')}")
            return

        counts = report.state_counts
        print(
            f"Sync state:    {counts.get('documents', 0)} document(s), "
            f"{counts.get('publications', 0)} publication(s)"
        )
        print(f"               {dim(str(report.state_path))}")

        last = report.last_run
        if not last:
            return
        outcome = last.get("outcome") or "unfinished"
        when = last.get("finished_at") or last.get("started_at") or ""
        # An interrupted or failed run is the one line worth colouring: it is
        # why a document the user expects to see is not in the vault.
        tone = {"success": dim, "interrupted": yellow}.get(outcome, red)
        print(f"               last run {when} {tone(f'({outcome})')}")

    @staticmethod
    def _render_settings(report: StatusReport) -> None:
        """Print the effective settings and the layer each one came from.

        A value can come from the config file, an environment variable, or a
        built-in default, and only the first of those is visible by reading
        ``config.yml``. Printing the winning layer next to each value turns
        "why is it doing that?" into something answerable without a debugger.

        Args:
            report: Snapshot produced by collect_status().
        """
        from living_ink.setup_wizard import bold, cyan, dim, yellow

        if not report.settings:
            return

        print()
        print(bold(cyan("Effective settings")))
        width = max(len(origin.name) for origin in report.settings)
        for origin in report.settings:
            # An environment override is the surprising case, so it is the one
            # that gets colour; config and defaults are expected and stay quiet.
            if origin.source == SOURCE_ENV:
                note = yellow(f"{origin.source} ({origin.env_var})")
            else:
                note = dim(origin.source)
            print(f"  {origin.name.ljust(width)}  {origin.display()}  {note}")
