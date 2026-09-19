"""The destination contract: the registry, the errors, and the ABC itself.

Everything every destination must honour lives here, and nothing else does.
How a body is rendered, whether attachments are separate objects, whether a
write is retried — all of that is the destination's own business.
"""

import abc
import logging
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional, Type

from living_ink.core.document import PublishResult
from living_ink.settings import Settings

logger = logging.getLogger(__name__)


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
    """

    def decorator(cls: Type["Destination"]) -> Type["Destination"]:
        cls.config_key = config_key
        cls.enabled_by_default = enabled_by_default
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
    """

    config_key: ClassVar[str] = ""
    enabled_by_default: ClassVar[bool] = False

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
        return self.config_key or type(self).__name__

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
        name = type(self).__name__
        logger.info("%s does not support removing notes.", name)
        return PublishResult(ok=False, detail=f"{name} cannot remove notes.")
