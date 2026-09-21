# Living Ink

**Turn handwritten reMarkable notebooks into searchable Markdown in your Obsidian vault.**

[![CI](https://github.com/EmilioFC99/living-ink/actions/workflows/ci.yml/badge.svg)](https://github.com/EmilioFC99/living-ink/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)

Living Ink pulls your notebooks off a reMarkable tablet — over USB or the reMarkable
Cloud — renders each page, has an AI model read the handwriting, and writes real
Markdown into your vault. Your tablet's folder tree is mirrored, your tags come
across, and anything you type into the note yourself is never overwritten.

## What you get

- **Searchable text, not images.** One multimodal AI call per page does OCR and
  clean-up together. Headings, lists, tables and callouts survive.
- **Your folder tree, mirrored.** `Finance/2026/Q1/Budget` on the tablet becomes
  `Finance/2026/Q1/Budget.md` in the vault.
- **Notes you can still edit.** Living Ink owns the frontmatter and the page
  blocks. Everything you write between them is left alone on every re-sync.
- **Cheap to repeat.** Transcriptions are cached, so re-syncing an unchanged
  notebook costs nothing and Ctrl+C only loses the page in flight.

A published note looks like this:

```markdown
---
living_ink_id: 8f3c1a9e-...
created: 2026-03-02
updated: 2026-09-18
synced: 2026-09-20
source: Remarkable/Work/2026
type: notebook
tags: [meeting, q3]
---

## Page 1

# Q3 planning

- Ship the importer by the 14th
- [ ] Ask Dana about the migration window

> [!quote] Highlight
> The bottleneck is review, not authoring.
```

## Requirements

| | |
|---|---|
| Python | 3.10 or newer |
| Package manager | [uv](https://docs.astral.sh/uv/) (the installer sets it up for you) |
| Tablet | A reMarkable on firmware 3.0 or newer, reachable over USB or the reMarkable Cloud |
| AI | An API key for a vision-capable model — or [Ollama](https://ollama.com) running locally, which needs no key |
| Platform | macOS or Linux |

## Install

**One-line installer** — installs `uv`, installs Living Ink, launches the wizard:

```bash
curl -fsSL https://raw.githubusercontent.com/EmilioFC99/living-ink/main/install.sh | bash
```

**With `uv` already installed:**

```bash
uv tool install "git+https://github.com/EmilioFC99/living-ink.git"
living-ink setup
```

**From source:**

```bash
git clone https://github.com/EmilioFC99/living-ink.git
cd living-ink
uv sync --all-extras
uv run living-ink setup
```

To upgrade, re-run the installer or `uv tool install --force "git+https://github.com/EmilioFC99/living-ink.git"`.

## Quickstart

```bash
living-ink setup            # 1. the wizard: tablet, what to sync, AI provider, vault
living-ink sync --preview   # 2. see what would happen — free, no API calls
living-ink sync             # 3. do it
```

**Two things the wizard decides for you, and you can change:**

- A sync processes **every pending document** (`sync.limit: 0`). Set
  `sync.limit` to a number if you would rather cap each run.
- Only **handwritten notebooks** sync, unless you ticked more in the wizard.
  Either way `sync.types` is the setting, and `living-ink sync --pdf --epub`
  overrides it for one run.

Trash, Templates and Quick sheets are always skipped.

## Commands

| Command | What it does |
|---|---|
| [`sync`](#sync) | Run the pipeline now |
| [`watch`](#watch) | Keep syncing on a schedule until stopped |
| [`setup`](#setup) | The first-run wizard |
| [`config`](#config) | Change any setting, edit the prompts, clear the caches |
| [`info`](#info) | Read-only health check |
| [`uninstall`](#uninstall) | Remove what `setup` installed — never your notes |

Running bare `living-ink` syncs if you are configured, and runs `setup` if you are not.

### `sync`

Downloads what changed, reads it, publishes it. A notebook re-syncs when you
edited it on the tablet, when a setting that changes the output changed (a new
model, an edited prompt, a different vault folder), or when a previous run left
pages untranscribed. Everything else is skipped, and skipping is free.

```bash
living-ink sync --preview                # what would happen; no download, no OCR, no cost
living-ink sync                          # sync
living-ink sync --limit 3                # cap this run at three documents
living-ink sync --notebook "Work/Ideas"  # one notebook, by name, path or id
```

Every other flag — filtering, rehearsals, cache control, one-off overrides —
is in the [User Manual](docs/USER_MANUAL.md#sync).

### `watch`

Syncs on the cron schedule in your config and keeps going until you stop it.

```bash
living-ink watch
```

The schedule is one cron expression you can read and change (`living-ink config`
→ Watch offers common ones and shows the next three fire times). On macOS,
`setup` installs a LaunchAgent whose only job is keeping `watch` alive.

`watch` deliberately accepts **no behaviour flags**: a supervised process is
restarted without its arguments, so a `--limit` here would silently stop
applying. What a scheduled run does is what the config says it does.

### `setup`

The first-run wizard. It tests your tablet connection, verifies your AI key by
sending it a real image, finds your Obsidian vault, and shows a summary.
**Nothing is written until you confirm it.** It needs a terminal; with no TTY it
exits 2 rather than guessing.

```bash
living-ink setup
```

Safe to re-run at any time.

### `config`

An interactive menu over every setting, generated from the schema — so every
value `info` prints is editable here under the same name. Also where you edit
the two AI prompts and clear the caches.

```bash
living-ink config
```

Nothing is written until you save, and the summary before the save names every
change. Editing a prompt is a real change: it invalidates cached pages, so the
next sync reads them again.

### `info`

Reads; changes nothing. The health check to run when something looks wrong.

```bash
living-ink info          # connection, AI, vault, caches, schedule, sync state
living-ink info --json   # the same, plus every sync-state row
```

`info` reads, `config` writes. If a scheduled sync was missed, `info` prints a
banner above everything else.

### `uninstall`

Removes the background job and the caches, then asks separately before removing
your settings, credentials and sync record. **It never touches your notes, under
any flag.**

```bash
living-ink uninstall        # asks
living-ink uninstall --yes  # answers yes to everything
```

This does not remove the program — uninstall the package the way you installed it.

### Shell completions

```bash
living-ink completions zsh > ~/.zfunc/_living-ink   # then: fpath+=(~/.zfunc) before compinit
```

`bash` and `fish` work the same way; the redirect target for each is named in a
comment at the top of the output. The script is generated from the installed
version's own parser, so regenerate it after an upgrade rather than editing it.

## AI providers

Any OpenAI-compatible endpoint works. The model must be able to read images —
the wizard proves this by sending it one, so a text-only model fails at setup
rather than at page 200.

| `ai.provider` | Default model | Key needed |
|---|---|---|
| `gemini` | `gemini-flash-latest` | yes |
| `openai` | `gpt-4o-mini` | yes |
| `ollama` | `llama3.2` | no — runs locally |
| `groq` | `llama-3.3-70b-versatile` | yes |
| `openrouter` | `google/gemini-2.0-flash-exp:free` | yes |
| `mistral` | `mistral-small-latest` | yes |
| `together` | `meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo` | yes |
| `custom` | you set `ai.base_url` | depends |

There is no non-AI OCR path. Setting `ai.provider: none` makes `sync` refuse to
start rather than publish empty notes.

**What leaves your machine:** the page image and the two prompts, over HTTPS, to
the provider you chose. Your notebook titles, folder paths, tags and vault path
are not in the request. With `ollama` or a `custom` endpoint on your own
hardware, nothing leaves at all.

## Configuration

Settings live in `~/.config/living-ink/config.yml`. `living-ink config` edits
them; `living-ink info` shows every effective value and where it came from.

Every setting can also be given as an environment variable or, mostly, a CLI
flag. They resolve in one order: **flag → environment variable → stored
credential → config file → default.**

**API keys are never in `config.yml`.** They live one per file at
`~/.config/living-ink/credentials/`, mode `0600`. Caches, `state.db` and
`logs/pipeline.log` live in `~/.local/share/living-ink/`.

Full reference: [User Manual → Configuration](docs/USER_MANUAL.md#configuration-reference).

## How it works

1. **Download** — fetch changed documents over USB SSH or the reMarkable Cloud.
2. **Render** — turn vector strokes into high-resolution PNGs.
3. **Read** — one multimodal AI call per page returns clean, structured text.
4. **Publish** — write Markdown and page images into your vault.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Only a few notebooks synced | `sync.limit` caps a run. `0` means no cap — `living-ink sync --limit 0` takes all of them. |
| My annotated PDFs are ignored | Notebooks only, by default. `living-ink sync --pdf --epub`, or set `sync.types`. |
| Tablet not found over USB | Plug the cable in and enable the USB web interface. `living-ink info` prints the exact remedy, including the `ssh-copy-id` line. |
| Pages came back blank | Usually a rate limit. Lower `ocr.concurrency`. Failed pages publish a warning callout and are retried automatically on the next sync — only they are billed again. |
| A "Living Ink — sync failed" note appeared | Living Ink wrote it when a run failed. Read it, fix the run; it deletes itself on the next successful sync. |
| Scheduled syncs stopped | `living-ink info` shows the last and next scheduled run, and warns if one was missed. |

Logs: `~/.local/share/living-ink/logs/pipeline.log`.

## Documentation

- **[User Manual](docs/USER_MANUAL.md)** — every command, every option, examples
  and recipes.
- **[AGENTS.md](AGENTS.md)** — architecture and contributor guide. Not user
  documentation.

## Contributing

```bash
uv sync --all-extras
uv run ruff check .
uv run ruff format --check .
uv run pytest -v
```

Those four are exactly what CI runs, on Python 3.10 and 3.12, on Linux and
macOS. Branches are `feat/<description>` or `fix/<description>`; commits follow
[Conventional Commits](https://www.conventionalcommits.org/). Read
[AGENTS.md](AGENTS.md) before changing anything structural.

## License

[MIT](LICENSE).

## Acknowledgements

Built on top of the generic [remarkable-mcp](https://github.com/SamMorrowDrums/remarkable-mcp) project.
