"""Tests for living_ink.providers module.

Covers the provider factory, all presets, custom endpoints,
backward compatibility, error handling, and the HTTP call mechanics
of ``UniversalChatProvider``.
"""

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from living_ink.providers import (
    MAX_ATTEMPTS,
    MAX_RETRY_DELAY,
    PROVIDER_PRESETS,
    PROVIDER_REGISTRY,
    RETRY_BASE_DELAY,
    NoneProvider,
    TextRepairProvider,
    UniversalChatProvider,
    get_provider,
    register_provider,
)
from living_ink.settings import Settings

# =========================================================================
# TextRepairProvider (ABC)
# =========================================================================


class TestTextRepairProviderABC:
    """Tests for the abstract base class contract."""

    def test_cannot_instantiate_directly(self):
        """TextRepairProvider cannot be instantiated without subclassing."""
        with pytest.raises(TypeError):
            TextRepairProvider()

    def test_subclass_must_implement_repair_text(self):
        """Subclass missing repair_text raises TypeError."""

        class Incomplete(TextRepairProvider):
            @property
            def name(self) -> str:
                return "incomplete"

        with pytest.raises(TypeError):
            Incomplete()

    def test_subclass_must_implement_name(self):
        """Subclass missing name property raises TypeError."""

        class Incomplete(TextRepairProvider):
            def repair_text(self, raw_text, instructions):
                return raw_text

        with pytest.raises(TypeError):
            Incomplete()

    def test_valid_subclass_works(self):
        """A complete subclass can be instantiated and used."""

        class Complete(TextRepairProvider):
            def repair_text(self, raw_text, instructions):
                return raw_text.upper()

            @property
            def name(self):
                return "test"

        p = Complete()
        assert p.name == "test"
        assert p.repair_text("hello", "") == "HELLO"

    def test_supports_vision_defaults_to_false(self):
        """TextRepairProvider subclasses default to supports_vision=False."""

        class Complete(TextRepairProvider):
            def repair_text(self, raw_text, instructions):
                return raw_text

            @property
            def name(self):
                return "test"

        p = Complete()
        assert p.supports_vision is False

    def test_ocr_image_raises_not_implemented(self):
        """TextRepairProvider default ocr_image raises NotImplementedError."""

        class Complete(TextRepairProvider):
            def repair_text(self, raw_text, instructions):
                return raw_text

            @property
            def name(self):
                return "test"

        p = Complete()
        with pytest.raises(NotImplementedError, match="does not support vision OCR"):
            p.ocr_image("/path/to/img.png", "instructions")


# =========================================================================
# NoneProvider
# =========================================================================


class TestNoneProvider:
    """Tests for the no-op NoneProvider."""

    def test_returns_original_text(self):
        """repair_text returns the input unchanged."""
        p = NoneProvider()
        assert p.repair_text("hello world", "instructions") == "hello world"

    def test_returns_empty_string(self):
        """repair_text handles empty strings."""
        p = NoneProvider()
        assert p.repair_text("", "instructions") == ""

    def test_returns_whitespace_only(self):
        """repair_text preserves whitespace-only strings."""
        p = NoneProvider()
        assert p.repair_text("   \n\t  ", "instructions") == "   \n\t  "

    def test_ignores_instructions(self):
        """The instructions parameter is not used."""
        p = NoneProvider()
        text = "some text"
        assert p.repair_text(text, "do something wild") == text

    def test_name(self):
        """Name includes 'no AI cleanup' for clarity in logs."""
        p = NoneProvider()
        assert "no AI cleanup" in p.name.lower() or "none" in p.name.lower()

    def test_supports_vision_is_false(self):
        """NoneProvider does not support vision."""
        p = NoneProvider()
        assert p.supports_vision is False

    def test_ocr_image_raises_not_implemented(self):
        """NoneProvider ocr_image raises NotImplementedError."""
        p = NoneProvider()
        with pytest.raises(NotImplementedError, match="does not support vision OCR"):
            p.ocr_image("/path/to/img.png", "instructions")


# =========================================================================
# UniversalChatProvider — construction and properties
# =========================================================================


class TestUniversalChatProviderInit:
    """Tests for UniversalChatProvider initialization."""

    def test_strips_trailing_slash_from_base_url(self):
        """Trailing slash on base_url is removed."""
        p = UniversalChatProvider(base_url="https://api.example.com/v1/")
        assert p.base_url == "https://api.example.com/v1"

    def test_default_values(self):
        """Verify default parameter values."""
        p = UniversalChatProvider(base_url="https://x.com/v1")
        assert p.api_key == ""
        assert p.model == ""
        assert p.temperature == 0.3
        assert p.auth_header == "Authorization"
        assert p.auth_prefix == "Bearer"

    def test_name_includes_label_and_model(self):
        """Name property includes both provider label and model."""
        p = UniversalChatProvider(
            base_url="https://x.com/v1",
            model="test-model",
            provider_label="mycloud",
        )
        assert p.name == "mycloud (test-model)"

    def test_build_url(self):
        """_build_url appends /chat/completions."""
        p = UniversalChatProvider(base_url="https://api.example.com/v1")
        assert p._build_url() == "https://api.example.com/v1/chat/completions"

    def test_build_url_no_double_slash(self):
        """No double slash even if base_url had trailing slash."""
        p = UniversalChatProvider(base_url="https://api.example.com/v1/")
        assert "//" not in p._build_url().replace("https://", "")


# =========================================================================
# UniversalChatProvider — repair_text behavior
# =========================================================================


class TestUniversalChatProviderRepairText:
    """Tests for UniversalChatProvider.repair_text."""

    def test_empty_string_returns_empty(self):
        """Empty input is returned without making an API call."""
        p = UniversalChatProvider(base_url="https://x.com/v1")
        with patch.object(p, "_chat") as mock_chat:
            result = p.repair_text("", "instructions")
            assert result == ""
            mock_chat.assert_not_called()

    def test_whitespace_only_returns_unchanged(self):
        """Whitespace-only input is returned without an API call."""
        p = UniversalChatProvider(base_url="https://x.com/v1")
        with patch.object(p, "_chat") as mock_chat:
            result = p.repair_text("   \n  ", "instructions")
            assert result == "   \n  "
            mock_chat.assert_not_called()

    def test_successful_repair(self):
        """Successful API call returns cleaned text."""
        p = UniversalChatProvider(base_url="https://x.com/v1")
        with patch.object(p, "_chat", return_value="cleaned text"):
            result = p.repair_text("messy text", "fix this")
            assert result == "cleaned text"

    def test_strips_result(self):
        """Result from API is stripped of whitespace."""
        p = UniversalChatProvider(base_url="https://x.com/v1")
        with patch.object(p, "_chat", return_value="  cleaned  \n "):
            result = p.repair_text("messy text", "fix this")
            assert result == "cleaned"

    def test_api_failure_returns_original(self):
        """If the API returns empty string, original text is returned."""
        p = UniversalChatProvider(base_url="https://x.com/v1")
        with patch.object(p, "_chat", return_value=""):
            result = p.repair_text("original text", "fix this")
            assert result == "original text"

    def test_prompt_format(self):
        """Verify the prompt sent to _chat includes instructions and text."""
        p = UniversalChatProvider(base_url="https://x.com/v1")
        with patch.object(p, "_chat", return_value="ok") as mock_chat:
            p.repair_text("raw text here", "Clean this up.")
            prompt = mock_chat.call_args[0][0]
            assert "Clean this up." in prompt
            assert "raw text here" in prompt
            assert "TEXT:" in prompt


# =========================================================================
# UniversalChatProvider — HTTP _chat method
# =========================================================================


@pytest.fixture(autouse=True)
def no_retry_sleep():
    """Keep the retry backoff from making the suite wait for real seconds."""
    with patch("living_ink.providers.time.sleep"):
        yield


class TestUniversalChatProviderChat:
    """Tests for the HTTP call mechanics of _chat."""

    def _make_response(self, content: str) -> bytes:
        """Build a mock OpenAI-compatible JSON response body.

        Args:
            content: The assistant message content.

        Returns:
            JSON bytes matching the OpenAI response format.
        """
        return json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")

    @patch("living_ink.providers.urllib.request.urlopen")
    def test_sends_correct_payload(self, mock_urlopen):
        """Verify the HTTP request payload structure."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = self._make_response("result")
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        p = UniversalChatProvider(
            base_url="https://api.test.com/v1",
            api_key="test-key",
            model="test-model",
            temperature=0.5,
        )
        p._chat("hello")

        # Extract the Request object passed to urlopen
        call_args = mock_urlopen.call_args
        request = call_args[0][0]

        # Verify URL
        assert request.full_url == "https://api.test.com/v1/chat/completions"

        # Verify payload
        payload = json.loads(request.data.decode("utf-8"))
        assert payload["model"] == "test-model"
        assert payload["temperature"] == 0.5
        assert len(payload["messages"]) == 2
        assert payload["messages"][0]["role"] == "system"
        assert payload["messages"][1]["role"] == "user"
        assert payload["messages"][1]["content"] == "hello"

    @patch("living_ink.providers.urllib.request.urlopen")
    def test_auth_header_bearer(self, mock_urlopen):
        """Bearer auth header is set correctly."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = self._make_response("ok")
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        p = UniversalChatProvider(
            base_url="https://api.test.com/v1",
            api_key="my-secret",
            auth_header="Authorization",
            auth_prefix="Bearer",
        )
        p._chat("test")

        request = mock_urlopen.call_args[0][0]
        assert request.get_header("Authorization") == "Bearer my-secret"

    @patch("living_ink.providers.urllib.request.urlopen")
    def test_no_auth_header_when_none(self, mock_urlopen):
        """No auth header is added when auth_header is None."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = self._make_response("ok")
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        p = UniversalChatProvider(
            base_url="http://localhost:11434/v1",
            auth_header=None,
            auth_prefix=None,
        )
        p._chat("test")

        request = mock_urlopen.call_args[0][0]
        assert request.get_header("Authorization") is None

    @patch("living_ink.providers.urllib.request.urlopen")
    def test_http_error_returns_empty(self, mock_urlopen):
        """HTTPError is caught and returns empty string."""
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://x.com",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=MagicMock(read=MagicMock(return_value=b"bad key")),
        )

        p = UniversalChatProvider(
            base_url="https://api.test.com/v1",
            api_key="bad-key",
        )
        result = p._chat("test")
        assert result == ""

    @patch("living_ink.providers.urllib.request.urlopen")
    def test_url_error_returns_empty(self, mock_urlopen):
        """URLError (connection failure) returns empty string."""
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        p = UniversalChatProvider(base_url="http://localhost:9999/v1")
        result = p._chat("test")
        assert result == ""

    @patch("living_ink.providers.urllib.request.urlopen")
    def test_unexpected_error_returns_empty(self, mock_urlopen):
        """Any unexpected exception returns empty string."""
        mock_urlopen.side_effect = RuntimeError("Something broke")

        p = UniversalChatProvider(base_url="https://api.test.com/v1")
        result = p._chat("test")
        assert result == ""

    @patch("living_ink.providers.urllib.request.urlopen")
    def test_chat_handles_empty_choices(self, mock_urlopen):
        """_chat returns empty string if choices array is empty."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"choices": []}).encode("utf-8")
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        p = UniversalChatProvider(base_url="https://api.test.com/v1")
        assert p._chat("test") == ""

    @patch("living_ink.providers.urllib.request.urlopen")
    def test_chat_handles_missing_content(self, mock_urlopen):
        """_chat returns empty string if message has no content key."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(
            {"choices": [{"message": {"role": "assistant"}}]}
        ).encode("utf-8")
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        p = UniversalChatProvider(base_url="https://api.test.com/v1")
        assert p._chat("test") == ""


# =========================================================================
# UniversalChatProvider — vision OCR capabilities
# =========================================================================


class TestUniversalChatProviderVision:
    """Tests for vision OCR via UniversalChatProvider."""

    def _make_response(self, content: str) -> bytes:
        """Helper to create a mock OpenAI-compatible JSON response."""
        return json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": content}}]}
        ).encode("utf-8")

    def test_supports_vision_is_true(self):
        """UniversalChatProvider reports supports_vision=True."""
        p = UniversalChatProvider(base_url="https://api.test.com/v1")
        assert p.supports_vision is True

    @patch("living_ink.providers.urllib.request.urlopen")
    def test_ocr_image_sends_multimodal_payload(self, mock_urlopen, tmp_path):
        """ocr_image sends correct multimodal payload with base64 data URI."""
        img_path = tmp_path / "page.png"
        img_path.write_bytes(b"fake-png-bytes")

        mock_resp = MagicMock()
        mock_resp.read.return_value = self._make_response("Transcribed notes")
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        p = UniversalChatProvider(
            base_url="https://api.test.com/v1",
            api_key="secret",
            model="gemini-2.0-flash",
        )
        result = p.ocr_image(str(img_path), "Transcribe this page.")

        assert result == "Transcribed notes"

        # Check request
        request = mock_urlopen.call_args[0][0]
        assert request.full_url == "https://api.test.com/v1/chat/completions"
        assert request.get_header("Authorization") == "Bearer secret"

        payload = json.loads(request.data.decode("utf-8"))
        assert payload["model"] == "gemini-2.0-flash"
        assert len(payload["messages"]) == 2
        assert payload["messages"][0]["role"] == "system"

        user_content = payload["messages"][1]["content"]
        assert isinstance(user_content, list)
        assert user_content[0]["type"] == "text"
        assert user_content[0]["text"] == "Transcribe this page."
        assert user_content[1]["type"] == "image_url"
        url = user_content[1]["image_url"]["url"]
        assert url.startswith("data:image/png;base64,")

    @pytest.mark.parametrize(
        "filename, expected_mime",
        [
            ("page.png", "image/png"),
            ("page.jpg", "image/jpeg"),
            ("page.jpeg", "image/jpeg"),
            ("page.webp", "image/webp"),
            ("page.unknown", "image/png"),
        ],
    )
    @patch("living_ink.providers.urllib.request.urlopen")
    def test_ocr_image_mime_types(self, mock_urlopen, tmp_path, filename, expected_mime):
        """ocr_image detects correct MIME type from extension."""
        img_path = tmp_path / filename
        img_path.write_bytes(b"image-data")

        mock_resp = MagicMock()
        mock_resp.read.return_value = self._make_response("ok")
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        p = UniversalChatProvider(base_url="https://api.test.com/v1")
        p.ocr_image(str(img_path), "prompt")

        request = mock_urlopen.call_args[0][0]
        payload = json.loads(request.data.decode("utf-8"))
        url = payload["messages"][1]["content"][1]["image_url"]["url"]
        assert url.startswith(f"data:{expected_mime};base64,")

    @patch("living_ink.providers.urllib.request.urlopen")
    def test_ocr_image_raw_auth_header(self, mock_urlopen, tmp_path):
        """ocr_image supports auth header without prefix."""
        img_path = tmp_path / "page.png"
        img_path.write_bytes(b"data")

        mock_resp = MagicMock()
        mock_resp.read.return_value = self._make_response("ok")
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        p = UniversalChatProvider(
            base_url="https://api.test.com/v1",
            api_key="raw-token",
            auth_header="X-API-Key",
            auth_prefix=None,
        )
        p.ocr_image(str(img_path), "prompt")

        request = mock_urlopen.call_args[0][0]
        assert request.get_header("X-api-key") == "raw-token"

    @patch("living_ink.providers.urllib.request.urlopen")
    def test_ocr_image_http_error_returns_empty(self, mock_urlopen, tmp_path):
        """ocr_image returns empty string on HTTPError."""
        img_path = tmp_path / "page.png"
        img_path.write_bytes(b"data")

        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://api.test.com/v1",
            code=400,
            msg="Bad Request",
            hdrs={},
            fp=MagicMock(read=MagicMock(return_value=b"invalid request")),
        )

        p = UniversalChatProvider(base_url="https://api.test.com/v1")
        result = p.ocr_image(str(img_path), "prompt")
        assert result == ""

    @patch("living_ink.providers.urllib.request.urlopen")
    def test_ocr_image_url_error_returns_empty(self, mock_urlopen, tmp_path):
        """ocr_image returns empty string on URLError."""
        img_path = tmp_path / "page.png"
        img_path.write_bytes(b"data")

        mock_urlopen.side_effect = urllib.error.URLError("Connection reset")

        p = UniversalChatProvider(base_url="https://api.test.com/v1")
        result = p.ocr_image(str(img_path), "prompt")
        assert result == ""

    @patch("living_ink.providers.urllib.request.urlopen")
    def test_ocr_image_unexpected_error_returns_empty(self, mock_urlopen, tmp_path):
        """ocr_image returns empty string on general Exception."""
        img_path = tmp_path / "page.png"
        img_path.write_bytes(b"data")

        mock_urlopen.side_effect = RuntimeError("File I/O failure")

        p = UniversalChatProvider(base_url="https://api.test.com/v1")
        result = p.ocr_image(str(img_path), "prompt")
        assert result == ""

    @patch("living_ink.providers.urllib.request.urlopen")
    def test_ocr_image_handles_empty_choices(self, mock_urlopen, tmp_path):
        """ocr_image returns empty string if choices array is empty."""
        img_path = tmp_path / "page.png"
        img_path.write_bytes(b"data")

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"choices": []}).encode("utf-8")
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        p = UniversalChatProvider(base_url="https://api.test.com/v1")
        assert p.ocr_image(str(img_path), "prompt") == ""

    @patch("living_ink.providers.urllib.request.urlopen")
    def test_ocr_image_handles_content_filter(self, mock_urlopen, tmp_path):
        """ocr_image returns empty string without error when blocked by content filter."""
        img_path = tmp_path / "page.png"
        img_path.write_bytes(b"data")

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(
            {
                "choices": [
                    {
                        "finish_reason": "content_filter: RECITATION",
                        "index": 0,
                        "message": {"role": "assistant"},
                    }
                ]
            }
        ).encode("utf-8")
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        p = UniversalChatProvider(base_url="https://api.test.com/v1")
        assert p.ocr_image(str(img_path), "prompt") == ""


# =========================================================================
# get_provider factory — preset providers
# =========================================================================


class TestGetProviderPresets:
    """Tests for the get_provider factory with named presets."""

    @pytest.mark.parametrize("preset_name", list(PROVIDER_PRESETS.keys()))
    def test_all_presets_return_universal_provider(self, preset_name):
        """Every named preset returns a UniversalChatProvider."""
        provider = get_provider(Settings(ai_provider=preset_name, ai_api_key="test-key"))
        assert isinstance(provider, UniversalChatProvider)

    @pytest.mark.parametrize("preset_name", list(PROVIDER_PRESETS.keys()))
    def test_all_presets_use_correct_base_url(self, preset_name):
        """Each preset maps to its expected base URL."""
        provider = get_provider(Settings(ai_provider=preset_name, ai_api_key="k"))
        expected_url = PROVIDER_PRESETS[preset_name]["base_url"]
        assert provider.base_url == expected_url

    @pytest.mark.parametrize("preset_name", list(PROVIDER_PRESETS.keys()))
    def test_all_presets_have_default_model(self, preset_name):
        """Each preset assigns a default model when none is specified."""
        provider = get_provider(Settings(ai_provider=preset_name, ai_api_key="k"))
        expected_model = PROVIDER_PRESETS[preset_name]["default_model"]
        assert provider.model == expected_model

    def test_gemini_preset(self):
        """Gemini preset has the correct endpoint and model."""
        p = get_provider(Settings(ai_provider="gemini", ai_api_key="AIza"))
        assert "generativelanguage" in p.base_url
        assert p.model == "gemini-flash-latest"
        assert p.api_key == "AIza"

    def test_openai_preset(self):
        """OpenAI preset has the correct endpoint and model."""
        p = get_provider(Settings(ai_provider="openai", ai_api_key="sk-x"))
        assert "api.openai.com" in p.base_url
        assert p.model == "gpt-4o-mini"

    def test_ollama_preset_no_auth(self):
        """Ollama preset uses localhost and no auth."""
        p = get_provider(Settings(ai_provider="ollama"))
        assert "localhost" in p.base_url
        assert p.auth_header is None

    def test_model_override(self):
        """User-specified model overrides the preset default."""
        p = get_provider(Settings(ai_provider="gemini", ai_api_key="k", ai_model="gemini-1.5-pro"))
        assert p.model == "gemini-1.5-pro"

    def test_temperature_override(self):
        """User-specified temperature overrides the default 0.3."""
        p = get_provider(Settings(ai_provider="openai", ai_api_key="k", ai_temperature=0.8))
        assert p.temperature == 0.8

    def test_case_insensitive_provider_name(self):
        """Provider name matching is case-insensitive."""
        p = get_provider(Settings(ai_provider="GEMINI", ai_api_key="k"))
        assert isinstance(p, UniversalChatProvider)
        assert "generativelanguage" in p.base_url

    def test_provider_name_stripped(self):
        """Leading/trailing whitespace in provider name is ignored."""
        p = get_provider(Settings(ai_provider="  openai  ", ai_api_key="k"))
        assert isinstance(p, UniversalChatProvider)
        assert "api.openai.com" in p.base_url


# =========================================================================
# get_provider factory — none provider
# =========================================================================


class TestGetProviderNone:
    """Tests for NoneProvider selection."""

    def test_explicit_none(self):
        """provider: 'none' returns NoneProvider."""
        p = get_provider(Settings(ai_provider="none"))
        assert isinstance(p, NoneProvider)

    def test_empty_provider_string(self):
        """Empty provider string returns NoneProvider."""
        p = get_provider(Settings(ai_provider=""))
        assert isinstance(p, NoneProvider)

    def test_no_provider_named(self):
        """Nothing configured returns NoneProvider."""
        p = get_provider(Settings())
        assert isinstance(p, NoneProvider)


# =========================================================================
# get_provider factory — custom provider
# =========================================================================


class TestGetProviderCustom:
    """Tests for custom endpoint configuration."""

    def test_custom_with_all_fields(self):
        """Custom provider with all fields creates correct provider."""
        p = get_provider(
            Settings(
                ai_provider="custom",
                ai_base_url="https://my-llm.com/v1",
                ai_api_key="my-key",
                ai_model="my-model",
                ai_temperature=0.7,
            )
        )
        assert isinstance(p, UniversalChatProvider)
        assert p.base_url == "https://my-llm.com/v1"
        assert p.api_key == "my-key"
        assert p.model == "my-model"
        assert p.temperature == 0.7

    def test_custom_missing_base_url_raises(self):
        """Custom provider without base_url raises ValueError."""
        with pytest.raises(ValueError, match="base_url"):
            get_provider(Settings(ai_provider="custom", ai_api_key="k"))

    def test_custom_empty_base_url_raises(self):
        """Custom provider with empty base_url raises ValueError."""
        with pytest.raises(ValueError, match="base_url"):
            get_provider(Settings(ai_provider="custom", ai_base_url="", ai_api_key="k"))


# =========================================================================
# get_provider factory — backward compatibility
# =========================================================================


class TestGetProviderLegacy:
    """Tests for backward compatibility with legacy openai config."""

    def test_legacy_openai_section(self):
        """A key with no provider named is the pre-'ai:' config, which was OpenAI."""
        p = get_provider(Settings(ai_api_key="sk-legacy-key"))
        assert isinstance(p, UniversalChatProvider)
        assert "api.openai.com" in p.base_url
        assert p.api_key == "sk-legacy-key"
        assert p.model == "gpt-4o-mini"

    def test_legacy_placeholder_ignored(self):
        """Legacy config with placeholder value returns NoneProvider."""
        p = get_provider(Settings(ai_api_key="YOUR-OPENAI-KEY-HERE"))
        assert isinstance(p, NoneProvider)

    def test_legacy_empty_key_ignored(self):
        """Legacy config with empty key returns NoneProvider."""
        p = get_provider(Settings(ai_api_key=""))
        assert isinstance(p, NoneProvider)

    def test_new_ai_section_takes_precedence(self):
        """A named provider is used even when the key came from an old key."""
        p = get_provider(Settings(ai_provider="gemini", ai_api_key="gemini-key"))
        assert isinstance(p, UniversalChatProvider)
        assert "generativelanguage" in p.base_url
        assert p.api_key == "gemini-key"


# =========================================================================
# get_provider factory — error cases
# =========================================================================


class TestGetProviderErrors:
    """Tests for error handling in get_provider."""

    def test_unknown_provider_raises_valueerror(self):
        """Unknown provider name raises ValueError with helpful message."""
        with pytest.raises(ValueError, match="Unknown AI provider"):
            get_provider(Settings(ai_provider="banana"))

    def test_unknown_provider_lists_available(self):
        """Error message for unknown provider lists available presets."""
        with pytest.raises(ValueError, match="gemini") as exc_info:
            get_provider(Settings(ai_provider="banana"))
        error_msg = str(exc_info.value)
        assert "openai" in error_msg
        assert "custom" in error_msg
        assert "none" in error_msg

    def test_missing_api_key_for_cloud_provider_warns(self):
        """Cloud provider without API key logs a warning (not an error)."""
        # Should not raise, but should warn
        p = get_provider(Settings(ai_provider="openai"))
        assert isinstance(p, UniversalChatProvider)
        assert p.api_key == ""


# =========================================================================
# PROVIDER_PRESETS structure validation
# =========================================================================


class TestProviderPresets:
    """Tests to validate the PROVIDER_PRESETS dictionary structure."""

    @pytest.mark.parametrize("preset_name", list(PROVIDER_PRESETS.keys()))
    def test_preset_has_required_keys(self, preset_name):
        """Every preset has base_url, default_model, auth_header, auth_prefix."""
        preset = PROVIDER_PRESETS[preset_name]
        assert "base_url" in preset
        assert "default_model" in preset
        assert "auth_header" in preset
        assert "auth_prefix" in preset

    @pytest.mark.parametrize("preset_name", list(PROVIDER_PRESETS.keys()))
    def test_preset_base_url_not_empty(self, preset_name):
        """Preset base URLs are non-empty strings."""
        assert PROVIDER_PRESETS[preset_name]["base_url"]

    @pytest.mark.parametrize("preset_name", list(PROVIDER_PRESETS.keys()))
    def test_preset_default_model_not_empty(self, preset_name):
        """Preset default models are non-empty strings."""
        assert PROVIDER_PRESETS[preset_name]["default_model"]

    def test_preset_count(self):
        """Sanity check: at least 5 presets exist."""
        assert len(PROVIDER_PRESETS) >= 5


# =========================================================================
# Provider registry
# =========================================================================


class TestProviderRegistry:
    """A provider that is not OpenAI-compatible can still be selected by name."""

    @pytest.fixture
    def clean_registry(self):
        """Restore the registry after a test registers something into it."""
        original = dict(PROVIDER_REGISTRY)
        yield PROVIDER_REGISTRY
        PROVIDER_REGISTRY.clear()
        PROVIDER_REGISTRY.update(original)

    @pytest.fixture
    def echo_provider(self, clean_registry):
        """Register a minimal provider under the name 'echo'."""

        @register_provider("Echo")
        class EchoProvider(TextRepairProvider):
            def __init__(self, suffix: str = ""):
                self.suffix = suffix

            @classmethod
            def from_config(cls, settings):
                return cls(suffix=settings.ai_model or "")

            def repair_text(self, raw_text: str, instructions: str) -> str:
                return raw_text + self.suffix

            @property
            def name(self) -> str:
                return "echo"

        return EchoProvider

    def test_registration_normalizes_the_name(self, echo_provider):
        assert PROVIDER_REGISTRY["echo"] is echo_provider

    def test_get_provider_builds_the_registered_class(self, echo_provider):
        provider = get_provider(Settings(ai_provider="echo", ai_model="!"))

        assert isinstance(provider, echo_provider)
        assert provider.repair_text("hi", "") == "hi!"

    def test_provider_name_is_case_insensitive(self, echo_provider):
        assert isinstance(get_provider(Settings(ai_provider="ECHO")), echo_provider)

    def test_registration_wins_over_a_preset_of_the_same_name(self, clean_registry):
        @register_provider("ollama")
        class Replacement(NoneProvider):
            @classmethod
            def from_config(cls, settings):
                return cls()

        assert isinstance(get_provider(Settings(ai_provider="ollama")), Replacement)

    def test_unknown_provider_error_lists_registered_names(self, echo_provider):
        with pytest.raises(ValueError, match="Unknown AI provider") as exc_info:
            get_provider(Settings(ai_provider="banana"))
        assert "echo" in str(exc_info.value)

    def test_presets_still_resolve_when_nothing_is_registered(self):
        provider = get_provider(Settings(ai_provider="gemini", ai_api_key="k"))
        assert isinstance(provider, UniversalChatProvider)


class TestRetries:
    """Transient API failures are retried; permanent ones are not."""

    def _ok_response(self) -> MagicMock:
        body = json.dumps({"choices": [{"message": {"content": "transcribed"}}]}).encode()
        resp = MagicMock()
        resp.read.return_value = body
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def _http_error(self, code: int, retry_after: str = None) -> urllib.error.HTTPError:
        headers = {"Retry-After": retry_after} if retry_after else {}
        return urllib.error.HTTPError(
            url="https://api.test.com", code=code, msg="nope", hdrs=headers, fp=None
        )

    def _provider(self) -> UniversalChatProvider:
        return UniversalChatProvider(
            base_url="https://api.test.com/v1", api_key="k", model="m", provider_label="test"
        )

    @patch("living_ink.providers.urllib.request.urlopen")
    def test_rate_limit_is_retried_until_it_succeeds(self, mock_urlopen):
        mock_urlopen.side_effect = [self._http_error(429), self._ok_response()]

        assert self._provider().repair_text("raw", "clean it") == "transcribed"
        assert mock_urlopen.call_count == 2

    @patch("living_ink.providers.urllib.request.urlopen")
    def test_server_error_is_retried(self, mock_urlopen):
        mock_urlopen.side_effect = [self._http_error(503), self._ok_response()]

        assert self._provider().repair_text("raw", "clean it") == "transcribed"

    @patch("living_ink.providers.urllib.request.urlopen")
    def test_connection_error_is_retried(self, mock_urlopen):
        mock_urlopen.side_effect = [urllib.error.URLError("down"), self._ok_response()]

        assert self._provider().repair_text("raw", "clean it") == "transcribed"

    @patch("living_ink.providers.urllib.request.urlopen")
    def test_bad_key_is_not_retried(self, mock_urlopen):
        """A 401 will fail identically every time; retrying only wastes time."""
        mock_urlopen.side_effect = self._http_error(401)

        assert self._provider().repair_text("raw", "clean it") == "raw"
        assert mock_urlopen.call_count == 1

    @patch("living_ink.providers.urllib.request.urlopen")
    def test_attempts_are_capped(self, mock_urlopen):
        mock_urlopen.side_effect = self._http_error(429)

        assert self._provider().repair_text("raw", "clean it") == "raw"
        assert mock_urlopen.call_count == MAX_ATTEMPTS

    @patch("living_ink.providers.urllib.request.urlopen")
    def test_a_recovered_page_is_not_lost(self, mock_urlopen, tmp_path):
        """Vision OCR goes through the same retry path, so a 429 is not a blank page."""
        image = tmp_path / "page-1.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n")
        mock_urlopen.side_effect = [self._http_error(429), self._ok_response()]

        assert self._provider().ocr_image(str(image), "transcribe") == "transcribed"


class TestRetryDelay:
    """The wait between attempts backs off, respects the server, and is capped."""

    def test_backoff_doubles(self):
        first = UniversalChatProvider._retry_delay(1)
        third = UniversalChatProvider._retry_delay(3)

        assert RETRY_BASE_DELAY <= first < RETRY_BASE_DELAY * 2
        assert third >= RETRY_BASE_DELAY * 4

    def test_retry_after_header_wins(self):
        assert UniversalChatProvider._retry_delay(1, retry_after="7") == 7.0

    def test_unparseable_retry_after_falls_back_to_backoff(self):
        delay = UniversalChatProvider._retry_delay(1, retry_after="Wed, 21 Oct 2026 07:28:00 GMT")
        assert delay >= RETRY_BASE_DELAY

    def test_delay_is_capped(self):
        assert UniversalChatProvider._retry_delay(20) == MAX_RETRY_DELAY
        assert UniversalChatProvider._retry_delay(1, retry_after="9999") == MAX_RETRY_DELAY


class TestProviderSecretRegistration:
    """A provider's API key is registered so it cannot leak through logs."""

    def test_the_api_key_is_registered_on_construction(self):
        from living_ink import redact as redact_mod
        from living_ink.providers import UniversalChatProvider

        redact_mod.clear_secrets()
        try:
            UniversalChatProvider(
                base_url="https://example.test/v1",
                api_key="AIzaSyExampleKeyForTesting1234",
                model="test-model",
            )
            assert "AIzaSyExampleKeyForTesting1234" in redact_mod.registered_secrets()
        finally:
            redact_mod.clear_secrets()

    def test_an_empty_key_is_not_registered(self):
        """Local backends such as Ollama pass no key; masking "" would be absurd."""
        from living_ink import redact as redact_mod
        from living_ink.providers import UniversalChatProvider

        redact_mod.clear_secrets()
        try:
            UniversalChatProvider(
                base_url="http://localhost:11434/v1",
                api_key="",
                model="llama3",
            )
            assert redact_mod.registered_secrets() == set()
        finally:
            redact_mod.clear_secrets()
