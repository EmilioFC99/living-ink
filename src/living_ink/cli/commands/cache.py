"""``living-ink cache`` — show, prune, or clear the transcription and render caches."""

import argparse
import json
import logging

# The module, never the symbol: these are the seams the behaviour tests
# replace, and a ``from ... import`` binds a copy the patch cannot reach —
# silently, with the test still passing against the real thing.
from living_ink.cli import caches as caches_api
from living_ink.cli.base import BaseCommand

logger = logging.getLogger(__name__)


class CacheCommand(BaseCommand):
    """Inspect and maintain the caches.

    Two of them: transcribed pages, which cost money, and rendered pages, which
    cost time. Together they are what make re-syncing an unchanged notebook
    free, so the only things worth doing by hand are seeing how big they have
    grown, dropping what nobody has used in months, and — when the output is
    wrong and a prompt edit is not the fix — throwing it all away.
    """

    name = "cache"
    help = "Show, prune, or clear the caches"
    description = "Show how much is cached. With no options, prints a summary."

    @classmethod
    def register_args(cls, parser: argparse.ArgumentParser) -> None:
        """Register arguments for the cache command.

        Args:
            parser: Subparser to attach arguments to.
        """
        action = parser.add_mutually_exclusive_group()
        action.add_argument(
            "--clear",
            action="store_true",
            help="Delete every cached page",
        )
        action.add_argument(
            "--prune",
            nargs="?",
            const=-1,
            type=int,
            metavar="DAYS",
            help="Delete entries unused for DAYS days (default: the configured age)",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Output in JSON format",
        )

    def run(self, args: argparse.Namespace) -> int:
        """Show the cache, or prune or clear it.

        Args:
            args: Parsed arguments for cache.

        Returns:
            0. An empty cache is a normal state, not an error.
        """
        caches = caches_api.all_caches()
        as_json = getattr(args, "json", False)

        if getattr(args, "clear", False):
            removed = {c.noun: c.clear() for c in caches}
            return self._report_removal(removed, "cleared", as_json)

        days = getattr(args, "prune", None)
        if days is not None:
            # A bare --prune means "the configured age", which each cache
            # carries for itself.
            removed = {c.noun: c.prune(None if days < 0 else days) for c in caches}
            limit = days if days >= 0 else caches[0].max_age_days
            return self._report_removal(removed, "pruned", as_json, days=limit)

        return self._summary(caches, as_json)

    @staticmethod
    def _summary(caches, as_json: bool) -> int:
        """Print what each cache is holding.

        Args:
            caches: The caches to report on.
            as_json: Whether to print JSON instead of a console summary.

        Returns:
            0.
        """
        from living_ink.cache import format_size

        payload = {}
        for cache in caches:
            entries, total = cache.stats()
            payload[cache.noun] = {
                "path": str(cache.root),
                "enabled": cache.enabled,
                "entries": entries,
                "size_bytes": total,
                "max_age_days": cache.max_age_days,
            }

        if as_json:
            print(json.dumps(payload, indent=2))
            return 0

        from living_ink.setup_wizard import bold, cyan, dim

        print()
        print(bold(cyan("Caches")))
        for cache in caches:
            entry = payload[cache.noun]
            print(f"  {cache.noun}s")
            print(f"    {dim(entry['path'])}")
            if not entry["enabled"]:
                print("    disabled — every page is done afresh")
            print(f"    {entry['entries']} page(s), {format_size(entry['size_bytes'])}")
            print(f"    pruned after {entry['max_age_days']} day(s) unused")
        print()
        return 0

    @staticmethod
    def _report_removal(removed: dict, verb: str, as_json: bool, days: int = 0) -> int:
        """Report how many entries an operation removed from each cache.

        Args:
            removed: How many entries went, keyed by what the cache calls one.
            verb: Past-tense description of what happened.
            as_json: Whether to print JSON instead of a console message.
            days: The age limit used, for a prune.

        Returns:
            0.
        """
        if as_json:
            print(json.dumps({"action": verb, "removed": removed, "max_age_days": days}))
            return 0

        for noun, count in removed.items():
            print(f"{verb.capitalize()} {count} cached {noun}(s).")
        if removed.get("transcribed page"):
            print("Those pages will be transcribed again, and paid for, on the next sync.")
        return 0
