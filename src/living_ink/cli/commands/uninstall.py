"""``living-ink uninstall`` — remove what Living Ink installed, and nothing else.

The command exists because :mod:`living_ink.setup_wizard` writes to four places
outside the checkout — a launchd job, a config file, a credentials directory
and a data directory — and until now the only way to undo any of it was to know
all four paths. ``uninstall_launch_agent`` had been written for a year without
ever being called.

Three rules shape it.

**Your notes are never touched, under any flag.** The vault is the output, not
the installation: the whole point of publishing into Obsidian is that what
lands there is yours afterwards. There is no flag that removes it, which is why
the vault path is *printed* during the run — a user watching an uninstall
scroll past should see the one thing that is staying.

**Removal is tiered, and the tiers are ordered by what they cost to rebuild.**
The background job goes without asking, because an uninstall that leaves a
process waking up every morning has not uninstalled anything and the job costs
one ``setup`` to reinstall. Caches go with it — they are regenerable by
definition, and leaving several hundred megabytes of PNGs behind is not
restraint. Everything whose loss costs the user something real is a separate
question with its consequence stated in it: credentials mean pairing the tablet
again, and the sync record means the next sync re-transcribes and republishes
every document it already paid for.

**The flag is what makes it non-interactive, so the flag is where the terminal
test lives.** ``interactive`` is a class attribute checked once by the app
before a command runs, and it cannot know about ``--yes``; a command that
declared itself interactive would be refused in the script that most needs it.
So this one declares itself non-interactive and refuses *itself*, in ``run``,
when it has questions to ask and nowhere to ask them.
"""

import argparse
import logging
import shutil
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from living_ink import ui
from living_ink.cli.base import BaseCommand

logger = logging.getLogger(__name__)


class UninstallCommand(BaseCommand):
    """Remove the background job, the caches, and — if asked — the settings."""

    name = "uninstall"
    help = "Remove Living Ink's background job, settings and caches"
    description = (
        "Remove what Living Ink installed: the background sync job and the caches "
        "always, your settings, credentials and sync record on confirmation. "
        "Your notes are never touched. This does not remove the program itself — "
        "uninstall the package the way you installed it."
    )

    @classmethod
    def register_args(cls, parser: argparse.ArgumentParser) -> None:
        """Register the one flag: answer every question with yes.

        Args:
            parser: Subparser to attach arguments to.
        """
        parser.add_argument(
            "--yes",
            "-y",
            action="store_true",
            help="Remove everything without asking, including credentials and sync state",
        )

    def run(self, args: argparse.Namespace) -> int:
        """Remove the installation, tier by tier.

        Args:
            args: Parsed arguments for uninstall.

        Returns:
            0 when everything the user agreed to was removed, 1 if something
            could not be, 2 when there are questions to ask and no terminal to
            ask them in.
        """
        assume_yes = bool(getattr(args, "yes", False))
        if not assume_yes and not ui.is_tty():
            # Its own message rather than ``ui.NO_TTY_MESSAGE``: that one sends
            # the reader to environment variables, and no environment variable
            # answers "may I delete your credentials?".
            print(
                "living-ink uninstall: this command asks before removing anything, "
                "and there is no terminal to ask in. Pass --yes to remove "
                "everything without asking.",
                file=sys.stderr,
            )
            return 2

        from living_ink.logs import console

        console("")
        console(ui.bold("Uninstalling Living Ink"))
        self._promise_the_notes_are_safe()

        failures: List[str] = []
        failures += self._remove_the_job()
        failures += self._remove_the_caches()
        failures += self._remove_the_settings(assume_yes)
        failures += self._remove_the_sync_record(assume_yes)
        failures += self._remove_the_command(assume_yes)

        console("")
        if failures:
            for problem in failures:
                console(ui.yellow(f"  ⚠ {problem}"))
            console(ui.yellow("Some things could not be removed. Remove them by hand."))
            return 1

        console(ui.green("Done."))
        console(ui.dim("The program itself is still installed — remove it with 'uv tool uninstall"))
        console(ui.dim("living-ink' or 'pip uninstall living-ink', whichever you used."))
        return 0

    # -- the one thing that is never removed ---------------------------------

    @staticmethod
    def _promise_the_notes_are_safe() -> None:
        """Name the vault, and say it is staying.

        Printed before anything is deleted rather than after, because the
        reassurance a user needs is the one they get *before* watching a list
        of removals scroll past.
        """
        from living_ink.logs import console

        vault = ""
        try:
            # Read the file, not ``pipeline.get_default_config()``: that one
            # caches for the life of the process, and the file this command is
            # about to delete is the one whose answer matters.
            from living_ink.config import get_config_path, read_config_file
            from living_ink.settings import Settings

            config_path = get_config_path()
            settings = Settings.resolve(read_config_file(config_path), config_path=config_path)
            vault = (settings.obsidian_vault_path or "").strip()
        except Exception:
            # A config too broken to read is not a reason to skip the promise;
            # it is a reason to make it without naming a path.
            logger.debug("Could not read the vault path", exc_info=True)

        where = f" in {vault}" if vault else ""
        console(ui.dim(f"  Your notes{where} are not touched."))

    # -- tier 1: removed without asking --------------------------------------

    @staticmethod
    def _remove_the_job() -> List[str]:
        """Unload and delete the background job.

        Returns:
            A list of problems, empty when there was nothing to do or the job
            was removed.
        """
        import platform

        from living_ink.logs import console

        if platform.system() != "Darwin":
            # Nothing was installed to remove, and guessing at somebody's
            # systemd unit is how an uninstall deletes a file it did not write.
            console(ui.dim("  · Background job: nothing installed by Living Ink"))
            return []

        from living_ink.setup_wizard import uninstall_launch_agent

        ok, message = uninstall_launch_agent()
        if ok:
            console(f"  {ui.green('✓')} {message}")
            return []
        return [message]

    @staticmethod
    def _remove_the_caches() -> List[str]:
        """Delete the rendered pages, the transcriptions and the work area.

        Returns:
            A list of problems, one per path that resisted.
        """
        from living_ink.pipeline import (
            LOGS_DIR,
            RENDER_CACHE_DIR,
            TRANSCRIPT_CACHE_DIR,
            WORK_DIR,
        )

        return _remove_each(
            [
                (TRANSCRIPT_CACHE_DIR, "Transcription cache"),
                (RENDER_CACHE_DIR, "Rendered pages"),
                (WORK_DIR, "Temporary files"),
                (LOGS_DIR, "Logs"),
            ]
        )

    # -- tier 2: removed on confirmation -------------------------------------

    @staticmethod
    def _remove_the_settings(assume_yes: bool) -> List[str]:
        """Delete ``config.yml`` and every stored credential, if agreed.

        Args:
            assume_yes: Whether ``--yes`` answered this already.

        Returns:
            A list of problems, one per path that resisted.
        """
        from living_ink.config import credentials_dir, get_config_path

        config_path = get_config_path()
        secrets = credentials_dir(config_path)
        if not _agreed(
            assume_yes,
            "Remove your settings and saved credentials?",
            "You would have to run 'living-ink setup' and pair the tablet again.",
        ):
            return []

        return _remove_each([(secrets, "Credentials"), (config_path, "Settings")])

    @staticmethod
    def _remove_the_sync_record(assume_yes: bool) -> List[str]:
        """Delete ``state.db``, if agreed.

        A separate question from the settings, because its consequence is
        different in kind: the next sync would not know any document had ever
        been published, so it would transcribe and republish all of them —
        which costs API calls, not just a wizard.

        Args:
            assume_yes: Whether ``--yes`` answered this already.

        Returns:
            A list of problems, empty when nothing was removed.
        """
        from living_ink.cli.caches import state_db_path

        if not _agreed(
            assume_yes,
            "Remove the record of what has already been synced?",
            "The next sync would transcribe and publish every document again.",
        ):
            return []

        return _remove_each([(state_db_path(), "Sync record")])

    @staticmethod
    def _remove_the_command(assume_yes: bool) -> List[str]:
        """Delete the ``living-ink`` wrapper the wizard wrote, if agreed.

        Only the wrapper, and only when it is a plain file: a symlink in
        ``~/.local/bin`` belongs to ``uv tool``, which installed the package
        and is the thing that should remove it.

        Args:
            assume_yes: Whether ``--yes`` answered this already.

        Returns:
            A list of problems, empty when nothing was removed.
        """
        wrapper = Path.home() / ".local" / "bin" / "living-ink"
        if wrapper.is_symlink() or not wrapper.exists():
            return []

        if not _agreed(
            assume_yes,
            f"Remove the 'living-ink' command at {wrapper}?",
            "The command would stop working immediately, including this one.",
        ):
            return []

        return _remove_each([(wrapper, "Command")])


def _agreed(assume_yes: bool, question: str, consequence: str) -> bool:
    """Ask one removal question, with its cost stated next to it.

    Args:
        assume_yes: Whether ``--yes`` already answered every question.
        question: What to ask.
        consequence: What saying yes costs, printed above the prompt.

    Returns:
        True if the thing should be removed. A cancelled prompt answers no —
        :func:`living_ink.ui.confirm` returns None, and ``bool(None)`` is the
        right reading of it here: a user who interrupts a deletion prompt has
        not agreed to the deletion.
    """
    from living_ink.logs import console

    if assume_yes:
        return True

    console("")
    console(ui.dim(f"  {consequence}"))
    return bool(ui.confirm(question, default=False))


def _remove_each(targets: List[Tuple[Optional[Path], str]]) -> List[str]:
    """Delete each path that exists, reporting one line per removal.

    Args:
        targets: ``(path, label)`` pairs. A path that does not exist is not a
            failure and prints nothing — an uninstall on a half-installed
            machine should read as an uninstall, not as four warnings.

    Returns:
        A list of problems, one per path that could not be removed.
    """
    from living_ink.logs import console

    problems: List[str] = []
    for path, label in targets:
        if path is None or not path.exists():
            continue
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as error:
            problems.append(f"{label}: could not remove {path} ({error})")
            continue
        console(f"  {ui.green('✓')} {label} removed ({path})")
    return problems
