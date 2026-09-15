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

## 2. Google Cloud Vision API (Required for Handwriting OCR)

We use Google's Cloud Vision API for handwriting recognition because it is significantly more accurate for handwritten notes than on-device models.

1. **Create Project**:
   * Go to the [Google Cloud Console](https://console.cloud.google.com/).
   * Click the project dropdown (top left) and select **New Project**.
   * Name it "Remarkable OCR" and create it.
2. **Enable API**:
   * In the search bar, type "Cloud Vision API".
   * Select **Cloud Vision API** from the marketplace results.
   * Click **Enable**.
   * *Note: Google Cloud includes a free tier of 1,000 units/month.*
3. **Create Service Account**:
   * Go to **IAM & Admin** -> **Service Accounts**.
   * Click **+ Create Service Account**.
   * Name: `remarkable-ocr-sa`.
   * Description: "OCR for remarkable sync".
   * Click **Create and Continue**.
   * **Role**: Select **Cloud Vision API User** (or **Basic** -> **Viewer**).
   * Click **Done**.
4. **Download Key**:
   * Click on the newly created service account email.
   * Go to the **Keys** tab.
   * Click **Add Key** -> **Create new key**.
   * Select **JSON**.
   * A `.json` file will download to your computer.
5. **Configuration**:
   * In `config/config.yml`, set `credentials_path` to the absolute path of your downloaded JSON file:
     ```yaml
     google_vision:
       credentials_path: "/Users/yourname/path/to/my-credentials.json"
     ```

---

## 3. reMarkable Tablet Connection

The application downloads your notebooks from the official reMarkable cloud.

1. **Get One-Time Code**:
   * Go to [my.remarkable.com/device/desktop/connect](https://my.remarkable.com/device/desktop/connect).
   * Log in and click **Connect a new device** -> **Desktop**.
   * Copy the 8-letter pairing code.
2. **Generate Device Token**:
   * Run the command:
     ```bash
     uv run python -c "from remarkable_mcp.api import register_and_get_token; print(register_and_get_token('<YOUR-8-LETTER-CODE>'))"
     ```
   * Or if you previously used `rmapi`, your token in `~/.rmapi` will be detected automatically.
3. **Configure**:
   * Paste the token in `config/config.yml` under `remarkable.device_token`.

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
