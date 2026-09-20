"""Tests for living_ink.setup_wizard — what the wizard knows, not what it asks.

Obsidian vault auto-detection, folder listing, reMarkable pairing, AI provider
verification, LaunchAgent creation and YAML generation. The conversation that
uses all of this lives in :mod:`tests.test_wizard`, because ``info`` calls the
same probes and neither half should have to import the other to be tested.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
import yaml

from living_ink import setup_wizard
from living_ink.config import credentials
from living_ink.setup_wizard import (
    detect_obsidian_vaults,
    generate_config_yaml,
    get_existing_remarkable_token,
    install_cli_command,
    install_launch_agent,
    list_vault_folders,
    pair_remarkable_device,
    uninstall_launch_agent,
    verify_ai_provider,
    verify_remarkable_ssh,
    verify_remarkable_token,
)

# =========================================================================
# Obsidian Detection & Vault Listing
# =========================================================================


class TestObsidianDetection:
    """Tests for auto-detecting Obsidian vaults and folders."""

    def test_detect_obsidian_vaults_missing_config(self):
        """Returns empty list if obsidian.json does not exist."""
        with patch("living_ink.setup_wizard.get_obsidian_config_path", return_value=None):
            vaults = detect_obsidian_vaults()
            assert vaults == []

    def test_detect_obsidian_vaults_parses_json(self, tmp_path):
        """Correctly extracts existing vaults from obsidian.json."""
        v1 = tmp_path / "MyVault"
        v1.mkdir()
        v2 = tmp_path / "Work Vault"
        v2.mkdir()
        v_missing = tmp_path / "NonExistent"

        config_file = tmp_path / "obsidian.json"
        config_data = {
            "vaults": {
                "id1": {"path": str(v1), "open": True},
                "id2": {"path": str(v2), "open": False},
                "id3": {"path": str(v_missing), "open": False},
            }
        }
        config_file.write_text(json.dumps(config_data), encoding="utf-8")

        with patch("living_ink.setup_wizard.get_obsidian_config_path", return_value=config_file):
            vaults = detect_obsidian_vaults()
            assert len(vaults) == 2
            names = [v["name"] for v in vaults]
            assert "MyVault" in names
            assert "Work Vault" in names

    def test_list_vault_folders(self, tmp_path):
        """Lists non-hidden subdirectories inside a vault."""
        (tmp_path / "Notes").mkdir()
        (tmp_path / "Projects").mkdir()
        (tmp_path / ".obsidian").mkdir()
        (tmp_path / ".trash").mkdir()
        (tmp_path / "hello.txt").write_text("file")

        folders = list_vault_folders(tmp_path)
        assert folders == ["Notes", "Projects"]

    def test_list_vault_folders_nonexistent(self, tmp_path):
        """Handles non-existent directory gracefully."""
        folders = list_vault_folders(tmp_path / "ghost")
        assert folders == []


# =========================================================================
# reMarkable Token & Pairing
# =========================================================================


class TestRemarkablePairing:
    """Tests for reMarkable device pairing and token checks."""

    def test_get_existing_remarkable_token(self, tmp_path):
        """Reads token from ~/.rmapi if present."""
        rmapi = tmp_path / ".rmapi"
        rmapi.write_text("valid-jwt-token", encoding="utf-8")

        with patch("pathlib.Path.home", return_value=tmp_path):
            token = get_existing_remarkable_token()
            assert token == "valid-jwt-token"

    def test_get_existing_remarkable_token_prefers_the_credentials_store(
        self, tmp_path, monkeypatch
    ):
        """A stored token wins over ``~/.rmapi``.

        Re-running the wizard must not resurrect a token the user replaced by
        pairing again: the credentials directory is what this build writes, so
        it is what this build believes.
        """
        monkeypatch.setenv("LIVING_INK_CONFIG_DIR", str(tmp_path / "config"))
        credentials.write_secret(credentials.CLOUD_TOKEN, "stored-token")
        (tmp_path / ".rmapi").write_text("stale-token", encoding="utf-8")

        with patch("pathlib.Path.home", return_value=tmp_path):
            assert get_existing_remarkable_token() == "stored-token"

    def test_get_existing_remarkable_token_ignores_placeholder(self, tmp_path):
        """Ignores placeholder token values."""
        rmapi = tmp_path / ".rmapi"
        rmapi.write_text("YOUR-TOKEN-HERE", encoding="utf-8")

        with patch("pathlib.Path.home", return_value=tmp_path):
            token = get_existing_remarkable_token()
            assert token is None

    def test_verify_remarkable_token_empty(self):
        """Empty or placeholder token fails verification."""
        ok, msg = verify_remarkable_token("")
        assert ok is False
        assert "empty" in msg.lower()

    @patch("living_ink.sync.load_client_from_token")
    def test_verify_remarkable_token_success(self, mock_load):
        """Valid token connects and reports notebook count."""
        mock_client = MagicMock()
        mock_doc = MagicMock()
        mock_doc.Type = "DocumentType"
        mock_client.get_meta_items.return_value = [mock_doc]
        mock_load.return_value = mock_client

        ok, msg = verify_remarkable_token("good-token")
        assert ok is True
        assert "1 notebooks found" in msg

    def test_pair_remarkable_device_length_check(self):
        """Rejects codes that are not 8 characters."""
        ok, token, msg = pair_remarkable_device("abc")
        assert ok is False
        assert "8 letters" in msg

    @patch("living_ink.api.register_and_get_token")
    def test_pair_remarkable_device_success(self, mock_register):
        """Exchanges 8-letter code for device token."""
        mock_register.return_value = "new-device-token"
        ok, token, msg = pair_remarkable_device("abcdefgh")
        assert ok is True
        assert token == "new-device-token"

    @patch("living_ink.ssh.SSHClient")
    def test_verify_remarkable_ssh_success(self, mock_ssh_cls):
        """Successful SSH connection verification returns True."""
        mock_client = MagicMock()
        mock_client.check_connection.return_value = True
        mock_doc = MagicMock()
        mock_doc.Type = "DocumentType"
        mock_client.get_meta_items.return_value = [mock_doc]
        mock_ssh_cls.return_value = mock_client

        ok, msg = verify_remarkable_ssh()
        assert ok is True
        assert "Connected via USB SSH" in msg
        assert "1 notebooks found" in msg

    @patch("living_ink.ssh.SSHClient")
    def test_verify_remarkable_ssh_failure(self, mock_ssh_cls):
        """Failed SSH connection returns False and helpful message."""
        mock_client = MagicMock()
        mock_client.check_connection.return_value = False
        mock_ssh_cls.return_value = mock_client

        ok, msg = verify_remarkable_ssh()
        assert ok is False
        assert "Could not establish passwordless SSH connection" in msg


# =========================================================================
# AI Provider Verification
# =========================================================================


class TestAiVerification:
    """Tests for AI provider connection testing."""

    def test_verify_none_provider(self):
        """Provider 'none' always passes without making network calls."""
        ok, msg = verify_ai_provider("none")
        assert ok is True
        assert "disabled" in msg.lower()

    @pytest.mark.parametrize("name", [None, "", "   "])
    def test_no_provider_at_all_reads_as_none(self, name):
        """``provider:`` with nothing after it is not a provider to verify.

        ``info`` passes whatever the config held, and a valueless key parses
        to ``None`` — which used to reach ``.strip()`` and crash the health
        check rather than report the cleanup as off.
        """
        ok, msg = verify_ai_provider(name)
        assert ok is True
        assert "disabled" in msg.lower()

    @patch("living_ink.providers.get_provider")
    def test_verify_provider_success(self, mock_get_provider):
        """Provider returning valid response passes verification."""
        mock_p = MagicMock()
        mock_p.name = "gemini (gemini-flash-latest)"
        mock_p.probe_vision.return_value = "READY"
        mock_get_provider.return_value = mock_p

        ok, msg = verify_ai_provider("gemini", api_key="secret")
        assert ok is True
        assert "Verified" in msg

    @patch("living_ink.providers.get_provider")
    def test_verify_provider_failure(self, mock_get_provider):
        """Provider returning empty string fails verification."""
        mock_p = MagicMock()
        mock_p.name = "gemini"
        mock_p.probe_vision.return_value = ""
        mock_p.last_failure = None
        mock_get_provider.return_value = mock_p

        ok, msg = verify_ai_provider("gemini", api_key="bad-key")
        assert ok is False
        assert "read images" in msg.lower()

    @patch("living_ink.providers.get_provider")
    def test_the_reason_the_probe_came_back_empty_is_reported(self, mock_get_provider):
        """Telling the user to check the API key is bad advice on a timeout.

        The provider reports a failed request by returning nothing, so a
        rejected key, an unreachable host and a content filter all arrive here
        as the same empty string. Only the provider knows which it was.
        """
        mock_p = MagicMock()
        mock_p.name = "gemini"
        mock_p.probe_vision.return_value = ""
        mock_p.last_failure = "HTTP 401 Unauthorized"
        mock_get_provider.return_value = mock_p

        ok, msg = verify_ai_provider("gemini", api_key="bad-key")

        assert ok is False
        assert "HTTP 401 Unauthorized" in msg

    @patch("living_ink.providers.get_provider")
    def test_the_probe_is_the_call_a_sync_makes(self, mock_get_provider):
        """A text prompt verifies the wrong thing.

        Every page is read by ``ocr_image``; a model that answers text and
        refuses images passes a text probe and then fails on page one, at one
        wasted call per page, with the run already underway.
        """
        mock_p = MagicMock()
        mock_p.name = "gemini"
        mock_p.probe_vision.return_value = "READY"
        mock_get_provider.return_value = mock_p

        verify_ai_provider("gemini", api_key="secret")

        mock_p.probe_vision.assert_called_once_with()
        mock_p.repair_text.assert_not_called()

    @patch("living_ink.providers.get_provider")
    def test_a_provider_without_vision_is_refused_before_it_is_called(self, mock_get_provider):
        """There is no second OCR backend, so this cannot be a warning."""
        mock_p = MagicMock()
        mock_p.name = "plugin"
        mock_p.supports_vision = False
        mock_get_provider.return_value = mock_p

        ok, msg = verify_ai_provider("plugin", api_key="secret")

        assert ok is False
        assert "cannot read images" in msg
        mock_p.probe_vision.assert_not_called()


# =========================================================================
# Configuration YAML Generation
# =========================================================================


class TestConfigGeneration:
    """Tests for config.yml generation."""

    def test_generate_config_yaml_valid(self):
        """Generated YAML parses back into valid python dict."""
        yaml_str = generate_config_yaml(
            ai_provider="gemini",
            ai_model="gemini-flash-latest",
            obsidian_enabled=True,
            obsidian_vault_path="/Users/test/Vault",
            obsidian_root_folder="Living Ink",
            obsidian_mirror_folders=True,
        )
        parsed = yaml.safe_load(yaml_str)
        assert parsed["ai"]["provider"] == "gemini"
        assert parsed["remarkable"]["preferred_connection"] == "ssh"
        assert parsed["obsidian"]["enabled"] is True
        assert parsed["obsidian"]["vault_path"] == "/Users/test/Vault"
        assert parsed["obsidian"]["root_folder"] == "Living Ink"
        assert parsed["obsidian"]["mirror_folders"] is True

    def test_generate_config_yaml_holds_no_secrets(self):
        """The generated config carries no key for a secret at all.

        Not "an empty key": the whole point of the credentials directory is
        that ``config.yml`` is a file a user can paste into an issue.
        """
        parsed = yaml.safe_load(
            generate_config_yaml(
                ai_provider="gemini",
                ai_model="gemini-flash-latest",
                obsidian_enabled=True,
                obsidian_vault_path="/Users/test/Vault",
            )
        )
        assert "api_key" not in parsed["ai"]
        assert "device_token" not in parsed["remarkable"]

    def test_generate_config_yaml_with_ssh(self):
        """Generated YAML with use_ssh=True parses with SSH fields."""
        yaml_str = generate_config_yaml(
            ai_provider="ollama",
            ai_model="llama3.2",
            preferred_connection="ssh",
            use_ssh=True,
            ssh_host="10.11.99.1",
            ssh_port=22,
            obsidian_enabled=True,
            obsidian_vault_path="/Users/test/Vault",
        )
        parsed = yaml.safe_load(yaml_str)
        assert parsed["ai"]["provider"] == "ollama"
        assert parsed["remarkable"]["preferred_connection"] == "ssh"
        assert parsed["remarkable"]["use_ssh"] is True
        assert parsed["remarkable"]["ssh_host"] == "10.11.99.1"

    def test_generate_config_yaml_escapes_special_characters(self):
        """Quotes, backslashes and colons in user input survive a round-trip."""
        vault_path = '/Users/test/My "Vault": notes\\here'
        folder = 'Note"s: #1'

        parsed = yaml.safe_load(
            generate_config_yaml(
                ai_provider="openai",
                ai_model="gpt-4o-mini",
                obsidian_enabled=True,
                obsidian_vault_path=vault_path,
                obsidian_root_folder=folder,
            )
        )

        assert parsed["obsidian"]["vault_path"] == vault_path
        assert parsed["obsidian"]["root_folder"] == folder

    def test_generate_config_yaml_explains_itself_from_the_schema(self):
        """Each section is introduced by the same help the validator declares."""
        from living_ink.config import SECTIONS

        yaml_str = generate_config_yaml(ai_provider="gemini", ai_model="gemini-flash-latest")
        assert "# Living Ink configuration" in yaml_str
        assert f"# {SECTIONS['ai'].help}" in yaml_str
        assert f"# {SECTIONS['obsidian'].help}" in yaml_str

    def test_generate_config_yaml_offers_no_second_ocr_backend(self):
        """There is one way to read a page, so the config stops implying two."""
        yaml_str = generate_config_yaml(ai_provider="gemini", ai_model="gemini-flash-latest")
        assert "google_vision" not in yaml_str
        assert "google_vision" not in yaml.safe_load(yaml_str)


# =========================================================================
# LaunchAgent Management
# =========================================================================


class TestLaunchAgent:
    """Tests for macOS LaunchAgent installation."""

    def test_install_launch_agent_non_macos(self):
        """Rejects installation on non-macOS systems."""
        with patch("platform.system", return_value="Linux"):
            ok, msg = install_launch_agent()
            assert ok is False
            assert "only supported on macOS" in msg

    @patch("subprocess.run")
    @patch("platform.system", return_value="Darwin")
    def test_install_launch_agent_macos(self, mock_system, mock_run, tmp_path):
        """Writes a plist that supervises ``watch`` and schedules nothing.

        The three assertions are the whole of §13.3. ``StartInterval`` is gone
        because *when* a sync happens is a cron expression in ``config.yml``
        that the watcher re-reads every tick — left in the plist, it would be a
        second schedule, invisible to ``info`` and unreachable from ``config``.
        ``KeepAlive`` is what replaces it: launchd's remaining job is to
        restart the watcher, not to time it.
        """
        mock_run.return_value = MagicMock(returncode=0)

        cli_path = tmp_path / "living-ink"
        cli_path.write_text("#!/bin/sh\n", encoding="utf-8")
        mock_plist_path = tmp_path / "com.livingink.sync.plist"
        with (
            patch.object(setup_wizard, "LAUNCH_AGENT_PLIST", mock_plist_path),
            patch.object(setup_wizard.shutil, "which", return_value=str(cli_path)),
        ):
            ok, msg = install_launch_agent(repo_dir=tmp_path)
            assert ok is True
            assert mock_plist_path.exists()
            content = mock_plist_path.read_text(encoding="utf-8")
            assert "com.livingink.sync" in content
            assert "<string>watch</string>" in content
            assert "StartInterval" not in content
            assert "<key>KeepAlive</key>" in content

    @patch("platform.system", return_value="Darwin")
    def test_install_launch_agent_needs_the_command(self, mock_system, tmp_path):
        """Refuses rather than supervising an interpreter out of a checkout.

        The old fallback was ``uv run python -m living_ink`` from the
        repository, which works until the directory is renamed — and then the
        background job dies silently, months later, with the only symptom
        being notebooks that stop arriving.
        """
        mock_plist_path = tmp_path / "com.livingink.sync.plist"
        with (
            patch.object(setup_wizard, "LAUNCH_AGENT_PLIST", mock_plist_path),
            patch.object(setup_wizard.shutil, "which", return_value=None),
        ):
            ok, msg = install_launch_agent(repo_dir=tmp_path)

        assert ok is False
        assert "not installed" in msg
        assert not mock_plist_path.exists()

    @patch("subprocess.run")
    def test_uninstall_launch_agent(self, mock_run, tmp_path):
        """Uninstalls and removes existing LaunchAgent plist."""
        mock_plist = tmp_path / "com.livingink.sync.plist"
        mock_plist.write_text("<plist></plist>", encoding="utf-8")
        mock_run.return_value = MagicMock(returncode=0)

        with patch.object(setup_wizard, "LAUNCH_AGENT_PLIST", mock_plist):
            ok, msg = uninstall_launch_agent()
            assert ok is True
            assert not mock_plist.exists()


class TestInstallCliCommand:
    """Tests for global living-ink CLI launcher installation."""

    def test_install_cli_command_success(self, tmp_path):
        """Creates executable wrapper pointing to repository root."""
        repo_dir = tmp_path / "my-repo"
        bin_dir = tmp_path / "bin"
        repo_dir.mkdir()

        ok, msg = install_cli_command(repo_dir=repo_dir, bin_dir=bin_dir)
        assert ok is True
        assert "Global command 'living-ink' installed" in msg

        wrapper = bin_dir / "living-ink"
        assert wrapper.exists()
        content = wrapper.read_text(encoding="utf-8")
        assert f'VENV_BIN="{repo_dir.resolve()}/.venv/bin/living-ink"' in content
        assert 'exec "$VENV_BIN" "$@"' in content
