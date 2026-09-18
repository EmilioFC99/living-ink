"""Tests for the typed Settings object and its precedence rules."""

from dataclasses import FrozenInstanceError

import pytest

from living_ink.settings import (
    DEFAULT_APPLE_NOTES_FOLDER,
    DEFAULT_CACHE_MAX_AGE_DAYS,
    DEFAULT_OCR_CONCURRENCY,
    DEFAULT_SSH_HOST,
    FIELD_ENV_VARS,
    SOURCE_CONFIG,
    SOURCE_DEFAULT,
    SOURCE_ENV,
    Settings,
    as_bool,
    as_int,
    as_str,
)


class TestCoercion:
    """The parsers accept the shapes YAML and the environment actually produce."""

    def test_as_bool_accepts_yaml_booleans(self):
        assert as_bool(True) is True
        assert as_bool(False) is False

    def test_as_bool_accepts_strings(self):
        for truthy in ("true", "TRUE", " yes ", "1", "on"):
            assert as_bool(truthy) is True
        for falsy in ("false", "no", "0", ""):
            assert as_bool(falsy) is False

    def test_as_bool_uses_default_for_none(self):
        assert as_bool(None, default=True) is True
        assert as_bool(None, default=False) is False

    def test_as_int_falls_back_on_garbage(self):
        assert as_int("7", 1) == 7
        assert as_int(" 7 ", 1) == 7
        assert as_int("seven", 1) == 1
        assert as_int(None, 1) == 1

    def test_as_str_treats_blank_as_absent(self):
        assert as_str("  hi  ") == "hi"
        assert as_str("   ", "fallback") == "fallback"
        assert as_str(None, "fallback") == "fallback"


class TestDefaults:
    """An empty config and an empty environment still produce a usable run."""

    def test_all_defaults(self):
        s = Settings.resolve(config={}, env={})
        assert s.preferred_connection == "ssh"
        assert s.use_ssh is True
        assert s.ssh_host == DEFAULT_SSH_HOST
        assert s.ssh_port == 22
        assert s.sync_pdfs is False
        assert s.sync_epubs is False
        assert s.max_notebooks_per_run == 1
        assert s.apple_notes_folder == DEFAULT_APPLE_NOTES_FOLDER
        assert s.remarkable_token is None

    def test_settings_are_frozen(self):
        s = Settings.resolve(config={}, env={})
        with pytest.raises(FrozenInstanceError):
            s.use_ssh = False


class TestConfigValues:
    """YAML values are read from the sections the config file actually uses."""

    def test_reads_every_section(self):
        config = {
            "remarkable": {
                "device_token": " tok ",
                "preferred_connection": "Cloud",
                "ssh_host": "192.168.1.5",
                "ssh_user": "rm",
                "ssh_port": "2222",
            },
            "sync": {"sync_pdfs": True, "sync_epubs": "yes", "max_notebooks_per_run": 5},
            "apple_notes": {"folder_name": "Notebooks"},
        }
        s = Settings.resolve(config=config, env={})

        assert s.remarkable_token == "tok"
        assert s.preferred_connection == "cloud"
        assert s.ssh_host == "192.168.1.5"
        assert s.ssh_user == "rm"
        assert s.ssh_port == 2222
        assert s.sync_pdfs is True
        assert s.sync_epubs is True
        assert s.max_notebooks_per_run == 5
        assert s.apple_notes_folder == "Notebooks"

    def test_cloud_preference_disables_ssh_unless_stated(self):
        s = Settings.resolve(config={"remarkable": {"preferred_connection": "cloud"}}, env={})
        assert s.use_ssh is False

    def test_explicit_use_ssh_wins_over_preference(self):
        config = {"remarkable": {"preferred_connection": "cloud", "use_ssh": True}}
        assert Settings.resolve(config=config, env={}).use_ssh is True

    def test_legacy_top_level_use_ssh(self):
        assert Settings.resolve(config={"use_ssh": False}, env={}).use_ssh is False

    def test_none_sections_are_tolerated(self):
        s = Settings.resolve(config={"remarkable": None, "sync": None}, env={})
        assert s.ssh_host == DEFAULT_SSH_HOST


class TestPrecedence:
    """Environment beats config; config beats defaults."""

    def test_env_overrides_config(self):
        config = {
            "remarkable": {"device_token": "from-config", "ssh_host": "config-host"},
            "sync": {"max_notebooks_per_run": 5, "sync_pdfs": False},
            "apple_notes": {"folder_name": "ConfigFolder"},
        }
        env = {
            "REMARKABLE_TOKEN": "from-env",
            "REMARKABLE_SSH_HOST": "env-host",
            "SYNC_MAX_NOTEBOOKS": "9",
            "SYNC_PDFS": "true",
            "APPLE_NOTES_FOLDER": "EnvFolder",
        }
        s = Settings.resolve(config=config, env=env)

        assert s.remarkable_token == "from-env"
        assert s.ssh_host == "env-host"
        assert s.max_notebooks_per_run == 9
        assert s.sync_pdfs is True
        assert s.apple_notes_folder == "EnvFolder"

    def test_blank_env_does_not_override_config(self):
        config = {"remarkable": {"ssh_host": "config-host"}}
        s = Settings.resolve(config=config, env={"REMARKABLE_SSH_HOST": ""})
        assert s.ssh_host == "config-host"

    def test_config_overrides_defaults(self):
        s = Settings.resolve(config={"sync": {"max_notebooks_per_run": 3}}, env={})
        assert s.max_notebooks_per_run == 3

    def test_from_env_ignores_config_entirely(self):
        s = Settings.from_env(env={"REMARKABLE_USE_SSH": "false"})
        assert s.use_ssh is False

    def test_from_env_defaults_to_os_environ(self, monkeypatch):
        monkeypatch.setenv("APPLE_NOTES_FOLDER", "FromProcess")
        assert Settings.from_env().apple_notes_folder == "FromProcess"


class TestOcrConcurrency:
    """How many pages are transcribed at once is configurable, and never zero."""

    def test_defaults_to_a_handful(self):
        assert Settings.resolve(config={}, env={}).ocr_concurrency == DEFAULT_OCR_CONCURRENCY

    def test_config_sets_it(self):
        s = Settings.resolve(config={"sync": {"ocr_concurrency": 8}}, env={})
        assert s.ocr_concurrency == 8

    def test_env_outranks_config(self):
        s = Settings.resolve(
            config={"sync": {"ocr_concurrency": 8}}, env={"SYNC_OCR_CONCURRENCY": "2"}
        )
        assert s.ocr_concurrency == 2

    def test_one_disables_concurrency(self):
        assert (
            Settings.resolve(config={"sync": {"ocr_concurrency": 1}}, env={}).ocr_concurrency == 1
        )

    def test_zero_or_negative_falls_back_to_serial(self):
        assert (
            Settings.resolve(config={"sync": {"ocr_concurrency": 0}}, env={}).ocr_concurrency == 1
        )
        assert (
            Settings.resolve(config={"sync": {"ocr_concurrency": -4}}, env={}).ocr_concurrency == 1
        )


class TestTranscriptCache:
    """The cache is on by default, and both of its knobs are reachable."""

    def test_caching_is_on_by_default(self):
        assert Settings.resolve(config={}, env={}).transcript_cache is True

    def test_config_can_turn_it_off(self):
        s = Settings.resolve(config={"sync": {"transcript_cache": False}}, env={})
        assert s.transcript_cache is False

    def test_env_outranks_config(self):
        s = Settings.resolve(
            config={"sync": {"transcript_cache": True}},
            env={"SYNC_TRANSCRIPT_CACHE": "false"},
        )
        assert s.transcript_cache is False

    def test_the_prune_age_defaults_to_a_season(self):
        s = Settings.resolve(config={}, env={})
        assert s.cache_max_age_days == DEFAULT_CACHE_MAX_AGE_DAYS

    def test_config_sets_the_prune_age(self):
        s = Settings.resolve(config={"sync": {"cache_max_age_days": 7}}, env={})
        assert s.cache_max_age_days == 7

    def test_render_caching_is_on_by_default(self):
        assert Settings.resolve(config={}, env={}).render_cache is True

    def test_config_can_turn_the_render_cache_off(self):
        s = Settings.resolve(config={"sync": {"render_cache": False}}, env={})
        assert s.render_cache is False

    def test_env_outranks_config_for_the_render_cache(self):
        s = Settings.resolve(
            config={"sync": {"render_cache": True}},
            env={"SYNC_RENDER_CACHE": "false"},
        )
        assert s.render_cache is False

    def test_the_two_caches_are_independent(self):
        """Rendering is free to reuse even when transcription must not."""
        s = Settings.resolve(config={"sync": {"transcript_cache": False}}, env={})
        assert s.render_cache is True


class TestExplain:
    """Every setting can say which layer supplied its value."""

    def test_reports_the_layer_that_won(self):
        origins = {
            o.name: o
            for o in Settings.explain(
                config={"sync": {"ocr_concurrency": 8}, "remarkable": {"ssh_host": "10.0.0.1"}},
                env={"SYNC_OCR_CONCURRENCY": "2"},
            )
        }

        assert (origins["ocr_concurrency"].value, origins["ocr_concurrency"].source) == (
            2,
            SOURCE_ENV,
        )
        assert (origins["ssh_host"].value, origins["ssh_host"].source) == (
            "10.0.0.1",
            SOURCE_CONFIG,
        )
        assert origins["ssh_user"].source == SOURCE_DEFAULT

    def test_an_empty_env_var_does_not_count_as_set(self):
        """An exported-but-blank variable is how shells leave unset values."""
        origins = {o.name: o for o in Settings.explain(config={}, env={"REMARKABLE_SSH_USER": ""})}

        assert origins["ssh_user"].source == SOURCE_DEFAULT

    def test_covers_every_field_and_agrees_with_resolve(self):
        config = {"remarkable": {"preferred_connection": "cloud"}}
        env = {"SYNC_PDFS": "true"}
        resolved = Settings.resolve(config=config, env=env)

        origins = Settings.explain(config=config, env=env)

        assert {o.name for o in origins} == set(FIELD_ENV_VARS)
        assert all(getattr(resolved, o.name) == o.value for o in origins)

    def test_the_token_is_never_displayed(self):
        origins = {
            o.name: o
            for o in Settings.explain(config={"remarkable": {"device_token": "sekrit"}}, env={})
        }
        token = origins["remarkable_token"]

        assert token.secret is True
        assert token.display() == "set"
        assert "sekrit" not in token.display()

    def test_display_renders_booleans_and_absences_readably(self):
        origins = {o.name: o for o in Settings.explain(config={}, env={"SYNC_PDFS": "true"})}

        assert origins["sync_pdfs"].display() == "true"
        assert origins["sync_epubs"].display() == "false"
        assert origins["remarkable_token"].display() == "not set"

    def test_names_the_variable_that_would_override(self):
        origins = {o.name: o for o in Settings.explain(config={}, env={})}

        assert origins["ocr_concurrency"].env_var == "SYNC_OCR_CONCURRENCY"
