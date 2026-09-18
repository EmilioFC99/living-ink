"""Tests for credential masking in logs and error output."""

import logging

import pytest

from living_ink import redact as redact_mod
from living_ink.redact import MASK, SecretFilter, redact, register_secret

TOKEN = "rm-device-token-abcdef123456"
API_KEY = "AIzaSyExampleKeyForTesting1234"


@pytest.fixture(autouse=True)
def clean_registry():
    """Keep registrations from leaking between tests."""
    redact_mod.clear_secrets()
    yield
    redact_mod.clear_secrets()


class TestRegisterSecret:
    """Registration is forgiving so callers do not have to guard."""

    def test_a_registered_value_is_masked(self):
        register_secret(TOKEN)
        assert redact(f"token={TOKEN}") == f"token={MASK}"

    def test_registering_twice_is_harmless(self):
        register_secret(TOKEN)
        register_secret(TOKEN)
        assert redact(TOKEN) == MASK

    def test_none_is_ignored(self):
        register_secret(None)
        assert redact_mod.registered_secrets() == set()

    def test_empty_string_is_ignored(self):
        register_secret("")
        assert redact_mod.registered_secrets() == set()

    def test_a_short_value_is_ignored(self):
        """A three-character secret would match half the words in a log line."""
        register_secret("abc")
        assert redact("abc is a common substring") == "abc is a common substring"


class TestRedact:
    """Masking covers every occurrence without corrupting the rest."""

    def test_every_occurrence_is_masked(self):
        register_secret(TOKEN)
        assert redact(f"{TOKEN} and again {TOKEN}") == f"{MASK} and again {MASK}"

    def test_multiple_secrets_are_masked(self):
        register_secret(TOKEN)
        register_secret(API_KEY)
        cleaned = redact(f"key={API_KEY} token={TOKEN}")
        assert TOKEN not in cleaned
        assert API_KEY not in cleaned

    def test_a_secret_containing_another_leaves_no_fragment(self):
        """Longest-first replacement stops a partial match leaving a tail."""
        inner = "abcdefgh12345678"
        outer = inner + "-suffix-extra"
        register_secret(inner)
        register_secret(outer)
        assert redact(f"value={outer}") == f"value={MASK}"

    def test_surrounding_text_is_preserved(self):
        register_secret(TOKEN)
        assert redact(f"HTTP 400 for {TOKEN} at 10:32") == f"HTTP 400 for {MASK} at 10:32"

    def test_extra_values_are_masked_for_one_call_only(self):
        assert redact("key=" + API_KEY, extra=[API_KEY]) == f"key={MASK}"
        assert redact("key=" + API_KEY) == f"key={API_KEY}"

    def test_an_empty_string_is_returned_unchanged(self):
        register_secret(TOKEN)
        assert redact("") == ""

    def test_a_non_string_is_returned_unchanged(self):
        register_secret(TOKEN)
        assert redact(None) is None
        assert redact(42) == 42


class TestSecretFilter:
    """The filter masks records without dropping them."""

    def _record(self, msg, args=None):
        """Build a log record the way logging would."""
        return logging.LogRecord(
            name="living_ink.providers",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg=msg,
            args=args,
            exc_info=None,
        )

    def test_the_message_is_masked(self):
        register_secret(TOKEN)
        record = self._record(f"failed with {TOKEN}")
        assert SecretFilter().filter(record) is True
        assert TOKEN not in record.getMessage()

    def test_positional_arguments_are_masked(self):
        """The provider logs the response body as an argument, not in the message."""
        register_secret(API_KEY)
        record = self._record("Details: %s", (f'{{"echo": "{API_KEY}"}}',))
        SecretFilter().filter(record)
        assert API_KEY not in record.getMessage()
        assert MASK in record.getMessage()

    def test_dict_arguments_are_masked(self):
        register_secret(API_KEY)
        # logging receives a mapping wrapped in a tuple and unwraps it itself.
        record = self._record("Details: %(body)s", ({"body": API_KEY},))
        SecretFilter().filter(record)
        assert API_KEY not in record.getMessage()

    def test_non_string_arguments_survive(self):
        register_secret(TOKEN)
        record = self._record("status %s after %s attempts", (429, 3))
        SecretFilter().filter(record)
        assert record.getMessage() == "status 429 after 3 attempts"

    def test_records_are_never_dropped(self):
        record = self._record("nothing secret here")
        assert SecretFilter().filter(record) is True
