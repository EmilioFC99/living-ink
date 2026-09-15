"""AI text repair providers for OCR cleanup.

Architecture:
    - ``UniversalChatProvider``: Works with ANY OpenAI-compatible API endpoint.
    - ``NoneProvider``: No-op, returns raw text unchanged.
    - ``get_provider``: Factory that reads config and returns the right provider.

No provider-specific code. No external SDKs required.
Just standard HTTP to a configurable endpoint.

Supported providers (via presets):
    openai, gemini, ollama, groq, openrouter, mistral, together
    + any custom OpenAI-compatible endpoint via ``provider: "custom"``

Example:
    >>> from remarkable_mcp.providers import get_provider
    >>> provider = get_provider({"ai": {"provider": "gemini", "api_key": "..."}})
    >>> cleaned = provider.repair_text("messy OCR text", "Clean this text.")
"""

import abc
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
        "default_model": "gemini-2.0-flash",
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

    def _chat(self, prompt: str) -> str:
        """Send a chat completion request to the configured endpoint.

        Constructs an OpenAI-compatible JSON payload with system and user
        messages, sends it via HTTP POST, and extracts the response content.

        Uses stdlib ``urllib`` — no external HTTP libraries required.

        Args:
            prompt: The user message content to send.

        Returns:
            The assistant's response content string, or empty string
            on any error.
        """
        url = self._build_url()
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_MESSAGE},
                {"role": "user", "content": prompt},
            ],
            "temperature": self.temperature,
        }

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")

        # Add auth header if configured (some local providers
        # like Ollama don't need it)
        if self.auth_header and self.api_key:
            if self.auth_prefix:
                req.add_header(
                    self.auth_header,
                    f"{self.auth_prefix} {self.api_key}",
                )
            else:
                req.add_header(self.auth_header, self.api_key)

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = resp.read().decode("utf-8")
                j = json.loads(body)
                return j["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            logger.error(
                "AI API HTTP Error (%s): %s %s",
                self.name,
                e.code,
                e.reason,
            )
            try:
                err_body = e.read().decode("utf-8")
                logger.error("Details: %s", err_body)
            except Exception:
                pass
            return ""
        except urllib.error.URLError as e:
            logger.error(
                "AI API Connection Error (%s): %s",
                self.name,
                e.reason,
            )
            return ""
        except Exception as e:
            logger.error(
                "AI API Unexpected Error (%s): %s",
                self.name,
                e,
            )
            return ""

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
                        "model": "gemini-2.0-flash",  # optional
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
        'gemini-2.0-flash'
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
