"""OCR text cleanup and vision OCR module.

Delegates AI-powered text repair to the provider configured in
``config.yml``. See ``remarkable_mcp.providers`` for available providers.

Vision OCR:
    When the configured provider supports vision (e.g., Gemini, GPT-4o),
    ``ocr_and_repair()`` can read handwritten text directly from page
    images — combining OCR + text cleanup in a single API call. This
    eliminates the need for a separate OCR service like Google Cloud Vision.

Backward compatibility:
    - ``repair_text_with_openai()`` still works as the public API entry point.
    - If no provider is configured, falls back to ``NoneProvider`` (raw text).
    - Legacy ``OPENAI_API_KEY`` env var is respected if ``ai`` config section
      is absent.

Example:
    >>> from remarkable_mcp.clean import configure, repair_text_with_openai, ocr_and_repair
    >>> configure({"ai": {"provider": "gemini", "api_key": "..."}})
    >>> cleaned = repair_text_with_openai("messy OCR text")
    >>> text = ocr_and_repair("/path/to/page.png")
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

# Prompt file — instructions for vision-based OCR from images
OCR_PROMPT_FILE = Path(__file__).parent / "ocr_prompt.txt"

# Module-level provider instance, initialized lazily via configure()
_provider: Optional[TextRepairProvider] = None

# Feature toggle (can be disabled via env var)
ENABLE_REPAIR = os.environ.get("ENABLE_REPAIR", "true").lower() in (
    "true",
    "1",
    "yes",
)


def _read_prompt_instructions() -> str:
    """Read the cleanup prompt instructions from the prompt file.

    Returns:
        The prompt text from ``cleanup_prompt.txt``, or a minimal
        fallback string if the file is missing.
    """
    if PROMPT_FILE.exists():
        return PROMPT_FILE.read_text(encoding="utf-8").strip()
    return "Clean this OCR text."


def configure(config: dict) -> None:
    """Initialize the AI provider from the parsed YAML config.

    Should be called once at startup (from ``process_notebook.py``).
    If not called, ``repair_text_with_openai()`` will attempt to
    auto-configure from legacy env vars.

    Args:
        config: The parsed YAML configuration dictionary containing
            an ``ai`` section (or legacy ``openai`` section).
    """
    global _provider
    _provider = get_provider(config)
    logger.info("AI text cleanup provider: %s", _provider.name)


def _get_provider() -> TextRepairProvider:
    """Get the configured provider, with lazy initialization fallback.

    If ``configure()`` was never called (e.g., direct script usage),
    attempts to build a provider from legacy environment variables.

    Returns:
        The active ``TextRepairProvider`` instance.
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
    """Clean up OCR text using the configured AI provider.

    Despite the legacy function name, this works with ANY configured
    provider (Gemini, Ollama, OpenAI, etc.). The name is kept for
    backward compatibility with existing callers.

    Args:
        text: Raw OCR text to clean up.

    Returns:
        Cleaned text, or the original text if repair fails, is
        disabled via ``ENABLE_REPAIR``, or the input is empty.
    """
    if not ENABLE_REPAIR:
        return text

    if not text or not text.strip():
        return text

    provider = _get_provider()
    instructions = _read_prompt_instructions()

    return provider.repair_text(text, instructions)


def _read_ocr_instructions() -> str:
    """Read the OCR prompt instructions from the prompt file.

    Returns:
        The prompt text from ``ocr_prompt.txt``, or a minimal
        fallback string if the file is missing.
    """
    if OCR_PROMPT_FILE.exists():
        return OCR_PROMPT_FILE.read_text(encoding="utf-8").strip()
    return "Transcribe the handwritten text from this notebook page image."


def vision_ocr_available() -> bool:
    """Check if the configured AI provider supports vision-based OCR.

    Returns:
        ``True`` if the provider supports multimodal image input and
        text repair is enabled. ``False`` otherwise.
    """
    if not ENABLE_REPAIR:
        return False
    provider = _get_provider()
    return provider.supports_vision


def ocr_and_repair(image_path: str) -> Optional[str]:
    """Perform OCR on a handwritten page image using AI vision.

    Uses the configured AI provider's vision capability to read handwriting
    directly from the image and return clean text. This combines OCR + text
    cleanup into a single API call, eliminating the need for a separate
    OCR service like Google Cloud Vision.

    Args:
        image_path: Absolute path to the PNG image file.

    Returns:
        Transcribed and cleaned text from the image, or ``None`` if
        the provider does not support vision OCR or if repair is disabled.
    """
    if not ENABLE_REPAIR:
        return None

    provider = _get_provider()

    if not provider.supports_vision:
        logger.info(
            "Provider '%s' does not support vision OCR. Falling back to traditional OCR.",
            provider.name,
        )
        return None

    instructions = _read_ocr_instructions()
    result = provider.ocr_image(image_path, instructions)

    if not result:
        logger.warning(
            "Vision OCR returned empty result (%s) for %s.",
            provider.name,
            image_path,
        )
        return None

    return result
