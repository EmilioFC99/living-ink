#!/usr/bin/env python3
"""Process notebooks: preprocess PNGs, run OCR, aggregate text, publish notes."""

import datetime
import json
import logging
import os
import re
import sys
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from PIL import Image, ImageFilter, ImageOps

# Ensure living_ink is importable
sys.path.append(str(Path(__file__).parent.parent))

from living_ink.clean import configure as configure_ai_provider
from living_ink.clean import ocr_and_repair, repair_text_with_openai, vision_ocr_available
from living_ink.config import find_repo_root, get_config_path, get_data_dir, get_logs_dir
from living_ink.destinations import (
    AppleNotesDestination,
    Destination,
    DestinationError,
    ObsidianDestination,
)


# --- LOGGING SUPPRESSION ---
# Suppress specific benign warnings from rmscene/rmc that scare users
class WarningFilter(logging.Filter):
    def filter(self, record):
        try:
            msg = record.getMessage()
            if any(
                p in msg
                for p in (
                    "Unknown formatting code",
                    "Some data has not been read",
                    "Unknown block type",
                )
            ):
                return False
        except Exception:
            pass
        return True


# Apply filter and elevate log level for rmscene / rmc
_suppress_filter = WarningFilter()
for logger_name in [
    "rmscene",
    "rmscene.tagged_block_reader",
    "rmscene.scene_stream",
    "rmscene.scene_tree",
    "rmscene.text",
    "rmc",
]:
    _l = logging.getLogger(logger_name)
    _l.addFilter(_suppress_filter)
    _l.setLevel(logging.ERROR)


ROOT = find_repo_root()

# All user runtime artifacts (PNGs, PDFs, OCR texts, logs, state) live under standard XDG DATA_DIR
DATA_DIR = get_data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)

WHITE_DIR = DATA_DIR / "remarkable_pngs_white"
VISION_DIR = DATA_DIR / "remarkable_pngs_for_vision"
OCR_DIR = DATA_DIR / "output"  # OCR text files
PDF_DIR = DATA_DIR / "remarkable_pdfs"
DOCS_DIR = DATA_DIR / "remarkable_documents"
LOGS_DIR = get_logs_dir()
LOGS_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOGS_DIR / "pipeline.log"

WHITE_DIR.mkdir(parents=True, exist_ok=True)
VISION_DIR.mkdir(parents=True, exist_ok=True)
OCR_DIR.mkdir(parents=True, exist_ok=True)
PDF_DIR.mkdir(parents=True, exist_ok=True)
DOCS_DIR.mkdir(parents=True, exist_ok=True)


# --- CONFIGURATION LOADING (YAML) ---
def load_yaml_config(config_path: Optional[Path] = None) -> Dict[str, Any]:
    """Load configuration from YAML and apply settings to environment.

    Args:
        config_path: Path to YAML config file. Defaults to get_config_path().

    Returns:
        Dictionary containing the parsed YAML configuration.
    """
    cfg_path = config_path or get_config_path()
    yaml_config: Dict[str, Any] = {}

    if cfg_path.exists():
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                try:
                    yaml_config = yaml.safe_load(f) or {}
                except yaml.YAMLError as ye:
                    print("\n❌ CONFIGURATION ERROR: Could not parse config.yml")
                    print("Please check your indentation. YAML is very sensitive to spaces.")
                    if hasattr(ye, "problem_mark"):
                        mark = ye.problem_mark
                        print(f"Error position: line {mark.line + 1}, column {mark.column + 1}")
                    print(f"Details: {ye}\n")
                    yaml_config = {}

                # 1. OpenAI
                if "openai" in yaml_config and "api_key" in yaml_config["openai"]:
                    os.environ.setdefault(
                        "OPENAI_API_KEY", str(yaml_config["openai"]["api_key"]).strip()
                    )

                # 2. reMarkable
                if "remarkable" in yaml_config:
                    rm_cfg = yaml_config["remarkable"]

                    if "preferred_connection" in rm_cfg and rm_cfg["preferred_connection"]:
                        os.environ.setdefault(
                            "REMARKABLE_PREFERRED_CONNECTION",
                            str(rm_cfg["preferred_connection"]).strip().lower(),
                        )

                    if "device_token" in rm_cfg and rm_cfg["device_token"]:
                        os.environ.setdefault(
                            "REMARKABLE_TOKEN", str(rm_cfg["device_token"]).strip()
                        )

                    # SSH connection settings
                    ssh_enabled = (
                        rm_cfg.get("use_ssh") if "use_ssh" in rm_cfg else yaml_config.get("use_ssh")
                    )
                    if ssh_enabled is not None and "REMARKABLE_USE_SSH" not in os.environ:
                        if isinstance(ssh_enabled, bool):
                            os.environ["REMARKABLE_USE_SSH"] = "true" if ssh_enabled else "false"
                        elif str(ssh_enabled).strip().lower() in ("1", "true", "yes"):
                            os.environ["REMARKABLE_USE_SSH"] = "true"
                        else:
                            os.environ["REMARKABLE_USE_SSH"] = "false"

                    if "ssh_host" in rm_cfg and rm_cfg["ssh_host"]:
                        os.environ.setdefault(
                            "REMARKABLE_SSH_HOST", str(rm_cfg["ssh_host"]).strip()
                        )

                    if "ssh_port" in rm_cfg and rm_cfg["ssh_port"]:
                        os.environ.setdefault(
                            "REMARKABLE_SSH_PORT", str(rm_cfg["ssh_port"]).strip()
                        )

                    if "ssh_user" in rm_cfg and rm_cfg["ssh_user"]:
                        os.environ.setdefault(
                            "REMARKABLE_SSH_USER", str(rm_cfg["ssh_user"]).strip()
                        )

                # 3. Google Vision (Handle JSON content directly or file path)
                if "google_vision" in yaml_config:
                    gv = yaml_config["google_vision"]

                    # Option A: Path to JSON file (Preferred for humans)
                    if "credentials_path" in gv and gv["credentials_path"]:
                        path_str = str(gv["credentials_path"]).strip()
                        # Handle typical user paths like ~/Documents
                        expanded_path = os.path.expanduser(path_str)

                        if os.path.exists(expanded_path):
                            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = expanded_path
                        else:
                            print(
                                f"❌ Config Error: credentials_path file not found at: {path_str}"
                            )

                    # Option B: Embedded JSON content
                    elif "credentials_json" in gv:
                        creds_content = gv["credentials_json"]

                        # Validate if it looks like JSON
                        if isinstance(creds_content, str):
                            creds_content = creds_content.strip()
                            if not creds_content.startswith("{"):
                                print(
                                    "⚠️ Warning: 'credentials_json' in config.yml does not start with '{'. Did you forget the indentation?"
                                )

                        if isinstance(creds_content, dict):
                            creds_content = json.dumps(creds_content)

                        # Write to config/google_creds.json
                        creds_path = cfg_path.parent / "google_creds.json"
                        try:
                            if not creds_path.exists() or creds_path.read_text() != creds_content:
                                creds_path.write_text(creds_content)
                            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(creds_path)
                        except Exception as weave_err:
                            print(f"❌ Error writing google_creds.json: {weave_err}")

                # 4. Sync Settings (Global vars that will be picked up later)
                if "sync" in yaml_config:
                    if "max_notebooks_per_run" in yaml_config["sync"]:
                        os.environ["SYNC_MAX_NOTEBOOKS"] = str(
                            yaml_config["sync"]["max_notebooks_per_run"]
                        )
                    if "sync_pdfs" in yaml_config["sync"]:
                        val = yaml_config["sync"]["sync_pdfs"]
                        os.environ["SYNC_PDFS"] = (
                            "true" if str(val).strip().lower() in ("1", "true", "yes") else "false"
                        )
                    if "sync_epubs" in yaml_config["sync"]:
                        val = yaml_config["sync"]["sync_epubs"]
                        os.environ["SYNC_EPUBS"] = (
                            "true" if str(val).strip().lower() in ("1", "true", "yes") else "false"
                        )

                # 5. Apple Notes Settings
                if "apple_notes" in yaml_config:
                    if "folder_name" in yaml_config["apple_notes"]:
                        os.environ["APPLE_NOTES_FOLDER"] = str(
                            yaml_config["apple_notes"]["folder_name"]
                        )

                # 6. AI Provider — initialize from new 'ai' section or legacy 'openai' section
                configure_ai_provider(yaml_config)

        except Exception as e:
            print(f"Critical error loading config.yml: {e}")

    # Legacy Fallback
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    return yaml_config


YAML_CONFIG_PATH = get_config_path()
yaml_config = load_yaml_config(YAML_CONFIG_PATH)

max_notebooks_per_run = int(os.environ.get("SYNC_MAX_NOTEBOOKS", 1))

# --- DESTINATION SETUP ---


def get_destinations_from_config(config_dict) -> List[Destination]:
    """Factory to create a list of enabled Destinations based on config."""
    dests = []

    # 1. Check for Apple Notes
    # Enabled by default if not explicitly disabled or if folder is set
    an_config = config_dict.get("apple_notes", {})
    an_enabled = an_config.get("enabled", True)  # Default true

    # Check if we were explicitly told to target something else in the old 'destination' key
    if "destination" in config_dict:
        if (
            isinstance(config_dict["destination"], str)
            and config_dict["destination"] != "apple_notes"
        ):
            an_enabled = False
        elif (
            isinstance(config_dict["destination"], dict)
            and config_dict["destination"].get("type") != "apple_notes"
        ):
            an_enabled = False

    if an_enabled:
        folder = os.environ.get("APPLE_NOTES_FOLDER", an_config.get("folder_name", "reMarkable"))
        dests.append(AppleNotesDestination(folder_name=folder))
        print(f"Destination added: Apple Notes (Folder: {folder})")

    # 2. Check for Obsidian
    # Check legacy 'destination' key first
    obs_config = config_dict.get("obsidian", {})
    obs_enabled = obs_config.get("enabled", False)  # Default false unless configured
    vault_path = obs_config.get("vault_path")

    # Legacy config support
    if "destination" in config_dict:
        d = config_dict["destination"]
        if isinstance(d, dict) and d.get("type") == "obsidian":
            obs_enabled = True
            vault_path = d.get("vault_path", vault_path)

    if obs_enabled:
        if vault_path:
            root_folder = obs_config.get("root_folder")
            mirror_folders = obs_config.get("mirror_folders", True)
            attachments_folder = obs_config.get("attachments_folder", "_attachments")
            dests.append(
                ObsidianDestination(
                    vault_path=vault_path,
                    attachments_folder=attachments_folder,
                    root_folder=root_folder,
                    mirror_folders=mirror_folders,
                )
            )
            folder_info = f" (Root: {root_folder})" if root_folder else ""
            print(f"Destination added: Obsidian (Vault: {vault_path}{folder_info})")
        else:
            print("⚠️ Obsidian enabled but 'vault_path' is missing. Skipping.")

    return dests


# Construct global destinations list
ACTIVE_DESTINATIONS = get_destinations_from_config(yaml_config)


def get_state_file_path(dest_name: str) -> Path:
    """Get the path to the state file for a specific destination."""
    new_path = DATA_DIR / f"processed_notebooks_{dest_name}.json"
    legacy_path = ROOT / f"processed_notebooks_{dest_name}.json"
    # Automatically migrate legacy state file from root to data directory if present
    if not new_path.exists() and legacy_path.exists() and new_path != legacy_path:
        try:
            legacy_path.rename(new_path)
        except Exception:
            return legacy_path
    return new_path


def load_processed_log(dest_name: str):
    log_path = get_state_file_path(dest_name)
    if log_path.exists():
        try:
            with open(log_path, "r") as f:
                data = json.load(f)
                # Handle legacy format (list of IDs) - backward compatibility
                if isinstance(data, list):
                    return {doc_id: 0 for doc_id in data}
                # Handle new format (dict of ID -> Version)
                return data
        except Exception:
            return {}
    return {}


def add_to_processed_log(dest_name: str, doc_id, version):
    processed = load_processed_log(dest_name)
    processed[doc_id] = version
    log_path = get_state_file_path(dest_name)
    with open(log_path, "w") as f:
        json.dump(processed, f, indent=2, sort_keys=True)


def preprocess_image(in_path: Path, out_path: Path):
    im = Image.open(in_path)
    # Always composite onto a white background, regardless of mode
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        bg.paste(im, (0, 0), im if im.mode == "RGBA" else None)
        im = bg.convert("RGB")
    else:
        im = im.convert("RGB")

    # Autocontrast
    im = ImageOps.autocontrast(im, cutoff=2)

    # Upscale 1.5x (rounded)
    w, h = im.size
    im = im.resize((int(w * 1.5), int(h * 1.5)), resample=Image.Resampling.LANCZOS)

    # Sharpen
    im = im.filter(ImageFilter.SHARPEN)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    im.save(out_path, quality=95)


# --- Google Vision OCR using API key (legacy) ---
def google_vision_available() -> bool:
    """Check if Google Cloud Vision credentials are configured and valid.

    Returns:
        bool: True if Google Cloud Vision service account credentials exist.
    """
    creds_env = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if creds_env and Path(creds_env).exists():
        try:
            content = Path(creds_env).read_text(encoding="utf-8")
            if "your-project-id" not in content and "BEGIN PRIVATE KEY" in content:
                return True
        except Exception:
            pass
    secrets_dir = ROOT / "secrets"
    if secrets_dir.exists() and list(secrets_dir.glob("*.json")):
        return True
    return False


# --- Google Vision OCR using service account (preferred) ---
def vision_ocr_image_service_account(png_path: Path):
    try:
        from google.cloud import vision
    except ImportError:
        print("google-cloud-vision not installed. Please run: uv add google-cloud-vision")
        return None

    try:
        client = vision.ImageAnnotatorClient()
        with open(png_path, "rb") as f:
            content = f.read()
        image = vision.Image(content=content)
        response = client.document_text_detection(image=image)
        if response.error.message:
            print(f"Vision API error: {response.error.message}")
            return None
        if response.full_text_annotation and response.full_text_annotation.text:
            return response.full_text_annotation.text.strip()
        return ""
    except Exception as e:
        print(f"Google Cloud Vision error: {e}")
        return None


def sanitize_filename(name: str) -> str:
    """Make a notebook name safe for a temporary working-file path.

    Note:
        This is deliberately *not* the same as
        ``ObsidianDestination._sanitize_filename``. This one names throwaway
        artifacts under the data directory, so it collapses spaces to
        underscores for shell-friendliness; the Obsidian one names files the
        user will see in their vault, so it keeps spaces and uses hyphens.
        Merging the two would silently rename every note in existing vaults.

    Args:
        name: Raw notebook name.

    Returns:
        A path-safe variant of the name.
    """
    return name.replace("/", "_").replace("\\", "_").replace(" ", "_")


def log(msg):
    print(msg)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"{datetime.datetime.now().isoformat()} {msg}\n")


def cleanup_temp_artifacts(keep_temp: bool = False) -> None:
    """Clean all temporary working folders (PNG, OCR, PDF, Vision, Documents) and zip archives.

    Args:
        keep_temp: If True, preserve files on disk for debugging.
    """
    if keep_temp:
        log("Preserving temporary working files (--keep-temp enabled).")
        return

    import shutil

    temp_folders = [WHITE_DIR, VISION_DIR, OCR_DIR, PDF_DIR, DOCS_DIR]
    for folder in temp_folders:
        if folder.exists():
            for item in folder.iterdir():
                try:
                    if item.is_file() or item.is_symlink():
                        item.unlink()
                    elif item.is_dir():
                        shutil.rmtree(item)
                except Exception as e:
                    logging.debug("Failed to remove temporary item %s: %s", item, e)
            folder.mkdir(parents=True, exist_ok=True)

    # Clean any lingering zip archives in DATA_DIR
    if DATA_DIR.exists():
        for zip_file in DATA_DIR.glob("*.zip"):
            try:
                zip_file.unlink(missing_ok=True)
            except Exception:
                pass


def clean_notebook_temp_artifacts(safe_notebook: str, keep_temp: bool = False) -> None:
    """Clean temporary artifacts for a specific completed notebook.

    Args:
        safe_notebook: Sanitized notebook name prefix.
        keep_temp: If True, preserve files on disk.
    """
    if keep_temp:
        return

    import shutil

    for folder in [WHITE_DIR, OCR_DIR, PDF_DIR, DOCS_DIR]:
        if folder.exists():
            for p in folder.glob(f"{safe_notebook}*"):
                try:
                    if p.is_file() or p.is_symlink():
                        p.unlink()
                    elif p.is_dir():
                        shutil.rmtree(p)
                except Exception:
                    pass

    if VISION_DIR.exists():
        for p in VISION_DIR.glob(f"{safe_notebook}*"):
            try:
                if p.is_file() or p.is_symlink():
                    p.unlink()
                elif p.is_dir():
                    shutil.rmtree(p)
            except Exception:
                pass


def validate_environment():
    """Check configuration health and fail fast with helpful docs if missing."""
    docs_path = ROOT / "docs" / "SETUP_GUIDE.md"
    docs_hint = f"See {docs_path} for instructions."

    errors = []
    warnings = []

    # 1. Check AI Provider (replaces hardcoded OpenAI check)
    from living_ink.clean import _get_provider
    from living_ink.providers import NoneProvider

    provider = _get_provider()
    if isinstance(provider, NoneProvider):
        warnings.append(
            "⚠️ No AI text cleanup provider configured. "
            "OCR text will not be cleaned up. "
            "To enable, add an 'ai' section to config.yml."
        )

    # 2. Check Google Credentials
    # The config loader above sets GOOGLE_APPLICATION_CREDENTIALS if a key exists in config.yml
    # Or users might have put a file in secrets/ (legacy)
    has_creds = False

    # Check env var (set by config.yml loader or system)
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        # Check if it points to a file with default placeholder content
        p = Path(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
        if p.exists():
            content = p.read_text()
            if "your-project-id" in content or "BEGIN PRIVATE KEY" not in content:
                errors.append(
                    "❌ Google Credentials JSON still has default placeholder values. Please edit config.yml."
                )
            else:
                has_creds = True
        else:
            errors.append(f"❌ Google Credentials file defined but not found at {p}")

    # Check Legacy Secrets Dir
    if not has_creds:
        secrets_dir = ROOT / "secrets"
        if secrets_dir.exists() and list(secrets_dir.glob("*.json")):
            has_creds = True

    if not has_creds:
        if vision_ocr_available():
            # AI vision OCR available — Google Vision not needed
            warnings.append(
                "ℹ️ No Google Cloud Vision credentials found. "
                "Using AI vision OCR instead (reads images directly)."
            )
        else:
            errors.append(
                "❌ No OCR method available. Either:\n"
                "   • Add an AI provider with vision support (e.g., Gemini) to config.yml, OR\n"
                "   • Add Google Cloud Vision credentials (see SETUP_GUIDE.md)."
            )

    # Print warnings (non-fatal)
    for w in warnings:
        log(w)
        print(w)

    if errors:
        msg = "\n".join(errors)
        log("\n" + "=" * 60)
        log("CONFIGURATION ERROR")
        log("=" * 60)
        log(msg)
        log("-" * 60)
        log(docs_hint)
        log("=" * 60 + "\n")

        print("\n" + "=" * 60)
        print("CONFIGURATION ERROR")
        print("=" * 60)
        print(msg)
        print("-" * 60)
        print(docs_hint)
        print("=" * 60 + "\n")

        if sys.stdin.isatty():
            try:
                prompt_text = "Would you like to run the interactive setup wizard now? [Y/n]: "
                choice = input(prompt_text).strip().lower()
                if choice in ("", "y", "yes"):
                    from living_ink.setup_wizard import run_wizard

                    run_wizard()
                    sys.exit(0)
            except (KeyboardInterrupt, EOFError):
                pass

        # Hard exit if non-interactive or user declines
        sys.exit(1)

    log("Configuration valid.")


def get_val(item: Any, key: str) -> Any:
    """Safely get a property or dictionary key from a document item."""
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, getattr(item, key.lower(), None))


def get_notebook_path(item: Any, id_map: Dict[str, Any]) -> str:
    """Construct the folder path for an item using the ID lookup map."""
    path = []
    current = item
    while get_val(current, "Parent"):
        parent_id = get_val(current, "Parent")
        if parent_id == "trash":
            path.insert(0, "[TRASH]")
            break
        parent = id_map.get(parent_id)
        if parent:
            parent_name = get_val(parent, "VissibleName") or get_val(parent, "VisibleName")
            path.insert(0, parent_name)
            current = parent
        else:
            break
    return " / ".join(path)


def normalize_path_str(path_str: str) -> str:
    """Normalize a path string by stripping whitespace around slashes and lowercasing."""
    parts = [p.strip().lower() for p in path_str.replace("\\", "/").split("/") if p.strip()]
    return "/".join(parts)


def matches_notebook_target(item: Any, target_str: str, id_map: Dict[str, Any]) -> bool:
    """Check if a document matches a target string by ID, name, or folder path.

    Args:
        item: Document item.
        target_str: Search target (name, folder path, or document UUID).
        id_map: Map of ID -> Document for resolving parent folders.

    Returns:
        True if the item matches the target.
    """
    t = target_str.strip()
    if not t:
        return False

    # 1. Exact ID match (case-insensitive)
    doc_id = str(get_val(item, "ID") or getattr(item, "id", "") or "").strip()
    if doc_id.lower() == t.lower():
        return True

    # 2. Name match (case-insensitive, exact or substring)
    name = str(
        get_val(item, "VissibleName")
        or get_val(item, "VisibleName")
        or getattr(item, "name", "")
        or ""
    ).strip()
    if name.lower() == t.lower() or t.lower() in name.lower():
        return True

    # 3. Path match: e.g. "Work/Notes" or "Work / Notes"
    folder_path = get_notebook_path(item, id_map)
    if folder_path:
        full_spaced = f"{folder_path} / {name}"
        full_slash = f"{folder_path}/{name}"
        t_norm = normalize_path_str(t)
        norm_spaced = normalize_path_str(full_spaced)
        norm_slash = normalize_path_str(full_slash)
        if t_norm == norm_spaced or t_norm == norm_slash or t_norm in norm_spaced:
            return True

    return False


def get_document_type(item: Any, client: Optional[Any] = None) -> str:
    """Determine whether an item is a 'notebook', 'pdf', or 'epub'.

    Args:
        item: The document/metadata item or dict.
        client: Optional API client to query for file type.

    Returns:
        One of 'notebook', 'pdf', or 'epub'.
    """
    if client is not None and hasattr(client, "get_file_type"):
        try:
            ft = client.get_file_type(item)
            if ft in ("pdf", "epub"):
                return ft
        except Exception:
            pass

    files = get_val(item, "files") or []
    for f in files:
        fid = str(f.get("id") if isinstance(f, dict) else getattr(f, "id", "")).lower()
        if fid.endswith(".pdf"):
            return "pdf"
        if fid.endswith(".epub"):
            return "epub"

    name = str(
        get_val(item, "VissibleName")
        or get_val(item, "VisibleName")
        or getattr(item, "name", "")
        or ""
    ).lower()
    if name.endswith(".pdf"):
        return "pdf"
    if name.endswith(".epub"):
        return "epub"

    return "notebook"


def format_notebook_item(item: Any, id_map: Dict[str, Any], client: Optional[Any] = None) -> str:
    """Format a notebook item description for display in selection prompts."""
    name = str(
        get_val(item, "VissibleName")
        or get_val(item, "VisibleName")
        or getattr(item, "name", "")
        or "Untitled"
    )
    folder = get_notebook_path(item, id_map)
    title = f"{folder} / {name}" if folder else name
    doc_id = str(get_val(item, "ID") or getattr(item, "id", "") or "")
    short_id = doc_id[:8] if len(doc_id) > 8 else doc_id

    doc_type = get_document_type(item, client)
    type_badge = f" [{doc_type.upper()}]" if doc_type in ("pdf", "epub") else ""

    mod_val = get_val(item, "ModifiedClient") or getattr(item, "last_modified", None)
    mod_str = ""
    if mod_val:
        if isinstance(mod_val, datetime.datetime):
            mod_str = f" (modified: {mod_val.strftime('%Y-%m-%d %H:%M')})"
        elif isinstance(mod_val, (int, float)):
            try:
                ts = float(mod_val)
                if ts > 1e11:
                    ts = ts / 1000
                mod_str = (
                    f" (modified: {datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')})"
                )
            except Exception:
                pass

    id_label = f" [ID: {short_id}]" if short_id else ""
    return f"{title}{type_badge}{id_label}{mod_str}"


def select_notebook_interactive(
    matches: List[Any],
    query: str,
    id_map: Dict[str, Any],
    input_func=input,
    print_func=print,
    is_interactive: Optional[bool] = None,
) -> List[Any]:
    """Prompt the user to choose from multiple matching notebooks.

    Args:
        matches: List of matching notebook items.
        query: The user-supplied --notebook query.
        id_map: Map of ID -> Document for path resolution.
        input_func: Function for reading user input.
        print_func: Function for printing messages.
        is_interactive: Whether terminal is interactive (defaults to sys.stdin.isatty()).

    Returns:
        List of selected notebook items to process. Empty list if cancelled.
    """
    if len(matches) <= 1:
        return matches

    if is_interactive is None:
        is_interactive = sys.stdin.isatty()

    if not is_interactive:
        print_func(
            f"ℹ️ Multiple notebooks ({len(matches)}) match '{query}' in non-interactive mode. Processing all."
        )
        return matches

    print_func("")
    print_func(f"Found {len(matches)} notebooks matching '{query}':")
    for idx, it in enumerate(matches, 1):
        desc = format_notebook_item(it, id_map)
        print_func(f"  [{idx}] {desc}")
    print_func(f"  [a] Process all {len(matches)} matching notebooks")
    print_func("  [q] Cancel / Quit")
    print_func("")

    while True:
        try:
            raw = (
                input_func(f"Select a notebook [1-{len(matches)}, a, q] (default: a): ")
                .strip()
                .lower()
            )
        except (KeyboardInterrupt, EOFError):
            print_func("\nCancelled by user.")
            return []

        if raw in ("", "a", "all"):
            return matches
        if raw in ("q", "quit", "exit"):
            print_func("Cancelled by user.")
            return []
        if raw.isdigit():
            num = int(raw)
            if 1 <= num <= len(matches):
                selected = [matches[num - 1]]
                sel_title = format_notebook_item(selected[0], id_map)
                print_func(f"Selected: {sel_title}")
                return selected

        print_func(f"Invalid selection '{raw}'. Please enter 1-{len(matches)}, 'a', or 'q'.")


@dataclass(frozen=True)
class SyncOptions:
    """Per-run choices for a single sync, separate from the persisted config.

    These are the knobs a caller sets at invocation time — the CLI flags, in
    practice. Config supplies the defaults; a field left at ``None`` means
    "no override, use whatever config says". The boolean flags default to
    ``False`` rather than ``None`` because they are store-true switches with no
    meaningful third state.

    Kept frozen so a pipeline's options cannot drift underneath it mid-run; use
    :meth:`merged_with` to derive a variant.

    Attributes:
        notebook: Target a single notebook by name, folder path, or document ID.
        limit: Maximum number of notebooks to process. None or 0 means "use config".
        folder: Apple Notes folder override.
        ssh: Force the USB SSH transport.
        cloud: Force the reMarkable Cloud transport.
        preferred_connection: Explicit transport preference ('ssh' or 'cloud'),
            used when neither ``ssh`` nor ``cloud`` is set.
        sync_pdfs: Include PDF documents. None means "use config".
        sync_epubs: Include EPUB documents. None means "use config".
        all_types: Include every document type; overrides sync_pdfs/sync_epubs.
        keep_temp: Preserve rendered PNGs and OCR transcripts for debugging.
    """

    notebook: Optional[str] = None
    limit: Optional[int] = None
    folder: Optional[str] = None
    ssh: bool = False
    cloud: bool = False
    preferred_connection: Optional[str] = None
    sync_pdfs: Optional[bool] = None
    sync_epubs: Optional[bool] = None
    all_types: bool = False
    keep_temp: bool = False

    @classmethod
    def from_args(cls, args: Any) -> "SyncOptions":
        """Build options from a parsed argparse namespace.

        This is the single place that knows CLI flag names, so adding a flag
        means touching the parser and this method — not the pipeline internals.

        Note:
            ``--sync-pdfs`` / ``--sync-epubs`` are store-true flags, so an unset
            flag is mapped to None ("defer to config") rather than to False
            ("explicitly disable"), which would silently override the config.

        Args:
            args: Namespace produced by the sync subparser.

        Returns:
            A populated SyncOptions.
        """
        return cls(
            notebook=getattr(args, "notebook", None),
            limit=getattr(args, "limit", None),
            folder=getattr(args, "folder", None),
            ssh=getattr(args, "ssh", False),
            cloud=getattr(args, "cloud", False),
            sync_pdfs=getattr(args, "sync_pdfs", False) or None,
            sync_epubs=getattr(args, "sync_epubs", False) or None,
            all_types=getattr(args, "all_types", False),
            keep_temp=getattr(args, "keep_temp", False),
        )

    def merged_with(self, **overrides: Any) -> "SyncOptions":
        """Return a copy with the supplied non-None fields replaced.

        Args:
            **overrides: Field names and values. None values are ignored so
                callers can pass through optional arguments unconditionally.

        Returns:
            A new SyncOptions; the receiver is unchanged.
        """
        return replace(self, **{k: v for k, v in overrides.items() if v is not None})


class SyncPipeline:
    """Orchestrator for syncing reMarkable notebooks to configured destinations.

    Encapsulates configuration, runtime options, document discovery, rendering,
    OCR text extraction, cleanup, and publication to destinations.
    """

    def __init__(
        self,
        options: Optional[SyncOptions] = None,
        config_path: Optional[Path] = None,
        data_dir: Optional[Path] = None,
        destinations: Optional[List[Destination]] = None,
    ):
        """Initialize the SyncPipeline by resolving options against configuration.

        Every per-run knob arrives in ``options``; config supplies the defaults
        that the options do not override. The resolution happens once, here, so
        that by the time :meth:`run` is called the pipeline's state is settled.

        Args:
            options: Per-run overrides. Defaults to an all-defaults SyncOptions,
                i.e. "do exactly what the config says".
            config_path: Path to YAML config file. Defaults to standard config path.
            data_dir: Path to runtime data directory. Defaults to standard data dir.
            destinations: Explicit list of destinations. Defaults to active destinations from config.
        """
        self.options = options or SyncOptions()
        opts = self.options

        self.config_path = config_path or get_config_path()
        self.data_dir = data_dir or DATA_DIR
        self.keep_temp = opts.keep_temp

        if self.config_path and self.config_path != YAML_CONFIG_PATH:
            self.raw_config = load_yaml_config(self.config_path)
            self.destinations = (
                destinations
                if destinations is not None
                else get_destinations_from_config(self.raw_config)
            )
        else:
            self.raw_config = yaml_config
            self.destinations = (
                destinations if destinations is not None else list(ACTIVE_DESTINATIONS)
            )

        # 1. Connection properties
        rm_cfg = self.raw_config.get("remarkable", {})
        base_pref = (
            rm_cfg.get("preferred_connection")
            or os.environ.get("REMARKABLE_PREFERRED_CONNECTION")
            or "ssh"
        )
        base_ssh = rm_cfg.get("use_ssh")
        if base_ssh is None:
            base_ssh = os.environ.get("REMARKABLE_USE_SSH", "true").lower() in (
                "1",
                "true",
                "yes",
            )

        if opts.ssh:
            self.preferred_connection = "ssh"
            self.use_ssh = True
        elif opts.cloud:
            self.preferred_connection = "cloud"
            self.use_ssh = False
        elif opts.preferred_connection:
            self.preferred_connection = opts.preferred_connection.strip().lower()
            self.use_ssh = self.preferred_connection == "ssh"
        else:
            self.preferred_connection = str(base_pref).strip().lower()
            self.use_ssh = bool(base_ssh)

        os.environ["REMARKABLE_PREFERRED_CONNECTION"] = self.preferred_connection
        os.environ["REMARKABLE_USE_SSH"] = "true" if self.use_ssh else "false"

        # 2. Document types and limits
        sync_cfg = self.raw_config.get("sync", {})
        cfg_sync_pdfs = sync_cfg.get(
            "sync_pdfs",
            os.environ.get("SYNC_PDFS", "false").strip().lower() in ("1", "true", "yes"),
        )
        cfg_sync_epubs = sync_cfg.get(
            "sync_epubs",
            os.environ.get("SYNC_EPUBS", "false").strip().lower() in ("1", "true", "yes"),
        )
        cfg_limit = int(
            sync_cfg.get(
                "max_notebooks_per_run",
                os.environ.get("SYNC_MAX_NOTEBOOKS", max_notebooks_per_run),
            )
        )

        self.target_notebook = opts.notebook.strip() if opts.notebook else None
        self.all_types = opts.all_types

        if opts.all_types:
            self.sync_pdfs = True
            self.sync_epubs = True
        else:
            self.sync_pdfs = bool(cfg_sync_pdfs) if opts.sync_pdfs is None else opts.sync_pdfs
            self.sync_epubs = bool(cfg_sync_epubs) if opts.sync_epubs is None else opts.sync_epubs

        if opts.limit is not None and opts.limit > 0:
            self.limit = opts.limit
        else:
            self.limit = cfg_limit

        # 3. Destination folder
        self.folder = (
            opts.folder
            or os.environ.get("APPLE_NOTES_FOLDER")
            or self.raw_config.get("apple_notes", {}).get("folder_name", "Living Ink")
        )
        if self.folder:
            os.environ["APPLE_NOTES_FOLDER"] = self.folder
            for dest in self.destinations:
                if isinstance(dest, AppleNotesDestination):
                    dest.folder_name = self.folder

    def connect(self) -> Any:
        """Establish connection to reMarkable tablet (via SSH or Cloud)."""
        from living_ink.api import get_rmapi

        return get_rmapi()

    def discover_documents(self, client: Any) -> Tuple[List[Any], Dict[str, Any]]:
        """Discover documents in the tablet library matching configured document types.

        Returns:
            Tuple of (candidate_documents_list, id_map_dictionary).
        """
        collection = client.get_meta_items()
        id_map = {get_val(item, "ID"): item for item in collection}

        is_targeted = bool(self.target_notebook)

        candidates = [
            item
            for item in collection
            if get_val(item, "Type") == "DocumentType"
            and (get_val(item, "VissibleName") or get_val(item, "VisibleName"))
            and not get_notebook_path(item, id_map).startswith("[TRASH]")
        ]

        notebooks = []
        skipped_pdfs = 0
        skipped_epubs = 0
        for item in candidates:
            dtype = get_document_type(item, client)
            if dtype == "notebook":
                notebooks.append(item)
            elif dtype == "pdf":
                if self.sync_pdfs or is_targeted:
                    notebooks.append(item)
                else:
                    skipped_pdfs += 1
            elif dtype == "epub":
                if self.sync_epubs or is_targeted:
                    notebooks.append(item)
                else:
                    skipped_epubs += 1

        if skipped_pdfs > 0:
            log(f"Skipped {skipped_pdfs} PDF documents (enable with --sync-pdfs or in config.yml).")
        if skipped_epubs > 0:
            log(
                f"Skipped {skipped_epubs} EPUB documents (enable with --sync-epubs or in config.yml)."
            )

        return notebooks, id_map

    def filter_pending_documents(
        self, notebooks: List[Any], id_map: Dict[str, Any]
    ) -> Tuple[List[Any], Dict[str, List[Destination]], bool]:
        """Determine which notebooks need updating for active destinations.

        Returns:
            Tuple of (notebooks_to_process, needs_update_map, should_continue_bool).
        """
        active_dests = self.destinations or ACTIVE_DESTINATIONS
        needs_update: Dict[str, List[Destination]] = {}
        dest_states = {}
        for dest in active_dests:
            dest_name = type(dest).__name__
            dest_states[dest_name] = load_processed_log(dest_name)

        for item in notebooks:
            doc_id = get_val(item, "ID")
            curr_val = get_val(item, "hash")
            if not curr_val:
                try:
                    curr_val = int(get_val(item, "Version"))
                except (ValueError, TypeError):
                    curr_val = 1

            for dest in active_dests:
                dest_name = type(dest).__name__
                last_val = dest_states[dest_name].get(doc_id, -1)

                if str(last_val) != str(curr_val):
                    if doc_id not in needs_update:
                        needs_update[doc_id] = []
                    needs_update[doc_id].append(dest)

        if self.target_notebook:
            target_name = self.target_notebook
            log(f"Filtering for notebook: {target_name}")

            matched_items = [
                item for item in notebooks if matches_notebook_target(item, target_name, id_map)
            ]

            if not matched_items:
                log(f"Notebook '{target_name}' not found in library. Exiting.")
                return [], {}, False

            selected_items = select_notebook_interactive(
                matches=matched_items,
                query=target_name,
                id_map=id_map,
            )

            if not selected_items:
                log("Sync cancelled by user. Exiting.")
                return [], {}, True

            for it in selected_items:
                doc_id = get_val(it, "ID")
                needs_update[doc_id] = active_dests

            return selected_items, needs_update, True

        else:
            candidates = [item for item in notebooks if get_val(item, "ID") in needs_update]

            if not candidates:
                log("No new or updated notebooks found for any active destination. Exiting.")
                return [], {}, True

            if self.limit > 0:
                candidates = candidates[: self.limit]

            return candidates, needs_update, True

    def process_notebook_item(
        self,
        nb_item: Any,
        client: Any,
        id_map: Dict[str, Any],
        needs_update: Dict[str, List[Destination]],
        keep_temp: Optional[bool] = None,
    ) -> bool:
        """Process a single notebook or document item through extraction, OCR, and publishing.

        Args:
            nb_item: reMarkable item metadata.
            client: reMarkable API client.
            id_map: Mapping from document ID to metadata item.
            needs_update: Mapping from document ID to target destinations.
            keep_temp: Whether to keep temporary files on disk.

        Returns:
            True if notebook was processed and published successfully, False otherwise.
        """
        notebook = get_val(nb_item, "VissibleName") or get_val(nb_item, "VisibleName")
        notebook_id = get_val(nb_item, "ID")
        doc_type = get_document_type(nb_item, client)
        effective_keep_temp = self.keep_temp if keep_temp is None else keep_temp

        # Get the value to store after processing (Hash or Version)
        item_hash = get_val(nb_item, "hash")
        if item_hash:
            notebook_version = item_hash
        else:
            try:
                notebook_version = int(get_val(nb_item, "Version"))
            except (ValueError, TypeError):
                notebook_version = 1

        safe_notebook = sanitize_filename(notebook)

        # Determine Path and Display Title
        folder_path = get_notebook_path(nb_item, id_map)
        if folder_path:
            display_title = f"{folder_path} / {notebook}"
        else:
            display_title = notebook

        type_badge = f" ({doc_type.upper()})" if doc_type != "notebook" else ""
        log(f"Processing {doc_type}: {display_title}{type_badge} (ID: {notebook_id})")

        notebook_tags: List[str] = []

        # Step 1: Pull and render pages / extract document
        prefix_pattern = safe_notebook + "."
        imgs = sorted(
            [
                p
                for p in WHITE_DIR.iterdir()
                if p.name.startswith(prefix_pattern) and p.suffix.lower() == ".png"
            ]
        )
        doc_file_path = None
        extracted_doc_text = ""

        if doc_type in ("pdf", "epub"):
            doc_file_path = DOCS_DIR / f"{safe_notebook}.{doc_type}"

        if not imgs:
            log(
                f"No white-background PNGs found for {notebook}. Attempting to pull from reMarkable..."
            )
            doc = nb_item
            if not doc:
                log(f'Document "{notebook}" not found in your reMarkable library. Skipping.')
                return False

            from living_ink.extract import (
                extract_raw_document_from_zip,
                extract_tags_from_zip,
                extract_text_from_epub,
                extract_text_from_pdf,
                get_document_page_count,
                get_pdf_annotated_page_map,
                render_composite_pdf_page,
                render_page_from_document_zip,
                render_pdf_page_preview,
            )

            tmp_zip = DATA_DIR / f"{safe_notebook}.zip"
            raw_bytes = client.download(doc)
            if not raw_bytes:
                log(f"Failed to download document zip for {notebook}.")
                return False
            with open(tmp_zip, "wb") as f:
                f.write(raw_bytes)

            zip_tags = extract_tags_from_zip(tmp_zip)
            if zip_tags:
                notebook_tags.extend(zip_tags)

            if doc_type == "pdf":
                # 1. Extract raw PDF
                extract_raw_document_from_zip(tmp_zip, doc_file_path)
                if not doc_file_path.exists() and hasattr(client, "download_raw_file"):
                    raw_pdf_bytes = client.download_raw_file(doc, "pdf")
                    if raw_pdf_bytes:
                        doc_file_path.write_bytes(raw_pdf_bytes)

                # 2. Check for annotated pages
                annotated_pages = get_pdf_annotated_page_map(tmp_zip)
                if annotated_pages and doc_file_path.exists():
                    log(f"Rendering {len(annotated_pages)} annotated pages for PDF '{notebook}'...")
                    with zipfile.ZipFile(tmp_zip, "r") as zf:
                        for p_info in annotated_pages:
                            rm_name = p_info["rm_file_name"]
                            rm_data = zf.read(rm_name) if rm_name in zf.namelist() else b""
                            page_num = p_info["page_num"]
                            comp_bytes = render_composite_pdf_page(
                                doc_file_path, p_info["pdf_page_index"], rm_data
                            )
                            if comp_bytes:
                                out_img = WHITE_DIR / f"{safe_notebook}.page-{page_num}.png"
                                out_img.write_bytes(comp_bytes)
                                log(f"Saved annotated page: {out_img}")
                elif doc_file_path.exists():
                    log(
                        f"PDF '{notebook}' has no handwritten annotations. Extracting text & cover preview..."
                    )
                    cover_bytes = render_pdf_page_preview(doc_file_path, 0)
                    if cover_bytes:
                        out_img = WHITE_DIR / f"{safe_notebook}.page-1.png"
                        out_img.write_bytes(cover_bytes)
                        log(f"Saved cover preview: {out_img}")
                    extracted_doc_text = extract_text_from_pdf(doc_file_path)

            elif doc_type == "epub":
                # 1. Extract raw EPUB
                extract_raw_document_from_zip(tmp_zip, doc_file_path)
                if not doc_file_path.exists() and hasattr(client, "download_raw_file"):
                    raw_epub_bytes = client.download_raw_file(doc, "epub")
                    if raw_epub_bytes:
                        doc_file_path.write_bytes(raw_epub_bytes)

                if doc_file_path.exists():
                    extracted_doc_text = extract_text_from_epub(doc_file_path)

                # If there are any .rm files, render them as page images
                page_count = get_document_page_count(tmp_zip)
                if page_count > 0:
                    log(f"Rendering {page_count} annotation pages for EPUB '{notebook}'...")
                    for page in range(1, page_count + 1):
                        png_bytes = render_page_from_document_zip(tmp_zip, page)
                        if png_bytes:
                            out_img = WHITE_DIR / f"{safe_notebook}.page-{page}.png"
                            out_img.write_bytes(png_bytes)
                            log(f"Saved: {out_img}")

            else:  # Standard notebook
                page_count = get_document_page_count(tmp_zip)
                if page_count == 0:
                    log(f"Notebook '{notebook}' has 0 pages (empty notebook). Skipping.")
                    tmp_zip.unlink(missing_ok=True)
                    return True

                log(f"Rendering {page_count} pages for {notebook}...")
                for page in range(1, page_count + 1):
                    png_bytes = render_page_from_document_zip(tmp_zip, page)
                    if png_bytes is None:
                        log(f"Failed to render page {page} of {notebook}.")
                        continue
                    out_img = WHITE_DIR / f"{safe_notebook}.page-{page}.png"
                    out_img.write_bytes(png_bytes)
                    log(f"Saved: {out_img}")

            # Remove temp zip
            tmp_zip.unlink(missing_ok=True)
            # Re-scan for white PNGs
            imgs = sorted(
                [
                    p
                    for p in WHITE_DIR.iterdir()
                    if p.name.startswith(prefix_pattern) and p.suffix.lower() == ".png"
                ]
            )
            if not imgs and not extracted_doc_text:
                log(f"No pages or text could be extracted for '{notebook}'. Skipping.")
                return False

        log(f"Found {len(imgs)} white-background PNGs for {notebook}: {[p.name for p in imgs]}")

        if not notebook_tags:
            from living_ink.api import get_document_tags

            fallback_tags = get_document_tags(client, nb_item)
            if fallback_tags:
                notebook_tags.extend(fallback_tags)

        if notebook_tags:
            log(f"Tags found for '{notebook}': {notebook_tags}")

        # Preprocess images
        pre_dir = VISION_DIR / safe_notebook
        pre_dir.mkdir(parents=True, exist_ok=True)
        pre_paths = []
        for p in imgs:
            out_p = pre_dir / p.name
            preprocess_image(p, out_p)
            pre_paths.append(out_p)

        # OCR — try AI vision first, fall back to Google Cloud Vision
        use_vision_ocr = vision_ocr_available()
        if use_vision_ocr:
            log("Using AI vision OCR (single-step: reads image + cleans text)")
        else:
            log("Using Google Cloud Vision OCR + AI text cleanup")

        raw_texts = []
        cleaned_texts = []
        if pre_paths:
            for p in pre_paths:
                if use_vision_ocr:
                    # Single-step: AI reads the image and returns clean text
                    log(f"  AI Vision OCR: {p.name}...")
                    cleaned_text = ocr_and_repair(str(p))
                    if cleaned_text:
                        raw_texts.append(cleaned_text)  # No separate raw text in vision mode
                        cleaned_texts.append(cleaned_text)
                        continue
                    # Vision returned None/empty — fall through to Google Vision if configured
                    log(f"  AI Vision returned empty for {p.name}")
                    if google_vision_available():
                        log("  Falling back to Google Cloud Vision...")
                    else:
                        log(
                            f"  Google Cloud Vision not configured; skipping OCR fallback for {p.name}."
                        )
                        raw_texts.append("")
                        cleaned_texts.append("")
                        continue

                # Two-step: Google Cloud Vision OCR → AI text cleanup
                if google_vision_available():
                    log(f"  Google Vision OCR: {p.name}...")
                    txt = vision_ocr_image_service_account(p)
                    if txt is None:
                        log(f"  Vision failed for {p}")

                    raw_text = txt or ""
                    raw_texts.append(raw_text)

                    log(f"  Cleaning text with AI for {p.name}...")
                    cleaned_text = repair_text_with_openai(raw_text)
                    cleaned_texts.append(cleaned_text)
                else:
                    log(f"  Google Cloud Vision not configured for {p.name}.")
                    raw_texts.append("")
                    cleaned_texts.append("")
                    continue

        if not any(t.strip() for t in cleaned_texts) and extracted_doc_text:
            raw_texts = [extracted_doc_text]
            cleaned_texts = [extracted_doc_text]

        from living_ink.extract import format_page_section_header

        def _get_page_pnum(idx: int) -> int:
            if idx < len(imgs):
                page_m = re.search(r"page-(\d+)", imgs[idx].name, re.IGNORECASE)
                if page_m:
                    return int(page_m.group(1))
            return idx + 1

        # Save RAW text
        raw_out_txt = OCR_DIR / f"{safe_notebook}_raw.txt"
        meta = {"notebook": notebook, "images": [p.name for p in imgs]}
        with open(raw_out_txt, "w", encoding="utf-8") as f:
            f.write(json.dumps(meta) + "\n\n")
            if raw_texts == [extracted_doc_text] and extracted_doc_text:
                f.write(extracted_doc_text + "\n")
            else:
                for i, t in enumerate(raw_texts):
                    header = format_page_section_header(
                        _get_page_pnum(i), doc_file_path, include_divider=True
                    )
                    f.write(f"{header}\n\n{(t or '').strip()}\n\n")
        log(f"Raw OCR text saved to {raw_out_txt}")

        # Save CLEANED text (this is what goes to Apple Notes / Obsidian)
        clean_out_txt = OCR_DIR / f"{safe_notebook}_clean.txt"
        with open(clean_out_txt, "w", encoding="utf-8") as f:
            f.write(json.dumps(meta) + "\n\n")
            if cleaned_texts == [extracted_doc_text] and extracted_doc_text:
                f.write(extracted_doc_text + "\n")
            else:
                for i, t in enumerate(cleaned_texts):
                    header = format_page_section_header(
                        _get_page_pnum(i), doc_file_path, include_divider=True
                    )
                    body = (t or "").strip()
                    if body:
                        f.write(f"{header}\n\n{body}\n\n")
                    else:
                        f.write(f"{header}\n\n")
        log(f"Cleaned OCR text saved to {clean_out_txt}")

        # Create Note (Apple Notes or Obsidian)
        success = False
        try:
            # Prepare text content (extract from saved file)
            full_text = clean_out_txt.read_text(errors="ignore") if clean_out_txt.exists() else ""

            # Skip the first JSON line/metadata and find first page marker or divider
            lines = full_text.split("\n")
            text_start = 0
            for i, line in enumerate(lines):
                if line.startswith("---") or line.startswith("###"):
                    text_start = i
                    break
            clean_text = "\n".join(lines[text_start:]).strip()

            # Determine folder paths for nesting
            full_subfolder = None
            top_level_subfolder = None
            if folder_path:
                parts = [p.strip() for p in folder_path.split(" / ") if p.strip()]
                if parts:
                    top_level_subfolder = sanitize_filename(parts[0])
                    full_subfolder = "/".join(parts)

            # Destinations that specifically request this notebook
            targets = needs_update.get(notebook_id, [])
            active_dests = self.destinations or ACTIVE_DESTINATIONS

            # Fallback: if 'needs_update' is empty (forced run), target all active
            if not targets and active_dests:
                targets = active_dests

            if targets:
                all_success = True
                for dest in targets:
                    dest_name = type(dest).__name__
                    log(f"Publishing to {dest_name}...")

                    # Apple Notes only supports 1 level of sub-folder under rootFolder.
                    # Obsidian supports full nested hierarchy.
                    if isinstance(dest, AppleNotesDestination):
                        target_subfolder = top_level_subfolder
                    else:
                        target_subfolder = full_subfolder

                    # A DestinationError is an expected, user-actionable failure
                    # (vault gone, Notes not responding): report it plainly and
                    # carry on to the next destination. Anything else is a bug,
                    # and is logged with a traceback so it is distinguishable.
                    try:
                        dest_success = dest.publish(
                            notebook_name=display_title,
                            text_content=clean_text,
                            image_paths=imgs,
                            sub_folder=target_subfolder,
                            document_path=doc_file_path
                            if (doc_file_path and doc_file_path.exists())
                            else None,
                            tags=notebook_tags,
                        )
                    except DestinationError as e:
                        log(f"⚠️ {dest_name}: {e}")
                        dest_success = False
                    except Exception:
                        import traceback

                        log(f"❌ Unexpected error publishing to {dest_name} — this is a bug:")
                        log(traceback.format_exc())
                        dest_success = False

                    if dest_success:
                        # Update state for THIS destination immediately
                        add_to_processed_log(dest_name, notebook_id, notebook_version)
                    else:
                        all_success = False
                        log(f"⚠️ Failed to publish to {dest_name}")

                success = all_success
            else:
                log("No destinations need update for this notebook (or none configured).")
                success = True

        except Exception as e:
            log(f"Failed publishing note: {e}")
            import traceback

            log(traceback.format_exc())

        if success:
            log(f"Notebook {notebook} processing complete.")
            clean_notebook_temp_artifacts(safe_notebook, keep_temp=effective_keep_temp)
        else:
            log(f"Notebook {notebook} processing FAILED.")

        return success

    def run(self) -> bool:
        """Execute the sync pipeline.

        Options were resolved in ``__init__``; to sync with different options,
        construct a new pipeline (``SyncPipeline(opts.merged_with(limit=1))``)
        rather than mutating this one.

        Returns:
            True if sync succeeded or completed gracefully, False on error.
        """
        # At the start of run(), clear the log for a new run
        with open(LOG_PATH, "w", encoding="utf-8") as f:
            f.write("")

        validate_environment()
        log("Pipeline started.")

        # Clean temporary working artifacts at start of run and register exit cleanup
        import atexit

        cleanup_temp_artifacts(keep_temp=self.keep_temp)
        atexit.register(cleanup_temp_artifacts, keep_temp=self.keep_temp)

        client = self.connect()
        notebooks, id_map = self.discover_documents(client)
        to_process, needs_update, should_continue = self.filter_pending_documents(notebooks, id_map)

        if not should_continue:
            return False
        if not to_process:
            return True

        all_success = True
        for nb_item in to_process:
            item_success = self.process_notebook_item(
                nb_item=nb_item,
                client=client,
                id_map=id_map,
                needs_update=needs_update,
                keep_temp=self.keep_temp,
            )
            if not item_success:
                all_success = False

        log("Pipeline finished.")
        cleanup_temp_artifacts(keep_temp=self.keep_temp)
        return all_success
