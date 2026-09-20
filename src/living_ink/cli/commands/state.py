"""``living-ink state`` — inspect, reset, or check the sync state database."""

import argparse
import json
import logging
from pathlib import Path
from typing import Optional

# The module, never the symbol: these are the seams the behaviour tests
# replace, and a ``from ... import`` binds a copy the patch cannot reach —
# silently, with the test still passing against the real thing.
from living_ink.cli import caches as caches_api
from living_ink.cli.base import BaseCommand
from living_ink.cli.status import short_destination

logger = logging.getLogger(__name__)


class StateCommand(BaseCommand):
    """Inspect and repair the sync state database.

    Everything Living Ink remembers between runs lives in one SQLite file, and
    when it is wrong the symptom is indirect: a notebook that will not re-sync,
    or one that re-syncs every time. These are the three things worth doing to
    it by hand — look at it, forget one document, and check it is not corrupt.
    """

    name = "state"
    help = "Inspect, reset, or check the sync state database"
    description = (
        "Show what Living Ink remembers between runs. With no options, prints a "
        "summary of the state database."
    )

    @classmethod
    def register_args(cls, parser: argparse.ArgumentParser) -> None:
        """Register arguments for the state command.

        Args:
            parser: Subparser to attach arguments to.
        """
        action = parser.add_mutually_exclusive_group()
        action.add_argument(
            "--dump",
            action="store_true",
            help="Print every row in the database",
        )
        action.add_argument(
            "--forget",
            metavar="DOCUMENT",
            help="Forget a document by id, name, or 'Folder/Name' so it syncs again",
        )
        action.add_argument(
            "--repair",
            action="store_true",
            help="Check the database for damage and compact it",
        )
        parser.add_argument(
            "--destination",
            help="Limit --forget to one destination (e.g. ObsidianDestination)",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Output in JSON format",
        )

    def run(self, args: argparse.Namespace) -> int:
        """Inspect or modify the state database.

        Args:
            args: Parsed arguments for state.

        Returns:
            0 on success; 1 when the database is missing, the named document
            is unknown or ambiguous, or the integrity check fails.
        """
        path = caches_api.state_db_path()
        if not path.exists():
            print(f"No state database yet ({path}). Run 'living-ink sync' first.")
            return 1

        from living_ink.pipeline import get_state_store

        store = get_state_store()
        as_json = getattr(args, "json", False)

        if getattr(args, "dump", False):
            return self._dump(store, as_json)
        if getattr(args, "forget", None):
            return self._forget(store, args.forget, getattr(args, "destination", None), as_json)
        if getattr(args, "repair", False):
            return self._repair(store, path, as_json)
        return self._summary(store, path, as_json)

    @staticmethod
    def _summary(store, path: Path, as_json: bool) -> int:
        """Print what the database holds and when it was last written.

        Args:
            store: Open state store.
            path: Database file.
            as_json: Whether to print JSON instead of a console summary.

        Returns:
            0.
        """
        counts = store.counts()
        last_run = store.last_run()
        payload = {
            "path": str(path),
            "size_bytes": path.stat().st_size,
            "schema_version": store._conn.execute("PRAGMA user_version").fetchone()[0],
            "counts": counts,
            "last_run": last_run,
        }

        if as_json:
            print(json.dumps(payload, indent=2))
            return 0

        from living_ink.setup_wizard import bold, cyan, dim

        print()
        print(bold(cyan("Sync state")))
        print(f"  {dim(str(path))}  ({payload['size_bytes'] / 1024:.1f} KB)")
        print(f"  schema {payload['schema_version']}")
        print()
        for table, count in counts.items():
            print(f"  {table.ljust(14)} {count}")
        if last_run:
            print()
            outcome = last_run["outcome"] or "unfinished"
            print(f"  last run       {last_run['started_at']} ({outcome})")
        print()
        return 0

    @staticmethod
    def _dump(store, as_json: bool) -> int:
        """Print every row, for when the summary is not enough.

        Args:
            store: Open state store.
            as_json: Whether to print JSON instead of a console listing.

        Returns:
            0.
        """
        data = store.dump()
        if as_json:
            print(json.dumps(data, indent=2))
            return 0

        for table, rows in data.items():
            print(f"\n[{table}] {len(rows)} row(s)")
            for row in rows:
                print("  " + "  ".join(f"{key}={value!r}" for key, value in row.items()))
        print()
        return 0

    @staticmethod
    def _forget(store, query: str, destination: Optional[str], as_json: bool) -> int:
        """Drop a document's publication records so the next sync redoes it.

        Refuses to guess when a name matches more than one document: picking
        one would silently re-OCR the wrong notebook, and the user can see the
        ids and say which they meant.

        Args:
            store: Open state store.
            query: Id, name, or ``Folder/Name``.
            destination: Limit to one destination, or None for all of them.
            as_json: Whether to print JSON instead of a console message.

        Returns:
            0 when one document was forgotten, 1 otherwise.
        """
        matches = store.find_documents(query)

        if not matches:
            print(f"No document matches {query!r}. Try 'living-ink sync --preview --all'.")
            return 1
        if len(matches) > 1:
            print(f"{query!r} matches {len(matches)} documents. Use an id:")
            for row in matches:
                print(f"  {row['id']}  {row['name']}  ({row['folder'] or '—'})")
            return 1

        document = matches[0]
        removed = store.forget(document["id"], destination)

        if as_json:
            print(
                json.dumps(
                    {
                        "id": document["id"],
                        "name": document["name"],
                        "publications_removed": removed,
                    }
                )
            )
            return 0

        where = f" for {short_destination(destination)}" if destination else ""
        print(f"Forgot {document['name'] or document['id']}{where} ({removed} record(s) removed).")
        print("It will be transcribed and published again on the next sync.")
        return 0

    @staticmethod
    def _repair(store, path: Path, as_json: bool) -> int:
        """Check the database for damage and reclaim space.

        A damaged file is reported, never deleted. Rebuilding it costs a full
        re-OCR of every notebook, which is real money, so that decision stays
        with the user.

        Args:
            store: Open state store.
            path: Database file.
            as_json: Whether to print JSON instead of a console message.

        Returns:
            0 when the database is intact, 1 when it is not.
        """
        result = store.integrity_check()
        healthy = result == "ok"
        before = path.stat().st_size

        if healthy:
            store.vacuum()
        after = path.stat().st_size

        if as_json:
            print(
                json.dumps(
                    {
                        "integrity": result,
                        "healthy": healthy,
                        "bytes_before": before,
                        "bytes_after": after,
                    }
                )
            )
            return 0 if healthy else 1

        from living_ink.setup_wizard import green, red

        if healthy:
            print(f"Integrity: {green('ok')}")
            print(f"Compacted {before / 1024:.1f} KB -> {after / 1024:.1f} KB")
            return 0

        print(f"Integrity: {red('damaged')}")
        print(result)
        print()
        print(f"Move {path} aside and run 'living-ink sync' to rebuild it.")
        print("Everything will be transcribed again, which costs another full OCR pass.")
        return 1
