"""The command-line front end.

One module per command under :mod:`living_ink.cli.commands`, and the shared
readers those commands print — status, inventory, caches — beside them. The
package re-exports the lot, so ``from living_ink.cli import X`` is the stable
import path and the file a symbol lives in is free to move.

Patching is the one thing a re-export cannot carry: a test that replaces
``living_ink.cli.collect_status`` rebinds this module's name and nothing else,
because every caller reaches the function through its own module. Patch the
module that *defines* it — ``living_ink.cli.status.collect_status`` — or the
substitution silently does nothing while the test keeps passing.
"""

from living_ink.cli.app import LivingInkCLI, add_verbosity_args, configure_logging, main
from living_ink.cli.base import BaseCommand
from living_ink.cli.caches import all_caches, render_cache, state_db_path, transcript_cache
from living_ink.cli.commands.cache import CacheCommand
from living_ink.cli.commands.setup import SetupCommand
from living_ink.cli.commands.state import StateCommand
from living_ink.cli.commands.status import StatusCommand
from living_ink.cli.commands.sync import SyncCommand, sync_arguments
from living_ink.cli.commands.watch import WatchCommand
from living_ink.cli.inventory import (
    NAME_WIDTH,
    PAGE_SIZE,
    compare_with_device,
    count_by_status,
    format_comparison_row,
    inventory_as_json,
    render_comparison,
    rows_from_selection,
    tone_colour,
)
from living_ink.cli.status import StatusReport, collect_status, short_destination

__all__ = [
    "NAME_WIDTH",
    "PAGE_SIZE",
    "BaseCommand",
    "CacheCommand",
    "LivingInkCLI",
    "SetupCommand",
    "StateCommand",
    "StatusCommand",
    "StatusReport",
    "SyncCommand",
    "WatchCommand",
    "add_verbosity_args",
    "all_caches",
    "collect_status",
    "compare_with_device",
    "configure_logging",
    "count_by_status",
    "format_comparison_row",
    "inventory_as_json",
    "main",
    "render_cache",
    "render_comparison",
    "rows_from_selection",
    "short_destination",
    "state_db_path",
    "sync_arguments",
    "tone_colour",
    "transcript_cache",
]
