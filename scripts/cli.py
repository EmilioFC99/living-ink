#!/usr/bin/env python3
"""CLI wrapper script for Living Ink."""

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from living_ink.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
