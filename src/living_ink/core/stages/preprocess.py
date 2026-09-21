"""Preparing a rendered page for the model that has to read it.

Handwriting rendered at panel size is thin, low-contrast and often sitting on
transparency. Every step here exists because the OCR pass reads better after
it, and none of it is reversible — the results are written beside the original
page rather than over it, so ``--keep-temp`` leaves both to compare.
"""

from pathlib import Path
from typing import List, Sequence

from PIL import Image, ImageFilter, ImageOps

#: How much a page is enlarged before it is read. Small strokes survive the
#: JPEG-quality save at this size and disappear below it.
UPSCALE = 1.5

#: Maximum dimension (width or height) for preprocessed images to ensure they
#: fit within standard local LLM vision context limits (e.g. Ollama's 4096-token
#: window) without sacrificing handwriting legibility.
MAX_DIMENSION = 1800


def preprocess_image(in_path: Path, out_path: Path) -> None:
    """Flatten, sharpen and enlarge one page image.

    Args:
        in_path: The rendered page.
        out_path: Where to write the prepared copy. Parent directories are
            created.
    """
    im = Image.open(in_path)
    # Always composite onto a white background, regardless of mode
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        bg.paste(im, (0, 0), im if im.mode == "RGBA" else None)
        im = bg.convert("RGB")
    else:
        im = im.convert("RGB")

    # Autocontrast
    im = ImageOps.autocontrast(im, cutoff=2)

    # Upscale, capped at MAX_DIMENSION so vision tokens fit LLM context limits
    w, h = im.size
    target_w = int(w * UPSCALE)
    target_h = int(h * UPSCALE)
    if max(target_w, target_h) > MAX_DIMENSION:
        scale = MAX_DIMENSION / max(target_w, target_h)
        target_w = int(target_w * scale)
        target_h = int(target_h * scale)
    im = im.resize((target_w, target_h), resample=Image.Resampling.LANCZOS)

    # Sharpen
    im = im.filter(ImageFilter.SHARPEN)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    im.save(out_path, quality=95)


def prepare_pages(images: Sequence[Path], out_dir: Path) -> List[Path]:
    """Prepare every page of a document for OCR.

    Args:
        images: The rendered pages, in page order.
        out_dir: Where the prepared copies go. Created if it does not exist.

    Returns:
        The prepared pages, in the same order — which is the order the
        transcriber will report them back in.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    prepared = []
    for image in images:
        out_path = out_dir / image.name
        preprocess_image(image, out_path)
        prepared.append(out_path)
    return prepared
