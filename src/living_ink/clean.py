"""OCR text cleanup and vision OCR module.

Delegates AI-powered text repair to the provider configured in
``config.yml``. See ``living_ink.providers`` for available providers.

Vision OCR:
    ``ocr_and_repair()`` reads handwritten text directly from a page image,
    combining OCR and text cleanup in a single API call. It is the only way a
    page becomes text: there is no separate OCR service behind it, so a
    provider without vision support (``supports_vision``) transcribes nothing
    rather than degrading to something worse.

Backward compatibility:
    - ``repair_text_with_openai()`` still works as the public API entry point.
    - If no provider is configured, falls back to ``NoneProvider`` (raw text).

Example:
    >>> from living_ink.clean import configure, repair_text_with_openai, ocr_and_repair
    >>> configure(Settings(ai_provider="gemini", ai_api_key="..."))
    >>> cleaned = repair_text_with_openai("messy OCR text")
    >>> text = ocr_and_repair("/path/to/page.png")
"""

import hashlib
import logging
import re
from pathlib import Path
from typing import Optional

from living_ink.providers import (
    VISION_SYSTEM_MESSAGE,
    NoneProvider,
    TextRepairProvider,
    get_provider,
)
from living_ink.settings import Settings

logger = logging.getLogger(__name__)

# Prompt file — provider-agnostic instructions for text cleanup
PROMPT_FILE = Path(__file__).parent / "cleanup_prompt.txt"

# Prompt file — instructions for vision-based OCR from images
OCR_PROMPT_FILE = Path(__file__).parent / "ocr_prompt.txt"

# Module-level provider instance, initialized lazily via configure()
_provider: Optional[TextRepairProvider] = None

# Whether the AI pass runs at all, as of the last configure(). None until then.
_repair_enabled: Optional[bool] = None

# ``ai.prompt_dir`` as of the last configure(). The two prompt files ship inside
# the package, which means an upgrade overwrites an edited one; pointing this at
# a directory of your own is how a customised prompt survives one.
_prompt_dir: Optional[str] = None


def _read_prompt(path: Path) -> str:
    """Read one of the two packaged prompt files.

    A missing file is an error rather than a fallback to a terser prompt
    written inline. Both files ship inside the package, so the only way one
    goes missing is a broken installation — and the silent substitute was worse
    than a crash twice over: the run pays for a page of OCR done against a
    prompt nobody wrote down, and
    :func:`transcription_fingerprint` hashes the substitute, so the cache fills
    up with transcriptions that look reusable and are not.

    Args:
        path: :data:`PROMPT_FILE` or :data:`OCR_PROMPT_FILE`.

    Returns:
        The prompt text, stripped.

    Raises:
        OSError: If the file cannot be read.
    """
    return path.read_text(encoding="utf-8").strip()


def _prompt_path(packaged: Path, prompt_dir: Optional[str]) -> Path:
    """Resolve a prompt file, preferring the user's own copy over the shipped one.

    The override is opt-in and per-file: a ``prompt_dir`` holding only
    ``ocr_prompt.txt`` overrides the OCR prompt and leaves the cleanup prompt
    packaged. A named directory that does not hold the file is not an error,
    because the alternative is refusing to run over a prompt the user never
    claimed to have replaced.

    Args:
        packaged: :data:`PROMPT_FILE` or :data:`OCR_PROMPT_FILE`.
        prompt_dir: ``ai.prompt_dir``, or None for the packaged prompts.

    Returns:
        The path to read the prompt from.
    """
    if prompt_dir:
        candidate = Path(prompt_dir).expanduser() / packaged.name
        if candidate.is_file():
            return candidate
    return packaged


def prompt_paths(prompt_dir: Optional[str]) -> tuple[Path, Path]:
    """Report which two files a run with these settings reads its prompts from.

    The config menu opens what a run actually reads, rather than the packaged
    copy, so editing a prompt and syncing cannot disagree about which file was
    edited.

    Args:
        prompt_dir: ``ai.prompt_dir``, or None for the packaged prompts.

    Returns:
        The OCR prompt path and the cleanup prompt path, in that order.
    """
    return (
        _prompt_path(OCR_PROMPT_FILE, prompt_dir),
        _prompt_path(PROMPT_FILE, prompt_dir),
    )


def _read_prompt_instructions(prompt_dir: Optional[str] = None) -> str:
    """Read the cleanup prompt instructions from the prompt file.

    Args:
        prompt_dir: ``ai.prompt_dir``, or None for the packaged prompt.

    Returns:
        The prompt text from ``cleanup_prompt.txt``.
    """
    return _read_prompt(_prompt_path(PROMPT_FILE, prompt_dir))


def configure(settings: Settings) -> None:
    """Initialize the AI provider from the run's resolved settings.

    Should be called once at startup (from ``living_ink.pipeline`` or
    ``SyncPipeline``). If not called, there is no AI pass at all —
    ``repair_text_with_openai()`` returns its input unchanged.

    Args:
        settings: The run's settings. Carries both the provider configuration
            and whether the AI pass runs at all.
    """
    global _provider, _repair_enabled, _prompt_dir
    _provider = get_provider(settings)
    _repair_enabled = settings.repair_enabled
    _prompt_dir = settings.ai_prompt_dir
    logger.info("AI text cleanup provider: %s", _provider.name)


def repair_enabled() -> bool:
    """Report whether the AI pass runs at all.

    Answered from the settings :func:`configure` was given. A caller reached
    without one — a direct script, a test — falls back to the environment,
    which is the only place this setting has ever been written.

    Returns:
        True unless the run has turned cleanup off.
    """
    global _repair_enabled
    if _repair_enabled is None:
        _repair_enabled = Settings.from_env().repair_enabled
    return _repair_enabled


def _get_provider() -> TextRepairProvider:
    """Get the configured provider, defaulting to no AI at all.

    Returns:
        The active ``TextRepairProvider`` instance, or :class:`NoneProvider`
        if ``configure()`` was never called.
    """
    global _provider
    if _provider is not None:
        return _provider

    # No environment fallback. A caller that never ran ``configure()`` has no
    # settings, and guessing OpenAI from a stray ``OPENAI_API_KEY`` sends
    # handwriting to a provider nobody chose. ``Settings.from_env()`` is the
    # supported way to configure without a config file.
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
        disabled via ``repair_enabled``, or the input is empty.
    """
    if not repair_enabled():
        return text

    if not text or not text.strip():
        return text

    provider = _get_provider()
    instructions = _read_prompt_instructions(_prompt_dir)

    cleaned = provider.repair_text(text, instructions)
    return normalize_callout_annotations(cleaned)


def normalize_callout_annotations(text: str) -> str:
    """Normalize common AI annotation headers into standard Obsidian callouts.

    Ensures that highlighted passages, boxed passages, and margin notes
    consistently use Obsidian callout syntax:
        > [!quote] Highlight
        > [!example] Boxed Passage
        > [!note] Margin Note

    Args:
        text: Raw or cleaned transcription text.

    Returns:
        Text with annotations normalized to Obsidian callouts.
    """
    if not text or not text.strip():
        return text

    s = text.strip()
    if s.startswith("```markdown"):
        s = s[11:].lstrip("\r\n")
        if s.endswith("```"):
            s = s[:-3].rstrip()
    elif s.startswith("```"):
        s = s[3:].lstrip("\r\n")
        if s.endswith("```"):
            s = s[:-3].rstrip()

    has_legacy = bool(
        re.search(
            r"(?i)^(?:\[\s*(?:boxed|highlight|margin).*?\]:?|\*{0,2}(?:boxed|highlight|margin).*?:?\*{0,2}:?)\s*$",
            s,
            flags=re.MULTILINE,
        )
    )
    if not has_legacy:
        return s

    s = re.sub(
        r"(?i)^(?:\[\s*boxed(?:\s+passage|\s+text)?.*?\]:?|\*{0,2}boxed(?:\s+passage|\s+text)?.*?:?\*{0,2}:?)\s*$",
        "__CALLOUT_BOXED__",
        s,
        flags=re.MULTILINE,
    )
    s = re.sub(
        r"(?i)^(?:\[\s*highlight(?:ed)?(?:\s+passage|\s+text)?.*?\]:?|\*{0,2}highlight(?:ed)?(?:\s+passage|\s+text)?.*?:?\*{0,2}:?)\s*$",
        "__CALLOUT_QUOTE__",
        s,
        flags=re.MULTILINE,
    )
    s = re.sub(
        r"(?i)^(?:\[\s*margin(?:\s+annotation|\s+note)?.*?\]:?|\*{0,2}margin(?:\s+annotations?|\s+notes?).*?:?\*{0,2}:?)\s*$",
        "__CALLOUT_NOTE__",
        s,
        flags=re.MULTILINE,
    )

    lines = s.split("\n")
    out_lines = []
    in_callout = False

    for line in lines:
        stripped = line.strip()
        if stripped == "__CALLOUT_BOXED__":
            if in_callout:
                out_lines.append("")
            out_lines.append("> [!example] Boxed Passage")
            in_callout = True
        elif stripped == "__CALLOUT_QUOTE__":
            if in_callout:
                out_lines.append("")
            out_lines.append("> [!quote] Highlight")
            in_callout = True
        elif stripped == "__CALLOUT_NOTE__":
            if in_callout:
                out_lines.append("")
            out_lines.append("> [!note] Margin Note")
            in_callout = True
        elif in_callout:
            if stripped.startswith("> [!"):
                out_lines.append(line)
            elif (
                stripped.startswith("---")
                or stripped.startswith("<span")
                or stripped.startswith("###")
            ):
                in_callout = False
                out_lines.append(line)
            elif not stripped:
                out_lines.append(">")
            else:
                if stripped.startswith(">"):
                    out_lines.append(line)
                else:
                    out_lines.append(f"> {line}")
        else:
            out_lines.append(line)

    res = "\n".join(out_lines)
    res = re.sub(r">\s*\n+(?=> \[!|\Z)", "\n\n", res)
    return res.strip()


def _read_ocr_instructions(prompt_dir: Optional[str] = None) -> str:
    """Read the OCR prompt instructions from the prompt file.

    Args:
        prompt_dir: ``ai.prompt_dir``, or None for the packaged prompt.

    Returns:
        The prompt text from ``ocr_prompt.txt``.
    """
    return _read_prompt(_prompt_path(OCR_PROMPT_FILE, prompt_dir))


def transcription_fingerprint(settings: Settings) -> str:
    """Identify everything, other than the page itself, that shapes a transcription.

    A cached transcription is only reusable while the thing that produced it is
    unchanged: the provider and the model, the sampling temperature, the
    language asked for, the system prompt the vision call opens with, and the
    two prompt files, which ship inside the package and are meant to be edited.
    Folding them into one digest means an edited prompt or a switched model
    misses the cache instead of quietly serving the old answer. It hashes the
    prompts a run would actually *send*, resolved through ``ai.prompt_dir``, so
    pointing that at a directory of edited prompts misses the cache too.

    **This builds nothing.** It used to read ``_get_provider().name``, which
    was only ever a way of spelling "provider and model" through an object that
    happened to be lying around — and which made the digest unavailable before
    a provider existed. Change detection needs it *before* any page reaches
    OCR, to decide whether there is work at all, so every input is now a value
    on a frozen dataclass and no network client is constructed.

    Args:
        settings: The run's resolved settings.

    Returns:
        A short hex digest identifying the current transcription behaviour.
    """
    parts = (
        settings.ai_provider or "",
        settings.ai_model or "",
        repr(settings.ai_temperature),
        settings.ai_language or "",
        VISION_SYSTEM_MESSAGE,
        _read_ocr_instructions(settings.ai_prompt_dir),
        _read_prompt_instructions(settings.ai_prompt_dir),
    )
    digest = hashlib.sha256("\0".join(parts).encode("utf-8"))
    return digest.hexdigest()[:16]


def vision_ocr_available() -> bool:
    """Check if the configured AI provider supports vision-based OCR.

    Returns:
        ``True`` if the provider supports multimodal image input and
        text repair is enabled. ``False`` otherwise.
    """
    if not repair_enabled():
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
    if not repair_enabled():
        return None

    provider = _get_provider()

    if not provider.supports_vision:
        logger.error(
            "Provider '%s' cannot read an image, and it is the only thing that reads "
            "pages. Set 'ai.provider' to one with vision support.",
            provider.name,
        )
        return None

    instructions = _read_ocr_instructions(_prompt_dir)
    result = provider.ocr_image(image_path, instructions)

    if not result:
        logger.warning(
            "Vision OCR returned empty result (%s) for %s.",
            provider.name,
            image_path,
        )
        return None

    return normalize_callout_annotations(result)
