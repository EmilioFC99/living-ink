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
from typing import TYPE_CHECKING, Dict, Optional, Tuple

if TYPE_CHECKING:  # pragma: no cover - only needed for annotations
    from living_ink.state import StateStore
    from living_ink.transport import DeviceInfo, RemarkableTransport

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
    # Measured on the hardware: the device reports itself as "reMarkable Tatsu"
    # and its own boot splash is 1404×1872, the same panel as the reMarkable 2.
    "reMarkable Paper Pure": DeviceProfile("reMarkable Paper Pure", (1404, 1872), color=False),
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
    "tatsu": "reMarkable Paper Pure",
    "paper pure": "reMarkable Paper Pure",
}


def identify(machine: str) -> Optional[DeviceProfile]:
    """Match a machine string against the models this project knows.

    Args:
        machine: Contents of the tablet's machine identifier, e.g.
            ``"reMarkable 2.0"``. May be empty if the device did not answer.

    Returns:
        The matching profile, or None. None is the honest answer for a device
        nobody here has seen, and callers that must produce *something* say so
        rather than passing the default off as a match.
    """
    haystack = (machine or "").lower()
    hits = [hint for hint in _MACHINE_HINTS if hint.lower() in haystack]
    if not hits:
        return None
    # Longest hint wins so "reMarkable Paper Pro" is not read as a plain
    # "reMarkable 2" when both would match.
    return DEVICE_PROFILES[_MACHINE_HINTS[max(hits, key=len)]]


def profile_for(machine: str) -> DeviceProfile:
    """Identify a device, falling back to the default geometry.

    Args:
        machine: Contents of the tablet's machine identifier.

    Returns:
        The matching profile, or :data:`DEFAULT_PROFILE` for anything
        unrecognised. An unknown device is logged rather than passed over, so a
        wrong-geometry bug report starts with the model that caused it.
    """
    profile = identify(machine)
    if profile is not None:
        return profile

    logger.info(
        "Unrecognised reMarkable model %r; using %s geometry.",
        machine,
        DEFAULT_PROFILE.name,
    )
    return DEFAULT_PROFILE


#: A reading taken live from the tablet over USB. Authoritative.
SOURCE_USB = "usb"

#: A reading a past USB session took and the state store kept.
SOURCE_REMEMBERED = "remembered"

#: No reading has ever been taken; :data:`DEFAULT_PROFILE` is standing in.
SOURCE_DEFAULT = "default"


@dataclass(frozen=True)
class DeviceReading:
    """What Living Ink believes it is syncing with, and how sure it is.

    Attributes:
        info: The device itself.
        source: One of :data:`SOURCE_USB`, :data:`SOURCE_REMEMBERED` or
            :data:`SOURCE_DEFAULT`.
        learned_at: ISO timestamp of the USB session that produced a
            remembered reading. Empty for the other two sources.
    """

    info: DeviceInfo
    source: str
    learned_at: str = ""

    def describe(self) -> str:
        """Render the reading as one line for ``status``.

        Returns:
            The device description, followed by where the belief came from. A
            remembered reading says so, and carries its date, because claiming
            a live reading for a tablet that is not plugged in would make a
            stale geometry impossible to spot.
        """
        base = self.info.describe()
        if self.source == SOURCE_USB:
            return base
        if self.source == SOURCE_REMEMBERED:
            when = f", {self.learned_at[:10]}" if self.learned_at else ""
            return f"{base} (remembered from USB{when})"
        return f"{base} (assumed — connect over USB to confirm)"


def default_reading() -> DeviceReading:
    """Return the reading used when nothing has ever been learned.

    Returns:
        :data:`DEFAULT_PROFILE`, marked :data:`SOURCE_DEFAULT` so every caller
        can tell a guess from a measurement.
    """
    from living_ink.transport import DeviceInfo

    return DeviceReading(
        info=DeviceInfo(
            model=DEFAULT_PROFILE.name,
            firmware="",
            screen=DEFAULT_PROFILE.screen,
            color=DEFAULT_PROFILE.color,
        ),
        source=SOURCE_DEFAULT,
    )


def resolve_device(
    transport: Optional["RemarkableTransport"] = None,
    store: Optional["StateStore"] = None,
) -> DeviceReading:
    """Work out which tablet this is, preferring evidence over assumption.

    Only USB SSH can see the hardware, and most runs are Cloud-only, so the
    order is: ask the tablet if it can answer, otherwise use what a past USB
    session taught us, otherwise fall back to the named default. A live reading
    is written back to the store, which is the whole point — one USB session is
    enough to make every later Cloud-only run right.

    Args:
        transport: Transport to ask. May be None, or may not support the call.
        store: State store to read the memory from and write a live reading to.
            May be None, in which case nothing is remembered.

    Returns:
        The best available reading, never None.
    """
    from living_ink.transport import DeviceInfo, UnsupportedOperation

    if transport is not None:
        try:
            info = transport.get_device_info()
        except (UnsupportedOperation, RuntimeError, OSError) as e:
            # Not an error: a Cloud-only setup is a supported setup. The
            # memory below is exactly what covers it.
            logger.debug("No live device reading: %s", e)
        else:
            # Checked rather than trusted: this value is about to be written to
            # a durable store and used as render geometry, and a transport that
            # answers with something else should not poison either.
            if isinstance(info, DeviceInfo):
                if store is not None:
                    store.remember_device(info)
                return DeviceReading(info=info, source=SOURCE_USB)
            logger.debug("Transport returned %r rather than a DeviceInfo.", type(info))

    if store is not None:
        remembered = store.recall_device()
        if remembered is not None:
            info, learned_at = remembered
            return DeviceReading(info=info, source=SOURCE_REMEMBERED, learned_at=learned_at)

    return default_reading()
