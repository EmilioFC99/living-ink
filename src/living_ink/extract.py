"""
Text extraction helpers for reMarkable documents.
"""

import io
import json
import logging
import os
import re
import tempfile
import time
import zipfile
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pymupdf as fitz  # PyMuPDF
from PIL import Image

logger = logging.getLogger(__name__)

# reMarkable tablet screen dimensions (in pixels) - used as fallback
REMARKABLE_WIDTH = 1404
REMARKABLE_HEIGHT = 1872

# Standard reMarkable background color (light cream/gray)
# Can be overridden via REMARKABLE_BACKGROUND_COLOR environment variable
_DEFAULT_BACKGROUND_COLOR = "#FBFBFB"


def get_background_color() -> str:
    """Get the background color, checking env var for override."""
    return os.environ.get("REMARKABLE_BACKGROUND_COLOR", _DEFAULT_BACKGROUND_COLOR)


# For backwards compatibility, expose as module constant (evaluated at import)
# Use get_background_color() for runtime evaluation of env var
REMARKABLE_BACKGROUND_COLOR = get_background_color()

# Margin around content when using content-based bounding box (in pixels)
CONTENT_MARGIN = 50

# Cache TTL in seconds (5 minutes)
CACHE_TTL_SECONDS = 300

# Module-level cache for OCR results (full document)
# Key: doc_id
# Value: {"result": extraction_result, "include_ocr": bool, "timestamp": float}
_extraction_cache: Dict[str, Dict[str, Any]] = {}

# Per-page cache for sampling OCR results
# Key: (doc_id, page_number, backend)
# Value: {"text": str, "timestamp": float}
_page_ocr_cache: Dict[tuple, Dict[str, Any]] = {}


def _is_cache_valid(cached: Dict[str, Any]) -> bool:
    """Check if a cached entry is still valid based on TTL."""
    if "timestamp" not in cached:
        return True  # Old cache entries without timestamp are valid
    return (time.time() - cached["timestamp"]) < CACHE_TTL_SECONDS


def clear_extraction_cache(doc_id: Optional[str] = None) -> None:
    """
    Clear the extraction cache.

    Args:
        doc_id: If provided, only clear cache for this document.
                If None, clear the entire cache.
    """
    if doc_id:
        _extraction_cache.pop(doc_id, None)
        # Also clear per-page cache entries for this document
        keys_to_remove = [k for k in _page_ocr_cache if k[0] == doc_id]
        for key in keys_to_remove:
            _page_ocr_cache.pop(key, None)
    else:
        _extraction_cache.clear()
        _page_ocr_cache.clear()


def get_cached_page_ocr(
    doc_id: str,
    page: int,
    backend: str,
) -> Optional[str]:
    """
    Get cached OCR result for a specific page.

    Args:
        doc_id: Document ID
        page: Page number (1-indexed)
        backend: OCR backend used ("sampling", "google", "tesseract")

    Returns:
        Cached OCR text or None if not cached/expired
    """
    cache_key = (doc_id, page, backend)
    if cache_key in _page_ocr_cache:
        cached = _page_ocr_cache[cache_key]
        if _is_cache_valid(cached):
            return cached["text"]
        # Expired, remove it
        _page_ocr_cache.pop(cache_key, None)
    return None


def cache_page_ocr(
    doc_id: str,
    page: int,
    backend: str,
    text: str,
) -> None:
    """
    Cache OCR result for a specific page.

    Args:
        doc_id: Document ID
        page: Page number (1-indexed)
        backend: OCR backend used ("sampling", "google", "tesseract")
        text: OCR text result
    """
    cache_key = (doc_id, page, backend)
    _page_ocr_cache[cache_key] = {
        "text": text,
        "timestamp": time.time(),
    }


def get_cached_ocr_result(
    doc_id: str,
    include_ocr: bool = True,
    ocr_backend: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Get cached OCR result for a document if available and valid.

    Args:
        doc_id: Document ID to look up
        include_ocr: Whether OCR content is required
        ocr_backend: If specified, only return cache if it was produced by this backend.
                     Use "sampling", "google", or "tesseract". None accepts any backend.

    Returns:
        Cached result dict or None if not cached/expired/wrong backend
    """
    if doc_id in _extraction_cache:
        cached = _extraction_cache[doc_id]
        if (cached["include_ocr"] or not include_ocr) and _is_cache_valid(cached):
            # Check backend match if specified
            if ocr_backend is not None:
                cached_backend = cached["result"].get("ocr_backend")
                if cached_backend != ocr_backend:
                    return None
            return cached["result"]
    return None


def cache_ocr_result(
    doc_id: str,
    result: Dict[str, Any],
    include_ocr: bool = True,
) -> None:
    """
    Cache an OCR result for a document.

    Args:
        doc_id: Document ID
        result: Extraction result dict with keys: typed_text, highlights,
                handwritten_text, pages, page_ids, ocr_backend
        include_ocr: Whether this result includes OCR content
    """
    _extraction_cache[doc_id] = {
        "result": result,
        "include_ocr": include_ocr,
        "timestamp": time.time(),
    }


def find_similar_documents(query: str, documents: List, limit: int = 5) -> List[str]:
    """Find documents with similar names for 'did you mean' suggestions."""
    query_lower = query.lower()
    scored = []
    for doc in documents:
        name = doc.VissibleName
        # Use sequence matcher for fuzzy matching
        ratio = SequenceMatcher(None, query_lower, name.lower()).ratio()
        # Boost partial matches
        if query_lower in name.lower():
            ratio += 0.3
        scored.append((name, ratio))

    scored.sort(key=lambda x: x[1], reverse=True)
    return [name for name, score in scored[:limit] if score > 0.3]


def extract_text_from_pdf(pdf_path: Path) -> str:
    """
    Extract text from a PDF file using PyMuPDF.

    Returns the full text content of the PDF.
    """
    try:
        import pymupdf as fitz  # PyMuPDF

        text_parts = []
        with fitz.open(pdf_path) as doc:
            for page_num, page in enumerate(doc, 1):
                page_text = page.get_text()
                if page_text.strip():
                    p_header = format_page_section_header(page_num, pdf_path, include_divider=True)
                    text_parts.append(f"{p_header}\n\n{page_text.strip()}")

        return "\n\n".join(text_parts) if text_parts else ""
    except ImportError:
        return ""
    except Exception:
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
        return ""


def extract_raw_document_from_zip(zip_path: Path, out_path: Path) -> Optional[Path]:
    """Extract the raw PDF or EPUB file stored inside a reMarkable document zip.

    Args:
        zip_path: Path to the downloaded document zip archive.
        out_path: Destination path for the extracted raw document.

    Returns:
        Path to the extracted file, or None if no PDF/EPUB found in archive.
    """
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for name in zf.namelist():
                lower_name = name.lower()
                if lower_name.endswith((".pdf", ".epub")):
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(out_path, "wb") as f:
                        f.write(zf.read(name))
                    return out_path
    except Exception as e:
        logger.debug(f"Failed to extract raw document from {zip_path}: {e}")
    return None


def get_pdf_annotated_page_map(zip_path: Path) -> List[Dict[str, Any]]:
    """Parse a document zip and find all annotated pages with their PDF page index.

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
                    except Exception:
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
    except Exception as e:
        logger.debug(f"Failed to read page map from {zip_path}: {e}")
        return []


def render_composite_pdf_page(
    pdf_path: Path,
    page_index: int,
    rm_bytes: bytes,
    dpi: int = 150,
) -> Optional[bytes]:
    """Render a PDF page with handwritten .rm strokes composited on top.

    Args:
        pdf_path: Path to the source PDF document.
        page_index: 0-indexed page number in the PDF.
        rm_bytes: Raw bytes of the .rm pen stroke file.
        dpi: Resolution for rendering the PDF page.

    Returns:
        PNG image bytes of the composite page, or None if rendering failed.
    """
    try:
        with fitz.open(str(pdf_path)) as doc:
            if page_index < 0 or page_index >= len(doc):
                return None
            page = doc[page_index]
            pix = page.get_pixmap(dpi=dpi)
            pdf_img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

        if not rm_bytes:
            out_buf = io.BytesIO()
            pdf_img.save(out_buf, format="PNG")
            return out_buf.getvalue()

        # Render the .rm file
        with tempfile.NamedTemporaryFile(suffix=".rm", delete=False) as f:
            f.write(rm_bytes)
            tmp_rm = Path(f.name)

        try:
            rm_png = render_rm_file_to_png(tmp_rm)
        finally:
            tmp_rm.unlink(missing_ok=True)

        if rm_png:
            rm_img = Image.open(io.BytesIO(rm_png))
            rm_resized = rm_img.resize((pix.width, pix.height), Image.Resampling.LANCZOS)
            if rm_resized.mode == "RGBA":
                pdf_img.paste(rm_resized, (0, 0), rm_resized)
            else:
                pdf_img.paste(rm_resized, (0, 0))

        out_buf = io.BytesIO()
        pdf_img.save(out_buf, format="PNG")
        return out_buf.getvalue()
    except Exception as e:
        logger.debug(f"Failed to render composite PDF page {page_index}: {e}")
        return None


def render_pdf_page_preview(
    pdf_path: Path,
    page_index: int = 0,
    dpi: int = 150,
) -> Optional[bytes]:
    """Render a single page of a PDF as a preview PNG image.

    Args:
        pdf_path: Path to the PDF document.
        page_index: 0-indexed page number to render (default: 0 for cover).
        dpi: Resolution for rendering.

    Returns:
        PNG image bytes, or None on failure.
    """
    try:
        with fitz.open(str(pdf_path)) as doc:
            if page_index < 0 or page_index >= len(doc):
                return None
            page = doc[page_index]
            pix = page.get_pixmap(dpi=dpi)
            pdf_img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

        out_buf = io.BytesIO()
        pdf_img.save(out_buf, format="PNG")
        return out_buf.getvalue()
    except Exception as e:
        logger.debug(f"Failed to render PDF page preview: {e}")
        return None


def extract_text_from_rm_file(rm_file_path: Path) -> List[str]:
    """
    Extract typed text from a .rm file using rmscene.

    This extracts text that was typed via Type Folio or on-screen keyboard.
    Does NOT require OCR - text is stored natively in v6 .rm files.
    """
    try:
        from rmscene import read_blocks
        from rmscene.scene_items import Text
        from rmscene.scene_tree import SceneTree

        with open(rm_file_path, "rb") as f:
            tree = SceneTree()
            for block in read_blocks(f):
                tree.add_block(block)

        text_lines = []

        # Extract text from the scene tree
        for item in tree.root.children.values():
            if hasattr(item, "value") and isinstance(item.value, Text):
                text_obj = item.value
                if hasattr(text_obj, "items"):
                    for text_item in text_obj.items:
                        if hasattr(text_item, "value") and text_item.value:
                            text_lines.append(str(text_item.value))

        return text_lines

    except ImportError:
        return []  # rmscene not available
    except Exception:
        # Log but don't fail - file might be older format
        return []


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
    except Exception:
        return None


def _patch_rmc() -> None:
    """Ensure rmc's RM_PALETTE and Pen.create are resilient to new pen/color types.

    Upstream rmc omits PenColor.HIGHLIGHT (value 9) from RM_PALETTE, which causes
    KeyError: 9 when converting notes that use the highlighter. This function
    ensures all colors have a fallback and unknown pen types default to Ballpoint.
    """
    try:
        import rmc.exporters.writing_tools as wt
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
            except Exception:
                from rmc.exporters.writing_tools import Ballpoint

                return Ballpoint(width, color_id)

        wt.Pen.create = safe_create
    except Exception:
        pass


def render_rm_file_to_png(
    rm_file_path: Path, background_color: Optional[str] = None
) -> Optional[bytes]:
    """
    Render a .rm file to PNG image bytes.

    Uses rmc to convert .rm to SVG, then cairosvg to convert to PNG.
    The output is sized based on the SVG content bounds with a margin.

    Args:
        rm_file_path: Path to the .rm file
        background_color: Background color (e.g., "#FFFFFF", "transparent", None).
                         None means transparent. Use REMARKABLE_BACKGROUND_COLOR
                         for the standard reMarkable paper color.

    Returns:
        PNG image bytes, or None if rendering failed
    """
    import subprocess
    import tempfile

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
            # Fallback for debugging provided the library call failed
            print(f"Error converting .rm to .svg: {e}")
            return None

        # Check if the file was actually created and has content
        if not tmp_svg_path.exists() or tmp_svg_path.stat().st_size == 0:
            return None

        # Get content bounds from SVG
        bounds = _get_svg_content_bounds(tmp_svg_path)
        if bounds:
            # Use content bounds with margin
            _, _, content_width, content_height = bounds
            output_width = int(content_width) + 2 * CONTENT_MARGIN
            output_height = int(content_height) + 2 * CONTENT_MARGIN
        else:
            # Fallback to standard reMarkable dimensions
            output_width = REMARKABLE_WIDTH
            output_height = REMARKABLE_HEIGHT

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

        except Exception as e:
            print(f"PyMuPDF rendering failed: {e}")
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

    except Exception:
        return None
    finally:
        if tmp_svg_path:
            tmp_svg_path.unlink(missing_ok=True)
        if tmp_png_path:
            tmp_png_path.unlink(missing_ok=True)
        if tmp_raw_path:
            tmp_raw_path.unlink(missing_ok=True)


def render_rm_file_to_svg(
    rm_file_path: Path, background_color: Optional[str] = None
) -> Optional[str]:
    """
    Render a .rm file to SVG string.

    Uses rmc to convert .rm to SVG, optionally adding a background.

    Args:
        rm_file_path: Path to the .rm file
        background_color: Background color (e.g., "#FFFFFF", None for transparent).
                         Use REMARKABLE_BACKGROUND_COLOR for the standard paper color.

    Returns:
        SVG content as string, or None if rendering failed
    """
    import subprocess
    import tempfile

    tmp_svg_path = None

    try:
        # Create temp file for SVG output
        with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as tmp_svg:
            tmp_svg_path = Path(tmp_svg.name)

        # Convert .rm to SVG using rmc
        result = subprocess.run(
            ["rmc", "-t", "svg", "-o", str(tmp_svg_path), str(rm_file_path)],
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            return None

        # Read SVG content
        svg_content = tmp_svg_path.read_text()

        # Add background rectangle if color specified
        if background_color:
            svg_content = _add_svg_background(svg_content, background_color)

        return svg_content

    except subprocess.TimeoutExpired:
        return None
    except FileNotFoundError:
        # rmc not installed
        return None
    except Exception:
        return None
    finally:
        if tmp_svg_path:
            tmp_svg_path.unlink(missing_ok=True)


def _add_svg_background(svg_content: str, background_color: str) -> str:
    """Add a background rectangle to an SVG.

    Inserts a rect element as the first child of the SVG to act as background.

    Args:
        svg_content: Original SVG content
        background_color: Background color (e.g., "#FFFFFF")

    Returns:
        SVG content with background added
    """
    import re

    # Find the opening <svg> tag and its attributes
    svg_match = re.search(r"(<svg[^>]*>)", svg_content, re.IGNORECASE)
    if not svg_match:
        return svg_content

    svg_tag = svg_match.group(1)

    # Extract viewBox or width/height for the background rect dimensions
    viewbox_match = re.search(r'viewBox="([^"]*)"', svg_tag)
    if viewbox_match:
        viewbox = viewbox_match.group(1)
        parts = viewbox.split()
        if len(parts) == 4:
            x, y, width, height = parts
            bg_rect = (
                f'<rect x="{x}" y="{y}" width="{width}" '
                f'height="{height}" fill="{background_color}"/>'
            )
        else:
            # Fallback to full page
            bg_rect = f'<rect x="0" y="0" width="100%" height="100%" fill="{background_color}"/>'
    else:
        # No viewBox, use 100% dimensions
        bg_rect = f'<rect x="0" y="0" width="100%" height="100%" fill="{background_color}"/>'

    # Insert background rect right after the opening svg tag
    insert_pos = svg_match.end()
    return svg_content[:insert_pos] + bg_rect + svg_content[insert_pos:]


def _get_ordered_rm_files(tmpdir_path: Path) -> List[Path]:
    """Extract and order .rm files from an extracted document directory.

    Reads the .content file to determine page order and returns .rm files
    sorted accordingly. Falls back to filesystem order if no page order found.

    Args:
        tmpdir_path: Path to the extracted document directory

    Returns:
        List of .rm file paths in correct page order
    """
    # Get page order from .content file
    page_order = []
    for content_file in tmpdir_path.glob("*.content"):
        try:
            data = json.loads(content_file.read_text())
            # New format: cPages.pages array
            if "cPages" in data and "pages" in data["cPages"]:
                page_order = [p["id"] for p in data["cPages"]["pages"]]
            # Fallback: pages array directly
            elif "pages" in data and isinstance(data["pages"], list):
                page_order = data["pages"]
        except Exception:
            # Ignore errors reading/parsing .content file; fallback to default page order
            pass
        break

    rm_files = list(tmpdir_path.glob("**/*.rm"))

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


def render_page_from_document_zip_svg(
    zip_path: Path, page: int = 1, background_color: Optional[str] = None
) -> Optional[str]:
    """
    Render a specific page from a reMarkable document zip to SVG.

    Args:
        zip_path: Path to the document zip file
        page: Page number (1-indexed)
        background_color: Background color (e.g., "#FFFFFF", None for transparent).
                         Use REMARKABLE_BACKGROUND_COLOR for the standard paper color.

    Returns:
        SVG content as string, or None if rendering failed or page doesn't exist
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmpdir_path)

        rm_files = _get_ordered_rm_files(tmpdir_path)

        # Validate page number
        if page < 1 or page > len(rm_files):
            return None

        # Render the requested page
        target_rm_file = rm_files[page - 1]
        return render_rm_file_to_svg(target_rm_file, background_color=background_color)


def render_page_from_document_zip(
    zip_path: Path, page: int = 1, background_color: Optional[str] = None
) -> Optional[bytes]:
    """
    Render a specific page from a reMarkable document zip to PNG.

    Args:
        zip_path: Path to the document zip file
        page: Page number (1-indexed)
        background_color: Background color (e.g., "#FFFFFF", None for transparent).
                         Use REMARKABLE_BACKGROUND_COLOR for the standard paper color.

    Returns:
        PNG image bytes, or None if rendering failed or page doesn't exist
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmpdir_path)

        rm_files = _get_ordered_rm_files(tmpdir_path)

        # Validate page number
        if page < 1 or page > len(rm_files):
            return None

        # Render the requested page
        target_rm_file = rm_files[page - 1]
        return render_rm_file_to_png(target_rm_file, background_color=background_color)


def get_document_page_count(zip_path: Path) -> int:
    """
    Get the number of pages in a reMarkable document zip.

    Args:
        zip_path: Path to the document zip file

    Returns:
        Number of pages (0 if unable to determine)
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmpdir_path)

        return len(list(tmpdir_path.glob("**/*.rm")))


def extract_text_from_document_zip(
    zip_path: Path, include_ocr: bool = False, doc_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Extract all text content from a reMarkable document zip.

    Args:
        zip_path: Path to the document zip file
        include_ocr: Whether to run OCR on handwritten content
        doc_id: Optional document ID for caching OCR results

    Returns:
        {
            "typed_text": [...],      # From rmscene parsing (list of strings)
            "highlights": [...],       # From PDF annotations
            "handwritten_text": [...], # From OCR (if enabled) - one per page, in order
            "pages": int,
            "page_ids": [...],         # Page UUIDs in order
            "ocr_backend": str,        # Which OCR backend was used (if any)
        }
    """
    # Check cache if doc_id provided
    if doc_id and doc_id in _extraction_cache:
        cached = _extraction_cache[doc_id]
        # Return cached result if OCR requirement is satisfied and cache is valid
        # (cached with OCR can satisfy no-OCR request, but not vice versa)
        if (cached["include_ocr"] or not include_ocr) and _is_cache_valid(cached):
            return cached["result"]

    result: Dict[str, Any] = {
        "typed_text": [],
        "highlights": [],
        "handwritten_text": None,
        "pages": 0,
        "page_ids": [],
        "ocr_backend": None,
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmpdir_path)

        # Get page order from .content file
        page_order = []
        for content_file in tmpdir_path.glob("*.content"):
            try:
                data = json.loads(content_file.read_text())
                # New format: cPages.pages array
                if "cPages" in data and "pages" in data["cPages"]:
                    page_order = [p["id"] for p in data["cPages"]["pages"]]
                # Fallback: pages array directly
                elif "pages" in data and isinstance(data["pages"], list):
                    page_order = data["pages"]
            except Exception:
                # Malformed .content file - continue without page order
                pass
            break  # Only process first .content file

        rm_files = list(tmpdir_path.glob("**/*.rm"))

        # If we have page order, sort rm_files accordingly
        if page_order:
            # Create mapping of page_id -> rm_file
            rm_by_id = {}
            for rm_file in rm_files:
                page_id = rm_file.stem  # filename without extension
                rm_by_id[page_id] = rm_file

            # Sort rm_files by page order
            ordered_rm_files = []
            for page_id in page_order:
                if page_id in rm_by_id:
                    ordered_rm_files.append(rm_by_id[page_id])
            # Add any remaining files not in page order
            for rm_file in rm_files:
                if rm_file not in ordered_rm_files:
                    ordered_rm_files.append(rm_file)
            rm_files = ordered_rm_files
            result["page_ids"] = [f.stem for f in rm_files]

        result["pages"] = len(rm_files)

        # Extract typed text from .rm files using rmscene
        for rm_file in rm_files:
            text_lines = extract_text_from_rm_file(rm_file)
            result["typed_text"].extend(text_lines)

        # Extract text from .txt and .md files
        for txt_file in tmpdir_path.glob("**/*.txt"):
            try:
                content = txt_file.read_text(errors="ignore")
                if content.strip():
                    result["typed_text"].append(content)
            except Exception:
                # File read failed - skip this file and continue
                pass

        for md_file in tmpdir_path.glob("**/*.md"):
            try:
                content = md_file.read_text(errors="ignore")
                if content.strip():
                    result["typed_text"].append(content)
            except Exception:
                # File read failed - skip this file and continue
                pass

        # Extract from .content files (metadata with text)
        for content_file in tmpdir_path.glob("**/*.content"):
            try:
                data = json.loads(content_file.read_text())
                if "text" in data:
                    result["typed_text"].append(data["text"])
            except Exception:
                # Malformed JSON or read error - skip this file
                pass

        # Extract PDF highlights
        for json_file in tmpdir_path.glob("**/*.json"):
            try:
                data = json.loads(json_file.read_text())
                if isinstance(data, dict) and "highlights" in data:
                    for h in data.get("highlights", []):
                        if "text" in h and h["text"]:
                            result["highlights"].append(h["text"])
            except Exception:
                # Malformed JSON - skip this file
                pass

        # OCR for handwritten content (optional)
        if include_ocr and rm_files:
            ocr_result, ocr_backend = extract_handwriting_ocr(rm_files)
            result["handwritten_text"] = ocr_result
            result["ocr_backend"] = ocr_backend

    # Cache result if doc_id provided
    if doc_id:
        _extraction_cache[doc_id] = {
            "result": result,
            "include_ocr": include_ocr,
            "timestamp": time.time(),
        }

    return result


def extract_handwriting_ocr(rm_files: List[Path]) -> tuple[Optional[List[str]], Optional[str]]:
    """
    Extract handwritten text using OCR.

    Supports multiple backends (set REMARKABLE_OCR_BACKEND env var):
    - "sampling": Uses client's LLM via MCP sampling (requires async context, tools only)
    - "google": Google Cloud Vision - best for handwriting
    - "tesseract": pytesseract - basic OCR, requires rmc + cairosvg
    - "auto" (default): Google if API key provided, else Tesseract

    Note: "sampling" backend requires async context and is only available via tools,
    not via MCP resources. When sampling is configured but this sync function is called
    (e.g., from resources), it falls back to the auto-detection logic.

    Returns:
        Tuple of (ocr_results, backend_used) where backend_used is "google" or "tesseract"
    """
    import os

    backend = os.environ.get("REMARKABLE_OCR_BACKEND", "auto").lower()

    # Sampling backend requires async context - can't be used from sync functions
    # Fall back to auto-detection for resources and other sync callers
    if backend == "sampling":
        backend = "auto"

    # Auto-detect best available backend
    if backend == "auto":
        # Check for Google Vision API key first (simplest auth method)
        if os.environ.get("GOOGLE_VISION_API_KEY"):
            backend = "google"
        else:
            backend = "tesseract"

    if backend == "google":
        result = _ocr_google_vision(rm_files)
        return (result, "google")
    else:
        result = _ocr_tesseract(rm_files)
        return (result, "tesseract")


def _ocr_google_vision(rm_files: List[Path]) -> Optional[List[str]]:
    """
    OCR using Google Cloud Vision API.
    Best quality for handwriting recognition.

    Supports two authentication methods:
    1. GOOGLE_VISION_API_KEY env var (simplest - just an API key)
    2. GOOGLE_APPLICATION_CREDENTIALS or default credentials (service account)
    """
    import os

    api_key = os.environ.get("GOOGLE_VISION_API_KEY")

    if api_key:
        # Use REST API with API key (simpler, no SDK needed)
        return _ocr_google_vision_rest(rm_files, api_key)
    else:
        # Use SDK with service account credentials
        return _ocr_google_vision_sdk(rm_files)


def _ocr_google_vision_rest(rm_files: List[Path], api_key: str) -> Optional[List[str]]:
    """
    OCR using Google Cloud Vision REST API with API key.
    """
    import base64
    import subprocess
    import tempfile

    import requests

    ocr_results = []

    for rm_file in rm_files:
        tmp_svg_path = None
        tmp_png_path = None
        tmp_raw_path = None
        try:
            # Create temp files
            with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as tmp_svg:
                tmp_svg_path = Path(tmp_svg.name)
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_png:
                tmp_png_path = Path(tmp_png.name)

            # Convert .rm to SVG using rmc
            result = subprocess.run(
                ["rmc", "-t", "svg", "-o", str(tmp_svg_path), str(rm_file)],
                capture_output=True,
                timeout=30,
            )
            if result.returncode != 0:
                continue

            # Convert SVG to PNG
            try:
                import cairosvg
                from PIL import Image as PILImage

                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_raw:
                    tmp_raw_path = Path(tmp_raw.name)

                cairosvg.svg2png(
                    url=str(tmp_svg_path),
                    write_to=str(tmp_raw_path),
                    output_width=REMARKABLE_WIDTH,
                    output_height=REMARKABLE_HEIGHT,
                )

                # Add white background
                img = PILImage.open(tmp_raw_path)
                if img.mode == "RGBA":
                    bg = PILImage.new("RGB", img.size, (255, 255, 255))
                    bg.paste(img, mask=img.split()[3])
                    img = bg
                img.save(tmp_png_path)
                tmp_raw_path.unlink(missing_ok=True)
                tmp_raw_path = None
            except ImportError:
                result = subprocess.run(
                    ["inkscape", str(tmp_svg_path), "--export-filename", str(tmp_png_path)],
                    capture_output=True,
                    timeout=30,
                )
                if result.returncode != 0:
                    continue

            # Read and encode image
            with open(tmp_png_path, "rb") as f:
                image_content = base64.b64encode(f.read()).decode("utf-8")

            # Call Google Vision REST API
            url = f"https://vision.googleapis.com/v1/images:annotate?key={api_key}"
            payload = {
                "requests": [
                    {
                        "image": {"content": image_content},
                        "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
                    }
                ]
            }

            response = requests.post(url, json=payload, timeout=60)
            if response.status_code == 200:
                data = response.json()
                if "responses" in data and data["responses"]:
                    resp = data["responses"][0]
                    if "fullTextAnnotation" in resp:
                        text = resp["fullTextAnnotation"]["text"]
                        if text.strip():
                            ocr_results.append(text.strip())
            elif response.status_code in (401, 403):
                # API key invalid or API not enabled - fall back to Tesseract
                return _ocr_tesseract(rm_files)

        except subprocess.TimeoutExpired:
            # Page rendering timed out - skip this page and continue
            pass
        except FileNotFoundError:
            return None
        except Exception:
            # API call or rendering failed - skip this page and continue
            pass
        finally:
            if tmp_svg_path:
                tmp_svg_path.unlink(missing_ok=True)
            if tmp_png_path:
                tmp_png_path.unlink(missing_ok=True)
            if tmp_raw_path:
                tmp_raw_path.unlink(missing_ok=True)

    return ocr_results if ocr_results else None


def _ocr_google_vision_sdk(rm_files: List[Path]) -> Optional[List[str]]:
    """
    OCR using Google Cloud Vision SDK with service account credentials.
    """
    try:
        import subprocess
        import tempfile

        from google.cloud import vision

        client = vision.ImageAnnotatorClient()
        ocr_results = []

        for rm_file in rm_files:
            tmp_svg_path = None
            tmp_png_path = None
            tmp_raw_path = None
            try:
                # Create temp files
                with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as tmp_svg:
                    tmp_svg_path = Path(tmp_svg.name)
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_png:
                    tmp_png_path = Path(tmp_png.name)

                # Convert .rm to SVG using rmc
                result = subprocess.run(
                    ["rmc", "-t", "svg", "-o", str(tmp_svg_path), str(rm_file)],
                    capture_output=True,
                    timeout=30,
                )
                if result.returncode != 0:
                    continue

                # Convert SVG to PNG using cairosvg
                try:
                    import cairosvg
                    from PIL import Image as PILImage

                    # Convert to PNG (comes out with transparent background)
                    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_raw:
                        tmp_raw_path = Path(tmp_raw.name)

                    cairosvg.svg2png(
                        url=str(tmp_svg_path),
                        write_to=str(tmp_raw_path),
                        output_width=REMARKABLE_WIDTH,
                        output_height=REMARKABLE_HEIGHT,
                    )

                    # Add white background (SVG renders as black-on-transparent)
                    img = PILImage.open(tmp_raw_path)
                    if img.mode == "RGBA":
                        bg = PILImage.new("RGB", img.size, (255, 255, 255))
                        bg.paste(img, mask=img.split()[3])
                        img = bg
                    img.save(tmp_png_path)
                    tmp_raw_path.unlink(missing_ok=True)
                    tmp_raw_path = None
                except ImportError:
                    # Fall back to inkscape
                    result = subprocess.run(
                        ["inkscape", str(tmp_svg_path), "--export-filename", str(tmp_png_path)],
                        capture_output=True,
                        timeout=30,
                    )
                    if result.returncode != 0:
                        continue

                # Send to Google Vision API
                with open(tmp_png_path, "rb") as f:
                    content = f.read()

                image = vision.Image(content=content)

                # Use DOCUMENT_TEXT_DETECTION for best handwriting results
                response = client.document_text_detection(image=image)

                if response.error.message:
                    continue

                if response.full_text_annotation.text:
                    ocr_results.append(response.full_text_annotation.text.strip())

            except subprocess.TimeoutExpired:
                # Page rendering timed out - skip this page and continue
                pass
            except FileNotFoundError:
                # rmc not installed
                return None
            finally:
                if tmp_svg_path:
                    tmp_svg_path.unlink(missing_ok=True)
                if tmp_png_path:
                    tmp_png_path.unlink(missing_ok=True)
                if tmp_raw_path:
                    tmp_raw_path.unlink(missing_ok=True)

        return ocr_results if ocr_results else None

    except ImportError:
        # google-cloud-vision not installed, fall back to tesseract
        return _ocr_tesseract(rm_files)
    except Exception:
        # API error, fall back to tesseract
        return _ocr_tesseract(rm_files)


def _ocr_tesseract(rm_files: List[Path]) -> Optional[List[str]]:
    """
    OCR using Tesseract.
    Basic quality - designed for printed text, not handwriting.

    Requires: pytesseract, rmc, cairosvg (or inkscape)
    """
    try:
        import subprocess
        import tempfile

        import pytesseract
        from PIL import Image, ImageFilter, ImageOps

        ocr_results = []

        for rm_file in rm_files:
            tmp_svg_path = None
            tmp_png_path = None
            tmp_raw_path = None
            try:
                # Create temp files
                with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as tmp_svg:
                    tmp_svg_path = Path(tmp_svg.name)
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_png:
                    tmp_png_path = Path(tmp_png.name)

                # Convert .rm to SVG using rmc
                result = subprocess.run(
                    ["rmc", "-t", "svg", "-o", str(tmp_svg_path), str(rm_file)],
                    capture_output=True,
                    timeout=30,
                )
                if result.returncode != 0:
                    continue

                # Convert SVG to PNG with higher resolution for better OCR
                try:
                    import cairosvg

                    # Convert to PNG (comes out with transparent background)
                    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_raw:
                        tmp_raw_path = Path(tmp_raw.name)

                    # Use 1.5x resolution for better OCR (2x is too slow)
                    cairosvg.svg2png(
                        url=str(tmp_svg_path),
                        write_to=str(tmp_raw_path),
                        output_width=2106,  # 1.5x reMarkable width
                        output_height=2808,  # 1.5x reMarkable height
                    )

                    # Add white background (SVG renders as black-on-transparent)
                    img = Image.open(tmp_raw_path)
                    if img.mode == "RGBA":
                        bg = Image.new("RGB", img.size, (255, 255, 255))
                        bg.paste(img, mask=img.split()[3])
                        img = bg
                    img.save(tmp_png_path)
                    tmp_raw_path.unlink(missing_ok=True)
                    tmp_raw_path = None
                except ImportError:
                    result = subprocess.run(
                        ["inkscape", str(tmp_svg_path), "--export-filename", str(tmp_png_path)],
                        capture_output=True,
                        timeout=30,
                    )
                    if result.returncode != 0:
                        continue

                # Preprocess image for better OCR
                img = Image.open(tmp_png_path)

                # Convert to grayscale
                img = img.convert("L")

                # Increase contrast
                img = ImageOps.autocontrast(img, cutoff=2)

                # Slight sharpening
                img = img.filter(ImageFilter.SHARPEN)

                # Run OCR with optimized settings for sparse handwriting
                # PSM 11 = Sparse text - find as much text as possible
                # PSM 6 = Uniform block of text (alternative)
                custom_config = r"--psm 11 --oem 3"
                text = pytesseract.image_to_string(img, config=custom_config)

                if text.strip():
                    ocr_results.append(text.strip())

            except subprocess.TimeoutExpired:
                # Page rendering timed out - skip this page and continue
                pass
            except FileNotFoundError:
                # rmc not installed
                return None
            finally:
                if tmp_svg_path:
                    tmp_svg_path.unlink(missing_ok=True)
                if tmp_png_path:
                    tmp_png_path.unlink(missing_ok=True)
                if tmp_raw_path:
                    tmp_raw_path.unlink(missing_ok=True)

        return ocr_results if ocr_results else None

    except ImportError:
        # OCR dependencies not installed
        return None


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
                    except Exception:
                        pass
    except Exception as e:
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

            doc = fitz.open(pdf_path)
            try:
                idx = page_num - 1
                if 0 <= idx < len(doc):
                    label = doc[idx].get_label()
                    if label and label.strip() and label.strip().lower() != str(page_num):
                        return f"Page {label.strip()} (pdf-{page_num})"
            finally:
                doc.close()
        except Exception as e:
            logger.debug(f"Failed to read page label from {pdf_path}: {e}")

    return f"Page {page_num}"


@lru_cache(maxsize=16)
def _get_pdf_toc_entries(pdf_path_str: str) -> List[Tuple[int, str, int]]:
    """Cached helper to read Table of Contents entries from a PDF."""
    try:
        import pymupdf as fitz

        doc = fitz.open(pdf_path_str)
        try:
            return [(int(lvl), str(title).strip(), int(p)) for lvl, title, p in doc.get_toc()]
        finally:
            doc.close()
    except Exception as e:
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
    if not pdf_path or not Path(pdf_path).exists():
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

    Returns:
        Formatted Markdown header string.
    """
    page_label = format_page_label(page_num, pdf_path)
    breadcrumbs = get_pdf_toc_breadcrumbs(page_num, pdf_path)

    if breadcrumbs:
        lowest = breadcrumbs[-1]
        parents = breadcrumbs[:-1]
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
