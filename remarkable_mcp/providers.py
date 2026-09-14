"""
AI text repair providers for OCR cleanup.

Architecture:
- UniversalChatProvider: Works with ANY OpenAI-compatible API endpoint.
- NoneProvider: No-op, returns raw text unchanged.
- get_provider(config): Factory that reads config and returns the right provider.

No provider-specific code. No external SDKs required.
Just standard HTTP to a configurable endpoint.

Supported providers (via presets):
    openai, gemini, ollama, groq, openrouter, mistral, together
    + any custom OpenAI-compatible endpoint via provider: "custom"

Usage:
    from remarkable_mcp.providers import get_provider

    provider = get_provider(yaml_config)
    cleaned = provider.repair_text("messy OCR text", "instructions...")
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
    """Base class for AI text cleanup providers."""

    @abc.abstractmethod
    def repair_text(self, raw_text: str, instructions: str) -> str:
        """
        Clean up OCR text using AI.

        Args:
            raw_text: The raw OCR output to clean.
            instructions: Prompt instructions for the AI (from cleanup_prompt.txt).

        Returns:
            Cleaned text, or the original raw_text if cleanup fails or is disabled.
        """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Human-readable provider name for logging."""


# ---------------------------------------------------------------------------
# NoneProvider — no AI cleanup
# ---------------------------------------------------------------------------


class NoneProvider(TextRepairProvider):
    """Returns raw OCR text unchanged. No external API calls made."""

    def repair_text(self, raw_text: str, instructions: str) -> str:
        return raw_text

    @property
    def name(self) -> str:
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
    """
    Works with ANY OpenAI-compatible chat completions API.

    Tested with: OpenAI, Google Gemini, Ollama, Groq, OpenRouter,
    Mistral, Together, LM Studio, vLLM, and any future provider
    that supports the standard chat completions format.
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
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.auth_header = auth_header
        self.auth_prefix = auth_prefix
        self._provider_label = provider_label

    @property
    def name(self) -> str:
        return f"{self._provider_label} ({self.model})"

    def _build_url(self) -> str:
        """Build the chat completions endpoint URL."""
        return f"{self.base_url}/chat/completions"

    def _chat(self, prompt: str) -> str:
        """
        Send a chat completion request to the configured endpoint.

        Uses stdlib urllib — no external HTTP libraries required.
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

        # Add auth header if configured (some local providers like Ollama don't need it)
        if self.auth_header and self.api_key:
            if self.auth_prefix:
                req.add_header(self.auth_header, f"{self.auth_prefix} {self.api_key}")
            else:
                req.add_header(self.auth_header, self.api_key)

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = resp.read().decode("utf-8")
                j = json.loads(body)
                return j["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            logger.error(f"AI API HTTP Error ({self.name}): {e.code} {e.reason}")
            try:
                err_body = e.read().decode("utf-8")
                logger.error(f"Details: {err_body}")
            except Exception:
                pass
            return ""
        except urllib.error.URLError as e:
            logger.error(f"AI API Connection Error ({self.name}): {e.reason}")
            return ""
        except Exception as e:
            logger.error(f"AI API Unexpected Error ({self.name}): {e}")
            return ""

    def repair_text(self, raw_text: str, instructions: str) -> str:
        if not raw_text or not raw_text.strip():
            return raw_text

        prompt = f"{instructions}\n\nTEXT:\n{raw_text}"
        result = self._chat(prompt)

        if not result:
            logger.warning(f"AI cleanup returned empty result ({self.name}). Using raw text.")
            return raw_text

        return result.strip()


# ---------------------------------------------------------------------------
# Factory — builds the right provider from config
# ---------------------------------------------------------------------------


def get_provider(config: dict) -> TextRepairProvider:
    """
    Factory: reads the YAML config dict and returns the appropriate provider.

    Supports:
        - ai.provider: "openai" | "gemini" | "ollama" | ... (named presets)
        - ai.provider: "custom" (user supplies base_url)
        - ai.provider: "none" (no AI cleanup)
        - Legacy: openai.api_key (backward compatible, maps to provider: "openai")

    Args:
        config: The parsed YAML config dictionary.

    Returns:
        A configured TextRepairProvider instance.
    """
    ai_config = config.get("ai", {})
    provider_name = str(ai_config.get("provider", "")).strip().lower()

    # ── Backward compatibility: legacy 'openai' section without 'ai' section ──
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

    # Validate API key for cloud providers (not needed for local like Ollama)
    if preset.get("auth_header") and not api_key:
        logger.warning(
            f"AI provider '{provider_name}' requires an API key but none was provided. "
            "Text cleanup will likely fail."
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
