#!/usr/bin/env python3
"""Process notebooks: CLI entry point forwarding to living_ink.pipeline."""

import sys
from pathlib import Path

import living_ink.pipeline as _pipeline
from living_ink.pipeline import *  # noqa: F401, F403
from living_ink.pipeline import main


def get_state_file_path(dest_name: str) -> Path:
    """Get the path to the state file for a specific destination."""
    mod = sys.modules.get("scripts.process_notebook", _pipeline)
    data_dir = getattr(mod, "DATA_DIR", _pipeline.DATA_DIR)
    root = getattr(mod, "ROOT", _pipeline.ROOT)
    new_path = data_dir / f"processed_notebooks_{dest_name}.json"
    legacy_path = root / f"processed_notebooks_{dest_name}.json"
    if not new_path.exists() and legacy_path.exists() and new_path != legacy_path:
        try:
            legacy_path.rename(new_path)
        except Exception:
            return legacy_path
    return new_path


if __name__ == "__main__":
    main()
