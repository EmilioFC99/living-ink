# Copilot Instructions for Living Ink

## Project Overview

**Living Ink** is a production-grade automated pipeline that syncs handwritten notebooks from a **reMarkable tablet** to digital note-taking apps (**Apple Notes** and **Obsidian**). It connects to the tablet via USB SSH or reMarkable Cloud, renders pages to images, performs OCR and text structuring via multimodal LLMs (Gemini, OpenAI, Ollama, etc.), and publishes structured notes.

- **Language**: Python 3.10+
- **Package Manager**: [uv](https://docs.astral.sh/uv/) (never pip)
- **Build System**: Hatchling (`src/` layout)
- **Linter/Formatter**: Ruff
- **Test Runner**: pytest

## Architecture & Layout

```
living-ink/
├── src/living_ink/              # Core Python package
│   ├── __init__.py              # Package init, version
│   ├── __main__.py              # python -m living_ink entry point
│   ├── api.py                   # reMarkable Cloud/SSH API client factory
│   ├── cli.py                   # Main CLI entry point (`living-ink`)
│   ├── config.py                # XDG path resolution & configuration helpers
│   ├── pipeline.py              # Main sync pipeline orchestrator
│   ├── sync.py                  # Cloud sync protocol (v3/v4) implementation
│   ├── ssh.py                   # Direct USB SSH transport to tablet
│   ├── extract.py               # .rm binary → SVG → PNG rendering
│   ├── clean.py                 # Multimodal AI vision OCR & text cleanup
│   ├── providers.py             # Multi-provider AI interface (Gemini, OpenAI, Ollama, etc.)
│   ├── setup_wizard.py          # Interactive onboarding setup wizard
│   ├── destinations.py          # Pluggable publish targets (Apple Notes, Obsidian)
│   ├── ocr_prompt.txt           # System prompt for vision OCR
│   └── openai_cleanup_prompt.txt# System prompt for text repair
├── scripts/
│   ├── process_notebook.py      # Backwards-compatible proxy to living_ink.pipeline
│   ├── cli.py                   # CLI wrapper entry point
│   └── setup.py                 # Setup wizard runner
├── tests/                       # Complete pytest suite
├── docs/                        # User and developer documentation
│   ├── SETUP_GUIDE.md           # API key / credential setup
│   └── USER_MANUAL.md           # End-user usage guide
├── config/
│   └── config.yml.example       # Configuration template
├── pyproject.toml               # Project metadata, Hatchling config, and dependencies
├── install.sh                   # One-line installer
├── Dockerfile                   # Multi-stage production container image
└── docker-compose.yml           # Compose file (CLI & background daemon)
```

## Stateless Repository & XDG Standards

The repository is completely stateless:
- **User Config**: Resolves to `~/.config/living-ink/config.yml` (overridable via `LIVING_INK_CONFIG` or `LIVING_INK_CONFIG_DIR`).
- **Runtime Data**: Resolves to `~/.local/share/living-ink/` (overridable via `LIVING_INK_DATA_DIR`).
- Personal tokens, credentials, and downloaded notebooks must NEVER be committed to git.

## Package Management

**Always use `uv` for all package management operations.**

```bash
# Install dependencies
uv sync --all-extras

# Add a dependency
uv add <package>

# Add a dev dependency
uv add --dev <package>
```

## Running Tests & Quality Checks

**Before committing, always run:**

```bash
# 1. Lint code
uv run ruff check .

# 2. Check formatting
uv run ruff format --check .

# 3. Run test suite
uv run pytest -v
```

## Running the Application

```bash
# Run CLI via uv
uv run living-ink --help
uv run living-ink status
uv run living-ink sync

# Or run as module
uv run python -m living_ink status
```
