"""Suite-wide fixtures.

The one thing in here is not a convenience: it is a guard. Several code paths
read and write credentials under the home directory (``~/.rmapi``, the config
directory), and a test that exercises them against the *real* home directory
does not fail loudly — it silently destroys the developer's own working setup.
That happened: a test setting ``REMARKABLE_TOKEN=cloud-token`` overwrote a real
reMarkable token with the literal string ``cloud-token``. Redirecting the home
directory for every test makes that class of accident impossible rather than
something each new test has to remember to avoid. The runtime data directory
gets the same treatment, for the same reason.
"""

import logging
import os
import tempfile
from pathlib import Path

import pytest

# Set before anything imports the package: ``pipeline.DATA_DIR`` is resolved at
# module load, so a fixture would run too late and the suite would read and
# write the developer's own ``state.db``. That is not hypothetical — a status
# test with a mocked SSH client wrote a fake tablet into the real device table.
os.environ["LIVING_INK_DATA_DIR"] = tempfile.mkdtemp(prefix="living-ink-tests-")

# Captured here, at collection, because ``isolated_home`` below redirects
# ``$HOME`` for every test — by the time a fixture body runs, ``Path.home()``
# points at a throwaway directory. The tier-2 device capture lives under the
# developer's real home and is the one thing that has to find it.
REAL_HOME = os.path.expanduser("~")

from living_ink import logs  # noqa: E402 - must follow the env var above


@pytest.fixture(autouse=True)
def pristine_logging():
    """Undo any logging configuration a test leaves behind.

    ``logs.configure()`` is global: it installs handlers on the package logger
    and sets ``propagate = False`` so an embedding application's root logger
    does not get our output. Both outlive the test that called it, which makes
    a ``caplog`` assertion pass or fail depending on what ran before it — the
    kind of failure that only shows up on CI, in a different test order.

    Yields:
        None. The teardown is the point.
    """
    yield
    logs.reset_handlers()
    logging.getLogger(logs.PACKAGE_LOGGER).propagate = True
    logs._console_mode = logs.ConsoleMode.PLAIN


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory, monkeypatch):
    """Point the home directory at a throwaway path for every test.

    ``Path.home()`` resolves through ``$HOME`` on macOS and Linux, so setting
    the variable covers every caller without patching ``pathlib``. The fake
    home gets its own directory rather than living inside ``tmp_path``, because
    tests that walk ``tmp_path`` (vault scans, stray-temp-file checks) would
    otherwise find it and fail.

    ``$HOME`` alone is not enough. ``get_config_path()`` prefers
    ``$XDG_CONFIG_HOME``, which a Linux session sets and macOS does not — so on
    CI the credentials directory resolved to a real, *shared* path that the
    redirected home never covered. Every test that stored a secret then wrote
    it there, and the next test read the previous test's token. The XDG
    variables are redirected here so the two platforms isolate identically.
    The same goes for ``LIVING_INK_CONFIG``: a developer with one exported
    would otherwise have the suite read their own profile.

    Args:
        tmp_path_factory: Pytest's temporary directory factory.
        monkeypatch: Pytest's environment patcher.

    Yields:
        The directory standing in for the user's home.
    """
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.delenv("LIVING_INK_CONFIG", raising=False)
    monkeypatch.delenv("LIVING_INK_CONFIG_DIR", raising=False)
    yield home


@pytest.fixture(autouse=True)
def completions_stay_home(monkeypatch):
    """Keep the completion installer inside the fake home, and out of real shells.

    Three things in :mod:`living_ink.setup_wizard` reach past a test on
    purpose, and ``isolated_home`` does not cover any of them.
    ``completion_dirs`` names ``/opt/homebrew/share/zsh/site-functions``,
    which on a developer's Mac exists and is writable — so any test that
    reaches the wizard's commit step installs into the machine running the
    suite. ``completion_is_live`` spawns the user's *interactive* shell, which
    sources their entire rc: correct in production, and a minute of wall clock
    in a fifteen-second suite. And ``detect_shell`` reads ``$SHELL``, so the
    same test takes a different branch on a laptop and on CI.

    All three are pinned here. The directory list is *filtered* rather than
    replaced, so the candidates a test sees are the real ones minus the system
    paths — a shell whose only home-relative fallback is removed would break
    this fixture rather than quietly pass. Tests that want the real functions
    patch them back, which is what ``tests/test_completions_install.py`` does.

    Args:
        monkeypatch: Pytest's environment and attribute patcher.
    """
    from living_ink import setup_wizard

    real_dirs = setup_wizard.completion_dirs

    def under_the_fake_home(shell: str):
        """Return only the candidate directories inside the redirected home."""
        home = Path.home()
        return tuple(
            (directory, searched)
            for directory, searched in real_dirs(shell)
            if home == directory or home in directory.parents
        )

    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setattr(setup_wizard, "completion_dirs", under_the_fake_home)
    monkeypatch.setattr(setup_wizard, "completion_is_live", lambda shell: True)


def pytest_configure(config):
    """Register the markers the fixture corpus uses.

    Args:
        config: Pytest's config object.
    """
    config.addinivalue_line(
        "markers",
        "corpus: needs a capture of the real tablet; skipped when none exists",
    )
    config.addinivalue_line(
        "markers",
        "live: talks to the real tablet or a real provider; never runs by default",
    )
    config.addinivalue_line(
        "markers",
        "slow: takes minutes; deselected by pyproject's addopts, run with -m slow",
    )


@pytest.fixture(scope="session")
def corpus_root() -> Path:
    """Return the committed fixture corpus directory.

    These are synthetic files generated by ``tests.fixtures.build_corpus`` and
    committed as frozen bytes, so this fixture always resolves.

    Returns:
        The corpus directory.
    """
    return Path(__file__).parent / "fixtures" / "corpus"


@pytest.fixture
def corpus_transport(corpus_root):
    """Return a transport serving the committed corpus.

    Args:
        corpus_root: The committed corpus directory.

    Returns:
        A :class:`~tests.fixtures.transport.CorpusTransport` over it.
    """
    from tests.fixtures.transport import CorpusTransport

    return CorpusTransport(corpus_root)


@pytest.fixture
def device_corpus() -> Path:
    """Return the most recent capture of the real tablet.

    The capture is never committed — it is the user's own documents, and the
    repository is public. A machine without one skips the test rather than
    failing it, so the suite stays green in CI and on a fresh clone.

    Returns:
        The newest capture directory.
    """
    override = os.environ.get("LIVING_INK_TEST_CORPUS")
    base = (
        Path(override)
        if override
        else Path(REAL_HOME) / ".local" / "share" / "living-ink" / "test-corpus"
    )
    captures = sorted(base.glob("device-*")) if base.is_dir() else []
    if not captures:
        pytest.skip("No device capture. Run scripts/capture_corpus.py with the tablet connected.")
    return captures[-1]
