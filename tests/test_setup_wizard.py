"""Tests for living_ink.setup_wizard module.

Covers Obsidian vault auto-detection, folder listing, reMarkable pairing,
AI provider verification, LaunchAgent creation, YAML generation,
and the interactive wizard workflow.
"""

import json
from unittest.mock import MagicMock, patch

import yaml

from living_ink import setup_wizard
from living_ink.setup_wizard import (
    detect_obsidian_vaults,
    generate_config_yaml,
    get_existing_remarkable_token,
    install_cli_command,
    install_launch_agent,
    list_vault_folders,
    pair_remarkable_device,
    run_wizard,
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

    @patch("living_ink.providers.get_provider")
    def test_verify_provider_success(self, mock_get_provider):
        """Provider returning valid response passes verification."""
        mock_p = MagicMock()
        mock_p.name = "gemini (gemini-flash-latest)"
        mock_p.repair_text.return_value = "READY"
        mock_get_provider.return_value = mock_p

        ok, msg = verify_ai_provider("gemini", api_key="secret")
        assert ok is True
        assert "Verified" in msg

    @patch("living_ink.providers.get_provider")
    def test_verify_provider_failure(self, mock_get_provider):
        """Provider returning empty string fails verification."""
        mock_p = MagicMock()
        mock_p.name = "gemini"
        mock_p.repair_text.return_value = ""
        mock_get_provider.return_value = mock_p

        ok, msg = verify_ai_provider("gemini", api_key="bad-key")
        assert ok is False
        assert "empty response" in msg.lower()


# =========================================================================
# Configuration YAML Generation
# =========================================================================


class TestConfigGeneration:
    """Tests for config.yml generation."""

    def test_generate_config_yaml_valid(self):
        """Generated YAML parses back into valid python dict."""
        yaml_str = generate_config_yaml(
            ai_provider="gemini",
            ai_api_key="my-key",
            ai_model="gemini-flash-latest",
            remarkable_token="tok123",
            obsidian_enabled=True,
            obsidian_vault_path="/Users/test/Vault",
            obsidian_root_folder="Living Ink",
            obsidian_mirror_folders=True,
            apple_notes_enabled=False,
        )
        parsed = yaml.safe_load(yaml_str)
        assert parsed["ai"]["provider"] == "gemini"
        assert parsed["ai"]["api_key"] == "my-key"
        assert parsed["remarkable"]["device_token"] == "tok123"
        assert parsed["remarkable"]["preferred_connection"] == "ssh"
        assert parsed["obsidian"]["enabled"] is True
        assert parsed["obsidian"]["vault_path"] == "/Users/test/Vault"
        assert parsed["obsidian"]["root_folder"] == "Living Ink"
        assert parsed["obsidian"]["mirror_folders"] is True
        assert parsed["apple_notes"]["enabled"] is False

    def test_generate_config_yaml_with_ssh(self):
        """Generated YAML with use_ssh=True parses with SSH fields."""
        yaml_str = generate_config_yaml(
            ai_provider="ollama",
            ai_api_key="",
            ai_model="llama3.2",
            remarkable_token="",
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
        assert parsed["remarkable"]["device_token"] == ""

    def test_generate_config_yaml_escapes_special_characters(self):
        """Quotes, backslashes and colons in user input survive a round-trip."""
        api_key = 'sk-ab"c\\d:e'
        vault_path = '/Users/test/My "Vault": notes\\here'
        folder = 'Note"s: #1'

        parsed = yaml.safe_load(
            generate_config_yaml(
                ai_provider="openai",
                ai_api_key=api_key,
                ai_model="gpt-4o-mini",
                obsidian_enabled=True,
                obsidian_vault_path=vault_path,
                obsidian_root_folder=folder,
                apple_notes_enabled=True,
                apple_notes_folder=folder,
            )
        )

        assert parsed["ai"]["api_key"] == api_key
        assert parsed["obsidian"]["vault_path"] == vault_path
        assert parsed["obsidian"]["root_folder"] == folder
        assert parsed["apple_notes"]["folder_name"] == folder

    def test_generate_config_yaml_preserves_section_comments(self):
        """The generated file stays readable and hand-editable."""
        yaml_str = generate_config_yaml(
            ai_provider="gemini", ai_api_key="k", ai_model="gemini-flash-latest"
        )
        assert "# Living Ink Configuration" in yaml_str
        assert "# 1. AI Handwriting OCR & Text Cleanup" in yaml_str
        assert "# 6. Apple Notes Destination" in yaml_str


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
        """Generates valid LaunchAgent plist file and runs launchctl."""
        mock_run.return_value = MagicMock(returncode=0)

        mock_plist_path = tmp_path / "com.livingink.sync.plist"
        with patch.object(setup_wizard, "LAUNCH_AGENT_PLIST", mock_plist_path):
            ok, msg = install_launch_agent(repo_dir=tmp_path, interval_seconds=3600)
            assert ok is True
            assert mock_plist_path.exists()
            content = mock_plist_path.read_text(encoding="utf-8")
            assert "com.livingink.sync" in content
            assert "<integer>3600</integer>" in content

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


# =========================================================================
# Interactive Wizard Execution
# =========================================================================


class TestRunWizard:
    """Tests for the interactive walkthrough workflow."""

    @patch("living_ink.setup_wizard.verify_remarkable_token", return_value=(True, "OK"))
    @patch("living_ink.setup_wizard.verify_ai_provider", return_value=(True, "OK"))
    @patch("living_ink.setup_wizard.get_existing_remarkable_token", return_value="existing-token")
    @patch("living_ink.setup_wizard.detect_obsidian_vaults")
    def test_run_wizard_cloud_flow(
        self,
        mock_detect_vaults,
        mock_get_token,
        mock_verify_ai,
        mock_verify_rm,
        tmp_path,
    ):
        """Walkthrough with Cloud connection creates config/config.yml."""
        mock_detect_vaults.return_value = [{"name": "MyVault", "path": str(tmp_path / "MyVault")}]
        (tmp_path / "MyVault").mkdir()
        (tmp_path / "MyVault" / "Living Ink").mkdir()

        # Simulated user responses:
        # Step 1: Option 2 (Cloud) -> Use existing token -> "y" -> SSH backup -> "n"
        # Step 2: Provider -> "1" (gemini), API key -> "AIzaTestKey"
        # Step 3: Enable Obsidian -> "y", Select vault -> "1",
        #         Choose folder -> "1" (Living Ink), Mirror -> "y"
        # Apple Notes -> "n"
        # macOS background sync -> "n"
        # First sync -> "n"
        inputs = iter(
            [
                "2",  # Cloud connection
                "y",  # Use existing token
                "n",  # USB SSH backup -> no
                "1",  # Gemini
                "AIzaTestKey",  # API Key
                "y",  # Enable Obsidian
                "1",  # Vault 1
                "1",  # Existing folder 1
                "y",  # Mirror folders
                "n",  # Apple notes
                "n",  # Background sync
                "n",  # First sync
            ]
        )

        outputs = []
        result = run_wizard(
            input_func=lambda prompt="": next(inputs),
            print_func=lambda *args: outputs.append(" ".join(str(a) for a in args)),
            repo_dir=tmp_path,
            bin_dir=tmp_path / "bin",
        )

        assert result.saved is True
        assert result.run_sync_requested is False
        saved_config = tmp_path / "config" / "config.yml"
        assert saved_config.exists()
        cfg = yaml.safe_load(saved_config.read_text(encoding="utf-8"))
        assert cfg["ai"]["provider"] == "gemini"
        assert cfg["ai"]["api_key"] == "AIzaTestKey"
        assert cfg["remarkable"]["preferred_connection"] == "cloud"
        assert cfg["remarkable"]["use_ssh"] is False
        assert cfg["remarkable"]["device_token"] == "existing-token"
        assert cfg["obsidian"]["enabled"] is True
        assert cfg["obsidian"]["root_folder"] == "Living Ink"

    @patch("living_ink.setup_wizard.verify_remarkable_ssh", return_value=(True, "Connected"))
    @patch("living_ink.setup_wizard.verify_ai_provider", return_value=(True, "OK"))
    @patch("living_ink.setup_wizard.detect_obsidian_vaults")
    def test_run_wizard_ssh_flow(
        self,
        mock_detect_vaults,
        mock_verify_ai,
        mock_verify_ssh,
        tmp_path,
    ):
        """Walkthrough with USB SSH (default) creates config/config.yml."""
        mock_detect_vaults.return_value = [{"name": "MyVault", "path": str(tmp_path / "MyVault")}]
        (tmp_path / "MyVault").mkdir()
        (tmp_path / "MyVault" / "Living Ink").mkdir()

        # Simulated user responses:
        # Step 1: Default (1 - SSH) -> host default "" -> Cloud backup -> "n"
        # Step 2: Provider -> "1" (gemini), API key -> "AIzaTestKey"
        # Step 3: Enable Obsidian -> "y", Select vault -> "1",
        #         Choose folder -> "1" (Living Ink), Mirror -> "y"
        # Apple Notes -> "n"
        # macOS background sync -> "n"
        # First sync -> "n"
        inputs = iter(
            [
                "1",  # SSH connection (option 1)
                "",  # Host default (10.11.99.1)
                "n",  # Cloud backup -> no
                "1",  # Gemini
                "AIzaTestKey",  # API Key
                "y",  # Enable Obsidian
                "1",  # Vault 1
                "1",  # Existing folder 1
                "y",  # Mirror folders
                "n",  # Apple notes
                "n",  # Background sync
                "n",  # First sync
            ]
        )

        outputs = []
        result = run_wizard(
            input_func=lambda prompt="": next(inputs),
            print_func=lambda *args: outputs.append(" ".join(str(a) for a in args)),
            repo_dir=tmp_path,
            bin_dir=tmp_path / "bin",
        )

        assert result.saved is True
        assert result.run_sync_requested is False
        saved_config = tmp_path / "config" / "config.yml"
        assert saved_config.exists()
        cfg = yaml.safe_load(saved_config.read_text(encoding="utf-8"))
        assert cfg["ai"]["provider"] == "gemini"
        assert cfg["ai"]["api_key"] == "AIzaTestKey"
        assert cfg["remarkable"]["preferred_connection"] == "ssh"
        assert cfg["remarkable"]["use_ssh"] is True
        assert cfg["remarkable"]["ssh_host"] == "10.11.99.1"
        assert cfg["remarkable"]["device_token"] == ""
        assert cfg["obsidian"]["enabled"] is True
        assert cfg["obsidian"]["root_folder"] == "Living Ink"

    @patch("living_ink.setup_wizard.verify_remarkable_token", return_value=(True, "OK"))
    @patch("living_ink.setup_wizard.get_existing_remarkable_token", return_value="existing-token")
    @patch("living_ink.setup_wizard.verify_remarkable_ssh", return_value=(True, "Connected"))
    @patch("living_ink.setup_wizard.verify_ai_provider", return_value=(True, "OK"))
    @patch("living_ink.setup_wizard.detect_obsidian_vaults")
    def test_run_wizard_ssh_with_cloud_backup_flow(
        self,
        mock_detect_vaults,
        mock_verify_ai,
        mock_verify_ssh,
        mock_get_token,
        mock_verify_rm,
        tmp_path,
    ):
        """Walkthrough configuring both USB SSH (preferred) and Cloud backup."""
        mock_detect_vaults.return_value = [{"name": "MyVault", "path": str(tmp_path / "MyVault")}]
        (tmp_path / "MyVault").mkdir()
        (tmp_path / "MyVault" / "Living Ink").mkdir()

        # Step 1: Option 1 (SSH) -> host default ""
        #         -> Cloud backup: "y" -> use existing token: "y"
        # Step 2: Provider -> "1" (gemini), API key -> "AIzaTestKey"
        # Step 3: Enable Obsidian -> "y", Select vault -> "1",
        #         Choose folder -> "1" (Living Ink), Mirror -> "y"
        # Apple Notes -> "n", Background sync -> "n", First sync -> "n"
        inputs = iter(
            [
                "1",  # SSH connection
                "",  # Host default
                "y",  # Configure Cloud backup
                "y",  # Use existing token
                "1",  # Gemini
                "AIzaTestKey",  # API Key
                "y",  # Enable Obsidian
                "1",  # Vault 1
                "1",  # Existing folder 1
                "y",  # Mirror folders
                "n",  # Apple notes
                "n",  # Background sync
                "n",  # First sync
            ]
        )

        outputs = []
        result = run_wizard(
            input_func=lambda prompt="": next(inputs),
            print_func=lambda *args: outputs.append(" ".join(str(a) for a in args)),
            repo_dir=tmp_path,
            bin_dir=tmp_path / "bin",
        )

        assert result.saved is True
        assert result.run_sync_requested is False
        saved_config = tmp_path / "config" / "config.yml"
        assert saved_config.exists()
        cfg = yaml.safe_load(saved_config.read_text(encoding="utf-8"))
        assert cfg["remarkable"]["preferred_connection"] == "ssh"
        assert cfg["remarkable"]["use_ssh"] is True
        assert cfg["remarkable"]["device_token"] == "existing-token"
