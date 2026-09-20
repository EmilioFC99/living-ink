"""Reading the tablet's listing: the few facts every stage needs up front.

A transport hands back either a :class:`~living_ink.models.Document` or a raw
dict, spelled in rmapy's names (``VissibleName``, ``Parent``, ``ModifiedClient``).
Everything that has to answer "what is this document called, where does it sit,
has it changed, what type is it" before anything is downloaded reads it through
these functions.

They live here rather than in :mod:`living_ink.pipeline` because
:mod:`living_ink.core.selection` is the one place that decides what a run will
do, and a selector that had to import the pipeline to read a title would invert
the layering the import graph test enforces. :mod:`living_ink.pipeline`
re-exports them, so the call sites that already name them are unchanged.
"""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: The parent id the device gives a document the user has thrown away.
TRASH_PARENT = "trash"

#: What :func:`get_notebook_path` puts at the head of a trashed document's path.
TRASH_MARKER = "[TRASH]"


def get_val(item: Any, key: str) -> Any:
    """Safely get a property or dictionary key from a document item."""
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, getattr(item, key.lower(), None))


def document_version(item: Any) -> str:
    """Return the value that decides whether a document has changed.

    The content hash when the transport offers one, the version counter
    otherwise. Lives here rather than inline in the selection pass because
    ``sync --preview`` predicts that decision, and a preview that disagrees with
    the run it predicts is worse than no preview.

    Args:
        item: A document from the transport's listing.

    Returns:
        The content hash, or the version number as a string, or ``"1"``.
    """
    value = get_val(item, "hash")
    if value:
        return str(value)
    try:
        return str(int(get_val(item, "Version")))
    except (ValueError, TypeError):
        return "1"


def document_name(item: Any) -> str:
    """Return a document's display title, however the transport spells it.

    Args:
        item: A document from the transport's listing.

    Returns:
        The title, or an empty string when the item has none.
    """
    return str(
        get_val(item, "VissibleName")
        or get_val(item, "VisibleName")
        or getattr(item, "name", "")
        or ""
    ).strip()


def get_notebook_path(item: Any, id_map: Dict[str, Any]) -> str:
    """Construct the folder path for an item using the ID lookup map."""
    path = []
    current = item
    while get_val(current, "Parent"):
        parent_id = get_val(current, "Parent")
        if parent_id == TRASH_PARENT:
            path.insert(0, TRASH_MARKER)
            break
        parent = id_map.get(parent_id)
        if parent:
            parent_name = get_val(parent, "VissibleName") or get_val(parent, "VisibleName")
            path.insert(0, parent_name)
            current = parent
        else:
            break
    return " / ".join(path)


def is_trashed(item: Any, id_map: Dict[str, Any]) -> bool:
    """Report whether the user has thrown this document away.

    Two signals, because neither is reliable alone. The ``deleted`` flag is
    what the metadata is documented to carry — and a real Paper Pure listing
    returns trashed documents with **no ``deleted`` key at all**, so every one
    of them reads as ``False`` and a notebook the user deleted gets published.
    What the device actually sets is ``parent: "trash"``, on the document or on
    a folder above it.

    Args:
        item: A document from the transport's listing.
        id_map: Every listed item by id, for walking to the trashed ancestor.

    Returns:
        True if the document or any folder containing it is in the trash.
    """
    if bool(get_val(item, "deleted")):
        return True
    return get_notebook_path(item, id_map).startswith(TRASH_MARKER)


def normalize_path_str(path_str: str) -> str:
    """Normalize a path string by stripping whitespace around slashes and lowercasing."""
    parts = [p.strip().lower() for p in path_str.replace("\\", "/").split("/") if p.strip()]
    return "/".join(parts)


def matches_notebook_target(item: Any, target_str: str, id_map: Dict[str, Any]) -> bool:
    """Check if a document matches a target string by ID, name, or folder path.

    Args:
        item: Document item.
        target_str: Search target (name, folder path, or document UUID).
        id_map: Map of ID -> Document for resolving parent folders.

    Returns:
        True if the item matches the target.
    """
    t = target_str.strip()
    if not t:
        return False

    # 1. Exact ID match (case-insensitive)
    doc_id = str(get_val(item, "ID") or getattr(item, "id", "") or "").strip()
    if doc_id.lower() == t.lower():
        return True

    # 2. Name match (case-insensitive, exact or substring)
    name = document_name(item)
    if name.lower() == t.lower() or t.lower() in name.lower():
        return True

    # 3. Path match: e.g. "Work/Notes" or "Work / Notes"
    folder_path = get_notebook_path(item, id_map)
    if folder_path:
        full_spaced = f"{folder_path} / {name}"
        full_slash = f"{folder_path}/{name}"
        t_norm = normalize_path_str(t)
        norm_spaced = normalize_path_str(full_spaced)
        norm_slash = normalize_path_str(full_slash)
        if t_norm == norm_spaced or t_norm == norm_slash or t_norm in norm_spaced:
            return True

    return False


def get_document_type(item: Any, client: Optional[Any] = None) -> str:
    """Determine which registered source handles a document.

    Three signals, strongest first: the ``fileType`` the transport reports, the
    extension of a file listed against the document, and the extension of its
    display title. Every one of them is answered by
    :mod:`living_ink.sources`, so adding a document type does not mean editing
    this function.

    Args:
        item: The document/metadata item or dict.
        client: Optional API client to query for file type. Pass it whenever
            there is one: the two fallbacks below are extension matching, and
            a PDF whose title does not end in ``.pdf`` reads as a notebook.

    Returns:
        The name of a registered source — ``'notebook'``, ``'pdf'`` or
        ``'epub'`` today, and the fallback source's name when nothing matches.
    """
    # Function-level on purpose: core/ takes no module-level dependency on a
    # plugin package, and tests/test_layering.py enforces it.
    from living_ink.sources import fallback_source, source_for_file_type, source_for_filename

    if client is not None:
        try:
            source = source_for_file_type(client.get_file_type(item))
            if source:
                return source.name
        except Exception:
            # Deliberately broad. This is a probe against whichever transport
            # happens to be connected, and the filename fallback below answers
            # the question just as well. A type lookup must never be the reason
            # a document drops out of discovery.
            logger.debug("get_file_type probe failed", exc_info=True)

    files = get_val(item, "files") or []
    for f in files:
        fid = str(f.get("id") if isinstance(f, dict) else getattr(f, "id", ""))
        source = source_for_filename(fid)
        if source:
            return source.name

    source = source_for_filename(document_name(item))
    if source:
        return source.name

    return fallback_source().name
