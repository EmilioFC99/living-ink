import os
import sys
from pathlib import Path

from PIL import Image

DATA_DIR = Path(os.environ.get("LIVING_INK_DATA_DIR", "data"))
IN = DATA_DIR / "remarkable_pngs"
OUT = DATA_DIR / "remarkable_pngs_white"
OUT.mkdir(parents=True, exist_ok=True)

if not IN.exists():
    print("Input directory not found:", IN)
    sys.exit(1)

pngs = sorted([p for p in IN.iterdir() if p.suffix.lower() in (".png", ".jpg", ".jpeg")])
if not pngs:
    print("No images found in", IN)
    sys.exit(0)

for p in pngs:
    try:
        im = Image.open(p).convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        bg.paste(im, (0, 0), im)
        out = bg.convert("RGB")
        out_path = OUT / p.name
        out.save(out_path, quality=95)
        print("Wrote", out_path)
    except Exception as e:
        print("Failed", p, e)

print("Done. Processed", len(pngs), "files ->", OUT)
