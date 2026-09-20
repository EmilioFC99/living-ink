"""Tests for ``living-ink setup`` — the conversation, not the probes.

:mod:`tests.test_setup_wizard` covers what the wizard *knows* (vault detection,
token verification, launch agents); this covers what it *asks* and, far more
importantly, **when it writes**.

The widgets are replaced here rather than driven through a pipe. The pipe
harness in :mod:`tests.test_ui` already proves that a real ``questionary``
prompt reads a real keystroke; re-proving it eleven times per flow would test
``prompt_toolkit`` and make every flow test hostage to a rendering change. What
is worth pinning at this level is the flow: which question follows which
answer, and that nothing reaches disk before the summary is confirmed.
"""

import stat
from typing import Any, Dict, List
from unittest.mock import patch

import pytest
import yaml

from living_ink import ui
from living_ink.cli.commands.setup import NEW_FOLDER, Wizard
from living_ink.config import credentials


class Script:
    """Answers the wizard's questions by matching the text of each one.

    Keying on the question rather than on call order is what makes a flow test
    readable a year later: the test says "asked about the Cloud, answered no",
    and an unscripted question fails by naming itself instead of silently
    consuming the answer meant for the next one.

    Attributes:
        asked: Every question put to the user, in order.
    """

    def __init__(self, answers: Dict[str, Any]) -> None:
        """Prepare a set of answers.

        Args:
            answers: Lowercase fragment of a question mapped to the answer.
                Each fragment must match exactly one question.
        """
        self._answers = answers
        self.asked: List[str] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> "Script":
        """Replace every widget in :mod:`living_ink.ui` with this script.

        Args:
            monkeypatch: The fixture that undoes it afterwards.

        Returns:
            This script, for chaining.
        """
        for widget in ("select", "checkbox", "confirm", "text", "password", "path"):
            monkeypatch.setattr(ui, widget, self._reply)
        return self

    def _reply(self, message: str, *_args: Any, **_kwargs: Any) -> Any:
        """Answer one question.

        Args:
            message: The question as the user would read it.

        Returns:
            The scripted answer.

        Raises:
            AssertionError: If nothing in the script matches the question.
        """
        self.asked.append(message)
        lowered = message.lower()
        for fragment, answer in self._answers.items():
            if fragment in lowered:
                return answer
        raise AssertionError(f"Unscripted question: {message!r}")


CLOUD_ONLY = {
    "how should living ink reach": "cloud",
    "reuse the remarkable pairing": True,
    "also set up the usb cable": False,
    "which ai provider": "gemini",
    "model": "gemini-2.0-flash",
    "api key": "AIzaTestKey",
    "publish to obsidian": True,
    "which vault": "",  # filled in per test
    "which folder inside the vault": "Living Ink",
    "mirror the tablet": True,
    "sync automatically every hour": False,
    "save this configuration": True,
    "run your first sync now": False,
}

USB_ONLY = {
    "how should living ink reach": "ssh",
    "ssh host": "10.11.99.1",
    "also pair with the cloud": False,
    "reuse the remarkable pairing": True,
    "which ai provider": "gemini",
    "model": "gemini-2.0-flash",
    "api key": "AIzaTestKey",
    "publish to obsidian": True,
    "which vault": "",
    "which folder inside the vault": "Living Ink",
    "mirror the tablet": True,
    "sync automatically every hour": False,
    "save this configuration": True,
    "run your first sync now": False,
}


@pytest.fixture
def vault(tmp_path):
    """Create a vault with a ``Living Ink`` folder already in it.

    Args:
        tmp_path: Per-test directory.

    Returns:
        The vault path.
    """
    path = tmp_path / "MyVault"
    (path / "Living Ink").mkdir(parents=True)
    return path


@pytest.fixture
def probes(vault):
    """Stub every probe the wizard makes, so no test touches a network.

    Args:
        vault: The vault the detector should report.

    Yields:
        None, for the duration of the test.
    """
    with (
        patch(
            "living_ink.setup_wizard.detect_obsidian_vaults",
            return_value=[{"name": vault.name, "path": str(vault)}],
        ),
        patch("living_ink.setup_wizard.verify_remarkable_ssh", return_value=(True, "Connected")),
        patch("living_ink.setup_wizard.verify_remarkable_token", return_value=(True, "OK")),
        patch("living_ink.setup_wizard.get_existing_remarkable_token", return_value="tok-existing"),
        patch("living_ink.setup_wizard.verify_ai_provider", return_value=(True, "OK")),
        patch.object(Wizard, "estimate", lambda self: None),
    ):
        yield


def run_wizard(monkeypatch, tmp_path, answers, vault):
    """Run one wizard to completion against a scripted user.

    Args:
        monkeypatch: For installing the script.
        tmp_path: The root the config is written under.
        answers: The script, as :class:`Script` takes it.
        vault: The vault to answer "which vault?" with.

    Returns:
        A ``(result, script)`` pair.
    """
    script = Script({**answers, "which vault": str(vault)}).install(monkeypatch)
    wizard = Wizard(root=tmp_path, bin_dir=tmp_path / "bin")
    return wizard.run(), script


def saved_config(tmp_path):
    """Return the config the wizard wrote under a test root.

    Args:
        tmp_path: The root the wizard was given.

    Returns:
        The path to ``config/config.yml``.
    """
    return tmp_path / "config" / "config.yml"


class TestTheFlow:
    """Which questions get asked, and what the answers add up to."""

    def test_cloud_flow_writes_a_cloud_config(self, monkeypatch, tmp_path, vault, probes):
        """Choosing the Cloud stores the pairing and turns SSH off."""
        result, _script = run_wizard(monkeypatch, tmp_path, CLOUD_ONLY, vault)

        assert result.saved is True
        assert result.run_sync_requested is False

        config_file = saved_config(tmp_path)
        cfg = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        assert cfg["remarkable"]["preferred_connection"] == "cloud"
        assert cfg["remarkable"]["use_ssh"] is False
        assert cfg["ai"]["provider"] == "gemini"
        assert cfg["obsidian"]["enabled"] is True
        assert cfg["obsidian"]["root_folder"] == "Living Ink"
        assert cfg["obsidian"]["vault_path"] == str(vault)

        assert (
            credentials.read_secret("ai.api_key.gemini", config_path=config_file) == "AIzaTestKey"
        )
        assert (
            credentials.read_secret(credentials.CLOUD_TOKEN, config_path=config_file)
            == "tok-existing"
        )

    def test_usb_flow_writes_an_ssh_config_and_no_token(self, monkeypatch, tmp_path, vault, probes):
        """Declining the Cloud fallback stores no pairing at all."""
        result, _script = run_wizard(monkeypatch, tmp_path, USB_ONLY, vault)

        assert result.saved is True
        config_file = saved_config(tmp_path)
        cfg = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        assert cfg["remarkable"]["preferred_connection"] == "ssh"
        assert cfg["remarkable"]["use_ssh"] is True
        assert cfg["remarkable"]["ssh_host"] == "10.11.99.1"
        assert credentials.read_secret(credentials.CLOUD_TOKEN, config_path=config_file) is None

    def test_usb_with_a_cloud_fallback_stores_both(self, monkeypatch, tmp_path, vault, probes):
        """A preferred transport and a fallback are two separate answers."""
        answers = {**USB_ONLY, "also pair with the cloud": True}
        run_wizard(monkeypatch, tmp_path, answers, vault)

        config_file = saved_config(tmp_path)
        cfg = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        assert cfg["remarkable"]["preferred_connection"] == "ssh"
        assert (
            credentials.read_secret(credentials.CLOUD_TOKEN, config_path=config_file)
            == "tok-existing"
        )

    def test_the_cloud_flow_never_asks_about_the_cable(self, monkeypatch, tmp_path, vault, probes):
        """A question whose answer is already implied is not asked."""
        _result, script = run_wizard(monkeypatch, tmp_path, CLOUD_ONLY, vault)
        assert not any("also pair with the cloud" in q.lower() for q in script.asked)

    def test_a_new_folder_is_named_by_the_user(self, monkeypatch, tmp_path, vault, probes):
        """Choosing 'create a new folder' asks what to call it."""
        answers = {
            **CLOUD_ONLY,
            "which folder inside the vault": NEW_FOLDER,
            "new folder name": "Ink",
        }
        run_wizard(monkeypatch, tmp_path, answers, vault)

        cfg = yaml.safe_load(saved_config(tmp_path).read_text(encoding="utf-8"))
        assert cfg["obsidian"]["root_folder"] == "Ink"

    def test_the_vault_root_is_an_answer(self, monkeypatch, tmp_path, vault, probes):
        """An empty root folder means the vault itself, not an unanswered question."""
        answers = {**CLOUD_ONLY, "which folder inside the vault": ""}
        run_wizard(monkeypatch, tmp_path, answers, vault)

        cfg = yaml.safe_load(saved_config(tmp_path).read_text(encoding="utf-8"))
        assert cfg["obsidian"]["root_folder"] == ""


class TestNothingIsWrittenBeforeTheSummary:
    """The atomic commit, which is the whole reason the wizard was rewritten."""

    def test_declining_the_summary_writes_nothing_at_all(
        self, monkeypatch, tmp_path, vault, probes
    ):
        """Not the config, not the key, not the pairing — the user said no."""
        answers = {**CLOUD_ONLY, "save this configuration": False}
        result, _script = run_wizard(monkeypatch, tmp_path, answers, vault)

        assert result.saved is False
        assert not saved_config(tmp_path).exists()
        assert not (tmp_path / "config").exists()

    def test_declining_the_summary_does_not_offer_a_sync(
        self, monkeypatch, tmp_path, vault, probes
    ):
        """There is nothing to sync with, so the question is not asked."""
        answers = {**CLOUD_ONLY, "save this configuration": False}
        _result, script = run_wizard(monkeypatch, tmp_path, answers, vault)
        assert not any("first sync" in question.lower() for question in script.asked)

    def test_a_cancelled_answer_leaves_nothing_behind(self, monkeypatch, tmp_path, vault, probes):
        """Ctrl+C at question two is a KeyboardInterrupt, and writes nothing."""
        answers = {**CLOUD_ONLY, "which ai provider": None}
        script = Script({**answers, "which vault": str(vault)}).install(monkeypatch)
        assert script  # installed

        with pytest.raises(KeyboardInterrupt):
            Wizard(root=tmp_path, bin_dir=tmp_path / "bin").run()

        assert not saved_config(tmp_path).exists()


class TestWhatTheSummaryShows:
    """A user confirms what they can read, including what did not verify."""

    def test_the_summary_masks_every_secret(self, monkeypatch, tmp_path, vault, probes, capsys):
        """A key or a token on screen survives in the scrollback."""
        run_wizard(monkeypatch, tmp_path, CLOUD_ONLY, vault)
        out = capsys.readouterr().out
        assert "AIzaTestKey" not in out
        assert "tok-existing" not in out
        assert credentials.mask("AIzaTestKey") in out

    def test_a_failed_check_is_repeated_at_the_summary(
        self, monkeypatch, tmp_path, vault, probes, capsys
    ):
        """A warning ten questions ago has scrolled away by the time it matters."""
        with patch(
            "living_ink.setup_wizard.verify_remarkable_ssh",
            return_value=(False, "No route to host"),
        ):
            run_wizard(monkeypatch, tmp_path, USB_ONLY, vault)

        out = capsys.readouterr().out
        assert out.count("No route to host") >= 2

    def test_no_destination_is_a_warning_not_a_refusal(
        self, monkeypatch, tmp_path, vault, probes, capsys
    ):
        """Setup still finishes; the sync is what refuses to run."""
        answers = {**CLOUD_ONLY, "publish to obsidian": False}
        result, _script = run_wizard(monkeypatch, tmp_path, answers, vault)

        assert result.saved is True
        cfg = yaml.safe_load(saved_config(tmp_path).read_text(encoding="utf-8"))
        assert cfg["obsidian"]["enabled"] is False
        assert "no destination" in capsys.readouterr().out.lower()


class TestTheClosingEstimate:
    """§14.3: the last thing a first run says is what the next one will do."""

    def _rows(self, count, folder="Work"):
        """Build a comparison table of pending documents.

        Args:
            count: How many pending rows to produce.
            folder: The folder every row sits in.

        Returns:
            Rows shaped like ``inventory.compare_with_device`` returns.
        """
        return [{"pending": True, "folder": folder} for _ in range(count)]

    def test_it_reports_the_counts_a_real_preview_found(self, tmp_path, capsys):
        """The same selector the run uses, so the numbers are the run's numbers."""
        with patch(
            "living_ink.cli.inventory.compare_with_device",
            return_value=(self._rows(3), [], None),
        ):
            Wizard(root=tmp_path).estimate()

        out = capsys.readouterr().out
        assert "3 documents" in out
        assert "1 folder" in out

    def test_it_puts_no_price_on_the_run(self, tmp_path, capsys):
        """Pages per document is unknowable first time; an invented figure is worse."""
        with patch(
            "living_ink.cli.inventory.compare_with_device",
            return_value=(self._rows(1), [], None),
        ):
            Wizard(root=tmp_path).estimate()

        out = capsys.readouterr().out
        assert "$" not in out
        assert "cached" in out.lower()

    def test_an_unreachable_tablet_does_not_undo_the_setup(self, tmp_path, capsys):
        """The config was already written; a preview is a courtesy on top of it."""
        with patch(
            "living_ink.cli.inventory.compare_with_device",
            side_effect=OSError("no route to host"),
        ):
            Wizard(root=tmp_path).estimate()

        out = capsys.readouterr().out
        assert "no route to host" in out
        assert "living-ink sync --preview" in out


class TestWhatTheWizardWrites:
    """The config describes a machine; the credentials sit beside it at 0600."""

    def test_the_config_and_its_credentials_are_owner_only(
        self, monkeypatch, tmp_path, vault, probes
    ):
        """Nothing the wizard writes may be readable by another account."""
        run_wizard(monkeypatch, tmp_path, CLOUD_ONLY, vault)

        config_file = saved_config(tmp_path)
        assert stat.S_IMODE(config_file.stat().st_mode) == 0o600
        assert stat.S_IMODE(config_file.parent.stat().st_mode) == 0o700

        stored = list((config_file.parent / "credentials").iterdir())
        assert len(stored) == 2
        for path in stored:
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert credentials.insecure_credentials(config_path=config_file) == []

    def test_no_secret_reaches_the_config_file(self, monkeypatch, tmp_path, vault, probes):
        """The file is something a user can paste into an issue."""
        run_wizard(monkeypatch, tmp_path, CLOUD_ONLY, vault)
        text = saved_config(tmp_path).read_text(encoding="utf-8")
        assert "AIzaTestKey" not in text
        assert "tok-existing" not in text

    def test_the_written_config_validates(self, monkeypatch, tmp_path, vault, probes):
        """A wizard that writes a config the loader rejects is worse than none."""
        from living_ink.config import ERROR, read_config_file, validate_config

        run_wizard(monkeypatch, tmp_path, CLOUD_ONLY, vault)
        problems = validate_config(read_config_file(saved_config(tmp_path)))
        assert [p for p in problems if p.level == ERROR] == []
