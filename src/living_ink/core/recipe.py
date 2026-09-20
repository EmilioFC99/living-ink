"""The digest of everything, other than the document, that shapes the output.

``version`` — the tablet's own content hash — answers *did the document
change*. It is the only question the sync gate used to ask, which is why
editing a prompt, bumping a renderer, switching models or moving the vault
changed nothing at all: every document still matched the recorded version, so
every document was reported unchanged and skipped. The user concluded the
setting did not work.

The recipe answers the other half: *would we produce different output from the
same document*. A document is pending when either differs from what
``publications`` recorded.

Two properties make this usable as a gate rather than as an audit:

- **It is computable before the work.** Every input is a value on a frozen
  :class:`~living_ink.settings.Settings` or a declaration on a class. Nothing
  here downloads, renders, opens a network client or reads a page. A digest of
  the *output* — which is what the deleted ``publications.content_hash`` was —
  can only be compared after doing the work it was supposed to avoid.
- **It is per (source type, destination), not per run and not per document.**
  Each renderer carries its own :attr:`~living_ink.sources.base.Renderer.version`
  and each destination declares its own
  :attr:`~living_ink.destinations.base.Destination.settings`, so a run computes
  at most *types × destinations* values — three, today.

This module names :class:`~living_ink.destinations.base.Destination` and
:class:`~living_ink.sources.base.SourceType` only under ``TYPE_CHECKING``:
``core/`` is what the plugin packages read, so it must not import them back.
What it actually needs from each is one attribute.
"""

import hashlib
import json
from typing import TYPE_CHECKING, Any, Dict

from living_ink.clean import transcription_fingerprint
from living_ink.settings import Settings

if TYPE_CHECKING:  # pragma: no cover - annotations only; core/ takes no plugin dependency
    from living_ink.destinations.base import Destination
    from living_ink.sources.base import SourceType

#: Hex characters kept from each digest. Long enough that a collision is not a
#: thing that happens, short enough to print in a debug line.
_DIGEST_LENGTH = 16


def settings_digest(destination: "Destination", settings: Settings) -> str:
    """Digest the settings this destination actually reads.

    The scope is the destination's own declared
    :attr:`~living_ink.destinations.base.Destination.settings`, never the whole
    of :class:`~living_ink.settings.Settings`. Digesting everything would be
    wrong rather than merely wasteful: raising ``ocr.concurrency`` would flip
    every document pending for every destination, the user would watch their
    vault rewrite itself over a threading knob, and they would stop trusting
    the mechanism that was supposed to be telling them something.

    Args:
        destination: The destination whose settings to digest. Only the class
            is read, so an instance that has not been configured still works.
        settings: The run's resolved settings.

    Returns:
        A short hex digest. Empty declarations still digest — to the same
        value every time, which is the correct answer for a destination whose
        output no setting changes.
    """
    values: Dict[str, Any] = {
        setting.field: getattr(settings, setting.field, None)
        for setting in type(destination).settings
    }
    # sort_keys rather than relying on the declaration order, so moving a
    # Setting up the schema file does not re-publish the vault. default=str
    # lets a Path digest as the string it prints as.
    payload = json.dumps(values, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:_DIGEST_LENGTH]


def document_recipe(
    source_type: "SourceType",
    destination: "Destination",
    settings: Settings,
) -> str:
    """Digest every input, other than the document itself, that shapes the output.

    Three inputs, in the order the pipeline applies them: how the pages are
    drawn, how they are read, and how the result is written.

    Args:
        source_type: The registered source this document resolves to. Its
            renderer's ``version`` is what a rendering change bumps — one
            number per renderer, because a single global one could not say
            which of three had changed and so invalidated all three.
        destination: The destination the document would be published to.
        settings: The run's resolved settings.

    Returns:
        A short hex digest, stable for as long as all three are unchanged.
    """
    parts = (
        source_type.name,
        str(source_type.renderer.version),
        transcription_fingerprint(settings),
        settings_digest(destination, settings),
    )
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:_DIGEST_LENGTH]
