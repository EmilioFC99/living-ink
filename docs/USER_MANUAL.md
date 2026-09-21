# Living Ink — User Manual

Every command, every option, and what each one actually does.

New here? Start with the [README](../README.md). This page is the reference you
come back to.

---

## Contents

- [How a sync decides what to do](#how-a-sync-decides-what-to-do)
- [Commands](#commands)
  - [Global flags](#global-flags)
  - [`sync`](#sync)
  - [`watch`](#watch)
  - [`setup`](#setup)
  - [`info`](#info)
  - [`config`](#config)
  - [`uninstall`](#uninstall)
  - [`completions`](#completions)
- [Configuration reference](#configuration-reference)
- [AI providers](#ai-providers)
- [The published note](#the-published-note)
- [Caches](#caches)
- [Scheduling](#scheduling)
- [Recipes](#recipes)
- [Troubleshooting](#troubleshooting)
- [Exit codes](#exit-codes)

---

## How a sync decides what to do

A run has four stages. Understanding them explains almost every question about
cost and behaviour.

1. **Discover** — list what is on the tablet. Titles, folders, versions, types.
   Nothing is downloaded yet.
2. **Select** — decide which documents are pending. Everything else is skipped,
   and a skip is free.
3. **Process** — download, render each page to a PNG, send each PNG to the AI
   model, get text back.
4. **Publish** — write the Markdown and the page images into your vault.

A document is **pending** when any of these is true:

| Reason | What triggers it |
|---|---|
| It changed | You edited it on the tablet, so its version is newer than the one recorded |
| The recipe changed | A setting that changes the *output* changed — the model, the prompt files, the vault folder, the render background |
| It has never been published | To this destination. State is per destination, not global |
| A previous run left gaps | Some pages failed to transcribe. Only those pages are re-read |
| You said so | `--force` |

Note the split: **version** answers "did the document change", the **recipe**
answers "would we produce different output from the same document". Both are
recorded, so switching to a better model re-reads your notebooks, and switching
back does not.

Selection also filters. In order: document type (`sync.types`), excluded folders
(`sync.exclude`), tags (`sync.tags`), then any `--notebook` / `--source-path` /
`--source-regex` you passed, then the limit (`sync.limit`).

**`sync --preview` runs stages 1 and 2 and stops.** It calls the same selector
the real run calls, so what it says is what would happen. It costs nothing.

---

## Commands

```
living-ink [global flags] <command> [command flags]
```

Bare `living-ink` syncs if you are configured, and runs `setup` if you are not.

### Global flags

| Flag | Meaning |
|---|---|
| `-h`, `--help` | Help for the program or the command before it |
| `-v`, `--version` | Print the version and exit |
| `-c PATH`, `--config PATH` | Use this `config.yml` instead of the default |
| `-q`, `--quiet` | The run report and nothing else |
| `--verbose` | A line per page |

`--config` also moves the credentials directory, because credentials are stored
next to the config that names them. That makes it a clean way to keep a second
profile:

```bash
living-ink --config ~/work-profile/config.yml sync
```

`-q` and `--verbose` are mutually exclusive, and both work as a global flag or
on the subcommand.

---

### `sync`

Run the pipeline now.

```
living-ink sync [flags]
```

Most flags are one-run overrides of a config setting — the same value, spelled
as a flag, winning for this run only. A handful have no config equivalent
because they only make sense on the command line; those are marked **run-only**.

#### Choosing what to sync

| Flag | Meaning |
|---|---|
| `--notebooks` | Include handwritten notebooks |
| `--pdf` | Include annotated PDFs |
| `--epub` | Include annotated EPUBs |
| `--tag TAG` | Only documents carrying one of these tablet tags. Repeatable, or comma-separated |
| `--exclude NAME` | Tablet folders never synced. Repeatable, or comma-separated |
| `--limit N` | Most documents in one run. `0` means no limit |
| `--notebook NAME` | **Run-only.** One document by name, folder path (`Work/Notes`) or document ID |
| `--source-path TEXT` | **Run-only.** Only documents whose full path contains this text, case-insensitive |
| `--source-regex PATTERN` | **Run-only.** Only documents whose full path matches this pattern, case-sensitive |
| `--force` | **Run-only.** Publish every selected document, even one the comparison calls unchanged |

Four things to know:

- **The type flags replace the configured list, they do not add to it.** Passing
  `--pdf` alone syncs PDFs *only*. For notebooks and PDFs together, pass both:
  `--notebooks --pdf`.
- **`--notebook` ignores the limit entirely.** If the name matches several
  documents they all sync. Use `--preview` first to see which.
- `--source-path` and `--source-regex` are mutually exclusive.
- **`--force` re-reads pages that are not in the cache and re-publishes
  everything.** It does not clear the transcript cache, so forcing an unchanged
  notebook is usually free. To genuinely re-transcribe, add
  `--no-transcript-cache`.

#### Rehearsing

| Flag | Meaning |
|---|---|
| `--preview` | Show what this exact command would do and exit. No download, no OCR, no API call |
| `--transcribe` | With `--preview`: also transcribe. Real OCR, nothing published |
| `--all` | With `--preview`: list every document instead of the first ten |

`--preview` is free and instant. `--preview --transcribe` is the expensive
rehearsal — it costs exactly what the real run would cost, and publishes
nothing. Because nothing is published, the documents stay pending; and because
the transcriptions land in the cache, the real run afterwards is nearly free.
Pair it with `--keep-temp` if you want to read the transcripts.

#### Connection

| Flag | Meaning | Setting |
|---|---|---|
| `--ssh` | Prefer the USB cable | `remarkable.preferred_connection` |
| `--cloud` | Prefer the reMarkable Cloud | `remarkable.preferred_connection` |
| `--ssh-host HOST` | Tablet address over USB | `remarkable.ssh_host` |
| `--ssh-user USER` | SSH user on the tablet | `remarkable.ssh_user` |
| `--ssh-port PORT` | SSH port on the tablet | `remarkable.ssh_port` |

`--ssh` and `--cloud` set which route is *tried first*, not which is allowed.
If both are configured and the preferred one fails, Living Ink falls back to the
other automatically, per call.

#### AI

| Flag | Meaning | Setting |
|---|---|---|
| `--ai-provider NAME` | `gemini`, `openai`, `ollama`, `groq`, `openrouter`, `mistral`, `together`, `custom`, `none` | `ai.provider` |
| `--ai-model NAME` | Model name. Empty means the provider's default | `ai.model` |
| `--ai-base-url URL` | Endpoint for a self-hosted or custom provider | `ai.base_url` |
| `--ai-temperature N` | Sampling temperature | `ai.temperature` |
| `--ai-language LANG` | Language to transcribe in, or `auto` | `ai.language` |
| `--ai-prompt-dir PATH` | Directory holding your own copies of the prompts | `ai.prompt_dir` |
| `--ocr-concurrency N` | Pages read in parallel. This is the rate-limit knob | `ocr.concurrency` |

Every one of these is part of the recipe, so changing one makes your documents
pending again and re-reads them. That is the point — a better model should
produce better notes — but it is also a bill, so check with `--preview` first.

#### Publishing

| Flag | Meaning | Setting |
|---|---|---|
| `--destination PATH` | Vault this run publishes into | `obsidian.vault_path` |
| `--destination-folder NAME` | Folder inside the vault everything lands under | `obsidian.root_folder` |
| `--mirror-folders` / `--no-mirror-folders` | Reproduce the tablet's folder tree | `obsidian.mirror_folders` |
| `--attachments-folder NAME` | Subfolder page images land in. Empty means beside the note | `obsidian.attachments_folder` |
| `--embed-images` / `--no-embed-images` | Embed page images alongside the text | `obsidian.embed_images` |
| `--skip-empty` | Do not publish a document whose pages all transcribe to nothing | `sync.skip_empty` |
| `--prune` | Delete published notes whose document is gone from the tablet | `sync.prune` |

**`--prune` deletes files.** Without it, a document deleted from the tablet is
reported as an orphan and its note is left alone. With it, the note is deleted —
but only if it still carries the `living_ink_id` that says Living Ink wrote it.
A note you took over is never deleted. Run `--preview --prune` first.

#### Caches and output

| Flag | Meaning | Setting |
|---|---|---|
| `--no-transcript-cache` | Do not reuse transcriptions across runs | `cache.transcripts` |
| `--no-render-cache` | Do not reuse rendered page images across runs | `cache.renders` |
| `--data-dir PATH` | Where runtime artifacts live | `paths.data_dir` |
| `--keep-temp` | **Run-only.** Keep rendered images, transcripts and the downloaded document after the run |
| `--json` | Print the run report as one JSON document instead of a table | `output.json` |
| `-q` / `--verbose` | Report only / a line per page | `output.verbosity` |

`--no-transcript-cache` means every page is billed again. It is the debugging
flag, not the daily one.

#### Examples

```bash
# What would happen — free
living-ink sync --preview
living-ink sync --preview --all              # the full list, not the first ten

# The whole backlog
living-ink sync --limit 0

# One notebook
living-ink sync --notebook "Work/Meeting Notes"
living-ink sync --notebook 8f3c1a9e-...      # by document ID

# Notebooks and annotated PDFs together
living-ink sync --notebooks --pdf

# Only what's tagged
living-ink sync --tag work --tag urgent

# Debug a bad render: keep the PNGs and the source
living-ink sync --notebook "Sketches" --keep-temp --verbose

# Cost the migration to a better model before paying for it
living-ink sync --ai-model gemini-2.5-pro --preview --transcribe --limit 3

# Publish somewhere else, once, without touching the config
living-ink sync --destination ~/Vaults/Archive --limit 0

# Script it
living-ink sync --json --quiet | jq '.documents[] | select(.status=="failed") | .name'
```

---

### `watch`

Sync on the configured schedule, until stopped.

```
living-ink watch
```

`watch` takes the output flags (`-q`, `--verbose`, `--json`) and **no behaviour
flags at all**. That is deliberate: a supervised process is restarted without
its arguments, so a `--limit` here would silently stop applying the first time
the daemon came back. What a scheduled run does is what the config says it does.

What it does each tick:

1. Reload the config, so an edit is picked up by the next run rather than a
   restart.
2. Take the run lock. If a sync is already going, this tick steps aside and
   records `skipped_overlapping` — the run already in flight will pick up
   whatever this one would have.
3. Sync.
4. Record the outcome in the `runs` table, with the fire time it was answering.

**Missed runs are caught up once, not replayed.** If your laptop was closed for
five days, the next start does one sync, not five. A sync reconciles what is on
the tablet *now*; replaying history would just be four wasted runs. And any run
counts as coverage — a manual `living-ink sync` ten minutes ago cancels this
morning's missed 09:00.

**One watcher per machine.** A second `watch` refuses loudly rather than
doubling your API bill.

On macOS, `setup` installs a LaunchAgent whose only job is keeping `watch`
alive — it restarts it on login and on crash. It does **not** hold the schedule;
that is in your config where you can read it. Stop the background watcher with:

```bash
launchctl unload ~/Library/LaunchAgents/com.livingink.sync.plist
```

On Linux, run `watch` under whatever supervisor you already use (systemd user
unit, supervisord, a terminal). Living Ink does not write a unit file for you,
and `uninstall` will not delete one you wrote.

---

### `setup`

The first-run wizard.

```
living-ink setup
```

It walks through:

1. **Tablet** — pick USB or Cloud, test the connection for real, and pair if
   needed.
2. **What to sync** — tick the document types you want (`sync.types`).
   Handwritten notebooks are ticked by default; annotated PDFs and EPUBs are
   the two you opt into, and each annotated page of one costs an AI call the
   same way a handwritten page does. Ticking nothing keeps notebooks.
3. **AI** — pick a provider, enter a key, and **verify it by sending a real
   image**. A text-only model fails here rather than at page 200.
4. **Vault** — find or type your Obsidian vault path, and choose the folder
   layout.
5. **Schedule** — optionally install the background watcher.
6. **Summary** — everything you chose, then a cost sketch for the first run.

**Nothing reaches disk until you confirm the summary.** Cancel at any point with
Ctrl+C and your existing config is untouched.

It needs a terminal. With no TTY it exits 2 immediately, before creating a
config directory or making a network call, and tells you to use environment
variables instead.

Safe to re-run at any time — it is also how you change tablets or re-pair.

---

### `info`

Read the setup and report whether it is healthy. Changes nothing.

```
living-ink info
living-ink info --json
```

It reports:

- **Connection** — whether the tablet answers over USB and/or Cloud, with the
  exact remedy when it does not (including the `ssh-copy-id` line to run).
- **AI** — provider, model, whether a key is present, masked.
- **Destination** — the vault path and whether it is writable.
- **Caches** — how many pages are cached and how much space they take.
- **Watch** — the last scheduled run and the next one.
- **Sync state** — how many documents are published, pending, or failed.

`--json` adds every sync-state row: one per `(document, destination)`, with the
version, where it landed, and when.

If a scheduled sync was due and did not happen, `info` prints a banner above
everything else. The grace period is 15 minutes, so a run that is merely late is
not an alarm.

**`info` reads, `config` writes.** The destructive maintenance — clearing caches,
repairing the database — lives in `config → Advanced`, because a command you run
to find out what is wrong should not be one keystroke from making it worse.

---

### `config`

Change settings, edit the prompts, run maintenance.

```
living-ink config
```

The menu is generated from the settings schema, so every value `info` prints has
a row here under the same name. Top level:

| Row | What is in it |
|---|---|
| `ai` | Which model reads your handwriting |
| `ocr` | How pages are read |
| `render` | How a page is drawn before it is read |
| `remarkable` | How to reach the tablet |
| `sync` | What gets synced, and what gets skipped |
| `obsidian` | Where notes are published |
| `watch` | The scheduled sync |
| `cache` | What is reused between runs |
| `paths` | Where runtime artifacts live |
| `output` | What a run prints |
| Prompts… | Edit what the model is told, in your editor |
| Advanced… | Caches and the sync database |

Three behaviours worth knowing:

- **Nothing is written until you save.** The save summary names every change,
  and "Discard and exit" is always the last row.
- **A save starts from your file, not from the resolved settings.** Your
  comments and ordering survive, and thirty-eight defaults are not materialised
  into the file just because you changed one thing. A section Living Ink does
  not recognise is preserved under its own heading.
- **An edit shadowed by an environment variable is flagged the moment you make
  it.** The precedence ladder makes it a no-op, and that is the only moment you
  can act on it.

#### Prompts…

Opens `ocr_prompt.txt` or `cleanup_prompt.txt` in `$EDITOR`.

The two prompts ship inside the package, which means an upgrade overwrites an
edited one. Set `ai.prompt_dir` to a directory of your own and Living Ink reads
your copies instead; the menu then offers to seed that directory with the
packaged text the first time, and opens *your* copy from then on.

Editing a prompt changes the transcription fingerprint, so every cached page
misses and your documents become pending. The menu says so before you leave it.

#### Advanced…

| Action | What it does |
|---|---|
| Clear the caches | Delete every cached page render and transcription |
| Prune old cache entries | Delete only what is past `cache.max_age_days` |
| Check the sync database | Read-only integrity check |
| Compact the sync database | Rebuild it to reclaim space |

Each asks for confirmation. **Clearing the transcript cache means the next sync
pays for every page again.** It does not touch your notes.

---

### `uninstall`

Remove what `setup` installed.

```
living-ink uninstall
living-ink uninstall --yes
```

Three tiers:

1. **Always removed, no question** — the background job and the caches. A
   LaunchAgent and a cache are things the program made for itself.
2. **Asked for, one question each** —
   - your settings and credentials (costs: a wizard run, and re-pairing),
   - the sync record (costs: re-transcribing every document — API calls, not
     inconvenience),
   - the `~/.local/bin/living-ink` shim, if it is a plain file. A symlink there
     belongs to `uv tool` and is left alone.
3. **Never removed** — your notes. Before anything is deleted, `uninstall`
   prints your vault path and says it is staying.

Each question prints what saying yes *costs*, above the prompt.

`--yes` answers everything yes. Without it and without a terminal, the command
exits 2 rather than guessing — no environment variable answers "may I delete
your credentials".

It does **not** remove the program. Uninstall the package the way you installed
it:

```bash
uv tool uninstall living-ink
```

On anything but macOS, the background job is left alone: a hand-written systemd
unit is not Living Ink's to delete.

---

### `completions`

```
living-ink completions {bash|zsh|fish}
```

Prints a completion script to stdout. It is generated from the installed
version's own argument parser, so it is never out of date with the build that
produced it — and regenerating it after an upgrade is the right move, rather
than editing it.

```bash
# zsh
living-ink completions zsh > ~/.zfunc/_living-ink
#   then, in ~/.zshrc, before compinit:  fpath+=(~/.zfunc)

# bash
living-ink completions bash > ~/.local/share/bash-completion/completions/living-ink

# fish
living-ink completions fish > ~/.config/fish/completions/living-ink.fish
```

The right target for your shell is also printed as a comment at the top of the
script itself, where it survives the redirect.

There is no `--install`: the correct directory depends on the shell, the
platform and the package manager, and a wrong guess leaves a stale file
shadowing the right one forever.

No credential is ever completable — a setting marked secret gets no flag at all,
so nothing can leak into a shell script or a history file.

---

## Configuration reference

### Where things live

| What | Where | Override |
|---|---|---|
| Config | `~/.config/living-ink/config.yml` | `--config`, `LIVING_INK_CONFIG`, `LIVING_INK_CONFIG_DIR` |
| Credentials | `<config dir>/credentials/`, one file each, mode `0600` | follows the config path |
| Caches, `state.db`, logs | `~/.local/share/living-ink/` | `--data-dir`, `LIVING_INK_DATA_DIR`, `paths.data_dir` |
| Log file | `~/.local/share/living-ink/logs/pipeline.log` | follows the data dir |

The repository and the installed package are stateless. Nothing is ever written
into the checkout.

### Precedence

Every setting resolves through one ladder, highest first:

1. **CLI flag** — `--ai-model gpt-4o`
2. **Environment variable** — `LIVING_INK_AI_MODEL=gpt-4o`
3. **Stored credential** — for secrets only
4. **Config file** — the current key, then any legacy spelling of it
5. **Default**

Two details that bite:

- **A blank string in the config file is a value, not an omission.**
  `obsidian.attachments_folder: ""` means "put the images beside the note". A
  blank *environment variable* is an omission, which is what a shell means by it.
- **`--limit 0` is a value.** It means no limit. It does not mean "unset".

`living-ink info` shows the resolved value of every setting and which layer it
came from.

### Settings

The **key** is what goes in `config.yml`. The **field** is the name `info` and
the config menu print.

#### `ai` — which model reads your handwriting

| Key | Default | Flag | Environment variable |
|---|---|---|---|
| `ai.provider` | — | `--ai-provider` | `LIVING_INK_AI_PROVIDER` |
| `ai.model` | provider's default | `--ai-model` | `LIVING_INK_AI_MODEL` |
| `ai.base_url` | — | `--ai-base-url` | `LIVING_INK_AI_BASE_URL` |
| `ai.temperature` | `0.3` | `--ai-temperature` | `LIVING_INK_AI_TEMPERATURE` |
| `ai.language` | `auto` | `--ai-language` | `LIVING_INK_AI_LANGUAGE` |
| `ai.prompt_dir` | — | `--ai-prompt-dir` | `LIVING_INK_AI_PROMPT_DIR` |
| `ai.repair_enabled` | `true` | — | `ENABLE_REPAIR` |
| *(credential)* API key | — | — | `LIVING_INK_AI_API_KEY` |

- `ai.language` — set a language name (`English`, `Español`, `Deutsch`) to stop
  a model code-switching on a mixed page. `auto` lets it infer.
- `ai.temperature` — low on purpose. Transcription is not a creative task.
- `ai.repair_enabled: false` **does not give you raw OCR.** There is only one
  OCR path, and it is the AI call. Turning this off is the same as
  `ai.provider: none`: `sync` refuses to start rather than publishing empty
  notes.
- The API key is kept **per provider**, so switching to Ollama and back does not
  destroy your Gemini key.

#### `ocr` — how pages are read

| Key | Default | Flag | Environment variable |
|---|---|---|---|
| `ocr.concurrency` | `5` | `--ocr-concurrency` | `SYNC_OCR_CONCURRENCY` |

Pages read in parallel. This is where the wall-clock time of a run goes, and it
is the rate-limit knob. Lower it to `1` or `2` if pages come back blank; raise it
if your provider has generous limits and you have a backlog.

#### `render` — how a page is drawn

| Key | Default | Flag | Environment variable |
|---|---|---|---|
| `render.background` | `#FBFBFB` | — | `REMARKABLE_BACKGROUND_COLOR` |

The paper colour behind a rendered page. Part of the render cache key, so
changing it re-renders every page — but not re-transcribes them, unless the text
changes.

#### `remarkable` — how to reach the tablet

| Key | Default | Flag | Environment variable |
|---|---|---|---|
| `remarkable.preferred_connection` | `ssh` | `--ssh` / `--cloud` | `REMARKABLE_PREFERRED_CONNECTION` |
| `remarkable.use_ssh` | `true` | — | `REMARKABLE_USE_SSH` |
| `remarkable.ssh_host` | `10.11.99.1` | `--ssh-host` | `REMARKABLE_SSH_HOST` |
| `remarkable.ssh_user` | `root` | `--ssh-user` | `REMARKABLE_SSH_USER` |
| `remarkable.ssh_port` | `22` | `--ssh-port` | `REMARKABLE_SSH_PORT` |
| *(credential)* SSH password | — | — | `REMARKABLE_SSH_PASSWORD` |
| *(credential)* Cloud token | — | — | `REMARKABLE_TOKEN` |

`10.11.99.1` is the tablet's address over the USB cable, with the USB web
interface enabled in the tablet's settings. The SSH password is the one shown on
the tablet under **Settings → Help → Copyrights and licenses**, at the bottom.

Set up an SSH key (`info` prints the exact `ssh-copy-id` line) and the password
is no longer needed.

The Cloud token is written by pairing during `setup`, not typed.

#### `sync` — what gets synced

| Key | Default | Flag | Environment variable |
|---|---|---|---|
| `sync.types` | `notebook` | `--notebooks`, `--pdf`, `--epub` | `SYNC_TYPES` |
| `sync.tags` | *(none)* | `--tag` | `SYNC_TAGS` |
| `sync.exclude` | `Trash`, `Templates`, `Quick sheets` | `--exclude` | `SYNC_EXCLUDE` |
| `sync.skip_empty` | `false` | `--skip-empty` | `SYNC_SKIP_EMPTY` |
| `sync.limit` | `1`, or `0` from the wizard | `--limit` | `SYNC_MAX_NOTEBOOKS` |
| `sync.prune` | `false` | `--prune` | `SYNC_PRUNE` |

- `sync.types` accepts any of `notebook`, `pdf`, `epub`. Only annotated PDFs and
  EPUBs produce anything: an unannotated PDF has no pages to render, and its
  embedded text layer is published on its own.
- **`sync.limit` is `0` — no limit — in a config the wizard wrote.** The schema
  default is `1`, and that applies only to a config you hand-wrote without the
  key: a low cap protects a *scripted* run from an accidentally enormous one,
  whereas a first run's whole job is to get the tablet into the vault.
- `sync.exclude` matches tablet folder names. Setting it replaces the default
  list, so include `Trash` yourself if you still want it skipped.
- `sync.skip_empty` — off by default, so an all-blank result is published rather
  than silently dropped. Turn it on if you keep scratch notebooks you never want
  as notes.

#### `obsidian` — where notes are published

| Key | Default | Flag | Environment variable |
|---|---|---|---|
| `obsidian.enabled` | `true` | — | `LIVING_INK_OBSIDIAN_ENABLED` |
| `obsidian.vault_path` | — | `--destination` | `LIVING_INK_OBSIDIAN_VAULT_PATH` |
| `obsidian.root_folder` | — | `--destination-folder` | `LIVING_INK_OBSIDIAN_ROOT_FOLDER` |
| `obsidian.mirror_folders` | `true` | `--mirror-folders` / `--no-…` | `LIVING_INK_OBSIDIAN_MIRROR_FOLDERS` |
| `obsidian.attachments_folder` | `_attachments` | `--attachments-folder` | `LIVING_INK_OBSIDIAN_ATTACHMENTS_FOLDER` |
| `obsidian.embed_images` | `true` | `--embed-images` / `--no-…` | `LIVING_INK_OBSIDIAN_EMBED_IMAGES` |

- `root_folder` — everything lands under this folder inside the vault. Leave it
  empty to publish at the vault root.
- `mirror_folders` — on, `Work/2026/Notes` on the tablet becomes
  `Work/2026/Notes.md`. Off, everything is flat.
- `attachments_folder` — set it to `""` to put page images directly beside the
  note instead of in a subfolder. Doing so changes the image filenames, because
  they then need the note's name to stay unique.

#### `watch` — the scheduled sync

| Key | Default | Environment variable |
|---|---|---|
| `watch.enabled` | `false` | `LIVING_INK_WATCH_ENABLED` |
| `watch.schedule` | — | `LIVING_INK_WATCH_SCHEDULE` |
| `watch.timezone` | the host's zone | `LIVING_INK_WATCH_TIMEZONE` |

See [Scheduling](#scheduling).

#### `cache` — what is reused between runs

| Key | Default | Flag | Environment variable |
|---|---|---|---|
| `cache.transcripts` | `true` | `--no-transcript-cache` | `SYNC_TRANSCRIPT_CACHE` |
| `cache.renders` | `true` | `--no-render-cache` | `SYNC_RENDER_CACHE` |
| `cache.max_age_days` | `90` | — | `SYNC_CACHE_MAX_AGE_DAYS` |

See [Caches](#caches).

#### `paths` and `output`

| Key | Default | Flag | Environment variable |
|---|---|---|---|
| `paths.data_dir` | `~/.local/share/living-ink` | `--data-dir` | `LIVING_INK_DATA_DIR` |
| `output.verbosity` | `normal` | `-q` / `--verbose` | `LIVING_INK_VERBOSITY` |
| `output.json` | `false` | `--json` | `LIVING_INK_OUTPUT_JSON` |

`output.verbosity` is one of `quiet`, `normal`, `verbose`.

### Credentials

Three secrets, none of them in `config.yml`:

| Secret | Environment variable |
|---|---|
| AI API key (one per provider) | `LIVING_INK_AI_API_KEY` |
| reMarkable Cloud token | `REMARKABLE_TOKEN` |
| Tablet SSH password | `REMARKABLE_SSH_PASSWORD` |

They live one per file in `<config dir>/credentials/`, written atomically at
mode `0600`. Anything that prints a key masks it (`AIza••••••••3f2a`), including
`info` and the config menu.

Nothing is exported into the environment for a subprocess to inherit — every
provider is handed its key explicitly, so an SSH subprocess or your `$EDITOR`
never sees one.

If you keep a key in the environment instead, it simply wins over the stored one
on the precedence ladder. Nothing is written to disk.

### Deprecated and removed keys

Old spellings keep working; you get a warning naming the replacement, and the
value is used. **No config file is ever rewritten** — the mapping happens in
memory at load time, so your comments and ordering survive.

| In your config | Status | Use instead |
|---|---|---|
| `openai:` | deprecated | `ai:` |
| `destination:` | deprecated | `obsidian:` |
| `use_ssh` (top level) | deprecated | `remarkable.use_ssh` |
| `sync.sync_pdfs`, `sync.sync_epubs` | removed | `sync.types` |
| `apple_notes:` | removed | — |
| `google_vision:` | removed | — |

A removed section is ignored with a warning, not an error — so an old config
still loads.

### A complete `config.yml`

```yaml
remarkable:
  preferred_connection: ssh      # try the USB cable first, fall back to cloud
  ssh_host: 10.11.99.1

ai:
  provider: gemini
  model: gemini-flash-latest
  language: auto
  temperature: 0.3
  # the API key is NOT here — it is in credentials/

ocr:
  concurrency: 5

sync:
  types: [notebook, pdf]
  exclude: [Trash, Templates, Quick sheets, Scratch]
  limit: 0
  skip_empty: false

obsidian:
  enabled: true
  vault_path: ~/Documents/Obsidian/Main
  root_folder: reMarkable
  mirror_folders: true
  attachments_folder: _attachments
  embed_images: true

watch:
  enabled: true
  schedule: "0 9,18 * * *"
  timezone: Europe/Madrid

cache:
  transcripts: true
  renders: true
  max_age_days: 90
```

---

## AI providers

Any endpoint that speaks the OpenAI chat API works. Adding one is a preset
entry, not code.

| `ai.provider` | Default model | Base URL | Key |
|---|---|---|---|
| `gemini` | `gemini-flash-latest` | Google | yes |
| `openai` | `gpt-4o-mini` | OpenAI | yes |
| `ollama` | `llama3.2` | `http://localhost:11434/v1` | no |
| `groq` | `llama-3.3-70b-versatile` | Groq | yes |
| `openrouter` | `google/gemini-2.0-flash-exp:free` | OpenRouter | yes |
| `mistral` | `mistral-small-latest` | Mistral | yes |
| `together` | `meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo` | Together | yes |
| `custom` | you set it | you set `ai.base_url` | depends |
| `none` | — | — | — |

**The model must be able to read images.** This is not optional and there is no
fallback: vision OCR is the only way a page is read. `setup` proves it by
sending the model a real image, so a text-only model fails at setup rather than
at page 200 of a backlog.

`ai.provider: none` — and equivalently `ai.repair_enabled: false` — makes `sync`
refuse to start. It is not a "raw OCR" mode; there is no raw OCR.

### Choosing one

- **Start with `gemini`.** Cheap, fast, good at handwriting, generous free tier.
- **Nothing leaves your machine:** `ollama` with a vision model, or `custom`
  pointed at hardware you control.
- **Best quality:** a frontier model. Set `ai.model` explicitly and rehearse the
  cost with `--preview --transcribe --limit 3` before re-reading your archive.
- **Hitting rate limits?** Lower `ocr.concurrency` before changing provider.

### Cost

One API call per page, per transcription. Living Ink reports the **number of API
calls** in the run report and deliberately does not guess a dollar figure —
there is no authoritative price table per provider and model. Multiply by your
own rate.

The things that keep the bill down, in order of effect:

1. The transcript cache. A repeat sync of an unchanged notebook costs zero.
2. `sync.limit`, for a first run against a large archive.
3. `--preview` (free) instead of guessing.
4. `sync.exclude`, so scratch folders never cost anything.

### What is sent

Per page: the page image, a fixed system message, and the contents of
`ocr_prompt.txt` — over HTTPS, to the provider you chose.

**Not sent:** the notebook title, the folder path, the tags, the vault path, the
document ID, or anything from any other page.

### Customising the prompts

Two plain-text files control everything the model is told:

- `ocr_prompt.txt` — how to read a page and what Markdown to produce.
- `cleanup_prompt.txt` — how to tidy text.

Edit them through `living-ink config → Prompts…`. To survive upgrades, set
`ai.prompt_dir` to a directory of your own first; the menu then seeds it from
the packaged text and opens your copy from then on.

Editing a prompt changes the transcription fingerprint, so cached pages miss and
your documents become pending — which is correct, and is also a bill. Rehearse
with `--preview --transcribe --limit 1` before letting it loose on an archive.

---

## The published note

### Structure

```markdown
---
living_ink_id: 8f3c1a9e-4b2d-...
created: 2026-03-02
updated: 2026-09-18
synced: 2026-09-20
source: Remarkable/Work/2026
type: notebook
document: Meeting Notes
tags: [meeting, q3]
---

<!-- living-ink:begin page-1 h=8f3c1a2e -->
## Page 1

# Q3 planning
- Ship the importer by the 14th
<!-- living-ink:end page-1 -->

Your commentary about page 1 goes here, and stays here forever.

<!-- living-ink:begin page-2 h=1d4f0b77 -->
## Page 2
...
<!-- living-ink:end page-2 -->
```

### Your edits are safe

The contract is written into the file:

1. **Free text is never written to.** Everything between one block's `end` and
   the next block's `begin` is yours, permanently. That is why blocks are
   per *page* — the place a reaction belongs is directly under the page it is
   about, not at the bottom of a forty-page dump.
2. **A block this run did not generate is not deleted.** A page that failed OCR
   cannot erase the block and strand your commentary underneath it. Deletion
   needs `--prune`.
3. **An edit inside a block is preserved, not overwritten.** The `h=` on the
   begin marker is a digest of what Living Ink last wrote there. A mismatch
   means you typed inside the boundaries, and both versions survive — yours is
   parked above with a comment saying so.
4. **Living Ink owns only its frontmatter keys** — `living_ink_id`, `created`,
   `updated`, `synced`, `source`, `type`, `document`, `tags`. Any other key you
   add is preserved in its original order and formatting. Tags you add by hand
   are kept and merged with the tablet's.

The markers are HTML comments, so Obsidian renders nothing and the note looks
no different to read.

### The three dates

They are three different facts, not one:

| Key | Means |
|---|---|
| `created` | Kept from the existing note if present; else when Living Ink first published it; else the tablet's modification date |
| `updated` | The tablet's own modification date — when *you* last wrote on it |
| `synced` | Today |

### Identity

`living_ink_id` is the document's ID on the tablet, and it is the identity — not
the title. Consequences:

- **Rename a notebook on the tablet and the note is renamed**, with its
  attachments, rather than a second note appearing.
- **Move it to another folder and the note moves.**
- A same-titled note belonging to a *different* document is stepped around:
  the new one becomes `Notes (2).md`.
- A note without a matching `living_ink_id` is never modified or deleted — if
  you took over a note, it is yours.

### Page numbering

A page carries the number the *document* calls it. An annotated 400-page PDF
with marks on pages 12 and 377 produces `## Page 12` and `## Page 377`, not
"Page 1" and "Page 2".

### Failed pages

A page that failed to transcribe publishes a marker instead of silently
publishing nothing:

```markdown
> [!warning] Page not transcribed
```

The document still publishes — a failed page does not fail the document — and
the failure is recorded, so the **next sync retries only the failed pages**. The
rest come from the cache. Retrying a 200-page notebook for three failed pages
costs three API calls.

If a page salvaged part of its transcription before failing, **both** are
published: the marker first, then the text. Discarding text you already paid for
would be worse than a gap.

### Attachments

Page images go to `obsidian.attachments_folder` (default `_attachments`) inside
the note's folder, and are embedded under each page unless `embed_images` is
off. Set the folder to `""` to put them directly beside the note.

### Deletions

A document that has been published but is no longer on the tablet is an
**orphan**. By default it is reported and nothing is deleted. `sync --prune`
deletes the note — but only if it still carries the matching `living_ink_id`.

### The failure note

When a run fails, Living Ink writes `Living Ink — sync failed.md` at the root of
your notes folder with a summary, so you find out in the place you actually
look. It carries a reserved ID and is deleted automatically on the next
successful sync. An interrupt (Ctrl+C) is not a failure and writes nothing, and
neither does a dry run.

---

## Caches

Two caches, both under the data directory, both on by default.

| Cache | Stores | Keyed by |
|---|---|---|
| Transcripts | The text of each page | The page image bytes + provider + model + temperature + language + both prompt files |
| Renders | The PNG of each page | The page's source + renderer versions + the renderer's own version + `render.background` + panel size |

Why it matters:

- **A repeat sync of an unchanged notebook is free.** No render, no API call.
- **A run is resumable.** Each page is banked the moment it comes back, so
  Ctrl+C costs the page in flight and nothing else. Re-run and it picks up.
- **A failed page retry is cheap.** Only the failed pages are billed again.
- **Changing the model or a prompt correctly misses**, because those are in the
  key. That is the intended behaviour, and it is also a bill.

The caches live outside the temp directories that are purged after every run —
surviving the purge is the whole point.

Turn them off with `cache.transcripts: false` / `cache.renders: false`, or
per-run with `--no-transcript-cache` / `--no-render-cache`. Clear or prune them
in `config → Advanced`. A successful sync prunes anything past
`cache.max_age_days` automatically. `info` reports their size.

### Temp artifacts

Rendered PNGs, transcript files and the downloaded document zip are purged at
pipeline start, after each document, and at exit. **When debugging rendering or
OCR, always pass `--keep-temp`** or they will be gone before you can look at
them.

Nothing implies `--keep-temp`. If you want the transcripts a
`--preview --transcribe` rehearsal produced, ask for them explicitly.

---

## Scheduling

One cron expression in `config.yml`, in a place you can read and change:

```yaml
watch:
  enabled: true
  schedule: "0 9,18 * * *"
  timezone: Europe/Madrid
```

`living-ink config → watch` offers the common ones and shows the next three fire
times for whatever you pick, including a custom expression:

| Preset | Expression |
|---|---|
| Off — sync only when I ask | *(none)* |
| Every day at 09:00 | `0 9 * * *` |
| Twice a day, 09:00 and 18:00 | `0 9,18 * * *` |
| Every hour | `0 * * * *` |
| Every Monday at 09:00 | `0 9 * * 1` |

Format is standard five-field cron: `minute hour day month weekday`.

`watch.timezone` is an IANA zone name (`Europe/Madrid`, `America/New_York`).
Unset means the host's zone.

### Daylight saving

Handled explicitly, because getting it wrong makes a 09:00 schedule silently
drift by an hour:

- A time that **does not exist** on the spring-forward day fires at the first
  instant after the gap.
- A time that **happens twice** on the autumn day fires once, on the first
  occurrence.

### Missed runs

Catch-up is **one tick, not a backlog**. Five missed dailies are one useful
sync, because a sync reconciles what is on the tablet now. Any run counts as
coverage: a manual `living-ink sync` cancels a missed scheduled one.

`info` shows the last scheduled run and the next, and warns if one was due more
than 15 minutes ago and did not happen. It ignores manual runs when judging
whether the *schedule* is healthy, so a hand-run `sync` cannot make a broken
schedule look fine.

### Overlap

Two locks, two different questions:

- **One watcher per machine.** A second `watch` refuses loudly.
- **One sync at a time.** A tick that finds a sync already running steps aside
  and records `skipped_overlapping`. Two syncs writing one vault could
  double-write a note, and the run already going will pick up whatever this one
  would have.

### Run outcomes

Each run is recorded with what asked for it (manual or scheduled) and which fire
time it was answering:

| Outcome | Healthy? |
|---|---|
| `success` | yes |
| `nothing_to_do` | yes |
| `skipped_overlapping` | yes — it did the right thing |
| `partial` | no — some documents failed |
| `error` | no |
| `interrupted` | no — Ctrl+C |

---

## Recipes

### First run against a large archive

```bash
living-ink sync --preview --all          # see the whole backlog, free
living-ink sync --limit 3                # prove the output looks right
living-ink sync --limit 0                # then the rest
```

### Try a different model without paying twice

```bash
living-ink sync --ai-model gemini-2.5-pro --preview --transcribe --limit 3 --keep-temp
# read the transcripts, then:
living-ink config                        # make it permanent
living-ink sync --limit 0                # cached — nearly free
```

Because `--preview --transcribe` publishes nothing, those three documents stay
pending; because their pages are now cached, the real run does not pay again.

### Only sync what you tagged on the tablet

```yaml
sync:
  tags: [publish]
```

Tag a notebook `publish` on the tablet, and nothing else syncs.

### Keep work and personal in separate vaults

```bash
living-ink --config ~/.config/living-ink-work/config.yml sync
```

Separate config, separate credentials, separate sync state. Point each at its
own vault and use `sync.tags` or `sync.exclude` to split the documents.

### Fully local, nothing leaves the machine

```yaml
ai:
  provider: ollama
  model: llama3.2-vision
```

Requires Ollama running locally with a vision-capable model pulled. No API key.

### Re-transcribe one notebook from scratch

```bash
living-ink sync --notebook "Meeting Notes" --force --no-transcript-cache
```

`--force` alone re-publishes from the cache. Add `--no-transcript-cache` to
actually re-read the pages.

### Clean up notes for deleted documents

```bash
living-ink sync --preview --prune        # see what would be deleted
living-ink sync --prune                  # do it
```

### Script a sync and act on failures

```bash
living-ink sync --json --quiet > run.json || echo "sync failed"
jq -r '.documents[] | select(.status == "failed") | .name' run.json
```

Each document reports `name`, `doc_id`, `status` (`published`, `skipped`,
`failed`, or `would_publish` under `--preview`), `pages`, `transcribed`,
`cached`, `pages_failed`, `destinations` and `reason`. Exit code is 0 on
success, 1 on failure, 130 on Ctrl+C.

---

## Troubleshooting

### Only a few notebooks synced

`sync.limit` caps a run. The wizard writes `0`, which means no cap, so this is
a config that was hand-written or edited since. Use `living-ink sync --limit 0`
for one run, or change `sync.limit` in `config → sync`.

### My annotated PDFs are ignored

`sync.types` is `notebook` by default. Use `living-ink sync --notebooks --pdf`,
or set `sync.types: [notebook, pdf]`.

Remember the flags **replace** the list — `--pdf` alone means PDFs only.

### The tablet is not found over USB

1. Plug the cable in.
2. On the tablet: **Settings → Storage → USB web interface**, enabled.
3. `living-ink info` — it prints the exact remedy, including the `ssh-copy-id`
   line for key-based login.
4. Try `living-ink sync --cloud` to confirm the tablet is not the problem.

### "No OCR method available"

Your provider cannot read images, or `ai.provider` is `none`, or
`ai.repair_enabled` is `false`. There is no non-AI OCR path. Set `ai.provider`
to one with vision support and re-run `setup` to verify the key against a real
image.

### Pages came back blank

Almost always a rate limit. Lower `ocr.concurrency` to `2` and re-run — only the
failed pages are billed, because the rest are cached.

Failed pages publish a `> [!warning] Page not transcribed` callout rather than
disappearing, and are retried automatically on the next sync.

### A notebook is "skipped: every page is blank"

The document genuinely transcribed to nothing on every page — a template, or a
notebook with only a light sketch. If you expected text, check the rendered PNGs:

```bash
living-ink sync --notebook "That One" --force --keep-temp --verbose
```

### The transcription is wrong or badly formatted

Edit `ocr_prompt.txt` via `config → Prompts…`. Set `ai.prompt_dir` first so your
edit survives an upgrade. Then re-transcribe:

```bash
living-ink sync --notebook "That One" --force --no-transcript-cache
```

For mixed-language pages, set `ai.language` explicitly rather than `auto`.

### My own text disappeared from a note

It should not, and Living Ink never writes to free text. Check for a
`<!-- living-ink: your edit to ... -->` comment — that is where an edit made
*inside* a generated block is parked when it is detected.

If text is genuinely gone, that is a bug worth reporting, with the note and the
log.

### A "Living Ink — sync failed" note appeared in my vault

A run failed and left you a summary where you would see it. Read it, fix the
cause, run `living-ink sync` — the note deletes itself on success.

### Scheduled syncs stopped

```bash
living-ink info     # last scheduled run, next one, and a banner if one was missed
```

Then check the watcher is alive. On macOS:

```bash
launchctl list | grep livingink
```

Remember: `watch` takes no behaviour flags. If a scheduled run does less than
you expect, the answer is in `config.yml`, not in the plist.

### Blank or clipped page renders after an upgrade

Living Ink patches the `rmc` renderer to control SVG background and bounds.
Upgrading `rmc` or `rmscene` is the likely cause. Clear the render cache
(`config → Advanced`) and re-render; if it persists, pin the previous versions
and open an issue.

### Where are the logs?

```
~/.local/share/living-ink/logs/pipeline.log
```

Or `<data dir>/logs/pipeline.log` if you moved it. `--verbose` prints a line per
page to the console as well.

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Failure — something you have to fix |
| `2` | Usage error, or an interactive command with no terminal |
| `130` | Interrupted with Ctrl+C |

A run where some documents failed and others succeeded exits `1` and is recorded
as `partial`.
