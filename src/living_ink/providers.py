"""AI text repair and vision OCR providers.

Architecture:
    - ``UniversalChatProvider``: Works with ANY OpenAI-compatible API endpoint.
      Supports both text-only cleanup and multimodal vision OCR (reads
      handwritten images directly via the standard ``image_url`` format).
    - ``NoneProvider``: No-op, returns raw text unchanged.
    - ``get_provider``: Factory that reads config and returns the right provider.

No provider-specific code. No external SDKs required.
Just standard HTTP to a configurable endpoint.

Supported providers (via presets):
    openai, gemini, ollama, groq, openrouter, mistral, together
    + any custom OpenAI-compatible endpoint via ``provider: "custom"``

Example:
    >>> from living_ink.providers import get_provider
    >>> provider = get_provider({"ai": {"provider": "gemini", "api_key": "..."}})
    >>> cleaned = provider.repair_text("messy OCR text", "Clean this text.")
    >>> text = provider.ocr_image("/path/to/page.png", "Transcribe this page.")
"""

import abc
import base64
import json
import logging
import urllib.error
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Named presets — users pick a name, we fill in the endpoint details.
# Adding a new provider = adding an entry here. No code changes needed.
# ---------------------------------------------------------------------------

PROVIDER_PRESETS = {
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer",
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "default_model": "gemini-flash-latest",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer",
    },
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "default_model": "llama3.2",
        "auth_header": None,
        "auth_prefix": None,
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "default_model": "llama-3.3-70b-versatile",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "google/gemini-2.0-flash-exp:free",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer",
    },
    "mistral": {
        "base_url": "https://api.mistral.ai/v1",
        "default_model": "mistral-small-latest",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer",
    },
    "together": {
        "base_url": "https://api.together.xyz/v1",
        "default_model": "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer",
    },
}


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------


class TextRepairProvider(abc.ABC):
    """Abstract base class for AI text cleanup providers.

    All providers must implement ``repair_text()`` and the ``name`` property.
    Subclass this to add new provider types beyond the built-in
    ``UniversalChatProvider`` and ``NoneProvider``.
    """

    @abc.abstractmethod
    def repair_text(self, raw_text: str, instructions: str) -> str:
        """Clean up OCR text using AI.

        Args:
            raw_text: The raw OCR output to clean.
            instructions: Prompt instructions for the AI
                (loaded from cleanup_prompt.txt).

        Returns:
            Cleaned text, or the original ``raw_text`` if cleanup
            fails or is disabled.
        """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Human-readable provider name for logging."""

    @property
    def supports_vision(self) -> bool:
        """Whether this provider can perform vision-based OCR on images.

        Returns:
            ``True`` if the provider supports multimodal image input
            via the OpenAI-compatible vision format. Defaults to ``False``.
        """
        return False

    def ocr_image(self, image_path: str, instructions: str) -> str:
        """Perform OCR on an image using the AI provider's vision capability.

        Reads the image file, encodes it as base64, and sends it to the AI
        along with transcription instructions in a single API call. This
        combines OCR + text cleanup into one step.

        Args:
            image_path: Absolute path to the PNG image file.
            instructions: Prompt instructions for transcription and cleanup.

        Returns:
            Transcribed and cleaned text from the image.

        Raises:
            NotImplementedError: If the provider does not support vision OCR.
        """
        raise NotImplementedError(f"Provider '{self.name}' does not support vision OCR.")


# ---------------------------------------------------------------------------
# NoneProvider — no AI cleanup
# ---------------------------------------------------------------------------


class NoneProvider(TextRepairProvider):
    """No-op provider that returns raw OCR text unchanged.

    Use when AI text cleanup is disabled (``provider: "none"`` in config).
    Makes no external API calls.
    """

    def repair_text(self, raw_text: str, instructions: str) -> str:
        """Return raw text unchanged.

        Args:
            raw_text: The raw OCR output.
            instructions: Ignored by this provider.

        Returns:
            The original ``raw_text``, unmodified.
        """
        return raw_text

    @property
    def name(self) -> str:
        """Return the provider display name."""
        return "None (no AI cleanup)"


# ---------------------------------------------------------------------------
# UniversalChatProvider — works with any OpenAI-compatible API
# ---------------------------------------------------------------------------

SYSTEM_MESSAGE = (
    "You are an expert editor for handwritten notes. "
    "Your goal is to restore the author's original intent "
    "by fixing OCR misinterpretations while preserving their voice."
)

VISION_SYSTEM_MESSAGE = (
    "You are an expert handwriting transcription assistant. "
    "Your goal is to accurately read handwritten text from notebook page "
    "images and produce clean, well-formatted plain text output."
)


class UniversalChatProvider(TextRepairProvider):
    """Provider that works with any OpenAI-compatible chat completions API.

    Sends standard chat completion requests via HTTP using stdlib ``urllib``.
    No external SDK dependencies required.

    Tested with: OpenAI, Google Gemini, Ollama, Groq, OpenRouter,
    Mistral, Together, LM Studio, vLLM, and any future provider
    that supports the standard chat completions format.

    Attributes:
        base_url: The API base URL (without trailing slash).
        api_key: API key for authentication.
        model: Model identifier string.
        temperature: Sampling temperature (0.0–2.0).
        auth_header: HTTP header name for auth (e.g., "Authorization").
        auth_prefix: Prefix before the key (e.g., "Bearer").
    """

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        model: str = "",
        temperature: float = 0.3,
        auth_header: Optional[str] = "Authorization",
        auth_prefix: Optional[str] = "Bearer",
        provider_label: str = "custom",
    ):
        """Initialize the provider with endpoint and auth configuration.

        Args:
            base_url: The API base URL (e.g., "https://api.openai.com/v1").
                Trailing slashes are stripped automatically.
            api_key: API key for authentication. Empty string for local
                providers like Ollama that don't require auth.
            model: Model identifier (e.g., "gpt-4o-mini", "gemini-2.0-flash").
            temperature: Sampling temperature. Lower values produce more
                deterministic output. Defaults to 0.3.
            auth_header: HTTP header name for the API key. Set to ``None``
                to skip auth entirely. Defaults to "Authorization".
            auth_prefix: Prefix before the API key in the auth header.
                Set to ``None`` for raw key headers. Defaults to "Bearer".
            provider_label: Human-readable label for logging.
                Defaults to "custom".
        """
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.auth_header = auth_header
        self.auth_prefix = auth_prefix
        self._provider_label = provider_label

    @property
    def name(self) -> str:
        """Return the provider display name including model."""
        return f"{self._provider_label} ({self.model})"

    def _build_url(self) -> str:
        """Build the chat completions endpoint URL.

        Returns:
            Full URL to the chat completions endpoint.
        """
        return f"{self.base_url}/chat/completions"

    def _post_chat(
        self,
        messages: list,
        temperature: float,
        purpose: str = "AI API",
    ) -> str:
        """POST an OpenAI-compatible chat completion and return the reply text.

        This is the single HTTP path for the provider. Text cleanup and vision
        OCR differ only in the messages they build and the temperature they
        want, so they both funnel through here rather than each carrying their
        own copy of the request construction, auth, and error handling.

        Uses stdlib ``urllib`` — no external HTTP libraries required.

        Args:
            messages: The ``messages`` array, already in OpenAI wire format.
                For vision requests the user content is a list of parts.
            temperature: Sampling temperature for this request.
            purpose: Human-readable label used to prefix log lines, so a
                failure is attributable to cleanup or to OCR.

        Returns:
            The assistant's reply, stripped. Empty string on any failure —
            callers are expected to degrade gracefully (fall back to the raw
            text) rather than abort the sync over one unreachable API.
        """
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }

        req = urllib.request.Request(
            self._build_url(),
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
        )
        req.add_header("Content-Type", "application/json")

        # Some local providers (Ollama) need no auth at all.
        if self.auth_header and self.api_key:
            value = f"{self.auth_prefix} {self.api_key}" if self.auth_prefix else self.api_key
            req.add_header(self.auth_header, value)

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            logger.error("%s HTTP Error (%s): %s %s", purpose, self.name, e.code, e.reason)
            try:
                logger.error("Details: %s", e.read().decode("utf-8"))
            except Exception:
                pass
            return ""
        except urllib.error.URLError as e:
            logger.error("%s Connection Error (%s): %s", purpose, self.name, e.reason)
            return ""
        except Exception as e:
            logger.error("%s Unexpected Error (%s): %s", purpose, self.name, e)
            return ""

        choices = body.get("choices", [])
        if not choices:
            return ""

        choice = choices[0]
        content = choice.get("message", {}).get("content")
        if not content:
            # A safety filter returns a well-formed response with no content;
            # say so explicitly rather than reporting a mysterious empty result.
            finish_reason = choice.get("finish_reason")
            if finish_reason and "filter" in str(finish_reason).lower():
                logger.warning(
                    "%s completion blocked by filter (%s): %s",
                    purpose,
                    self.name,
                    finish_reason,
                )
            return ""

        return content.strip()

    def _chat(self, prompt: str) -> str:
        """Send a plain text chat completion request.

        Args:
            prompt: The user message content to send.

        Returns:
            The assistant's response content, or empty string on any error.
        """
        return self._post_chat(
            [
                {"role": "system", "content": SYSTEM_MESSAGE},
                {"role": "user", "content": prompt},
            ],
            temperature=self.temperature,
        )

    def repair_text(self, raw_text: str, instructions: str) -> str:
        """Clean up OCR text by sending it to the configured AI API.

        Combines the prompt instructions with the raw text and sends
        a chat completion request. Falls back to the original text
        if the API call fails or returns empty.

        Args:
            raw_text: The raw OCR output to clean.
            instructions: Prompt instructions prepended to the text.

        Returns:
            Cleaned text from the AI, or the original ``raw_text``
            if the API call fails.
        """
        if not raw_text or not raw_text.strip():
            return raw_text

        prompt = f"{instructions}\n\nTEXT:\n{raw_text}"
        result = self._chat(prompt)

        if not result:
            logger.warning(
                "AI cleanup returned empty result (%s). Using raw text.",
                self.name,
            )
            return raw_text

        return result.strip()

    @property
    def supports_vision(self) -> bool:
        """Whether this provider supports vision-based OCR.

        Returns:
            Always ``True`` — all OpenAI-compatible endpoints supported
            by this class handle multimodal image input.
        """
        return True

    #: Extension -> MIME type for the data URI. Anything else is sent as PNG,
    #: which is what the renderer produces.
    _MIME_TYPES = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
    }

    def ocr_image(self, image_path: str, instructions: str) -> str:
        """Perform OCR on an image via the provider's vision capability.

        Reads the image, encodes it as a base64 data URI, and sends a
        multimodal chat completion. The AI reads the handwriting and returns
        clean text in a single step — no separate OCR service required.

        Uses the standard OpenAI vision format (``image_url`` with data URI),
        which is supported by Gemini, OpenAI GPT-4o, Ollama (LLaVA), Groq,
        OpenRouter, Mistral Pixtral, and others.

        Note:
            Temperature is pinned to 0.2 regardless of ``self.temperature``:
            transcription wants determinism, whereas text repair may be
            configured looser.

        Args:
            image_path: Absolute path to the image file.
            instructions: Prompt instructions for transcription
                (loaded from ``ocr_prompt.txt``).

        Returns:
            Transcribed and cleaned text from the image, or empty
            string on any error.
        """
        with open(image_path, "rb") as f:
            b64_string = base64.b64encode(f.read()).decode("ascii")

        path_str = str(image_path)
        ext = path_str.rsplit(".", 1)[-1].lower() if "." in path_str else "png"
        mime_type = self._MIME_TYPES.get(ext, "image/png")

        return self._post_chat(
            [
                {"role": "system", "content": VISION_SYSTEM_MESSAGE},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": instructions},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime_type};base64,{b64_string}"},
                        },
                    ],
                },
            ],
            temperature=0.2,
            purpose="Vision OCR",
        )


# ---------------------------------------------------------------------------
# Factory — builds the right provider from config
# ---------------------------------------------------------------------------


def get_provider(config: dict) -> TextRepairProvider:
    """Create a text repair provider from a YAML config dictionary.

    Factory function that reads the ``ai`` section of the config and
    returns the appropriate provider instance. Supports named presets,
    custom endpoints, and backward-compatible legacy ``openai`` config.

    Args:
        config: The parsed YAML configuration dictionary. Expected
            structure::

                {
                    "ai": {
                        "provider": "gemini",
                        "api_key": "...",
                        "model": "gemini-flash-latest",  # optional
                    }
                }

    Returns:
        A configured ``TextRepairProvider`` instance.

    Raises:
        ValueError: If the provider name is unknown or if ``custom``
            provider is missing ``base_url``.

    Examples:
        >>> provider = get_provider({"ai": {"provider": "none"}})
        >>> isinstance(provider, NoneProvider)
        True

        >>> provider = get_provider({"ai": {"provider": "gemini", "api_key": "k"}})
        >>> provider.model
        'gemini-flash-latest'
    """
    ai_config = config.get("ai", {})
    provider_name = str(ai_config.get("provider", "")).strip().lower()

    # ── Backward compat: legacy 'openai' section without 'ai' ──
    if not provider_name and "openai" in config:
        openai_cfg = config["openai"]
        api_key = str(openai_cfg.get("api_key", "")).strip()
        if api_key and "YOUR" not in api_key:
            logger.info("Using legacy 'openai' config. Consider migrating to the new 'ai' section.")
            return UniversalChatProvider(
                base_url="https://api.openai.com/v1",
                api_key=api_key,
                model="gpt-4o-mini",
                provider_label="openai (legacy config)",
            )

    # ── No provider configured → NoneProvider ──
    if not provider_name or provider_name == "none":
        return NoneProvider()

    # ── Custom provider → user supplies everything ──
    if provider_name == "custom":
        base_url = str(ai_config.get("base_url", "")).strip()
        if not base_url:
            raise ValueError(
                "AI provider 'custom' requires 'base_url' in config.\n"
                "Example:\n"
                "  ai:\n"
                '    provider: "custom"\n'
                '    base_url: "https://my-llm.example.com/v1"\n'
                '    api_key: "my-key"\n'
                '    model: "my-model"'
            )
        return UniversalChatProvider(
            base_url=base_url,
            api_key=str(ai_config.get("api_key", "")).strip(),
            model=str(ai_config.get("model", "")).strip(),
            temperature=float(ai_config.get("temperature", 0.3)),
            provider_label="custom",
        )

    # ── Named preset ──
    preset = PROVIDER_PRESETS.get(provider_name)
    if not preset:
        available = ", ".join(sorted(PROVIDER_PRESETS.keys()))
        raise ValueError(
            f"Unknown AI provider: '{provider_name}'.\n"
            f"Available presets: {available}, 'custom', 'none'"
        )

    api_key = str(ai_config.get("api_key", "")).strip()
    model = str(ai_config.get("model", "")).strip() or preset["default_model"]

    # Warn for cloud providers missing API key (not local like Ollama)
    if preset.get("auth_header") and not api_key:
        logger.warning(
            "AI provider '%s' requires an API key but none was "
            "provided. Text cleanup will likely fail.",
            provider_name,
        )

    return UniversalChatProvider(
        base_url=preset["base_url"],
        api_key=api_key,
        model=model,
        temperature=float(ai_config.get("temperature", 0.3)),
        auth_header=preset.get("auth_header"),
        auth_prefix=preset.get("auth_prefix"),
        provider_label=provider_name,
    )
