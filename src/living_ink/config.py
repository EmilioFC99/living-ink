"""Configuration and directory path management for Living Ink.

Supports standard XDG directories for global tool installations while
maintaining full backward compatibility with repository-relative paths
during local development and testing.
"""

import os
from pathlib import Path
from typing import Optional


def find_repo_root() -> Optional[Path]:
    """Locate the project root when running from a source checkout.

    The current working directory wins if it looks like a checkout or already
    holds a config file, so that ``cd``-ing into a clone behaves the way a user
    expects. Otherwise the package's own location is walked upwards looking for
    ``pyproject.toml``.

    This is the single root-discovery implementation for the whole package; the
    CLI and the pipeline both call it rather than rolling their own.

    Returns:
        Path to the project root, or None when installed as a plain wheel with
        no checkout in sight.
    """
    cwd = Path.cwd().resolve()
    if (
        (cwd / "pyproject.toml").exists()
        or (cwd / "config" / "config.yml").exists()
        or (cwd / "config.yml").exists()
    ):
        return cwd

    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return None


def get_config_path(repo_dir: Optional[Path] = None) -> Path:
    """Find the config.yml file path.

    Resolution order:
    1. LIVING_INK_CONFIG environment variable (explicit file path)
    2. LIVING_INK_CONFIG_DIR environment variable (directory containing config.yml)
    3. Explicit repo_dir (if provided and config exists or for test mocks)
    4. Standard XDG user config: ~/.config/living-ink/config.yml (if exists)
    5. Local repository fallback: repo/config/config.yml (if running inside git checkout)
    6. Current working directory: ./config/config.yml or ./config.yml (if exists)
    7. Legacy user config: ~/.living-ink/config.yml (if exists)
    8. Standard XDG user config: ~/.config/living-ink/config.yml (default destination)

    Args:
        repo_dir: Optional repository root (e.g. for testing).

    Returns:
        Path to config.yml (may or may not exist yet).
    """
    env_file = os.environ.get("LIVING_INK_CONFIG")
    if env_file:
        return Path(env_file).resolve()

    env_dir = os.environ.get("LIVING_INK_CONFIG_DIR")
    if env_dir:
        return (Path(env_dir) / "config.yml").resolve()

    if repo_dir:
        for candidate in [repo_dir / "config" / "config.yml", repo_dir / "config.yml"]:
            if candidate.exists():
                return candidate.resolve()
        return (repo_dir / "config" / "config.yml").resolve()

    # Standard XDG config: ~/.config/living-ink/config.yml
    xdg_base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    xdg_config = xdg_base / "living-ink" / "config.yml"
    if xdg_config.exists():
        return xdg_config.resolve()

    # Fallback to local git checkout config if present
    repo_root = find_repo_root()
    if repo_root:
        for candidate in [repo_root / "config" / "config.yml", repo_root / "config.yml"]:
            if candidate.exists():
                return candidate.resolve()

    # Fallback to cwd config if present
    cwd = Path.cwd().resolve()
    for candidate in [cwd / "config" / "config.yml", cwd / "config.yml"]:
        if candidate.exists():
            return candidate

    # Legacy config: ~/.living-ink/config.yml
    legacy_config = Path.home() / ".living-ink" / "config.yml"
    if legacy_config.exists():
        return legacy_config.resolve()

    return xdg_config.resolve()


def get_data_dir(repo_dir: Optional[Path] = None) -> Path:
    """Find the data directory for logs, state, and rendered artifacts.

    Resolution order:
    1. LIVING_INK_DATA_DIR environment variable
    2. Explicit repo_dir (if provided and explicitly contains data or for testing)
    3. User standard XDG data directory: ~/.local/share/living-ink/

    Args:
        repo_dir: Optional repository root (e.g. for testing).

    Returns:
        Path to the data directory.
    """
    env_data = os.environ.get("LIVING_INK_DATA_DIR")
    if env_data:
        return Path(env_data).resolve()

    if repo_dir:
        return (repo_dir / "data").resolve()

    xdg_data = (
        Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "living-ink"
    )
    return xdg_data.resolve()


def get_logs_dir(repo_dir: Optional[Path] = None) -> Path:
    """Find the logs directory.

    Args:
        repo_dir: Optional repository root.

    Returns:
        Path to logs directory.
    """
    return get_data_dir(repo_dir) / "logs"
