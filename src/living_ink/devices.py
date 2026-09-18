"""What each reMarkable model is, as declared data rather than assumption.

Rendering geometry used to be two module-level constants in
:mod:`living_ink.extract` describing the reMarkable 1/2 panel, applied to every
device. This module turns that into a table: a model the project has never seen
falls back to the same constants, but it does so *named* and logged, instead of
silently guessing.

Supporting a device nobody here owns should be one entry in
:data:`DEVICE_PROFILES` that a contributor can submit and a maintainer can read.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Tuple

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeviceProfile:
    """The physical characteristics of one reMarkable model.

    Attributes:
        name: The model name this project uses in messages and reports.
        screen: Panel size in pixels, as ``(width, height)``.
        color: Whether the panel can display colour.
    """

    name: str
    screen: Tuple[int, int]
    color: bool = False


#: Known models, keyed by the name this project uses for them.
DEVICE_PROFILES: Dict[str, DeviceProfile] = {
    "reMarkable 1": DeviceProfile("reMarkable 1", (1404, 1872), color=False),
    "reMarkable 2": DeviceProfile("reMarkable 2", (1404, 1872), color=False),
    "reMarkable Paper Pro": DeviceProfile("reMarkable Paper Pro", (1620, 2160), color=True),
}

#: What the code assumed before profiles existed, now named rather than implied.
DEFAULT_PROFILE = DEVICE_PROFILES["reMarkable 2"]

# Matched against the device's own machine string, which is not the name anyone
# would write by hand: a reMarkable 2 reports "reMarkable 2.0", and the first
# generation reports "reMarkable Prototype 1". Longest match wins, so "ferrari"
# is not shadowed by a shorter substring that happens to also appear.
_MACHINE_HINTS = {
    "prototype 1": "reMarkable 1",
    "reMarkable 1": "reMarkable 1",
    "reMarkable 2": "reMarkable 2",
    "ferrari": "reMarkable Paper Pro",
    "paper pro": "reMarkable Paper Pro",
}


def profile_for(machine: str) -> DeviceProfile:
    """Identify a device from the machine string it reports about itself.

    Args:
        machine: Contents of the tablet's machine identifier, e.g.
            ``"reMarkable 2.0"``. May be empty if the device did not answer.

    Returns:
        The matching profile, or :data:`DEFAULT_PROFILE` for anything
        unrecognised. An unknown device is logged rather than passed over, so a
        wrong-geometry bug report starts with the model that caused it.
    """
    haystack = (machine or "").lower()
    matches = [name for hint, name in _MACHINE_HINTS.items() if hint.lower() in haystack]
    if matches:
        # Longest hint wins so "reMarkable Paper Pro" is not read as a plain
        # "reMarkable 2" when both would match.
        best = max(
            (hint for hint in _MACHINE_HINTS if hint.lower() in haystack),
            key=len,
        )
        return DEVICE_PROFILES[_MACHINE_HINTS[best]]

    logger.info(
        "Unrecognised reMarkable model %r; using %s geometry.",
        machine,
        DEFAULT_PROFILE.name,
    )
    return DEFAULT_PROFILE
