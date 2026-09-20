"""Builders for the items a transport's listing is made of.

Both shipped transports hand back :class:`living_ink.models.Document`, so a
test that wants to say "a document called Notes, in a folder, changed since
last time" should say it in that shape and nothing else. Tests used to write
the dict the reMarkable API returns, spelled in rmapy's names — which worked
only because the model carried ``VissibleName`` / ``ID`` / ``Parent`` aliases
and the reader fell back to a dict lookup, so the fixtures were proving a shape
no transport produces.

``hash`` defaults to empty rather than to something plausible: a test that
cares whether a document has changed has to say so, and one that does not
should not be silently pinned to a content hash it never mentions.
"""

from datetime import datetime
from typing import Any, Optional

from living_ink.models import Document


def make_item(
    doc_id: str = "doc-1",
    name: str = "Notes",
    *,
    doc_type: str = "DocumentType",
    parent: str = "",
    content_hash: str = "",
    modified: Optional[datetime] = None,
    **fields: Any,
) -> Document:
    """Build one listing entry, the shape both transports hand back.

    Args:
        doc_id: The reMarkable UUID.
        name: The display title.
        doc_type: ``"DocumentType"`` or ``"CollectionType"``.
        parent: The containing folder's id; empty at the root.
        content_hash: The content hash, which is what decides whether the
            document has changed.
        modified: When the tablet says it was last written on.
        **fields: Anything else :class:`living_ink.models.Document` declares —
            ``deleted``, ``pinned``, ``synced``, ``files``, ``tags``.

    Returns:
        The document.
    """
    return Document(
        id=doc_id,
        hash=content_hash,
        name=name,
        doc_type=doc_type,
        parent=parent,
        last_modified=modified,
        **fields,
    )


def make_folder(doc_id: str = "f-1", name: str = "Work", **fields: Any) -> Document:
    """Build one listing entry for a folder.

    Args:
        doc_id: The reMarkable UUID.
        name: The folder's name.
        **fields: Anything :func:`make_item` accepts other than ``doc_type``.

    Returns:
        The folder.
    """
    return make_item(doc_id, name, doc_type="CollectionType", **fields)


__all__ = ["make_folder", "make_item"]
