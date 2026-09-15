# Validation & Test Plan

This document outlines how to validate the Living Ink refactoring, ensuring that the new Obsidian integration works correctly and that existing Apple Notes functionality is preserved.

## 1. Environment Setup

Before running tests, ensure you have the correct dependencies and a valid configuration.

```bash
# Install dependencies (if not already done)
uv sync

# Create a config file from the example
cp config/config.yml.example config/config.yml
```

Edit `config/config.yml` with your API keys:
*   `remarkable.device_token`
*   `openai.api_key`
*   `google_vision` credentials

## 2. Test Cases

### Test Case A: Regression Test - Apple Notes Only
**Goal**: Verify that the refactoring hasn't broken the original functionality.

1.  **Configure `config.yml`**:
    ```yaml
    apple_notes:
      enabled: true
      folder_name: "Test-AppleOnly"
    obsidian:
      enabled: false
    ```
2.  **Run Sync**:
    ```bash
    living-ink sync --limit 1
    ```
3.  **Verification**:
    *   [ ] Open Apple Notes.
    *   [ ] Check for folder "Test-AppleOnly".
    *   [ ] Verify the note was created.
    *   [ ] Verify text is formatted (HTML).
    *   [ ] Verify original page images are attached.

### Test Case B: New Feature - Obsidian Only
**Goal**: Verify that Markdown and attachments are generated correctly.

1.  **Prepare a Test Vault**:
    *   Create a temporary folder: `mkdir -p ~/Documents/TestVault`
2.  **Configure `config.yml`**:
    ```yaml
    apple_notes:
      enabled: false
    obsidian:
      enabled: true
      vault_path: "~/Documents/TestVault"
    ```
3.  **Run Sync**:
    ```bash
    living-ink sync --limit 1
    ```
4.  **Verification**:
    *   [ ] Check `~/Documents/TestVault`.
    *   [ ] Verify `.md` file exists.
    *   [ ] Open the `.md` file.
    *   [ ] Verify specific Markdown formatting (Assignments, checkboxes).
    *   [ ] Verify `attachments/` folder exists.
    *   [ ] Verify images are inside `attachments/`.
    *   [ ] Verify existing image links in the Markdown work (`![[image.png]]`).

### Test Case C: Dual Sync
**Goal**: Verify that both destinations can be targeted simultaneously.

1.  **Configure `config.yml`**:
    ```yaml
    apple_notes:
      enabled: true
      folder_name: "Test-Dual"
    obsidian:
      enabled: true
      vault_path: "~/Documents/TestVault"
    ```
2.  **Run Sync**:
    ```bash
    living-ink sync --limit 1
    ```
3.  **Verification**:
    *   [ ] Verify note appears in Apple Notes ("Test-Dual").
    *   [ ] Verify note appears in Obsidian TestVault.

## 3. Troubleshooting

**Logs**:
Check `logs/pipeline.log` for detailed execution creation.

**Common Errors**:
*   `Vault path does not exist`: Ensure you created the test vault folder manually before running.
*   `AppleScript error`: Ensure Apple Notes is installed and you have granted Terminal/VSCode permission to control it (System Settings > Privacy > Automation).
