"""What is on the tablet, what is published, and the difference.

The preview and the run must agree, so both read ``core.selection``; this module
only turns the selection into rows a person or a JSON consumer can read.
"""

import argparse
import dataclasses
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from living_ink.config import get_config_path
from living_ink.settings import Settings

if TYPE_CHECKING:  # pragma: no cover - annotations only; both are imported lazily
    from living_ink.core.selection import Selection
    from living_ink.state import StateStore, SyncStatus

logger = logging.getLogger(__name__)


def count_by_status(inventory: list[dict[str, Any]]) -> dict["SyncStatus", int]:
    """Total the inventory by sync status.

    Args:
        inventory: Rows carrying a ``status``, as
            :func:`rows_from_selection` returns.

    Returns:
        Mapping of every :data:`~living_ink.state.SYNC_STATUSES` entry to its
        count, zeros included so callers can format without checking for
        missing keys. Iterating the registry rather than naming statuses is
        what lets a new status be one entry in ``state.py`` and nothing here.
    """
    from living_ink.state import SYNC_STATUSES

    counts = {status: 0 for status in SYNC_STATUSES}
    for row in inventory:
        if row["status"] in counts:
            counts[row["status"]] += 1
    return counts


def compare_with_device(args: argparse.Namespace, root: Optional[Path] = None):
    """List the tablet and judge it against what has been published.

    The judgement is not made here. The listing goes to
    :func:`living_ink.core.selection.select` — the same call, with the same
    arguments, that the sync itself makes — because a preview that predicts
    something other than what the run does is worse than no preview, in a
    feature whose entire purpose is to say what will happen. Everything below
    is presentation: order the answer by the listing and name the status.

    Metadata only. ``get_meta_items()`` is one listing call, and no client is
    passed to the classifier, so the document type comes from the listing's own
    file index rather than a round trip per document.

    Args:
        args: Parsed sync arguments; ``--ssh`` / ``--cloud`` select the
            transport, otherwise the configured preference wins.
        root: Optional repository root, for locating the config.

    Returns:
        ``(rows, orphans, device)`` — the comparison rows, documents published
        but no longer on the tablet, and what the transport says it is talking
        to, or None when it cannot say.

    Raises:
        ConfigurationMissing: If configuration is absent or unusable.
    """
    from living_ink.api import get_rmapi
    from living_ink.core.selection import SelectionCriteria, select
    from living_ink.pipeline import get_default_config, get_default_destinations, get_state_store

    cfg_path = get_config_path(root)
    if cfg_path.exists():
        os.environ.setdefault("LIVING_INK_CONFIG_DIR", str(cfg_path.parent))

    settings = Settings.resolve(get_default_config())
    if getattr(args, "ssh", False):
        settings = dataclasses.replace(settings, preferred_connection="ssh")
    elif getattr(args, "cloud", False):
        settings = dataclasses.replace(settings, preferred_connection="cloud")

    client = get_rmapi(settings)

    try:
        device = client.get_device_info()
    except Exception:
        # Broad on purpose, and it covers UnsupportedOperation: the comparison
        # needs the device's listing, not its identity. Saying nothing about
        # which tablet beats refusing to answer the question asked.
        logger.debug("Transport could not identify the device", exc_info=True)
        device = None

    collection = list(client.get_meta_items())
    store = get_state_store()
    destinations = get_default_destinations()

    chosen = select(
        collection,
        # No limit and no type filter: a preview answers "where does everything
        # stand", and a document this run's flags would skip still has a state
        # worth reporting. The limit belongs to the run, not to the question.
        #
        # The exclusions are the exception, and they are here because they are
        # not a flag: a folder the config says is never synced is not pending,
        # and reporting it as pending is the preview and the run disagreeing.
        # ``sync.tags`` stays out — applying it means one round trip per
        # document, which is not what a status question should cost.
        SelectionCriteria(exclude=frozenset(settings.sync_exclude or ())),
        store,
        destinations,
        settings=settings,
    )

    return (
        rows_from_selection(collection, chosen, store, destinations),
        _orphan_records(chosen, store),
        device,
    )


def rows_from_selection(
    collection: list[Any],
    chosen: "Selection",
    store: "StateStore",
    destinations: list[Any],
) -> list[dict[str, Any]]:
    """Render a selection as the comparison rows the status view prints.

    Ordered by the tablet's own listing rather than by the selection, which
    groups documents by what happened to them: a list the user can find their
    notebook in beats a list sorted by a verdict they have not read yet.

    Args:
        collection: The transport's listing, documents and folders.
        chosen: What a sync would do with it.
        store: Where publications are recorded, for the versions already held.
        destinations: The enabled destinations.

    Returns:
        One row per judged document — its fields plus ``status``, ``pending``
        and ``published``. Documents the classifier never reached, because they
        are in the trash, are absent.
    """
    from living_ink.core.listing import get_val
    from living_ink.core.selection import Candidate
    from living_ink.state import DocumentView, classify

    judged: dict[str, Candidate] = {}
    for candidate in (*chosen.to_process, *chosen.deferred):
        judged[candidate.doc_id] = candidate
    for item, _reason in chosen.skipped:
        if isinstance(item, Candidate):
            judged[item.doc_id] = item

    publications = store.all_publications()
    known = {record["id"]: record for record in store.all_documents()}
    names = [dest.state_key for dest in destinations]

    rows: list[dict[str, Any]] = []
    for item in collection:
        candidate = judged.get(get_val(item, "ID"))
        if candidate is None:
            continue

        record = known.get(candidate.doc_id, {})
        published = {
            name: row["version"] for name, row in publications.get(candidate.doc_id, {}).items()
        }
        pending = [dest.state_key for dest in candidate.pending]

        rows.append(
            {
                "id": candidate.doc_id,
                "name": candidate.name,
                # The database fills the gaps the listing leaves: a document's
                # type costs a round trip to determine for certain, and a
                # previous run already paid for it.
                "folder": candidate.folder or record.get("folder"),
                "doc_type": candidate.source or record.get("doc_type"),
                "version": candidate.version,
                "last_error": record.get("last_error"),
                "status": classify(
                    DocumentView(
                        doc_id=candidate.doc_id,
                        last_error=record.get("last_error"),
                        published=published,
                        pending=pending,
                        destinations=names,
                    )
                ),
                "pending": pending,
                "published": published,
            }
        )
    return rows


def _orphan_records(chosen: "Selection", store: "StateStore") -> list[dict[str, Any]]:
    """Describe the documents that have notes but are no longer on the tablet.

    Args:
        chosen: What a sync would do, including the ids it found orphaned.
        store: Where documents are recorded, for the names to print.

    Returns:
        One record per orphan, carrying at least its ``id``.
    """
    known = {record["id"]: record for record in store.all_documents()}
    return [known.get(doc_id) or {"id": doc_id} for doc_id in chosen.orphans]


def inventory_as_json(inventory: list[dict[str, Any]]) -> dict[str, Any]:
    """Render the inventory as JSON-safe data.

    A row's ``status`` is a :class:`~living_ink.state.SyncStatus`, which is not
    serialisable and whose ``label`` is free to be reworded. Both the rows and
    the counts are keyed by the stable ``key`` instead, so a script reading
    this output survives a rename in the UI.

    Args:
        inventory: Rows from :func:`collect_inventory`.

    Returns:
        A dict with ``documents`` and ``counts``, ready for :func:`json.dumps`.
    """
    documents = [{**row, "status": row["status"].key} for row in inventory]
    counts = {status.key: count for status, count in count_by_status(inventory).items()}
    return {"documents": documents, "counts": counts}


#: How many rows a page of the comparison shows.
PAGE_SIZE = 10

#: Width of the truncated document name column.
NAME_WIDTH = 15


def format_comparison_row(row: dict[str, Any]) -> str:
    """Render one document as `<id> <name>.<type> <status>`.

    Args:
        row: One entry from :func:`rows_from_selection`.

    Returns:
        The line to print, without a leading indent and without colour — the
        caller colours the status so that the columns stay aligned whether or
        not escape codes are in play.
    """
    doc_id = str(row.get("id") or "")[:8].ljust(8)
    name = str(row.get("name") or row.get("id") or "")
    # Truncated with an ellipsis rather than hard-cut, so a row that lost
    # characters admits it instead of quietly reading as a different note.
    if len(name) > NAME_WIDTH:
        name = name[: NAME_WIDTH - 1] + "…"
    doc_type = row.get("doc_type") or "notebook"
    return f"{doc_id}  {name.ljust(NAME_WIDTH)}.{doc_type.ljust(8)}  "


def render_comparison(
    rows: list[dict[str, Any]],
    orphans: list[dict[str, Any]],
    device: Any,
    *,
    show_all: bool,
) -> None:
    """Print the summary and as much of the list as was asked for.

    Args:
        rows: Comparison rows, in listing order.
        orphans: Documents published but no longer on the tablet.
        device: What the transport is talking to, or None.
        show_all: Whether to page through everything rather than show the
            first :data:`PAGE_SIZE` rows.
    """
    from living_ink.setup_wizard import bold, dim, green
    from living_ink.state import SYNC_STATUSES

    print()
    against = device.describe() if device else "your reMarkable"
    print(bold(f"Comparing {against} against your notes"))
    print()

    if not rows:
        print(dim("Nothing on the tablet to compare."))
        print()
        return

    counts = count_by_status(rows)
    for status in SYNC_STATUSES:
        count = counts[status]
        if not count:
            continue
        colour = tone_colour(status.tone)
        print(f"  {colour(str(count).rjust(4))}  {status.label}")
    if orphans:
        print(f"  {dim(str(len(orphans)).rjust(4))}  no longer on the tablet")
    print()

    # Outstanding work first: a list that opens with forty up-to-date notes
    # buries the three that need attention.
    ordered = sorted(rows, key=lambda row: SYNC_STATUSES.index(row["status"]))

    if not any(row["status"].needs_sync for row in rows):
        print(green("Everything is up to date."))
        print()
        if not show_all:
            return

    if show_all:
        _print_paged(ordered)
    else:
        for row in ordered[:PAGE_SIZE]:
            _print_comparison_row(row)
        remaining = len(ordered) - PAGE_SIZE
        if remaining > 0:
            print()
            print(dim(f"{PAGE_SIZE} of {len(ordered)} shown · {remaining} more — use --all"))
    print()


def _print_comparison_row(row: dict[str, Any]) -> None:
    """Print one row with its status coloured.

    Args:
        row: One comparison row.
    """
    status = row["status"]
    print(f"  {format_comparison_row(row)}{tone_colour(status.tone)(status.label)}")


def _print_paged(rows: list[dict[str, Any]]) -> None:
    """Print every row, pausing each page when someone is watching.

    Args:
        rows: Comparison rows, already ordered.
    """
    from living_ink.setup_wizard import dim

    interactive = sys.stdout.isatty() and sys.stdin.isatty()

    for start in range(0, len(rows), PAGE_SIZE):
        for row in rows[start : start + PAGE_SIZE]:
            _print_comparison_row(row)

        shown = min(start + PAGE_SIZE, len(rows))
        if not interactive or shown >= len(rows):
            continue

        print()
        try:
            answer = input(dim(f"  {shown} of {len(rows)} — Enter for more, q to stop: "))
        except (EOFError, KeyboardInterrupt):
            # Piped into `head`, or the user gave up. Neither is an error.
            print()
            return
        if answer.strip().lower().startswith("q"):
            return


def tone_colour(tone: str):
    """Map a status tone onto the colour helper that renders it.

    Statuses name a tone rather than carrying a colour function so that
    :mod:`living_ink.state` stays free of presentation imports. This is the
    one place that translation happens.

    Args:
        tone: ``"good"``, ``"warn"``, ``"bad"`` or anything else.

    Returns:
        A callable taking a string and returning it wrapped in escape codes.
        An unrecognised tone renders dim rather than raising — a new status
        should never be able to crash the renderer.
    """
    from living_ink.setup_wizard import dim, green, red, yellow

    return {"good": green, "warn": yellow, "bad": red}.get(tone, dim)
