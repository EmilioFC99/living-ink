"""Destinations for publishing reMarkable notes.

Abstracts the publication target (Apple Notes, Obsidian, etc.) from the
processing logic. All publication targets inherit from
:class:`~living_ink.destinations.base.Destination`.

Destinations publish in registry order, and registry order is the order of the
imports below, which is alphabetical. With one destination that is invisible;
with two, "whose failure is reported first" would otherwise be decided by an
import statement nobody thinks of as configuration.

Example:
    >>> from living_ink.destinations import ObsidianDestination
    >>> dest = ObsidianDestination(vault_path="/path/to/vault", root_folder="Living Ink")
    >>> dest.publish("Meeting Notes", "# Content", [])
"""

import logging
from typing import Any, Dict, List

from living_ink import logs
from living_ink.destinations.apple_notes import AppleNotesDestination
from living_ink.destinations.base import (
    DESTINATION_REGISTRY,
    Destination,
    DestinationError,
    DestinationStatus,
    DestinationUnavailable,
    register_destination,
)
from living_ink.destinations.obsidian import ObsidianDestination
from living_ink.settings import Settings

logger = logging.getLogger(__name__)

__all__ = [
    "DESTINATION_REGISTRY",
    "AppleNotesDestination",
    "Destination",
    "DestinationError",
    "DestinationStatus",
    "DestinationUnavailable",
    "ObsidianDestination",
    "build_destinations",
    "register_destination",
]


def _apply_legacy_destination(config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Translate the old single ``destination`` key into per-destination sections.

    Early configs named one destination at the top level, either as a string
    (``destination: obsidian``) or as a dict carrying that destination's own
    settings. Both forms mean "this one and no other", so they are normalized
    into the same per-section shape the registry reads.

    Args:
        config: Parsed ``config.yml`` contents.

    Returns:
        One section per registered destination, legacy overrides applied.
    """
    sections = {key: dict(config.get(key) or {}) for key in DESTINATION_REGISTRY}

    legacy = config.get("destination")
    if legacy is None:
        return sections

    if isinstance(legacy, str):
        # The string form only rules the others out; the named destination is
        # still configured by, and enabled by, its own section.
        named, extras, selects = legacy.strip(), {}, False
    elif isinstance(legacy, dict):
        named = str(legacy.get("type", "")).strip()
        extras = {k: v for k, v in legacy.items() if k != "type"}
        selects = True
    else:
        return sections

    for key, section in sections.items():
        if key != named:
            section["enabled"] = False
        elif selects:
            # A legacy dict both selects the destination and configures it.
            section.update(extras)
            section["enabled"] = True

    return sections


def build_destinations(config: Dict[str, Any], settings: Settings) -> List[Destination]:
    """Build every destination the configuration enables.

    Walks :data:`DESTINATION_REGISTRY` rather than naming destinations one by
    one, so a newly registered subclass is picked up here for free.

    Args:
        config: Parsed ``config.yml`` contents.
        settings: The run's resolved settings.

    Returns:
        The enabled destinations, in registration order. A destination whose
        section is incomplete is skipped with a warning rather than aborting
        the run.
    """
    sections = _apply_legacy_destination(config)
    built: List[Destination] = []

    for key, cls in DESTINATION_REGISTRY.items():
        section = sections.get(key, {})
        if not section.get("enabled", cls.enabled_by_default):
            continue

        try:
            destination = cls.from_config(section, settings)
        except Exception as e:
            # Broad by contract: one misconfigured destination skips itself
            # rather than taking the other destinations down with it.
            # console(), not print(): under --json stdout must carry the
            # JSON document and nothing else.
            logs.console(f"⚠️ Could not set up destination '{key}': {e}")
            logger.warning("Destination '%s' failed to build: %s", key, e, exc_info=True)
            continue

        if destination is not None:
            built.append(destination)
            logs.console(f"Destination added: {destination.describe()}")

    return built
