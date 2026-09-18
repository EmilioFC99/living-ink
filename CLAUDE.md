# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

All tooling runs through `uv` — never raw `pip`.

```bash
uv sync --all-extras          # install deps (incl. dev)
uv run living-ink --help      # CLI: sync | watch | setup | status
uv run living-ink sync --notebook "Foo" --keep-temp   # single notebook, preserve artifacts
uv run living-ink sync --dry-run                      # transcribe but publish nothing
uv run living-ink status                              # health check + effective settings
uv run living-ink watch --interval 600                # sync every 10 minutes until stopped
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

**1. Transport is a Protocol, and it is self-healing.** `transport.RemarkableTransport` is the whole contract (`check_connection`, `get_meta_items`, `get_doc`, `download`, `get_file_type`, `download_raw_file`, `get_tags`). `api.get_rmapi()` returns a Cloud client (`sync.py`, sync protocol v3/v4), an SSH client (`ssh.py`, USB at `10.11.99.1`), or a `FallbackClient` wrapping both. Every method goes through `FallbackClient._with_fallback`, so a new Protocol method needs one proxy line, not bespoke retry logic. A transport that genuinely cannot serve a call raises `transport.UnsupportedOperation` — never omit the method, because callers do not use `hasattr`. `tests/test_transport.py` fails if any shipped client drops a method. `get_rmapi()` takes the resolved `Settings` and no longer reads the environment itself; preference resolves from `--ssh`/`--cloud` flags → `REMARKABLE_PREFERRED_CONNECTION` env → `preferred_connection` config → `"ssh"`.

**2. Destinations are an ABC with per-destination state.** `destinations.Destination.publish()` is the only contract; `AppleNotesDestination` (AppleScript via `osascript`, flattens folders) and `ObsidianDestination` (Markdown + YAML frontmatter + WikiLinks, mirrors the full reMarkable folder tree) implement it. Sync state is tracked **per destination** in `processed_notebooks_{DestName}.json` (doc id → version), so a notebook can be published to Obsidian but still pending for Apple Notes. Adding a destination means: subclass `Destination`, implement `publish()` + `from_config(section, settings)` + `describe()`, and decorate it with `@register_destination("<config section>")`. `destinations.build_destinations()` walks `DESTINATION_REGISTRY`, so nothing in `pipeline.py` needs editing. A `from_config` returning `None` (or raising) skips that destination with a warning instead of failing the run. The legacy single `destination:` key is normalized into per-section config by `_apply_legacy_destination()`.

**3. AI is one universal OpenAI-compatible provider.** `providers.UniversalChatProvider` talks to Gemini, OpenAI, Ollama, Groq, OpenRouter, Mistral, Together, or a custom base URL — selected by `PROVIDER_PRESETS` (`providers.py:39`). `NoneProvider` disables cleanup (raw OCR passthrough). Any backend that speaks the OpenAI chat API is a **preset entry, not code**; one that does not is a `TextRepairProvider` subclass with a `from_config()` classmethod, decorated `@register_provider("<name>")`. `get_provider()` checks `PROVIDER_REGISTRY` first, then the presets. Vision OCR and text repair are the *same* call path with different system prompts, loaded from `ocr_prompt.txt` / `openai_cleanup_prompt.txt` / `cleanup_prompt.txt` (plain text files shipped inside the package — edit these to change LLM behavior, not the Python). `clean.ocr_and_repair()` does OCR + cleanup in one multimodal request; Google Cloud Vision (`extract._ocr_google_vision*`) and Tesseract are optional fallbacks.

**4. CLI is Command Pattern; pipeline is a class.** `cli.py` registers `BaseCommand` subclasses (`SyncCommand`, `WatchCommand`, `SetupCommand`, `StatusCommand`) on `LivingInkCLI`, each owning its own `register_args()` + `run()`. `SyncCommand.execute_sync()` returns a bool so a caller can decide what a failure means; only `run()` exits. `WatchCommand` reuses both — it delegates `register_args` to `SyncCommand` and loops over `execute_sync`, surviving failed cycles and stopping on `ConfigurationMissing` or Ctrl+C, which is what the compose daemon service now runs. `SyncCommand` constructs a `SyncPipeline`, whose `run()` is a fixed sequence: `connect()` → `discover_documents()` → `filter_pending_documents()` → `process_notebook_item()` per doc. `process_notebook_item()` is itself a fixed sequence of stages — `_describe_job` → `_acquire_pages` → `_collect_tags` → `_preprocess_images` → `_ocr_pages` → `_write_transcripts` → `_publish` — passing a mutable `DocumentJob` between them; a stage with nothing left to do raises `_StopProcessing(success, reason)` rather than returning a sentinel. `_ocr_pages` fans out over `settings.ocr_concurrency` threads (`_transcribe_pages`) — a page is one network round trip, so this is where the wall-clock time goes; results come back in page order regardless of completion order. `--dry-run` short-circuits `_publish`, so nothing is sent and no processed-log entry is written, leaving the notebook pending. Rendering dispatches through the `SyncPipeline._RENDERERS` table (`pdf`, `epub`, anything else → handwritten notebook), so a new document type is one renderer method plus one table entry. Test at the seam that matches: command-level in `test_cli.py`, stage-level in `test_pipeline.py`.

## Non-obvious behaviors

- **Importing `pipeline.py` is side-effect free — keep it that way.** Config is read by `get_default_config()`, destinations are built by `get_default_destinations()`, and directories are created by `ensure_runtime_dirs()`; all three cache and are called on demand, not at module load. `TestImportPurity` in `tests/test_pipeline.py` asserts a bare import creates no directories and prints nothing. The path constants (`DATA_DIR`, `WHITE_DIR`, …) are still module-level, so tests patch those; anything that reads config or touches disk goes behind an accessor.
- **Settings are resolved once, not passed through the environment.** `settings.Settings.resolve(config)` merges the YAML config with `os.environ` into one frozen typed object; precedence is **CLI options > env var > config file > default**. `SyncPipeline.__init__` layers its `SyncOptions` on top with `dataclasses.replace` and stores the result as `self.settings` — `pipeline.use_ssh`, `.limit`, `.folder` and friends are read-only views onto it. A new setting means one field on `Settings`, one entry in `FIELD_ENV_VARS`/`_config_values()`, and one line in `resolve()` — `Settings.explain()` then reports its origin, and `living-ink status` prints it, without further edits; do **not** reintroduce the old `os.environ` write-back. The only env vars `load_yaml_config()` still exports are the ones third-party SDKs read for themselves (`OPENAI_API_KEY`, `GOOGLE_APPLICATION_CREDENTIALS`). Callers with no config in hand (`api.get_rmapi()`) fall back to `Settings.from_env()`.
- **The repo is stateless.** Config resolves to `~/.config/living-ink/config.yml` (8-step lookup in `config.get_config_path()`, incl. `LIVING_INK_CONFIG` / `LIVING_INK_CONFIG_DIR`); runtime artifacts to `~/.local/share/living-ink/` (`LIVING_INK_DATA_DIR`). Never write tokens, credentials, or notebooks into the checkout.
- **Transcriptions are cached, and the cache is not a temp artifact.** `cache.TranscriptCache` stores each page's transcription under `DATA_DIR/transcripts/`, keyed by `sha256(page bytes + route + clean.transcription_fingerprint())` — the fingerprint covers the provider, the model, and both prompt files, so editing `ocr_prompt.txt` correctly misses. It lives outside the purged temp dirs on purpose: surviving the purge is what makes a repeat sync of an unchanged notebook free. An empty transcription is never stored (a blank page is usually a rate limit). `living-ink cache` shows, prunes, and clears it; `sync.transcript_cache: false` turns it off.
- **Rendered pages are cached the same way.** `cache.RenderCache` stores each page's PNG under `DATA_DIR/renders/`, keyed by the page's `.rm` source plus `extract.renderer_fingerprint()` and the background colour, so an unchanged page skips `.rm` → SVG → PNG entirely. Both caches derive from one `cache.FileCache` base — a third cache is a subclass with a `suffix`, a `noun`, and a `get`/`put` pair. `extract.RENDER_FORMAT_VERSION` is in the fingerprint: **bump it whenever `extract.py`'s rendering changes**, or the cache serves images the current code would not produce. `sync.render_cache: false` turns it off. The PDF composite path is not cached.
- **Temp artifacts are auto-purged.** `cleanup_temp_artifacts()` runs at pipeline start, after each notebook, and via `atexit`. When debugging rendering or OCR, always pass `--keep-temp` or the PNGs vanish before you can look at them. `--dry-run` implies `--keep-temp`, since the transcripts it points at would otherwise be purged on the way out.
- **`extract.py` monkey-patches `rmc`** (`_patch_rmc()`) to control SVG background and bounds. Upgrading `rmc`/`rmscene` is the likely cause of blank or clipped page renders.

## Conventions

- Google-style docstrings on all modules, classes, and functions.
- Branches: `feat/<description>`, `fix/<description>`.
- Preserve existing comments and docstrings in code you aren't changing.
