"""Tests for the typed Settings object and its precedence rules."""

from dataclasses import FrozenInstanceError, fields

import pytest

from living_ink.config import credentials
from living_ink.config.schema import SETTINGS, STORE_CONFIG, STORE_CREDENTIALS, STORE_ENV_ONLY
from living_ink.settings import (
    DEFAULT_CACHE_MAX_AGE_DAYS,
    DEFAULT_OCR_CONCURRENCY,
    DEFAULT_SSH_HOST,
    SOURCE_CONFIG,
    SOURCE_CREDENTIALS,
    SOURCE_DEFAULT,
    SOURCE_ENV,
    SOURCE_FLAG,
    Settings,
    as_bool,
    as_int,
    as_list,
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

    def test_as_list_reads_a_yaml_list(self):
        assert as_list(["work", " ideas "]) == ("work", "ideas")

    def test_as_list_reads_a_comma_separated_string(self):
        """An environment variable has no other way to say "several"."""
        assert as_list("work, ideas") == ("work", "ideas")

    def test_as_list_drops_blanks_and_keeps_a_lone_value(self):
        assert as_list("work,,") == ("work",)
        assert as_list("") == ()
        assert as_list(None, ("fallback",)) == ("fallback",)


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
                "preferred_connection": "Cloud",
                "ssh_host": "192.168.1.5",
                "ssh_user": "rm",
                "ssh_port": "2222",
            },
            "ai": {"provider": "gemini", "model": "gemini-2.0-flash", "temperature": "0.7"},
            "ocr": {"concurrency": 6},
            "sync": {"sync_pdfs": True, "sync_epubs": "yes", "limit": 5, "tags": ["work"]},
            "obsidian": {"enabled": True, "vault_path": "/tmp/vault", "root_folder": "Ink"},
            "cache": {"transcripts": False},
            "output": {"verbosity": "verbose"},
        }
        s = Settings.resolve(config=config, env={})

        assert s.preferred_connection == "cloud"
        assert s.ssh_host == "192.168.1.5"
        assert s.ssh_user == "rm"
        assert s.ssh_port == 2222
        assert s.ai_provider == "gemini"
        assert s.ai_model == "gemini-2.0-flash"
        assert s.ai_temperature == 0.7
        assert s.ocr_concurrency == 6
        assert s.sync_pdfs is True
        assert s.sync_epubs is True
        assert s.max_notebooks_per_run == 5
        assert s.sync_tags == ("work",)
        assert s.obsidian_enabled is True
        assert s.obsidian_vault_path == "/tmp/vault"
        assert s.obsidian_root_folder == "Ink"
        assert s.transcript_cache is False
        assert s.verbosity == "verbose"

    def test_a_blank_string_in_the_file_is_a_value_not_an_omission(self):
        """An empty attachments folder means "beside the note", not "default"."""
        s = Settings.resolve(config={"obsidian": {"attachments_folder": ""}}, env={})
        assert s.obsidian_attachments_folder == ""

    def test_a_blank_environment_variable_is_an_omission(self):
        """A variable set to nothing is how a shell says "unset"."""
        s = Settings.resolve(
            config={"obsidian": {"attachments_folder": "Pages"}},
            env={"LIVING_INK_OBSIDIAN_ATTACHMENTS_FOLDER": ""},
        )
        assert s.obsidian_attachments_folder == "Pages"

    def test_cloud_preference_disables_ssh_unless_stated(self):
        s = Settings.resolve(config={"remarkable": {"preferred_connection": "cloud"}}, env={})
        assert s.use_ssh is False

    def test_explicit_use_ssh_wins_over_preference(self):
        config = {"remarkable": {"preferred_connection": "cloud", "use_ssh": True}}
        assert Settings.resolve(config=config, env={}).use_ssh is True

    def test_none_sections_are_tolerated(self):
        s = Settings.resolve(config={"remarkable": None, "sync": None}, env={})
        assert s.ssh_host == DEFAULT_SSH_HOST


class TestLegacyKeys:
    """A renamed key keeps reading from wherever the user already wrote it.

    The pairing is declared once, on the surviving setting, as
    ``legacy_keys`` — so nothing has to be renamed in a config file that
    already works.
    """

    def test_the_old_top_level_use_ssh(self):
        assert Settings.resolve(config={"use_ssh": False}, env={}).use_ssh is False

    def test_the_old_sync_section_spellings(self):
        config = {
            "sync": {
                "max_notebooks_per_run": 5,
                "ocr_concurrency": 8,
                "transcript_cache": False,
                "render_cache": False,
                "cache_max_age_days": 7,
            }
        }
        s = Settings.resolve(config=config, env={})

        assert s.max_notebooks_per_run == 5
        assert s.ocr_concurrency == 8
        assert s.transcript_cache is False
        assert s.render_cache is False
        assert s.cache_max_age_days == 7

    def test_the_old_openai_section(self):
        s = Settings.resolve(config={"openai": {"model": "gpt-4o-mini"}}, env={})
        assert s.ai_model == "gpt-4o-mini"

    def test_the_old_device_token_key(self):
        """A 0.1 install kept its cloud token in config.yml. It still reads."""
        s = Settings.resolve(config={"remarkable": {"device_token": " tok "}}, env={})
        assert s.remarkable_token == "tok"

    def test_the_current_spelling_wins_when_both_are_written(self):
        config = {"sync": {"max_notebooks_per_run": 5, "limit": 9}}
        assert Settings.resolve(config=config, env={}).max_notebooks_per_run == 9


class TestCredentials:
    """Secrets resolve from the credentials directory, not from config.yml."""

    def test_a_stored_token_is_read(self):
        credentials.write_secret(credentials.CLOUD_TOKEN, "stored-token")
        assert Settings.resolve(config={}, env={}).remarkable_token == "stored-token"

    def test_a_stored_token_beats_one_left_in_an_old_config(self):
        """Downgrading never deleted the old copy, so the new one must win."""
        credentials.write_secret(credentials.CLOUD_TOKEN, "stored-token")
        config = {"remarkable": {"device_token": "from-config"}}
        assert Settings.resolve(config=config, env={}).remarkable_token == "stored-token"

    def test_the_environment_still_beats_a_stored_token(self):
        credentials.write_secret(credentials.CLOUD_TOKEN, "stored-token")
        s = Settings.resolve(config={}, env={"REMARKABLE_TOKEN": "from-env"})
        assert s.remarkable_token == "from-env"

    def test_the_ai_key_is_read_under_the_configured_provider(self):
        credentials.write_secret(credentials.ai_key_name("gemini"), "gemini-key")
        credentials.write_secret(credentials.ai_key_name("openai"), "openai-key")

        assert Settings.resolve({"ai": {"provider": "gemini"}}, env={}).ai_api_key == "gemini-key"
        assert Settings.resolve({"ai": {"provider": "openai"}}, env={}).ai_api_key == "openai-key"

    def test_no_provider_means_no_key_to_look_up(self):
        credentials.write_secret(credentials.ai_key_name("gemini"), "gemini-key")
        assert Settings.resolve(config={}, env={}).ai_api_key is None
        assert Settings.resolve({"ai": {"provider": "none"}}, env={}).ai_api_key is None

    def test_a_key_left_in_an_old_config_is_still_read(self):
        s = Settings.resolve({"openai": {"api_key": "sk-x"}}, env={})
        assert s.ai_api_key == "sk-x"

    def test_the_ssh_password_comes_from_the_store(self):
        credentials.write_secret(credentials.SSH_PASSWORD, "hunter2")
        assert Settings.resolve(config={}, env={}).ssh_password == "hunter2"


class TestPrecedence:
    """Flag beats environment; environment beats config; config beats defaults."""

    def test_env_overrides_config(self):
        config = {
            "remarkable": {"ssh_host": "config-host"},
            "sync": {"limit": 5, "sync_pdfs": False},
            "obsidian": {"root_folder": "ConfigFolder"},
        }
        env = {
            "REMARKABLE_SSH_HOST": "env-host",
            "SYNC_MAX_NOTEBOOKS": "9",
            "SYNC_PDFS": "true",
            "LIVING_INK_OBSIDIAN_ROOT_FOLDER": "EnvFolder",
        }
        s = Settings.resolve(config=config, env=env)

        assert s.ssh_host == "env-host"
        assert s.max_notebooks_per_run == 9
        assert s.sync_pdfs is True
        assert s.obsidian_root_folder == "EnvFolder"

    def test_a_flag_overrides_the_environment(self):
        s = Settings.resolve(
            config={"remarkable": {"ssh_host": "config-host"}},
            env={"REMARKABLE_SSH_HOST": "env-host"},
            flags={"ssh_host": "flag-host"},
        )
        assert s.ssh_host == "flag-host"

    def test_an_absent_flag_is_none_not_empty(self):
        """argparse hands over every option it knows, set or not."""
        s = Settings.resolve(
            config={"remarkable": {"ssh_host": "config-host"}},
            env={},
            flags={"ssh_host": None, "max_notebooks_per_run": None},
        )
        assert s.ssh_host == "config-host"

    def test_blank_env_does_not_override_config(self):
        config = {"remarkable": {"ssh_host": "config-host"}}
        s = Settings.resolve(config=config, env={"REMARKABLE_SSH_HOST": ""})
        assert s.ssh_host == "config-host"

    def test_config_overrides_defaults(self):
        s = Settings.resolve(config={"sync": {"limit": 3}}, env={})
        assert s.max_notebooks_per_run == 3

    def test_from_env_ignores_config_entirely(self):
        s = Settings.from_env(env={"REMARKABLE_USE_SSH": "false"})
        assert s.use_ssh is False

    def test_from_env_defaults_to_os_environ(self, monkeypatch):
        monkeypatch.setenv("LIVING_INK_OBSIDIAN_ROOT_FOLDER", "FromProcess")
        assert Settings.from_env().obsidian_root_folder == "FromProcess"


class TestOcrConcurrency:
    """How many pages are transcribed at once is configurable, and never zero."""

    def test_defaults_to_a_handful(self):
        assert Settings.resolve(config={}, env={}).ocr_concurrency == DEFAULT_OCR_CONCURRENCY

    def test_config_sets_it(self):
        s = Settings.resolve(config={"ocr": {"concurrency": 8}}, env={})
        assert s.ocr_concurrency == 8

    def test_env_outranks_config(self):
        s = Settings.resolve(config={"ocr": {"concurrency": 8}}, env={"SYNC_OCR_CONCURRENCY": "2"})
        assert s.ocr_concurrency == 2

    def test_one_disables_concurrency(self):
        assert Settings.resolve(config={"ocr": {"concurrency": 1}}, env={}).ocr_concurrency == 1

    def test_zero_or_negative_falls_back_to_serial(self):
        assert Settings.resolve(config={"ocr": {"concurrency": 0}}, env={}).ocr_concurrency == 1
        assert Settings.resolve(config={"ocr": {"concurrency": -4}}, env={}).ocr_concurrency == 1


class TestTranscriptCache:
    """The cache is on by default, and both of its knobs are reachable."""

    def test_caching_is_on_by_default(self):
        assert Settings.resolve(config={}, env={}).transcript_cache is True

    def test_config_can_turn_it_off(self):
        s = Settings.resolve(config={"cache": {"transcripts": False}}, env={})
        assert s.transcript_cache is False

    def test_env_outranks_config(self):
        s = Settings.resolve(
            config={"cache": {"transcripts": True}},
            env={"SYNC_TRANSCRIPT_CACHE": "false"},
        )
        assert s.transcript_cache is False

    def test_the_prune_age_defaults_to_a_season(self):
        s = Settings.resolve(config={}, env={})
        assert s.cache_max_age_days == DEFAULT_CACHE_MAX_AGE_DAYS

    def test_config_sets_the_prune_age(self):
        s = Settings.resolve(config={"cache": {"max_age_days": 7}}, env={})
        assert s.cache_max_age_days == 7

    def test_render_caching_is_on_by_default(self):
        assert Settings.resolve(config={}, env={}).render_cache is True

    def test_config_can_turn_the_render_cache_off(self):
        s = Settings.resolve(config={"cache": {"renders": False}}, env={})
        assert s.render_cache is False

    def test_env_outranks_config_for_the_render_cache(self):
        s = Settings.resolve(
            config={"cache": {"renders": True}},
            env={"SYNC_RENDER_CACHE": "false"},
        )
        assert s.render_cache is False

    def test_the_two_caches_are_independent(self):
        """Rendering is free to reuse even when transcription must not."""
        s = Settings.resolve(config={"cache": {"transcripts": False}}, env={})
        assert s.render_cache is True


class TestExplain:
    """Every setting can say which layer supplied its value."""

    def test_reports_the_layer_that_won(self):
        origins = {
            o.name: o
            for o in Settings.explain(
                config={"ocr": {"concurrency": 8}, "remarkable": {"ssh_host": "10.0.0.1"}},
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

    def test_names_the_particular_thing_that_supplied_it(self):
        """ "From a flag" is not actionable; "from --ssh-host" is."""
        origins = {
            o.name: o
            for o in Settings.explain(
                config={"remarkable": {"ssh_user": "rm"}},
                env={"REMARKABLE_SSH_PORT": "2222"},
                flags={"ssh_host": "flag-host"},
            )
        }

        assert origins["ssh_host"].origin_detail == "--ssh-host"
        assert origins["ssh_port"].origin_detail == "REMARKABLE_SSH_PORT"
        assert origins["ssh_user"].origin_detail == "remarkable.ssh_user"
        assert origins["ssh_host"].source == SOURCE_FLAG

    def test_a_legacy_key_is_reported_under_the_key_it_was_read_from(self):
        """Telling a user their value came from a key they did not write is useless."""
        origins = {o.name: o for o in Settings.explain(config={"use_ssh": False}, env={})}

        assert origins["use_ssh"].origin_detail == "use_ssh"
        assert origins["use_ssh"].label == "remarkable.use_ssh"

    def test_a_stored_credential_names_the_store(self):
        credentials.write_secret(credentials.CLOUD_TOKEN, "stored-token")
        origins = {o.name: o for o in Settings.explain(config={}, env={})}

        assert origins["remarkable_token"].source == SOURCE_CREDENTIALS
        assert origins["remarkable_token"].origin_detail == credentials.CLOUD_TOKEN

    def test_an_empty_env_var_does_not_count_as_set(self):
        """An exported-but-blank variable is how shells leave unset values."""
        origins = {o.name: o for o in Settings.explain(config={}, env={"REMARKABLE_SSH_USER": ""})}

        assert origins["ssh_user"].source == SOURCE_DEFAULT

    def test_covers_every_field_and_agrees_with_resolve(self):
        config = {"remarkable": {"preferred_connection": "cloud"}}
        env = {"SYNC_PDFS": "true"}
        resolved = Settings.resolve(config=config, env=env)

        origins = Settings.explain(config=config, env=env)

        assert {o.name for o in origins} == {s.field for s in SETTINGS}
        assert all(getattr(resolved, o.name) == o.value for o in origins)

    def test_the_token_is_never_displayed(self):
        origins = {
            o.name: o
            for o in Settings.explain(config={"remarkable": {"device_token": "sekrit"}}, env={})
        }
        token = origins["remarkable_token"]

        assert token.secret is True
        assert token.display() == "••••••••"
        assert "sekrit" not in token.display()

    def test_a_long_token_shows_its_edges_and_nothing_else(self):
        """Enough to recognise which token it is, not enough to use it."""
        secret = "eyJhbGciOi-middle-of-a-real-jwt-9f3c"
        origins = {
            o.name: o
            for o in Settings.explain(config={"remarkable": {"device_token": secret}}, env={})
        }
        shown = origins["remarkable_token"].display()

        assert shown == "eyJh••••••••9f3c"
        assert "middle-of-a-real-jwt" not in shown

    def test_display_renders_booleans_lists_and_absences_readably(self):
        origins = {o.name: o for o in Settings.explain(config={}, env={"SYNC_PDFS": "true"})}

        assert origins["sync_pdfs"].display() == "true"
        assert origins["sync_epubs"].display() == "false"
        assert origins["remarkable_token"].display() == "not set"
        assert origins["sync_exclude"].display() == "Trash, Templates, Quick sheets"
        assert origins["sync_tags"].display() == "not set"

    def test_names_the_variable_that_would_override(self):
        origins = {o.name: o for o in Settings.explain(config={}, env={})}

        assert origins["ocr_concurrency"].env_var == "SYNC_OCR_CONCURRENCY"


class TestSchemaParity:
    """The schema and the dataclass are one list, checked rather than trusted.

    Settings used to be written down in four places — the dataclass, the
    validator's table, ``FIELD_ENV_VARS`` and the CLI's flags — and they had
    drifted: ``explain`` was blind to settings that shaped every run. There is
    one list now, and this is what keeps it one.
    """

    def test_every_schema_setting_is_a_field_and_the_reverse(self):
        declared = {setting.field for setting in SETTINGS}
        present = {f.name for f in fields(Settings)}

        assert declared == present

    def test_no_two_settings_share_an_environment_variable(self):
        """A shared variable silently sets two things, or the wrong one."""
        names = [setting.env for setting in SETTINGS if setting.env]

        assert len(names) == len(set(names))

    def test_no_two_settings_share_a_config_key(self):
        keys = [setting.key for setting in SETTINGS if setting.key]

        assert len(keys) == len(set(keys))

    def test_no_legacy_key_collides_with_a_current_one(self):
        """A path that means two things resolves as whichever is checked first."""
        current = {setting.key for setting in SETTINGS if setting.key}
        legacy = [path for setting in SETTINGS for path in setting.legacy_keys]

        assert len(legacy) == len(set(legacy))
        assert not (current & set(legacy))

    def test_every_setting_has_help(self):
        """The help line is what the menu, the generated config and info print."""
        assert all(setting.help.strip() for setting in SETTINGS)

    def test_every_setting_is_documented_on_the_dataclass(self):
        """A field nobody can look up is a field nobody configures."""
        doc = Settings.__doc__ or ""

        missing = [setting.field for setting in SETTINGS if f"{setting.field}:" not in doc]
        assert missing == []

    def test_a_secret_is_never_storable_in_the_config_file(self):
        """A key in config.yml is a key in a backup, a sync folder and a gist."""
        for setting in SETTINGS:
            if setting.secret:
                assert setting.store == STORE_CREDENTIALS
                assert setting.key is None
                assert setting.flag is None

    def test_every_setting_can_be_supplied_by_something(self):
        """A setting with no key, no flag and no variable cannot be set at all."""
        for setting in SETTINGS:
            assert setting.key or setting.env or setting.flag, setting.field

    def test_a_keyless_setting_is_declared_as_such(self):
        """``store`` is how info explains why a setting is not in the file."""
        for setting in SETTINGS:
            if setting.store == STORE_CONFIG:
                assert setting.key, setting.field
            else:
                assert setting.key is None, setting.field

    def test_an_env_only_setting_has_a_variable(self):
        for setting in SETTINGS:
            if setting.store == STORE_ENV_ONLY:
                assert setting.env, setting.field

    def test_a_choice_setting_enumerates_its_choices(self):
        for setting in SETTINGS:
            if setting.choices:
                values = [choice.value for choice in setting.choices]
                assert len(values) == len(set(values))
                assert setting.default is None or setting.default in values
