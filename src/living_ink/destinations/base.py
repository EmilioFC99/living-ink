"""The destination contract: the registry, the errors, and the ABC itself.

Everything every destination must honour lives here, and nothing else does.
How a body is rendered, whether attachments are separate objects, whether a
write is retried — all of that is the destination's own business.
"""

import abc
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional, Type

from living_ink.core.document import PublishResult
from living_ink.settings import Settings

logger = logging.getLogger(__name__)


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


class DestinationUnavailable(DestinationError):
    """The destination could not be reached; retrying later is sensible."""


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
    """

    config_key: ClassVar[str] = ""
    enabled_by_default: ClassVar[bool] = False
    state_key: ClassVar[str] = ""
    display_name: ClassVar[str] = ""

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
    def publish(
        self,
        notebook_name: str,
        text_content: str,
        image_paths: List[Path],
        sub_folder: Optional[str] = None,
        document_path: Optional[Path] = None,
        tags: Optional[List[str]] = None,
        existing_id: Optional[str] = None,
        adopt_by_name: bool = False,
        doc_id: Optional[str] = None,
        existing_target: Optional[str] = None,
        document_modified: Optional[str] = None,
        first_published: Optional[str] = None,
    ) -> PublishResult:
        """Publish a notebook to the destination.

        Args:
            notebook_name: Title of the notebook.
            text_content: The cleaned-up text content.
            image_paths: List of file paths to rendered page images.
            sub_folder: Optional relative sub-folder path (e.g., "Work/Projects").
            document_path: Optional path to underlying raw document (PDF or EPUB).
            tags: Optional list of tags associated with the notebook or its pages.
            existing_id: Identifier this destination returned the last time it
                published this notebook, if one was recorded. Replacing exactly
                that object is the only safe way to re-publish.
            adopt_by_name: Permission to fall back to matching on title when no
                ``existing_id`` is known. Only true when sync state says this
                notebook was published here before, which means the note with
                that title was almost certainly created by Living Ink.
            doc_id: The reMarkable document id. This is the note's identity —
                titles collide and change, document ids do not — so a
                destination that can record it alongside the note should.
            existing_target: Where this destination put the note last time, as
                it reported it in :attr:`PublishResult.target`. When the note
                belongs somewhere else now — the notebook was renamed or moved
                on the tablet — a destination that can move it should, rather
                than leaving a copy under the old name.
            document_modified: ``YYYY-MM-DD`` the user last wrote on this
                notebook, as the tablet reports it. This is the date worth
                sorting a library by, and it is not the date of the sync.
            first_published: ``YYYY-MM-DD`` this document first reached this
                destination, for a note that has to state when it came into
                existence and has no other record of it.

        Returns:
            The outcome, carrying where the note landed and any identifier the
            destination wants fed back to it next run.

        Raises:
            DestinationError: Publication failed for an expected reason. The
                message is user-facing and names the cause.
        """

    def unpublish(
        self,
        target: Optional[str] = None,
        external_id: Optional[str] = None,
        doc_id: Optional[str] = None,
    ) -> PublishResult:
        """Remove a note whose document no longer exists on the tablet.

        Only ever called for a document Living Ink published itself and can no
        longer find, and only when the user asked for it with ``--prune``. The
        default is to refuse: a destination that cannot prove which note is the
        right one must not delete any.

        Args:
            target: Where the note was recorded as landing.
            external_id: Identifier this destination reported for the note.
            doc_id: The reMarkable document id it was published for.

        Returns:
            The outcome. ``ok`` is False when nothing was removed, which is not
            an error — it is the default answer.

        Raises:
            DestinationError: Removal failed for an expected reason.
        """
        name = self.display_name or type(self).__name__
        logger.info("%s does not support removing notes.", name)
        return PublishResult(ok=False, detail=f"{name} cannot remove notes.")
