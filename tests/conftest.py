"""Suite-wide fixtures.

The one thing in here is not a convenience: it is a guard. Several code paths
read and write credentials under the home directory (``~/.rmapi``, the config
directory), and a test that exercises them against the *real* home directory
does not fail loudly — it silently destroys the developer's own working setup.
That happened: a test setting ``REMARKABLE_TOKEN=cloud-token`` overwrote a real
reMarkable token with the literal string ``cloud-token``. Redirecting the home
directory for every test makes that class of accident impossible rather than
something each new test has to remember to avoid.
"""

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory, monkeypatch):
    """Point the home directory at a throwaway path for every test.

    ``Path.home()`` resolves through ``$HOME`` on macOS and Linux, so setting
    the variable covers every caller without patching ``pathlib``. The fake
    home gets its own directory rather than living inside ``tmp_path``, because
    tests that walk ``tmp_path`` (vault scans, stray-temp-file checks) would
    otherwise find it and fail.

    Args:
        tmp_path_factory: Pytest's temporary directory factory.
        monkeypatch: Pytest's environment patcher.

    Yields:
        The directory standing in for the user's home.
    """
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    yield home
