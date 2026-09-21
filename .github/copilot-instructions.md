# Copilot Instructions for Living Ink

## Project Overview

**Living Ink** syncs handwritten notebooks from a **reMarkable tablet** to an **Obsidian vault**. It connects over USB SSH or reMarkable Cloud, renders pages to images, transcribes them with a multimodal LLM (Gemini, OpenAI, Ollama, …), and publishes structured Markdown.

- **Language**: Python 3.10+
- **Package Manager**: [uv](https://docs.astral.sh/uv/) (never pip)
- **Build System**: Hatchling (`src/` layout)
- **Linter/Formatter**: Ruff
- **Test Runner**: pytest

## Architecture & Layout

```
living-ink/
├── src/living_ink/
│   ├── __main__.py          # python -m living_ink entry point
│   ├── cli/                 # Command Pattern: one BaseCommand subclass per command
│   ├── config/              # paths.py, validate.py, credentials.py, schema.py, writer.py
│   ├── core/                # What a run and a preview must agree on (+ core/stages/)
│   ├── destinations/        # base.py (ABC + registry), filesystem.py, markup.py, obsidian.py
│   ├── sources/             # base.py (registry), notebook.py, pdf.py, epub.py
│   ├── pipeline.py          # SyncPipeline: owns the order of the stages
│   ├── settings.py          # Settings: resolved, typed, frozen configuration
│   ├── transport.py         # RemarkableTransport Protocol
│   ├── api.py               # Client factory + automatic fallback
│   ├── sync.py              # Cloud sync protocol (v3/v4)
│   ├── ssh.py               # USB SSH transport
│   ├── extract.py           # .rm → SVG → PNG, PDF/EPUB handling
│   ├── clean.py             # Vision OCR & text cleanup
│   ├── providers.py         # AI providers (presets + registry)
│   ├── notemerge.py         # Splice generated blocks without destroying user text
│   ├── scheduler.py         # Cron arithmetic for `watch` (imports only `state`)
│   ├── setup_wizard.py      # Interactive onboarding
│   ├── ocr_prompt.txt       # System prompt for vision OCR
│   └── cleanup_prompt.txt   # System prompt for text repair
├── tests/                   # pytest suite
├── docs/USER_MANUAL.md      # End-user reference
├── pyproject.toml
└── install.sh               # One-line installer
```

The commands are `sync`, `watch`, `setup`, `info`, `config`, `uninstall`, and a deliberately-hidden `completions`. There is no `status` command — `info` is the one read-only surface.

Deeper architectural notes — the transport Protocol, the destination/provider/source registries, and the pipeline stages — live in `AGENTS.md`. Read it before changing anything structural.

## Stateless Repository & XDG Standards

The repository is completely stateless:

- **User Config**: `~/.config/living-ink/config.yml` (overridable via `--config`, `LIVING_INK_CONFIG`, `LIVING_INK_CONFIG_DIR`).
- **Credentials**: one file per secret in `<config dir>/credentials/`, mode `0600`. **Never in `config.yml`.**
- **Runtime Data**: `~/.local/share/living-ink/` (overridable via `LIVING_INK_DATA_DIR`).
- Personal tokens, credentials, and downloaded notebooks must NEVER be committed to git.

## Configuration

Every setting is declared exactly once, as a `Setting(...)` entry in `config/schema.py` — field, config key, kind, default, help, env var, CLI flag, choices, store and legacy keys. Adding a setting is **one schema entry plus one dataclass field on `Settings`**; `settings._assert_parity()` raises at import if those two sides disagree.

`settings.Settings.resolve(config)` merges the layers into one frozen typed object. Precedence is **flag > env var > credentials store > config file (current key, then `legacy_keys`) > default**, implemented once in `Settings._layers` / `._pick`. There is no `FIELD_ENV_VARS` table and no per-field config reader. Never write settings back into `os.environ`.

`living-ink info` prints every resolved setting alongside the layer that supplied it, via `Settings.explain()`.

Retire a setting with its `status` field; never delete it from the schema, because an unrecognised section is a hard error.

## Import Layering

`tests/test_layering.py` parses module-level imports with `ast` and enforces the import graph. `config/`, `core/`, `destinations/`, `sources/` and `scheduler.py` each have rules about what they may import. Run it before assuming a new import is fine.

## Package Management

**Always use `uv` for all package management operations.**

```bash
uv sync --all-extras      # install dependencies
uv add <package>          # add a dependency
uv add --dev <package>    # add a dev dependency
```

## Running Tests & Quality Checks

**Before committing, always run:**

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -v
```

## Running the Application

```bash
uv run living-ink --help
uv run living-ink info
uv run living-ink sync --preview

# Or run as module
uv run python -m living_ink info
```

## Conventions

- Google docstrings on all modules, classes, and functions.
- Feature branches: `feat/<description>`, bug fixes: `fix/<description>`.
- Preserve existing comments and docstrings in code you are not modifying.
