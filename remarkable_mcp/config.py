"""Configuration and directory path management for Living Ink.

Supports standard XDG directories for global tool installations while
maintaining full backward compatibility with repository-relative paths
during local development.
"""

import os
from pathlib import Path
from typing import Optional


def get_config_path(repo_dir: Optional[Path] = None) -> Path:
    """Find the config.yml file path.

    Resolution order:
    1. LIVING_INK_CONFIG_DIR environment variable
    2. Explicit repo_dir (if provided and config exists)
    3. Current working directory (./config/config.yml or ./config.yml)
    4. Source repo root (if running inside git checkout)
    5. Standard XDG user config: ~/.config/living-ink/config.yml
    6. Local repo fallback: ~/repos/living-ink/config/config.yml
    7. Legacy user config: ~/.living-ink/config.yml

    Args:
        repo_dir: Optional repository root.

    Returns:
        Path to config.yml (may or may not exist yet).
    """
    env_dir = os.environ.get("LIVING_INK_CONFIG_DIR")
    if env_dir:
        return (Path(env_dir) / "config.yml").resolve()

    if repo_dir:
        for candidate in [repo_dir / "config" / "config.yml", repo_dir / "config.yml"]:
            if candidate.exists():
                return candidate.resolve()
        return (repo_dir / "config" / "config.yml").resolve()

    cwd = Path.cwd().resolve()
    for candidate in [cwd / "config" / "config.yml", cwd / "config.yml"]:
        if candidate.exists():
            return candidate

    src_repo = Path(__file__).resolve().parent.parent
    if (src_repo / "pyproject.toml").exists():
        for candidate in [src_repo / "config" / "config.yml", src_repo / "config.yml"]:
            if candidate.exists():
                return candidate.resolve()

    # Standard XDG config: ~/.config/living-ink/config.yml
    xdg_base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    xdg_config = xdg_base / "living-ink" / "config.yml"
    if xdg_config.exists():
        return xdg_config.resolve()

    # Known local checkout fallback
    user_repo_cfg = Path.home() / "repos" / "living-ink" / "config" / "config.yml"
    if user_repo_cfg.exists():
        return user_repo_cfg.resolve()

    # Legacy config: ~/.living-ink/config.yml
    legacy_config = Path.home() / ".living-ink" / "config.yml"
    if legacy_config.exists():
        return legacy_config.resolve()

    if repo_dir:
        return (repo_dir / "config" / "config.yml").resolve()

    return xdg_config.resolve()


def get_config_dir(repo_dir: Optional[Path] = None) -> Path:
    """Find the configuration directory.

    Args:
        repo_dir: Optional repository root.

    Returns:
        Path to the configuration directory.
    """
    return get_config_path(repo_dir).parent


def get_data_dir(repo_dir: Optional[Path] = None) -> Path:
    """Find the data directory for logs, state, and rendered artifacts.

    Resolution order:
    1. LIVING_INK_DATA_DIR environment variable
    2. Local repository if running in git checkout (e.g. repo/data)
    3. User standard XDG data directory: ~/.local/share/living-ink/

    Args:
        repo_dir: Optional repository root.

    Returns:
        Path to the data directory.
    """
    env_data = os.environ.get("LIVING_INK_DATA_DIR")
    if env_data:
        return Path(env_data).resolve()

    if repo_dir and (repo_dir / "pyproject.toml").exists():
        return (repo_dir / "data").resolve()

    src_repo = Path(__file__).resolve().parent.parent
    if (src_repo / "pyproject.toml").exists():
        return (src_repo / "data").resolve()

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
