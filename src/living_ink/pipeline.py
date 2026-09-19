#!/usr/bin/env python3
"""Process notebooks: preprocess PNGs, run OCR, aggregate text, publish notes."""

import datetime
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from PIL import Image, ImageFilter, ImageOps

from living_ink import logs, state
from living_ink.cache import CACHE_DIRNAME, RENDER_CACHE_DIRNAME, RenderCache, TranscriptCache
from living_ink.clean import configure as configure_ai_provider
from living_ink.clean import (
    ocr_and_repair,
    repair_text_with_openai,
    transcription_fingerprint,
    vision_ocr_available,
)
from living_ink.config import (
    ConfigurationMissing,
    find_repo_root,
    get_config_path,
    get_data_dir,
    get_logs_dir,
)
from living_ink.destinations import (
    AppleNotesDestination,
    Destination,
    DestinationError,
    build_destinations,
)
from living_ink.devices import default_reading
from living_ink.redact import redact, register_secret
from living_ink.report import (
    FAILED,
    PUBLISHED,
    SKIPPED,
    WOULD_PUBLISH,
    DocumentOutcome,
    RunReport,
)
from living_ink.safeio import restrict_permissions
from living_ink.settings import Settings


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
        except (TypeError, ValueError, KeyError):
            # getMessage() interpolates the record's args; a library that logs
            # a mismatched format string must not take the filter down with it.
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

WHITE_DIR = DATA_DIR / "remarkable_pngs_white"
VISION_DIR = DATA_DIR / "remarkable_pngs_for_vision"
OCR_DIR = DATA_DIR / "output"  # OCR text files
PDF_DIR = DATA_DIR / "remarkable_pdfs"
DOCS_DIR = DATA_DIR / "remarkable_documents"
# Deliberately not one of the temp dirs: the whole value of a cached
# transcription is that it outlives the purge which removes the page it came
# from. See living_ink.cache.
TRANSCRIPT_CACHE_DIR = DATA_DIR / CACHE_DIRNAME
RENDER_CACHE_DIR = DATA_DIR / RENDER_CACHE_DIRNAME
LOGS_DIR = get_logs_dir()
LOG_PATH = LOGS_DIR / "pipeline.log"

#: Everything log() emits goes through here, alongside the rest of the package.
_logger = logging.getLogger(__name__)


_runtime_dirs_ready = False
_state_store = None


def ensure_runtime_dirs() -> None:
    """Create the runtime artifact directories if they do not exist yet.

    Called on demand rather than at import time so that merely importing this
    module has no filesystem side effects. Repeat calls are near-free.
    """
    global _runtime_dirs_ready
    if _runtime_dirs_ready:
        return
    for folder in (DATA_DIR, LOGS_DIR, WHITE_DIR, VISION_DIR, OCR_DIR, PDF_DIR, DOCS_DIR):
        folder.mkdir(parents=True, exist_ok=True)
    _runtime_dirs_ready = True


# --- CONFIGURATION LOADING (YAML) ---
def load_yaml_config(config_path: Optional[Path] = None) -> Dict[str, Any]:
    """Load configuration from YAML and export the credentials third parties read.

    Only settings that another library picks up from the environment on its own
    are exported (``OPENAI_API_KEY``, ``GOOGLE_APPLICATION_CREDENTIALS``).
    Living Ink's own settings are not: they are resolved from this dictionary by
    :class:`living_ink.settings.Settings` and passed explicitly.

    Args:
        config_path: Path to YAML config file. Defaults to get_config_path().

    Returns:
        Dictionary containing the parsed YAML configuration.
    """
    cfg_path = config_path or get_config_path()
    yaml_config: Dict[str, Any] = {}

    if cfg_path.exists():
        # Configs written before Living Ink set permissions land at the default
        # umask, leaving the API key and device token readable by every account
        # on the machine. Repair on the way past rather than only warning.
        if restrict_permissions(cfg_path):
            print(f"⚠️  Tightened permissions on {cfg_path} — it was readable by other users.")
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

                # Register credentials for masking as soon as they are read,
                # not when a provider is eventually built: a run that fails
                # during setup still writes a log the user may share.
                for section in ("ai", "openai"):
                    if isinstance(yaml_config.get(section), dict):
                        register_secret(str(yaml_config[section].get("api_key", "")).strip())
                rm_section = yaml_config.get("remarkable")
                if isinstance(rm_section, dict):
                    register_secret(str(rm_section.get("device_token", "")).strip())

                # 1. OpenAI
                if "openai" in yaml_config and "api_key" in yaml_config["openai"]:
                    os.environ.setdefault(
                        "OPENAI_API_KEY", str(yaml_config["openai"]["api_key"]).strip()
                    )

                # 2. Google Vision (Handle JSON content directly or file path)
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
                        except OSError as weave_err:
                            print(f"❌ Error writing google_creds.json: {weave_err}")

                # 3. AI Provider — initialize from new 'ai' section or legacy 'openai' section
                configure_ai_provider(yaml_config)

        except Exception as e:
            # Deliberately broad. Everything downstream of the parse — env
            # export, credential weaving, provider configuration — is driven by
            # whatever shape the user's YAML happens to have, and a config that
            # cannot be understood has to degrade to one printed line and an
            # empty dict rather than abort the run before it reports anything.
            print(f"Critical error loading config.yml: {e}")
            logging.debug("Loading %s failed", cfg_path, exc_info=True)

    # Legacy Fallback
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    return yaml_config


_default_config: Optional[Dict[str, Any]] = None


def get_default_config() -> Dict[str, Any]:
    """Return the config loaded from the standard config path, loading it once.

    Deferred rather than evaluated at import time because
    :func:`load_yaml_config` writes to ``os.environ`` and configures the AI
    provider; importing this module should do neither.
    """
    global _default_config
    if _default_config is None:
        _default_config = load_yaml_config(get_config_path())
    return _default_config


# --- DESTINATION SETUP ---


def get_destinations_from_config(
    config_dict, settings: Optional[Settings] = None
) -> List[Destination]:
    """Build the destinations this configuration enables.

    Thin wrapper over :func:`living_ink.destinations.build_destinations`, which
    walks the destination registry. Adding a destination means registering it
    there, not editing this module.

    Args:
        config_dict: Parsed ``config.yml`` contents.
        settings: Resolved settings. Defaults to resolving them from
            ``config_dict`` and the environment.

    Returns:
        The destinations enabled by this configuration.
    """
    return build_destinations(config_dict, settings or Settings.resolve(config_dict))


_default_destinations: Optional[List[Destination]] = None


def get_default_destinations() -> List[Destination]:
    """Return the destinations enabled by the standard config, building them once.

    Deferred rather than evaluated at import time so that importing this module
    neither reads config nor prints to stdout.
    """
    global _default_destinations
    if _default_destinations is None:
        _default_destinations = get_destinations_from_config(get_default_config())
    return _default_destinations


def get_state_db_path() -> Path:
    """Get the path to the sync state database."""
    ensure_runtime_dirs()
    return DATA_DIR / state.DB_FILENAME


def get_state_store() -> "state.StateStore":
    """Return the shared state store, opening it on first use.

    Cached rather than built at import time, so importing this module still
    touches no disk. The one-time import of the old per-destination JSON files
    happens here, on the first open after an upgrade.

    Returns:
        The process-wide open StateStore.
    """
    global _state_store
    if _state_store is None:
        # Legacy state lived beside the checkout before it moved under the
        # data directory; sweep both so an upgrade from either layout keeps
        # its history instead of re-OCRing every notebook.
        db_path = get_state_db_path()
        store = state.StateStore(db_path)
        for source in (DATA_DIR, ROOT):
            imported = state.import_legacy_json(store, source)
            if imported:
                log(f"📦 Imported {imported} sync records from {source} into {db_path.name}.")
        _state_store = store
    return _state_store


def reset_state_store() -> None:
    """Close and drop the cached state store.

    Exists for tests and for the setup wizard, both of which can move the data
    directory out from under an already-open connection.
    """
    global _state_store
    if _state_store is not None:
        _state_store.close()
        _state_store = None


def load_processed_log(dest_name: str):
    """Return the published version of every document for one destination.

    Args:
        dest_name: Destination class name, e.g. ``ObsidianDestination``.

    Returns:
        Mapping of document id to the version last published there.
    """
    return get_state_store().published_versions(dest_name)


def add_to_processed_log(
    dest_name: str, doc_id, version, run_id=None, external_id=None, target=None
):
    """Record that a document reached a destination.

    Args:
        dest_name: Destination class name.
        doc_id: reMarkable document id.
        version: Device version or content hash that was published.
        run_id: Run that published it, when one is in progress.
        external_id: Identifier the destination gave the note, so the next
            sync replaces that exact note rather than one sharing its title.
        target: Where the note landed, so a later run can tell it has moved.
    """
    get_state_store().record_publication(
        doc_id, dest_name, version, run_id=run_id, external_id=external_id, target=target
    )


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
        except (OSError, UnicodeDecodeError):
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
        # Deliberately broad. Vision raises out of google.api_core, whose
        # exception hierarchy only exists when the optional dependency is
        # installed, so it cannot be named here. OCR through this path is a
        # fallback anyway: any failure means "no text from Vision", not a
        # failed run.
        print(f"Google Cloud Vision error: {e}")
        logging.debug("Google Cloud Vision OCR failed for %s", png_path, exc_info=True)
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
    # Redacted at the single choke point rather than at each of the ~90 call
    # sites: pipeline.log is the file a user attaches to a bug report.
    msg = redact(str(msg))
    # Console and file are separate decisions now: --quiet silences the first,
    # and the second is a rotating handler shared with every other module's
    # logger calls, so the file holds more than just these ~90 messages.
    logs.console(msg)
    logs.ensure_configured(LOG_PATH)
    _logger.info(msg)


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
                except OSError as e:
                    logging.debug("Failed to remove temporary item %s: %s", item, e)
            folder.mkdir(parents=True, exist_ok=True)

    # Clean any lingering zip archives in DATA_DIR
    if DATA_DIR.exists():
        for zip_file in DATA_DIR.glob("*.zip"):
            try:
                zip_file.unlink(missing_ok=True)
            except OSError:
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
                except OSError:
                    pass

    if VISION_DIR.exists():
        for p in VISION_DIR.glob(f"{safe_notebook}*"):
            try:
                if p.is_file() or p.is_symlink():
                    p.unlink()
                elif p.is_dir():
                    shutil.rmtree(p)
            except OSError:
                pass


def validate_environment():
    """Check configuration health, logging warnings and failing on hard errors.

    Raises:
        ConfigurationMissing: If configuration is absent or invalid. Offering
            the setup wizard is the CLI's decision, not this function's.
    """
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

    # Print warnings (non-fatal). log() already reaches the console; the bare
    # print that used to follow it printed every warning twice and bypassed
    # --quiet and --json alike.
    for w in warnings:
        log(w)

    if errors:
        msg = "\n".join(errors)
        log("\n" + "=" * 60)
        log("CONFIGURATION ERROR")
        log("=" * 60)
        log(msg)
        log("-" * 60)
        log(docs_hint)
        log("=" * 60 + "\n")

        raise ConfigurationMissing(msg, hint=docs_hint)

    log("Configuration valid.")


def get_val(item: Any, key: str) -> Any:
    """Safely get a property or dictionary key from a document item."""
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, getattr(item, key.lower(), None))


def document_version(item: Any) -> str:
    """Return the value that decides whether a document has changed.

    The content hash when the transport offers one, the version counter
    otherwise. Lives here rather than inline in
    :meth:`SyncPipeline.filter_pending_documents` because ``sync --status``
    predicts that decision, and a preview that disagrees with the run it
    predicts is worse than no preview.

    Args:
        item: A document from the transport's listing.

    Returns:
        The content hash, or the version number as a string, or ``"1"``.
    """
    value = get_val(item, "hash")
    if value:
        return str(value)
    try:
        return str(int(get_val(item, "Version")))
    except (ValueError, TypeError):
        return "1"


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
    if client is not None:
        try:
            ft = client.get_file_type(item)
            if ft in ("pdf", "epub"):
                return ft
        except Exception:
            # Deliberately broad. This is a probe against whichever transport
            # happens to be connected, and the filename fallback below answers
            # the question just as well. A type lookup must never be the reason
            # a document drops out of discovery.
            logging.debug("get_file_type probe failed", exc_info=True)

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


def to_datetime(value: Any) -> Optional[datetime.datetime]:
    """Coerce whatever a transport calls a timestamp into a datetime.

    The two transports disagree: the cloud client hands back a datetime, SSH
    hands back the device's epoch value, and the device counts in milliseconds
    where everything else counts in seconds. A string may be either an ISO
    timestamp or a number that happens to be spelled out.

    Args:
        value: A datetime, an epoch number, an ISO string, or None.

    Returns:
        The moment it names, or None when it names nothing intelligible.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime.datetime):
        return value

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if not text.replace(".", "", 1).isdigit():
            try:
                return datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
        value = text

    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None

    # The device reports milliseconds. Anything past this is not a plausible
    # second-count for a tablet that did not exist before 2017.
    if seconds > 1e11:
        seconds /= 1000
    try:
        return datetime.datetime.fromtimestamp(seconds)
    except (ValueError, OverflowError, OSError):
        # A timestamp outside the range this platform can represent.
        return None


def to_iso_date(value: Any) -> Optional[str]:
    """Render a transport timestamp as a plain ``YYYY-MM-DD`` date.

    Args:
        value: Anything :func:`to_datetime` accepts.

    Returns:
        The date, or None when the value names no moment.
    """
    moment = to_datetime(value)
    return None if moment is None else moment.date().isoformat()


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

    modified = to_datetime(get_val(item, "ModifiedClient") or getattr(item, "last_modified", None))
    mod_str = "" if modified is None else f" (modified: {modified.strftime('%Y-%m-%d %H:%M')})"

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
        dry_run: Do everything except publish, so a run can be inspected first.
        prune: Delete notes whose notebook is gone from the tablet, instead of
            only reporting them.
        json_output: Print the run summary as JSON instead of a table.
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
    dry_run: bool = False
    prune: bool = False
    json_output: bool = False

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
            dry_run=getattr(args, "dry_run", False),
            prune=getattr(args, "prune", False),
            json_output=getattr(args, "json", False),
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


class _StopProcessing(Exception):
    """Raised by a stage when there is nothing left to do for a document.

    Attributes:
        success: The verdict to report for the document. An empty notebook is
            a success with nothing to publish; a failed download is not.
        reason: Message to log, if any.
    """

    def __init__(self, success: bool, reason: str = "") -> None:
        super().__init__(reason)
        self.success = success
        self.reason = reason


def _item_version(item: Any) -> Any:
    """Return the value that identifies this revision of a document.

    Sync v3/v4 items carry a content hash; older items only carry an integer
    version. Either is stored in the processed log and compared on the next run.

    Args:
        item: reMarkable item metadata.

    Returns:
        The item's hash, else its integer version, else 1.
    """
    item_hash = get_val(item, "hash")
    if item_hash:
        return item_hash
    try:
        return int(get_val(item, "Version"))
    except (ValueError, TypeError):
        return 1


def _strip_transcript_metadata(path: Optional[Path]) -> str:
    """Read a transcript and drop the leading metadata line.

    Args:
        path: The transcript file, or None.

    Returns:
        The note body: everything from the first page header or divider on.
    """
    if not path or not path.exists():
        return ""

    lines = path.read_text(errors="ignore").split("\n")
    start = 0
    for i, line in enumerate(lines):
        if line.startswith("---") or line.startswith("###"):
            start = i
            break
    return "\n".join(lines[start:]).strip()


@dataclass
class DocumentJob:
    """One document's state as it moves through the processing stages.

    The stages of :meth:`SyncPipeline.process_notebook_item` communicate
    through this object rather than through a few hundred lines of locals.
    Identity fields are set once by ``_describe_job``; the rest accumulate.
    """

    item: Any
    notebook: str
    notebook_id: Any
    doc_type: str
    version: Any
    safe_name: str
    folder_path: str
    display_title: str
    keep_temp: bool
    doc_file_path: Optional[Path] = None

    tags: List[str] = field(default_factory=list)
    imgs: List[Path] = field(default_factory=list)
    page_hashes: List[str] = field(default_factory=list)
    source_hashes: List[str] = field(default_factory=list)
    transcribed_pages: int = 0
    cached_pages: int = 0
    failed_pages: int = 0
    reused_transcript: bool = False
    published_to: List[str] = field(default_factory=list)
    would_publish_to: List[str] = field(default_factory=list)
    pre_paths: List[Path] = field(default_factory=list)
    extracted_doc_text: str = ""
    raw_texts: List[str] = field(default_factory=list)
    cleaned_texts: List[str] = field(default_factory=list)
    clean_out_txt: Optional[Path] = None

    def modified_date(self) -> Optional[str]:
        """Return the date the tablet says this notebook was last written on.

        Returns:
            ``YYYY-MM-DD``, or None when the transport reported no usable
            timestamp for it.
        """
        return to_iso_date(
            get_val(self.item, "ModifiedClient") or getattr(self.item, "last_modified", None)
        )

    def page_number(self, index: int) -> int:
        """Return the document page number for the given transcript index.

        Annotated PDFs only render the pages that were written on, so the
        page number comes from the image name rather than the loop counter.

        Args:
            index: Position in the transcript list.

        Returns:
            The page number to show in the section header.
        """
        if index < len(self.imgs):
            match = re.search(r"page-(\d+)", self.imgs[index].name, re.IGNORECASE)
            if match:
                return int(match.group(1))
        return index + 1

    def source_file(self) -> Optional[Path]:
        """Return the original PDF/EPUB to attach, if one was retrieved."""
        if self.doc_file_path and self.doc_file_path.exists():
            return self.doc_file_path
        return None

    def _folder_parts(self) -> List[str]:
        """Split the reMarkable folder path into its individual folder names."""
        return [p.strip() for p in self.folder_path.split(" / ") if p.strip()]

    def full_subfolder(self) -> Optional[str]:
        """Return the whole folder hierarchy, for destinations that nest."""
        parts = self._folder_parts()
        return "/".join(parts) if parts else None

    def top_level_subfolder(self) -> Optional[str]:
        """Return only the outermost folder, for destinations that do not nest."""
        parts = self._folder_parts()
        return sanitize_filename(parts[0]) if parts else None


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
        ensure_runtime_dirs()
        self.options = options or SyncOptions()
        opts = self.options

        # Filled in by _learn_device once the transport is up. Until then the
        # named default stands in, so nothing downstream has to handle None.
        self.device = default_reading()

        self.config_path = config_path or get_config_path()
        self.data_dir = data_dir or DATA_DIR
        self.dry_run = opts.dry_run
        self.prune = opts.prune
        self.json_output = opts.json_output
        # A dry run's whole output is the transcripts it leaves behind, so it
        # implies --keep-temp; purging them would delete what it points at.
        self.keep_temp = opts.keep_temp or opts.dry_run

        if self.config_path and self.config_path != get_config_path():
            self.raw_config = load_yaml_config(self.config_path)
            self.destinations = (
                destinations
                if destinations is not None
                else get_destinations_from_config(self.raw_config)
            )
        else:
            self.raw_config = get_default_config()
            self.destinations = (
                destinations if destinations is not None else list(get_default_destinations())
            )

        # Config and environment are merged once, here; the CLI options layered
        # on top are the only thing that outranks them.
        base = Settings.resolve(self.raw_config)

        # 1. Connection properties
        if opts.ssh:
            preferred, use_ssh = "ssh", True
        elif opts.cloud:
            preferred, use_ssh = "cloud", False
        elif opts.preferred_connection:
            preferred = opts.preferred_connection.strip().lower()
            use_ssh = preferred == "ssh"
        else:
            preferred, use_ssh = base.preferred_connection, base.use_ssh

        # 2. Document types and limits
        self.target_notebook = opts.notebook.strip() if opts.notebook else None
        self.all_types = opts.all_types

        if opts.all_types:
            sync_pdfs = sync_epubs = True
        else:
            sync_pdfs = base.sync_pdfs if opts.sync_pdfs is None else opts.sync_pdfs
            sync_epubs = base.sync_epubs if opts.sync_epubs is None else opts.sync_epubs

        limit = (
            opts.limit if opts.limit is not None and opts.limit > 0 else base.max_notebooks_per_run
        )

        self.settings = replace(
            base,
            preferred_connection=preferred,
            use_ssh=use_ssh,
            sync_pdfs=sync_pdfs,
            sync_epubs=sync_epubs,
            max_notebooks_per_run=limit,
            apple_notes_folder=opts.folder or base.apple_notes_folder,
        )

        # 3. Destination folder
        for dest in self.destinations:
            if isinstance(dest, AppleNotesDestination):
                dest.folder_name = self.settings.apple_notes_folder

        # Opened by run(); every state row written during that run carries it,
        # so "what did the 03:00 sync touch" has an answer.
        self.run_id: Optional[int] = None
        self.report: Optional[RunReport] = None

        # 4. Transcription cache. Pages are transcribed concurrently, so the
        # hit and miss tallies need a lock even though the entries themselves
        # are independent files.
        self.cache = TranscriptCache(
            TRANSCRIPT_CACHE_DIR,
            enabled=self.settings.transcript_cache,
            max_age_days=self.settings.cache_max_age_days,
        )
        self._cache_lock = threading.Lock()
        self._cache_hits = 0
        self._cache_misses = 0

        # 5. Render cache. Rendering costs CPU rather than money, but it is
        # the slowest local step and just as pure, so it caches the same way.
        self.renders = RenderCache(
            RENDER_CACHE_DIR,
            enabled=self.settings.render_cache,
            max_age_days=self.settings.cache_max_age_days,
        )

    # The resolved settings are the single source of truth; these read-only
    # views keep the pipeline's long-standing attribute names working.

    @property
    def preferred_connection(self) -> str:
        """Which transport to try first, ``"ssh"`` or ``"cloud"``."""
        return self.settings.preferred_connection

    @property
    def use_ssh(self) -> bool:
        """Whether SSH is usable for this run."""
        return self.settings.use_ssh

    @property
    def sync_pdfs(self) -> bool:
        """Whether annotated PDFs are included in this run."""
        return self.settings.sync_pdfs

    @property
    def sync_epubs(self) -> bool:
        """Whether annotated EPUBs are included in this run."""
        return self.settings.sync_epubs

    @property
    def limit(self) -> int:
        """Maximum number of documents to process in this run."""
        return self.settings.max_notebooks_per_run

    @property
    def folder(self) -> str:
        """Destination folder name in Apple Notes."""
        return self.settings.apple_notes_folder

    def connect(self) -> Any:
        """Establish connection to reMarkable tablet (via SSH or Cloud)."""
        from living_ink.api import get_rmapi

        return get_rmapi(self.settings)

    def _learn_device(self, client: Any) -> None:
        """Identify the tablet once per run, and remember a USB reading.

        Runs for its side effect: a run that reaches the hardware banks what it
        saw, so every later Cloud-only run knows the model and geometry without
        the cable. Never fatal — a sync that cannot name the tablet still syncs.

        Args:
            client: The connected transport.
        """
        from living_ink.devices import SOURCE_USB, resolve_device

        try:
            reading = resolve_device(client, get_state_store())
        except (RuntimeError, OSError) as e:
            _logger.debug("Could not identify the device: %s", e, exc_info=True)
            return

        self.device = reading
        if reading.source == SOURCE_USB:
            log(f"📱 {reading.describe()}")
        else:
            _logger.info("Device: %s", reading.describe())

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

    def _record_seen_document(
        self, item: Any, doc_id: str, version: Any, id_map: Dict[str, Any]
    ) -> None:
        """Note in the state database that a document exists on the device.

        Best-effort: a sync that cannot write its inventory should still sync.
        The publication record, which is what prevents re-paying for OCR, is
        written on a path that does report failure.

        Args:
            item: Raw document item from the transport.
            doc_id: reMarkable document id.
            version: Device version or content hash.
            id_map: Map of id to document, for resolving the folder path.
        """
        try:
            name = str(get_val(item, "VissibleName") or get_val(item, "VisibleName") or "").strip()
            mod = get_val(item, "ModifiedClient") or getattr(item, "last_modified", None)
            get_state_store().record_document(
                doc_id,
                name=name or None,
                folder=get_notebook_path(item, id_map) or None,
                doc_type=get_document_type(item),
                version=version,
                last_modified=None if mod is None else str(mod),
                run_id=self.run_id,
            )
        except (sqlite3.Error, OSError, RuntimeError) as e:
            # Everything the store can raise: a statement or lock failure, the
            # database file being unreachable, and a schema written by a newer
            # build. None of them is a reason to skip the document.
            log(f"⚠️ Could not record {doc_id} in the state database: {e}")

    def filter_pending_documents(
        self, notebooks: List[Any], id_map: Dict[str, Any]
    ) -> Tuple[List[Any], Dict[str, List[Destination]], bool]:
        """Determine which notebooks need updating for active destinations.

        The decision is not made here. The listing is handed to
        :meth:`~living_ink.state.StateStore.compare_with_listing`, which is the
        same call ``living-ink sync --status`` makes, so the preview and the run
        it predicts share one classifier rather than two expressions of the same
        rule that are free to drift apart.

        Returns:
            Tuple of (notebooks_to_process, needs_update_map, should_continue_bool).
        """
        active_dests = self.destinations or get_default_destinations()
        by_name = {type(dest).__name__: dest for dest in active_dests}

        listing = []
        for item in notebooks:
            doc_id = get_val(item, "ID")
            curr_val = document_version(item)

            # Recorded whether or not it needs publishing: an inventory of what
            # is on the device is what makes "what is pending" answerable
            # without talking to the tablet again. Recorded *before* the
            # comparison, so the classifier judges this run's facts.
            self._record_seen_document(item, doc_id, curr_val, id_map)

            listing.append(
                {
                    "id": doc_id,
                    "name": get_val(item, "VissibleName") or get_val(item, "VisibleName"),
                    "folder": get_notebook_path(item, id_map) or None,
                    "doc_type": get_document_type(item),
                    "version": curr_val,
                }
            )

        rows, _ = get_state_store().compare_with_listing(listing, list(by_name))
        # ``pending`` as well as ``needs_sync``: a status can want attention
        # without owing any enabled destination a publish, and there is nothing
        # for this loop to do about one that does not.
        needs_update: Dict[str, List[Destination]] = {
            row["id"]: [by_name[name] for name in row["pending"]]
            for row in rows
            if row["pending"] and row["status"].needs_sync
        }

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

        The stages run in a fixed order and pass state through a
        :class:`DocumentJob`: acquire pages, collect tags, preprocess images,
        OCR, write transcripts, publish. A stage that finds nothing left to do
        raises :class:`_StopProcessing` carrying the verdict to report.

        Args:
            nb_item: reMarkable item metadata.
            client: reMarkable API client.
            id_map: Mapping from document ID to metadata item.
            needs_update: Mapping from document ID to target destinations.
            keep_temp: Whether to keep temporary files on disk.

        Returns:
            True if notebook was processed and published successfully, False otherwise.
        """
        job = self._describe_job(nb_item, client, id_map, keep_temp)

        try:
            self._acquire_pages(job, client)
            self._collect_tags(job, client)
            if not self._reuse_transcript(job):
                self._preprocess_images(job)
                self._ocr_pages(job)
                self._write_transcripts(job)
            success = self._publish(job, needs_update)
        except _StopProcessing as stop:
            if stop.reason:
                log(stop.reason)
            self._record_outcome(job, stop.success, stop.reason)
            self._report_job(job, stop.success, stop.reason)
            return stop.success

        if success:
            log(f"Notebook {job.notebook} processing complete.")
            clean_notebook_temp_artifacts(job.safe_name, keep_temp=job.keep_temp)
        else:
            log(f"Notebook {job.notebook} processing FAILED.")

        self._record_outcome(job, success, None if success else "Processing failed.")
        self._report_job(job, success, None if success else "processing failed")
        return success

    def _report_job(self, job: DocumentJob, success: bool, reason: Optional[str]) -> None:
        """Add one finished document to the run summary.

        Args:
            job: The document that just finished, however it finished.
            success: Whether it reached its destinations.
            reason: Why it did not, when it did not.
        """
        if self.report is None:
            return
        if not success:
            status = FAILED
        elif job.published_to:
            status = PUBLISHED
        elif job.would_publish_to:
            # A dry run did all the work and deliberately sent nothing. That is
            # a rehearsal, not a document that needed no work.
            status = WOULD_PUBLISH
        else:
            status = SKIPPED
        self.report.add(
            DocumentOutcome(
                name=job.display_title or job.notebook,
                doc_id=job.notebook_id,
                status=status,
                pages=len(job.imgs),
                transcribed=job.transcribed_pages,
                cached=job.cached_pages,
                pages_failed=job.failed_pages,
                reused_transcript=job.reused_transcript,
                destinations=list(job.published_to or job.would_publish_to),
                reason=(
                    None
                    if status in (PUBLISHED, WOULD_PUBLISH)
                    else (reason or "nothing to publish")
                ),
            )
        )

    def _record_outcome(self, job: DocumentJob, success: bool, reason: Optional[str]) -> None:
        """Remember whether this document worked, so ``list`` can report it.

        A success clears any earlier failure: the document is no longer broken
        and saying otherwise would send the user chasing a problem that fixed
        itself.

        Best-effort and never fatal — a sync that cannot write a note about a
        failure has still done the sync.

        Args:
            job: The document just attempted.
            success: Whether it finished cleanly.
            reason: What went wrong, when it did not.
        """
        if self.dry_run:
            return
        try:
            store = get_state_store()
            if success:
                store.clear_failure(job.notebook_id)
            else:
                store.record_failure(job.notebook_id, redact(reason or "Processing failed."))
        except (sqlite3.Error, OSError, RuntimeError) as e:
            log(f"⚠️ Could not record the outcome for {job.notebook_id}: {e}")

    # ── Stage 1: identify ────────────────────────────────────────────────

    def _describe_job(
        self,
        nb_item: Any,
        client: Any,
        id_map: Dict[str, Any],
        keep_temp: Optional[bool],
    ) -> DocumentJob:
        """Resolve a library item into the job the later stages operate on.

        Args:
            nb_item: reMarkable item metadata.
            client: reMarkable API client, used to determine the document type.
            id_map: Mapping from document ID to metadata item, for folder paths.
            keep_temp: Per-call override for keeping temp artifacts.

        Returns:
            A DocumentJob with identity, paths and titles filled in.
        """
        notebook = get_val(nb_item, "VissibleName") or get_val(nb_item, "VisibleName")
        doc_type = get_document_type(nb_item, client)
        folder_path = get_notebook_path(nb_item, id_map)
        safe_name = sanitize_filename(notebook)

        job = DocumentJob(
            item=nb_item,
            notebook=notebook,
            notebook_id=get_val(nb_item, "ID"),
            doc_type=doc_type,
            version=_item_version(nb_item),
            safe_name=safe_name,
            folder_path=folder_path,
            display_title=f"{folder_path} / {notebook}" if folder_path else notebook,
            keep_temp=self.keep_temp if keep_temp is None else keep_temp,
            doc_file_path=(
                DOCS_DIR / f"{safe_name}.{doc_type}" if doc_type in ("pdf", "epub") else None
            ),
        )

        type_badge = f" ({doc_type.upper()})" if doc_type != "notebook" else ""
        log(f"Processing {doc_type}: {job.display_title}{type_badge} (ID: {job.notebook_id})")
        return job

    # ── Stage 2: acquire pages ───────────────────────────────────────────

    def _acquire_pages(self, job: DocumentJob, client: Any) -> None:
        """Ensure rendered page images exist for this job, downloading if needed.

        Pages left on disk by an earlier run are reused as-is. Otherwise the
        document zip is downloaded once and handed to the renderer registered
        for its type.

        Args:
            job: The job to fill in; sets ``imgs`` and ``extracted_doc_text``.
            client: reMarkable API client.

        Raises:
            _StopProcessing: If the document cannot be downloaded, or yields
                neither pages nor text.
        """
        job.imgs = self._rendered_pages(job)

        if not job.imgs:
            log(
                f"No white-background PNGs found for {job.notebook}. "
                "Attempting to pull from reMarkable..."
            )
            if not job.item:
                raise _StopProcessing(
                    False,
                    f'Document "{job.notebook}" not found in your reMarkable library. Skipping.',
                )

            self._render_document(job, client)

            job.imgs = self._rendered_pages(job)
            if not job.imgs and not job.extracted_doc_text:
                raise _StopProcessing(
                    False, f"No pages or text could be extracted for '{job.notebook}'. Skipping."
                )

        log(
            f"Found {len(job.imgs)} white-background PNGs for {job.notebook}: "
            f"{[p.name for p in job.imgs]}"
        )

    def _rendered_pages(self, job: DocumentJob) -> List[Path]:
        """List the page images already rendered for this job, in page order."""
        prefix = job.safe_name + "."
        return sorted(
            p
            for p in WHITE_DIR.iterdir()
            if p.name.startswith(prefix) and p.suffix.lower() == ".png"
        )

    def _render_document(self, job: DocumentJob, client: Any) -> None:
        """Download the document zip and render it with the right renderer.

        Args:
            job: The job being rendered.
            client: reMarkable API client.

        Raises:
            _StopProcessing: If the zip cannot be downloaded.
        """
        from living_ink.extract import extract_tags_from_zip

        tmp_zip = DATA_DIR / f"{job.safe_name}.zip"
        raw_bytes = client.download(job.item)
        if not raw_bytes:
            raise _StopProcessing(False, f"Failed to download document zip for {job.notebook}.")
        tmp_zip.write_bytes(raw_bytes)

        try:
            job.tags.extend(extract_tags_from_zip(tmp_zip) or [])
            renderer = self._RENDERERS.get(job.doc_type, SyncPipeline._render_notebook)
            renderer(self, job, tmp_zip, client)
        finally:
            tmp_zip.unlink(missing_ok=True)

    def _ensure_source_file(self, job: DocumentJob, tmp_zip: Path, client: Any) -> bool:
        """Put the original PDF/EPUB on disk, from the zip or by direct download.

        Args:
            job: The job whose ``doc_file_path`` should end up on disk.
            tmp_zip: The downloaded document zip.
            client: reMarkable API client, for the direct-download fallback.

        Returns:
            True if the source file is now on disk.
        """
        from living_ink.api import download_raw_file
        from living_ink.extract import extract_raw_document_from_zip

        extract_raw_document_from_zip(tmp_zip, job.doc_file_path)
        if not job.doc_file_path.exists():
            raw = download_raw_file(client, job.item, job.doc_type)
            if raw:
                job.doc_file_path.write_bytes(raw)
        return job.doc_file_path.exists()

    def _render_pdf(self, job: DocumentJob, tmp_zip: Path, client: Any) -> None:
        """Render an annotated PDF's marked-up pages, or fall back to its text."""
        from living_ink.extract import (
            extract_text_from_pdf,
            get_pdf_annotated_page_map,
            render_composite_pdf_page,
            render_pdf_page_preview,
        )

        if not self._ensure_source_file(job, tmp_zip, client):
            return

        annotated_pages = get_pdf_annotated_page_map(tmp_zip)
        if annotated_pages:
            log(f"Rendering {len(annotated_pages)} annotated pages for PDF '{job.notebook}'...")
            with zipfile.ZipFile(tmp_zip, "r") as zf:
                names = set(zf.namelist())
                for p_info in annotated_pages:
                    rm_name = p_info["rm_file_name"]
                    rm_data = zf.read(rm_name) if rm_name in names else b""
                    comp_bytes = render_composite_pdf_page(
                        job.doc_file_path, p_info["pdf_page_index"], rm_data
                    )
                    if comp_bytes:
                        self._save_page(job, p_info["page_num"], comp_bytes, "Saved annotated page")
            return

        log(
            f"PDF '{job.notebook}' has no handwritten annotations. "
            "Extracting text & cover preview..."
        )
        cover_bytes = render_pdf_page_preview(job.doc_file_path, 0)
        if cover_bytes:
            self._save_page(job, 1, cover_bytes, "Saved cover preview")
        job.extracted_doc_text = extract_text_from_pdf(job.doc_file_path)

    def _render_epub(self, job: DocumentJob, tmp_zip: Path, client: Any) -> None:
        """Extract an EPUB's text, and render any annotation pages it carries."""
        from living_ink.extract import (
            extract_text_from_epub,
            get_document_page_count,
        )

        if self._ensure_source_file(job, tmp_zip, client):
            job.extracted_doc_text = extract_text_from_epub(job.doc_file_path)

        page_count = get_document_page_count(tmp_zip)
        if page_count > 0:
            log(f"Rendering {page_count} annotation pages for EPUB '{job.notebook}'...")
            self._render_zip_pages(job, tmp_zip, page_count)

    def _render_notebook(self, job: DocumentJob, tmp_zip: Path, client: Any) -> None:
        """Render every page of a handwritten notebook.

        Raises:
            _StopProcessing: If the notebook is empty. That is not a failure —
                there is simply nothing to publish.
        """
        from living_ink.extract import get_document_page_count

        page_count = get_document_page_count(tmp_zip)
        if page_count == 0:
            raise _StopProcessing(
                True, f"Notebook '{job.notebook}' has 0 pages (empty notebook). Skipping."
            )

        log(f"Rendering {page_count} pages for {job.notebook}...")
        self._render_zip_pages(job, tmp_zip, page_count)

    def _render_zip_pages(self, job: DocumentJob, tmp_zip: Path, page_count: int) -> None:
        """Render every page of a document zip, reusing what has not changed.

        Turning one ``.rm`` page into a PNG goes through rmc, an SVG, and
        PyMuPDF, and it is the slowest local step in a sync. It is also pure:
        the same strokes rendered by the same libraries give the same image.
        So a page whose source is byte-identical to one already rendered is
        served from the cache and never re-rendered.

        Args:
            job: The job being rendered.
            tmp_zip: The downloaded document zip.
            page_count: How many pages the zip holds.
        """
        from living_ink.extract import (
            RenderError,
            get_background_color,
            get_page_source_hashes,
            render_page_from_document_zip,
            renderer_fingerprint,
        )

        # Computed even with the cache off: hashing the .rm entries of a zip
        # already on disk is cheap, and the source hashes are what let a later
        # stage tell "this page changed" from "the renderer changed".
        source_hashes = get_page_source_hashes(tmp_zip)
        job.source_hashes = source_hashes
        # The background is chosen per run rather than baked into the build,
        # so it belongs in the key rather than in the renderer fingerprint.
        fingerprint = (
            f"{renderer_fingerprint()}:{get_background_color()}"
            if source_hashes and self.renders.enabled
            else ""
        )

        reused = 0
        for page in range(1, page_count + 1):
            key = self._render_key(source_hashes, page, fingerprint)
            png_bytes = self.renders.get(key) if key else None

            if png_bytes is None:
                try:
                    png_bytes = render_page_from_document_zip(tmp_zip, page)
                except RenderError as e:
                    # Named rather than counted: a page that renders to nothing
                    # used to publish as an empty note with no error anywhere.
                    job.failed_pages += 1
                    log(f"Failed to render page {page} of {job.notebook}: {e}")
                    if self.report:
                        self.report.warn(f"{job.notebook} page {page}: {e}")
                    continue
                if png_bytes is None:
                    job.failed_pages += 1
                    log(f"Failed to render page {page} of {job.notebook}.")
                    continue
                if key:
                    self.renders.put(key, png_bytes)
            else:
                reused += 1

            self._save_page(job, page, png_bytes)

        if reused:
            log(f"{reused} of {page_count} pages were already rendered; reused as-is.")

    def _render_key(self, source_hashes: List[str], page: int, fingerprint: str) -> Optional[str]:
        """Return the render cache key for one page, or None if it has none.

        Args:
            source_hashes: Per-page source digests, in page order.
            page: One-based page number.
            fingerprint: Identifies the renderer and the background colour.

        Returns:
            A cache key, or None when the page's source could not be hashed.
        """
        if page > len(source_hashes):
            return None
        digest = source_hashes[page - 1]
        if not digest:
            return None
        return self.renders.key(digest.encode("utf-8"), fingerprint)

    def _save_page(self, job: DocumentJob, page: int, data: bytes, label: str = "Saved") -> None:
        """Write one rendered page image into the white-background directory."""
        out_img = WHITE_DIR / f"{job.safe_name}.page-{page}.png"
        out_img.write_bytes(data)
        log(f"{label}: {out_img}")

    # Which renderer handles which document type. A type with no entry here is
    # rendered as a handwritten notebook.
    _RENDERERS = {"pdf": _render_pdf, "epub": _render_epub}

    # ── Stage 3: tags ────────────────────────────────────────────────────

    def _collect_tags(self, job: DocumentJob, client: Any) -> None:
        """Fill in the job's tags from the transport if the zip carried none."""
        if not job.tags:
            from living_ink.api import get_document_tags

            job.tags.extend(get_document_tags(client, job.item) or [])

        if job.tags:
            log(f"Tags found for '{job.notebook}': {job.tags}")

    # ── Stage 4: preprocess ──────────────────────────────────────────────

    def _preprocess_images(self, job: DocumentJob) -> None:
        """Prepare each page image for OCR, writing the results to VISION_DIR."""
        pre_dir = VISION_DIR / job.safe_name
        pre_dir.mkdir(parents=True, exist_ok=True)

        for p in job.imgs:
            out_p = pre_dir / p.name
            preprocess_image(p, out_p)
            job.pre_paths.append(out_p)

        self._record_page_hashes(job)

    def _record_page_hashes(self, job: DocumentJob) -> None:
        """Hash every rendered page and remember it against the document.

        The rendered PNG is what gets hashed, not the ``.rm`` source: the PNG
        is what OCR actually reads, so it is the thing whose change means the
        page has to be read again. It also moves when ``rmc`` is upgraded,
        which is correct — a differently rendered page is a different page.

        The ``.rm`` source hash is recorded alongside it. The pair is what
        makes :meth:`_check_renderer` possible: a page whose source did not
        move but whose render did means the renderer changed, not the note.

        Best-effort. A page hash that cannot be computed or stored costs a
        later cache miss, nothing more.
        """
        previous = self._previous_page_hashes(job)

        job.page_hashes = []
        for index, page in enumerate(job.imgs):
            try:
                digest = hashlib.sha256(page.read_bytes()).hexdigest()
            except OSError:
                logging.debug("Could not hash %s", page, exc_info=True)
                continue
            job.page_hashes.append(digest)
            if self.dry_run:
                continue
            try:
                get_state_store().record_page(
                    job.notebook_id,
                    index,
                    source_hash=job.source_hashes[index]
                    if index < len(job.source_hashes)
                    else None,
                    render_hash=digest,
                    run_id=self.run_id,
                )
            except (sqlite3.Error, OSError, RuntimeError) as e:
                log(f"⚠️ Could not record page {index + 1} of {job.notebook}: {e}")

        self._check_renderer(job, previous)

    def _previous_page_hashes(self, job: DocumentJob) -> Dict[int, Dict[str, Any]]:
        """Return what the last run recorded about this document's pages.

        Args:
            job: The document about to be hashed.

        Returns:
            Page index to that page's stored row, empty when there is nothing
            recorded or the store cannot be read.
        """
        try:
            return get_state_store().get_pages(job.notebook_id)
        except (sqlite3.Error, OSError, RuntimeError):
            logging.debug("Could not read stored page hashes", exc_info=True)
            return {}

    def _check_renderer(self, job: DocumentJob, previous: Dict[int, Dict[str, Any]]) -> None:
        """Warn when the pages look wrong rather than merely different.

        Two failures are worth catching before they reach a destination, and
        both are invisible in the per-page logs:

        A page whose ``.rm`` source is byte-identical to last time but whose
        rendered PNG is not means the *renderer* changed. Upgrading ``rmc`` or
        ``rmscene`` is the documented cause of blank or clipped pages, and it
        arrives silently as a transitive dependency bump.

        A notebook whose pages nearly all render to the *same* bytes is a
        notebook that rendered blank. Real handwriting does not repeat.

        Args:
            job: The document just hashed.
            previous: What the last run recorded, from
                :meth:`_previous_page_hashes`.
        """
        if not self.report or not job.page_hashes:
            return

        moved = 0
        for index, digest in enumerate(job.page_hashes):
            row = previous.get(index)
            source = job.source_hashes[index] if index < len(job.source_hashes) else None
            if not row or not source or not row.get("source_hash"):
                continue
            if row["source_hash"] == source and row.get("render_hash") not in (None, digest):
                moved += 1

        if moved:
            self.report.warn(
                f"{moved} page(s) of {job.notebook} rendered differently from last time "
                "even though the page itself did not change — the renderer moved. "
                "Check the output before trusting it; an rmc/rmscene upgrade is the usual cause."
            )

        # Three is the smallest count where repetition is not a coincidence:
        # two identical pages happen, three do not.
        if len(job.page_hashes) >= 3:
            commonest = max(set(job.page_hashes), key=job.page_hashes.count)
            repeats = job.page_hashes.count(commonest)
            if repeats >= max(3, len(job.page_hashes) // 2):
                self.report.warn(
                    f"{repeats} of {len(job.page_hashes)} pages of {job.notebook} rendered "
                    "to identical images — they are almost certainly blank."
                )

    # ── Stage 5: OCR ─────────────────────────────────────────────────────

    def _ocr_pages(self, job: DocumentJob) -> None:
        """Transcribe every prepared page into raw and cleaned text.

        Prefers single-step AI vision OCR, which reads and cleans in one call,
        and falls back per page to Google Cloud Vision plus AI text repair. If
        no page yielded text but the document carried extractable text (an
        unannotated PDF, an EPUB), that text is used instead.
        """
        use_vision_ocr = vision_ocr_available()
        if use_vision_ocr:
            log("Using AI vision OCR (single-step: reads image + cleans text)")
        else:
            log("Using Google Cloud Vision OCR + AI text cleanup")

        before = self._cache_hits
        results = self._transcribe_pages(job.pre_paths, use_vision_ocr)
        reused = self._cache_hits - before
        if reused:
            log(f"{reused} of {len(job.pre_paths)} pages came from the cache; no API call made.")

        job.transcribed_pages = len(job.pre_paths) - reused
        job.cached_pages = reused

        job.raw_texts = [raw for raw, _ in results]
        job.cleaned_texts = [cleaned for _, cleaned in results]

        if not any(t.strip() for t in job.cleaned_texts) and job.extracted_doc_text:
            job.raw_texts = [job.extracted_doc_text]
            job.cleaned_texts = [job.extracted_doc_text]

    def _transcribe_pages(self, paths: List[Path], use_vision_ocr: bool) -> List[Tuple[str, str]]:
        """Transcribe pages, several at a time, and return them in page order.

        A page is one network round trip and nothing else, so running a few
        concurrently is most of the wall-clock win available in a sync. The
        ceiling is the AI provider's rate limit, which is why the width is the
        configurable ``ocr_concurrency`` rather than the page count.

        Args:
            paths: Prepared page images, in page order.
            use_vision_ocr: Whether single-step AI vision OCR is available.

        Returns:
            One (raw text, cleaned text) pair per page, in the order given.
        """
        width = min(self.settings.ocr_concurrency, len(paths))
        if width <= 1:
            return [self._transcribe_page(p, use_vision_ocr) for p in paths]

        log(f"Transcribing {len(paths)} pages, {width} at a time...")
        with ThreadPoolExecutor(max_workers=width) as pool:
            # ``map`` yields in submission order, so pages stay in page order
            # however the calls happen to finish.
            return list(pool.map(lambda p: self._transcribe_page(p, use_vision_ocr), paths))

    def _transcribe_page(self, path: Path, use_vision_ocr: bool) -> Tuple[str, str]:
        """Transcribe one page, preferring vision OCR and falling back to Vision.

        Args:
            path: The prepared page image.
            use_vision_ocr: Whether single-step AI vision OCR is available.

        Returns:
            A (raw text, cleaned text) pair. In vision mode both are the same
            text: the model reads and cleans in one call, so there is no
            separate raw transcript.
        """
        key = self._cache_key(path, use_vision_ocr)
        if key:
            cached = self.cache.get(key)
            if cached is not None:
                with self._cache_lock:
                    self._cache_hits += 1
                log(f"  Cached: {path.name}")
                return cached
            with self._cache_lock:
                self._cache_misses += 1

        if use_vision_ocr:
            cleaned_text = self._vision_ocr_page(path)
            if cleaned_text:
                return self._cached(key, cleaned_text, cleaned_text)

        raw, cleaned = self._google_ocr_page(path)
        return self._cached(key, raw, cleaned)

    def _cache_key(self, path: Path, use_vision_ocr: bool) -> Optional[str]:
        """Return the cache key for one page, or None if it cannot be computed.

        The key covers the page image, the model and prompts behind it, and
        which of the two OCR routes produced it — the same page read by vision
        and read by Google Vision are different answers and must not share an
        entry.

        Args:
            path: The prepared page image.
            use_vision_ocr: Which OCR route is about to run.

        Returns:
            A cache key, or None when caching is off or the page is unreadable.
        """
        if not self.cache.enabled:
            return None
        try:
            image_bytes = path.read_bytes()
        except OSError:
            # No page to hash means nothing to key on; transcribe uncached.
            return None
        route = "vision" if use_vision_ocr else "google"
        return self.cache.key(image_bytes, f"{route}:{transcription_fingerprint()}")

    def _cached(self, key: Optional[str], raw: str, cleaned: str) -> Tuple[str, str]:
        """Store a freshly transcribed page and return it unchanged.

        An empty result is not stored. A page that read as nothing is usually a
        provider hiccup or a rate limit rather than a blank page, and caching
        it would make one bad minute permanent.

        Args:
            key: The cache key, or None if this page is not cacheable.
            raw: The raw OCR text.
            cleaned: The cleaned text.

        Returns:
            The ``(raw, cleaned)`` pair it was given.
        """
        if key and cleaned.strip():
            self.cache.put(key, raw, cleaned)
        return raw, cleaned

    def _vision_ocr_page(self, path: Path) -> str:
        """Read and clean one page in a single AI vision call.

        Args:
            path: The prepared page image.

        Returns:
            The cleaned text, or an empty string if vision returned nothing.
        """
        log(f"  AI Vision OCR: {path.name}...")
        cleaned_text = ocr_and_repair(str(path))
        if cleaned_text:
            return cleaned_text

        log(f"  AI Vision returned empty for {path.name}")
        if google_vision_available():
            log("  Falling back to Google Cloud Vision...")
        return ""

    def _google_ocr_page(self, path: Path) -> Tuple[str, str]:
        """Read one page with Google Cloud Vision, then repair the text with AI.

        Args:
            path: The prepared page image.

        Returns:
            A (raw text, cleaned text) pair; both empty if Vision is unavailable.
        """
        if not google_vision_available():
            log(f"  Google Cloud Vision not configured for {path.name}.")
            return "", ""

        log(f"  Google Vision OCR: {path.name}...")
        txt = vision_ocr_image_service_account(path)
        if txt is None:
            log(f"  Vision failed for {path}")

        log(f"  Cleaning text with AI for {path.name}...")
        return txt or "", repair_text_with_openai(txt or "")

    # ── Stages 4-6, skipped: an existing transcript ──────────────────────

    def _reuse_transcript(self, job: DocumentJob) -> bool:
        """Adopt a transcript from an earlier run instead of re-transcribing.

        Transcribing is the only part of a sync that costs money, and it is
        pure with respect to the page images: the same pages produce the same
        text. A transcript newer than every page it was made from is therefore
        still correct, and re-running OCR over it would be paying twice.

        This only comes up when the transcript survived the last run — after
        ``--dry-run`` or ``--keep-temp``, or when a run got as far as
        transcribing and then failed to publish. The usual auto-purge removes
        transcripts, so an ordinary repeat sync still transcribes afresh.

        Args:
            job: The job about to be transcribed; ``clean_out_txt`` is set when
                an existing transcript is adopted.

        Returns:
            True if a current transcript was adopted and OCR can be skipped.
        """
        existing = OCR_DIR / f"{job.safe_name}_clean.txt"
        if not job.imgs or not existing.exists() or not existing.stat().st_size:
            return False

        transcribed_at = existing.stat().st_mtime
        if any(p.stat().st_mtime > transcribed_at for p in job.imgs):
            log(f"Pages for {job.notebook} are newer than their transcript; transcribing again.")
            return False

        log(f"Reusing the existing transcript for {job.notebook}: {existing}")
        job.clean_out_txt = existing
        job.reused_transcript = True
        return True

    # ── Stage 6: transcripts ─────────────────────────────────────────────

    def _write_transcripts(self, job: DocumentJob) -> None:
        """Write the raw and cleaned transcripts to the output directory.

        The cleaned file is the one that gets published; the raw file exists so
        a user can see what OCR actually read before the AI tidied it.
        """
        meta = {"notebook": job.notebook, "images": [p.name for p in job.imgs]}

        raw_out_txt = OCR_DIR / f"{job.safe_name}_raw.txt"
        self._write_transcript(job, raw_out_txt, meta, job.raw_texts, pad_empty_pages=True)
        log(f"Raw OCR text saved to {raw_out_txt}")

        job.clean_out_txt = OCR_DIR / f"{job.safe_name}_clean.txt"
        self._write_transcript(
            job, job.clean_out_txt, meta, job.cleaned_texts, pad_empty_pages=False
        )
        log(f"Cleaned OCR text saved to {job.clean_out_txt}")

    def _write_transcript(
        self,
        job: DocumentJob,
        path: Path,
        meta: Dict[str, Any],
        texts: List[str],
        pad_empty_pages: bool,
    ) -> None:
        """Write one transcript: a metadata line, then a section per page.

        Args:
            job: The job being transcribed.
            path: File to write.
            meta: Metadata dict, written as the first line.
            texts: One entry per page, in page order.
            pad_empty_pages: Whether a page that produced no text still gets a
                blank body under its header.
        """
        from living_ink.extract import format_page_section_header

        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(meta) + "\n\n")

            if job.extracted_doc_text and texts == [job.extracted_doc_text]:
                f.write(job.extracted_doc_text + "\n")
                return

            for i, text in enumerate(texts):
                header = format_page_section_header(
                    job.page_number(i), job.doc_file_path, include_divider=True
                )
                body = (text or "").strip()
                if body or pad_empty_pages:
                    f.write(f"{header}\n\n{body}\n\n")
                else:
                    f.write(f"{header}\n\n")

    # ── Stage 7: publish ─────────────────────────────────────────────────

    def _publish(self, job: DocumentJob, needs_update: Dict[str, List[Destination]]) -> bool:
        """Publish the cleaned transcript to every destination that wants it.

        Args:
            job: The processed job.
            needs_update: Mapping from document ID to the destinations that are
                behind on it. No entry means a forced run, which targets every
                active destination.

        Returns:
            True if every targeted destination accepted the note.
        """
        try:
            clean_text = _strip_transcript_metadata(job.clean_out_txt)
            targets = needs_update.get(job.notebook_id) or (
                self.destinations or get_default_destinations()
            )
            if not targets:
                log("No destinations need update for this notebook (or none configured).")
                return True

            if self.dry_run:
                self._report_dry_run(job, targets)
                return True

            all_success = True
            for dest in targets:
                if self._publish_to(dest, job, clean_text):
                    job.published_to.append(type(dest).__name__)
                    # Update state for THIS destination immediately.
                    add_to_processed_log(
                        type(dest).__name__,
                        job.notebook_id,
                        job.version,
                        run_id=self.run_id,
                        external_id=dest.last_external_id,
                        target=dest.last_target,
                    )
                else:
                    all_success = False
                    log(f"⚠️ Failed to publish to {type(dest).__name__}")

            return all_success
        except Exception as e:
            # Deliberately broad. This is the stage boundary: a notebook that
            # cannot be published is reported as one failed notebook, with the
            # traceback, so the rest of the run still goes out.
            log(f"Failed publishing note: {e}")
            import traceback

            log(traceback.format_exc())
            return False

    def _report_dry_run(self, job: DocumentJob, targets: List[Destination]) -> None:
        """Say what a real run would have published, and where to read it.

        Nothing is sent and no processed-log entry is written, so the same
        notebook is still pending afterwards and a later real run picks it up.

        Args:
            job: The processed job.
            targets: The destinations a real run would have published to.
        """
        log(f"🔍 Dry run — not publishing '{job.display_title}'.")
        job.would_publish_to = [type(dest).__name__ for dest in targets]
        for dest in targets:
            sub_folder = (
                job.top_level_subfolder()
                if isinstance(dest, AppleNotesDestination)
                else job.full_subfolder()
            )
            where = f" under '{sub_folder}'" if sub_folder else ""
            log(f"   Would publish to {dest.describe()}{where}")
        log(f"   Transcript: {job.clean_out_txt}")
        if job.imgs:
            log(f"   {len(job.imgs)} page image(s) in {WHITE_DIR}")
        if job.tags:
            log(f"   Tags: {job.tags}")

    def _publish_to(self, dest: Destination, job: DocumentJob, clean_text: str) -> bool:
        """Publish one note to one destination.

        A DestinationError is an expected, user-actionable failure (vault gone,
        Notes not responding): report it plainly and let the caller carry on to
        the next destination. Anything else is a bug, and is logged with a
        traceback so it is distinguishable.

        Args:
            dest: The destination to publish to.
            job: The processed job.
            clean_text: The note body.

        Returns:
            True if the destination accepted the note.
        """
        dest_name = type(dest).__name__
        log(f"Publishing to {dest_name}...")

        # Apple Notes only supports 1 level of sub-folder under rootFolder.
        # Obsidian supports the full nested hierarchy.
        sub_folder = (
            job.top_level_subfolder()
            if isinstance(dest, AppleNotesDestination)
            else job.full_subfolder()
        )

        # What this destination called the note last time, so it can replace
        # exactly that one instead of deleting whatever shares the title.
        previous = get_state_store().get_publication(job.notebook_id, dest_name)
        existing_id = previous["external_id"] if previous else None
        # Where it landed last time. A notebook renamed or moved on the tablet
        # is the same note in a new place, not a second note.
        existing_target = previous["target"] if previous else None
        # When the note first appeared here, for a note that has to state when
        # it came into existence and predates the frontmatter that says so.
        first_published = to_iso_date(previous["first_published_at"]) if previous else None

        try:
            published = dest.publish(
                notebook_name=job.display_title,
                text_content=clean_text,
                image_paths=job.imgs,
                sub_folder=sub_folder,
                document_path=job.source_file(),
                tags=job.tags,
                existing_id=existing_id,
                # Only when we already know we published here before: then the
                # note carrying this title is one we created.
                adopt_by_name=bool(previous) and not existing_id,
                doc_id=job.notebook_id,
                existing_target=existing_target,
                document_modified=job.modified_date(),
                first_published=first_published,
            )
        except DestinationError as e:
            log(f"⚠️ {dest_name}: {e}")
            return False
        except Exception:
            # Deliberately broad, and the message says so: a destination is
            # contracted to raise DestinationError, so anything else reaching
            # here is a defect in that destination. Print it loudly and keep
            # publishing to the others.
            import traceback

            log(f"❌ Unexpected error publishing to {dest_name} — this is a bug:")
            log(traceback.format_exc())
            return False

        return published

    def _handle_orphans(self, id_map: Dict[str, Any]) -> None:
        """Report, and optionally delete, notes whose notebook is gone.

        A document that was published once and is no longer in the tablet's
        listing has usually been deleted there — but it can also mean the
        listing came back short, and acting on that would destroy notes for a
        transport hiccup. So the default is to say so and do nothing;
        ``--prune`` is the user taking responsibility for the difference.

        The whole listing is used, not the sync candidates, so a notebook in
        the trash or of a type this run skipped is not mistaken for a deletion.

        Args:
            id_map: Every document the tablet listed, keyed by id.
        """
        if not id_map or self.dry_run:
            # An empty listing means the transport told us nothing, which is
            # not the same as the tablet being empty.
            return

        try:
            publications = get_state_store().all_publications()
        except (sqlite3.Error, OSError, RuntimeError) as e:
            log(f"⚠️ Could not check for deleted notebooks: {e}")
            return

        orphans = {doc_id: rows for doc_id, rows in publications.items() if doc_id not in id_map}
        if not orphans:
            return

        if not self.prune:
            log(f"{len(orphans)} published notebook(s) are no longer on the tablet:")
            for doc_id, rows in orphans.items():
                where = ", ".join(sorted(rows))
                log(f"  {self._orphan_label(doc_id)} — still in {where}")
            log("Their notes were left alone. Run with --prune to delete them.")
            return

        for doc_id, rows in orphans.items():
            self._prune_orphan(doc_id, rows)

    def _orphan_label(self, doc_id: str) -> str:
        """Return the friendliest name known for a document that is gone.

        Args:
            doc_id: reMarkable document id.

        Returns:
            The recorded name, falling back to the id.
        """
        try:
            document = get_state_store().get_document(doc_id)
        except (sqlite3.Error, OSError, RuntimeError):
            return doc_id
        name = (document or {}).get("name")
        return f"{name} ({doc_id})" if name else doc_id

    def _prune_orphan(self, doc_id: str, rows: Dict[str, Any]) -> None:
        """Delete one deleted notebook's notes and forget it.

        The state row is dropped whatever the destination says. A destination
        that refuses — because the note is already gone, or carries no proof of
        ownership — has still told us everything it is going to, and keeping
        the row would only report the same orphan on every run.

        Args:
            doc_id: reMarkable document id.
            rows: Its publication rows, keyed by destination class name.
        """
        by_name = {type(d).__name__: d for d in (self.destinations or get_default_destinations())}
        label = self._orphan_label(doc_id)

        for dest_name, row in rows.items():
            dest = by_name.get(dest_name)
            if dest is None:
                log(f"  {label}: {dest_name} is not configured; its note was left alone.")
                continue
            try:
                removed = dest.unpublish(
                    target=row.get("target"),
                    external_id=row.get("external_id"),
                    doc_id=doc_id,
                )
            except DestinationError as e:
                log(f"  ⚠️ {dest_name}: {e}")
                continue
            log(f"  {label}: {'deleted from' if removed else 'left alone in'} {dest_name}.")

        try:
            get_state_store().forget(doc_id)
        except (sqlite3.Error, OSError, RuntimeError) as e:
            log(f"  ⚠️ Could not forget {label}: {e}")

    def run(self) -> bool:
        """Execute the sync pipeline.

        Options were resolved in ``__init__``; to sync with different options,
        construct a new pipeline (``SyncPipeline(opts.merged_with(limit=1))``)
        rather than mutating this one.

        Returns:
            True if sync succeeded or completed gracefully, False on error.
        """
        ensure_runtime_dirs()
        logs.ensure_configured(LOG_PATH)
        logs.mark_run_start()
        try:
            return self._run_recorded()
        finally:
            self.run_id = None

    def _run_recorded(self) -> bool:
        """Run the pipeline inside an open state-database run record.

        Returns:
            True if sync succeeded or completed gracefully, False on error.
        """
        store = get_state_store()
        # A dry run must leave no trace, and a run row is a trace.
        self.run_id = None if self.dry_run else store.start_run()
        seen = published = failed = 0
        outcome = "error"
        try:
            result = self._execute()
            seen, published, failed = self._counts
            outcome = "success" if result else "partial"
            return result
        except KeyboardInterrupt:
            # Ctrl+C is not a failure, and the run did not do nothing. Record
            # what it got through, then let the interrupt carry on out.
            seen, published, failed = self._counts
            outcome = "interrupted"
            self._report_interrupt(published)
            raise
        finally:
            if self.run_id is not None:
                store.finish_run(
                    self.run_id,
                    outcome=outcome,
                    seen=seen,
                    published=published,
                    failed=failed,
                )

    def _report_interrupt(self, published: int) -> None:
        """Say what an interrupted run kept, so the user knows what it cost.

        Transcribing is the only part of a sync that costs money, and every
        page is written to the transcript cache the moment it comes back. An
        interrupt therefore loses the current page and nothing else — but that
        is not obvious from the outside, so it is worth saying out loud.

        Args:
            published: Notebooks published before the interrupt.
        """
        log("")
        log("⏹️  Interrupted.")
        if published:
            log(f"   {published} notebook(s) were published and will not be synced again.")
        if self.cache.enabled:
            log("   Pages already transcribed are cached; resuming will not pay for them twice.")

    def _execute(self) -> bool:
        """Do the actual sync work.

        Returns:
            True if sync succeeded or completed gracefully, False on error.
        """
        self._counts = (0, 0, 0)
        self.report = RunReport()

        validate_environment()
        log("Pipeline started.")
        if self.dry_run:
            log("🔍 Dry run: nothing will be published and no sync state will be recorded.")

        # Clean temporary working artifacts at start of run and register exit cleanup
        import atexit

        cleanup_temp_artifacts(keep_temp=self.keep_temp)
        atexit.register(cleanup_temp_artifacts, keep_temp=self.keep_temp)

        client = self.connect()
        self._learn_device(client)
        notebooks, id_map = self.discover_documents(client)
        self._counts = (len(notebooks), 0, 0)
        self._handle_orphans(id_map)
        to_process, needs_update, should_continue = self.filter_pending_documents(notebooks, id_map)

        if not should_continue:
            return False
        self._report_unchanged(notebooks, to_process)
        if not to_process:
            self._print_summary()
            return True

        all_success = True
        published = failed = 0
        for nb_item in to_process:
            item_success = self.process_notebook_item(
                nb_item=nb_item,
                client=client,
                id_map=id_map,
                needs_update=needs_update,
                keep_temp=self.keep_temp,
            )
            if item_success:
                published += 1
            else:
                failed += 1
                all_success = False
            # Updated per notebook, not once at the end: a run that is
            # interrupted half way through still did the work it did, and the
            # run record has to say so.
            self._counts = (len(notebooks), published, failed)

        log("Pipeline finished.")
        cleanup_temp_artifacts(keep_temp=self.keep_temp)
        self._print_summary()
        return all_success

    def _report_unchanged(self, notebooks: List[Any], to_process: List[Any]) -> None:
        """Record the documents this run will not touch, and why.

        A summary that lists only what was synced cannot answer "why was my
        notebook not picked up", which is the question a user actually has.

        Args:
            notebooks: Everything discovered on the device.
            to_process: The subset this run will work on.
        """
        if self.report is None:
            return
        if self.target_notebook:
            # A targeted run did not consider the rest of the library, so
            # calling it "unchanged" would be a claim it never checked.
            return
        pending = {id(item) for item in to_process}
        for item in notebooks:
            if id(item) in pending:
                continue
            self.report.add(
                DocumentOutcome(
                    name=str(
                        get_val(item, "VissibleName")
                        or get_val(item, "VisibleName")
                        or get_val(item, "ID")
                        or "(unnamed)"
                    ),
                    doc_id=get_val(item, "ID"),
                    status=SKIPPED,
                    reason="unchanged",
                )
            )

    def _print_summary(self) -> None:
        """Print the run summary, as a table or as JSON.

        The summary is the last thing on screen on purpose: per-notebook log
        lines scroll, and the one question left at the end of a sync is what
        the whole thing added up to.
        """
        if self.report is None:
            return
        self.report.finish()
        if self.json_output:
            # Straight to stdout, not through log(): under --json every
            # progress line has been moved to stderr precisely so this
            # document can be the only thing a caller has to parse.
            print(self.report.as_json())
            _logger.info("Run summary: %s", self.report.as_json())
            return
        log(self.report.render())
