# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

All tooling runs through `uv` — never raw `pip`.

```bash
uv sync --all-extras          # install deps (incl. dev)
uv run living-ink --help      # CLI: sync | setup | status
uv run living-ink sync --notebook "Foo" --keep-temp   # single notebook, preserve artifacts
uv run living-ink status --json                       # machine-readable health check

uv run ruff check .           # lint
uv run ruff format --check .  # format check (run `ruff format .` to fix)
uv run pytest -v              # full suite
uv run pytest tests/test_pipeline.py::test_name -v    # single test
./tests/test_docker.sh        # Docker build + smoke tests (slow)
```

Before committing: `uv run ruff check . && uv run ruff format --check . && uv run pytest -v`.

## Architecture

Pipeline: reMarkable tablet → download → render `.rm` → PNG → OCR/cleanup via LLM → publish to destinations.

Four seams matter more than the file list:

**1. Transport is swappable and self-healing.** `api.get_rmapi()` returns either a Cloud client (`sync.py`, sync protocol v3/v4), an SSH client (`ssh.py`, USB at `10.11.99.1`), or a `FallbackClient` (`api.py:21`) wrapping both. `FallbackClient` tries the preferred transport per call and silently retries on the other, so any new client method must be proxied there too, not just on the concrete clients. Preference resolves from `--ssh`/`--cloud` flags → `preferred_connection` config → `REMARKABLE_PREFERRED_CONNECTION` env → `"ssh"`.

**2. Destinations are an ABC with per-destination state.** `destinations.Destination.publish()` is the only contract; `AppleNotesDestination` (AppleScript via `osascript`, flattens folders) and `ObsidianDestination` (Markdown + YAML frontmatter + WikiLinks, mirrors the full reMarkable folder tree) implement it. Sync state is tracked **per destination** in `processed_notebooks_{DestName}.json` (doc id → version), so a notebook can be published to Obsidian but still pending for Apple Notes. Adding a destination means: subclass, then wire it into `pipeline.get_destinations_from_config()`.

**3. AI is one universal OpenAI-compatible provider.** `providers.UniversalChatProvider` talks to Gemini, OpenAI, Ollama, Groq, OpenRouter, Mistral, Together, or a custom base URL — selected by `PROVIDER_PRESETS` (`providers.py:39`). `NoneProvider` disables cleanup (raw OCR passthrough). Vision OCR and text repair are the *same* call path with different system prompts, loaded from `ocr_prompt.txt` / `openai_cleanup_prompt.txt` / `cleanup_prompt.txt` (plain text files shipped inside the package — edit these to change LLM behavior, not the Python). `clean.ocr_and_repair()` does OCR + cleanup in one multimodal request; Google Cloud Vision (`extract._ocr_google_vision*`) and Tesseract are optional fallbacks.

**4. CLI is Command Pattern; pipeline is a class.** `cli.py` registers `BaseCommand` subclasses (`SyncCommand`, `SetupCommand`, `StatusCommand`) on `LivingInkCLI`, each owning its own `register_args()` + `run()`. `SyncCommand` constructs a `SyncPipeline` (`pipeline.py:936`), whose `run()` is a fixed sequence: `connect()` → `discover_documents()` → `filter_pending_documents()` → `process_notebook_item()` per doc. Test at the seam that matches: command-level in `test_cli.py`, stage-level in `test_pipeline.py`.

## Non-obvious behaviors

- **Importing `pipeline.py` is side-effect free — keep it that way.** Config is read by `get_default_config()`, destinations are built by `get_default_destinations()`, and directories are created by `ensure_runtime_dirs()`; all three cache and are called on demand, not at module load. `TestImportPurity` in `tests/test_pipeline.py` asserts a bare import creates no directories and prints nothing. The path constants (`DATA_DIR`, `WHITE_DIR`, …) are still module-level, so tests patch those; anything that reads config or touches disk goes behind an accessor.
- **Config is YAML-first but flows through env vars.** `load_yaml_config()` translates YAML into `os.environ` (`SYNC_PDFS`, `APPLE_NOTES_FOLDER`, `REMARKABLE_USE_SSH`, …), and `SyncPipeline.__init__` writes back to `os.environ` too. Any new setting needs both paths.
- **The repo is stateless.** Config resolves to `~/.config/living-ink/config.yml` (8-step lookup in `config.get_config_path()`, incl. `LIVING_INK_CONFIG` / `LIVING_INK_CONFIG_DIR`); runtime artifacts to `~/.local/share/living-ink/` (`LIVING_INK_DATA_DIR`). Never write tokens, credentials, or notebooks into the checkout.
- **Temp artifacts are auto-purged.** `cleanup_temp_artifacts()` runs at pipeline start, after each notebook, and via `atexit`. When debugging rendering or OCR, always pass `--keep-temp` or the PNGs vanish before you can look at them.
- **`extract.py` monkey-patches `rmc`** (`_patch_rmc()`) to control SVG background and bounds. Upgrading `rmc`/`rmscene` is the likely cause of blank or clipped page renders.

## Conventions

- Google-style docstrings on all modules, classes, and functions.
- Branches: `feat/<description>`, `fix/<description>`.
- Preserve existing comments and docstrings in code you aren't changing.

## Stale docs to distrust

- `AGENTS.md` and `.github/copilot-instructions.md` still describe a `scripts/` directory and `config/config.yml.example` that no longer exist, and predate the `SyncPipeline` class / CLI Command Pattern refactor (`8be0bc1`).
