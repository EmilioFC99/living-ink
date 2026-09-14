"""
OCR text cleanup module.

Delegates AI-powered text repair to the provider configured in config.yml.
See remarkable_mcp/providers.py for available providers.

Backward compatibility:
    - repair_text_with_openai() still works as the public API entry point.
    - If no provider is configured, falls back to NoneProvider (raw text).
    - Legacy OPENAI_API_KEY env var is respected if 'ai' config section is absent.
"""

import logging
import os
from pathlib import Path
from typing import Optional

from remarkable_mcp.providers import NoneProvider, TextRepairProvider, get_provider

logger = logging.getLogger(__name__)

# Prompt file — provider-agnostic instructions for text cleanup
PROMPT_FILE = Path(__file__).parent / "cleanup_prompt.txt"
# Fallback to legacy name if new file doesn't exist yet
if not PROMPT_FILE.exists():
    PROMPT_FILE = Path(__file__).parent / "openai_cleanup_prompt.txt"

# Module-level provider instance, initialized lazily via configure()
_provider: Optional[TextRepairProvider] = None

# Feature toggle (can be disabled via env var)
ENABLE_REPAIR = os.environ.get("ENABLE_REPAIR", "true").lower() in ("true", "1", "yes")


def _read_prompt_instructions() -> str:
    """Read the cleanup prompt instructions from the prompt file."""
    if PROMPT_FILE.exists():
        return PROMPT_FILE.read_text(encoding="utf-8").strip()
    return "Clean this OCR text."


def configure(config: dict) -> None:
    """
    Initialize the AI provider from the parsed YAML config.

    Should be called once at startup (from process_notebook.py).
    If not called, repair_text_with_openai() will attempt to
    auto-configure from legacy env vars.

    Args:
        config: The parsed YAML configuration dictionary.
    """
    global _provider
    _provider = get_provider(config)
    logger.info(f"AI text cleanup provider: {_provider.name}")


def _get_provider() -> TextRepairProvider:
    """
    Get the configured provider, with lazy initialization fallback.

    If configure() was never called (e.g., direct script usage),
    attempts to build a provider from legacy environment variables.
    """
    global _provider
    if _provider is not None:
        return _provider

    # Lazy fallback: try legacy env var configuration
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if api_key:
        logger.info("Auto-configuring from OPENAI_API_KEY env var (legacy mode).")
        _provider = get_provider({"openai": {"api_key": api_key}})
    else:
        logger.info("No AI provider configured. Text cleanup disabled.")
        _provider = NoneProvider()

    return _provider


def repair_text_with_openai(text: str) -> str:
    """
    Clean up OCR text using the configured AI provider.

    Despite the legacy function name, this works with ANY configured provider
    (Gemini, Ollama, OpenAI, etc.). The name is kept for backward compatibility
    with existing callers.

    Args:
        text: Raw OCR text to clean up.

    Returns:
        Cleaned text, or the original text if repair fails or is disabled.
    """
    if not ENABLE_REPAIR:
        return text

    if not text or not text.strip():
        return text

    provider = _get_provider()
    instructions = _read_prompt_instructions()

    return provider.repair_text(text, instructions)
