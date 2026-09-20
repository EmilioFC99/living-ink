"""Tests for the one serialiser both writing commands share.

``setup`` and ``config`` are the two places a ``config.yml`` is written, and
they used to disagree: the wizard had a hand-numbered writer that knew the eight
keys it asked about, so a menu able to change any of thirty-eight settings would
have needed a second one. What is pinned here is therefore mostly about *not
losing things* — an unknown section, a deliberate empty string, the order the
schema declares — because a serialiser that drops something is a save that
quietly deletes a user's decision.
"""

from typing import Any, Dict

import pytest
import yaml

from living_ink.config.schema import ACTIVE, SECTIONS
from living_ink.config.writer import HEADER, render_config


def parsed(values: Dict[str, Any]) -> Dict[str, Any]:
    """Render a mapping and read it straight back.

    Args:
        values: The configuration to serialise.

    Returns:
        What a loader would see.
    """
    return yaml.safe_load(render_config(values))


class TestTheFileExplainsItself:
    """A config found on disk in a year has to say what put it there."""

    def test_it_opens_with_the_header(self):
        rendered = render_config({"ai": {"model": "m"}})
        assert rendered.startswith("\n".join(HEADER))

    def test_it_ends_with_exactly_one_newline(self):
        rendered = render_config({"ai": {"model": "m"}})
        assert rendered.endswith("\n")
        assert not rendered.endswith("\n\n")

    def test_a_section_carries_the_help_the_schema_declares(self):
        rendered = render_config({"obsidian": {"root_folder": "Notes"}})
        assert f"# {SECTIONS['obsidian'].help}" in rendered

    def test_the_schema_version_is_labelled_as_not_for_editing(self):
        rendered = render_config({"schema_version": 1})
        assert "# Config format version — do not edit." in rendered
        assert rendered.index("schema_version") < len(rendered)

    def test_the_schema_version_comes_before_any_section(self):
        rendered = render_config({"ai": {"model": "m"}, "schema_version": 1})
        assert rendered.index("schema_version") < rendered.index("ai:")

    def test_every_comment_line_is_a_comment(self):
        """A help string with a newline in it would produce a broken file."""
        rendered = render_config({name: {"x": 1} for name in SECTIONS})
        yaml.safe_load(rendered)


class TestOrderComesFromTheSchema:
    """Two saves of the same settings must produce the same file."""

    def test_sections_are_written_in_declaration_order(self):
        rendered = render_config(
            {"watch": {"enabled": True}, "ai": {"model": "m"}, "sync": {"limit": 1}}
        )
        positions = [rendered.index(f"{name}:") for name in ("ai", "sync", "watch")]
        assert positions == sorted(positions)

    def test_the_insertion_order_of_the_mapping_does_not_leak(self):
        first = render_config({"watch": {"enabled": True}, "ai": {"model": "m"}})
        second = render_config({"ai": {"model": "m"}, "watch": {"enabled": True}})
        assert first == second

    def test_keys_inside_a_section_keep_the_order_they_were_given(self):
        rendered = render_config({"ssh": {}, "obsidian": {"vault_path": "/v", "root_folder": "N"}})
        assert rendered.index("vault_path") < rendered.index("root_folder")


class TestWhatIsLeftOut:
    """A key absent from the file is a key at its default, and that is the point."""

    def test_an_empty_section_is_dropped(self):
        assert "watch:" not in render_config({"watch": {}, "ai": {"model": "m"}})

    def test_a_section_the_schema_does_not_have_is_not_invented(self):
        rendered = render_config({"ai": {"model": "m"}})
        assert "obsidian:" not in rendered
        assert "sync:" not in rendered

    def test_an_empty_mapping_still_produces_a_readable_file(self):
        rendered = render_config({})
        assert yaml.safe_load(rendered) is None
        assert rendered.startswith("# Living Ink configuration")


class TestWhatIsKept:
    """A save is not a way to lose something the writer did not recognise."""

    def test_a_plugin_destination_section_survives(self):
        values = {"ai": {"model": "m"}, "notion": {"token_name": "work", "database": "abc"}}
        assert parsed(values)["notion"] == {"token_name": "work", "database": "abc"}

    def test_it_says_that_it_did_not_understand_them(self):
        rendered = render_config({"notion": {"token_name": "work"}})
        assert "# Not part of the Living Ink schema; preserved as found." in rendered

    def test_a_retired_section_is_preserved_rather_than_deleted(self):
        """``apple_notes:`` is REMOVED, so the loader ignores it — but deleting
        it on the user's behalf is a different thing from ignoring it, and a
        downgrade would need it back."""
        retired = next(n for n, s in SECTIONS.items() if s.status != ACTIVE)
        assert parsed({retired: {"kept": True}})[retired] == {"kept": True}

    def test_a_top_level_scalar_the_schema_does_not_know_survives(self):
        assert parsed({"experimental": True, "ai": {"model": "m"}})["experimental"] is True

    def test_a_blank_string_is_a_value_and_not_an_omission(self):
        """``obsidian.attachments_folder: ""`` means "beside the note"."""
        assert parsed({"obsidian": {"attachments_folder": ""}})["obsidian"] == {
            "attachments_folder": ""
        }

    def test_false_and_zero_are_written(self):
        result = parsed({"sync": {"prune": False, "limit": 0}})
        assert result["sync"] == {"prune": False, "limit": 0}


class TestValuesRoundTrip:
    """Serialising through PyYAML rather than interpolation is why these work."""

    @pytest.mark.parametrize(
        "value",
        [
            "/Users/me/Obsidian: My Vault",
            "C:\\Users\\me\\vault",
            'a "quoted" folder',
            "  leading and trailing  ",
            "Notas de reuniõe s — 日本語",
            "#not-a-comment",
            "- not a list",
            "true",
            "null",
        ],
    )
    def test_an_awkward_string_comes_back_as_itself(self, value):
        assert parsed({"obsidian": {"root_folder": value}})["obsidian"]["root_folder"] == value

    def test_unicode_is_written_literally_rather_than_escaped(self):
        rendered = render_config({"obsidian": {"root_folder": "日本語"}})
        assert "日本語" in rendered

    def test_a_tuple_is_written_as_a_list(self):
        """The menu holds list settings as tuples; YAML has no such thing."""
        assert parsed({"sync": {"types": ("notebook", "pdf")}})["sync"]["types"] == [
            "notebook",
            "pdf",
        ]

    def test_a_number_stays_a_number(self):
        result = parsed({"sync": {"limit": 7}, "ai": {"temperature": 0.25}})
        assert isinstance(result["sync"]["limit"], int)
        assert isinstance(result["ai"]["temperature"], float)

    def test_the_whole_mapping_round_trips(self):
        values = {
            "schema_version": 1,
            "ai": {"provider": "gemini", "model": "gemini-2.0-flash", "temperature": 0.1},
            "obsidian": {"vault_path": "/tmp/v", "attachments_folder": ""},
            "sync": {"types": ["notebook"], "limit": 3, "prune": False},
            "notion": {"token_name": "work"},
        }
        assert parsed(values) == values


class TestWhatItWritesIsWhatTheValidatorAccepts:
    """The two halves of the schema must agree, or a save produces a warning."""

    def test_a_rendered_config_validates_clean(self):
        from living_ink.config.validate import validate_config

        values = {
            "schema_version": 1,
            "ai": {"provider": "gemini", "model": "gemini-2.0-flash"},
            "obsidian": {"enabled": True, "vault_path": "/tmp/v"},
            "sync": {"types": ["notebook"], "limit": 3},
            "remarkable": {"preferred_connection": "ssh"},
        }
        assert validate_config(yaml.safe_load(render_config(values))) == []
