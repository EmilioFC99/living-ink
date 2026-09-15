# Living Ink

**Automate the flow of your "Living Signal" from reMarkable to Apple Notes or Obsidian.**

Living Ink is an automated pipeline that bridges the gap between your reMarkable tablet and your digital "Second Brain" (Apple Notes or Obsidian). It goes beyond simple PDF export by converting your handwritten notebooks into fully searchable, typed text while preserving the original context and folder structure.

## 🚀 Features

*   **Smart Sync**: Automatically detects new or updated notebooks on your reMarkable.
*   **AI Vision OCR (Single API Key)**: Uses multimodal AI models (**Google Gemini**, **OpenAI GPT-4o**) to read handwritten pages directly and format them into clean text in a single step — **no Google Cloud Console or service accounts needed**.
*   **Multi-Provider AI**: Supports:
    *   **Google Gemini** (recommended: fast, free tier, vision OCR + text repair)
    *   **OpenAI** (GPT-4o / GPT-4o-mini with vision OCR)
    *   **Ollama** (100% local, no API key needed)
    *   **Groq, OpenRouter, Mistral, Together AI**, or any custom OpenAI-compatible endpoint
    *   **Google Cloud Vision** as an optional traditional OCR fallback
    *   Option to disable AI cleanup entirely for raw OCR text
*   **Apple Notes Integration**: Creates formatted notes containing cleaned text and original handwritten page images.
*   **Obsidian Integration**: Exports notes as Markdown files with YAML frontmatter, WikiLinked page attachments, and **full folder hierarchy mirroring** inside your vault (or a configurable root folder).
*   **Folder Mirroring**: Replicates your exact reMarkable folder structure (e.g., `Finance/2026/Q1/Budget` → `Living Ink/Finance/2026/Q1/Budget.md`).

## 📚 Documentation

*   **[Setup Guide](docs/SETUP_GUIDE.md)**: How to get your API keys (Google Gemini / OpenAI, Google Cloud Vision, reMarkable) and configure the app.
*   **[User Manual](docs/USER_MANUAL.md)**: How to use the application in Automatic or Manual modes.
*   **[AGENTS.md](AGENTS.md)**: Architecture and contributor guide for AI coding assistants.

## 🛠️ Installation & Config

1.  **Clone and Install Dependencies**:
    ```bash
    git clone https://github.com/EmilioFC99/living-ink.git
    cd living-ink
    uv sync --all-extras
    ```

2.  **Run the Interactive Setup Wizard (Recommended)**:
    ```bash
    uv run python scripts/setup.py
    ```
    The wizard will automatically pair your tablet, verify your AI key live, detect your Obsidian vaults, and choose your destination folders!

    *(Alternatively, you can manually copy `config.yml.example` to `config/config.yml` and edit it).*
    ```yaml
    # 1. AI Provider (e.g. Google Gemini, OpenAI, Ollama, etc.)
    ai:
      provider: "gemini"               # "gemini", "openai", "ollama", "groq", "none", etc.
      api_key: "AIzaSy..."             # From https://aistudio.google.com/apikey
      # model: "gemini-2.0-flash"      # Optional: sensible default picked per provider

    # 2. reMarkable Cloud
    remarkable:
      device_token: "YOUR-DEVICE-TOKEN-HERE"

    # 3. Google Cloud Vision (Optional fallback — not needed if using Gemini or OpenAI)
    # google_vision:
    #   credentials_path: "/path/to/credentials.json"

    # 4. Destination: Apple Notes
    apple_notes:
      enabled: true
      folder_name: "Living Ink"

    # 5. Destination: Obsidian
    obsidian:
      enabled: true
      vault_path: "/Users/yourname/Documents/Obsidian Vault"
      root_folder: "Living Ink"        # Optional: prefix folder (leave empty for vault root)
      mirror_folders: true             # Replicate reMarkable folder tree
      attachments_folder: "attachments"
    ```
    *(See `config.yml.example` for all options and presets)*

3.  **Run the Pipeline**:
    ```bash
    uv run python scripts/process_notebook.py
    ```

## 🏗️ How It Works

1.  **Download**: Fetches modified notebooks from reMarkable Cloud (or USB SSH).
2.  **Render**: Converts vector strokes into high-resolution white-background PNG images.
3.  **Read**: Google Cloud Vision extracts raw text from handwriting.
4.  **Refine**: Your configured AI provider cleans up OCR artifacts and structures paragraphs.
5.  **Publish**: Dispatches structured notes and page images to your enabled destinations.

## 🧪 Running Tests

```bash
uv run pytest -v
```

## Acknowledgements

Built on top of the generic [remarkable-mcp](https://github.com/SamMorrowDrums/remarkable-mcp) project.
