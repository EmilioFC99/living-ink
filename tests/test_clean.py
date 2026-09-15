"""Tests for remarkable_mcp.clean module.

Covers the configure/repair integration, ENABLE_REPAIR toggle,
prompt file loading, lazy provider initialization, and backward
compatibility with legacy environment variables.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from remarkable_mcp import clean
from remarkable_mcp.providers import NoneProvider, UniversalChatProvider

# =========================================================================
# Fixtures
# =========================================================================


@pytest.fixture(autouse=True)
def reset_provider():
    """Reset the module-level provider before each test.

    This ensures tests don't leak provider state to each other.
    """
    clean._provider = None
    yield
    clean._provider = None


@pytest.fixture
def mock_prompt_file(tmp_path):
    """Create a temporary prompt file and patch PROMPT_FILE to use it.

    Args:
        tmp_path: Pytest temporary directory fixture.

    Yields:
        Path to the temporary prompt file.
    """
    prompt = tmp_path / "cleanup_prompt.txt"
    prompt.write_text("Test instructions: clean this text.")
    with patch.object(clean, "PROMPT_FILE", prompt):
        yield prompt


# =========================================================================
# configure()
# =========================================================================


class TestConfigure:
    """Tests for the configure() function."""

    def test_configure_with_gemini(self):
        """Configuring with Gemini creates a UniversalChatProvider."""
        clean.configure({"ai": {"provider": "gemini", "api_key": "test"}})
        provider = clean._get_provider()
        assert isinstance(provider, UniversalChatProvider)
        assert "generativelanguage" in provider.base_url

    def test_configure_with_none(self):
        """Configuring with 'none' creates a NoneProvider."""
        clean.configure({"ai": {"provider": "none"}})
        provider = clean._get_provider()
        assert isinstance(provider, NoneProvider)

    def test_configure_with_empty_config(self):
        """Configuring with empty dict creates NoneProvider."""
        clean.configure({})
        provider = clean._get_provider()
        assert isinstance(provider, NoneProvider)

    def test_configure_with_legacy_openai(self):
        """Legacy openai config is accepted for backward compatibility."""
        clean.configure({"openai": {"api_key": "sk-legacy"}})
        provider = clean._get_provider()
        assert isinstance(provider, UniversalChatProvider)
        assert "api.openai.com" in provider.base_url

    def test_configure_overrides_previous(self):
        """Calling configure() again replaces the previous provider."""
        clean.configure({"ai": {"provider": "gemini", "api_key": "k"}})
        p1 = clean._get_provider()
        assert "generativelanguage" in p1.base_url

        clean.configure({"ai": {"provider": "openai", "api_key": "k"}})
        p2 = clean._get_provider()
        assert "api.openai.com" in p2.base_url


# =========================================================================
# _get_provider() — lazy initialization
# =========================================================================


class TestGetProviderLazy:
    """Tests for lazy provider initialization via _get_provider()."""

    def test_returns_none_provider_when_unconfigured(self):
        """Without configure() or env vars, returns NoneProvider."""
        with patch.dict(os.environ, {}, clear=True):
            # Remove OPENAI_API_KEY if present
            os.environ.pop("OPENAI_API_KEY", None)
            provider = clean._get_provider()
            assert isinstance(provider, NoneProvider)

    def test_lazy_init_from_openai_env_var(self):
        """Falls back to OPENAI_API_KEY env var when not configured."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-from-env"}):
            provider = clean._get_provider()
            assert isinstance(provider, UniversalChatProvider)
            assert provider.api_key == "sk-from-env"

    def test_caches_provider_after_first_call(self):
        """Provider is cached after first lazy initialization."""
        clean.configure({"ai": {"provider": "none"}})
        p1 = clean._get_provider()
        p2 = clean._get_provider()
        assert p1 is p2


# =========================================================================
# repair_text_with_openai()
# =========================================================================


class TestRepairTextWithOpenai:
    """Tests for the main repair_text_with_openai() entry point."""

    def test_returns_original_when_none_provider(self):
        """With NoneProvider, original text is returned unchanged."""
        clean.configure({"ai": {"provider": "none"}})
        result = clean.repair_text_with_openai("hello world")
        assert result == "hello world"

    def test_returns_empty_for_empty_input(self):
        """Empty string input returns empty string."""
        clean.configure({"ai": {"provider": "gemini", "api_key": "k"}})
        result = clean.repair_text_with_openai("")
        assert result == ""

    def test_returns_whitespace_for_whitespace_input(self):
        """Whitespace-only input is returned unchanged."""
        clean.configure({"ai": {"provider": "gemini", "api_key": "k"}})
        result = clean.repair_text_with_openai("   \n  ")
        assert result == "   \n  "

    def test_delegates_to_provider(self, mock_prompt_file):
        """Text is forwarded to the configured provider's repair_text."""
        mock_provider = MagicMock(spec=UniversalChatProvider)
        mock_provider.repair_text.return_value = "cleaned"
        clean._provider = mock_provider

        result = clean.repair_text_with_openai("raw text")
        assert result == "cleaned"
        mock_provider.repair_text.assert_called_once()

    def test_passes_prompt_instructions(self, mock_prompt_file):
        """Prompt instructions from file are passed to the provider."""
        mock_provider = MagicMock(spec=UniversalChatProvider)
        mock_provider.repair_text.return_value = "ok"
        clean._provider = mock_provider

        clean.repair_text_with_openai("raw text")
        call_args = mock_provider.repair_text.call_args
        instructions = call_args[0][1]
        assert "Test instructions" in instructions


# =========================================================================
# ENABLE_REPAIR toggle
# =========================================================================


class TestEnableRepairToggle:
    """Tests for the ENABLE_REPAIR environment variable toggle."""

    def test_disabled_returns_original(self):
        """When ENABLE_REPAIR is false, text is returned unchanged."""
        original_flag = clean.ENABLE_REPAIR
        try:
            clean.ENABLE_REPAIR = False
            clean.configure({"ai": {"provider": "gemini", "api_key": "k"}})
            result = clean.repair_text_with_openai("test text")
            assert result == "test text"
        finally:
            clean.ENABLE_REPAIR = original_flag

    def test_enabled_processes_text(self):
        """When ENABLE_REPAIR is true, text is processed."""
        original_flag = clean.ENABLE_REPAIR
        try:
            clean.ENABLE_REPAIR = True
            mock_provider = MagicMock(spec=UniversalChatProvider)
            mock_provider.repair_text.return_value = "fixed"
            clean._provider = mock_provider

            result = clean.repair_text_with_openai("raw")
            assert result == "fixed"
        finally:
            clean.ENABLE_REPAIR = original_flag


# =========================================================================
# _read_prompt_instructions()
# =========================================================================


class TestReadPromptInstructions:
    """Tests for prompt file reading."""

    def test_reads_from_file(self, mock_prompt_file):
        """Reads instructions from the prompt file."""
        result = clean._read_prompt_instructions()
        assert "Test instructions" in result

    def test_fallback_when_file_missing(self, tmp_path):
        """Returns fallback text when prompt file doesn't exist."""
        missing = tmp_path / "nonexistent.txt"
        with patch.object(clean, "PROMPT_FILE", missing):
            result = clean._read_prompt_instructions()
            assert "Clean this OCR text" in result

    def test_strips_whitespace(self, tmp_path):
        """Result is stripped of leading/trailing whitespace."""
        prompt = tmp_path / "prompt.txt"
        prompt.write_text("  \n  instructions here  \n  ")
        with patch.object(clean, "PROMPT_FILE", prompt):
            result = clean._read_prompt_instructions()
            assert result == "instructions here"
