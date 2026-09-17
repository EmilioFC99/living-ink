# AGENTS.md — Living Ink

> AI coding agent guide for the Living Ink codebase.

## Project Overview

**Living Ink** is an automated pipeline that syncs handwritten notebooks from a **reMarkable tablet** to digital note-taking apps (**Apple Notes** and **Obsidian**). It downloads notebooks from reMarkable Cloud (or via USB SSH), renders pages to images, performs OCR via Google Cloud Vision, cleans the text with an LLM, and publishes structured notes to configured destinations.

- **Language**: Python 3.10+
- **Package Manager**: [uv](https://docs.astral.sh/uv/) (not pip)
- **Build System**: Hatchling
- **Linter/Formatter**: Ruff
- **License**: MIT

## Repository Structure

```
living-ink/
├── src/
│   └── living_ink/              # Core Python package
│       ├── __init__.py          # Package init, version
│       ├── __main__.py          # Module entry point (`python -m living_ink`)
│       ├── api.py               # reMarkable Cloud/SSH API client factory
│       ├── cli.py               # Main CLI entry point (`living-ink`)
│       ├── config.py            # XDG path resolution & configuration helpers
│       ├── pipeline.py          # Main sync pipeline orchestrator
│       ├── sync.py              # Cloud sync protocol (v3/v4) implementation
│       ├── ssh.py               # Direct USB SSH transport to tablet
│       ├── extract.py           # .rm binary → SVG → PNG rendering
│       ├── clean.py             # Multimodal AI vision OCR & text cleanup
│       ├── providers.py         # Multi-provider AI interface (Gemini, OpenAI, Ollama, etc.)
│       ├── setup_wizard.py      # Interactive onboarding setup wizard
│       └── destinations.py      # Pluggable publish targets (Apple Notes, Obsidian)
├── scripts/
│   ├── process_notebook.py      # Backwards-compatible proxy to living_ink.pipeline
│   ├── cli.py                   # CLI wrapper entry point
│   ├── setup.py                 # Setup wizard runner
│   ├── test_docker.sh           # Automated Docker smoke test suite
│   ├── set_env.sh               # Environment variable helper
│   └── white_background.py      # Standalone image background tool
├── docs/                        # User and developer documentation
│   ├── SETUP_GUIDE.md           # API key / credential setup
│   ├── USER_MANUAL.md           # End-user usage guide
│   └── TEST_PLAN.md             # Test cases and verification matrix
├── pyproject.toml               # Python project metadata & deps
├── install.sh                   # One-line curl installer
├── Dockerfile                   # Multi-stage production container image
└── docker-compose.yml           # Compose file (CLI & background daemon)
```

## Architecture

### Pipeline Flow

```
reMarkable Tablet
    ↓ (Cloud API or USB SSH)
Download .rm notebook zip
    ↓
Render pages: .rm → SVG → PNG (white background)
    ↓
OCR & Text Processing:
    ├── AI Vision OCR (Default: Gemini, GPT-4o — reads handwriting + cleans in 1 step)
    └── Google Cloud Vision (Optional fallback: DOCUMENT_TEXT_DETECTION → AI cleanup)
    ↓
Publish: Destination.publish()
    ├── AppleNotesDestination (via osascript/AppleScript)
    └── ObsidianDestination (Markdown + YAML frontmatter + WikiLinks)
```

### Key Design Patterns

- **Destination ABC**: `src/living_ink/destinations.py` defines `Destination` base class. New targets subclass it and implement `publish()`.
- **Config Loading**: YAML-first via `living_ink.config` (checks env, repo, XDG `~/.config/living-ink/config.yml`), with env var overrides.
- **State Tracking**: Per-destination JSON files (`processed_notebooks_{DestName}.json`) track notebook hash/version to avoid reprocessing.
- **Folder Mirroring**: Full folder hierarchy mirroring supported in Obsidian; top-level flattening applied in Apple Notes.

## Key Configuration

| Source | Key | Description |
|--------|-----|-------------|
| `config.yml` | `ai.provider` / `ai.api_key` | LLM provider preset & API key for text cleanup |
| `config.yml` | `remarkable.preferred_connection` | Preferred connection method (`ssh` or `cloud`) |
| `config.yml` | `remarkable.use_ssh` | Enable USB SSH connection (`true`/`false`) |
| `config.yml` | `remarkable.ssh_host` / `remarkable.ssh_port` | SSH connection parameters (passwordless auth) |
| `config.yml` | `remarkable.device_token` | reMarkable Cloud auth token |
| `config.yml` | `google_vision.credentials_path` | Google Cloud Vision service account |
| `config.yml` | `obsidian.enabled` / `obsidian.vault_path` | Obsidian destination toggle |
| `config.yml` | `apple_notes.enabled` / `apple_notes.folder_name` | Apple Notes destination toggle |
| env var | `REMARKABLE_PREFERRED_CONNECTION` | Override preferred method (`ssh` or `cloud`) |
| env var | `REMARKABLE_USE_SSH` | Use USB SSH instead of Cloud (`true`/`false`) |
| env var | `REMARKABLE_SSH_HOST` / `REMARKABLE_SSH_PORT` | SSH connection overrides |
| env var | `OPENAI_REPAIR_MODEL` | Model name override |
| env var | `ENABLE_REPAIR` | Toggle LLM cleanup (`true`/`false`) |

## Development Commands

```bash
# Install dependencies
uv sync --all-extras

# Run the sync pipeline
uv run python scripts/process_notebook.py

# Lint
uv run ruff check .

# Format
uv run ruff format .

# Tests
uv run pytest -v
```

## Recent Architecture Improvements

1. **AI Vision OCR (Single API Key)**: Multimodal AI models (Gemini, GPT-4o) perform handwriting OCR directly from page images via standard OpenAI-compatible `image_url` data URIs, combining OCR + text cleanup in one step and making Google Cloud Vision optional.
2. **Multi-Provider AI**: `living_ink/providers.py` provides universal OpenAI-compatible completions supporting Google Gemini, OpenAI, Ollama, Groq, OpenRouter, Mistral, Together, and custom endpoints, plus raw OCR mode (`none`).
3. **Full Folder Hierarchy Mirroring**: `living_ink/destinations.py` replicates complete reMarkable nested folders into Obsidian (`root_folder` and `mirror_folders` options supported).
4. **Comprehensive Test Suite**: `tests/` contains 201 unit tests covering providers, vision OCR, clean, destination logic, SSH, and CLI.
5. **Google Docstrings**: All core modules follow Google docstring conventions.
6. **Preferred Connection with Automatic Fallback**: Users select their preferred method in setup wizard (USB SSH or Cloud). `FallbackClient` automatically tries the preferred method first and seamlessly falls back to the secondary method if the primary is unavailable. CLI flags `--ssh` and `--cloud` allow forcing either method.

## Known Issues & Tech Debt

- None currently tracking. Previous tech debt (package naming, single-provider lock-in, folder flattening) resolved.

## Conventions

- Always use `uv` for package management, never raw `pip`.
- Run `uv run ruff check . && uv run ruff format --check .` before committing.
- Run `uv run pytest -v` before committing.
- Follow Google docstrings format for all functions, classes, and modules.
- Feature branches: `feat/<description>`, bug fixes: `fix/<description>`.
- Preserve existing comments and docstrings in code you don't modify.
- Configuration should support both YAML (`config.yml`) and environment variables.
- New destinations must subclass `Destination` ABC in `destinations.py`.
