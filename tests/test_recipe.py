"""The other half of change detection: what makes a document pending.

``version`` covers the tablet. These cover everything else, and the tests are
written the way §20.3 F1 asks for them — one per input in the digest, each
asserting that changing it changes the answer. A false negative here is the
bug the whole column exists to close: the user edits a prompt, runs sync,
nothing happens, and they conclude the prompt does not work.
"""

import dataclasses
from typing import ClassVar, Tuple

import pytest

from living_ink.config import Setting, settings_for_section
from living_ink.config.schema import TEXT
from living_ink.core.recipe import document_recipe, settings_digest
from living_ink.destinations.base import Destination, DestinationStatus
from living_ink.destinations.obsidian import ObsidianDestination
from living_ink.settings import Settings
from living_ink.sources.base import SourceType, source_for_name


class Bare(Destination):
    """A destination whose output no setting changes."""

    state_key: ClassVar[str] = "Bare"
    display_name: ClassVar[str] = "Bare"

    def check(self) -> DestinationStatus:
        return DestinationStatus(ok=True, detail="ready")

    def publish(self, doc, ctx):  # pragma: no cover - never called here
        raise NotImplementedError


class Narrow(Bare):
    """A destination that reads exactly one setting."""

    state_key: ClassVar[str] = "Narrow"
    settings: ClassVar[Tuple[Setting, ...]] = (
        Setting(field="obsidian_root_folder", key="x.root", kind=TEXT, default="", help="h"),
    )


@pytest.fixture
def settings():
    """A resolved-looking Settings with every recipe input populated."""
    return Settings(
        ai_provider="openai",
        ai_model="gpt-4o-mini",
        obsidian_vault_path="/tmp/vault",
        obsidian_root_folder="Inbox",
    )


@pytest.fixture
def obsidian(settings):
    """A built Obsidian destination; only its class is read."""
    return ObsidianDestination.from_config({}, settings)


class TestSettingsDigestIsScopedToTheDestination:
    """Digesting the whole of Settings would be wrong, not merely wasteful."""

    def test_it_is_stable_for_unchanged_settings(self, obsidian, settings):
        assert settings_digest(obsidian, settings) == settings_digest(obsidian, settings)

    def test_a_setting_the_destination_reads_changes_it(self, obsidian, settings):
        moved = dataclasses.replace(settings, obsidian_root_folder="Archive")
        assert settings_digest(obsidian, moved) != settings_digest(obsidian, settings)

    def test_a_setting_it_does_not_read_leaves_it_alone(self, obsidian, settings):
        """Raising a threading knob must not rewrite the vault."""
        busier = dataclasses.replace(settings, ocr_concurrency=settings.ocr_concurrency + 4)
        assert settings_digest(obsidian, busier) == settings_digest(obsidian, settings)

    def test_a_destination_declaring_nothing_still_digests(self, settings):
        assert settings_digest(Bare(), settings) == settings_digest(Bare(), settings)

    def test_two_destinations_reading_different_settings_disagree(self, obsidian, settings):
        assert settings_digest(Narrow(), settings) != settings_digest(obsidian, settings)

    def test_only_the_declared_setting_moves_a_narrow_destination(self, settings):
        before = settings_digest(Narrow(), settings)
        unread = dataclasses.replace(settings, obsidian_vault_path="/tmp/elsewhere")
        read = dataclasses.replace(settings, obsidian_root_folder="Archive")

        assert settings_digest(Narrow(), unread) == before
        assert settings_digest(Narrow(), read) != before

    def test_the_declaration_order_does_not_matter(self, settings):
        """Moving a Setting up the schema file must not re-publish anything."""

        class Reversed(Bare):
            state_key: ClassVar[str] = "Reversed"
            settings: ClassVar[Tuple[Setting, ...]] = tuple(
                reversed(settings_for_section("obsidian"))
            )

        class Forward(Bare):
            state_key: ClassVar[str] = "Forward"
            settings: ClassVar[Tuple[Setting, ...]] = settings_for_section("obsidian")

        assert settings_digest(Reversed(), settings) == settings_digest(Forward(), settings)


class TestTheRecipeCoversEveryInput:
    """One test per input, because a missed one is a silent false negative."""

    def test_it_is_stable_when_nothing_changed(self, obsidian, settings):
        source = source_for_name("notebook")
        assert document_recipe(source, obsidian, settings) == document_recipe(
            source, obsidian, settings
        )

    @pytest.mark.parametrize(
        "field, value",
        [
            ("ai_provider", "gemini"),
            ("ai_model", "gpt-4o"),
            ("ai_temperature", 0.9),
            ("ai_language", "fr"),
            ("obsidian_root_folder", "Archive"),
            ("obsidian_mirror_folders", False),
        ],
    )
    def test_changing_a_setting_changes_it(self, obsidian, settings, field, value):
        source = source_for_name("notebook")
        before = document_recipe(source, obsidian, settings)
        after = dataclasses.replace(settings, **{field: value})
        assert document_recipe(source, obsidian, after) != before

    def test_editing_a_prompt_changes_it(self, obsidian, settings, tmp_path, monkeypatch):
        from living_ink import clean

        prompt = tmp_path / "ocr_prompt.txt"
        prompt.write_text("Transcribe this page.")
        monkeypatch.setattr(clean, "OCR_PROMPT_FILE", prompt)

        source = source_for_name("notebook")
        before = document_recipe(source, obsidian, settings)
        prompt.write_text("Transcribe this page, but in French.")
        assert document_recipe(source, obsidian, settings) != before

    def test_bumping_a_renderer_changes_it(self, obsidian, settings):
        source = source_for_name("notebook")
        before = document_recipe(source, obsidian, settings)
        bumped = dataclasses.replace(
            source, renderer=_VersionedRenderer(source.renderer, source.renderer.version + 1)
        )
        assert document_recipe(bumped, obsidian, settings) != before

    def test_each_source_type_gets_its_own(self, obsidian, settings):
        """One recipe per run would bake one renderer's version into all three."""
        recipes = {
            name: document_recipe(source_for_name(name), obsidian, settings)
            for name in ("notebook", "pdf", "epub")
        }
        assert len(set(recipes.values())) == 3

    def test_each_destination_gets_its_own(self, obsidian, settings):
        source = source_for_name("notebook")
        assert document_recipe(source, Narrow(), settings) != document_recipe(
            source, obsidian, settings
        )


class TestTheRecipeIsComputableBeforeTheWork:
    """It is a gate, so every input has to be knowable before the gate."""

    def test_it_builds_no_provider(self, obsidian, settings, monkeypatch):
        from living_ink import clean

        def explode(*args, **kwargs):
            raise AssertionError("document_recipe must not construct a provider")

        monkeypatch.setattr(clean, "get_provider", explode)
        monkeypatch.setattr(clean, "_provider", None)
        assert document_recipe(source_for_name("notebook"), obsidian, settings)

    def test_it_is_short_enough_to_print(self, obsidian, settings):
        assert len(document_recipe(source_for_name("notebook"), obsidian, settings)) == 16


class _VersionedRenderer:
    """Wraps a renderer to report a different version, for the bump test."""

    def __init__(self, inner, version):
        self._inner = inner
        self.version = version

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_a_source_type_is_a_frozen_dataclass():
    """The renderer-bump test replaces one; it would silently pass on a mutable."""
    assert dataclasses.is_dataclass(SourceType)
    with pytest.raises(dataclasses.FrozenInstanceError):
        source_for_name("notebook").name = "other"
