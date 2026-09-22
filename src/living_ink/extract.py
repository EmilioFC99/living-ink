"""
Text extraction helpers for reMarkable documents.
"""

import hashlib
import io
import json
import logging
import re
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple, Union

import pymupdf as fitz  # PyMuPDF
from PIL import Image

logger = logging.getLogger(__name__)


#: Opening or rendering a document. PyMuPDF raises its own errors
#: (FileDataError and friends) as RuntimeError subclasses, a path it cannot
#: read as an OSError, and a page index or argument it does not like as a
#: ValueError; Pillow reports a broken image or an unsupported mode the same
#: way. None of these is worth more than "no image from this page".
_DOC_ERRORS = (RuntimeError, OSError, ValueError)

#: Reading one member out of a document archive: the archive is not a zip, the
#: member is missing, or it is encrypted. BadZipFile is not an OSError, so it
#: has to be named.
_ZIP_ERRORS = (OSError, zipfile.BadZipFile, RuntimeError)


@contextmanager
def quiet_mupdf() -> Iterator[None]:
    """Route MuPDF's own complaints into the debug log instead of stderr.

    MuPDF writes from C, straight past Python logging, so a book with sloppy
    stylesheets prints a wall of ``MuPDF error: syntax error: css syntax
    error`` in the middle of a sync that is going fine. The messages still
    matter when a document genuinely will not open, so they are drained and
    logged rather than dropped.

    Yields:
        None, with MuPDF's stderr output suppressed for the duration.
    """
    tools = fitz.TOOLS
    prior_errors = tools.mupdf_display_errors()
    prior_warnings = tools.mupdf_display_warnings()
    tools.mupdf_display_errors(False)
    tools.mupdf_display_warnings(False)
    tools.reset_mupdf_warnings()
    try:
        yield
    finally:
        messages = tools.mupdf_warnings()
        tools.mupdf_display_errors(prior_errors)
        tools.mupdf_display_warnings(prior_warnings)
        if messages:
            logger.debug("MuPDF said: %s", messages)


#: Parsing a ``.content`` or ``.metadata`` payload. JSONDecodeError and
#: UnicodeDecodeError are both ValueErrors; TypeError and KeyError cover JSON
#: that parsed but is not the shape the format promises.
_JSON_ERRORS = (ValueError, TypeError, KeyError)

# Margin around content when using content-based bounding box (in pixels)
CONTENT_MARGIN = 50

#: The first bytes of every ``.rm`` file: a fixed ASCII string ending in the
#: format version, padded to a constant width.
_RM_HEADER_PREFIX = b"reMarkable .lines file, version="

#: How much of a ``.rm`` file has to be read to learn its version.
_RM_HEADER_LENGTH = 43

#: Versions the installed parser claims to handle. ``rmscene`` accepts only
#: v6 (``rmscene.tagged_block_common.HEADER_V6``); the v3 and v5 files written
#: by firmware before 3.0 need a different library entirely. Rendering one
#: anyway produces an empty SVG rather than an error, which is the silent
#: failure this set exists to turn into a message.
SUPPORTED_RM_VERSIONS = frozenset({6})

#: SVG elements rmc emits for ink. An SVG carrying none of them drew nothing.
_SVG_INK_ELEMENTS = ("<path", "<polyline", "<line", "<text", "<image")


class RenderError(RuntimeError):
    """A page could not be rendered, for a reason worth telling the user.

    Distinct from the ``None`` the render functions return for the ordinary
    misses (no such page, no temp file). This is raised only when the cause is
    known and actionable, so the run report can name it instead of printing
    "failed to render page 3".
    """


class UnsupportedRmFormat(RenderError):
    """A ``.rm`` file declares a format version the parser does not support."""


class BlankRenderError(RenderError):
    """A page with strokes in it rendered to no ink at all."""


def read_rm_version(rm_file_path: Path) -> Optional[int]:
    """Read the format version a ``.rm`` file declares in its header.

    Args:
        rm_file_path: Path to the ``.rm`` file.

    Returns:
        The declared version, or None if the file is unreadable or does not
        carry a reMarkable lines header at all.
    """
    try:
        with open(rm_file_path, "rb") as f:
            header = f.read(_RM_HEADER_LENGTH)
    except OSError:
        logger.debug("Could not read a header from %s", rm_file_path, exc_info=True)
        return None

    if not header.startswith(_RM_HEADER_PREFIX):
        return None

    try:
        return int(header[len(_RM_HEADER_PREFIX) :].strip())
    except ValueError:
        logger.debug("Unparseable .rm version in %s: %r", rm_file_path, header)
        return None


@dataclass(frozen=True)
class RmPageStats:
    """What a ``.rm`` file's blocks say about the page, before rendering it.

    Attributes:
        strokes: Line items the parser understood.
        unreadable: Blocks the parser could not decode. ``rmscene`` does not
            raise on these — it wraps each one in an ``UnreadableBlock`` and
            carries on, and the scene builder then drops it silently. That is
            the exact path by which a page full of strokes renders to nothing.
    """

    strokes: int
    unreadable: int

    @property
    def has_content(self) -> bool:
        """Whether the page had anything the renderer was meant to draw."""
        return bool(self.strokes or self.unreadable)


def inspect_rm_bytes(rm_bytes: bytes) -> Optional[RmPageStats]:
    """Count what raw ``.rm`` bytes hold, without rendering them.

    Args:
        rm_bytes: Raw bytes of a ``.rm`` file.

    Returns:
        The page's block counts, or None if the bytes could not be inspected.
    """
    if not rm_bytes or not rm_bytes.startswith(b"reMarkable .lines file, version="):
        return None

    try:
        from rmscene import read_blocks
        from rmscene.scene_stream import SceneLineItemBlock, UnreadableBlock
    except ImportError:
        return None

    strokes = 0
    unreadable = 0
    try:
        for block in read_blocks(io.BytesIO(rm_bytes)):
            if isinstance(block, UnreadableBlock):
                unreadable += 1
            elif isinstance(block, SceneLineItemBlock) and block.item.value is not None:
                strokes += 1
    except Exception:
        logger.debug("Could not inspect .rm bytes", exc_info=True)
        return None

    return RmPageStats(strokes=strokes, unreadable=unreadable)


def inspect_rm_page(rm_file_path: Path) -> Optional[RmPageStats]:
    """Count what a ``.rm`` file holds, without rendering it.

    This is the second half of the blank-page question. An SVG with no ink is
    only a bug if the source had something to draw, and the only way to know
    that is to look at the source rather than at the picture.

    Args:
        rm_file_path: Path to the ``.rm`` file.

    Returns:
        The page's block counts, or None if the file could not be inspected —
        in which case the caller must not conclude anything from it.
    """
    try:
        data = rm_file_path.read_bytes()
    except OSError:
        logger.debug("Could not read %s to inspect", rm_file_path, exc_info=True)
        return None

    return inspect_rm_bytes(data)


def _svg_has_ink(svg_path: Path) -> bool:
    """Report whether an SVG contains any drawing element at all.

    Args:
        svg_path: Path to the SVG rmc produced.

    Returns:
        True if the markup holds at least one ink element. An unreadable file
        reads as True, so a failure here can never be mistaken for a blank
        page.
    """
    try:
        markup = svg_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        logger.debug("Could not read back %s", svg_path, exc_info=True)
        return True

    return any(element in markup for element in _SVG_INK_ELEMENTS)


def is_image_blank(
    image: Union[Path, str, Image.Image],
    tolerance: int = 3,
) -> bool:
    """Report whether an image is completely blank (uniform colour or fully transparent).

    Args:
        image: Path to an image file or an open PIL Image.
        tolerance: Maximum difference between min and max brightness in grayscale.
            Defaults to 3, allowing for minor compression or antialiasing noise.

    Returns:
        True if the image contains no visible strokes, drawings or text.
    """
    if isinstance(image, (str, Path)):
        try:
            im = Image.open(image)
        except Exception:
            logger.debug("Could not open image to check blankness: %s", image, exc_info=True)
            return False
    else:
        im = image

    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im_rgba = im.convert("RGBA")
        alpha = im_rgba.getchannel("A")
        _, max_a = alpha.getextrema()
        if max_a == 0:
            return True
        bg = Image.new("RGBA", im_rgba.size, (255, 255, 255, 255))
        bg.paste(im_rgba, (0, 0), im_rgba)
        im = bg.convert("RGB")

    gray = im.convert("L")
    min_val, max_val = gray.getextrema()
    return (max_val - min_val) <= tolerance


def extract_text_from_pdf(pdf_path: Path) -> str:
    """
    Extract text from a PDF file using PyMuPDF.

    Returns the full text content of the PDF.
    """
    try:
        import pymupdf as fitz  # PyMuPDF

        text_parts = []
        with quiet_mupdf(), fitz.open(pdf_path) as doc:
            for page_num, page in enumerate(doc, 1):
                page_text = page.get_text()
                if page_text.strip():
                    p_header = format_page_section_header(page_num, pdf_path, include_divider=True)
                    text_parts.append(f"{p_header}\n\n{page_text.strip()}")

        return "\n\n".join(text_parts) if text_parts else ""
    except ImportError:
        return ""
    except _DOC_ERRORS:
        logger.debug("Failed to extract text from %s", pdf_path, exc_info=True)
        return ""


def extract_text_from_epub(epub_path: Path) -> str:
    """
    Extract text from an EPUB file.

    Returns the full text content of the EPUB.
    """
    try:
        from bs4 import BeautifulSoup
        from ebooklib import ITEM_DOCUMENT, epub

        book = epub.read_epub(str(epub_path), options={"ignore_ncx": True})
        text_parts = []

        for item in book.get_items():
            if item.get_type() == ITEM_DOCUMENT:
                soup = BeautifulSoup(item.get_content(), "html.parser")
                # Get text, preserving some structure
                text = soup.get_text(separator="\n", strip=True)
                if text:
                    text_parts.append(text)

        return "\n\n".join(text_parts) if text_parts else ""
    except ImportError:
        return ""
    except Exception:
        # Deliberately broad. ebooklib and BeautifulSoup are optional imports,
        # so their exception types cannot be named at module scope, and both
        # raise freely on a malformed book. An EPUB that will not parse means
        # no embedded text, not a failed run.
        logger.debug("Failed to extract text from %s", epub_path, exc_info=True)
        return ""


def extract_raw_document_from_zip(
    zip_path: Path, out_path: Path, suffixes: Optional[Sequence[str]] = None
) -> Optional[Path]:
    """Extract the original document stored inside a reMarkable document zip.

    Args:
        zip_path: Path to the downloaded document zip archive.
        out_path: Destination path for the extracted raw document.
        suffixes: Which extensions count as the original document, without the
            dot ("pdf", "epub"). Defaults to every suffix the source registry
            declares, so registering a source is enough to have its files
            pulled out of the zip. Pass an explicit tuple to narrow it — a
            renderer that would rather not accept a neighbouring format.

    Returns:
        Path to the extracted file, or None if the archive holds none.
    """
    if suffixes is None:
        # Call-time import: `sources` imports this module, so taking the
        # dependency at module level would close the cycle.
        from living_ink.sources import source_suffixes

        suffixes = source_suffixes()
    wanted = tuple(f".{s.lower().lstrip('.')}" for s in suffixes)
    if not wanted:
        return None

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for name in zf.namelist():
                lower_name = name.lower()
                if lower_name.endswith(wanted):
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(out_path, "wb") as f:
                        f.write(zf.read(name))
                    return out_path
    except _ZIP_ERRORS as e:
        logger.debug(f"Failed to extract raw document from {zip_path}: {e}")
    return None


def get_pdf_annotated_page_map(zip_path: Path) -> List[Dict[str, Any]]:
    """Parse a document zip and find all annotated pages with their PDF page index.

    An annotation the user deleted on the tablet is left out, the same way
    :func:`_get_ordered_rm_files` leaves out a deleted notebook page.

    Args:
        zip_path: Path to the document zip file.

    Returns:
        List of dicts with keys:
            - page_id: UUID of the page
            - pdf_page_index: 0-indexed page in the underlying PDF (or None)
            - page_num: 1-indexed human-readable page number
            - rm_file_name: Name of the .rm file inside the zip
    """
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            namelist = zf.namelist()
            rm_names = {Path(n).name: n for n in namelist if n.endswith(".rm")}
            if not rm_names:
                return []

            # Find .content file
            content_data = {}
            for n in namelist:
                if n.endswith(".content"):
                    try:
                        content_data = json.loads(zf.read(n).decode("utf-8"))
                    except (*_ZIP_ERRORS, *_JSON_ERRORS):
                        pass
                    break

            pages_meta = []
            if "cPages" in content_data and "pages" in content_data["cPages"]:
                pages_meta = content_data["cPages"]["pages"]
            elif "pages" in content_data and isinstance(content_data["pages"], list):
                pages_meta = content_data["pages"]

            results = []
            matched_rm_names = set()

            for idx, p in enumerate(pages_meta):
                p_id = p.get("id") if isinstance(p, dict) else str(p)
                rm_key = f"{p_id}.rm"
                if page_is_deleted(p):
                    # Claim it so the orphan sweep below does not hand the same
                    # annotation back under a synthesised page number.
                    matched_rm_names.add(rm_key)
                    continue
                if rm_key in rm_names:
                    matched_rm_names.add(rm_key)
                    redir_val = None
                    if isinstance(p, dict) and "redir" in p:
                        redir_entry = p["redir"]
                        if isinstance(redir_entry, dict):
                            redir_val = redir_entry.get("value")
                        elif isinstance(redir_entry, int):
                            redir_val = redir_entry

                    pdf_idx = redir_val if redir_val is not None else idx
                    results.append(
                        {
                            "page_id": p_id,
                            "pdf_page_index": pdf_idx,
                            "page_num": pdf_idx + 1,
                            "rm_file_name": rm_names[rm_key],
                        }
                    )

            # Add any orphaned .rm files not explicitly in cPages
            for rm_k, rm_full in rm_names.items():
                if rm_k not in matched_rm_names:
                    p_id = rm_k[:-3]
                    results.append(
                        {
                            "page_id": p_id,
                            "pdf_page_index": len(results),
                            "page_num": len(results) + 1,
                            "rm_file_name": rm_full,
                        }
                    )

            # Sort by pdf_page_index
            results.sort(key=lambda x: x["pdf_page_index"])
            return results
    except (*_ZIP_ERRORS, *_JSON_ERRORS) as e:
        logger.debug(f"Failed to read page map from {zip_path}: {e}")
        return []


def render_rm_for_pdf_page(
    rm_file_path: Path,
    pdf_width: float,
    pdf_height: float,
    dpi: int = 150,
) -> Optional[Image.Image]:
    """Render handwritten .rm strokes mapped to a PDF page's coordinate space.

    On a reMarkable tablet, pen strokes on a PDF page are anchored in PDF point
    space (72 DPI), with the horizontal axis centered at zero (``x = 0`` is
    ``pdf_width / 2``) and the vertical axis starting at ``y = 0`` at the top of
    the page.

    Args:
        rm_file_path: Path to the .rm file.
        pdf_width: Width of the PDF page in points.
        pdf_height: Height of the PDF page in points.
        dpi: Resolution for rasterizing the stroke overlay.

    Returns:
        RGBA PIL Image matching the rendered resolution of the PDF page, or None
        if the page has no strokes or could not be rendered.

    Raises:
        UnsupportedRmFormat: If the file declares an unsupported .rm format.
        BlankRenderError: If a page with strokes rendered to empty markup.
    """
    version = read_rm_version(rm_file_path)
    if version is not None and version not in SUPPORTED_RM_VERSIONS:
        supported = ", ".join(str(v) for v in sorted(SUPPORTED_RM_VERSIONS))
        raise UnsupportedRmFormat(
            f"{rm_file_path.name} is .rm format version {version}; this build reads "
            f"version {supported}. Upgrade living-ink, or file an issue naming the "
            f"version and your firmware."
        )

    tmp_svg_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as tmp_svg:
            tmp_svg_path = Path(tmp_svg.name)

        try:
            _patch_rmc()
            from rmc.exporters.svg import rm_to_svg

            rm_to_svg(str(rm_file_path), str(tmp_svg_path))
        except Exception as e:
            logger.warning("rm_to_svg failed for %s: %s", rm_file_path, e, exc_info=True)
            return None

        if not tmp_svg_path.exists() or tmp_svg_path.stat().st_size == 0:
            return None

        if not _svg_has_ink(tmp_svg_path):
            stats = inspect_rm_page(rm_file_path)
            if stats is not None and stats.has_content:
                held = f"{stats.strokes} strokes"
                if stats.unreadable:
                    held += f" and {stats.unreadable} blocks this build cannot decode"
                raise BlankRenderError(
                    f"{rm_file_path.name} holds {held} but rendered to an empty image. "
                    f"Known cause: an rmc/rmscene version that cannot draw what this "
                    f"firmware wrote — try upgrading living-ink."
                )
            return None

        svg_text = tmp_svg_path.read_text(encoding="utf-8", errors="replace")
        new_header = (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'height="{pdf_height}" width="{pdf_width}" '
            f'viewBox="-{pdf_width / 2.0} 0.0 {pdf_width} {pdf_height}">'
        )
        modified_svg = re.sub(r"<svg[^>]+>", new_header, svg_text, count=1)

        try:
            with (
                quiet_mupdf(),
                fitz.open(stream=modified_svg.encode("utf-8"), filetype="svg") as svg_doc,
            ):
                svg_page = svg_doc[0]
                svg_pix = svg_page.get_pixmap(dpi=dpi, alpha=True)
                return Image.frombytes("RGBA", [svg_pix.width, svg_pix.height], svg_pix.samples)
        except _DOC_ERRORS as e:
            logger.warning("Failed to rasterize SVG stroke overlay for %s: %s", rm_file_path, e)
            return None
    finally:
        if tmp_svg_path is not None:
            tmp_svg_path.unlink(missing_ok=True)


def render_composite_pdf_page(
    pdf_path: Path,
    page_index: int,
    rm_bytes: bytes,
    dpi: int = 150,
    screen: Optional[Tuple[int, int]] = None,
) -> Optional[bytes]:
    """Render a PDF page with handwritten .rm strokes composited on top.

    Args:
        pdf_path: Path to the source PDF document.
        page_index: 0-indexed page number in the PDF.
        rm_bytes: Raw bytes of the .rm pen stroke file.
        dpi: Resolution for rendering the PDF page.
        screen: Panel size of the tablet that drew the annotations. Kept for
            API compatibility.

    Returns:
        PNG image bytes of the composite page, or None if rendering failed.
    """
    try:
        with quiet_mupdf(), fitz.open(str(pdf_path)) as doc:
            if page_index < 0 or page_index >= len(doc):
                return None
            page = doc[page_index]
            pix = page.get_pixmap(dpi=dpi)
            pdf_img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            page_w = page.rect.width
            page_h = page.rect.height

        if not rm_bytes:
            out_buf = io.BytesIO()
            pdf_img.save(out_buf, format="PNG")
            return out_buf.getvalue()

        # Render the .rm file mapped to PDF coordinate space
        with tempfile.NamedTemporaryFile(suffix=".rm", delete=False) as f:
            f.write(rm_bytes)
            tmp_rm = Path(f.name)

        try:
            rm_overlay = render_rm_for_pdf_page(tmp_rm, page_w, page_h, dpi=dpi)
        except RenderError as e:
            # The PDF page underneath is still worth having, and still worth
            # transcribing. Losing the annotation layer is not losing the page.
            logger.warning("Annotation layer on PDF page %s: %s", page_index, e)
            rm_overlay = None
        finally:
            tmp_rm.unlink(missing_ok=True)

        if rm_overlay is None or is_image_blank(rm_overlay):
            # No visible annotations on this PDF page. Return None so bare PDF
            # pages are not sent to AI vision OCR.
            return None

        if (rm_overlay.width, rm_overlay.height) != (pdf_img.width, pdf_img.height):
            rm_overlay = rm_overlay.resize(
                (pdf_img.width, pdf_img.height), Image.Resampling.LANCZOS
            )
        pdf_img.paste(rm_overlay, (0, 0), rm_overlay)

        out_buf = io.BytesIO()
        pdf_img.save(out_buf, format="PNG")
        return out_buf.getvalue()
    except _DOC_ERRORS as e:
        logger.debug(f"Failed to render composite PDF page {page_index}: {e}")
        return None


def _parse_hex_color(hex_color: str) -> tuple:
    """Parse a hex color string to RGBA tuple.

    Supports #RRGGBB (RGB) and #RRGGBBAA (RGBA) formats.

    Args:
        hex_color: Hex color string (e.g., "#FFFFFF" or "#FFFFFF80")

    Returns:
        Tuple of (r, g, b, a) values (0-255)
    """
    if not hex_color.startswith("#"):
        return (255, 255, 255, 255)

    hex_str = hex_color.lstrip("#")
    if len(hex_str) == 6:
        r, g, b = tuple(int(hex_str[i : i + 2], 16) for i in (0, 2, 4))
        return (r, g, b, 255)
    elif len(hex_str) == 8:
        r, g, b, a = tuple(int(hex_str[i : i + 2], 16) for i in (0, 2, 4, 6))
        return (r, g, b, a)
    else:
        return (255, 255, 255, 255)


def _get_svg_content_bounds(svg_path: Path) -> Optional[tuple]:
    """
    Parse SVG file to get the content bounding box from viewBox.

    Args:
        svg_path: Path to the SVG file

    Returns:
        Tuple of (min_x, min_y, width, height) or None if not determinable
    """
    import xml.etree.ElementTree as ET

    try:
        tree = ET.parse(svg_path)
        root = tree.getroot()

        # Try to get viewBox attribute
        viewbox = root.get("viewBox")
        if viewbox:
            parts = viewbox.split()
            if len(parts) == 4:
                return (float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]))

        # Fallback to width/height attributes
        width = root.get("width")
        height = root.get("height")
        if width and height:
            # Remove 'px' suffix if present
            w = float(width.replace("px", ""))
            h = float(height.replace("px", ""))
            return (0, 0, w, h)

        return None
    except (ValueError, TypeError, AttributeError):
        # An SVG whose viewBox or size attributes are absent or not numbers.
        return None


def _toposort_items(items: Any) -> Iterator[Any]:
    """Order CRDT sequence items by their left/right links, in linear time.

    A drop-in replacement for ``rmscene.crdt_sequence.toposort_items``. The
    upstream implementation is a layered topological sort that rebuilds its
    entire dependency dictionary once per layer. A run of typed text is one
    long chain — every character depends on the one before it — so it has as
    many layers as it has characters, and the sort costs O(n²). A 32 KB page
    of typed text took 295 seconds to render on the machine this was written
    on, almost all of it inside that loop; the same notebook's 58 KB page of
    pure handwriting takes 0.02 s, because ink is a shallow tree rather than a
    chain.

    This walks the same layers in the same order and yields the same ids — the
    tie-break inside a layer is still ``sorted()`` — but it keeps a count of
    unmet dependencies per node and a reverse index of who is waiting on whom,
    so each edge is visited once instead of once per layer.

    Args:
        items: The ``CrdtSequenceItem`` objects to order.

    Yields:
        The ``CrdtId`` of each item, in sequence order. Ids the items refer to
        but do not contain (the start and end sentinels) are not yielded.

    Raises:
        ValueError: If the links do not form a sequence — a cycle, or a set of
            items that cannot all be reached. Upstream raises ``ValueError``
            for the first and trips an ``assert`` for the second; both are the
            same corrupt page, so both are reported the same way.
    """
    from rmscene.crdt_sequence import END_MARKER

    item_dict = {item.item_id: item for item in items}
    if not item_dict:
        return

    def side_id(item: Any, side: str) -> Any:
        value = getattr(item, f"{side}_id")
        if value == END_MARKER or value not in item_dict:
            if value != END_MARKER:
                logger.debug("Ignoring unknown %s_id %s of %s", side, value, item)
            return "__start" if side == "left" else "__end"
        return value

    # waiting_on[node] is the set of nodes that must be yielded before it.
    waiting_on: Dict[Any, set] = {}
    for item in item_dict.values():
        waiting_on.setdefault(item.item_id, set()).add(side_id(item, "left"))
        waiting_on.setdefault(side_id(item, "right"), set()).add(item.item_id)
    for node in [dep for deps in waiting_on.values() for dep in deps]:
        waiting_on.setdefault(node, set())

    # The reverse index, built once. Rebuilding it per layer is the upstream
    # cost this function exists to avoid.
    unblocks: Dict[Any, List[Any]] = {}
    remaining = {}
    for node, deps in waiting_on.items():
        remaining[node] = len(deps)
        for dep in deps:
            unblocks.setdefault(dep, []).append(node)

    layer = [node for node, count in remaining.items() if count == 0]
    settled = 0
    while layer:
        if len(layer) == 1 and layer[0] == "__end":
            settled += 1
            break
        yield from sorted(node for node in layer if node in item_dict)
        nxt: List[Any] = []
        for node in layer:
            settled += 1
            for dependent in unblocks.get(node, ()):
                remaining[dependent] -= 1
                if remaining[dependent] == 0:
                    nxt.append(dependent)
        layer = nxt

    if settled != len(remaining):
        raise ValueError("cyclic dependency")


#: ``_patch_rmc`` replaces bound attributes with wrappers around whatever it
#: found there, so running it twice wraps the wrapper. Rendering calls it once
#: per page, which used to nest a new ``Pen.create`` a page deep.
_rmc_patched = False


def _patch_rmc() -> None:
    """Make rmc survive unknown pen colours and export typed text in linear time.

    Upstream rmc omits PenColor.HIGHLIGHT (value 9) from RM_PALETTE, which causes
    KeyError: 9 when converting notes that use the highlighter. This function
    ensures all colors have a fallback and unknown pen types default to Ballpoint.
    It also swaps rmscene's layered topological sort for :func:`_toposort_items`,
    which yields the same order without the quadratic rebuild — see that
    function for what the difference costs on a page of typed text.

    Calling this more than once is a no-op: every patch here wraps or replaces
    what it found, so a second pass would wrap its own output.
    """
    global _rmc_patched
    if _rmc_patched:
        return

    try:
        import rmc.exporters.writing_tools as wt
        import rmscene.crdt_sequence as cs
        import rmscene.scene_items as si

        class SafePalette(dict):
            def __missing__(self, key):
                return (0, 0, 0)

        palette = dict(wt.RM_PALETTE)
        palette[9] = (251, 247, 25)  # Standard highlighter yellow
        if hasattr(si.PenColor, "HIGHLIGHT"):
            palette[si.PenColor.HIGHLIGHT] = (251, 247, 25)

        wt.RM_PALETTE = SafePalette(palette)

        orig_create = wt.Pen.create

        @classmethod
        def safe_create(cls, pen_nr, color_id, width):
            try:
                return orig_create(pen_nr, color_id, width)
            except Exception:  # noqa: BLE001 - the whole point of the patch
                from rmc.exporters.writing_tools import Ballpoint

                return Ballpoint(width, color_id)

        wt.Pen.create = safe_create

        # CrdtSequence.__iter__ reads this by module global, so rebinding the
        # name is enough; there is no other importer of it in rmscene.
        cs.toposort_items = _toposort_items

        import textwrap

        import rmc.exporters.svg as rmc_svg

        def _wrap_paragraph(p: Any, width: int = 64) -> List[Tuple[str, List[Any], Any]]:
            chars: List[str] = []
            ids: List[Any] = []
            for subp in p.contents:
                for ch, cid in zip(subp.s, subp.i):
                    chars.append(ch)
                    ids.append(cid)

            full_text = "".join(chars)
            if not full_text.strip():
                return [("", [], p.start_id)]

            wrapped_lines = textwrap.wrap(
                full_text, width=width, break_long_words=False, break_on_hyphens=False
            )
            if not wrapped_lines:
                return [("", [], p.start_id)]

            result = []
            curr_idx = 0
            for line_str in wrapped_lines:
                start = full_text.find(line_str, curr_idx)
                if start == -1:
                    start = curr_idx
                end = start + len(line_str)
                curr_idx = end
                line_ids = ids[start:end]
                first_id = ids[start] if start < len(ids) else p.start_id
                result.append((line_str, line_ids, first_id))

            return result

        def safe_build_anchor_pos(text: Any) -> Dict[Any, int]:
            anchor_pos = {
                si.CrdtId(0, 281474976710654): 100,
                si.CrdtId(0, 281474976710655): 100,
            }
            if text is not None:
                doc = rmc_svg.TextDocument.from_scene_item(text)
                ypos = text.pos_y + rmc_svg.TEXT_TOP_Y
                for p in doc.contents:
                    lh = rmc_svg.LINE_HEIGHTS.get(p.style.value, 70)
                    wrap_w = 35 if p.style.value == si.ParagraphStyle.HEADING else 64
                    lines = _wrap_paragraph(p, width=wrap_w)
                    for _, line_ids, first_id in lines:
                        ypos += lh
                        anchor_pos[first_id] = ypos
                        for cid in line_ids:
                            anchor_pos[cid] = ypos
            return anchor_pos

        def safe_draw_text(text: si.Text, output: Any) -> None:
            output.write('\t\t<g class="root-text" style="display:inline">')
            output.write("""
            <style>
                text.heading {
                    font: 14pt serif;
                }
                text.bold {
                    font: 8pt sans-serif bold;
                }
                text, text.plain {
                    font: 7pt sans-serif;
                }
            </style>
""")
            y_offset = rmc_svg.TEXT_TOP_Y
            doc = rmc_svg.TextDocument.from_scene_item(text)
            for p in doc.contents:
                lh = rmc_svg.LINE_HEIGHTS.get(p.style.value, 70)
                cls = p.style.value.name.lower()
                wrap_w = 35 if p.style.value == si.ParagraphStyle.HEADING else 64
                lines = _wrap_paragraph(p, width=wrap_w)
                for line_str, _, _ in lines:
                    y_offset += lh
                    xpos = text.pos_x
                    ypos = text.pos_y + y_offset
                    if line_str.strip():
                        escaped = (
                            line_str.replace("&", "&amp;")
                            .replace("<", "&lt;")
                            .replace(">", "&gt;")
                            .replace('"', "&quot;")
                        )
                        output.write(
                            f'\t\t\t<text x="{rmc_svg.xx(xpos)}" y="{rmc_svg.yy(ypos)}" '
                            f'class="{cls}">{escaped}</text>\n'
                        )
            output.write("\t\t</g>\n")

        rmc_svg.build_anchor_pos = safe_build_anchor_pos
        rmc_svg.draw_text = safe_draw_text
    except (ImportError, AttributeError):
        # rmc or rmscene is absent, or an upgrade moved what this patches.
        logger.debug("Could not patch rmc", exc_info=True)
        return

    _rmc_patched = True


def output_size(
    bounds: Optional[Tuple[float, float, float, float]],
    screen: Optional[Tuple[int, int]] = None,
) -> Tuple[int, int]:
    """Decide how large a rendered page should be, in pixels.

    Content bounds win when the SVG has them: a page holding two words should
    not be rasterised as a whole empty sheet. When it has none, the page is
    sized as a sheet of the tablet that drew it — which is why this takes a
    panel rather than reading a module constant. A Paper Pro page is
    1620×2160, and rendering it at reMarkable 2 size is how a page comes back
    squashed.

    Args:
        bounds: ``(x, y, width, height)`` of the ink in the SVG, or None when
            the SVG declares none.
        screen: Panel size of the device that drew the page. Defaults to
            :data:`~living_ink.devices.DEFAULT_PROFILE`'s panel, which is named
            rather than assumed.

    Returns:
        ``(width, height)`` in pixels.
    """
    if bounds:
        _, _, content_width, content_height = bounds
        return (
            int(content_width) + 2 * CONTENT_MARGIN,
            int(content_height) + 2 * CONTENT_MARGIN,
        )

    from living_ink.devices import DEFAULT_PROFILE

    return screen or DEFAULT_PROFILE.screen


def render_rm_file_to_png(
    rm_file_path: Path,
    background_color: Optional[str] = None,
    screen: Optional[Tuple[int, int]] = None,
) -> Optional[bytes]:
    """
    Render a .rm file to PNG image bytes.

    Uses rmc to convert .rm to SVG, then PyMuPDF to rasterise it to PNG.
    The output is sized based on the SVG content bounds with a margin.

    Args:
        rm_file_path: Path to the .rm file
        background_color: Background color (e.g., "#FFFFFF", "transparent", None).
                         None means transparent. The paper colour a sync renders
                         on is ``Settings.render_background``; this module does
                         not read it, so a caller says which colour it wants.
        screen: Panel size of the tablet that drew the page, as
            ``(width, height)``. Only used when the SVG carries no content
            bounds to size the output from — a blank-ish page is then a whole
            sheet of *that* device rather than of a reMarkable 2. Defaults to
            :data:`~living_ink.devices.DEFAULT_PROFILE`'s panel.

    Returns:
        PNG image bytes, or None if rendering failed

    Raises:
        UnsupportedRmFormat: If the file declares a format version the
            installed parser does not support.
        BlankRenderError: If a page that contains strokes rendered to no ink.
    """
    import subprocess
    import tempfile

    version = read_rm_version(rm_file_path)
    if version is not None and version not in SUPPORTED_RM_VERSIONS:
        supported = ", ".join(str(v) for v in sorted(SUPPORTED_RM_VERSIONS))
        raise UnsupportedRmFormat(
            f"{rm_file_path.name} is .rm format version {version}; this build reads "
            f"version {supported}. Upgrade living-ink, or file an issue naming the "
            f"version and your firmware."
        )

    tmp_svg_path = None
    tmp_png_path = None
    tmp_raw_path = None

    try:
        # Create temp files
        with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as tmp_svg:
            tmp_svg_path = Path(tmp_svg.name)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_png:
            tmp_png_path = Path(tmp_png.name)

        # Convert .rm to SVG using rmc (direct library call to avoid subprocess issues)
        try:
            _patch_rmc()
            from rmc.exporters.svg import rm_to_svg

            rm_to_svg(str(rm_file_path), str(tmp_svg_path))
        except Exception as e:
            # Deliberately broad. rmc walks a binary format written by whatever
            # firmware drew the page and raises whatever its parser hits; one
            # unreadable page must not end the notebook.
            # Warned, not printed: the page comes back as an error page, so
            # the run report is where the user hears about it. A bare print
            # here also landed in the middle of a ``--json`` document.
            logger.warning("rm_to_svg failed for %s: %s", rm_file_path, e, exc_info=True)
            return None

        # Check if the file was actually created and has content
        if not tmp_svg_path.exists() or tmp_svg_path.stat().st_size == 0:
            return None

        # A page the user left blank is legal and renders to nothing. A page
        # with strokes that renders to nothing is a parser or exporter
        # mismatch, and used to reach the destination as an empty note.
        if not _svg_has_ink(tmp_svg_path):
            stats = inspect_rm_page(rm_file_path)
            if stats is not None and stats.has_content:
                held = f"{stats.strokes} strokes"
                if stats.unreadable:
                    held += f" and {stats.unreadable} blocks this build cannot decode"
                raise BlankRenderError(
                    f"{rm_file_path.name} holds {held} but rendered to an empty image. "
                    f"Known cause: an rmc/rmscene version that cannot draw what this "
                    f"firmware wrote — try upgrading living-ink."
                )

        # Get content bounds from SVG
        output_width, output_height = output_size(
            _get_svg_content_bounds(tmp_svg_path), screen=screen
        )

        # Convert SVG to PNG using PyMuPDF (fitz) to avoid system dependencies like cairo
        try:
            # Load the SVG into a PDF/Document object
            doc = fitz.open(tmp_svg_path)
            page = doc[0]  # SVGs are single page

            # Determine Scaling
            # We want the output to match output_width/height
            # Rect is usually (0, 0, w, h)
            rect = page.rect
            zoom_x = output_width / rect.width if rect.width > 0 else 1.0
            zoom_y = output_height / rect.height if rect.height > 0 else 1.0

            # Trust the calculated matrix
            mat = fitz.Matrix(zoom_x, zoom_y)

            # Render to Pixmap
            # alpha=True gives RGBA.
            pix = page.get_pixmap(matrix=mat, alpha=True)

            # Save to tmp_png_path
            pix.save(tmp_png_path)

            # If no background color specified (transparent), return as-is
            if background_color is None:
                with open(tmp_png_path, "rb") as f:
                    return f.read()

            # If background color specified, ensure it's applied properly
            from PIL import Image as PILImage

            img = PILImage.open(tmp_png_path)
            if img.mode == "RGBA" and background_color:
                # Parse hex color (supports #RRGGBB and #RRGGBBAA formats)
                r, g, b, a = _parse_hex_color(background_color)
                # Create background and composite foreground on top
                if a == 255:
                    # Fully opaque background - convert to RGB
                    bg = PILImage.new("RGB", img.size, (r, g, b))
                    bg.paste(img, mask=img.split()[3])
                    img = bg
                elif a > 0:
                    # Semi-transparent or transparent background
                    bg = PILImage.new("RGBA", img.size, (r, g, b, a))
                    img = PILImage.alpha_composite(bg, img)

            img.save(tmp_png_path)

            with open(tmp_png_path, "rb") as f:
                return f.read()

        except _DOC_ERRORS as e:
            logger.warning("PyMuPDF rendering failed for %s: %s", tmp_svg_path, e)
            # Fall back to inkscape as last resort
            try:
                result = subprocess.run(
                    ["inkscape", str(tmp_svg_path), "--export-filename", str(tmp_png_path)],
                    capture_output=True,
                    timeout=30,
                )
                if result.returncode != 0:
                    return None

                with open(tmp_png_path, "rb") as f:
                    return f.read()
            except (FileNotFoundError, subprocess.TimeoutExpired):
                return None

    except (OSError, ValueError):
        # Creating or reading back the temporary SVG and PNG.
        return None
    finally:
        if tmp_svg_path:
            tmp_svg_path.unlink(missing_ok=True)
        if tmp_png_path:
            tmp_png_path.unlink(missing_ok=True)
        if tmp_raw_path:
            tmp_raw_path.unlink(missing_ok=True)


def page_is_deleted(page: Any) -> bool:
    """Report whether a ``cPages.pages[]`` entry is a page the user removed.

    Deleting a page on the tablet does not remove it from ``.content`` and does
    not remove its ``.rm`` file from the zip: it adds a CRDT marker, and every
    other field stays exactly as it was. So a reader that does not look for the
    marker renders, transcribes, pays for and publishes a page the tablet is no
    longer showing — which is what Living Ink did until this existed.

    The marker is a CRDT register, ``{"timestamp": ..., "value": true}``, and
    its value is what decides. A bare ``true`` and a bare ``1`` are accepted
    too, because the same field is written flat in the older page format.

    Args:
        page: One entry from ``cPages.pages``. A non-dict entry is the oldest
            format, a bare page id, which carries no marker at all.

    Returns:
        True if the page was deleted on the tablet.
    """
    if not isinstance(page, dict):
        return False
    marker = page.get("deleted")
    if isinstance(marker, dict):
        return bool(marker.get("value"))
    return bool(marker)


def _live_page_ids(pages_meta: List[Any]) -> Tuple[List[str], set]:
    """Split a ``cPages.pages`` list into the pages that still exist and the rest.

    Args:
        pages_meta: The raw ``cPages.pages`` list, or the older flat ``pages``
            list of bare ids.

    Returns:
        A tuple of the live page ids in document order and the set of deleted
        page ids. The second is not the complement of the first: a caller that
        sweeps up ``.rm`` files the page list never mentioned needs to know
        which ids were mentioned *and* removed, or the file comes back in as an
        orphan.
    """
    live: List[str] = []
    dead: set = set()
    for page in pages_meta:
        page_id = page.get("id") if isinstance(page, dict) else str(page)
        if page_id is None:
            continue
        if page_is_deleted(page):
            dead.add(page_id)
        else:
            live.append(page_id)
    return live, dead


def _get_ordered_rm_files(tmpdir_path: Path) -> List[Path]:
    """Extract and order .rm files from an extracted document directory.

    Reads the .content file to determine page order and returns .rm files
    sorted accordingly. Falls back to filesystem order if no page order found.

    Pages the user deleted on the tablet are left out — both from the ordered
    list and from the sweep of files the page list does not mention, since a
    deleted page's ``.rm`` file is still in the zip and would otherwise return
    as an orphan.

    Args:
        tmpdir_path: Path to the extracted document directory

    Returns:
        List of .rm file paths in correct page order
    """
    # Get page order from .content file
    page_order: List[str] = []
    deleted_ids: set = set()
    for content_file in tmpdir_path.glob("*.content"):
        try:
            data = json.loads(content_file.read_text())
            # New format: cPages.pages array
            if "cPages" in data and "pages" in data["cPages"]:
                page_order, deleted_ids = _live_page_ids(data["cPages"]["pages"])
            # Fallback: pages array directly
            elif "pages" in data and isinstance(data["pages"], list):
                page_order, deleted_ids = _live_page_ids(data["pages"])
        except (OSError, *_JSON_ERRORS):
            # Ignore errors reading/parsing .content file; fallback to default page order
            pass
        break

    rm_files = [p for p in tmpdir_path.glob("**/*.rm") if p.stem not in deleted_ids]

    # Sort rm_files by page order if available
    if page_order:
        rm_by_id = {}
        for rm_file in rm_files:
            page_id = rm_file.stem
            rm_by_id[page_id] = rm_file

        ordered_rm_files = []
        for page_id in page_order:
            if page_id in rm_by_id:
                ordered_rm_files.append(rm_by_id[page_id])
        # Add any remaining files not in page order
        for rm_file in rm_files:
            if rm_file not in ordered_rm_files:
                ordered_rm_files.append(rm_file)
        return ordered_rm_files

    return rm_files


@contextmanager
def _open_document_zip(zip_path: Path) -> Iterator[Path]:
    """Extract a document zip into a temporary directory for the caller.

    Every operation that needs the *whole* document — as opposed to a single
    named member — goes through here, so extraction, cleanup, and the
    path-traversal guard live in one place.

    Args:
        zip_path: Path to the reMarkable document zip.

    Yields:
        Path to the temporary directory holding the extracted contents. It is
        deleted when the block exits.

    Raises:
        ValueError: If an entry would be written outside the temp directory.
            These zips come off the user's own tablet, but the whole point of
            a single extract site is that the check is written once.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir).resolve()

        with zipfile.ZipFile(zip_path, "r") as zf:
            for name in zf.namelist():
                target = (tmpdir_path / name).resolve()
                if not target.is_relative_to(tmpdir_path):
                    raise ValueError(f"Refusing to extract '{name}' outside the temp directory.")
            zf.extractall(tmpdir_path)

        yield tmpdir_path


def render_page_from_document_zip(
    zip_path: Path,
    page: int = 1,
    background_color: Optional[str] = None,
    screen: Optional[Tuple[int, int]] = None,
) -> Optional[bytes]:
    """
    Render a specific page from a reMarkable document zip to PNG.

    Args:
        zip_path: Path to the document zip file
        page: Page number (1-indexed)
        background_color: Background color (e.g., "#FFFFFF", None for transparent).
                         See :func:`render_rm_file_to_png`.
        screen: Panel size of the tablet that drew the page. See
            :func:`render_rm_file_to_png`.

    Returns:
        PNG image bytes, or None if rendering failed or page doesn't exist
    """
    with _open_document_zip(zip_path) as tmpdir_path:
        rm_files = _get_ordered_rm_files(tmpdir_path)

        # Validate page number
        if page < 1 or page > len(rm_files):
            return None

        # Render the requested page
        target_rm_file = rm_files[page - 1]
        return render_rm_file_to_png(
            target_rm_file, background_color=background_color, screen=screen
        )


def get_page_source_hashes(zip_path: Path) -> List[str]:
    """Hash each page's ``.rm`` source, in the same order pages are rendered.

    The hash identifies what the user drew, before any rendering happened, so
    it answers "has this page changed since last time" without paying for the
    render that would answer it by comparing PNGs.

    Args:
        zip_path: Path to the reMarkable document zip.

    Returns:
        One hex digest per page, in page order; empty when the zip cannot be
        read. An unreadable page yields an empty string in its slot, so the
        list always lines up with the page numbers.
    """
    hashes: List[str] = []
    try:
        with _open_document_zip(zip_path) as tmpdir_path:
            for rm_file in _get_ordered_rm_files(tmpdir_path):
                try:
                    hashes.append(hashlib.sha256(rm_file.read_bytes()).hexdigest())
                except OSError:
                    logger.debug("Could not hash %s", rm_file, exc_info=True)
                    hashes.append("")
    except (*_ZIP_ERRORS, ValueError):
        logger.debug("Could not read page sources from %s", zip_path, exc_info=True)
        return []
    return hashes


@lru_cache(maxsize=1)
def renderer_fingerprint() -> str:
    """Identify the libraries that turn a ``.rm`` file into an image.

    A cached PNG is only reusable while the code that produced it is
    unchanged. ``rmc`` and ``rmscene`` are the two libraries this module drives,
    and their versions are the part of that no source can know for itself.

    This used to carry a hand-maintained ``RENDER_FORMAT_VERSION`` covering
    what *this* module does — the monkey-patching, the background handling, the
    bounds. One number for three renderers meant a change to the PDF compositor
    threw away every cached notebook page, and remembering to bump it was a
    convention rather than a mechanism. Each
    :class:`~living_ink.sources.Renderer` now declares its own ``version`` and
    the pipeline puts it in the key beside this digest, so a renderer
    invalidates its own pages and nobody else's.

    The background colour and the panel size are deliberately *not* in here:
    they are per-run rather than per-build, so the caller folds them in.

    Returns:
        A short hex digest identifying the installed rendering libraries.
    """
    versions = []
    for module in ("rmc", "rmscene"):
        try:
            versions.append(f"{module}={version(module)}")
        except PackageNotFoundError:
            # Not installed, which is itself a rendering behaviour worth
            # distinguishing from any installed version.
            versions.append(f"{module}=absent")

    return hashlib.sha256("\0".join(versions).encode("utf-8")).hexdigest()[:16]


def get_document_page_count(zip_path: Path) -> int:
    """
    Get the number of pages in a reMarkable document zip.

    Args:
        zip_path: Path to the document zip file

    Returns:
        Number of pages (0 if unable to determine)
    """
    # Counted via the same ordered list the renderer walks, so the count and
    # the valid page numbers for render_page_from_document_zip() cannot drift.
    with _open_document_zip(zip_path) as tmpdir_path:
        return len(_get_ordered_rm_files(tmpdir_path))


def normalize_tag(tag: str) -> str:
    """Normalize a tag string for use in notes and frontmatter.

    - Strips leading '#'
    - Replaces spaces with hyphens
    - Keeps valid tag characters (alphanumeric, hyphens, underscores, slashes)

    Args:
        tag: Raw tag string.

    Returns:
        Normalized tag string.
    """
    clean = tag.strip().lstrip("#").strip()
    clean = re.sub(r"\s+", "-", clean)
    clean = re.sub(r"[^\w\-/]", "", clean)
    return clean


def extract_tags_from_dict(data: dict) -> List[str]:
    """Extract and normalize unique tags from a parsed .content or .metadata dict.

    Handles:
    - 'tags': list of strings or dicts with 'name' (document-level tags)
    - 'pageTags': list of dicts with 'name' (page-level tags)

    Args:
        data: Parsed JSON dict from .content or .metadata.

    Returns:
        List of cleaned, unique tag names preserving order.
    """
    raw_tags = []

    # 1. Document-level tags
    doc_tags = data.get("tags") or []
    if isinstance(doc_tags, list):
        for t in doc_tags:
            if isinstance(t, str) and t.strip():
                raw_tags.append(t.strip())
            elif isinstance(t, dict) and "name" in t:
                name = str(t["name"]).strip()
                if name:
                    raw_tags.append(name)

    # 2. Page-level tags
    page_tags = data.get("pageTags") or []
    if isinstance(page_tags, list):
        for t in page_tags:
            if isinstance(t, str) and t.strip():
                raw_tags.append(t.strip())
            elif isinstance(t, dict) and "name" in t:
                name = str(t["name"]).strip()
                if name:
                    raw_tags.append(name)

    # Normalize and deduplicate
    seen = set()
    unique_tags = []
    for tag in raw_tags:
        clean = normalize_tag(tag)
        if clean and clean.lower() not in seen:
            seen.add(clean.lower())
            unique_tags.append(clean)

    return unique_tags


def extract_tags_from_zip(zip_path: Path) -> List[str]:
    """Extract document and page tags from a reMarkable document zip.

    Args:
        zip_path: Path to the downloaded document .zip archive.

    Returns:
        List of unique, normalized tags.
    """
    tags: List[str] = []
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for name in zf.namelist():
                if name.endswith(".content") or name.endswith(".metadata"):
                    try:
                        data = json.loads(zf.read(name).decode("utf-8"))
                        if isinstance(data, dict):
                            tags.extend(extract_tags_from_dict(data))
                    except (*_ZIP_ERRORS, *_JSON_ERRORS):
                        pass
    except _ZIP_ERRORS as e:
        logger.debug(f"Failed to extract tags from zip {zip_path}: {e}")

    seen = set()
    result = []
    for t in tags:
        if t.lower() not in seen:
            seen.add(t.lower())
            result.append(t)
    return result


def format_page_label(page_num: int, pdf_path: Optional[Path] = None) -> str:
    """Format a human-readable page label.

    For PDFs with embedded page labels (e.g. Roman numerals in front matter,
    or offset book page numbers), formats as:
        'Page {label} (PDF p. {page_num})'
    If no label is present or label equals page_num, formats as:
        'Page {page_num}'

    Args:
        page_num: 1-indexed physical page number.
        pdf_path: Optional path to underlying PDF file.

    Returns:
        Formatted label string, e.g. 'Page xiii (PDF p. 19)' or 'Page 51 (PDF p. 77)'.
    """
    if pdf_path and Path(pdf_path).exists() and Path(pdf_path).suffix.lower() == ".pdf":
        try:
            import pymupdf as fitz

            with quiet_mupdf():
                doc = fitz.open(pdf_path)
                try:
                    idx = page_num - 1
                    if 0 <= idx < len(doc):
                        label = doc[idx].get_label()
                        if label and label.strip() and label.strip().lower() != str(page_num):
                            return f"Page {label.strip()} (pdf-{page_num})"
                finally:
                    doc.close()
        except _DOC_ERRORS as e:
            logger.debug(f"Failed to read page label from {pdf_path}: {e}")

    return f"Page {page_num}"


def page_labels(page_nums: Sequence[int], pdf_path: Optional[Path] = None) -> Dict[int, str]:
    """Label every page of one document, opening the document once.

    :func:`format_page_label` opens and closes the PDF on every call, which is
    fine for one page and is 300 opens for a 300-page annotated PDF. The label
    is needed for every page at the same moment — the pages have just been
    rendered — so the whole set is read in one pass.

    Args:
        page_nums: 1-indexed physical page numbers, in any order.
        pdf_path: The underlying document, if there is one. A notebook has
            none, and an EPUB has no PDF page labels to read.

    Returns:
        One label per requested page number.
    """
    plain = {n: f"Page {n}" for n in page_nums}
    if not pdf_path or Path(pdf_path).suffix.lower() != ".pdf" or not Path(pdf_path).exists():
        return plain

    try:
        import pymupdf as fitz

        with quiet_mupdf():
            doc = fitz.open(pdf_path)
            try:
                for num in page_nums:
                    idx = num - 1
                    if not (0 <= idx < len(doc)):
                        continue
                    label = doc[idx].get_label()
                    if label and label.strip() and label.strip().lower() != str(num):
                        plain[num] = f"Page {label.strip()} (pdf-{num})"
            finally:
                doc.close()
    except _DOC_ERRORS as e:
        # A label is decoration. A document whose labels cannot be read still
        # publishes, with the physical page numbers it was going to show anyway.
        logger.debug(f"Failed to read page labels from {pdf_path}: {e}")

    return plain


@lru_cache(maxsize=16)
def _get_pdf_toc_entries(pdf_path_str: str) -> List[Tuple[int, str, int]]:
    """Cached helper to read Table of Contents entries from a PDF."""
    try:
        import pymupdf as fitz

        with quiet_mupdf():
            doc = fitz.open(pdf_path_str)
            try:
                return [(int(lvl), str(title).strip(), int(p)) for lvl, title, p in doc.get_toc()]
            finally:
                doc.close()
    except _DOC_ERRORS as e:
        logger.debug(f"Failed to read TOC from {pdf_path_str}: {e}")
        return []


def get_pdf_toc_breadcrumbs(page_num: int, pdf_path: Optional[Path] = None) -> List[str]:
    """Extract hierarchical TOC breadcrumbs for a specific page in a PDF document.

    Traverses the document's Table of Contents and builds the breadcrumb trail
    active at ``page_num``.

    Args:
        page_num: 1-indexed physical page number.
        pdf_path: Optional path to PDF or document file.

    Returns:
        List of section titles, e.g. ['Part I', 'Chapter 2', 'Data Management'].
    """
    # Suffix-checked like format_page_label: an EPUB has no PDF outline to
    # read, and handing one to PyMuPDF only makes MuPDF parse its stylesheets
    # and complain about them for a result that is empty either way.
    if not pdf_path or Path(pdf_path).suffix.lower() != ".pdf" or not Path(pdf_path).exists():
        return []

    toc = _get_pdf_toc_entries(str(Path(pdf_path).resolve()))
    if not toc:
        return []

    hierarchy: Dict[int, str] = {}
    for lvl, title, p in toc:
        if p <= page_num:
            hierarchy[lvl] = title
            # Prune any deeper sub-levels from previously completed sections
            for k in list(hierarchy.keys()):
                if k > lvl:
                    del hierarchy[k]
        else:
            break

    return [hierarchy[k] for k in sorted(hierarchy.keys()) if hierarchy[k]]


def format_page_section_header(
    page_num: int,
    pdf_path: Optional[Path] = None,
    include_divider: bool = True,
    label: Optional[str] = None,
    breadcrumbs: Optional[Sequence[str]] = None,
) -> str:
    """Format a page section header with divider and two-tier styled TOC hierarchy.

    The lowest level in the PDF TOC hierarchy is styled with color #777777,
    and the parent hierarchy followed by the page label is styled with color #aaaaaa.

    Example output:
        ---

        <span style="font-size: 0.9em; color: #777777"><b>Data Management</b><br><span style="font-size: 0.8em; color: #aaaaaa">Part I. Foundation and Building Blocks | Chapter 2. The Data Engineering Lifecycle | Major Undercurrents Across the Data Engineering Lifecycle | Page 51 (pdf-77)</span></span>

    Args:
        page_num: 1-indexed physical page number.
        pdf_path: Optional path to underlying document file.
        include_divider: Whether to prepend a Markdown divider ('---').
        label: The page label, when the caller already has it. Passing it skips
            an open of ``pdf_path``, which a per-page loop repeats once a page.
        breadcrumbs: The TOC path, when the caller already has it. Same reason.

    Returns:
        Formatted Markdown header string.
    """
    page_label = label if label is not None else format_page_label(page_num, pdf_path)
    if breadcrumbs is None:
        breadcrumbs = get_pdf_toc_breadcrumbs(page_num, pdf_path)

    if breadcrumbs:
        lowest = breadcrumbs[-1]
        parents = list(breadcrumbs[:-1])
        if parents:
            sub_text = f"{' | '.join(parents)} | {page_label}"
        else:
            sub_text = page_label
        header_html = (
            f'<span style="font-size: 0.9em; color: #777777"><b>{lowest}</b><br>'
            f'<span style="font-size: 0.8em; color: #aaaaaa">{sub_text}</span></span>'
        )
    else:
        header_html = f'<span style="font-size: 0.9em; color: #777777"><b>{page_label}</b></span>'

    lines = []
    if include_divider:
        lines.append("---")
        lines.append("")
    lines.append(header_html)
    return "\n".join(lines)
