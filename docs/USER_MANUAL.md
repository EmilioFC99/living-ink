# User Manual: Living Ink

## Overview

**Living Ink** is an automated pipeline that bridges the gap between your reMarkable tablet and your digital notes apps (**Obsidian** and **Apple Notes**). It converts your handwritten notebooks into fully searchable, typed notes while preserving the original handwriting drawings as embedded images ("Living Signal").

---

## How It Works

```
reMarkable Tablet
    ↓ (Cloud API or USB SSH)
Download .rm notebook zip
    ↓
Render pages: .rm → SVG → PNG
    ↓
OCR & Text Transcription:
    ├── AI Vision OCR (Default: Gemini, GPT-4o — reads handwriting + formats in 1 step)
    └── Google Cloud Vision (Optional fallback: DOCUMENT_TEXT_DETECTION → AI cleanup)
    ↓
Publish: Destination.publish()
    ├── Obsidian (Markdown + YAML frontmatter + WikiLinks + full folder mirroring)
    └── Apple Notes (Rich text note with embedded images)
```

1. **Detect**: Checks your reMarkable account for new or updated notebooks.
   - **New Notebooks**: Automatically detected and processed.
   - **Updated Notebooks**: When you write new pages or edit an existing note, Living Ink detects the version change and re-syncs.
   - **Trash**: Notebooks in the Trash folder (or prefixed with `[TRASH]`) are automatically ignored.
2. **Download & Render**: Downloads the notebook pages and converts vector strokes into clean PNG images.
3. **OCR & Text Processing**:
   - **AI Vision OCR (Recommended)**: Sends page images to multimodal AI models (**Google Gemini**, **OpenAI GPT-4o**), which read handwriting and format clean text in a single step using your existing AI API key.
   - **Google Cloud Vision (Optional)**: Traditional OCR fallback if configured, followed by AI text repair.
4. **Publish**: Writes structured notes to your active destinations (**Obsidian**, **Apple Notes**, or both).

---

## Folder Syncing & Mirroring

### Obsidian (Full Hierarchy Mirroring)
Living Ink replicates your **complete nested reMarkable folder structure** inside your Obsidian vault:

- **reMarkable:** `/Work/Projects/2026/Q1 Planning`
- **Obsidian:** `Vault/Living Ink/Work/Projects/2026/Q1 Planning.md`
- **Attachments:** `Vault/Living Ink/Work/Projects/2026/attachments/Q1 Planning_page-1.png`

You can customize this in `config/config.yml`:
- `root_folder`: Place all notes in a designated subfolder (e.g., `"Living Ink"`) or directly in the vault root (`""`).
- `mirror_folders`: Set to `true` to replicate nested folders, or `false` to store all notes flat in the root folder.
- `attachments_folder`: Subfolder name for page images (defaults to `"attachments"`).

### Apple Notes (Top-Level Folder Nesting)
- **reMarkable:** `/Work/Projects/Q1 Planning`
- **Apple Notes:** `Living Ink > Work > Q1 Planning`

---

## Usage

### Process All New Notes
To scan and sync all new or updated notebooks:
```bash
uv run python scripts/process_notebook.py
```

### Process a Specific Notebook
To force-sync a single notebook by its reMarkable name:
```bash
uv run python scripts/process_notebook.py --notebook "My Notebook Name"
```

### Command-Line Options
```text
options:
  -h, --help            Show help message and exit
  --notebook NOTEBOOK   Process only the specified notebook name
  --limit LIMIT         Max notebooks to process per run (overrides config)
  --folder FOLDER       Apple Notes folder override
```

---

## Configuration Reference

Edit `config/config.yml` (or `config.yml` in project root):

```yaml
# 1. AI Provider & Vision OCR
ai:
  provider: "gemini"               # "gemini", "openai", "ollama", "groq", "none", etc.
  api_key: "AIzaSy..."             # From https://aistudio.google.com/apikey
  model: "gemini-flash-latest"     # Default model

# 2. reMarkable Tablet Connection
remarkable:
  device_token: "YOUR-DEVICE-TOKEN-HERE"

# 3. Google Cloud Vision (OPTIONAL — Not needed if using Gemini or OpenAI)
google_vision:
  credentials_path: ""

# 4. Sync Settings
sync:
  max_notebooks_per_run: 5

# 5. Obsidian Destination
obsidian:
  enabled: true
  vault_path: "/Users/username/Documents/MyVault"
  root_folder: "Living Ink"
  mirror_folders: true
  attachments_folder: "attachments"

# 6. Apple Notes Destination
apple_notes:
  enabled: false
  folder_name: "Living Ink"
```

---

## Troubleshooting

### "Configuration Error"
Refer to `docs/SETUP_GUIDE.md` for credential setup:
- Living Ink only requires an **AI API key** (e.g. Google Gemini or OpenAI) and your **reMarkable token**.
- Google Cloud Vision credentials are **optional** and only required if you do not use an AI Vision provider.

### Note Not Appearing in Obsidian or Apple Notes?
1. **Cloud Sync**: Ensure your reMarkable tablet has finished syncing to the cloud (swipe down on the notebook list to force a sync).
2. **Processed State**: If a notebook was already synced and hasn't changed on the tablet, Living Ink skips it. Use `--notebook "Name"` to force reprocessing.
3. **Logs**: Check `logs/pipeline.log` for detailed step-by-step logs and error messages.

### Highlighting or Drawing Glitches
Living Ink includes built-in safeguards for reMarkable Paper Pro and newer pen formats (including color highlighters and shader tools). If you encounter rendering issues on custom pen types, check `logs/pipeline.log`.

---

## Data Privacy
- **AI Provider**: When using Google Gemini or OpenAI, page images and text prompts are processed according to the respective provider's privacy terms (e.g. Google AI Studio API data terms).
- **Ollama (Local)**: If you configure `provider: "ollama"`, all processing runs 100% locally on your machine with zero data sent to external AI servers.
- **Local Storage**: All rendered notes and image attachments are stored locally inside your Obsidian vault or Apple Notes database.
