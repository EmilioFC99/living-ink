"""Tests for the stored-credentials directory.

Everything here is about one promise: a secret lives beside ``config.yml``,
never inside it, at a mode no other account on the machine can read.
"""

import stat

import pytest

from living_ink.config import credentials
from living_ink.config.paths import credentials_dir


@pytest.fixture
def config_path(tmp_path):
    """Point the credentials directory at a throwaway config.

    Args:
        tmp_path: Pytest temporary directory.

    Returns:
        A ``config.yml`` path that need not exist; the credentials directory is
        derived from it, so nothing here can touch the real one.
    """
    return tmp_path / "living-ink" / "config.yml"


class TestWhereCredentialsLive:
    """The directory is derived from the config, not from the home directory."""

    def test_it_sits_beside_the_config_file(self, config_path):
        assert credentials_dir(config_path) == config_path.parent / "credentials"

    def test_a_second_profile_gets_its_own_credentials(self, tmp_path):
        """``LIVING_INK_CONFIG`` pointing elsewhere must take the secrets with it.

        Otherwise a user keeping a second profile would silently sync the first
        profile's account, which is the one failure mode a "profile" exists to
        prevent.
        """
        work = tmp_path / "work" / "config.yml"
        home = tmp_path / "home" / "config.yml"

        credentials.write_secret(credentials.CLOUD_TOKEN, "work-token", config_path=work)

        assert credentials.read_secret(credentials.CLOUD_TOKEN, config_path=work) == "work-token"
        assert credentials.read_secret(credentials.CLOUD_TOKEN, config_path=home) is None

    def test_it_follows_the_config_environment_variable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LIVING_INK_CONFIG_DIR", str(tmp_path / "elsewhere"))

        credentials.write_secret(credentials.CLOUD_TOKEN, "tok")

        assert (tmp_path / "elsewhere" / "credentials" / credentials.CLOUD_TOKEN).exists()


class TestRoundTrip:
    """Writing and reading one secret."""

    def test_a_secret_survives_a_write_and_a_read(self, config_path):
        credentials.write_secret(credentials.CLOUD_TOKEN, "jwt-value", config_path=config_path)

        assert (
            credentials.read_secret(credentials.CLOUD_TOKEN, config_path=config_path) == "jwt-value"
        )

    def test_a_missing_secret_reads_as_none(self, config_path):
        assert credentials.read_secret(credentials.CLOUD_TOKEN, config_path=config_path) is None

    def test_surrounding_whitespace_is_stripped(self, config_path):
        """A key pasted from a browser arrives with a trailing newline.

        A key that differs from the one the user copied is unfalsifiable from
        their side, so the newline is removed rather than stored.
        """
        credentials.write_secret(credentials.CLOUD_TOKEN, "  tok\n", config_path=config_path)

        assert credentials.read_secret(credentials.CLOUD_TOKEN, config_path=config_path) == "tok"

    def test_a_rewrite_replaces_rather_than_appends(self, config_path):
        credentials.write_secret(credentials.CLOUD_TOKEN, "first", config_path=config_path)
        credentials.write_secret(credentials.CLOUD_TOKEN, "second", config_path=config_path)

        assert credentials.read_secret(credentials.CLOUD_TOKEN, config_path=config_path) == "second"

    def test_an_empty_value_is_refused(self, config_path):
        """Storing "" produces a file that reads back as absent — a disguised delete."""
        with pytest.raises(ValueError):
            credentials.write_secret(credentials.CLOUD_TOKEN, "   ", config_path=config_path)

        assert not credentials_dir(config_path).joinpath(credentials.CLOUD_TOKEN).exists()

    def test_a_unicode_secret_round_trips(self, config_path):
        credentials.write_secret(credentials.SSH_PASSWORD, "påsswörd✓", config_path=config_path)

        assert (
            credentials.read_secret(credentials.SSH_PASSWORD, config_path=config_path)
            == "påsswörd✓"
        )


class TestPermissions:
    """A credential no other account can read, in a directory none can replace."""

    def test_a_written_secret_is_owner_only(self, config_path):
        path = credentials.write_secret(credentials.CLOUD_TOKEN, "tok", config_path=config_path)

        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_the_directory_is_owner_only(self, config_path):
        """0600 inside a world-writable directory can still be swapped out."""
        credentials.write_secret(credentials.CLOUD_TOKEN, "tok", config_path=config_path)

        assert stat.S_IMODE(credentials_dir(config_path).stat().st_mode) == 0o700

    def test_ensure_directory_creates_it_owner_only(self, config_path):
        directory = credentials.ensure_directory(config_path=config_path)

        assert directory.is_dir()
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700

    def test_a_loose_credential_is_found(self, config_path):
        """A file restored from a backup arrives with whatever mode it had."""
        path = credentials.write_secret(credentials.CLOUD_TOKEN, "tok", config_path=config_path)
        path.chmod(0o644)

        assert credentials.insecure_credentials(config_path=config_path) == [path]

    def test_a_tight_credential_is_not_reported(self, config_path):
        credentials.write_secret(credentials.CLOUD_TOKEN, "tok", config_path=config_path)

        assert credentials.insecure_credentials(config_path=config_path) == []

    def test_no_credentials_at_all_is_not_a_problem(self, config_path):
        assert credentials.insecure_credentials(config_path=config_path) == []


class TestPerProviderKeys:
    """Switching AI providers must not destroy the key for the old one."""

    def test_the_name_carries_the_provider(self):
        assert credentials.ai_key_name("gemini") == "ai.api_key.gemini"

    def test_the_provider_is_normalised(self):
        """The same provider reaches this from a config file, a flag and a prompt."""
        assert credentials.ai_key_name("  GEMINI ") == "ai.api_key.gemini"

    def test_an_unnamed_provider_is_refused(self):
        with pytest.raises(ValueError):
            credentials.ai_key_name("   ")

    def test_two_providers_keep_separate_keys(self, config_path):
        credentials.write_secret(
            credentials.ai_key_name("gemini"), "AIza-gemini", config_path=config_path
        )
        credentials.write_secret(
            credentials.ai_key_name("openai"), "sk-openai", config_path=config_path
        )

        assert (
            credentials.read_secret(credentials.ai_key_name("gemini"), config_path=config_path)
            == "AIza-gemini"
        )
        assert (
            credentials.read_secret(credentials.ai_key_name("openai"), config_path=config_path)
            == "sk-openai"
        )

    def test_switching_away_and_back_finds_the_original_key(self, config_path):
        """The whole reason for the per-provider namespace."""
        credentials.write_secret(
            credentials.ai_key_name("gemini"), "AIza-gemini", config_path=config_path
        )
        credentials.write_secret(
            credentials.ai_key_name("openai"), "sk-openai", config_path=config_path
        )

        assert (
            credentials.read_secret(credentials.ai_key_name("gemini"), config_path=config_path)
            == "AIza-gemini"
        )

    def test_configured_providers_are_listed_without_reading_them(self, config_path):
        credentials.write_secret(credentials.ai_key_name("openai"), "b", config_path=config_path)
        credentials.write_secret(credentials.ai_key_name("gemini"), "a", config_path=config_path)
        credentials.write_secret(credentials.CLOUD_TOKEN, "tok", config_path=config_path)

        assert credentials.configured_ai_providers(config_path=config_path) == ["gemini", "openai"]


class TestNameValidation:
    """A credential name is also a filename, so it is restricted, not escaped."""

    @pytest.mark.parametrize(
        "name",
        [
            "../../../etc/passwd",
            "ai.api_key./etc/shadow",
            "..",
            "",
            "Has Spaces",
            "UPPER",
            "trailing.",
            "ai.api_key.gemini\n",
        ],
    )
    def test_a_name_that_could_escape_the_directory_is_refused(self, name, config_path):
        with pytest.raises(ValueError):
            credentials.read_secret(name, config_path=config_path)
        with pytest.raises(ValueError):
            credentials.write_secret(name, "value", config_path=config_path)

    def test_a_provider_name_from_a_config_file_cannot_traverse(self, config_path):
        """The untrusted route: the provider comes from ``config.yml``."""
        with pytest.raises(ValueError):
            credentials.ai_key_name("../../../../tmp/owned")

    @pytest.mark.parametrize(
        "name", ["remarkable.cloud_token", "ai.api_key.openrouter", "a-b", "x1"]
    )
    def test_an_ordinary_name_is_accepted(self, name, config_path):
        credentials.write_secret(name, "value", config_path=config_path)

        assert credentials.read_secret(name, config_path=config_path) == "value"


class TestListing:
    """Names may be printed; values may not."""

    def test_nothing_stored_lists_nothing(self, config_path):
        assert credentials.list_secrets(config_path=config_path) == []

    def test_names_come_back_sorted(self, config_path):
        credentials.write_secret(credentials.SSH_PASSWORD, "p", config_path=config_path)
        credentials.write_secret(credentials.CLOUD_TOKEN, "t", config_path=config_path)

        assert credentials.list_secrets(config_path=config_path) == [
            credentials.CLOUD_TOKEN,
            credentials.SSH_PASSWORD,
        ]

    def test_a_prefix_narrows_the_listing(self, config_path):
        credentials.write_secret(credentials.CLOUD_TOKEN, "t", config_path=config_path)
        credentials.write_secret(credentials.ai_key_name("gemini"), "k", config_path=config_path)

        assert credentials.list_secrets(credentials.AI_KEY_PREFIX, config_path=config_path) == [
            "ai.api_key.gemini"
        ]

    def test_a_stray_editor_backup_is_not_a_credential(self, config_path):
        """The directory belongs to the user; not everything in it is ours."""
        credentials.write_secret(credentials.CLOUD_TOKEN, "t", config_path=config_path)
        (credentials_dir(config_path) / "remarkable.cloud_token~").write_text("x")
        (credentials_dir(config_path) / "README").write_text("x")

        assert credentials.list_secrets(config_path=config_path) == [credentials.CLOUD_TOKEN]


class TestDeleting:
    """Removing a credential, and saying whether there was one."""

    def test_deleting_a_stored_secret_reports_true(self, config_path):
        credentials.write_secret(credentials.CLOUD_TOKEN, "t", config_path=config_path)

        assert credentials.delete_secret(credentials.CLOUD_TOKEN, config_path=config_path) is True
        assert credentials.read_secret(credentials.CLOUD_TOKEN, config_path=config_path) is None

    def test_deleting_nothing_reports_false(self, config_path):
        assert credentials.delete_secret(credentials.CLOUD_TOKEN, config_path=config_path) is False

    def test_deleting_one_provider_key_leaves_the_others(self, config_path):
        credentials.write_secret(credentials.ai_key_name("gemini"), "a", config_path=config_path)
        credentials.write_secret(credentials.ai_key_name("openai"), "b", config_path=config_path)

        credentials.delete_secret(credentials.ai_key_name("gemini"), config_path=config_path)

        assert credentials.configured_ai_providers(config_path=config_path) == ["openai"]


class TestMasking:
    """A key is shown so it can be recognised, not so it can be used."""

    def test_a_long_secret_keeps_its_edges(self):
        assert credentials.mask("AIzaSyTheMiddlePart3f2a") == "AIza••••••••3f2a"

    def test_a_short_secret_shows_nothing(self):
        """Showing the edges of a short value shows most of it."""
        assert credentials.mask("sk-abc") == "••••••••"

    def test_the_mask_does_not_leak_the_length(self):
        short = credentials.mask("A" * 20 + "tail")
        long = credentials.mask("A" * 400 + "tail")

        assert len(short) == len(long)

    def test_an_absent_secret_says_so(self):
        assert credentials.mask(None) == "not set"
        assert credentials.mask("") == "not set"
        assert credentials.mask("   ") == "not set"

    def test_no_part_of_the_middle_survives(self):
        assert "MiddlePart" not in credentials.mask("AIzaSyTheMiddlePart3f2a")


class TestMigration:
    """Credentials found in an older location are copied, never moved."""

    def test_a_value_from_the_old_location_is_stored(self, config_path):
        assert (
            credentials.migrate_secret(
                credentials.CLOUD_TOKEN, "old-token", config_path=config_path
            )
            is True
        )
        assert (
            credentials.read_secret(credentials.CLOUD_TOKEN, config_path=config_path) == "old-token"
        )

    def test_nothing_to_migrate_is_not_a_migration(self, config_path):
        assert (
            credentials.migrate_secret(credentials.CLOUD_TOKEN, None, config_path=config_path)
            is False
        )
        assert (
            credentials.migrate_secret(credentials.CLOUD_TOKEN, "  ", config_path=config_path)
            is False
        )

    def test_an_already_stored_credential_wins(self, config_path):
        """The stored one is the one this build wrote; the old file may be stale."""
        credentials.write_secret(credentials.CLOUD_TOKEN, "current", config_path=config_path)

        assert (
            credentials.migrate_secret(credentials.CLOUD_TOKEN, "stale", config_path=config_path)
            is False
        )
        assert (
            credentials.read_secret(credentials.CLOUD_TOKEN, config_path=config_path) == "current"
        )

    def test_a_failed_migration_degrades_instead_of_raising(self, config_path, monkeypatch):
        """A read-only config directory must not turn a sync into a crash."""

        def _boom(*args, **kwargs):
            raise OSError("read-only file system")

        monkeypatch.setattr(credentials, "write_secret", _boom)

        assert (
            credentials.migrate_secret(credentials.CLOUD_TOKEN, "tok", config_path=config_path)
            is False
        )


class TestRedaction:
    """A secret that reaches a log line is masked even by code that never knew."""

    def test_a_written_secret_is_registered_for_redaction(self, config_path):
        from living_ink.redact import redact

        credentials.write_secret(credentials.CLOUD_TOKEN, "hunter2-token", config_path=config_path)

        assert "hunter2-token" not in redact("token is hunter2-token")

    def test_a_read_secret_is_registered_for_redaction(self, config_path):
        from living_ink.redact import redact

        path = credentials_dir(config_path) / credentials.CLOUD_TOKEN
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("side-loaded-token", encoding="utf-8")

        credentials.read_secret(credentials.CLOUD_TOKEN, config_path=config_path)

        assert "side-loaded-token" not in redact("token is side-loaded-token")
