"""Tests for configuration paths and the ``config.yml`` schema."""

import yaml

from living_ink.config import (
    ERROR,
    SCHEMA_VERSION,
    WARNING,
    split_problems,
    validate_config,
)
from living_ink.setup_wizard import generate_config_yaml


class TestConfigSchema:
    """The schema catches what a silent parse used to let through."""

    def test_an_empty_config_is_valid(self):
        """Nothing configured is a legitimate state: every setting has a default."""
        assert validate_config(None) == []
        assert validate_config({}) == []

    def test_a_wizard_written_config_is_clean(self):
        """The file Living Ink writes itself must not warn about itself."""
        content = generate_config_yaml(
            ai_provider="gemini",
            ai_api_key="k",
            ai_model="gemini-2.0-flash",
            remarkable_token="",
            preferred_connection="ssh",
            obsidian_enabled=True,
            obsidian_vault_path="/tmp/vault",
        )
        assert validate_config(yaml.safe_load(content)) == []

    def test_a_misspelled_section_warns_with_a_suggestion(self):
        """``obsidain:`` parses fine and is ignored — the whole reason for this."""
        problems = validate_config({"obsidain": {"vault_path": "/tmp/v"}})
        assert [p.level for p in problems] == [WARNING]
        assert "did you mean obsidian?" in problems[0].describe()

    def test_a_misspelled_key_warns_with_a_suggestion(self):
        """A typo inside a real section is the same failure one level down."""
        problems = validate_config({"obsidian": {"vault-path": "/tmp/v"}})
        assert [p.level for p in problems] == [WARNING]
        assert problems[0].path == "obsidian.vault-path"
        assert "vault_path" in problems[0].hint

    def test_an_unrecognisable_key_warns_without_a_guess(self):
        """Suggesting the nearest key for something unrelated would mislead."""
        problems = validate_config({"obsidian": {"zzzzzzz": 1}})
        assert problems[0].hint == ""

    def test_an_unknown_key_is_never_an_error(self):
        """An old config, or one holding a plugin's key, must keep working."""
        problems = validate_config({"nonsense": {"a": 1}, "sync": {"whatever": 2}})
        assert {p.level for p in problems} == {WARNING}

    def test_a_value_nothing_can_read_is_an_error(self):
        """Left as a warning this becomes a silent fallback to the default."""
        problems = validate_config({"sync": {"max_notebooks_per_run": "many"}})
        assert [p.level for p in problems] == [ERROR]
        assert "whole number" in problems[0].message

    def test_a_quoted_number_is_accepted(self):
        """YAML cannot say "this 22 is a port", and Settings already coerces."""
        assert validate_config({"remarkable": {"ssh_port": "22"}}) == []

    def test_a_boolean_is_not_a_port(self):
        """bool is an int in Python; that must not make ``ssh_port: true`` legal."""
        problems = validate_config({"remarkable": {"ssh_port": True}})
        assert [p.level for p in problems] == [ERROR]

    def test_a_flag_accepts_the_words_yaml_does_not_fold(self):
        """``enabled: "on"`` survives quoting as a string and still means on."""
        assert validate_config({"obsidian": {"enabled": "on"}}) == []
        assert [p.level for p in validate_config({"obsidian": {"enabled": "maybe"}})] == [ERROR]

    def test_a_section_written_as_a_scalar_is_an_error(self):
        """Forgetting to indent turns a section into a string, and loses it."""
        problems = validate_config({"obsidian": "/tmp/vault"})
        assert [p.level for p in problems] == [ERROR]
        assert "indent" in problems[0].hint

    def test_an_empty_section_is_valid(self):
        """``sync:`` with nothing under it parses as None and sets nothing."""
        assert validate_config({"sync": None}) == []

    def test_an_empty_value_is_valid(self):
        """The wizard writes ``credentials_path:`` blank; that is not a type error."""
        assert validate_config({"google_vision": {"credentials_path": None}}) == []

    def test_the_legacy_destination_block_is_not_policed(self):
        """Its keys belong to whichever destination it names, not to this schema."""
        assert validate_config({"destination": {"type": "obsidian", "path": "/tmp"}}) == []

    def test_a_registered_destination_section_is_known(self):
        """Adding a destination must not make its own config look like a typo."""
        assert validate_config({"notion": {"token": "x"}}, extra_sections=("notion",)) == []
        assert [p.level for p in validate_config({"notion": {"token": "x"}})] == [WARNING]

    def test_a_newer_schema_version_is_refused(self):
        """Running an old build over a new file would ignore half of it."""
        problems = validate_config({"schema_version": SCHEMA_VERSION + 1})
        assert [p.level for p in problems] == [ERROR]
        assert "upgrade" in problems[0].hint

    def test_a_config_with_no_schema_version_is_version_one(self):
        """Every config written before this existed is a version 1 config."""
        assert validate_config({"sync": {"sync_pdfs": True}}) == []

    def test_split_problems_separates_by_level(self):
        """Callers act on the two levels differently, so they arrive separated."""
        errors, warnings = split_problems(
            validate_config({"typo": 1, "sync": {"ocr_concurrency": "lots"}})
        )
        assert len(errors) == 1 and len(warnings) == 1
