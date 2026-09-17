"""Tests for the destination registry and config-driven destination building."""

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from living_ink.destinations import (
    DESTINATION_REGISTRY,
    AppleNotesDestination,
    Destination,
    ObsidianDestination,
    build_destinations,
    register_destination,
)
from living_ink.settings import Settings

SETTINGS = Settings.resolve(config={}, env={})


@pytest.fixture
def clean_registry():
    """Restore the registry after a test registers something into it."""
    original = dict(DESTINATION_REGISTRY)
    yield DESTINATION_REGISTRY
    DESTINATION_REGISTRY.clear()
    DESTINATION_REGISTRY.update(original)


def build(config, settings=SETTINGS) -> List[Destination]:
    """Build destinations from a config dict."""
    return build_destinations(config, settings)


class TestShippedDestinations:
    """The two destinations that ship with Living Ink are registered."""

    def test_both_are_registered_under_their_config_sections(self):
        assert DESTINATION_REGISTRY["apple_notes"] is AppleNotesDestination
        assert DESTINATION_REGISTRY["obsidian"] is ObsidianDestination

    def test_registration_records_the_config_key(self):
        assert AppleNotesDestination.config_key == "apple_notes"
        assert ObsidianDestination.config_key == "obsidian"

    def test_apple_notes_is_on_by_default_and_obsidian_is_not(self):
        built = build({})
        assert [type(d) for d in built] == [AppleNotesDestination]

    def test_apple_notes_folder_comes_from_settings(self):
        settings = Settings.resolve(config={"apple_notes": {"folder_name": "Ideas"}}, env={})
        (dest,) = build({"apple_notes": {}}, settings)
        assert dest.folder_name == "Ideas"

    def test_apple_notes_can_be_disabled(self):
        assert build({"apple_notes": {"enabled": False}}) == []

    def test_obsidian_is_built_when_enabled_with_a_vault(self, tmp_path):
        config = {
            "apple_notes": {"enabled": False},
            "obsidian": {"enabled": True, "vault_path": str(tmp_path), "root_folder": "Ink"},
        }
        (dest,) = build(config)
        assert isinstance(dest, ObsidianDestination)
        assert dest.vault_path == tmp_path.resolve()
        assert dest.root_folder == "Ink"

    def test_obsidian_without_a_vault_is_skipped_not_fatal(self, capsys):
        built = build({"apple_notes": {"enabled": False}, "obsidian": {"enabled": True}})
        assert built == []
        assert "vault_path" in capsys.readouterr().out

    def test_an_unusable_vault_skips_only_that_destination(self, capsys):
        config = {"obsidian": {"enabled": True, "vault_path": "/nope/does/not/exist"}}
        built = build(config)

        assert [type(d) for d in built] == [AppleNotesDestination]
        assert "obsidian" in capsys.readouterr().out.lower()

    def test_describe_names_the_setting_that_matters(self, tmp_path):
        assert "Ideas" in AppleNotesDestination(folder_name="Ideas").describe()
        assert str(tmp_path.resolve()) in ObsidianDestination(vault_path=str(tmp_path)).describe()


class TestLegacyDestinationKey:
    """The old single 'destination' key still selects exactly one destination."""

    def test_string_form_disables_the_others(self):
        assert build({"destination": "obsidian"}) == []

    def test_string_form_naming_apple_notes_keeps_it(self):
        assert [type(d) for d in build({"destination": "apple_notes"})] == [AppleNotesDestination]

    def test_dict_form_enables_and_configures(self, tmp_path):
        config = {"destination": {"type": "obsidian", "vault_path": str(tmp_path)}}
        built = build(config)

        assert [type(d) for d in built] == [ObsidianDestination]
        assert built[0].vault_path == tmp_path.resolve()

    def test_dict_form_overrides_a_disabled_section(self, tmp_path):
        config = {
            "obsidian": {"enabled": False, "vault_path": str(tmp_path)},
            "destination": {"type": "obsidian"},
        }
        assert [type(d) for d in build(config)] == [ObsidianDestination]

    def test_unrecognized_legacy_value_is_ignored(self):
        assert [type(d) for d in build({"destination": 42})] == [AppleNotesDestination]


class TestAddingADestination:
    """A new destination needs nothing but a class and the decorator."""

    def test_registered_subclass_is_built_from_its_own_section(self, clean_registry):
        @register_destination("notion")
        class NotionDestination(Destination):
            def __init__(self, database_id: str) -> None:
                self.database_id = database_id

            @classmethod
            def from_config(
                cls, section: Dict[str, Any], settings: Settings
            ) -> Optional[Destination]:
                return cls(database_id=section["database_id"])

            def publish(
                self,
                notebook_name: str,
                text_content: str,
                image_paths: List[Path],
                sub_folder: Optional[str] = None,
                document_path: Optional[Path] = None,
                tags: Optional[List[str]] = None,
            ) -> bool:
                return True

        config = {
            "apple_notes": {"enabled": False},
            "notion": {"enabled": True, "database_id": "db-1"},
        }
        (dest,) = build(config)

        assert isinstance(dest, NotionDestination)
        assert dest.database_id == "db-1"

    def test_registered_subclass_is_off_unless_enabled(self, clean_registry):
        @register_destination("ghost")
        class GhostDestination(AppleNotesDestination):
            pass

        assert [type(d) for d in build({"apple_notes": {"enabled": False}})] == []

    def test_default_describe_falls_back_to_the_config_key(self, clean_registry):
        @register_destination("plain", enabled_by_default=True)
        class PlainDestination(AppleNotesDestination):
            pass

        assert PlainDestination(folder_name="x").config_key == "plain"
        assert Destination.describe(PlainDestination(folder_name="x")) == "plain"
