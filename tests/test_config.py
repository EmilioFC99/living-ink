"""Tests for configuration paths and the ``config.yml`` schema."""

import yaml

from living_ink.config import (
    ERROR,
    SCHEMA_VERSION,
    WARNING,
    ConfigProblem,
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

    def test_a_misspelled_section_is_refused_with_a_suggestion(self):
        """``obsidain:`` parses fine and does nothing — the whole reason for this."""
        problems = validate_config({"obsidain": {"vault_path": "/tmp/v"}})
        assert [p.level for p in problems] == [ERROR]
        assert "did you mean obsidian?" in problems[0].describe()

    def test_a_misspelled_key_is_refused_with_a_suggestion(self):
        """A typo inside a real section is the same failure one level down."""
        problems = validate_config({"obsidian": {"vault-path": "/tmp/v"}})
        assert [p.level for p in problems] == [ERROR]
        assert problems[0].path == "obsidian.vault-path"
        assert "vault_path" in problems[0].hint

    def test_an_unrecognisable_key_is_refused_without_a_guess(self):
        """Suggesting the nearest key for something unrelated would mislead."""
        problems = validate_config({"obsidian": {"zzzzzzz": 1}})
        assert [p.level for p in problems] == [ERROR]
        assert problems[0].hint == ""

    def test_every_unknown_key_is_reported_not_just_the_first(self):
        """One slip usually means several; fixing them one run at a time is misery."""
        problems = validate_config({"nonsense": {"a": 1}, "sync": {"whatever": 2}})
        assert [p.path for p in problems] == ["nonsense", "sync.whatever"]
        assert {p.level for p in problems} == {ERROR}

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
        """Adding a destination must not make its own config look like a typo.

        The escape hatch that makes strictness affordable: a section this build
        was not shipped with is legitimate exactly when something registered it.
        """
        assert validate_config({"notion": {"token": "x"}}, extra_sections=("notion",)) == []
        assert [p.level for p in validate_config({"notion": {"token": "x"}})] == [ERROR]

    def test_a_newer_schema_version_is_refused(self):
        """Running an old build over a new file would ignore half of it."""
        problems = validate_config({"schema_version": SCHEMA_VERSION + 1})
        assert [p.level for p in problems] == [ERROR]
        assert "upgrade" in problems[0].hint

    def test_a_config_with_no_schema_version_is_version_one(self):
        """Every config written before this existed is a version 1 config."""
        assert validate_config({"sync": {"sync_pdfs": True}}) == []

    def test_split_problems_separates_by_level(self):
        """Callers act on the two levels differently, so they arrive separated.

        Built by hand rather than through validate_config: nothing in the
        schema warns today, because WARNING is held for deprecations rather
        than spent on keys that cannot be read at all.
        """
        errors, warnings = split_problems(
            [
                ConfigProblem(ERROR, "sync.ocr_concurrency", "expected whole number"),
                ConfigProblem(WARNING, "destination", "deprecated"),
            ]
        )
        assert [p.path for p in errors] == ["sync.ocr_concurrency"]
        assert [p.path for p in warnings] == ["destination"]

    def test_nothing_in_the_schema_warns_today(self):
        """A key that cannot be read is refused, never downgraded to a warning."""
        problems = validate_config({"typo": 1, "obsidian": {"nope": 2, "enabled": "maybe"}})
        assert problems and all(p.level == ERROR for p in problems)
