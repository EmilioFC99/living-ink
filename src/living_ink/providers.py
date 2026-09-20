"""AI text repair and vision OCR providers.

Architecture:
    - ``UniversalChatProvider``: Works with ANY OpenAI-compatible API endpoint.
      Supports both text-only cleanup and multimodal vision OCR (reads
      handwritten images directly via the standard ``image_url`` format).
    - ``NoneProvider``: No-op, returns raw text unchanged.
    - ``get_provider``: Factory that reads the settings and returns the right
      provider.

No provider-specific code. No external SDKs required.
Just standard HTTP to a configurable endpoint.

Supported providers (via presets):
    openai, gemini, ollama, groq, openrouter, mistral, together
    + any custom OpenAI-compatible endpoint via ``provider: "custom"``

Example:
    >>> from living_ink.providers import get_provider
    >>> from living_ink.settings import Settings
    >>> provider = get_provider(Settings(ai_provider="gemini", ai_api_key="..."))
    >>> cleaned = provider.repair_text("messy OCR text", "Clean this text.")
    >>> text = provider.ocr_image("/path/to/page.png", "Transcribe this page.")
"""

import abc
import base64
import json
import logging
import random
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Optional, Type

from living_ink.redact import redact, register_secret
from living_ink.settings import Settings

logger = logging.getLogger(__name__)

# Transient failures are retried with exponential backoff. Pages are
# transcribed several at a time, so tripping a per-minute quota is an ordinary
# event, not an exceptional one — dropping the page instead would leave a
# silent hole in the note.
MAX_ATTEMPTS = 4
RETRY_BASE_DELAY = 1.0
MAX_RETRY_DELAY = 30.0
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

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
# Provider registry — for backends that are not OpenAI-compatible and so
# cannot be expressed as a preset above.
# ---------------------------------------------------------------------------

PROVIDER_REGISTRY: Dict[str, Type["TextRepairProvider"]] = {}


def register_provider(name: str):
    """Register a provider class under the name used in ``ai.provider``.

    Presets cover any backend that speaks the OpenAI chat API; this covers the
    ones that do not, without :func:`get_provider` having to grow a branch for
    each. A registered name takes precedence over a preset of the same name.

    Args:
        name: The value users write for ``ai.provider`` in config.yml.

    Returns:
        The class decorator.
    """

    def decorator(cls: Type["TextRepairProvider"]) -> Type["TextRepairProvider"]:
        PROVIDER_REGISTRY[name.strip().lower()] = cls
        return cls

    return decorator


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------


#: A 64×64 blank PNG, base64-encoded — the smallest thing that is
#: unambiguously an image to every endpoint. The probe asks what the *request*
#: is worth, not what the model can read, so there is nothing on it to read.
PROBE_IMAGE_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAAAAACPAi4CAAAAK0lEQVR42u3MMREAAAgE"
    "oNf+nTWEmwcBqMlNRyAQCAQCgUAgEAgEAsHzYAFQLQF/OJ8+2wAAAABJRU5ErkJggg=="
)

#: What the probe asks for. Deliberately not a transcription instruction: a
#: blank page transcribes to nothing, and an empty reply is how a *failed*
#: verification is reported.
PROBE_INSTRUCTIONS = "Reply with exactly: READY"


class TextRepairProvider(abc.ABC):
    """Abstract base class for AI text cleanup providers.

    All providers must implement ``repair_text()`` and the ``name`` property.
    Subclass this to add new provider types beyond the built-in
    ``UniversalChatProvider`` and ``NoneProvider``.

    A subclass reachable from configuration also implements
    :meth:`from_config` and is decorated with :func:`register_provider`.
    """

    @classmethod
    def from_config(cls, settings: "Settings") -> "TextRepairProvider":
        """Build this provider from the resolved settings.

        Args:
            settings: The run's settings. The ``ai_*`` fields carry everything
                a provider is configured with, already merged from the flags,
                the environment, the config file and the credentials store.

        Returns:
            A configured provider.
        """
        raise NotImplementedError

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

    def probe_vision(self) -> str:
        """Send one real image and return whatever comes back.

        Verification has to ask the question a sync will ask. Every page is
        read by :meth:`ocr_image` and there is no second OCR backend to fall
        back to, so a text-only model that answers a text prompt perfectly is
        still a provider that fails on the first page of the first notebook —
        one wasted call per page, discovered after the run. This goes through
        ``ocr_image`` rather than building its own request for the same
        reason: a probe that exercises a different code path can pass while
        the path that matters is broken.

        Returns:
            The provider's reply, empty if the request failed.

        Raises:
            NotImplementedError: If the provider has no vision support at all.
        """
        with tempfile.TemporaryDirectory(prefix="living-ink-probe-") as tmp:
            image = Path(tmp) / "probe.png"
            image.write_bytes(base64.b64decode(PROBE_IMAGE_B64))
            return self.ocr_image(str(image), PROBE_INSTRUCTIONS)


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
        register_secret(api_key)
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

        body = self._send_with_retries(req, purpose)
        if body is None:
            return ""

        choices: list = body.get("choices", [])
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

    def _send_with_retries(self, req: urllib.request.Request, purpose: str) -> Optional[dict]:
        """Send the request, retrying the failures that are worth retrying.

        Rate limits and 5xx responses are temporary by definition, and pages
        are transcribed several at a time, so a burst that trips a per-minute
        quota is expected rather than exceptional. Giving up on the first 429
        would silently drop a page from the note; backing off recovers it.
        A 4xx that is not a rate limit is a bad request or a bad key — retrying
        it just wastes the user's time.

        Args:
            req: The prepared request. Reused across attempts.
            purpose: Human-readable label used to prefix log lines.

        Returns:
            The decoded JSON body, or None if every attempt failed.
        """
        for attempt in range(1, MAX_ATTEMPTS + 1):
            retry_after: Optional[str] = None
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                logger.error("%s HTTP Error (%s): %s %s", purpose, self.name, e.code, e.reason)
                try:
                    # Several providers echo the failing request back in the
                    # body of a 400, headers included, so this is the most
                    # likely place for the API key to reach a log file.
                    logger.error("Details: %s", redact(e.read().decode("utf-8")))
                except (OSError, UnicodeDecodeError):
                    # No body, or not text. The status line above is already
                    # the useful half of the report.
                    pass
                retry_after = e.headers.get("Retry-After") if e.headers else None
                retryable = e.code in RETRYABLE_STATUS
            except urllib.error.URLError as e:
                logger.error("%s Connection Error (%s): %s", purpose, self.name, e.reason)
                retryable = True
            except Exception as e:
                # Broad because this is the outer edge of a network call into
                # nine different providers; retrying an error we cannot
                # classify would be guessing, so it is reported and dropped.
                logger.error("%s Unexpected Error (%s): %s", purpose, self.name, e, exc_info=True)
                retryable = False

            if not retryable or attempt == MAX_ATTEMPTS:
                return None

            delay = self._retry_delay(attempt, retry_after)
            logger.warning(
                "%s retrying in %.1fs (attempt %d of %d)", purpose, delay, attempt + 1, MAX_ATTEMPTS
            )
            time.sleep(delay)

        return None

    @staticmethod
    def _retry_delay(attempt: int, retry_after: Optional[str] = None) -> float:
        """Return how long to wait before the next attempt.

        The server's ``Retry-After`` wins when it sends one. Otherwise the wait
        doubles per attempt, with jitter so that concurrent page requests that
        were rate-limited together do not all come back at the same instant.

        Args:
            attempt: The attempt that just failed, counting from 1.
            retry_after: The response's ``Retry-After`` header, if any.

        Returns:
            Seconds to sleep, capped at ``MAX_RETRY_DELAY``.
        """
        if retry_after:
            try:
                return min(float(retry_after), MAX_RETRY_DELAY)
            except (TypeError, ValueError):
                pass

        backoff = RETRY_BASE_DELAY * (2 ** (attempt - 1))
        return min(backoff + random.uniform(0, RETRY_BASE_DELAY), MAX_RETRY_DELAY)

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


def get_provider(settings: Settings) -> TextRepairProvider:
    """Create a text repair provider from the run's resolved settings.

    Factory function that reads the ``ai_*`` settings and returns the
    appropriate provider instance. Resolution order: a class in
    :data:`PROVIDER_REGISTRY`, then a name in :data:`PROVIDER_PRESETS`, then
    ``custom`` endpoints.

    It takes :class:`~living_ink.settings.Settings` rather than the parsed
    YAML because the key is no longer in the YAML: it is in the credentials
    directory, under a name composed from the provider. Reading the file here
    would see a section with no key in it.

    Args:
        settings: The run's settings.

    Returns:
        A configured ``TextRepairProvider`` instance.

    Raises:
        ValueError: If the provider name is unknown or if ``custom``
            provider is missing ``base_url``.

    Examples:
        >>> provider = get_provider(Settings(ai_provider="none"))
        >>> isinstance(provider, NoneProvider)
        True

        >>> provider = get_provider(Settings(ai_provider="gemini", ai_api_key="k"))
        >>> provider.model
        'gemini-flash-latest'
    """
    provider_name = str(settings.ai_provider or "").strip().lower()
    api_key = str(settings.ai_api_key or "").strip()
    model = str(settings.ai_model or "").strip()

    # ── A key with no provider named ──
    # Three configurations arrive here and only one of them is historical: a
    # pre-0.2 ``openai:`` block (deprecated, so ``apply_status`` still copies
    # it forward), an ``ai.api_key`` written into the file by hand, and
    # ``LIVING_INK_AI_API_KEY`` exported with nothing else set — the last two
    # being what a user does today when they have a key and have not read the
    # schema. Refusing them would mean answering "you gave me a key and no
    # provider" with silent raw OCR. A key with nothing to use it on meant
    # OpenAI in 0.1 and still does. "YOUR..." is the sample config's
    # placeholder, which configures nothing.
    if not provider_name and api_key and "YOUR" not in api_key:
        logger.info("No 'ai.provider' set; using the API key with OpenAI.")
        provider_name = "openai"

    # ── No provider configured → NoneProvider ──
    if not provider_name or provider_name == "none":
        return NoneProvider()

    # ── Custom provider → user supplies everything ──
    if provider_name == "custom":
        base_url = str(settings.ai_base_url or "").strip()
        if not base_url:
            raise ValueError(
                "AI provider 'custom' requires 'base_url' in config.\n"
                "Example:\n"
                "  ai:\n"
                '    provider: "custom"\n'
                '    base_url: "https://my-llm.example.com/v1"\n'
                '    model: "my-model"'
            )
        return UniversalChatProvider(
            base_url=base_url,
            api_key=api_key,
            model=model,
            temperature=settings.ai_temperature,
            provider_label="custom",
        )

    # ── Registered provider class ──
    registered = PROVIDER_REGISTRY.get(provider_name)
    if registered:
        return registered.from_config(settings)

    # ── Named preset ──
    preset = PROVIDER_PRESETS.get(provider_name)
    if not preset:
        available = ", ".join(sorted({*PROVIDER_PRESETS, *PROVIDER_REGISTRY}))
        raise ValueError(
            f"Unknown AI provider: '{provider_name}'.\n"
            f"Available providers: {available}, 'custom', 'none'"
        )

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
        model=model or preset["default_model"],
        temperature=settings.ai_temperature,
        auth_header=preset.get("auth_header"),
        auth_prefix=preset.get("auth_prefix"),
        provider_label=provider_name,
    )
