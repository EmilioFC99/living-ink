"""Tests for living_ink.clean module.

Covers the configure/repair integration, the cleanup toggle,
prompt file loading, lazy provider initialization, and backward
compatibility with legacy environment variables.
"""

import dataclasses
import os
from unittest.mock import MagicMock, patch

import pytest

from living_ink import clean
from living_ink.providers import NoneProvider, UniversalChatProvider
from living_ink.settings import Settings

# =========================================================================
# Fixtures
# =========================================================================


@pytest.fixture(autouse=True)
def reset_provider():
    """Reset the module-level provider and toggle before each test.

    This ensures tests don't leak provider state to each other.
    """
    clean._provider = None
    clean._repair_enabled = None
    clean._prompt_dir = None
    yield
    clean._provider = None
    clean._repair_enabled = None
    clean._prompt_dir = None


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


@pytest.fixture
def mock_ocr_prompt_file(tmp_path):
    """Create a temporary OCR prompt file and patch OCR_PROMPT_FILE.

    Args:
        tmp_path: Pytest temporary directory fixture.

    Yields:
        Path to the temporary OCR prompt file.
    """
    prompt = tmp_path / "ocr_prompt.txt"
    prompt.write_text("Test OCR instructions: transcribe this image.")
    with patch.object(clean, "OCR_PROMPT_FILE", prompt):
        yield prompt


# =========================================================================
# configure()
# =========================================================================


class TestConfigure:
    """Tests for the configure() function."""

    def test_configure_with_gemini(self):
        """Configuring with Gemini creates a UniversalChatProvider."""
        clean.configure(Settings(ai_provider="gemini", ai_api_key="test"))
        provider = clean._get_provider()
        assert isinstance(provider, UniversalChatProvider)
        assert "generativelanguage" in provider.base_url

    def test_configure_with_none(self):
        """Configuring with 'none' creates a NoneProvider."""
        clean.configure(Settings(ai_provider="none"))
        provider = clean._get_provider()
        assert isinstance(provider, NoneProvider)

    def test_configure_with_empty_config(self):
        """Configuring with nothing set creates NoneProvider."""
        clean.configure(Settings())
        provider = clean._get_provider()
        assert isinstance(provider, NoneProvider)

    def test_configure_with_legacy_openai(self):
        """A key with no provider named is accepted, as it was before 'ai:'."""
        clean.configure(Settings(ai_api_key="sk-legacy"))
        provider = clean._get_provider()
        assert isinstance(provider, UniversalChatProvider)
        assert "api.openai.com" in provider.base_url

    def test_configure_overrides_previous(self):
        """Calling configure() again replaces the previous provider."""
        clean.configure(Settings(ai_provider="gemini", ai_api_key="k"))
        p1 = clean._get_provider()
        assert "generativelanguage" in p1.base_url

        clean.configure(Settings(ai_provider="openai", ai_api_key="k"))
        p2 = clean._get_provider()
        assert "api.openai.com" in p2.base_url


# =========================================================================
# _get_provider() — lazy initialization
# =========================================================================


class TestGetProviderLazy:
    """Tests for lazy provider initialization via _get_provider()."""

    def test_returns_none_provider_when_unconfigured(self):
        """Without configure(), returns NoneProvider."""
        with patch.dict(os.environ, {}, clear=True):
            provider = clean._get_provider()
            assert isinstance(provider, NoneProvider)

    def test_a_stray_openai_key_in_the_environment_configures_nothing(self):
        """Guessing a provider from an env var sends handwriting nobody chose."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-from-env"}):
            assert isinstance(clean._get_provider(), NoneProvider)

    def test_caches_provider_after_first_call(self):
        """Provider is cached after first lazy initialization."""
        clean.configure(Settings(ai_provider="none"))
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
        clean.configure(Settings(ai_provider="none"))
        result = clean.repair_text_with_openai("hello world")
        assert result == "hello world"

    def test_returns_empty_for_empty_input(self):
        """Empty string input returns empty string."""
        clean.configure(Settings(ai_provider="gemini", ai_api_key="k"))
        result = clean.repair_text_with_openai("")
        assert result == ""

    def test_returns_whitespace_for_whitespace_input(self):
        """Whitespace-only input is returned unchanged."""
        clean.configure(Settings(ai_provider="gemini", ai_api_key="k"))
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
# The cleanup toggle
# =========================================================================


class TestEnableRepairToggle:
    """Tests for turning the AI pass off entirely."""

    def test_disabled_returns_original(self):
        """With cleanup off, text is returned unchanged."""
        clean.configure(Settings(ai_provider="gemini", ai_api_key="k", repair_enabled=False))

        assert clean.repair_text_with_openai("test text") == "test text"

    def test_enabled_processes_text(self):
        """With cleanup on, text reaches the provider."""
        mock_provider = MagicMock(spec=UniversalChatProvider)
        mock_provider.repair_text.return_value = "fixed"
        clean._provider = mock_provider
        clean._repair_enabled = True

        assert clean.repair_text_with_openai("raw") == "fixed"

    def test_the_environment_still_turns_it_off(self, monkeypatch):
        """The one place this setting has ever been written."""
        monkeypatch.setenv("ENABLE_REPAIR", "false")

        assert clean.repair_enabled() is False


# =========================================================================
# _read_prompt_instructions()
# =========================================================================


class TestReadPromptInstructions:
    """Tests for prompt file reading."""

    def test_reads_from_file(self, mock_prompt_file):
        """Reads instructions from the prompt file."""
        result = clean._read_prompt_instructions()
        assert "Test instructions" in result

    def test_a_missing_file_raises(self, tmp_path):
        """Substituting a terser prompt would poison the transcript cache."""
        missing = tmp_path / "nonexistent.txt"
        with patch.object(clean, "PROMPT_FILE", missing):
            with pytest.raises(OSError):
                clean._read_prompt_instructions()

    def test_strips_whitespace(self, tmp_path):
        """Result is stripped of leading/trailing whitespace."""
        prompt = tmp_path / "prompt.txt"
        prompt.write_text("  \n  instructions here  \n  ")
        with patch.object(clean, "PROMPT_FILE", prompt):
            result = clean._read_prompt_instructions()
            assert result == "instructions here"


# =========================================================================
# vision_ocr_available()
# =========================================================================


class TestVisionOcrAvailable:
    """Tests for vision_ocr_available()."""

    def test_returns_true_when_supported_and_enabled(self):
        """Returns True when provider supports vision and repair is enabled."""
        clean.configure(Settings(ai_provider="gemini", ai_api_key="k"))
        assert clean.vision_ocr_available() is True

    def test_returns_false_when_repair_disabled(self):
        """Returns False when cleanup is turned off."""
        clean.configure(Settings(ai_provider="gemini", ai_api_key="k", repair_enabled=False))

        assert clean.vision_ocr_available() is False

    def test_returns_false_for_none_provider(self):
        """Returns False when configured with NoneProvider."""
        clean.configure(Settings(ai_provider="none"))
        assert clean.vision_ocr_available() is False


# =========================================================================
# ocr_and_repair()
# =========================================================================


class TestOcrAndRepair:
    """Tests for ocr_and_repair()."""

    def test_returns_none_when_repair_disabled(self):
        """Returns None when cleanup is turned off."""
        clean.configure(Settings(ai_provider="gemini", ai_api_key="k", repair_enabled=False))

        assert clean.ocr_and_repair("/path/to/img.png") is None

    def test_returns_none_when_provider_lacks_vision(self):
        """Returns None when provider does not support vision."""
        clean.configure(Settings(ai_provider="none"))
        assert clean.ocr_and_repair("/path/to/img.png") is None

    def test_calls_ocr_image_when_supported(self, mock_ocr_prompt_file):
        """Calls provider.ocr_image with image path and instructions."""
        mock_provider = MagicMock(spec=UniversalChatProvider)
        mock_provider.supports_vision = True
        mock_provider.ocr_image.return_value = "Page content transcribed"
        clean._provider = mock_provider

        result = clean.ocr_and_repair("/path/to/page.png")
        assert result == "Page content transcribed"
        mock_provider.ocr_image.assert_called_once()
        assert mock_provider.ocr_image.call_args[0][0] == "/path/to/page.png"
        assert "Test OCR instructions" in mock_provider.ocr_image.call_args[0][1]

    def test_returns_none_when_ocr_image_empty(self, mock_ocr_prompt_file):
        """Returns None when provider returns an empty string."""
        mock_provider = MagicMock(spec=UniversalChatProvider)
        mock_provider.supports_vision = True
        mock_provider.ocr_image.return_value = ""
        clean._provider = mock_provider

        result = clean.ocr_and_repair("/path/to/page.png")
        assert result is None


# =========================================================================
# _read_ocr_instructions()
# =========================================================================


class TestReadOcrInstructions:
    """Tests for OCR prompt file reading."""

    def test_reads_from_file(self, mock_ocr_prompt_file):
        """Reads instructions from the OCR prompt file."""
        result = clean._read_ocr_instructions()
        assert "Test OCR instructions" in result

    def test_a_missing_file_raises(self, tmp_path):
        """A broken install must not quietly transcribe against a stub prompt."""
        missing = tmp_path / "nonexistent.txt"
        with patch.object(clean, "OCR_PROMPT_FILE", missing):
            with pytest.raises(OSError):
                clean._read_ocr_instructions()


class TestTranscriptionFingerprint:
    """The fingerprint is what makes a cached transcription safe to reuse."""

    settings = Settings(ai_provider="openai", ai_model="gpt-4o-mini")

    def test_it_is_stable_for_unchanged_behaviour(self, mock_prompt_file, mock_ocr_prompt_file):
        assert clean.transcription_fingerprint(self.settings) == clean.transcription_fingerprint(
            self.settings
        )

    def test_editing_the_ocr_prompt_changes_it(self, mock_prompt_file, mock_ocr_prompt_file):
        """An edited prompt is supposed to change the answer, so it must miss."""
        before = clean.transcription_fingerprint(self.settings)
        mock_ocr_prompt_file.write_text("Transcribe, but in French.")
        assert clean.transcription_fingerprint(self.settings) != before

    def test_editing_the_cleanup_prompt_changes_it(self, mock_prompt_file, mock_ocr_prompt_file):
        before = clean.transcription_fingerprint(self.settings)
        mock_prompt_file.write_text("Clean this text, and shout.")
        assert clean.transcription_fingerprint(self.settings) != before

    @pytest.mark.parametrize(
        "field, value",
        [
            ("ai_provider", "gemini"),
            ("ai_model", "gpt-4o"),
            ("ai_temperature", 0.9),
            ("ai_language", "fr"),
        ],
    )
    def test_changing_an_input_changes_it(
        self, field, value, mock_prompt_file, mock_ocr_prompt_file
    ):
        before = clean.transcription_fingerprint(self.settings)
        after = dataclasses.replace(self.settings, **{field: value})
        assert clean.transcription_fingerprint(after) != before

    def test_it_builds_no_provider(self, mock_prompt_file, mock_ocr_prompt_file, monkeypatch):
        """Change detection runs it before any page reaches OCR."""

        def explode(*args, **kwargs):
            raise AssertionError("transcription_fingerprint must not construct a provider")

        monkeypatch.setattr(clean, "get_provider", explode)
        monkeypatch.setattr(clean, "_provider", None)
        assert clean.transcription_fingerprint(self.settings)

    def test_it_is_short_enough_to_print(self, mock_prompt_file, mock_ocr_prompt_file):
        assert len(clean.transcription_fingerprint(self.settings)) == 16


# =========================================================================
# ai.prompt_dir — user-owned prompt overrides
# =========================================================================


class TestPromptDirOverride:
    """``ai.prompt_dir`` was declared in the schema and read by nothing.

    The two prompts ship inside the installed package, so editing one in place
    is an edit an upgrade deletes. Pointing ``ai.prompt_dir`` at a directory of
    your own is the way a customised prompt survives one — which only works if
    the run reads from there and the cache notices that it did.
    """

    def test_no_prompt_dir_reads_the_packaged_prompt(self, mock_ocr_prompt_file):
        assert clean._prompt_path(clean.OCR_PROMPT_FILE, None) == mock_ocr_prompt_file

    def test_a_prompt_dir_without_the_file_reads_the_packaged_prompt(
        self, tmp_path, mock_ocr_prompt_file
    ):
        """Not an error. A directory holding one prompt overrides one prompt."""
        empty = tmp_path / "mine"
        empty.mkdir()
        assert clean._prompt_path(clean.OCR_PROMPT_FILE, str(empty)) == mock_ocr_prompt_file

    def test_a_prompt_dir_holding_the_file_wins(self, tmp_path, mock_ocr_prompt_file):
        mine = tmp_path / "mine"
        mine.mkdir()
        override = mine / "ocr_prompt.txt"
        override.write_text("Read it my way.")

        assert clean._prompt_path(clean.OCR_PROMPT_FILE, str(mine)) == override
        assert clean._read_ocr_instructions(str(mine)) == "Read it my way."

    def test_the_override_is_per_file(self, tmp_path, mock_prompt_file, mock_ocr_prompt_file):
        """Overriding the OCR prompt must not silently blank the cleanup one."""
        mine = tmp_path / "mine"
        mine.mkdir()
        (mine / "ocr_prompt.txt").write_text("Read it my way.")

        assert clean._read_ocr_instructions(str(mine)) == "Read it my way."
        assert clean._read_prompt_instructions(str(mine)) == mock_prompt_file.read_text()

    def test_a_tilde_is_expanded(self, tmp_path, monkeypatch, mock_ocr_prompt_file):
        monkeypatch.setenv("HOME", str(tmp_path))
        mine = tmp_path / "prompts"
        mine.mkdir()
        (mine / "ocr_prompt.txt").write_text("Home sweet home.")

        assert clean._read_ocr_instructions("~/prompts") == "Home sweet home."

    def test_prompt_paths_reports_both_in_ocr_then_cleanup_order(
        self, tmp_path, mock_prompt_file, mock_ocr_prompt_file
    ):
        mine = tmp_path / "mine"
        mine.mkdir()
        (mine / "cleanup_prompt.txt").write_text("Tidy it my way.")

        ocr, cleanup = clean.prompt_paths(str(mine))
        assert ocr == mock_ocr_prompt_file
        assert cleanup == mine / "cleanup_prompt.txt"

    def test_configure_records_the_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr(clean, "get_provider", lambda settings: NoneProvider())
        settings = Settings(ai_provider="none", ai_prompt_dir=str(tmp_path))

        clean.configure(settings)

        assert clean._prompt_dir == str(tmp_path)

    def test_an_overridden_prompt_misses_the_cache(
        self, tmp_path, mock_prompt_file, mock_ocr_prompt_file
    ):
        """The whole point: a different prompt must re-read every page."""
        settings = Settings(ai_provider="gemini", ai_model="m")
        before = clean.transcription_fingerprint(settings)

        mine = tmp_path / "mine"
        mine.mkdir()
        (mine / "ocr_prompt.txt").write_text("Read it my way.")
        after = dataclasses.replace(settings, ai_prompt_dir=str(mine))

        assert clean.transcription_fingerprint(after) != before

    def test_an_empty_prompt_dir_keeps_the_cache(
        self, tmp_path, mock_prompt_file, mock_ocr_prompt_file
    ):
        """Naming a directory that overrides nothing changes nothing."""
        settings = Settings(ai_provider="gemini", ai_model="m")
        empty = tmp_path / "empty"
        empty.mkdir()

        assert clean.transcription_fingerprint(
            dataclasses.replace(settings, ai_prompt_dir=str(empty))
        ) == clean.transcription_fingerprint(settings)
