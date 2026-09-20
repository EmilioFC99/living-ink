# Living Ink

**Automate the flow of your "Living Signal" from reMarkable to Obsidian.**

Living Ink is an automated pipeline that bridges the gap between your reMarkable tablet and your digital "Second Brain" (Obsidian). It goes beyond simple PDF export by converting your handwritten notebooks into fully searchable, typed text while preserving the original context and folder structure.

## 🚀 Features

*   **Smart Sync**: Automatically detects new or updated notebooks on your reMarkable.
*   **AI Vision OCR (Single API Key)**: Uses multimodal AI models (**Google Gemini**, **OpenAI GPT-4o**) to read handwritten pages directly and format them into clean text in a single step — **no Google Cloud Console or service accounts needed**.
*   **Multi-Provider AI**: Supports:
    *   **Google Gemini** (recommended: fast, free tier, vision OCR + text repair)
    *   **OpenAI** (GPT-4o / GPT-4o-mini with vision OCR)
    *   **Ollama** (100% local, no API key needed)
    *   **Groq, OpenRouter, Mistral, Together AI**, or any custom OpenAI-compatible endpoint
    *   Option to disable AI cleanup entirely for raw OCR text
*   **Obsidian Integration**: Exports notes as Markdown files with YAML frontmatter, WikiLinked page attachments, and **full folder hierarchy mirroring** inside your vault (or a configurable root folder).
*   **Folder Mirroring**: Replicates your exact reMarkable folder structure (e.g., `Finance/2026/Q1/Budget` → `Living Ink/Finance/2026/Q1/Budget.md`).

## 📚 Documentation

*   **[Setup Guide](docs/SETUP_GUIDE.md)**: How to get your API keys (Google Gemini / OpenAI, reMarkable) and configure the app.
*   **[User Manual](docs/USER_MANUAL.md)**: How to use the application in Automatic or Manual modes.
*   **[AGENTS.md](AGENTS.md)**: Architecture and contributor guide for AI coding assistants.

## ⚡ Quick Install (Recommended)

### Option A: One-Line Installer
Paste this command into your Terminal to install everything and launch the setup wizard:

```bash
curl -fsSL https://raw.githubusercontent.com/EmilioFC99/living-ink/main/install.sh | bash
```

### Option B: Using `uv tool`
If you already have [uv](https://docs.astral.sh/uv/) installed:

```bash
uv tool install "git+https://github.com/EmilioFC99/living-ink.git"
living-ink setup
```

Once installed, you can use the global `living-ink` command from anywhere:
```bash
living-ink           # Sync notes (or runs setup if unconfigured)
living-ink sync      # Run the sync pipeline
living-ink watch     # Keep syncing on the schedule in config.yml until you stop it
living-ink setup     # Re-run interactive setup wizard
living-ink info      # Check tablet, AI, vault, caches, schedule, and sync state
living-ink config    # Change any setting, edit the prompts, clear the caches
living-ink uninstall # Remove the background job, settings and caches
```

`info` reads; `config` writes. Every setting `info` prints appears in the `config`
menu under the same name, nothing is written until you confirm the summary, and
your API keys stay out of `config.yml` — they live in a `0600` file of their own.

Automatic syncing is one cron expression in `config.yml` (`config` → Watch offers
the common ones and shows you the next three times each would fire). `watch` reads
it, and on macOS `setup` installs a LaunchAgent whose only job is keeping `watch`
alive — so the schedule is somewhere you can read and change it, not buried in a
plist. `info` says when the last scheduled sync ran and when the next one is due,
and puts a banner above everything if one was missed.

`uninstall` removes the background job and the caches, then asks separately before
removing your settings, credentials and sync record. **It never touches your notes,
under any flag** — pass `--yes` to answer every question with yes.

---

## 🛠️ Manual Installation (Development)

If you are developing or prefer a local clone:

1.  **Clone and Install Dependencies**:
    ```bash
    git clone https://github.com/EmilioFC99/living-ink.git
    cd living-ink
    uv sync --all-extras
    ```

2.  **Run the Interactive Setup Wizard**:
    ```bash
    uv run living-ink setup
    ```

    The guided wizard tests your tablet connection (USB SSH or Cloud), verifies your AI API key live, detects your Obsidian vault, and saves your configuration to `~/.config/living-ink/config.yml`.

3.  **Run the Pipeline**:
    ```bash
    uv run living-ink sync
    ```

## 🏗️ How It Works

1.  **Download**: Fetches modified notebooks from reMarkable Cloud (or USB SSH).
2.  **Render**: Converts vector strokes into high-resolution white-background PNG images.
3.  **Read**: Your configured AI provider reads the handwriting and returns clean, structured text — one multimodal call per page, no separate OCR service.
4.  **Publish**: Dispatches structured notes and page images to your enabled destinations.

## 🧪 Running Tests

```bash
uv run pytest -v
```

## 🐳 Docker & DevOps

Living Ink includes a production-grade container image (non-root user, multi-stage `uv` build, Cairo runtime dependencies) and `docker-compose.yml` for headless servers, NAS, or containerized testing:

### Quick CLI via Docker Compose
```bash
# Check status
docker compose run --rm living-ink info

# Run notebook sync
docker compose run --rm living-ink sync
```

### 24/7 Background Sync Daemon
```bash
# Sync on a schedule (every 30 minutes by default; set WATCH_SCHEDULE to any
# five-field cron expression, and WATCH_TIMEZONE to the zone to read it in)
docker compose --profile daemon up -d
```

### Run Clean Docker Smoke Tests
```bash
# Test build, CLI entrypoint, unconfigured status, and live sync against an ephemeral test vault
./tests/test_docker.sh
```

## Acknowledgements

Built on top of the generic [remarkable-mcp](https://github.com/SamMorrowDrums/remarkable-mcp) project.
