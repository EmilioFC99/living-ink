#!/usr/bin/env python3
"""Entry point for the Living Ink interactive setup wizard."""

import sys
from pathlib import Path

# Add project root to sys.path
ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT))

from remarkable_mcp.setup_wizard import run_wizard  # noqa: E402

if __name__ == "__main__":
    try:
        run_wizard(repo_dir=ROOT)
    except KeyboardInterrupt:
        print("\n\nSetup cancelled by user.")
        sys.exit(0)
