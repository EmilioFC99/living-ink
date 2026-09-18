"""Tests for living_ink.devices — identifying the tablet from what it reports."""

import logging
import re
from unittest.mock import MagicMock

import pytest

from living_ink.devices import (
    DEFAULT_PROFILE,
    DEVICE_PROFILES,
    SOURCE_DEFAULT,
    SOURCE_REMEMBERED,
    SOURCE_USB,
    profile_for,
    resolve_device,
)
from living_ink.transport import DeviceInfo, UnsupportedOperation


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


class TestRememberingTheDevice:
    """One USB session has to be enough for every Cloud-only run after it."""

    @pytest.fixture
    def store(self, tmp_path):
        """Open a throwaway state store.

        Args:
            tmp_path: Pytest temporary directory.

        Returns:
            An empty StateStore.
        """
        from living_ink.state import StateStore

        return StateStore(tmp_path / "state.db")

    @staticmethod
    def _transport(result):
        """Build a transport stub whose get_device_info returns or raises.

        Args:
            result: A DeviceInfo to return, or an exception to raise.

        Returns:
            An object satisfying the one method resolve_device calls.
        """
        stub = MagicMock()
        if isinstance(result, Exception):
            stub.get_device_info.side_effect = result
        else:
            stub.get_device_info.return_value = result
        return stub

    def test_nothing_known_yet_is_the_named_default(self, store):
        reading = resolve_device(None, store)
        assert reading.source == SOURCE_DEFAULT
        assert reading.info.screen == DEFAULT_PROFILE.screen
        assert "assumed" in reading.describe()

    def test_a_live_reading_wins_and_is_banked(self, store):
        info = DeviceInfo("reMarkable Paper Pro", "3.20.0", (1620, 2160), color=True)
        reading = resolve_device(self._transport(info), store)

        assert reading.source == SOURCE_USB
        assert reading.describe() == info.describe()
        assert store.recall_device()[0] == info

    def test_the_memory_answers_when_the_cable_is_gone(self, store):
        info = DeviceInfo("reMarkable Paper Pro", "3.20.0", (1620, 2160), color=True)
        resolve_device(self._transport(info), store)

        reading = resolve_device(None, store)

        assert reading.source == SOURCE_REMEMBERED
        assert reading.info == info
        assert "remembered from USB" in reading.describe()
        assert reading.learned_at

    def test_a_cloud_transport_falls_through_to_the_memory(self, store):
        info = DeviceInfo("reMarkable 1", "2.15.1", (1404, 1872))
        store.remember_device(info)

        reading = resolve_device(self._transport(UnsupportedOperation("cloud")), store)

        assert reading.source == SOURCE_REMEMBERED
        assert reading.info == info

    def test_a_later_reading_replaces_an_earlier_one(self, store):
        store.remember_device(DeviceInfo("reMarkable 1", "2.15.1", (1404, 1872)))
        newer = DeviceInfo("reMarkable 2", "3.20.0", (1404, 1872))

        resolve_device(self._transport(newer), store)

        assert store.recall_device()[0] == newer

    def test_a_failed_probe_is_not_a_crash(self, store):
        reading = resolve_device(self._transport(RuntimeError("no route")), store)
        assert reading.source == SOURCE_DEFAULT

    def test_a_transport_answering_with_junk_is_not_written_to_the_store(self, store):
        reading = resolve_device(self._transport("not a DeviceInfo"), store)

        assert reading.source == SOURCE_DEFAULT
        assert store.recall_device() is None

    def test_no_store_at_all_still_resolves(self):
        info = DeviceInfo("reMarkable 2", "3.20.0", (1404, 1872))
        assert resolve_device(self._transport(info), None).source == SOURCE_USB
        assert resolve_device(None, None).source == SOURCE_DEFAULT

    def test_a_remembered_reading_shows_the_date_it_was_taken(self, store):
        store.remember_device(DeviceInfo("reMarkable 2", "3.20.0", (1404, 1872)))

        described = resolve_device(None, store).describe()

        assert re.search(r"remembered from USB, \d{4}-\d{2}-\d{2}\)$", described)
