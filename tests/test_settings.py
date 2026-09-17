"""Tests for the typed Settings object and its precedence rules."""

from dataclasses import FrozenInstanceError

import pytest

from living_ink.settings import (
    DEFAULT_APPLE_NOTES_FOLDER,
    DEFAULT_SSH_HOST,
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
