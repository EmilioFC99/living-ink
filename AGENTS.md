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
├── remarkable_mcp/              # Core Python package
│   ├── __init__.py              # Package init, version
│   ├── api.py                   # reMarkable Cloud/SSH API client factory
│   ├── sync.py                  # Cloud sync protocol (v3/v4) implementation
│   ├── ssh.py                   # Direct USB SSH transport to tablet
│   ├── extract.py               # .rm binary → SVG → PNG rendering
│   ├── clean.py                 # LLM-based OCR text cleanup (⚠️ OpenAI-hardcoded)
│   ├── destinations.py          # Pluggable publish targets (Apple Notes, Obsidian)
│   └── openai_cleanup_prompt.txt # System prompt for text repair
├── scripts/
│   ├── process_notebook.py      # Main pipeline orchestrator (entry point)
│   ├── cli.py                   # CLI wrapper entry point
│   ├── setup.py                 # Setup wizard runner
│   ├── test_docker.sh           # Automated Docker smoke test suite
│   ├── set_env.sh               # Environment variable helper
│   └── white_background.py      # Standalone image background tool
├── docs/                        # User and developer documentation
│   ├── SETUP_GUIDE.md           # API key / credential setup
│   ├── USER_MANUAL.md           # End-user usage guide
│   ├── REFACTOR_PLAN.md         # Architecture refactoring roadmap
│   ├── PARKING_LOT.md           # Known bugs and open questions
│   ├── future-plans.md          # Upstream feature ideas
│   ├── execution_plan_folder_mapping.md
│   ├── development.md           # Dev environment setup
│   └── ...                      # Additional reference docs
├── config/
│   └── config.yml.example       # Configuration template
├── data/                        # Runtime & personal data (logs, images, PDFs, state cache)
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

- **Destination ABC**: `remarkable_mcp/destinations.py` defines `Destination` base class. New targets subclass it and implement `publish()`.
- **Config Loading**: YAML-first (`config/config.yml`), with env var overrides, and legacy `config.py`/`.env` fallback. Config values are pushed into `os.environ` at startup.
- **State Tracking**: Per-destination JSON files (`processed_notebooks_{DestName}.json`) track notebook hash/version to avoid reprocessing.
- **Folder Mirroring**: Traverses parent UUID chain from reMarkable metadata, but currently **flattens to top-level only** (e.g., `Work/Projects/Q1` → just `Work`).

## Key Configuration

| Source | Key | Description |
|--------|-----|-------------|
| `config.yml` | `openai.api_key` | LLM API key for text cleanup |
| `config.yml` | `remarkable.device_token` | reMarkable Cloud auth token |
| `config.yml` | `google_vision.credentials_path` | Google Cloud Vision service account |
| `config.yml` | `obsidian.enabled` / `obsidian.vault_path` | Obsidian destination toggle |
| `config.yml` | `apple_notes.enabled` / `apple_notes.folder_name` | Apple Notes destination toggle |
| env var | `OPENAI_REPAIR_MODEL` | Model name (default: `gpt-4o-mini`) |
| env var | `ENABLE_REPAIR` | Toggle LLM cleanup (`true`/`false`) |
| env var | `REMARKABLE_USE_SSH` | Use USB SSH instead of Cloud |

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
2. **Multi-Provider AI**: `remarkable_mcp/providers.py` provides universal OpenAI-compatible completions supporting Google Gemini, OpenAI, Ollama, Groq, OpenRouter, Mistral, Together, and custom endpoints, plus raw OCR mode (`none`).
3. **Full Folder Hierarchy Mirroring**: `remarkable_mcp/destinations.py` replicates complete reMarkable nested folders into Obsidian (`root_folder` and `mirror_folders` options supported).
4. **Comprehensive Test Suite**: `tests/` contains 158 unit tests covering providers, vision OCR, clean, and destination logic.
5. **Google Docstrings**: All core modules follow Google docstring conventions.

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
