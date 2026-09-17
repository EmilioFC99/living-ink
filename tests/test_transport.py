"""Tests for the living_ink.transport seam."""

import pytest

from living_ink.api import FallbackClient
from living_ink.ssh import SSHClient
from living_ink.sync import RemarkableClient
from living_ink.transport import RemarkableTransport, UnsupportedOperation


@pytest.mark.parametrize("cls", [SSHClient, RemarkableClient, FallbackClient])
def test_shipped_clients_satisfy_the_protocol(cls):
    """Every shipped transport implements the full RemarkableTransport surface."""
    required = [name for name in vars(RemarkableTransport) if not name.startswith("_")]
    assert required, "the protocol should declare at least one method"
    missing = [name for name in required if not callable(getattr(cls, name, None))]
    assert missing == [], f"{cls.__name__} is missing {missing}"


def test_unsupported_operation_is_a_not_implemented_error():
    """Callers can catch UnsupportedOperation as the stdlib error it specialises."""
    assert issubclass(UnsupportedOperation, NotImplementedError)
