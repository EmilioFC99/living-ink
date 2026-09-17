"""Shared data models for Living Ink.

Types here are deliberately transport-agnostic: both the Cloud client
(:mod:`living_ink.sync`) and the USB SSH client (:mod:`living_ink.ssh`) produce
them, and everything downstream — the pipeline, the destinations — consumes
them without caring which transport supplied them. That is what makes
:class:`living_ink.api.FallbackClient` able to switch transports mid-run.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class Document:
    """A document or folder in a reMarkable library.

    The ``VissibleName`` / ``ID`` / ``Parent`` / ``Type`` / ``ModifiedClient``
    properties exist because the pipeline reads items through
    ``pipeline.get_val()``, which speaks the original rmapy attribute names.
    They are read-only aliases, not separate state — do not add more.

    Attributes:
        id: reMarkable document UUID.
        hash: Content hash. Used by the Cloud transport to fetch blobs and by
            the sync-state log to decide whether a document changed.
        name: Human-visible title.
        doc_type: Either "DocumentType" or "CollectionType" (a folder).
        parent: UUID of the containing folder; empty string at the root.
        deleted: Whether the document sits in the trash.
        pinned: Whether the user favourited it.
        synced: False means the document is cloud-archived and its content is
            not present on the device. Only the SSH transport sets this.
        last_modified: Client-side modification timestamp, when known.
        size: Size in bytes, when known.
        files: Transport-specific blob index entries.
        tags: reMarkable tags attached to the document.
        local_path: On-device path to the document folder. SSH transport only;
            None for cloud documents.
    """

    id: str
    hash: str
    name: str
    doc_type: str
    parent: str = ""
    deleted: bool = False
    pinned: bool = False
    synced: bool = True
    last_modified: Optional[datetime] = None
    size: int = 0
    files: List[Dict[str, Any]] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    local_path: Optional[str] = None

    @property
    def is_folder(self) -> bool:
        """Whether this entry is a folder rather than a document."""
        return self.doc_type == "CollectionType"

    @property
    def VissibleName(self) -> str:
        """Alias for ``name`` (rmapy spelling, typo and all)."""
        return self.name

    @property
    def ID(self) -> str:
        """Alias for ``id`` (rmapy naming)."""
        return self.id

    @property
    def Parent(self) -> str:
        """Alias for ``parent`` (rmapy naming)."""
        return self.parent

    @property
    def Type(self) -> str:
        """Alias for ``doc_type`` (rmapy naming)."""
        return self.doc_type

    @property
    def ModifiedClient(self) -> Optional[datetime]:
        """Alias for ``last_modified`` (rmapy naming)."""
        return self.last_modified
