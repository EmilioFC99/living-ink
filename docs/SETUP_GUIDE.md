# Setup Guide for Living Ink

This guide explains how to configure the necessary API keys and credentials for the Living Ink application (syncing reMarkable notes to Apple Notes and/or Obsidian).

---

## 1. AI Text Cleanup Provider (Required for Text Polish)

The application uses an AI model to clean up OCR misinterpretations, correct typos, and format notes into readable paragraphs. You can use **Google Gemini**, **OpenAI**, local **Ollama**, or other OpenAI-compatible providers.

### Option A: Google Gemini (Recommended — Free & Fast)

1. **Get Free API Key**:
   * Visit [Google AI Studio](https://aistudio.google.com/apikey).
   * Sign in with your Google account.
   * Click **Create API key** (or **Get API key**).
   * Copy the generated key (starts with `AIzaSy...`).
2. **Configure**:
   * In `config/config.yml`:
     ```yaml
     ai:
       provider: "gemini"
       api_key: "AIzaSy..."
       model: "gemini-2.0-flash"  # Default
     ```

### Option B: OpenAI

1. **Get API Key**:
   * Go to [platform.openai.com](https://platform.openai.com) and sign in.
   * Navigate to **Dashboard** -> **API keys**.
   * Click **+ Create new secret key** and copy it immediately.
2. **Configure**:
   * In `config/config.yml`:
     ```yaml
     ai:
       provider: "openai"
       api_key: "sk-proj-..."
       model: "gpt-4o-mini"       # Default
     ```

### Option C: Ollama (100% Local — Free & Private)

1. Install [Ollama](https://ollama.com).
2. Download a model: `ollama pull llama3.2`
3. Configure:
   ```yaml
   ai:
     provider: "ollama"
     # No API key needed!
   ```

### Option D: None (Raw OCR Only)

To bypass AI cleanup entirely and keep raw Google Vision OCR text:
```yaml
ai:
  provider: "none"
```

---

## 2. Handwriting OCR (AI Vision vs Google Cloud Vision)

Living Ink supports two ways to recognize handwritten notes:

### Option A: AI Vision OCR (Recommended — Zero Extra Setup)
If you configure an AI provider with vision capabilities (**Google Gemini** or **OpenAI**), you **do not need Google Cloud Vision at all**.
- Uses the **exact same API key** you configured in Step 1.
- Gemini 2.0 Flash reads handwriting directly from page images with high accuracy.
- Performs handwriting transcription and text cleanup in a single step.
- No Google Cloud Console, no billing accounts, no service account JSON files.

Simply leave `google_vision.credentials_path` empty in `config/config.yml`.

### Option B: Google Cloud Vision API (Optional Fallback)
If you prefer traditional Google Cloud Vision OCR, or use an AI provider without vision (or `provider: "none"`):

1. **Create Project**:
   * Go to the [Google Cloud Console](https://console.cloud.google.com/).
   * Click the project dropdown (top left) and select **New Project**.
   * Name it "Remarkable OCR" and create it.
2. **Enable API**:
   * Search for "Cloud Vision API" and click **Enable**.
3. **Create Service Account**:
   * Go to **IAM & Admin** -> **Service Accounts** -> **+ Create Service Account**.
   * Assign role **Cloud Vision API User**.
4. **Download Key**:
   * Under the service account's **Keys** tab, click **Add Key** -> **Create new key** (JSON).
5. **Configuration**:
   * In `config/config.yml`:
     ```yaml
     google_vision:
       credentials_path: "/Users/yourname/path/to/my-credentials.json"
     ```

---

## 3. reMarkable Tablet Connection

Living Ink can sync either via the official reMarkable Cloud or directly over USB via SSH.

### Option A: reMarkable Cloud (Recommended)
1. **Get One-Time Code**:
   * Go to [my.remarkable.com/device/desktop/connect](https://my.remarkable.com/device/desktop/connect).
   * Log in and click **Connect a new device** -> **Desktop**.
   * Copy the 8-letter pairing code.
2. **Pair Automatically via Setup Wizard**:
   * Run `living-ink setup` and paste your 8-letter code when prompted.
   * Or manually generate your token:
     ```bash
     uv run python -c "from remarkable_mcp.api import register_and_get_token; print(register_and_get_token('<YOUR-8-LETTER-CODE>'))"
     ```
3. **Configure**:
   * Paste the token in `config/config.yml` under `remarkable.device_token`.

### Option B: Direct USB SSH (Offline / No Cloud Subscription)
If you prefer not to use reMarkable Cloud or don't have a Connect subscription:
1. **Enable Developer Mode**:
   * On your reMarkable tablet, navigate to **Settings → General → Software → Developer mode**.
   * Note the root password displayed on screen.
2. **Connect via USB**:
   * Connect your tablet to your computer using a USB-C cable (default IP: `10.11.99.1`).
   * Verify access: `ssh root@10.11.99.1`
3. **Configure**:
   * Set the environment variable: `export REMARKABLE_USE_SSH=true` (or configure SSH key authentication).

---

## 4. Destinations Configuration

You can enable Apple Notes, Obsidian, or both at the same time:

### Apple Notes
```yaml
apple_notes:
  enabled: true
  folder_name: "Living Ink"  # Top-level folder created in Apple Notes
```

### Obsidian
```yaml
obsidian:
  enabled: true
  vault_path: "/Users/yourname/Documents/Obsidian Vault"
  root_folder: "Living Ink"  # Folder inside vault (leave empty for vault root)
  mirror_folders: true       # Replicates full reMarkable folder tree
  attachments_folder: "attachments"
```
