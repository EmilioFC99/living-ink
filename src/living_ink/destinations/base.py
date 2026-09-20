"""The destination contract: the registry, the errors, and the ABC itself.

Everything every destination must honour lives here, and nothing else does.
How a body is rendered, whether attachments are separate objects, whether a
write is retried — all of that is the destination's own business.
"""

import abc
import enum
import logging
from dataclasses import dataclass
from typing import Any, ClassVar, Dict, Optional, Tuple, Type

from living_ink.config import Setting
from living_ink.core.document import Document, PublishContext, PublishResult
from living_ink.settings import Settings

logger = logging.getLogger(__name__)


class MergeUnit(str, enum.Enum):
    """How much of an already-published note a destination has to rewrite.

    A closed pair. A third member would have to name a granularity between
    "one page" and "the whole thing" that some destination actually offers,
    and the split is really between targets that expose stable in-content
    anchors and targets that do not.

    ``str`` mixin rather than ``enum.StrEnum``: this package supports Python
    3.10, where the latter does not exist.
    """

    PAGE = "page"
    """One page can be replaced and the rest of the note — including text the
    user wrote themselves — left alone."""

    DOCUMENT = "document"
    """Only the whole note can be replaced. The default, because it is the
    assumption that is never unsafe: a destination with nowhere to anchor a
    reader-invisible page marker cannot promise anything narrower."""


@dataclass(frozen=True)
class DestinationStatus:
    """Whether a destination could publish right now, and what to do if not.

    Attributes:
        ok: Whether this destination can publish.
        detail: One line naming the state it is in, for preflight and ``status``.
        remedy: What the user should do about it, when there is an obvious
            answer. A detail without a remedy is a fact; with one it is a fix.
    """

    ok: bool
    detail: str
    remedy: Optional[str] = None


class DestinationError(Exception):
    """Publishing failed for an expected, user-actionable reason.

    Raised for conditions the user can fix (a vault path that no longer
    exists, a full disk, macOS denying automation access). Anything that is
    *not* one of these — an ``AttributeError`` in our own code, say — is
    deliberately left to propagate rather than being reported as an ordinary
    publish failure.
    """


DESTINATION_REGISTRY: Dict[str, Type["Destination"]] = {}


def register_destination(config_key: str, enabled_by_default: bool = False):
    """Register a Destination subclass under its ``config.yml`` section name.

    Adding a destination is then a matter of writing the class and decorating
    it; :func:`~living_ink.destinations.build_destinations` picks it up without
    anyone editing the pipeline.

    Args:
        config_key: The config section that configures this destination.
        enabled_by_default: Whether it runs when ``enabled`` is not stated.

    Returns:
        The class decorator.

    Raises:
        TypeError: The class did not declare its own :attr:`Destination.state_key`.
            Inheriting one would make two destinations share a row in the
            publications table; forgetting one entirely would go unnoticed until
            somebody renamed the class.
    """

    def decorator(cls: Type["Destination"]) -> Type["Destination"]:
        if "state_key" not in cls.__dict__:
            raise TypeError(
                f"{cls.__name__} must declare its own state_key; it is the name its "
                "sync state is filed under and cannot be inherited or inferred."
            )
        cls.config_key = config_key
        cls.enabled_by_default = enabled_by_default
        # Read from __dict__, not the attribute: a subclass that says nothing
        # should be named after its own section, not after its parent.
        cls.display_name = cls.__dict__.get("display_name") or config_key
        DESTINATION_REGISTRY[config_key] = cls
        return cls

    return decorator


class Destination(abc.ABC):
    """Abstract base class for publication destinations.

    Subclasses implement :meth:`publish` to format and write notes, and
    :meth:`from_config` to build themselves from their own section of
    ``config.yml``. Decorating the subclass with :func:`register_destination`
    is what makes it reachable from configuration — no other module needs to
    learn the new name.

    Attributes:
        config_key: The ``config.yml`` section this destination reads, set by
            :func:`register_destination`.
        enabled_by_default: Whether the destination is active when its section
            says nothing about ``enabled``.
        state_key: The name this destination's publications are filed under in
            ``state.db``. Declared, never derived: it used to be the class name,
            so renaming the class silently made every document look new — every
            note rewritten, every ``first_published`` date restarted. Changing
            this string is a state migration and nothing less.
        display_name: What the destination is called in a log line, the run
            summary and an error message. Free to change; ``state_key`` is not.
            Defaults to ``config_key`` when the class does not set one.
        merge_unit: The smallest thing this destination can rewrite without
            disturbing its neighbours. It is what a preview promises the user
            *before* the run — "pages 12 and 77 will be updated, anything you
            wrote between them is kept" is a different promise from "the whole
            note will be rewritten", and the pipeline reads this rather than
            branching on a class name to decide which one to make.
        settings: The settings whose values change what this destination
            writes. Declared, because change detection digests them: a
            destination that names its own inputs makes an edit to one of them
            re-publish the documents it affects, and only those. Digesting the
            whole of :class:`~living_ink.settings.Settings` instead would make
            an unrelated flag — ``--limit``, a log level — look like a content
            change and rewrite the entire vault.
    """

    config_key: ClassVar[str] = ""
    enabled_by_default: ClassVar[bool] = False
    state_key: ClassVar[str] = ""
    display_name: ClassVar[str] = ""
    merge_unit: ClassVar[MergeUnit] = MergeUnit.DOCUMENT
    settings: ClassVar[Tuple[Setting, ...]] = ()

    @classmethod
    def from_config(cls, section: Dict[str, Any], settings: Settings) -> Optional["Destination"]:
        """Build this destination from its config section.

        Args:
            section: The destination's own section of ``config.yml``.
            settings: The run's resolved settings.

        Returns:
            The configured destination, or None if the section is incomplete
            and the destination should be skipped. Implementations explain the
            skip to the user rather than failing the whole run.
        """
        raise NotImplementedError

    def describe(self) -> str:
        """Return a one-line description for the startup summary.

        Returns:
            The destination's name and the setting a user would want confirmed.
        """
        return self.display_name or self.config_key or type(self).__name__

    @abc.abstractmethod
    def check(self) -> DestinationStatus:
        """Report whether publishing would work, without publishing anything.

        Called by preflight before a single page is rendered, and by ``status``.
        Cheap by contract: a path test or one API ping, never a round trip.

        This is where a bad vault is caught. Construction does not validate —
        it used to, and the exception was swallowed into a warning, leaving a
        run to discover it had no destinations only by publishing to none of
        them and exiting 0.

        Returns:
            Whether this destination is ready, what state it is in, and the
            remedy if there is one.
        """

    @abc.abstractmethod
    def publish(self, doc: Document, ctx: PublishContext) -> PublishResult:
        """Publish one document to this destination.

        Two arguments, because the split is real: ``doc`` is the same for every
        destination and ``ctx`` is what this destination did with it last time.
        They used to be twelve positional parameters, which meant a new fact
        about a document was a signature change in every destination that had
        no use for it, and every caller.

        A destination reads what it can use and ignores the rest. Nothing here
        is a filesystem path except the two temp artifacts named in
        :class:`~living_ink.core.document.Page` and
        :attr:`~living_ink.core.document.Document.source_file`, both of which
        are gone by the time this call returns.

        Args:
            doc: The transcribed document, destination-neutral.
            ctx: This publish — the previous publication's coordinates, whether
                anything may be written, and the run's settings.

        Returns:
            The outcome, carrying where the note landed and any identifier the
            destination wants fed back to it next run.

        Raises:
            DestinationError: Publication failed for an expected reason. The
                message is user-facing and names the cause.
        """

    def unpublish(self, ctx: PublishContext) -> PublishResult:
        """Remove a note whose document no longer exists on the tablet.

        Only ever called for a document Living Ink published itself and can no
        longer find, and only when the user asked for it with ``--prune``. The
        default is to refuse: a destination that cannot prove which note is the
        right one must not delete any.

        There is no Document here and there cannot be — the document is gone.
        Everything known about the note comes from its state-store row, which
        is exactly what :class:`PublishContext` carries.

        Args:
            ctx: The coordinates of the note to remove.

        Returns:
            The outcome. ``ok`` is False when nothing was removed, which is not
            an error — it is the default answer.

        Raises:
            DestinationError: Removal failed for an expected reason.
        """
        name = self.display_name or type(self).__name__
        logger.info("%s does not support removing notes.", name)
        return PublishResult(ok=False, detail=f"{name} cannot remove notes.")

    def published_exists(self, ctx: PublishContext) -> bool:
        """Report whether the note recorded for this document is still there.

        Change detection asks this before it decides a document is settled: the
        state store says a note was published, and the user may since have
        deleted it. A notebook that is unchanged on the tablet but whose note
        is gone must come back, and nothing else in the run would notice.

        True by default, which is the answer that does the least damage when a
        destination genuinely cannot look. A default of False would make every
        such destination re-publish its whole library on every run for ever,
        rather than once; a destination that *can* check overrides this and
        gets the deletion caught on the next sync.

        Cheap by contract, like :meth:`check`: this runs once per published
        document per run, so it is a ``stat()`` or an equivalent, never a
        download.

        Args:
            ctx: Carries ``existing_target`` and ``existing_external_id`` — the
                coordinates the last publication recorded.

        Returns:
            Whether the note is still where it was left.
        """
        return True

    def report_failure(self, summary: str) -> None:
        """Tell the user, at the destination, that the last sync did not finish.

        A CLI that fails in a terminal nobody is watching has told nobody, and
        a sync that quietly stops running is the failure mode a user discovers
        weeks later by noticing a notebook never arrived. A destination the
        user actually looks at can say so.

        A no-op by default, because "leave a message" is not something every
        destination can do — and a destination that cannot should not have to
        pretend. Silence here is a deliberate answer, not a missing one.

        Args:
            summary: What went wrong, in the user's words.
        """

    def clear_failure(self) -> None:
        """Withdraw a previous :meth:`report_failure`, if there was one.

        Called after every successful run, so the notice a destination left
        disappears by itself rather than being cleaned up by hand.
        """
