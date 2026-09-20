#!/usr/bin/env python3
"""Process notebooks: preprocess PNGs, run OCR, aggregate text, publish notes."""

import atexit
import datetime
import hashlib
import json
import logging
import os
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Tuple

import yaml
from PIL import Image, ImageFilter, ImageOps

from living_ink import logs, state
from living_ink.cache import CACHE_DIRNAME, RENDER_CACHE_DIRNAME, RenderCache, TranscriptCache
from living_ink.clean import configure as configure_ai_provider
from living_ink.clean import (
    ocr_and_repair,
    transcription_fingerprint,
    vision_ocr_available,
)
from living_ink.config import (
    ConfigurationMissing,
    apply_status,
    find_repo_root,
    get_config_path,
    get_data_dir,
    get_logs_dir,
    split_problems,
    validate_config,
)
from living_ink.core.document import Document, Page, PublishContext, PublishResult
from living_ink.core.listing import (
    document_version,
    get_document_type,
    get_notebook_path,
    get_val,
)
from living_ink.core.selection import (
    NOT_TARGETED,
    WRONG_TYPE,
    Candidate,
    Selection,
    SelectionCriteria,
    select,
)
from living_ink.core.temp import DocumentWorkspace, page_number, purge_all
from living_ink.destinations import (
    DESTINATION_REGISTRY,
    Destination,
    DestinationError,
    MergeUnit,
    build_destinations,
)
from living_ink.devices import default_reading
from living_ink.redact import redact, register_secret
from living_ink.report import (
    DEFERRED,
    FAILED,
    PUBLISHED,
    SKIPPED,
    WOULD_PUBLISH,
    DocumentOutcome,
    RunReport,
)
from living_ink.safeio import restrict_permissions
from living_ink.settings import Settings

if TYPE_CHECKING:  # pragma: no cover - names for annotations only
    # Imported for typing rather than at module level on purpose: the sources
    # registry is reached at call time, so a source module can import from the
    # pipeline's neighbours without a cycle.
    from living_ink.sources import PageRef, RenderContext, SourceBundle, SourceType


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

# One subdirectory per document, named by its id. Five directories keyed on
# the sanitised *title* used to live here instead, which is how two similarly
# named notebooks came to share their pages. See living_ink.core.temp.
WORK_DIR = DATA_DIR / "work"
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
    for folder in (DATA_DIR, LOGS_DIR, WORK_DIR):
        folder.mkdir(parents=True, exist_ok=True)
    _runtime_dirs_ready = True


# --- CONFIGURATION LOADING (YAML) ---
def check_config(config: Dict[str, Any], cfg_path: Path) -> None:
    """Validate a parsed config and act on what the schema found.

    Errors stop the run, which is every unknown key and every unreadable value:
    a config Living Ink cannot fully read is one it cannot obey, and syncing
    anyway means doing something other than what the file asks and calling it
    success. Warnings — deprecations, settings still honoured but on their way
    out — are printed and logged, and the run continues.

    Sections belonging to registered destinations are passed through as known,
    so adding a destination does not make its own config section look like a
    misspelling.

    Args:
        config: Parsed ``config.yml`` contents.
        cfg_path: Where that config was read from, named in the error so the
            user knows which of the eight candidate paths actually won.

    Raises:
        ConfigurationMissing: If the config holds anything this build cannot
            read, listing every such problem rather than only the first — one
            slip usually means several, and fixing them one run at a time is
            its own small misery.
    """
    errors, warnings = split_problems(
        validate_config(config, extra_sections=tuple(DESTINATION_REGISTRY))
    )

    for problem in warnings:
        message = f"config.yml — {problem.describe()}"
        print(f"⚠️  {message}")
        _logger.warning(message)

    if not errors:
        return

    noun = "problem" if len(errors) == 1 else "problems"
    detail = "\n".join(f"  - {problem.describe()}" for problem in errors)
    raise ConfigurationMissing(
        f"{cfg_path} has {len(errors)} {noun}:\n{detail}",
        hint=f"edit {cfg_path}",
    )


def _migrate_config_ai_key(yaml_config: Dict[str, Any], cfg_path: Path) -> None:
    """Copy an API key left in ``config.yml`` into the credentials directory.

    The key is stored per provider — ``ai.api_key.gemini``, not ``ai.api_key``
    — so that trying OpenAI for an afternoon and going back does not mean
    retyping the Gemini key. Reading it back out is
    :class:`living_ink.settings.Settings`' job; this is only the move, which
    happens once, silently, on the next run after an upgrade. The original is
    left where it was, so downgrading does not mean retyping it either.

    Args:
        yaml_config: The parsed config. Not modified.
        cfg_path: The config file the credentials directory is derived from.
    """
    from living_ink.config.credentials import ai_key_name, migrate_secret

    section = yaml_config.get("ai")
    if not isinstance(section, dict):
        return

    in_config = str(section.get("api_key", "") or "").strip()
    if not in_config:
        return

    provider = str(section.get("provider", "")).strip().lower()
    if not provider or provider == "none":
        return

    try:
        name = ai_key_name(provider)
    except ValueError:
        # A provider name that cannot be a credential name is a config error,
        # and get_provider is about to report it far better than this could.
        return

    migrate_secret(name, in_config, config_path=cfg_path)


def load_yaml_config(config_path: Optional[Path] = None) -> Dict[str, Any]:
    """Load configuration from YAML and export the credentials third parties read.

    Only settings that another library picks up from the environment on its own
    are exported (``OPENAI_API_KEY``). Living Ink's own settings are not: they
    are resolved from this dictionary by :class:`living_ink.settings.Settings`
    and passed explicitly.

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

                # 2. AI Provider — configured from the settings this config
                #    resolves to, which is where the stored key is read from.
                _migrate_config_ai_key(yaml_config, cfg_path)
                configure_ai_provider(Settings.resolve(yaml_config, config_path=cfg_path))

        except Exception as e:
            # Deliberately broad. Everything downstream of the parse — env
            # export, credential weaving, provider configuration — is driven by
            # whatever shape the user's YAML happens to have, and a config that
            # cannot be understood has to degrade to one printed line and an
            # empty dict rather than abort the run before it reports anything.
            print(f"Critical error loading config.yml: {e}")
            logging.debug("Loading %s failed", cfg_path, exc_info=True)

        # Outside the try above on purpose: that block exists to keep a
        # malformed config from aborting the run before it reports anything,
        # and swallowing the validation result would defeat the point of
        # validating.
        check_config(yaml_config, cfg_path)

    # Legacy Fallback
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    # Last, so that everything above sees the file as the user wrote it and
    # everything downstream sees only spellings this build still knows: dead
    # sections are gone and deprecated keys have been copied onto their
    # replacements.
    return apply_status(yaml_config)


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


def registered_state_keys() -> List[str]:
    """Return the state key of every destination this build registers.

    Registered, not enabled: a destination the user has turned off still owns
    its publication rows, and turning it back on must not re-publish the whole
    library. Only a destination that no longer exists has no claim on them.

    Returns:
        One :attr:`Destination.state_key` per entry in the registry.
    """
    return [cls.state_key for cls in DESTINATION_REGISTRY.values()]


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

    So does the sweep of publication rows belonging to a destination this build
    no longer ships. This is the layer that can do it: ``state`` must not know
    what a destination is, and the registry only exists once ``destinations``
    has been imported. It runs after the legacy import, so a row that arrives
    from an old JSON file naming a deleted destination is swept in the same
    pass rather than surviving until the next run.

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
        for name, count in store.forget_unknown_destinations(registered_state_keys()).items():
            log(f"🧹 Forgot {count} publication record(s) for {name}, which no longer exists.")
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


def reset_caches() -> None:
    """Drop everything this module holds for the lifetime of the process.

    The three accessors above cache so that one run reads ``config.yml`` once.
    Across runs that is wrong: ``watch`` ticks in a single process for weeks,
    so a config read at start would be the config for ever, and
    ``get_default_destinations()`` would hand every tick the *same mutable
    objects* — one tick's per-run override leaking into the next.

    The scheduler calls this between ticks. A single ``sync`` never needs it,
    which is why the accessors do not invalidate themselves on a timer: the
    boundary is a run ending, and only the caller knows where that is.
    """
    global _default_config, _default_destinations

    _default_config = None
    _default_destinations = None
    reset_state_store()


def load_processed_log(dest_name: str):
    """Return the published version of every document for one destination.

    Args:
        dest_name: Destination class name, e.g. ``ObsidianDestination``.

    Returns:
        Mapping of document id to the version last published there.
    """
    return get_state_store().published_versions(dest_name)


def add_to_processed_log(
    dest_name: str,
    doc_id,
    version,
    *,
    recipe: str,
    pages_failed: int = 0,
    run_id=None,
    external_id=None,
    target=None,
):
    """Record that a document reached a destination.

    Args:
        dest_name: Destination class name.
        doc_id: reMarkable document id.
        version: Device version or content hash that was published.
        recipe: Digest of everything other than the document that shaped the
            output, so a prompt edit or a settings change makes it pending
            again even though the tablet's version is unchanged.
        pages_failed: How many pages did not transcribe, so a partial publish
            stays pending and the next run retries it.
        run_id: Run that published it, when one is in progress.
        external_id: Identifier the destination gave the note, so the next
            sync replaces that exact note rather than one sharing its title.
        target: Where the note landed, so a later run can tell it has moved.
    """
    get_state_store().record_publication(
        doc_id,
        dest_name,
        version,
        recipe=recipe,
        pages_failed=pages_failed,
        run_id=run_id,
        external_id=external_id,
        target=target,
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
    """Delete every document workspace left on disk.

    Args:
        keep_temp: If True, preserve files on disk for debugging.
    """
    if keep_temp:
        log("Preserving temporary working files (--keep-temp enabled).")
        return

    purge_all(WORK_DIR)


_temp_cleanup_registered = False
_temp_cleanup_keep = False


def register_temp_cleanup(keep_temp: bool) -> None:
    """Arrange for the temp folders to be purged when the process exits.

    Once per process, not once per run. ``watch`` loops in a single process for
    weeks, and registering inside each run leaves one handler per tick — a list
    that only grows, each entry pinning the ``keep_temp`` of a run that ended
    long ago. The flag lives beside the registration instead, so the last run
    to ask is the one the exit handler obeys.

    Args:
        keep_temp: Whether the run now starting wants its artifacts preserved.
    """
    global _temp_cleanup_registered, _temp_cleanup_keep

    _temp_cleanup_keep = keep_temp
    if _temp_cleanup_registered:
        return
    atexit.register(lambda: cleanup_temp_artifacts(keep_temp=_temp_cleanup_keep))
    _temp_cleanup_registered = True


def validate_environment():
    """Check configuration health, logging warnings and failing on hard errors.

    Raises:
        ConfigurationMissing: If configuration is absent or invalid. Offering
            the setup wizard is the CLI's decision, not this function's.
    """
    docs_hint = "run: living-ink setup"

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

    # 2. Check that something can read a page. There is one OCR backend — a
    # multimodal call to the configured provider — so a provider without
    # vision is not a degraded run, it is a run that transcribes nothing.
    if not vision_ocr_available():
        errors.append(
            "❌ No OCR method available. Set 'ai.provider' in config.yml to a "
            "provider with vision support (e.g. gemini, openai, or ollama for a "
            "local model)."
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
    #: Where this document's throwaway artifacts go. Keyed on the document id,
    folder_path: str
    display_title: str
    keep_temp: bool
    #: Where this document's throwaway artifacts go. Derived from the document
    #: id rather than the title, so two notebooks with similar names cannot
    #: write over each other's pages. Left unset by everything but a test.
    workspace: DocumentWorkspace = None  # type: ignore[assignment]
    #: Where the original PDF or EPUB inside the zip is unpacked, for the
    #: sources that have one. Inside the workspace, like everything else.
    doc_file_path: Optional[Path] = None

    #: Recipe digest per destination ``state_key``, computed by the selection
    #: pass that decided this document was pending. Carried rather than
    #: recomputed so the row recorded on the way out describes the inputs the
    #: decision was made on, even under ``--force``.
    recipes: Dict[str, str] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)
    imgs: List[Path] = field(default_factory=list)
    # One entry per rendered page, in page order. Built once the images are
    # settled and replaced in place as later stages learn the text: everything
    # a destination needs to *place* a page is known at render time, and
    # re-deriving it at publish time is what made the destination reopen the
    # source PDF once per page.
    pages: List[Page] = field(default_factory=list)
    page_hashes: List[str] = field(default_factory=list)
    source_hashes: List[str] = field(default_factory=list)
    transcribed_pages: int = 0
    cached_pages: int = 0
    failed_pages: int = 0
    published_to: List[str] = field(default_factory=list)
    would_publish_to: List[str] = field(default_factory=list)
    pre_paths: List[Path] = field(default_factory=list)
    extracted_doc_text: str = ""

    def __post_init__(self) -> None:
        """Derive the workspace from the document id when none was given."""
        if self.workspace is None:
            self.workspace = DocumentWorkspace(WORK_DIR, str(self.notebook_id))

    def modified_at(self) -> Optional[datetime.datetime]:
        """Return when the tablet says this notebook was last written on.

        Returns:
            The timestamp, or None when the transport reported nothing usable.
            A destination that wants a date takes it from here; today's date is
            never substituted, because "the tablet did not say" and "the tablet
            said today" are different facts.
        """
        return to_datetime(
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
            number = page_number(self.imgs[index])
            if number is not None:
                return number
        return index + 1

    def source_file(self) -> Optional[Path]:
        """Return the original PDF/EPUB to attach, if one was retrieved."""
        path = self.doc_file_path
        return path if path and path.exists() else None

    def folder_parts(self) -> Tuple[str, ...]:
        """Return the reMarkable folder hierarchy, outermost first.

        The parts, not a joined path: how deep a destination nests is its own
        decision, and this used to be two methods here because the pipeline
        made that decision for it by checking the destination's class.

        Returns:
            One entry per folder, empty at the library root.
        """
        return tuple(p.strip() for p in self.folder_path.split(" / ") if p.strip())


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
        )

        # Opened by run(); every state row written during that run carries it,
        # so "what did the 03:00 sync touch" has an answer.
        self.run_id: Optional[int] = None
        self.report: Optional[RunReport] = None

        # Set here and not at the top of _execute(): anything that raises
        # before the run proper starts — a Ctrl+C landing on entry, a failed
        # connection — is read back by _run_recorded(), and an unset attribute
        # there turns the real error into an AttributeError about counting.
        self._counts: Tuple[int, int, int] = (0, 0, 0)

        # 3. Transcription cache. Pages are transcribed concurrently, so the
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

    def connect(self) -> Any:
        """Establish connection to reMarkable tablet (via SSH or Cloud)."""
        from living_ink.api import get_rmapi

        return get_rmapi(self.settings)

    def preflight_destinations(self) -> None:
        """Refuse the run before a page is rendered if nowhere can receive it.

        Two failures, and they used to look identical from the outside. A vault
        that does not exist made ``build_destinations`` print one warning and
        return an empty list; the run then compared every document against no
        destinations, concluded nothing was pending, and exited 0 — reporting
        success for having published nothing. Both are now a hard stop with the
        reason and the remedy.

        Every enabled destination is checked, not just the first to fail, so a
        misconfigured pair is fixed in one pass rather than two runs.

        Raises:
            ConfigurationMissing: No destination is enabled, or at least one
                cannot publish right now.
        """
        active = self.destinations or get_default_destinations()
        if not active:
            raise ConfigurationMissing(
                "No destination is enabled, so there is nowhere to publish.",
                hint="Enable one with 'living-ink setup', or set obsidian.vault_path.",
            )

        failures = []
        for dest in active:
            status = dest.check()
            if status.ok:
                _logger.info("%s ready: %s", dest.display_name, status.detail)
                continue
            failures.append(
                status.detail if not status.remedy else f"{status.detail}\n   → {status.remedy}"
            )

        if failures:
            raise ConfigurationMissing(
                "A destination is not ready:\n" + "\n".join(f"❌ {f}" for f in failures),
                hint="Run 'living-ink status' to see every destination's state.",
            )

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

    def _criteria(self) -> SelectionCriteria:
        """Translate this run's resolved options into what narrows it.

        The one place the pipeline's long-standing attribute names are turned
        into the vocabulary :mod:`living_ink.core.selection` speaks, so the
        preview and the run can be handed the same object.

        Returns:
            The criteria for this run.
        """
        from living_ink.sources import SOURCE_REGISTRY

        if self.all_types:
            types = frozenset(SOURCE_REGISTRY)
        else:
            enabled = {"notebook"}
            if self.sync_pdfs:
                enabled.add("pdf")
            if self.sync_epubs:
                enabled.add("epub")
            types = frozenset(enabled)

        return SelectionCriteria(
            target=self.target_notebook,
            types=types,
            # A named notebook is not part of a sweep, so the sweep's cap does
            # not apply to it — and applying it would deal the second of two
            # same-named matches into ``deferred``, where the prompt that asks
            # the user which one they meant can no longer see it.
            limit=None if self.target_notebook else self.limit,
            # Naming a notebook is asking for that notebook, whether or not the
            # comparison thinks it is current. There is no --force flag yet;
            # this is the one thing that already behaved like one.
            force=bool(self.target_notebook),
        )

    def select_documents(self, listing: Sequence[Any], client: Any) -> Optional[Selection]:
        """Decide what this run will touch, and record the library it saw.

        The inventory is written first, and for every document rather than
        only the pending ones: "what is on my tablet" has to be answerable
        without talking to the tablet again, and it is recorded *before* the
        comparison so the classifier judges this run's facts.

        Args:
            listing: Everything the transport reported.
            client: The connected transport, passed on so the document type is
                the one the device reports rather than one guessed from a title.

        Returns:
            The selection, including what was left out and why — or None when
            ``--notebook`` named something the library does not contain, which
            is a failed run rather than an empty one.
        """
        id_map = {get_val(item, "ID"): item for item in listing}
        self._record_inventory(listing, id_map)

        chosen = select(
            listing,
            self._criteria(),
            get_state_store(),
            self.destinations or get_default_destinations(),
            settings=self.settings,
            client=client,
        )

        if not self.target_notebook:
            self._report_type_skips(chosen)
            return chosen

        log(f"Filtering for notebook: {self.target_notebook}")
        if not chosen.to_process:
            log(f"Notebook '{self.target_notebook}' not found in library. Exiting.")
            return None
        return self._disambiguate(chosen, id_map)

    def _report_type_skips(self, chosen: Selection) -> None:
        """Say how many documents were passed over for their type alone.

        Counted from the selection rather than tallied during discovery, so the
        number and the decision cannot disagree.

        Args:
            chosen: What this run decided to do.
        """
        skipped: Dict[str, int] = {}
        for item, reason in chosen.skipped:
            if reason == WRONG_TYPE:
                source = get_document_type(item)
                skipped[source] = skipped.get(source, 0) + 1

        for source, count in sorted(skipped.items()):
            log(
                f"Skipped {count} {source.upper()} document(s) "
                f"(enable with --sync-{source}s or in config.yml)."
            )

    def _disambiguate(self, chosen: Selection, id_map: Dict[str, Any]) -> Selection:
        """Let the user pick when ``--notebook`` matched more than one document.

        Args:
            chosen: The selection, already narrowed to the matches.
            id_map: Every listed item by id, for rendering the choices.

        Returns:
            The same selection when there is nothing to disambiguate, otherwise
            one narrowed to what the user chose — empty if they cancelled.
        """
        if not self.target_notebook:
            return chosen

        keep = select_notebook_interactive(
            matches=[candidate.item for candidate in chosen.to_process],
            query=self.target_notebook,
            id_map=id_map,
        )
        if not keep:
            log("Sync cancelled by user. Exiting.")
            return replace(chosen, to_process=())

        chosen_items = {id(item) for item in keep}
        return replace(
            chosen,
            to_process=tuple(c for c in chosen.to_process if id(c.item) in chosen_items),
        )

    def _record_inventory(self, listing: Sequence[Any], id_map: Dict[str, Any]) -> None:
        """Note every document the tablet listed in the state database.

        Args:
            listing: Everything the transport reported, folders included.
            id_map: Every listed item by id, for resolving folder paths.
        """
        for item in listing:
            if get_val(item, "Type") != "DocumentType":
                continue
            self._record_seen_document(item, get_val(item, "ID"), document_version(item), id_map)

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

    def process_notebook_item(
        self,
        candidate: Candidate,
        client: Any,
        keep_temp: Optional[bool] = None,
    ) -> bool:
        """Process a single notebook or document item through extraction, OCR, and publishing.

        The stages run in a fixed order and pass state through a
        :class:`DocumentJob`: acquire pages, collect tags, preprocess images,
        OCR, write transcripts, publish. A stage that finds nothing left to do
        raises :class:`_StopProcessing` carrying the verdict to report.

        Args:
            candidate: What the selection pass decided about this document —
                its identity, its type, and the destinations that owe it a
                publish. Nothing here re-derives any of it.
            client: reMarkable API client.
            keep_temp: Whether to keep temporary files on disk.

        Returns:
            True if notebook was processed and published successfully, False otherwise.
        """
        job = self._describe_job(candidate, keep_temp)

        try:
            self._acquire_pages(job, client)
            self._collect_tags(job, client)
            self._preprocess_images(job)
            self._ocr_pages(job)
            self._write_transcripts(job)
            success = self._publish(job, candidate.pending)
        except _StopProcessing as stop:
            if stop.reason:
                log(stop.reason)
            self._record_outcome(job, stop.success, stop.reason)
            self._report_job(job, stop.success, stop.reason)
            return stop.success

        if success:
            log(f"Notebook {job.notebook} processing complete.")
            if not job.keep_temp:
                job.workspace.purge()
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

    def _process_one(self, candidate: Candidate, client: Any) -> bool:
        """Process one document, and never let its failure end the run.

        The stages catch ``_StopProcessing``, which is the failure they *mean*;
        anything else — a transport that gave up after both fallbacks, a
        truncated PNG that throws inside PIL — used to propagate out of the
        batch loop, past ``_run_recorded``, to ``cli.main``. On the unattended
        nightly run this product is built around, that meant document 12 of 40
        took the other 28 with it: not processed, not recorded as failed, and
        no run summary at all, because ``_print_summary`` is after the loop.

        ``_transcribe_page`` already says "an error here costs one page, never
        the document". This is the same rule one level up.

        Args:
            candidate: The document to process.
            client: reMarkable API client.

        Returns:
            True if the document published, False if it failed for any reason.
        """
        try:
            return self.process_notebook_item(
                candidate=candidate,
                client=client,
                keep_temp=self.keep_temp,
            )
        except KeyboardInterrupt:
            # Ctrl+C is the user ending the run, not this document failing.
            raise
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"
            log(f"❌ {candidate.name} failed unexpectedly: {redact(reason)}")
            self._record_candidate_failure(candidate, reason)
            return False

    def _record_candidate_failure(self, candidate: Candidate, reason: str) -> None:
        """Report and remember a document that died before its stages could.

        Reported from the candidate rather than the job, because the job is
        one of the things that may not exist yet.

        Args:
            candidate: The document that failed.
            reason: What went wrong, unredacted; redacted on the way out.
        """
        if self.report is not None:
            self.report.add(
                DocumentOutcome(
                    name=candidate.name,
                    doc_id=candidate.doc_id,
                    status=FAILED,
                    reason=redact(reason),
                )
            )
        if self.dry_run:
            return
        try:
            get_state_store().record_failure(candidate.doc_id, redact(reason))
        except (sqlite3.Error, OSError, RuntimeError) as e:
            log(f"⚠️ Could not record the outcome for {candidate.doc_id}: {e}")

    # ── Stage 1: identify ────────────────────────────────────────────────

    def _describe_job(
        self,
        candidate: Candidate,
        keep_temp: Optional[bool],
    ) -> DocumentJob:
        """Turn a chosen document into the job the later stages operate on.

        Every fact here was established by the selection pass and is carried
        over rather than recomputed. Re-probing the type would mean a second
        round trip per document *and* the risk of answering differently from
        the answer the recipe was digested against.

        Args:
            candidate: The document this run chose, and what it knows about it.
            keep_temp: Per-call override for keeping temp artifacts.

        Returns:
            A DocumentJob with identity, paths and titles filled in.
        """
        from living_ink.sources import source_for_name

        notebook = candidate.name
        doc_type = candidate.source
        folder_path = candidate.folder
        workspace = DocumentWorkspace(WORK_DIR, str(candidate.doc_id)).ensure()

        # Where the source document lands, if this type has one. The extension
        # comes from the registered source rather than a `doc_type in
        # ("pdf", "epub")` test, so a new format needs no edit here.
        suffix = source_for_name(doc_type).source_suffix

        job = DocumentJob(
            item=candidate.item,
            notebook=notebook,
            notebook_id=candidate.doc_id,
            doc_type=doc_type,
            version=_item_version(candidate.item),
            folder_path=folder_path,
            display_title=f"{folder_path} / {notebook}" if folder_path else notebook,
            keep_temp=self.keep_temp if keep_temp is None else keep_temp,
            workspace=workspace,
            doc_file_path=workspace.source_file(suffix),
            recipes=dict(candidate.recipes),
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
        self._describe_pages(job)

    def _describe_pages(self, job: DocumentJob) -> None:
        """Record what each rendered page *is*, while the source is still around.

        The page number, the label a heading shows and the chapter it sits
        under are all properties of the render, not of the publish. They used
        to be recovered at publish time by regexing the PNG filename and
        reopening the source PDF once per page, from inside the destination.

        The source type decides how a page is labelled — a PDF reads its own
        page labels and outline, a notebook has neither — so this asks the
        renderer rather than checking a file extension. The bundle it builds
        carries no zip: the pages are already rendered and may have been
        rendered on an earlier run, and labelling reads the source document,
        never the zip.

        Args:
            job: The job whose images are settled; sets ``pages``.
        """
        from living_ink.sources import PageRef, SourceBundle, source_for_name

        source = source_for_name(job.doc_type)
        numbers = [job.page_number(i) for i in range(len(job.imgs))]
        refs = [
            PageRef(
                ordinal=index,
                number=number,
                # The digest the render cache keyed this page under, when the
                # page was rendered this run. A page reused from disk has none,
                # which is why this is a key and not an identity.
                source_key=(job.source_hashes[index] if index < len(job.source_hashes) else ""),
            )
            for index, number in enumerate(numbers)
        ]
        bundle = SourceBundle(
            doc_id=str(job.notebook_id),
            title=job.notebook,
            zip_path=None,
            source_path=job.doc_file_path,
        )
        descriptions = source.renderer.describe_pages(bundle, refs)

        job.pages = [
            Page(
                index=ref.ordinal,
                number=ref.number,
                label=description.label,
                breadcrumbs=description.breadcrumbs,
                image_path=image,
                source_key=ref.source_key,
            )
            for image, ref, description in zip(job.imgs, refs, descriptions)
        ]

    def _rendered_pages(self, job: DocumentJob) -> List[Path]:
        """List the page images already rendered for this job, in page order."""
        return job.workspace.rendered_pages()

    def _render_context(self) -> "RenderContext":
        """Build the render settings this run uses, once.

        Returns:
            A :class:`~living_ink.sources.RenderContext`. The background colour
            is in here and is passed to the renderer — it used to be folded
            into the cache key and then dropped on the way to the render call,
            so changing it invalidated every cached page and produced
            byte-identical images.
        """
        from living_ink.sources import RenderContext

        return RenderContext(
            device=self.device.info,
            background=self.settings.render_background,
            keep_temp=self.keep_temp,
        )

    def _render_document(self, job: DocumentJob, client: Any) -> None:
        """Download the document zip and render it with its source's renderer.

        Args:
            job: The job being rendered.
            client: reMarkable API client.

        Raises:
            _StopProcessing: If the zip cannot be downloaded, or the document
                holds nothing the renderer can produce.
        """
        from living_ink.extract import extract_tags_from_zip
        from living_ink.sources import SourceBundle, source_for_name

        tmp_zip = job.workspace.ensure().download
        raw_bytes = client.download(job.item)
        if not raw_bytes:
            raise _StopProcessing(False, f"Failed to download document zip for {job.notebook}.")
        tmp_zip.write_bytes(raw_bytes)

        try:
            job.tags.extend(extract_tags_from_zip(tmp_zip) or [])
            self._render_source(
                job,
                source_for_name(job.doc_type),
                SourceBundle(
                    doc_id=str(job.notebook_id),
                    title=job.notebook,
                    zip_path=tmp_zip,
                    source_path=job.doc_file_path,
                    item=job.item,
                    client=client,
                ),
            )
        finally:
            # The .rm source zip is exactly what a render bug needs, and
            # CLAUDE.md tells people to debug rendering with --keep-temp. It
            # used to be unlinked either way.
            if self.keep_temp:
                log(f"Keeping {tmp_zip} (--keep-temp).")
            else:
                tmp_zip.unlink(missing_ok=True)

    def _render_source(
        self, job: DocumentJob, source: "SourceType", bundle: "SourceBundle"
    ) -> None:
        """Run one source's renderer over one downloaded document.

        The sequence is the :class:`~living_ink.sources.Renderer` contract in
        order: prepare, enumerate, read the text layer, render. It is the same
        five calls for every source, which is the point — the differences
        between a notebook, a PDF and an EPUB live in the renderer, not here.

        Args:
            job: The job being rendered; sets ``extracted_doc_text``,
                ``source_hashes`` and the saved page images.
            source: The registered source handling this document.
            bundle: The document's files on disk.

        Raises:
            _StopProcessing: If the document yields neither pages nor text.
        """
        ctx = self._render_context()
        renderer = source.renderer

        if not renderer.prepare(bundle, ctx):
            self._nothing_to_render(job, source)

        refs = list(renderer.pages(bundle, ctx))
        job.extracted_doc_text = renderer.text_layer(bundle, ctx) or ""

        # Not `not refs`: an unannotated PDF and an EPUB with no annotations
        # both render zero pages and publish their text layer instead. Only a
        # document with neither has nothing to say.
        if not refs and not job.extracted_doc_text:
            self._nothing_to_render(job, source)

        if refs:
            log(f"Rendering {len(refs)} page(s) for {source.label} '{job.notebook}'...")
            self._render_pages(job, source, bundle, refs, ctx)

    def _nothing_to_render(self, job: DocumentJob, source: "SourceType") -> None:
        """Stop processing a document that produced neither pages nor text.

        Whether that is a skip or a failure is the source's declaration, not a
        guess made here: an empty notebook is a user who has not written
        anything yet, while a PDF that yielded nothing was supposed to have
        content and did not.

        Raises:
            _StopProcessing: Always.
        """
        if source.empty_is_skip:
            raise _StopProcessing(
                True, f"{source.label} '{job.notebook}' has 0 pages (empty). Skipping."
            )
        raise _StopProcessing(
            False, f"No pages or text could be extracted for '{job.notebook}'. Skipping."
        )

    def _render_pages(
        self,
        job: DocumentJob,
        source: "SourceType",
        bundle: "SourceBundle",
        refs: List["PageRef"],
        ctx: "RenderContext",
    ) -> None:
        """Render every page of a document, reusing what has not changed.

        Turning one page into a PNG goes through rmc, an SVG, and PyMuPDF, and
        it is the slowest local step in a sync. It is also pure: the same
        source rendered by the same code gives the same image. So a page whose
        source digest and render settings match one already rendered is served
        from the cache and never re-rendered. **Every source is cached this
        way** — the PDF composite path used to render uncached on every run.

        Args:
            job: The job being rendered.
            source: The registered source handling this document.
            bundle: The document's files on disk.
            refs: The pages to render, in publication order.
            ctx: The run's render settings.
        """
        from living_ink.extract import RenderError, renderer_fingerprint

        # The renderer's own version and the source's name are in the key
        # because a global format number cannot say which of three renderers
        # changed. The background and the panel size are chosen per run rather
        # than baked into the build, so they are in the key too: plugging in a
        # different tablet changes the size a boundless page renders at, and a
        # cache that ignored it would serve the other device's geometry.
        fingerprint = (
            f"{renderer_fingerprint()}:{source.name}:v{source.renderer.version}:{ctx.fingerprint()}"
            if self.renders.enabled
            else ""
        )

        reused = 0
        for ref in refs:
            key = (
                self.renders.key(ref.source_key.encode("utf-8"), fingerprint)
                if fingerprint and ref.source_key
                else None
            )
            png_bytes = self.renders.get(key) if key else None

            if png_bytes is None:
                try:
                    png_bytes = source.renderer.render(bundle, ref, ctx)
                except RenderError as e:
                    # Named rather than counted: a page that renders to nothing
                    # used to publish as an empty note with no error anywhere.
                    job.failed_pages += 1
                    log(f"Failed to render page {ref.number} of {job.notebook}: {e}")
                    if self.report:
                        self.report.warn(f"{job.notebook} page {ref.number}: {e}")
                    continue
                if png_bytes is None:
                    # A counted failure, not a skip. The PDF path used to drop
                    # a page it could not composite and never say so.
                    job.failed_pages += 1
                    log(f"Failed to render page {ref.number} of {job.notebook}.")
                    continue
                if key:
                    self.renders.put(key, png_bytes)
            else:
                reused += 1

            # Appended per saved page rather than assigned up front, so the
            # digests line up with the images even when a page fails.
            job.source_hashes.append(ref.source_key)
            self._save_page(job, ref.number, png_bytes)

        if reused:
            log(f"{reused} of {len(refs)} pages were already rendered; reused as-is.")

    def _save_page(self, job: DocumentJob, page: int, data: bytes, label: str = "Saved") -> None:
        """Write one rendered page image into the white-background directory."""
        out_img = job.workspace.page_image(page)
        out_img.write_bytes(data)
        log(f"{label}: {out_img}")

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
        """Prepare each page image for OCR, writing the results beside them."""
        pre_dir = job.workspace.preprocessed_dir
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
        """Transcribe every prepared page and write the results onto the pages.

        There is one way to read a page: a single multimodal call that reads
        and cleans in one step.

        A page that raised is recorded on that page and nowhere else. Three
        pages out of two hundred failing is not a failed notebook: the other
        197 publish, the three are marked, and the transcript cache makes the
        retry cost three API calls rather than two hundred.
        """
        before = self._cache_hits
        results = self._transcribe_pages(job.pre_paths)
        reused = self._cache_hits - before
        if reused:
            log(f"{reused} of {len(job.pre_paths)} pages came from the cache; no API call made.")

        job.transcribed_pages = len(job.pre_paths) - reused
        job.cached_pages = reused

        job.pages = [
            replace(page, text=text, error=error) for page, (text, error) in zip(job.pages, results)
        ]

        failed = [page for page in job.pages if page.error]
        job.failed_pages += len(failed)
        for page in failed:
            log(f"⚠️ {job.notebook} {page.label}: {page.error}")
            if self.report:
                self.report.warn(f"{job.notebook} {page.label}: {page.error}")

    def _transcribe_pages(self, paths: List[Path]) -> List[Tuple[str, Optional[str]]]:
        """Transcribe pages, several at a time, and return them in page order.

        A page is one network round trip and nothing else, so running a few
        concurrently is most of the wall-clock win available in a sync. The
        ceiling is the AI provider's rate limit, which is why the width is the
        configurable ``ocr_concurrency`` rather than the page count.

        Args:
            paths: Prepared page images, in page order.

        Returns:
            One ``(text, error)`` pair per page, in the order given.
        """
        width = min(self.settings.ocr_concurrency, len(paths))
        if width <= 1:
            return [self._transcribe_page(p) for p in paths]

        log(f"Transcribing {len(paths)} pages, {width} at a time...")
        with ThreadPoolExecutor(max_workers=width) as pool:
            # ``map`` yields in submission order, so pages stay in page order
            # however the calls happen to finish.
            return list(pool.map(self._transcribe_page, paths))

    def _transcribe_page(self, path: Path) -> Tuple[str, Optional[str]]:
        """Transcribe one page, from the cache when it is there.

        Args:
            path: The prepared page image.

        Returns:
            ``(text, error)``. A blank page is ``("", None)`` and a failed one
            is ``("", "<reason>")`` — the two are indistinguishable by text
            alone, which is the whole reason the second element exists. An
            error here costs one page, never the document: the caller marks a
            gap and publishes the rest.
        """
        key = self._cache_key(path)
        if key:
            cached = self.cache.get(key)
            if cached is not None:
                with self._cache_lock:
                    self._cache_hits += 1
                log(f"  Cached: {path.name}")
                return cached, None
            with self._cache_lock:
                self._cache_misses += 1

        try:
            return self._cached(key, self._vision_ocr_page(path)), None
        except Exception as e:
            # Deliberately broad, and it does not swallow: the reason is put on
            # the page, warned about, and counted. An unreadable image or a
            # provider that finally gave up used to propagate out of the thread
            # pool and fail every other page in the notebook with it.
            return "", redact(f"{type(e).__name__}: {e}")

    def _cache_key(self, path: Path) -> Optional[str]:
        """Return the cache key for one page, or None if it cannot be computed.

        The key covers the page image and the model and prompts behind it, so
        editing a prompt or switching provider correctly misses.

        Args:
            path: The prepared page image.

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
        return self.cache.key(image_bytes, transcription_fingerprint(self.settings))

    def _cached(self, key: Optional[str], text: str) -> str:
        """Store a freshly transcribed page and return it unchanged.

        An empty result is not stored. A page that read as nothing is usually a
        provider hiccup or a rate limit rather than a blank page, and caching
        it would make one bad minute permanent.

        Args:
            key: The cache key, or None if this page is not cacheable.
            text: The page's text.

        Returns:
            The text it was given.
        """
        if key and text.strip():
            self.cache.put(key, text)
        return text

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
        return ""

    # ── Stage 6: transcripts ─────────────────────────────────────────────

    def _write_transcripts(self, job: DocumentJob) -> None:
        """Write the transcript to the output directory.

        One file, because there is one transcription: the model reads and
        cleans the page in the same call, so there is no earlier, rawer text
        for a second file to hold.
        """
        meta = {"notebook": job.notebook, "images": [p.name for p in job.imgs]}

        self._write_transcript(job, job.workspace.transcript, meta)
        log(f"Cleaned OCR text saved to {job.workspace.transcript}")

    def _write_transcript(self, job: DocumentJob, path: Path, meta: Dict[str, Any]) -> None:
        """Write one transcript: a metadata line, then a section per page.

        A page that produced no text keeps its header and gets no body, so the
        page numbering still lines up with the notebook, and a page that failed
        says so where the missing text would have been.

        Written for a person to read. Nothing in the pipeline reads it back.

        Args:
            job: The job being transcribed.
            path: File to write.
            meta: Metadata dict, written as the first line.
        """
        from living_ink.extract import format_page_section_header

        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(meta) + "\n\n")

            if not job.pages and job.extracted_doc_text:
                f.write(job.extracted_doc_text + "\n")
                return

            for page in job.pages:
                header = format_page_section_header(
                    page.number,
                    job.doc_file_path,
                    include_divider=True,
                    # Already read once, when the page was rendered. Letting the
                    # header re-read them reopens the PDF once per page.
                    label=page.label,
                    breadcrumbs=page.breadcrumbs,
                )
                body = page.error if page.error else page.text.strip()
                f.write(f"{header}\n\n{body}\n\n" if body else f"{header}\n\n")

            if job.extracted_doc_text and not any(p.text.strip() for p in job.pages):
                f.write(job.extracted_doc_text + "\n")

    # ── Stage 7: publish ─────────────────────────────────────────────────

    def _build_document(self, job: DocumentJob) -> Document:
        """Turn a finished job into the destination-neutral document.

        This is the adapter, and it is temporary: the stages will build the
        document themselves once they are moved out of this class, and then
        ``DocumentJob`` stops being the thing that travels between them. Until
        that lands, one place converts and every destination sees the contract
        it will keep.

        Args:
            job: The job, transcribed and ready to publish.

        Returns:
            The document, carrying nothing about where it is going.
        """
        return Document(
            doc_id=job.notebook_id,
            # The title alone. It used to be glued to the folder path with
            # " / " and split apart again inside the destination, which filed a
            # notebook actually called "Q1 / Q2" in a folder named "Q1".
            title=job.notebook,
            folder_path=job.folder_parts(),
            source=job.doc_type,
            modified=job.modified_at(),
            tags=tuple(job.tags),
            pages=tuple(job.pages),
            body_text=job.extracted_doc_text or None,
            source_file=job.source_file(),
        )

    def _publish_context(self, dest: Destination, doc_id: str) -> PublishContext:
        """Look up what one destination did with this document last time.

        Keyed by ``state_key``, never by the display name: the row was written
        under the former, and a lookup under the latter silently finds nothing,
        which reads as "never published" and duplicates the note.

        Args:
            dest: The destination about to be asked to publish.
            doc_id: reMarkable document id.

        Returns:
            The context to hand to :meth:`Destination.publish`.
        """
        previous = get_state_store().get_publication(doc_id, dest.state_key)
        external_id = previous["external_id"] if previous else None
        return PublishContext(
            doc_id=doc_id,
            dry_run=self.dry_run,
            existing_external_id=external_id,
            # Where it landed last time. A notebook renamed or moved on the
            # tablet is the same note in a new place, not a second note.
            existing_target=previous["target"] if previous else None,
            # Only when we already know we published here before: then the note
            # carrying this title is one we created.
            adopt_by_name=bool(previous) and not external_id,
            # When the note first appeared here, for a note that has to state
            # when it came into existence and predates the frontmatter saying so.
            first_published=to_datetime(previous["first_published_at"]) if previous else None,
            settings=self.settings,
        )

    def _publish(self, job: DocumentJob, targets: Sequence[Destination]) -> bool:
        """Publish the transcribed document to every destination that wants it.

        The targets are passed in, never looked up and never defaulted. They
        used to be read out of a dict with ``or``, which cannot tell "no entry
        for this document" from "computed as pending at zero destinations" —
        so a document that needed nobody was published to everybody.

        Args:
            job: The processed job.
            targets: The destinations that are behind on this document, as
                decided by the selection pass.

        Returns:
            True if every targeted destination accepted the note.
        """
        try:
            targets = list(targets)
            if not targets:
                log("No destinations need update for this notebook (or none configured).")
                return True

            doc = self._build_document(job)

            if self.dry_run:
                self._report_dry_run(job, doc, targets)
                return True

            all_success = True
            for dest in targets:
                result = self._publish_to(dest, doc)
                self._report_destination_warnings(dest, result)
                if result.ok:
                    job.published_to.append(dest.state_key)
                    if result.detail:
                        log(f"   {result.detail}")
                    # Update state for THIS destination immediately, and after
                    # the note is on disk: a row recorded first would claim a
                    # note a crash never wrote, and that document is never
                    # retried. The other order costs one redundant republish.
                    add_to_processed_log(
                        dest.state_key,
                        job.notebook_id,
                        job.version,
                        recipe=self._recipe_for(job, dest),
                        run_id=self.run_id,
                        external_id=result.external_id,
                        target=result.target,
                    )
                else:
                    all_success = False
                    # The reason rides on the result, not only on an exception:
                    # a destination that reports a failure rather than raising
                    # would otherwise lose it here.
                    reason = f": {result.detail}" if result.detail else ""
                    log(f"⚠️ Failed to publish to {dest.display_name}{reason}")

            return all_success
        except Exception as e:
            # Deliberately broad. This is the stage boundary: a notebook that
            # cannot be published is reported as one failed notebook, with the
            # traceback, so the rest of the run still goes out.
            log(f"Failed publishing note: {e}")
            import traceback

            log(traceback.format_exc())
            return False

    def _recipe_for(self, job: DocumentJob, dest: Destination) -> str:
        """Digest the inputs that shaped what this destination was just given.

        Recorded beside the version so the next run can tell that the document
        is unchanged but the way it would be produced is not.

        The digest the selection pass computed is preferred over a fresh one:
        it is the digest the decision was made against, so recording it is what
        keeps a forced run from leaving the next ordinary run with a mismatch.

        Args:
            job: The processed job, for the recipes it carries and its source type.
            dest: The destination the note went to.

        Returns:
            The recipe digest. An unrecognised ``doc_type`` resolves to the
            fallback source, which is the same source that rendered the pages,
            so the digest still describes what actually happened.
        """
        recipe = job.recipes.get(dest.state_key)
        if recipe:
            return recipe

        from living_ink.core.recipe import document_recipe
        from living_ink.sources import source_for_name

        return document_recipe(source_for_name(job.doc_type), dest, self.settings)

    def _report_dry_run(self, job: DocumentJob, doc: Document, targets: List[Destination]) -> None:
        """Say what a real run would have published, and where to read it.

        Nothing is sent and no processed-log entry is written, so the same
        notebook is still pending afterwards and a later real run picks it up.

        Each destination also says how much of an existing note it would
        rewrite. "The whole note is replaced" is the fact a user needs before
        the run rather than after, and it is read from
        :attr:`Destination.merge_unit` rather than inferred from a class name.

        Args:
            job: The processed job, for the artifacts it left on disk.
            doc: What would be published.
            targets: The destinations a real run would have published to.
        """
        log(f"🔍 Dry run — not publishing '{job.display_title}'.")
        job.would_publish_to = [dest.state_key for dest in targets]
        # The document's folder, not the one a particular destination would
        # nest it in: how deep to nest is the destination's own decision now.
        where = f" under '{'/'.join(doc.folder_path)}'" if doc.folder_path else ""
        for dest in targets:
            log(f"   Would publish to {dest.describe()}{where}")
            if dest.merge_unit is MergeUnit.DOCUMENT:
                log("      Replaces the whole note, including anything you added to it.")
            else:
                log("      Replaces only the pages that changed; your own text is kept.")
        log(f"   Transcript: {job.workspace.transcript}")
        if job.imgs:
            log(f"   {len(job.imgs)} page image(s) in {job.workspace.pages_dir}")
        if job.tags:
            log(f"   Tags: {job.tags}")

    def _report_destination_warnings(self, dest: Destination, result: PublishResult) -> None:
        """Put a destination's warnings where the log lines cannot scroll past them.

        A destination reports what the user has to fix by hand — a note it
        stepped around, an attachment it could not copy — and the run summary
        is the only place that survives a long run.

        Args:
            dest: The destination that produced the result.
            result: What it returned.
        """
        if self.report is None:
            return
        for warning in result.warnings:
            self.report.warn(f"{dest.display_name}: {warning}")

    def _publish_to(self, dest: Destination, doc: Document) -> PublishResult:
        """Publish one document to one destination.

        A DestinationError is an expected, user-actionable failure (vault gone,
        Notes not responding): report it plainly and let the caller carry on to
        the next destination. Anything else is a bug, and is logged with a
        traceback so it is distinguishable.

        Args:
            dest: The destination to publish to.
            doc: The document to publish.

        Returns:
            What the destination reported, or a refusal carrying the reason it
            could not be asked.
        """
        dest_name = dest.display_name
        log(f"Publishing to {dest_name}...")

        try:
            published = dest.publish(doc, self._publish_context(dest, doc.doc_id))
        except DestinationError as e:
            log(f"⚠️ {dest_name}: {e}")
            return PublishResult(ok=False, detail=str(e))
        except Exception:
            # Deliberately broad, and the message says so: a destination is
            # contracted to raise DestinationError, so anything else reaching
            # here is a defect in that destination. Print it loudly and keep
            # publishing to the others.
            import traceback

            log(f"❌ Unexpected error publishing to {dest_name} — this is a bug:")
            log(traceback.format_exc())
            return PublishResult(ok=False, detail="Unexpected error; see the log.")

        return published

    def _handle_orphans(self, orphans: Sequence[str], id_map: Dict[str, Any]) -> None:
        """Report, and optionally delete, notes whose notebook is gone.

        A document that was published once and is no longer in the tablet's
        listing has usually been deleted there — but it can also mean the
        listing came back short, and acting on that would destroy notes for a
        transport hiccup. So the default is to say so and do nothing;
        ``--prune`` is the user taking responsibility for the difference.

        Which documents those are is decided by the selection pass, against the
        whole listing rather than the sync candidates, so a notebook in the
        trash or of a type this run skipped is not mistaken for a deletion.

        Args:
            orphans: Ids with publications that the tablet no longer lists.
            id_map: Every document the tablet listed, keyed by id.
        """
        if not id_map or self.dry_run or not orphans:
            # An empty listing means the transport told us nothing, which is
            # not the same as the tablet being empty.
            return

        try:
            publications = get_state_store().all_publications()
        except (sqlite3.Error, OSError, RuntimeError) as e:
            log(f"⚠️ Could not check for deleted notebooks: {e}")
            return

        rows_by_id = {doc_id: publications[doc_id] for doc_id in orphans if doc_id in publications}
        if not rows_by_id:
            return

        if not self.prune:
            log(f"{len(rows_by_id)} published notebook(s) are no longer on the tablet:")
            for doc_id, rows in rows_by_id.items():
                where = ", ".join(sorted(rows))
                log(f"  {self._orphan_label(doc_id)} — still in {where}")
            log("Their notes were left alone. Run with --prune to delete them.")
            return

        for doc_id, rows in rows_by_id.items():
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
            rows: Its publication rows, keyed by destination state key.
        """
        by_name = {d.state_key: d for d in (self.destinations or get_default_destinations())}
        label = self._orphan_label(doc_id)

        for dest_name, row in rows.items():
            dest = by_name.get(dest_name)
            if dest is None:
                log(f"  {label}: {dest_name} is not configured; its note was left alone.")
                continue
            try:
                result = dest.unpublish(
                    PublishContext(
                        doc_id=doc_id,
                        existing_external_id=row.get("external_id"),
                        existing_target=row.get("target"),
                        settings=self.settings,
                    )
                )
            except DestinationError as e:
                log(f"  ⚠️ {dest.display_name}: {e}")
                continue
            self._report_destination_warnings(dest, result)
            verb = "deleted from" if result.ok else "left alone in"
            log(f"  {label}: {verb} {dest.display_name}.")

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
            if outcome == "success":
                self._prune_caches()
            return result
        except KeyboardInterrupt:
            # Ctrl+C is not a failure, and the run did not do nothing. Record
            # what it got through, then let the interrupt carry on out.
            seen, published, failed = self._counts
            outcome = "interrupted"
            self._report_interrupt(published)
            raise
        finally:
            self._signal_outcome(outcome)
            if self.run_id is not None:
                store.finish_run(
                    self.run_id,
                    outcome=outcome,
                    seen=seen,
                    published=published,
                    failed=failed,
                )

    def _prune_caches(self) -> None:
        """Evict cache entries no run has wanted for ``cache.max_age_days``.

        Last, and only after a run that succeeded. A prune that ran first would
        be deleting exactly the entries the run about to happen is about to
        ask for: a notebook synced quarterly would find its own transcripts
        evicted moments before it needed them, and every cached page would
        turn back into an API call. Running last means an entry is only ever
        dropped after a run that did not want it. A failed or interrupted run
        prunes nothing, because it does not know what it would have used.

        There is no manual prune command, so this is the only caller — the
        placement is the feature, not a convenience on top of one.
        """
        if self.dry_run:
            return
        for cache in (self.cache, self.renders):
            try:
                removed = cache.prune()
            except OSError as e:
                log(f"Could not prune the {cache.noun} cache: {e}")
                continue
            if removed:
                log(f"Pruned {removed} unused {cache.noun} cache entries.")

    def _signal_outcome(self, outcome: str) -> None:
        """Tell the destinations themselves how the run ended.

        A sync that stops working is usually discovered weeks later, by
        noticing a notebook never arrived — the terminal it failed in was not
        being watched, and a log file is not somewhere anybody looks. The
        destination is, so a failed run leaves a note there and a successful
        one takes it away again.

        An interrupted run says nothing: Ctrl+C is the user's own decision and
        does not need reporting back to them. Neither does a dry run, which is
        contracted to change nothing.

        Args:
            outcome: The run's recorded outcome — ``success``, ``partial``,
                ``error`` or ``interrupted``.
        """
        if self.dry_run or outcome == "interrupted":
            return

        for dest in self.destinations:
            try:
                if outcome == "success":
                    dest.clear_failure()
                else:
                    dest.report_failure(self._failure_summary(outcome))
            except Exception as e:
                # Reporting a failure must never become a second one.
                log(f"Could not report the run outcome to {dest.display_name}: {e}")

    def _failure_summary(self, outcome: str) -> str:
        """Phrase what went wrong for a reader who was not at the terminal.

        Args:
            outcome: The run's recorded outcome.

        Returns:
            A short paragraph naming the counts and the run's warnings.
        """
        seen, published, failed = self._counts
        if outcome == "partial":
            headline = f"{published} of {seen} notebook(s) synced; {failed} failed."
        else:
            headline = f"The sync stopped before it finished. {published} of {seen} published."

        lines = [headline]
        if self.report and self.report.warnings:
            lines.append("")
            lines.extend(f"- {warning}" for warning in self.report.warnings)
        lines.append("")
        lines.append(f"Run `living-ink status` for details, or see the log at {LOG_PATH}.")
        return "\n".join(lines)

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

        cleanup_temp_artifacts(keep_temp=self.keep_temp)
        register_temp_cleanup(keep_temp=self.keep_temp)

        client = self.connect()
        self.preflight_destinations()
        self._learn_device(client)

        listing = list(client.get_meta_items())
        id_map = {get_val(item, "ID"): item for item in listing}
        chosen = self.select_documents(listing, client)
        if chosen is None:
            return False

        self._counts = (chosen.considered, 0, 0)
        self._handle_orphans(chosen.orphans, id_map)
        self._report_selection(chosen)

        if not chosen.to_process:
            if not self.target_notebook:
                log("No new or updated notebooks found for any active destination. Exiting.")
            self._print_summary()
            return True

        all_success = True
        published = failed = 0
        for candidate in chosen.to_process:
            item_success = self._process_one(candidate, client)
            if item_success:
                published += 1
            else:
                failed += 1
                all_success = False
            # Updated per notebook, not once at the end: a run that is
            # interrupted half way through still did the work it did, and the
            # run record has to say so.
            self._counts = (chosen.considered, published, failed)

        log("Pipeline finished.")
        cleanup_temp_artifacts(keep_temp=self.keep_temp)
        self._print_summary()
        return all_success

    def _report_selection(self, chosen: Selection) -> None:
        """Record the documents this run will not touch, and why.

        A summary that lists only what was synced cannot answer "why was my
        notebook not picked up", which is the question a user actually has —
        and it has to answer it *truthfully*. Everything here used to be
        reported as ``unchanged``, including documents that were pending and
        fell outside ``--limit``: with the default limit of one, nine pending
        notebooks were reported as up to date.

        Args:
            chosen: What this run decided to do, and what it left out.
        """
        if self.report is None:
            return

        for item, reason in chosen.skipped:
            if reason == NOT_TARGETED:
                # A targeted run never considered the rest of the library, so
                # listing all of it would be a claim it never checked — and on
                # a real tablet it would bury the one document asked for.
                continue
            self.report.add(
                DocumentOutcome(
                    name=self._selection_label(item),
                    doc_id=getattr(item, "doc_id", None) or get_val(item, "ID"),
                    status=SKIPPED,
                    reason=reason,
                )
            )

        for candidate in chosen.deferred:
            self.report.add(
                DocumentOutcome(
                    name=candidate.name or candidate.doc_id,
                    doc_id=candidate.doc_id,
                    status=DEFERRED,
                    destinations=[dest.state_key for dest in candidate.pending],
                )
            )

    @staticmethod
    def _selection_label(item: Any) -> str:
        """Name a skipped document, whether it got as far as being classified.

        Args:
            item: A :class:`~living_ink.core.selection.Candidate` for anything
                the classifier reached, or the transport's raw item for
                anything ruled out before that.

        Returns:
            Something to print in the summary's name column.
        """
        if isinstance(item, Candidate):
            return item.name or item.doc_id
        return str(
            get_val(item, "VissibleName")
            or get_val(item, "VisibleName")
            or get_val(item, "ID")
            or "(unnamed)"
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
