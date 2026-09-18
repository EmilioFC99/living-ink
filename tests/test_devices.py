"""Tests for living_ink.devices — identifying the tablet from what it reports."""

import logging

from living_ink.devices import DEFAULT_PROFILE, DEVICE_PROFILES, profile_for


class TestKnownModels:
    """A device this project has seen is matched from its own machine string."""

    def test_a_remarkable_2_reports_itself_as_2_point_0(self):
        """The kernel says "reMarkable 2.0"; nobody writes the model that way."""
        assert profile_for("reMarkable 2.0").name == "reMarkable 2"

    def test_the_first_generation_calls_itself_a_prototype(self):
        assert profile_for("reMarkable Prototype 1").name == "reMarkable 1"

    def test_the_colour_device_is_not_read_as_a_remarkable_2(self):
        """Both hints can match one string, so the longer one has to win."""
        profile = profile_for("reMarkable Paper Pro")
        assert profile.name == "reMarkable Paper Pro"
        assert profile.color is True

    def test_matching_ignores_case(self):
        assert profile_for("REMARKABLE 2.0").name == "reMarkable 2"

    def test_only_the_colour_device_is_colour(self):
        greyscale = [p for p in DEVICE_PROFILES.values() if not p.color]
        assert len(greyscale) == 2


class TestUnknownModels:
    """An unrecognised device degrades to the old constants, but says so."""

    def test_it_falls_back_to_the_documented_default(self):
        assert profile_for("reMarkable 9 Ultra") is DEFAULT_PROFILE

    def test_an_empty_machine_string_is_not_a_crash(self):
        assert profile_for("") is DEFAULT_PROFILE

    def test_the_fallback_is_logged_so_a_bug_report_names_the_model(self, caplog):
        with caplog.at_level(logging.INFO, logger="living_ink.devices"):
            profile_for("reMarkable 9 Ultra")
        assert "reMarkable 9 Ultra" in caplog.text

    def test_the_default_is_what_the_code_assumed_before_profiles_existed(self):
        assert DEFAULT_PROFILE.screen == (1404, 1872)
        assert DEFAULT_PROFILE.color is False
