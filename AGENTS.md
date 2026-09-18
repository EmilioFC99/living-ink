# AGENTS.md — Living Ink

> AI coding agent guide for the Living Ink codebase.

## Project Overview

**Living Ink** syncs handwritten notebooks from a **reMarkable tablet** to digital note apps (**Apple Notes** and **Obsidian**). It pulls documents over USB SSH or reMarkable Cloud, renders `.rm` pages to PNG, transcribes them with a multimodal LLM (with Google Cloud Vision as an optional fallback), and publishes structured notes to every configured destination.

- **Language**: Python 3.10+
- **Package manager**: [uv](https://docs.astral.sh/uv/) — never raw `pip`
- **Build system**: Hatchling, `src/` layout
- **Linter/formatter**: Ruff (line length 100)
- **Test runner**: pytest
- **License**: MIT

## Repository Structure

```
living-ink/
├── src/living_ink/
│   ├── __main__.py          # `python -m living_ink`
│   ├── cli.py               # Command Pattern CLI: sync | watch | setup | status
│   ├── pipeline.py          # SyncPipeline orchestrator + processing stages
│   ├── settings.py          # Settings: the resolved, typed configuration
│   ├── config.py            # XDG path resolution, ConfigurationMissing
│   ├── models.py            # Shared data models
│   ├── transport.py         # RemarkableTransport Protocol, UnsupportedOperation
│   ├── api.py               # Client factory + FallbackClient
│   ├── sync.py              # Cloud sync protocol (v3/v4)
│   ├── ssh.py               # USB SSH transport (10.11.99.1)
│   ├── extract.py           # .rm → SVG → PNG, PDF/EPUB handling
│   ├── clean.py             # Vision OCR and text repair entry points
│   ├── providers.py         # AI provider presets + provider registry
│   ├── destinations.py      # Destination ABC + destination registry
│   ├── setup_wizard.py      # Interactive onboarding
│   └── *_prompt.txt         # LLM system prompts (edit these, not the Python)
├── tests/                   # pytest suite + test_docker.sh smoke tests
├── docs/                    # SETUP_GUIDE.md, USER_MANUAL.md, TEST_PLAN.md
├── pyproject.toml
├── install.sh               # One-line installer
├── Dockerfile               # Multi-stage production image
└── docker-compose.yml       # CLI and background daemon services
```

## Architecture

### Pipeline Flow

```
reMarkable tablet
    ↓  USB SSH or Cloud API (whichever is preferred, with automatic fallback)
Download document zip
    ↓
Render pages: .rm → SVG → PNG (white background); PDFs composite annotations
    ↓
Transcribe:
    ├── AI vision OCR (default — reads handwriting and cleans in one call)
    └── Google Cloud Vision → AI text repair (optional fallback)
    ↓
Publish via Destination.publish()
    ├── AppleNotesDestination (AppleScript, one folder level)
    └── ObsidianDestination (Markdown + frontmatter, full folder tree)

Note identity is the reMarkable document id, never the title. `publish()` receives `doc_id`; Obsidian writes it into the frontmatter as `living_ink_id` and will not merge two different documents into one file; `publications.target` records where each note landed, and is fed back as `existing_target` so a rename or a move relocates the note. A published document missing from the tablet listing is reported as an orphan; `living-ink sync --prune` deletes it via `Destination.unpublish()`, which never removes a note that cannot be proven to be Living Ink's.
```

### The seams that matter

**Transport is a Protocol.** `transport.RemarkableTransport` is the whole contract. `api.get_rmapi(settings)` returns a Cloud client, an SSH client, or a `FallbackClient` wrapping both; every call goes through `FallbackClient._with_fallback`, so a new Protocol method needs one proxy line. A client that cannot serve a call raises `transport.UnsupportedOperation` — never omit the method, because callers do not use `hasattr`. `tests/test_transport.py` fails if any shipped client drops one.

**Destinations are a registry.** Subclass `Destination`, implement `publish()`, `from_config(section, settings)` and `describe()`, then decorate with `@register_destination("<config section>")`. `build_destinations()` walks the registry, so `pipeline.py` never learns the new name. A `from_config` that returns `None` or raises skips that destination with a warning rather than failing the run. Sync state is per destination: `processed_notebooks_{DestName}.json` maps doc id → version.

**AI providers are presets first, classes second.** Any backend speaking the OpenAI chat API is an entry in `PROVIDER_PRESETS`, not code. One that does not is a `TextRepairProvider` subclass with `from_config()`, decorated `@register_provider("<name>")`; `get_provider()` checks the registry before the presets. Vision OCR and text repair are the same call path with different system prompts.

**A command owns its loop, not its exit code.** `SyncCommand.execute_sync()` returns whether one sync worked; `run()` turns that into an exit code and `WatchCommand` re-runs it on a timer instead. `watch` registers every sync option by delegating to `SyncCommand.register_args`, rides out failed cycles, and stops only on a configuration error or Ctrl+C — the compose daemon service runs it directly rather than wrapping `sync` in a shell loop.

**Processing is a fixed sequence of stages.** `SyncPipeline.run()` does `connect()` → `discover_documents()` → `filter_pending_documents()` → `process_notebook_item()` per document. `process_notebook_item()` runs `_describe_job` → `_acquire_pages` → `_collect_tags` → `_preprocess_images` → `_ocr_pages` → `_write_transcripts` → `_publish`, passing a mutable `DocumentJob` between them; a stage with nothing left to do raises `_StopProcessing(success, reason)`. Rendering dispatches through `SyncPipeline._RENDERERS`, so a new document type is one renderer method plus one table entry. `_ocr_pages` transcribes `settings.ocr_concurrency` pages at a time and reassembles them in page order; `--dry-run` short-circuits `_publish` so nothing is sent and nothing is recorded as synced.

## Things that will bite you

- **Settings are resolved once.** `settings.Settings.resolve(config)` merges YAML and environment into one frozen typed object; precedence is **CLI options > env var > config file > default**. `Settings.explain(config)` reports the same merge annotated with the layer that won, which is what `living-ink status` prints; both read the field-to-env-var pairing from `FIELD_ENV_VARS`, so a new setting stays reportable for free. `SyncPipeline.__init__` layers `SyncOptions` on top with `dataclasses.replace`. A new setting is one field plus one line in `resolve()` — do not write settings back into `os.environ`. The only exported env vars are the ones third-party SDKs read themselves (`OPENAI_API_KEY`, `GOOGLE_APPLICATION_CREDENTIALS`).
- **Importing `pipeline.py` must stay side-effect free.** Config, destinations and directories all sit behind cached accessors. `TestImportPurity` asserts a bare import creates no directories and prints nothing.
- **The repository is stateless.** Config lives at `~/.config/living-ink/config.yml`, runtime artifacts at `~/.local/share/living-ink/`. Personal tokens, credentials and downloaded notebooks must NEVER be committed.
- **Transcriptions are cached under `DATA_DIR/transcripts/`**, keyed by the page bytes plus `clean.transcription_fingerprint()` (provider, model, both prompt files). Deliberately outside the purged temp dirs — surviving the purge is what makes a repeat sync free. `living-ink cache` shows/prunes/clears it.
- **Rendered pages are cached under `DATA_DIR/renders/`**, keyed by the page's `.rm` source plus `extract.renderer_fingerprint()` (the `RENDER_FORMAT_VERSION` constant, the installed `rmc` and `rmscene`) and the background colour. An unchanged page skips the `.rm` → SVG → PNG step entirely. Bump `RENDER_FORMAT_VERSION` whenever `extract.py`'s own rendering behaviour changes.
- **Temp artifacts are auto-purged** at pipeline start, after each notebook, and via `atexit`. Pass `--keep-temp` when debugging rendering or OCR; `--dry-run` implies it.
- **`extract.py` monkey-patches `rmc`** to control SVG background and bounds. Upgrading `rmc`/`rmscene` is the likely cause of blank or clipped renders.

## Configuration

| Source | Key | Description |
|--------|-----|-------------|
| `config.yml` | `ai.provider` / `ai.api_key` | LLM provider preset and API key |
| `config.yml` | `remarkable.preferred_connection` | `ssh` or `cloud` |
| `config.yml` | `remarkable.use_ssh` | Enable USB SSH (`true`/`false`) |
| `config.yml` | `remarkable.ssh_host` / `ssh_user` / `ssh_port` | SSH parameters (passwordless auth) |
| `config.yml` | `remarkable.device_token` | reMarkable Cloud auth token |
| `config.yml` | `google_vision.credentials_path` | Google Cloud Vision service account |
| `config.yml` | `apple_notes.enabled` / `apple_notes.folder_name` | Apple Notes destination |
| `config.yml` | `obsidian.enabled` / `obsidian.vault_path` / `root_folder` | Obsidian destination |
| `config.yml` | `sync.sync_pdfs` / `sync_epubs` / `max_notebooks_per_run` | What and how much to sync |
| `config.yml` | `sync.ocr_concurrency` | Pages transcribed at once (default 4; 1 is serial) |
| `config.yml` | `sync.transcript_cache` | Reuse transcriptions across runs (default `true`) |
| `config.yml` | `sync.render_cache` | Reuse rendered page images across runs (default `true`) |
| `config.yml` | `sync.cache_max_age_days` | Prune entries unused this long (default 90) |
| env var | `REMARKABLE_PREFERRED_CONNECTION` | Override preferred method |
| env var | `REMARKABLE_USE_SSH` | Override USB SSH toggle |
| env var | `REMARKABLE_SSH_HOST` / `REMARKABLE_SSH_PORT` | SSH overrides |
| env var | `SYNC_OCR_CONCURRENCY` | Override page transcription concurrency |
| env var | `SYNC_TRANSCRIPT_CACHE` | Toggle the transcription cache |
| env var | `SYNC_RENDER_CACHE` | Toggle the render cache |
| env var | `SYNC_CACHE_MAX_AGE_DAYS` | Override the cache prune age |
| env var | `ENABLE_REPAIR` | Toggle LLM cleanup (`true`/`false`) |
| env var | `LIVING_INK_CONFIG` / `LIVING_INK_CONFIG_DIR` / `LIVING_INK_DATA_DIR` | Path overrides |

## Commands

```bash
uv sync --all-extras                                  # install deps (incl. dev)
uv run living-ink --help                              # CLI: sync | watch | setup | status
uv run living-ink sync --notebook "Foo" --keep-temp   # one notebook, keep artifacts
uv run living-ink sync --dry-run                      # transcribe, publish nothing
uv run living-ink watch --interval 600                # sync every 10 minutes
uv run living-ink status                              # health check + effective settings
uv run living-ink status --json                       # machine-readable health check
uv run living-ink list                                # what is pending or failing
uv run living-ink state                               # what is remembered between runs
uv run living-ink sync --prune                        # also delete notes whose notebook is gone
uv run living-ink cache                               # transcription and render cache sizes
uv run living-ink cache --clear                       # drop them; the next sync pays again

uv run ruff check .           # lint
uv run ruff format --check .  # format check (`ruff format .` to fix)
uv run pytest -v              # full suite
uv run pytest tests/test_pipeline.py::test_name -v    # single test
./tests/test_docker.sh        # Docker build + smoke tests (slow)
```

Before committing: `uv run ruff check . && uv run ruff format --check . && uv run pytest -v`.

## Testing

Test at the seam that matches the change: command-level in `test_cli.py`, stage-level in `test_pipeline.py`, transport conformance in `test_transport.py`, config resolution in `test_settings.py`, registries in `test_destination_registry.py` and `test_providers.py`.

## Conventions

- Always use `uv`, never raw `pip`.
- Google-style docstrings on all modules, classes, and functions.
- Branches: `feat/<description>`, `fix/<description>`.
- Preserve existing comments and docstrings in code you are not changing.
- New destinations subclass `Destination` and register themselves; new AI backends are a preset entry unless they need custom code.
