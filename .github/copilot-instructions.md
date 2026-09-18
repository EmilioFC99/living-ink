# Copilot Instructions for Living Ink

## Project Overview

**Living Ink** syncs handwritten notebooks from a **reMarkable tablet** to **Apple Notes** and **Obsidian**. It connects over USB SSH or reMarkable Cloud, renders pages to images, transcribes them with a multimodal LLM (Gemini, OpenAI, Ollama, …), and publishes structured notes.

- **Language**: Python 3.10+
- **Package Manager**: [uv](https://docs.astral.sh/uv/) (never pip)
- **Build System**: Hatchling (`src/` layout)
- **Linter/Formatter**: Ruff
- **Test Runner**: pytest

## Architecture & Layout

```
living-ink/
├── src/living_ink/
│   ├── __main__.py              # python -m living_ink entry point
│   ├── cli.py                   # CLI entry point (`living-ink`): sync | watch | setup | status
│   ├── pipeline.py              # SyncPipeline orchestrator + processing stages
│   ├── settings.py              # Settings: resolved, typed configuration
│   ├── config.py                # XDG path resolution & config helpers
│   ├── models.py                # Shared data models
│   ├── transport.py             # RemarkableTransport Protocol
│   ├── api.py                   # Client factory + automatic fallback
│   ├── sync.py                  # Cloud sync protocol (v3/v4)
│   ├── ssh.py                   # USB SSH transport
│   ├── extract.py               # .rm → SVG → PNG, PDF/EPUB handling
│   ├── clean.py                 # Vision OCR & text cleanup
│   ├── providers.py             # AI providers (presets + registry)
│   ├── destinations.py          # Publish targets (ABC + registry)
│   ├── setup_wizard.py          # Interactive onboarding
│   ├── ocr_prompt.txt           # System prompt for vision OCR
│   ├── openai_cleanup_prompt.txt# System prompt for text repair
│   └── cleanup_prompt.txt       # System prompt for local cleanup
├── tests/                       # pytest suite + test_docker.sh smoke tests
├── docs/                        # SETUP_GUIDE.md, USER_MANUAL.md, TEST_PLAN.md
├── pyproject.toml               # Project metadata, Hatchling config, dependencies
├── install.sh                   # One-line installer
├── Dockerfile                   # Multi-stage production container image
└── docker-compose.yml           # Compose file (CLI & background daemon)
```

Deeper architectural notes — the transport Protocol, the destination and provider registries, and the notebook processing stages — live in `AGENTS.md`.

## Stateless Repository & XDG Standards

The repository is completely stateless:
- **User Config**: Resolves to `~/.config/living-ink/config.yml` (overridable via `LIVING_INK_CONFIG` or `LIVING_INK_CONFIG_DIR`).
- **Runtime Data**: Resolves to `~/.local/share/living-ink/` (overridable via `LIVING_INK_DATA_DIR`).
- Personal tokens, credentials, and downloaded notebooks must NEVER be committed to git.

## Configuration

Settings are resolved once by `settings.Settings.resolve(config)`, which merges `config.yml` with the environment into one frozen typed object. Precedence is **CLI options > env var > config file > default**. A new setting means one field on `Settings`, one entry in `FIELD_ENV_VARS`, and one line in `resolve()`; do not write settings back into `os.environ`. `living-ink status` prints every resolved setting alongside the layer that supplied it, via `Settings.explain()`.

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

## Conventions

- Google docstrings on all modules, classes, and functions.
- Feature branches: `feat/<description>`, bug fixes: `fix/<description>`.
- Preserve existing comments and docstrings in code you are not modifying.
