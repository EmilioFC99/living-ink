# AGENTS.md — Living Ink

> AI coding agent guide for the Living Ink codebase.

## Project Overview

**Living Ink** syncs handwritten notebooks from a **reMarkable tablet** to **Obsidian**. It pulls documents over USB SSH or reMarkable Cloud, renders `.rm` pages to PNG, transcribes them with a multimodal LLM, and publishes structured notes to every configured destination.

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
│   ├── pipeline.py          # SyncPipeline: the order of the stages, and only the order
│   ├── settings.py          # Settings: the resolved, typed configuration
│   ├── models.py            # Document: what a transport's listing is made of
│   ├── transport.py         # RemarkableTransport Protocol, UnsupportedOperation
│   ├── api.py               # Client factory + FallbackClient
│   ├── sync.py              # Cloud sync protocol (v3/v4)
│   ├── ssh.py               # USB SSH transport (10.11.99.1)
│   ├── extract.py           # .rm → SVG → PNG, PDF/EPUB handling
│   ├── clean.py             # AI vision OCR and text repair entry points
│   ├── providers.py         # AI provider presets + provider registry
│   ├── state.py             # SQLite state: documents, publications, pages, runs
│   ├── cache.py             # FileCache → TranscriptCache, RenderCache
│   ├── report.py            # RunReport: what the run adds up to, printed once
│   ├── notemerge.py         # Reading an existing note before rewriting it
│   ├── scheduler.py         # The background job the watch command installs
│   ├── setup_wizard.py      # Interactive onboarding
│   ├── logs.py              # log(): the one user-facing line, importable from anywhere
│   ├── redact.py            # Secret registry; stdlib-only leaf
│   ├── safeio.py            # Atomic 0600 writes; stdlib-only leaf
│   ├── ui.py                # Prompts, menus, Cancelled
│   ├── devices.py           # Panel sizes by device codename
│   ├── cli/                 # Command Pattern: app.py, base.py, commands/*
│   ├── config/              # paths.py, schema.py, validate.py, credentials.py, writer.py
│   ├── core/                # What both a run and a preview must agree on
│   │   ├── document.py      # Page, Document, PublishContext, PublishResult (a leaf)
│   │   ├── listing.py       # The reads that answer "what is this" before a download
│   │   ├── selection.py     # select(): the one classifier, the one candidate set
│   │   ├── recipe.py        # "Would we produce different output from the same document"
│   │   ├── temp.py          # The per-document workspace (a leaf)
│   │   └── stages/          # preprocess, transcribe, transcript, verdict
│   ├── destinations/        # base.py (ABC + registry), filesystem.py, markup.py, obsidian.py
│   ├── sources/             # base.py (registry), notebook.py, pdf.py, epub.py
│   └── *_prompt.txt         # LLM system prompts (edit these, not the Python)
├── tests/                   # pytest suite + test_docker.sh smoke tests
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
Transcribe: AI vision OCR (one multimodal call reads the page and cleans it)
    ↓
Publish via Destination.publish(doc, ctx)
    └── ObsidianDestination (Markdown + frontmatter, full folder tree)
```

Note identity is the reMarkable document id, never the title. Obsidian writes it into the frontmatter as `living_ink_id` and will not merge two different documents into one file; `publications.target` records where each note landed and is fed back as `ctx.existing_target`, so a rename or a move relocates the note rather than growing a second one. A published document missing from the tablet listing is reported as an orphan; `living-ink sync --prune` deletes it via `Destination.unpublish()`, which never removes a note that cannot be proven to be Living Ink's.

Obsidian's frontmatter carries three dates that mean three different things: `created` (preserved from the note if it already has one, else the first time this document was published here, else the tablet's modification date), `updated` (when the notebook was last written on, per the tablet) and `synced` (today). `pipeline.to_datetime()` reconciles the transports, which disagree — the cloud client returns a datetime, SSH returns the device's epoch in milliseconds.

### The seams that matter

**Transport is a Protocol.** `transport.RemarkableTransport` is the whole contract. `api.get_rmapi(settings)` returns a Cloud client, an SSH client, or a `FallbackClient` wrapping both; every call goes through `FallbackClient._with_fallback`, so a new Protocol method needs one proxy line. A client that cannot serve a call raises `transport.UnsupportedOperation` — never omit the method, because callers do not use `hasattr`. `tests/test_transport.py` fails if any shipped client drops one.

**Both transports hand back `models.Document`, and the readers are typed.** `core/listing.py` answers "what is this called, where does it sit, has it changed, what type is it" by naming the model's own fields. There used to be a `get_val(item, key)` taking the key as a string, and five rmapy-spelled aliases on the model so it would find them; a misspelling was a `None`, not an error.

**Destinations are a registry with per-destination state.** Subclass `Destination`, declare `state_key`, implement `from_config(section, settings)` + `describe()` + `check()` + `publish(doc, ctx)`, decorate with `@register_destination("<config section>")`, and add the import to `destinations/__init__.py` in alphabetical position — registry order is publish order. `build_destinations()` walks the registry, so `pipeline.py` never learns the new name. A `from_config` that returns `None` or raises skips that destination with a warning rather than failing the run. Sync state is per destination in `state.db`: one `publications` row per `(doc_id, destination)`, keyed by the **declared** `state_key`, never by the class name. Construction never validates — `check()` reports and `SyncPipeline.preflight_destinations()` refuses. A file-writing destination inherits its lifecycle from `filesystem.FileSystemDestination`, whose stage order is a dependency order with a dry-run cut after stage 2.

**A document format is a registered source, not a branch.** `sources/` holds one module per format, each a `Renderer` with a `register_source(SourceType(...))` call. `_render_document()` resolves the source by name and calls `prepare` → `pages` → `text_layer` → `render`. Page numbers are sparse (an annotated 400-page PDF yields 12 and 377), PDFs and EPUBs carry a text layer that bypasses OCR, and `empty_is_skip` declares whether rendering nothing is a skip or a failure. Bump a `Renderer.version` whenever its output changes — it is part of the render cache key.

**AI providers are presets first, classes second.** Any backend speaking the OpenAI chat API is an entry in `PROVIDER_PRESETS`, not code. One that does not is a `TextRepairProvider` subclass with `from_config()`, decorated `@register_provider("<name>")`; `get_provider(settings)` checks the registry before the presets. Vision OCR and text repair are the same call path with different system prompts.

**A command owns its loop, not its exit code.** `SyncCommand.execute_sync()` returns whether one sync worked; `run()` turns that into an exit code and `WatchCommand` re-runs it on a timer instead. `watch` registers every sync option by delegating to `SyncCommand.register_args`, rides out failed cycles, and stops only on a configuration error or Ctrl+C — the compose daemon service runs it directly rather than wrapping `sync` in a shell loop. `info` is the one read-only surface; the destructive halves live in `config` → Advanced.

**Processing is a fixed sequence of stages.** `SyncPipeline.run()` does `connect()` → `discover_documents()` → `filter_pending_documents()` → `process_notebook_item()` per document. `process_notebook_item()` runs `_describe_job` → `_acquire_pages` → `_collect_tags` → `_preprocess_images` → `_ocr_pages` → `_write_transcripts` → `_judge_pages` → `_publish`, passing a mutable `DocumentJob` between them; a stage with nothing left to do raises `_StopProcessing(success, reason)`. `_judge_pages` sits **after** the transcript is written, because its job is to stop a document publishing and the artifact is what a user debugging that needs. `_ocr_pages` transcribes `settings.ocr_concurrency` pages at a time and reassembles them in page order; `--preview --transcribe` short-circuits `_publish` so nothing is sent and nothing is recorded as synced.

## Things that will bite you

- **Every setting is declared once, in `config/schema.py`.** One `Setting(...)` entry carries the field name, the `config.yml` key, the kind, the default, the help, the env var, the CLI flag, the store and the `legacy_keys`. Nothing else enumerates settings. Adding one is **one schema entry plus one dataclass field**; `settings._assert_parity()` raises at import if the two sides disagree. A setting is retired by its `status` (`DEPRECATED` / `REMOVED`), never by deleting the entry — an unrecognised section is a hard error, so dropping it would stop every config that names it from loading. A *renamed* key is a `legacy_keys` entry on the setting that replaced it. No config file is ever rewritten; the mapping happens in memory at load.
- **Settings are resolved once, and nothing is written back to the environment.** Precedence is **flag > env var > credentials store > config file (current key, then `legacy_keys`) > default**, implemented once in `Settings._layers`/`._pick`; `resolve()` throws the provenance away, `explain()` keeps it, and neither re-implements the other. `SyncPipeline.__init__` passes this run's flags as the `flags=` layer and stores the result as `self.settings`. A blank string from the file is a *value*; a blank env var is an omission. `load_yaml_config()` exports **nothing** — it used to copy a legacy `openai.api_key` into `OPENAI_API_KEY`, which only widened who could read the credential.
- **The import graph is a test.** `tests/test_layering.py` parses module-level imports with `ast` and enforces the layering: `config/` imports nothing from the package but the two stdlib-only leaves, `destinations/` and `sources/` import nothing from the command layer, `core/` imports neither plugin package, and `sources/` reaches `extract` and `api` from inside methods only — hoisting those creates a cycle *and* silently breaks the ~15 tests that monkeypatch the renderer. Function-level and `TYPE_CHECKING` imports are deliberately exempt. Add the rule when you add the package.
- **Importing `pipeline.py` must stay side-effect free.** Config, destinations and directories all sit behind cached accessors. `TestImportPurity` asserts a bare import creates no directories and prints nothing.
- **The repository is stateless.** Config lives at `~/.config/living-ink/config.yml`, runtime artifacts at `~/.local/share/living-ink/`. Personal tokens, credentials and downloaded notebooks must NEVER be committed.
- **Secrets live beside `config.yml`, never in it.** `living_ink.config.credentials` stores one file per credential under `<config dir>/credentials/`, atomically at `0600`, registered with `redact` on every read and write. The directory is derived from the resolved config path, so a second profile gets its own secrets. The name carries the provider (`ai.api_key.<provider>`, composed only by `ai_key_name()`) so switching AI providers and back does not destroy a key; `_VALID_NAME` restricts a name rather than escaping it, because it is also a filename. `migrate_secret()` copies from the old locations (`config.yml`, `~/.rmapi`) and never deletes them, so a downgrade does not force re-pairing. Anything rendering a key uses `credentials.mask()`.
- **A failed page does not fail the document, and the verdict's question order is the contract.** `core/stages/verdict.judge_pages` tests **failure first**; get it backwards and a notebook whose every page hit a 429 is reported "skipped: every page is blank", a success, never retried. A partial document publishes with gap markers and a `pages_failed` count that `core/selection` reads as still pending — that clause is the only thing stopping the gaps from being permanent.
- **Transcriptions are cached under `DATA_DIR/transcripts/`**, keyed by the page bytes plus `clean.transcription_fingerprint()` (provider, model, temperature, language, both prompt files). Deliberately outside the purged temp dirs — surviving the purge is what makes a repeat sync free. `living-ink info` reports it, a successful sync prunes it, and `config` → Advanced clears it. It is also what makes a run resumable: a page is banked as soon as it comes back, so an interrupt costs the page in flight and nothing else. An interrupted run is recorded as `outcome="interrupted"` with the counts it actually reached, and exits 130 instead of printing a traceback.
- **Rendered pages are cached under `DATA_DIR/renders/`**, keyed by `PageRef.source_key` plus `extract.renderer_fingerprint()` (the installed `rmc` and `rmscene`), the source's name, **that renderer's own `version`**, the background colour and the device's panel size. There is no global format constant: one number for three renderers meant a change to the PDF compositor threw away every cached notebook page.
- **The `pages` table is what detects a broken renderer.** Each page's `.rm` source hash and rendered PNG hash are stored together; a page whose source is unchanged but whose render is not means the renderer moved (the documented symptom of an `rmc`/`rmscene` upgrade), and a notebook whose pages nearly all render to identical bytes rendered blank. Both surface as `RunReport` warnings at the end of the run, not as log lines that scroll away.
- **Temp artifacts are auto-purged** at pipeline start, after each notebook, and via `atexit`. Pass `--keep-temp` when debugging rendering or OCR; nothing else turns it on for you.
- **`extract.py` monkey-patches `rmc`** to control SVG background and bounds. Upgrading `rmc`/`rmscene` is the likely cause of blank or clipped renders.

## Configuration

| Source | Key | Description |
|--------|-----|-------------|
| `config.yml` | `ai.provider` / `ai.model` | LLM provider preset and model |
| `config.yml` | `ai.repair_enabled` | Run the AI pass at all (`true`/`false`) |
| `config.yml` | `remarkable.preferred_connection` | `ssh` or `cloud` |
| `config.yml` | `remarkable.use_ssh` | Enable USB SSH (`true`/`false`) |
| `config.yml` | `remarkable.ssh_host` / `ssh_user` / `ssh_port` | SSH parameters (passwordless auth) |
| credentials | `ai.api_key.<provider>` | LLM API key, one file per provider |
| credentials | `remarkable.cloud_token` | reMarkable Cloud auth token |
| credentials | `remarkable.ssh_password` | SSH password, when the tablet has one |
| `config.yml` | `obsidian.enabled` / `vault_path` / `root_folder` | Obsidian destination |
| `config.yml` | `sync.types` / `sync.limit` / `sync.tags` / `sync.exclude` | What and how much to sync |
| `config.yml` | `ocr.concurrency` | Pages transcribed at once (default 4; 1 is serial) |
| `config.yml` | `cache.transcripts` / `cache.renders` | Reuse work across runs (default `true`) |
| `config.yml` | `cache.max_age_days` | Prune entries unused this long (default 90) |
| `config.yml` | `watch.schedule` / `watch.timezone` | The background job's cron schedule |
| env var | `LIVING_INK_AI_PROVIDER` / `_AI_MODEL` / `_AI_API_KEY` | AI overrides; the key is env-only |
| env var | `ENABLE_REPAIR` | Toggle the AI pass (`ai.repair_enabled`) |
| env var | `REMARKABLE_PREFERRED_CONNECTION` / `_USE_SSH` | Transport overrides |
| env var | `REMARKABLE_SSH_HOST` / `_SSH_USER` / `_SSH_PORT` | SSH overrides |
| env var | `REMARKABLE_TOKEN` / `REMARKABLE_SSH_PASSWORD` | Credentials, env-only |
| env var | `SYNC_OCR_CONCURRENCY` / `SYNC_TYPES` / `SYNC_MAX_NOTEBOOKS` | Sync overrides |
| env var | `SYNC_TRANSCRIPT_CACHE` / `SYNC_RENDER_CACHE` / `SYNC_CACHE_MAX_AGE_DAYS` | Cache overrides |
| env var | `LIVING_INK_CONFIG` / `LIVING_INK_CONFIG_DIR` / `LIVING_INK_DATA_DIR` | Path overrides |

Every env var is declared in `config/schema.py`. If a name is not there, nothing reads it — which is what made `docker-compose.yml` hand containers three keys that named no setting at all.

## Commands

```bash
uv sync --all-extras                                  # install deps (incl. dev)
uv run living-ink --help                              # CLI: sync | watch | setup | info | config | completions | uninstall
uv run living-ink sync --notebook "Foo" --keep-temp   # one notebook, keep artifacts
uv run living-ink sync --preview --transcribe         # transcribe, publish nothing
uv run living-ink sync --json                         # run summary as JSON
uv run living-ink watch                               # sync on the cron schedule in config.yml until stopped
uv run living-ink info                                # health check, caches, schedule, sync state
uv run living-ink info --json                         # machine-readable health check + every state row
uv run living-ink sync --preview                      # what is new, changed, or up to date
uv run living-ink sync --prune                        # also delete notes whose notebook is gone
uv run living-ink config                              # settings menu, prompts, destructive maintenance
uv run living-ink completions zsh                     # tab completion, generated from this build's parser
uv run living-ink uninstall --yes                     # remove the background job, settings and caches

uv run ruff check .           # lint
uv run ruff format --check .  # format check (`ruff format .` to fix)
uv run pytest -v              # full suite
uv run pytest tests/test_pipeline.py::test_name -v    # single test
./tests/test_docker.sh        # Docker build + smoke tests (slow)
```

Before committing: `uv run ruff check . && uv run ruff format --check . && uv run pytest -v`.

## Testing

Test at the seam that matches the change: command-level in `test_cli.py`, stage order in `test_pipeline.py`, a stage's own behaviour in `test_stages_*.py`, transport conformance in `test_transport.py`, config resolution in `test_settings.py`, registries in `test_destination_registry.py` and `test_providers.py`, the import graph in `test_layering.py`.

Two helper modules exist so a test says what it means and nothing else: `tests/builders.py` fills in every field of the `(Document, PublishContext)` pair a destination is handed, and `tests/fixtures/listing.py` builds the `models.Document` a transport's listing is made of. `tests/fakes.py` runs the contracts nothing shipped runs — `FakeApiDestination` is the only thing that closes the `PublishResult.external_id` → `publications.external_id` loop end to end, because Obsidian identifies notes by frontmatter and reports no id at all.

## Conventions

- Always use `uv`, never raw `pip`.
- Google-style docstrings on all modules, classes, and functions.
- Branches: `feat/<description>`, `fix/<description>`.
- Preserve existing comments and docstrings in code you are not changing.
- New destinations subclass `Destination` and register themselves; new document formats are a module in `sources/`; new AI backends are a preset entry unless they need custom code.
