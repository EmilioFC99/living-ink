"""Tests for the destination registry and config-driven destination building.

1.0 ships one destination, so every test that needs a second one registers
:class:`tests.fakes.FakeApiDestination` behind the ``clean_registry`` fixture
rather than reaching for whichever real destination happens to exist. That was
what the Apple Notes tests did, and it meant deleting a destination broke the
registry's own contract tests.
"""

from typing import Any, Dict, List, Optional

import pytest

from living_ink.config import apply_status
from living_ink.destinations import (
    DESTINATION_REGISTRY,
    Destination,
    DestinationStatus,
    MergeUnit,
    ObsidianDestination,
    build_destinations,
    register_destination,
)
from living_ink.settings import Settings
from tests.fakes import FakeApiDestination


@pytest.fixture
def clean_registry():
    """Restore the registry after a test registers something into it."""
    original = dict(DESTINATION_REGISTRY)
    yield DESTINATION_REGISTRY
    DESTINATION_REGISTRY.clear()
    DESTINATION_REGISTRY.update(original)


def build(config, settings=None) -> List[Destination]:
    """Build destinations from a config dict, the way the pipeline does.

    The pipeline hands ``build_destinations`` a config that has been through
    :func:`apply_status` and the settings resolved from that same config, so
    the helper does both: a destination now reads its values from the settings,
    and resolving them from somewhere else would test a wiring that never runs.
    """
    prepared = apply_status(config)
    return build_destinations(prepared, settings or Settings.resolve(prepared, env={}))


class TestTheShippedDestination:
    """Obsidian is the only destination 1.0 registers."""

    def test_it_is_registered_under_its_config_section(self):
        assert DESTINATION_REGISTRY["obsidian"] is ObsidianDestination

    def test_it_is_the_only_one(self):
        assert list(DESTINATION_REGISTRY) == ["obsidian"]

    def test_registration_records_the_config_key(self):
        assert ObsidianDestination.config_key == "obsidian"

    def test_it_is_on_without_being_asked_for(self, tmp_path):
        """Being the only destination, it cannot be the one you opt into.

        Apple Notes was the default and Obsidian was opt-in; leaving it that
        way would make an untouched config publish nowhere at all.
        """
        built = build({"obsidian": {"vault_path": str(tmp_path)}})

        assert [type(d) for d in built] == [ObsidianDestination]

    def test_it_can_be_disabled(self, tmp_path):
        config = {"obsidian": {"enabled": False, "vault_path": str(tmp_path)}}

        assert build(config) == []

    def test_it_is_built_when_enabled_with_a_vault(self, tmp_path):
        config = {
            "obsidian": {"enabled": True, "vault_path": str(tmp_path), "root_folder": "Ink"},
        }
        (dest,) = build(config)
        assert isinstance(dest, ObsidianDestination)
        assert dest.vault_path == tmp_path.resolve()
        assert dest.root_folder == "Ink"

    def test_without_a_vault_it_is_skipped_not_fatal(self, capsys):
        built = build({"obsidian": {"enabled": True}})
        assert built == []
        assert "vault_path" in capsys.readouterr().err

    def test_an_unusable_vault_is_built_anyway_and_fails_its_check(self):
        """Building it is what lets preflight say which vault is wrong."""
        config = {"obsidian": {"enabled": True, "vault_path": "/nope/does/not/exist"}}
        built = build(config)

        assert [type(d) for d in built] == [ObsidianDestination]
        assert built[0].check().ok is False

    def test_it_reads_its_vault_from_the_environment(self, tmp_path):
        config = {"obsidian": {"enabled": True}}
        settings = Settings.resolve(
            config=config, env={"LIVING_INK_OBSIDIAN_VAULT_PATH": str(tmp_path)}
        )
        (dest,) = build(config, settings)
        assert dest.vault_path == tmp_path.resolve()

    def test_an_environment_vault_outranks_the_file(self, tmp_path):
        chosen = tmp_path / "chosen"
        chosen.mkdir()
        config = {"obsidian": {"enabled": True, "vault_path": str(tmp_path)}}
        settings = Settings.resolve(
            config=config, env={"LIVING_INK_OBSIDIAN_VAULT_PATH": str(chosen)}
        )
        (dest,) = build(config, settings)
        assert dest.vault_path == chosen.resolve()

    def test_describe_names_the_setting_that_matters(self, tmp_path):
        assert str(tmp_path.resolve()) in ObsidianDestination(vault_path=str(tmp_path)).describe()


class TestLegacyDestinationKey:
    """The old single 'destination' key still selects exactly one destination."""

    def test_a_string_naming_a_destination_that_is_gone_publishes_nowhere(self, tmp_path):
        """An 0.x config that chose Apple Notes must not quietly become Obsidian.

        The key means "this one and no other". No other still holds after the
        one it named was deleted, so the run has no destination and preflight
        refuses — which is the honest answer, not a silent substitution.
        """
        config = {"destination": "apple_notes", "obsidian": {"vault_path": str(tmp_path)}}

        assert build(config) == []

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

    def test_dict_form_carries_the_rest_of_the_obsidian_settings(self, tmp_path):
        config = {
            "destination": {
                "type": "obsidian",
                "vault_path": str(tmp_path),
                "root_folder": "Ink",
                "attachments_folder": "",
                "mirror_folders": False,
            }
        }
        (dest,) = build(config)

        assert dest.root_folder == "Ink"
        assert dest.attachments_folder == ""
        assert dest.mirror_folders is False

    def test_unrecognized_legacy_value_is_ignored(self, tmp_path):
        config = {"destination": 42, "obsidian": {"vault_path": str(tmp_path)}}

        assert [type(d) for d in build(config)] == [ObsidianDestination]


class TestAddingADestination:
    """A new destination needs nothing but a class and the decorator."""

    def test_registered_subclass_is_built_from_its_own_section(self, clean_registry):
        @register_destination("notion")
        class NotionDestination(Destination):
            state_key = "NotionDestination"

            def __init__(self, database_id: str) -> None:
                self.database_id = database_id

            @classmethod
            def from_config(
                cls, section: Dict[str, Any], settings: Settings
            ) -> Optional[Destination]:
                return cls(database_id=section["database_id"])

            def check(self) -> DestinationStatus:
                return DestinationStatus(ok=True, detail="ready")

            def publish(self, doc, ctx):
                return True

        config = {"notion": {"enabled": True, "database_id": "db-1"}}
        (dest,) = build(config)

        assert isinstance(dest, NotionDestination)
        assert dest.database_id == "db-1"

    def test_registered_subclass_is_off_unless_enabled(self, clean_registry):
        @register_destination("ghost")
        class GhostDestination(FakeApiDestination):
            state_key = "GhostDestination"

        assert build({}) == []

    def test_default_describe_falls_back_to_the_config_key(self, clean_registry):
        @register_destination("plain", enabled_by_default=True)
        class PlainDestination(FakeApiDestination):
            state_key = "PlainDestination"

        assert PlainDestination().config_key == "plain"
        assert Destination.describe(PlainDestination()) == "plain"


class TestTheStateKeyIsDeclared:
    """Sync state is filed under a name the class states, never under its own.

    It used to be ``type(dest).__name__``, which made renaming a class a silent
    data migration: every document looks unpublished, every note is rewritten,
    and every ``first_published`` date restarts at today.
    """

    def test_the_shipped_key_is_the_name_already_in_state_db(self):
        assert ObsidianDestination.state_key == "ObsidianDestination"

    def test_a_destination_without_one_cannot_register(self, clean_registry):
        with pytest.raises(TypeError, match="state_key"):

            @register_destination("keyless")
            class KeylessDestination(Destination):
                def publish(self, *args, **kwargs):
                    return None

    def test_an_inherited_key_does_not_count(self, clean_registry):
        """Two destinations sharing a key would share each other's rows."""
        with pytest.raises(TypeError, match="state_key"):

            @register_destination("borrowed")
            class BorrowedDestination(FakeApiDestination):
                pass

    def test_the_display_name_defaults_to_the_section(self, clean_registry):
        @register_destination("notion")
        class NotionDestination(FakeApiDestination):
            state_key = "NotionDestination"

        assert NotionDestination.display_name == "notion"

    def test_the_shipped_display_name_is_what_a_user_calls_it(self):
        assert ObsidianDestination.display_name == "Obsidian"


class TestMergeUnit:
    """How much of an existing note a destination rewrites is declared, not guessed."""

    def test_obsidian_rewrites_one_page_at_a_time(self):
        """A marked block per page, so the note under a page is left alone."""
        assert ObsidianDestination.merge_unit is MergeUnit.PAGE

    def test_the_default_is_the_assumption_that_is_never_unsafe(self, clean_registry):
        @register_destination("quiet")
        class QuietDestination(FakeApiDestination):
            state_key = "QuietDestination"

        assert QuietDestination.merge_unit is MergeUnit.DOCUMENT

    def test_it_is_a_string_so_it_survives_json(self):
        assert MergeUnit.PAGE == "page"
        assert MergeUnit.DOCUMENT == "document"
