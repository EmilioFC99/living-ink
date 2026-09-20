"""Tests for configuration paths and the ``config.yml`` schema."""

import pytest
import yaml

from living_ink.config import (
    ACTIVE,
    DEPRECATED,
    ERROR,
    FLAG,
    LIVE_SETTINGS,
    REMOVED,
    SCHEMA_VERSION,
    SECTION_KEYS,
    SETTINGS,
    WARNING,
    ConfigProblem,
    Setting,
    apply_status,
    read_config_file,
    split_problems,
    validate,
    validate_config,
)
from living_ink.config.validate import _retired_problem
from living_ink.settings import Settings
from living_ink.setup_wizard import generate_config_yaml


class TestReadingTheFile:
    """One parser, two callers, and the errors reach both of them.

    The pipeline reads the file to run against it and the front end reads it
    before any command runs to learn how loud the console should be. Each
    answers a broken file differently — one prints the line and column, the
    other cannot print anything yet — so the reader raises and neither owns a
    second ``yaml.safe_load`` with its own idea of what an empty file means.
    """

    def test_a_missing_file_is_an_empty_config(self, tmp_path):
        """Not an error: every setting has a default, so no file is a valid state."""
        assert read_config_file(tmp_path / "nope.yml") == {}

    def test_a_file_of_only_comments_is_an_empty_config(self, tmp_path):
        """``safe_load`` returns None for this, which is not a mapping."""
        path = tmp_path / "config.yml"
        path.write_text("# nothing here yet\n", encoding="utf-8")
        assert read_config_file(path) == {}

    def test_malformed_yaml_raises_rather_than_resolving_to_nothing(self, tmp_path):
        """Silently reading a broken config as ``{}`` runs with every default.

        That is the worst outcome available: the run succeeds, publishes to
        the wrong place, and never mentions the file it could not read.
        """
        path = tmp_path / "config.yml"
        path.write_text("output:\n\tverbosity: quiet\n", encoding="utf-8")
        with pytest.raises(yaml.YAMLError):
            read_config_file(path)

    def test_a_top_level_list_is_refused(self, tmp_path):
        """Valid YAML, wrong shape — and every reader downstream assumes a mapping."""
        path = tmp_path / "config.yml"
        path.write_text("- sync\n- obsidian\n", encoding="utf-8")
        with pytest.raises(TypeError):
            read_config_file(path)


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
            ai_model="gemini-2.0-flash",
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
        """A key written with nothing after it is unset, not mistyped."""
        assert validate_config({"obsidian": {"vault_path": None}}) == []

    def test_the_legacy_destination_block_is_not_policed(self):
        """Its keys belong to whichever destination it names, not to this schema."""
        problems = validate_config({"destination": {"type": "obsidian", "path": "/tmp"}})
        assert [p.path for p in problems] == ["destination"]
        assert problems[0].level == WARNING

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
        """Callers act on the two levels differently, so they arrive separated."""
        errors, warnings = split_problems(
            [
                ConfigProblem(ERROR, "sync.ocr_concurrency", "expected whole number"),
                ConfigProblem(WARNING, "destination", "deprecated"),
            ]
        )
        assert [p.path for p in errors] == ["sync.ocr_concurrency"]
        assert [p.path for p in warnings] == ["destination"]

    def test_an_unreadable_key_is_never_downgraded_to_a_warning(self):
        """WARNING is for a setting on its way out, not one that cannot be read."""
        problems = validate_config({"typo": 1, "obsidian": {"nope": 2, "enabled": "maybe"}})
        assert problems and all(p.level == ERROR for p in problems)


class TestStatus:
    """A setting can be retired without the next run refusing to start.

    An unknown section is a hard ERROR, so deleting one from the schema on the
    day its code is deleted breaks every config that names it. ``status`` is
    the alternative: the schema keeps the entry, says what happened to it, and
    the loader acts on that.
    """

    def test_a_removed_section_warns_instead_of_aborting(self):
        """The whole point: a dead section is a sentence, not a stack trace."""
        problems = validate_config({"google_vision": {"credentials_path": "/tmp/creds.json"}})
        assert [p.level for p in problems] == [WARNING]
        assert "no longer used" in problems[0].message
        assert "ai:" in problems[0].hint

    def test_a_removed_section_is_dropped_before_anything_reads_it(self):
        """Downstream code must not have to remember that dead keys can appear."""
        loaded = apply_status({"google_vision": {"credentials_path": "/x"}, "sync": {}})
        assert "google_vision" not in loaded
        assert "sync" in loaded

    def test_a_key_inside_a_removed_section_is_not_policed(self):
        """The section is already reported; naming its keys adds noise.

        It must also not be an ERROR. A stray key in a section that is about to
        be discarded has no business stopping a run.
        """
        problems = validate_config({"google_vision": {"anything_at_all": 1}})
        assert [p.level for p in problems] == [WARNING]
        assert problems[0].path == "google_vision"

    def test_a_deprecated_section_names_its_replacement(self):
        """ "Deprecated" without "use this instead" is a dead end."""
        problems = validate_config({"openai": {"api_key": "sk-x"}})
        assert {p.level for p in problems} == {WARNING}
        section = next(p for p in problems if p.path == "openai")
        assert section.hint == "use ai"

    def test_a_deprecated_key_names_its_replacement(self):
        """Status lives on keys too, not only on whole sections."""
        problems = validate_config({"use_ssh": True})
        assert [p.level for p in problems] == [WARNING]
        assert problems[0].path == "use_ssh"
        assert problems[0].hint == "use remarkable.use_ssh"

    def test_a_deprecated_key_is_read_under_its_new_name(self):
        """Mapped in memory, so nothing downstream learns the old spelling."""
        loaded = apply_status({"use_ssh": False})
        assert loaded["remarkable"]["use_ssh"] is False

    def test_the_current_spelling_wins_when_a_config_names_both(self):
        """A file naming both is stating a preference, and it is the new one."""
        loaded = apply_status({"use_ssh": False, "remarkable": {"use_ssh": True}})
        assert loaded["remarkable"]["use_ssh"] is True

    def test_a_deprecated_key_is_left_where_the_user_wrote_it(self):
        """No config file is ever rewritten, and the in-memory copy says so.

        Living Ink migrates nothing: rewriting ``config.yml`` in place would
        lose the comments and the ordering, and a ``.bak`` is no mitigation for
        that. The old key is copied forward, never moved.
        """
        loaded = apply_status({"use_ssh": False})
        assert loaded["use_ssh"] is False

    def test_the_original_config_is_not_modified(self):
        """Callers hold on to what they parsed; this returns a new dict."""
        original = {"google_vision": {"credentials_path": "/x"}, "use_ssh": True}
        apply_status(original)
        assert original == {"google_vision": {"credentials_path": "/x"}, "use_ssh": True}

    def test_a_deprecated_value_that_cannot_be_read_is_still_an_error(self):
        """Renaming a key the run cannot parse anyway would waste the trip."""
        problems = validate_config({"use_ssh": "maybe"})
        assert [p.level for p in problems] == [ERROR]

    def test_an_unknown_section_is_still_refused(self):
        """Status is for what this build retired, not for what it never had."""
        assert [p.level for p in validate_config({"nonsense": {}})] == [ERROR]

    def test_a_config_using_only_current_spellings_is_silent(self):
        """The mechanism must cost nothing to a config that needs none of it."""
        assert (
            validate_config({"ai": {"provider": "gemini"}, "remarkable": {"use_ssh": True}}) == []
        )

    def test_a_0_2_0_config_loads_with_warnings_and_no_errors(self):
        """The one stale config in existence: four sections, zero aborts."""
        stale = {
            "openai": {"api_key": "sk-x", "model": "gpt-4o-mini"},
            "google_vision": {"credentials_path": "/tmp/creds.json"},
            "destination": {"type": "obsidian"},
            "use_ssh": True,
            "obsidian": {"vault_path": "/tmp/vault"},
        }
        errors, warnings = split_problems(validate_config(stale))
        assert errors == []
        assert warnings
        loaded = apply_status(stale)
        assert "google_vision" not in loaded
        assert loaded["ai"]["model"] == "gpt-4o-mini"
        assert loaded["remarkable"]["use_ssh"] is True
        # The API key has no current spelling in config.yml at all — it is a
        # credential now — so it is not mapped onto one. It is still read.
        assert Settings.resolve(loaded, env={}).ai_api_key == "sk-x"


def _retired(status, *, key="sync.sync_pdfs", replacement=None):
    """Build a setting carrying a status, for the cases the schema has none of.

    Args:
        status: The status to declare.
        key: The dotted config key.
        replacement: The key named in the warning, if any.

    Returns:
        A :class:`~living_ink.config.schema.Setting` and nothing else — no
        registration, so the module-level indexes are untouched.
    """
    return Setting(
        field=key.rpartition(".")[2],
        key=key,
        kind=FLAG,
        default=False,
        help="",
        status=status,
        replacement=replacement,
    )


class TestAKeyHasAStatusOfItsOwn:
    """A section is not the only thing that can be retired.

    :class:`~living_ink.config.schema.Setting` has carried ``status`` since the
    mechanism was built for sections, and nothing read it: a key whose feature
    had been deleted could only be retired by deleting the whole section it
    lived in, or by leaving it in the schema pretending to still work.
    """

    def test_an_active_key_has_nothing_to_report(self):
        """The overwhelming majority, and they must cost nothing."""
        assert _retired_problem("sync.limit", _retired(ACTIVE)) is None

    def test_a_deprecated_key_names_its_replacement(self):
        """Still read, still works, and the user is told where it went."""
        problem = _retired_problem("sync.sync_pdfs", _retired(DEPRECATED, replacement="sync.types"))
        assert problem == ConfigProblem(WARNING, "sync.sync_pdfs", "deprecated", "use sync.types")

    def test_a_removed_key_says_it_is_being_ignored(self):
        """The difference that matters: this one no longer does anything."""
        problem = _retired_problem("sync.sync_pdfs", _retired(REMOVED, replacement="sync.types"))
        assert problem.level == WARNING
        assert problem.message == "no longer used, and ignored"
        assert problem.hint == "use sync.types instead"

    def test_a_removed_key_with_no_successor_is_told_to_go(self):
        """Nothing replaced it, so the only useful advice is to delete the line."""
        assert _retired_problem("sync.sync_pdfs", _retired(REMOVED)).hint == "delete it"

    def test_a_removed_key_is_dropped_before_anything_reads_it(self, monkeypatch):
        """Same contract as a removed section, one level down.

        A retired setting has no field on :class:`Settings` to resolve onto, so
        leaving it in the loaded config would hand the dataclass a keyword it
        does not have.
        """
        monkeypatch.setattr(validate, "SETTINGS", (_retired(REMOVED),))
        loaded = apply_status({"sync": {"sync_pdfs": True, "limit": 5}})
        assert loaded["sync"] == {"limit": 5}

    def test_dropping_a_key_leaves_the_section_and_the_original_alone(self, monkeypatch):
        """Emptying a section is not deleting it, and the parsed dict is the caller's."""
        monkeypatch.setattr(validate, "SETTINGS", (_retired(REMOVED),))
        original = {"sync": {"sync_pdfs": True}}
        loaded = apply_status(original)
        assert loaded["sync"] == {}
        assert original == {"sync": {"sync_pdfs": True}}

    def test_a_retired_key_stays_in_the_schema_so_it_stays_recognised(self):
        """Deleting the entry is what the status exists to avoid.

        An unrecognised key is a hard ERROR, so a key that disappears from
        :data:`SECTION_KEYS` stops every config still naming it from loading.
        """
        for setting in SETTINGS:
            if setting.status == REMOVED and setting.key is not None:
                assert SECTION_KEYS[setting.section][setting.leaf] is setting

    def test_only_the_live_settings_are_resolved(self):
        """What the validator reads and what the resolver reads are not the same set."""
        assert LIVE_SETTINGS == tuple(s for s in SETTINGS if s.status != REMOVED)
        assert all(s.status != REMOVED for s in LIVE_SETTINGS)
