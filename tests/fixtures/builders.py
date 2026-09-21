"""Construct real reMarkable files for tests.

The suite used to assert against dictionaries standing in for a binary format
nobody parsed, which is why a format bug could never fail a test. These builders
emit the actual bytes instead: ``.rm`` pages through :func:`rmscene.write_blocks`,
and the ``.metadata`` / ``.content`` JSON in the shape the device writes them.

Everything here is deterministic. There is no clock, no ``uuid4`` and no
randomness, so the same call always produces the same bytes and a fixture can be
committed and diffed.

Four details of the on-device format are reproduced deliberately, because a
hand-written mock gets each of them wrong:

- ``lastModified`` is epoch **milliseconds** in a JSON *string*.
- The per-page modification key in ``cPages`` is misspelled ``modifed``.
- Page order is the order of ``cPages.pages``, not the filename sort order.
- A deleted page stays in ``cPages.pages`` carrying a ``deleted`` marker.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional, Sequence, Tuple

from rmscene import (
    AuthorIdsBlock,
    Block,
    CrdtId,
    CrdtSequence,
    CrdtSequenceItem,
    LwwValue,
    MigrationInfoBlock,
    PageInfoBlock,
    RootTextBlock,
    SceneGroupItemBlock,
    SceneLineItemBlock,
    SceneTreeBlock,
    TreeNodeBlock,
    write_blocks,
)
from rmscene import scene_items as si

#: Fixed author id. A real page carries a per-device UUID; pinning it is what
#: makes a generated ``.rm`` byte-identical across runs and machines.
AUTHOR_UUID = uuid.UUID("00000000-0000-4000-8000-000000000001")

#: The ``.rm`` format version these builders write, and the one the shipped
#: renderer supports. :func:`unsupported_version_bytes` writes a different one
#: on purpose.
RM_FORMAT_VERSION = 6

#: Header of a version-6 ``.rm`` file, padded to the width the format fixes.
RM_HEADER = b"reMarkable .lines file, version=%d          " % RM_FORMAT_VERSION

#: A point's coordinate space is centred on x=0, with y growing downward. These
#: bounds keep a generated stroke inside the page on every current panel.
PAGE_X_RANGE = (-700.0, 700.0)
PAGE_Y_RANGE = (0.0, 1870.0)

Points = Sequence[Tuple[float, float]]


def point(x: float, y: float, *, width: int = 20, pressure: int = 100) -> si.Point:
    """Build one sample of a stroke.

    Args:
        x: Horizontal position, centred on zero.
        y: Vertical position, growing downward from the top of the page.
        width: Nib width in device units.
        pressure: Stylus pressure, 0-255.

    Returns:
        A point the renderer can draw.
    """
    return si.Point(
        x=float(x),
        y=float(y),
        speed=0,
        direction=0,
        width=width,
        pressure=pressure,
    )


def stroke(
    points: Points,
    *,
    tool: si.Pen = si.Pen.FINELINER_2,
    color: si.PenColor = si.PenColor.BLACK,
    width: int = 20,
    pressure: int = 100,
) -> si.Line:
    """Build a single pen stroke from a list of ``(x, y)`` pairs.

    Args:
        points: The path, as ``(x, y)`` tuples in page coordinates.
        tool: Which pen drew it. ``HIGHLIGHTER_2`` and ``ERASER`` exercise
            render paths a fineliner does not.
        color: Ink colour.
        width: Nib width applied to every point.
        pressure: Stylus pressure applied to every point.

    Returns:
        A line the renderer can draw.
    """
    return si.Line(
        color=color,
        tool=tool,
        points=[point(x, y, width=width, pressure=pressure) for x, y in points],
        thickness_scale=1.0,
        starting_length=0.0,
        move_id=None,
    )


@dataclass(frozen=True)
class Layer:
    """One named layer of a page, holding the strokes drawn on it.

    Attributes:
        name: The layer label the tablet shows, e.g. ``"Layer 1"``.
        strokes: The strokes on this layer, in drawing order.
    """

    name: str
    strokes: Sequence[si.Line] = ()


def page_blocks(
    layers: Sequence[Layer] = (),
    *,
    text: Optional[str] = None,
) -> Iterator[Block]:
    """Emit the block stream for one page.

    Block order matters: the parser reads the tree before the items that hang
    off it. This is the order a real page uses.

    Args:
        layers: The layers to draw, outermost first. An empty sequence with no
            ``text`` produces a legitimately blank page.
        text: Typed text to place in a ``RootTextBlock``, as the tablet's
            keyboard and Type Folio produce. ``None`` means a page with no
            text layer at all.

    Yields:
        The blocks making up the page, in write order.
    """
    yield AuthorIdsBlock(author_uuids={1: AUTHOR_UUID})
    yield MigrationInfoBlock(migration_id=CrdtId(1, 1), is_device=True)
    yield PageInfoBlock(
        loads_count=1,
        merges_count=0,
        text_chars_count=len(text) + 1 if text else 0,
        text_lines_count=text.count("\n") + 1 if text else 0,
    )

    # One tree block per layer, each hanging off the root group (0, 1).
    for index in range(max(len(layers), 1)):
        yield SceneTreeBlock(
            tree_id=_layer_id(index),
            node_id=CrdtId(0, 0),
            is_update=True,
            parent_id=CrdtId(0, 1),
        )

    if text is not None:
        yield RootTextBlock(
            block_id=CrdtId(0, 0),
            value=si.Text(
                items=CrdtSequence(
                    [
                        CrdtSequenceItem(
                            item_id=CrdtId(1, 16),
                            left_id=CrdtId(0, 0),
                            right_id=CrdtId(0, 0),
                            deleted_length=0,
                            value=text,
                        )
                    ]
                ),
                styles={
                    CrdtId(0, 0): LwwValue(timestamp=CrdtId(1, 15), value=si.ParagraphStyle.PLAIN),
                },
                pos_x=-468.0,
                pos_y=234.0,
                width=936.0,
            ),
        )

    yield TreeNodeBlock(si.Group(node_id=CrdtId(0, 1)))

    effective = list(layers) or [Layer("Layer 1")]
    for index, layer in enumerate(effective):
        node = _layer_id(index)
        yield TreeNodeBlock(
            si.Group(
                node_id=node,
                label=LwwValue(timestamp=CrdtId(0, node.part2 + 1), value=layer.name),
            )
        )

    for index, layer in enumerate(effective):
        node = _layer_id(index)
        yield SceneGroupItemBlock(
            parent_id=CrdtId(0, 1),
            item=CrdtSequenceItem(
                item_id=CrdtId(0, node.part2 + 2),
                left_id=CrdtId(0, 0),
                right_id=CrdtId(0, 0),
                deleted_length=0,
                value=node,
            ),
        )

    # Items are a CRDT sequence: each one points at the one to its left, so the
    # ids have to be allocated in drawing order and threaded through.
    item_counter = 20
    for index, layer in enumerate(effective):
        node = _layer_id(index)
        left = CrdtId(0, 0)
        for line in layer.strokes:
            item_id = CrdtId(1, item_counter)
            item_counter += 1
            yield SceneLineItemBlock(
                parent_id=node,
                item=CrdtSequenceItem(
                    item_id=item_id,
                    left_id=left,
                    right_id=CrdtId(0, 0),
                    deleted_length=0,
                    value=line,
                ),
            )
            left = item_id


def _layer_id(index: int) -> CrdtId:
    """Return the node id for the nth layer.

    Layers are spaced ten apart so each one has room for its label and group
    item without colliding with the next.

    Args:
        index: Zero-based layer position.

    Returns:
        The CRDT id identifying that layer's group node.
    """
    return CrdtId(0, 11 + 10 * index)


def write_rm(
    path: Path,
    layers: Sequence[Layer] = (),
    *,
    text: Optional[str] = None,
) -> Path:
    """Write one page to a ``.rm`` file.

    Args:
        path: Destination file. Parent directories are created.
        layers: Layers to draw, as for :func:`page_blocks`.
        text: Typed text, as for :func:`page_blocks`.

    Returns:
        The path written, for chaining.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        write_blocks(handle, page_blocks(layers, text=text))
    return path


def truncated_rm_bytes(path: Path, *, keep: int = 60) -> Path:
    """Write a ``.rm`` file that stops mid-block.

    Models a page interrupted by a flat battery or a failed transfer: the
    header parses, so the file is not obviously junk, and the failure only
    surfaces once the reader is partway through the stream.

    Args:
        path: Destination file.
        keep: How many bytes of a valid page to retain.

    Returns:
        The path written, for chaining.
    """
    full = page_blocks([Layer("Layer 1", [stroke([(0, 100), (100, 200)])])])
    path.parent.mkdir(parents=True, exist_ok=True)
    import io

    buffer = io.BytesIO()
    write_blocks(buffer, full)
    path.write_bytes(buffer.getvalue()[:keep])
    return path


def unsupported_version_bytes(path: Path, *, version: int = 7) -> Path:
    """Write a ``.rm`` file declaring a format version this build cannot read.

    Args:
        path: Destination file.
        version: The version to claim in the header.

    Returns:
        The path written, for chaining.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    header = b"reMarkable .lines file, version=%d          " % version
    path.write_bytes(header + b"\x00" * 32)
    return path


@dataclass
class PageSpec:
    """One page of a document, as ``cPages`` describes it.

    Attributes:
        page_id: UUID naming the ``.rm`` file on disk.
        template: The paper template name the tablet recorded.
        deleted: Whether the page was removed. A removed page stays in
            ``cPages.pages`` with this marker rather than leaving the array.
        modified_ms: Page modification time, epoch milliseconds.
    """

    page_id: str
    template: str = "Blank"
    deleted: bool = False
    modified_ms: int = 1789777577123


@dataclass
class DocumentSpec:
    """A document in the on-device xochitl layout.

    Attributes:
        doc_id: Document UUID.
        name: Title, as ``visibleName``.
        pages: Pages in display order.
        parent: Containing folder UUID; empty at the root, ``"trash"`` when
            the document has been deleted.
        file_type: ``"notebook"``, ``"pdf"`` or ``"epub"``.
        tags: Document-level tags.
        page_tags: Page-level tags, as ``(page_id, tag)`` pairs.
        created_ms: Creation time, epoch milliseconds.
        modified_ms: Modification time, epoch milliseconds.
        deleted: Whether the document is marked deleted.
    """

    doc_id: str
    name: str
    pages: List[PageSpec] = field(default_factory=list)
    parent: str = ""
    file_type: str = "notebook"
    tags: List[str] = field(default_factory=list)
    page_tags: List[Tuple[str, str]] = field(default_factory=list)
    created_ms: int = 1789532931108
    modified_ms: int = 1789777577062
    deleted: bool = False

    def metadata(self) -> dict:
        """Build the ``.metadata`` payload.

        Returns:
            The dict the device serialises as ``<doc-uuid>.metadata``. Note
            that both timestamps are strings, not numbers.
        """
        return {
            "createdTime": str(self.created_ms),
            "deleted": self.deleted,
            "lastModified": str(self.modified_ms),
            "lastOpened": str(self.modified_ms),
            "lastOpenedPage": 0,
            "metadatamodified": False,
            "modified": False,
            "new": False,
            "parent": self.parent,
            "pinned": False,
            "source": "",
            "synced": False,
            "type": "DocumentType",
            "version": 0,
            "visibleName": self.name,
        }

    def content(self) -> dict:
        """Build the ``.content`` payload.

        Returns:
            The dict the device serialises as ``<doc-uuid>.content``, including
            the misspelled ``modifed`` key and the fractional page index.
        """
        pages = []
        for position, page in enumerate(self.pages):
            entry = {
                "id": page.page_id,
                "idx": {"timestamp": f"1:{position + 2}", "value": _fractional(position)},
                "modifed": str(page.modified_ms),
                "template": {"timestamp": "1:1", "value": page.template},
            }
            if page.deleted:
                entry["deleted"] = {"timestamp": f"1:{position + 2}", "value": True}
            pages.append(entry)

        return {
            "cPages": {
                "lastOpened": {"timestamp": "1:1", "value": ""},
                "original": {"timestamp": "1:1", "value": -1},
                "pages": pages,
                "uuids": [{"first": str(AUTHOR_UUID), "second": 1}],
            },
            "coverPageNumber": -1,
            "customZoomCenterX": 0,
            "customZoomCenterY": 936,
            "customZoomOrientation": "portrait",
            "customZoomPageHeight": 1872,
            "customZoomPageWidth": 1404,
            "customZoomScale": 1,
            "documentMetadata": {},
            "extraMetadata": {},
            "fileType": self.file_type,
            "fontName": "",
            "formatVersion": 2,
            "lineHeight": -1,
            "orientation": "portrait",
            "pageCount": len([p for p in self.pages if not p.deleted]),
            "pageTags": [
                {"name": tag, "pageId": page_id, "timestamp": self.modified_ms}
                for page_id, tag in self.page_tags
            ],
            "sizeInBytes": "0",
            "tags": [{"name": tag, "timestamp": self.modified_ms} for tag in self.tags],
            "textAlignment": "justify",
            "textScale": 1,
            "zoomMode": "bestFit",
        }


@dataclass
class FolderSpec:
    """A folder in the on-device xochitl layout.

    Attributes:
        folder_id: Folder UUID.
        name: Title, as ``visibleName``.
        parent: Containing folder UUID; empty at the root.
        created_ms: Creation time, epoch milliseconds.
    """

    folder_id: str
    name: str
    parent: str = ""
    created_ms: int = 1789532919615

    def metadata(self) -> dict:
        """Build the ``.metadata`` payload for a folder.

        Returns:
            The dict the device serialises. A folder has no ``.content`` file
            and fewer keys than a document.
        """
        return {
            "createdTime": str(self.created_ms),
            "lastModified": str(self.created_ms),
            "new": False,
            "parent": self.parent,
            "pinned": False,
            "source": "",
            "type": "CollectionType",
            "visibleName": self.name,
        }


def _fractional(position: int) -> str:
    """Return the nth value of the tablet's fractional page index.

    The device orders pages by a CRDT fractional index rendered as a short
    string — ``ba``, ``bb``, ``bc`` and so on. The exact alphabet does not
    matter to any reader, only that the values sort in page order and that a
    page's identity does not change when its neighbours move.

    Args:
        position: Zero-based page position.

    Returns:
        The index string for that position.
    """
    alphabet = "abcdefghijklmnopqrstuvwxyz"
    if position < len(alphabet):
        return f"b{alphabet[position]}"
    high, low = divmod(position, len(alphabet))
    return f"b{alphabet[high]}{alphabet[low]}"


def write_document(
    root: Path,
    spec: DocumentSpec,
    *,
    pages: Optional[Sequence[Sequence[Layer]]] = None,
    texts: Optional[Sequence[Optional[str]]] = None,
    raw_file: Optional[Tuple[str, bytes]] = None,
) -> Path:
    """Write a whole document into a xochitl-shaped directory.

    Args:
        root: The corpus directory to write into.
        spec: The document's metadata and page list.
        pages: Layers for each page, positionally matched to ``spec.pages``.
            A short sequence leaves the remaining pages blank.
        texts: Typed text for each page, positionally matched to ``spec.pages``.
        raw_file: ``(extension, bytes)`` for the source PDF or EPUB behind a
            file-backed document.

    Returns:
        The document directory that was written.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{spec.doc_id}.metadata").write_text(json.dumps(spec.metadata(), indent=4) + "\n")
    (root / f"{spec.doc_id}.content").write_text(json.dumps(spec.content(), indent=4) + "\n")

    doc_dir = root / spec.doc_id
    doc_dir.mkdir(parents=True, exist_ok=True)
    for index, page in enumerate(spec.pages):
        layers = list(pages[index]) if pages and index < len(pages) else []
        text = texts[index] if texts and index < len(texts) else None
        write_rm(doc_dir / f"{page.page_id}.rm", layers, text=text)

    if raw_file is not None:
        extension, payload = raw_file
        (root / f"{spec.doc_id}.{extension}").write_bytes(payload)

    return doc_dir


def write_folder(root: Path, spec: FolderSpec) -> Path:
    """Write a folder's metadata into a xochitl-shaped directory.

    Args:
        root: The corpus directory to write into.
        spec: The folder's metadata.

    Returns:
        The metadata file that was written.
    """
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{spec.folder_id}.metadata"
    path.write_text(json.dumps(spec.metadata(), indent=4) + "\n")
    return path
