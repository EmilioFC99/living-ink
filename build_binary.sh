#!/bin/bash
set -e

# Define output
NAME="living-ink"

# Clean previous builds
rm -rf build dist

echo "Building $NAME with PyInstaller..."

# Run PyInstaller
# --hidden-import "yaml": Ensure PyYAML is included
# --hidden-import "rmc" / "rmscene": Ensure rmc is included for direct import
# --hidden-import "cairosvg": Ensure cairosvg is included for SVG->PNG conversion
uv run pyinstaller scripts/process_notebook.py \
    --name "$NAME" \
    --onefile \
    --clean \
    --paths . \
    --add-data "remarkable_mcp/cleanup_prompt.txt:remarkable_mcp" \
    --add-data "remarkable_mcp/ocr_prompt.txt:remarkable_mcp" \
    --add-data "remarkable_mcp/openai_cleanup_prompt.txt:remarkable_mcp" \
    --hidden-import "google.cloud.vision" \
    --hidden-import "yaml" \
    --hidden-import "rmc" \
    --hidden-import "rmscene" \
    --hidden-import "cairosvg" \
    --hidden-import "google.api_core" \
    --hidden-import "google.auth" \
    --hidden-import "yaml" \
    --collect-all "grpc" \
    --collect-all "google.cloud.vision"

echo "Build complete."
echo "Binary location: dist/$NAME"
