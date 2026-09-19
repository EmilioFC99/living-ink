"""Stored credentials — one secret per file, owner-readable only.

Secrets do not live in ``config.yml``. A config file is something a user
pastes into an issue, copies between machines, checks into a dotfiles repo and
reads over someone's shoulder; a device token and an API key are none of those
things. They live in a sibling directory instead, one file per credential,
written atomically at mode ``0600``.

The namespace is flat and the *name carries the provider*:

=============================  ==================================
Credential                     Name
=============================  ==================================
reMarkable Cloud device token  ``remarkable.cloud_token``
SSH password                   ``remarkable.ssh_password``
AI API key                     ``ai.api_key.<provider>``
=============================  ==================================

The last one is a product promise rather than an optimisation: a user who
tries OpenAI for an afternoon and goes back to Gemini must not find their
Gemini key gone. One slot per provider is what makes switching back free, and
:func:`ai_key_name` is the only place that composes the name, so no call site
can invent a second spelling.

Every value read or written here is registered with :mod:`living_ink.redact`,
so a secret that reaches a log line is masked even when the code that logged
it never knew it was holding one.
"""

import logging
import re
import stat
from pathlib import Path
from typing import List, Optional

from living_ink.config.paths import credentials_dir
from living_ink.redact import register_secret
from living_ink.safeio import write_secret_atomic

logger = logging.getLogger(__name__)

#: The reMarkable Cloud device token, as written by pairing.
CLOUD_TOKEN = "remarkable.cloud_token"

#: The password for USB SSH, when the tablet has one set.
SSH_PASSWORD = "remarkable.ssh_password"

#: Prefix for the per-provider AI API keys. Never used as a name on its own.
AI_KEY_PREFIX = "ai.api_key"

#: A credential name is also its filename, so it is restricted rather than
#: escaped: lowercase words joined by dots, dashes or underscores. That rejects
#: ``..``, ``/`` and every other way a provider name arriving from a config
#: file could climb out of the credentials directory. Anchored with ``\Z``
#: rather than ``$``, which would accept a trailing newline and quietly store
#: the secret in a file no clean lookup of the same name would ever find.
_VALID_NAME = re.compile(r"\A[a-z0-9]+(?:[._-][a-z0-9]+)*\Z")

#: How much of a secret stays visible when one is displayed.
_VISIBLE_EDGE = 4

#: Fixed-width middle, so the mask does not leak the length of the secret.
_MASK_BODY = "•" * 8


def ai_key_name(provider: str) -> str:
    """Return the credential name holding the API key for one provider.

    Args:
        provider: Provider identifier, e.g. ``"gemini"``. Case and surrounding
            whitespace are normalised, because the same provider reaches this
            function from a config file, a flag and a wizard prompt.

    Returns:
        The credential name, e.g. ``"ai.api_key.gemini"``.

    Raises:
        ValueError: If the provider is empty or not a usable name.
    """
    slug = str(provider).strip().lower()
    if not slug:
        raise ValueError("provider must be named to locate its API key")
    name = f"{AI_KEY_PREFIX}.{slug}"
    _check_name(name)
    return name


def _check_name(name: str) -> None:
    """Reject a credential name that cannot safely become a filename.

    Args:
        name: The credential name.

    Raises:
        ValueError: If the name is empty or contains anything outside
            :data:`_VALID_NAME`.
    """
    if not _VALID_NAME.match(name or ""):
        raise ValueError(f"not a valid credential name: {name!r}")


def _path_for(name: str, config_path: Optional[Path]) -> Path:
    """Resolve the file one credential is stored in.

    Args:
        name: The credential name.
        config_path: The resolved ``config.yml`` path, or None.

    Returns:
        The file path, which may not exist.

    Raises:
        ValueError: If the name is not a valid credential name.
    """
    _check_name(name)
    return credentials_dir(config_path) / name


def read_secret(name: str, *, config_path: Optional[Path] = None) -> Optional[str]:
    """Read one credential.

    Args:
        name: The credential name, e.g. :data:`CLOUD_TOKEN`.
        config_path: The resolved ``config.yml`` path, or None to resolve it.

    Returns:
        The stored value, or None when the credential is absent, empty or
        unreadable. Absent and unreadable are deliberately the same answer: a
        caller can only respond to "there is no usable credential here", and
        the distinction is in the debug log for whoever is diagnosing it.

    Raises:
        ValueError: If the name is not a valid credential name.
    """
    path = _path_for(name, config_path)
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as e:
        logger.debug("Could not read credential %s: %s", name, e, exc_info=True)
        return None

    if not value:
        return None
    register_secret(value)
    return value


def write_secret(name: str, value: str, *, config_path: Optional[Path] = None) -> Path:
    """Store one credential atomically, readable only by its owner.

    Args:
        name: The credential name, e.g. :data:`CLOUD_TOKEN`.
        value: The secret. Surrounding whitespace is stripped, because a key
            pasted from a browser usually arrives with a trailing newline and a
            key that differs from the one the user copied is unfalsifiable from
            their side.
        config_path: The resolved ``config.yml`` path, or None to resolve it.

    Returns:
        The path written.

    Raises:
        ValueError: If the name is not a valid credential name, or the value is
            empty — storing an empty credential produces a file that reads back
            as "absent", which is a delete wearing a write's name.
        OSError: If the file cannot be written.
    """
    cleaned = str(value).strip()
    if not cleaned:
        raise ValueError(f"refusing to store an empty value as {name}")

    path = _path_for(name, config_path)
    written = write_secret_atomic(path, cleaned)
    register_secret(cleaned)
    return written


def delete_secret(name: str, *, config_path: Optional[Path] = None) -> bool:
    """Remove one credential.

    Args:
        name: The credential name.
        config_path: The resolved ``config.yml`` path, or None to resolve it.

    Returns:
        True if a credential was removed, False if there was nothing to remove.

    Raises:
        ValueError: If the name is not a valid credential name.
        OSError: If the file exists but cannot be removed.
    """
    path = _path_for(name, config_path)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def list_secrets(prefix: str = "", *, config_path: Optional[Path] = None) -> List[str]:
    """Name the credentials that are stored, without reading any of them.

    This is what lets a preflight say *"no API key configured for OpenAI —
    configured providers: gemini, groq"* instead of just refusing. It is the
    one place a credential **name** is printed; the value never is.

    Args:
        prefix: Restrict to names starting with this, e.g.
            :data:`AI_KEY_PREFIX`. Empty lists everything.
        config_path: The resolved ``config.yml`` path, or None to resolve it.

    Returns:
        Credential names in sorted order. Empty when the directory does not
        exist. Files whose names are not valid credential names are skipped:
        the directory belongs to the user, and an editor's stray backup file is
        not a credential.
    """
    directory = credentials_dir(config_path)
    try:
        entries = list(directory.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return []
    except OSError as e:
        logger.debug("Could not list credentials in %s: %s", directory, e, exc_info=True)
        return []

    return sorted(
        entry.name
        for entry in entries
        if entry.is_file() and _VALID_NAME.match(entry.name) and entry.name.startswith(prefix)
    )


def configured_ai_providers(*, config_path: Optional[Path] = None) -> List[str]:
    """Name the providers that have an API key stored.

    Args:
        config_path: The resolved ``config.yml`` path, or None to resolve it.

    Returns:
        Provider identifiers in sorted order, e.g. ``["gemini", "openai"]``.
    """
    head = f"{AI_KEY_PREFIX}."
    return [name[len(head) :] for name in list_secrets(head, config_path=config_path)]


def mask(value: Optional[str]) -> str:
    """Render a secret so it can be shown without being disclosed.

    Enough of the edges survive that a user can tell *which* key they are
    looking at — the reason to display one at all — while the middle is a fixed
    width, so the rendering does not leak the length either.

    Args:
        value: The secret, or None.

    Returns:
        ``"AIza••••••••3f2a"`` for a long value, ``"••••••••"`` for a short one
        (where showing the edges would show most of it), and ``"not set"`` for
        an absent or empty one.
    """
    text = (value or "").strip()
    if not text:
        return "not set"
    if len(text) < _VISIBLE_EDGE * 3:
        return _MASK_BODY
    return f"{text[:_VISIBLE_EDGE]}{_MASK_BODY}{text[-_VISIBLE_EDGE:]}"


def insecure_credentials(*, config_path: Optional[Path] = None) -> List[Path]:
    """Find stored credentials that anyone on the machine can read.

    A credential written by this module is ``0600``, but one restored from a
    backup, copied with ``cp``, or written by an older Living Ink may not be.
    Reporting it is the health check's job; this only finds them.

    Args:
        config_path: The resolved ``config.yml`` path, or None to resolve it.

    Returns:
        Paths whose mode grants group or other any access, in sorted order.
    """
    directory = credentials_dir(config_path)
    loose = []
    for name in list_secrets(config_path=config_path):
        path = directory / name
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            continue
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            loose.append(path)
    return loose


def migrate_secret(
    name: str,
    value: Optional[str],
    *,
    config_path: Optional[Path] = None,
) -> bool:
    """Store a credential that arrived from an older location.

    Used for the values that 0.x kept in ``config.yml`` and in ``~/.rmapi``.
    Copying rather than moving is deliberate: this build no longer writes those
    places, but it still reads them, and deleting the original would break a
    rollback to the previous version for a user who has one bad sync.

    Args:
        name: The credential name.
        value: The value found in the old location, or None.
        config_path: The resolved ``config.yml`` path, or None to resolve it.

    Returns:
        True if the credential was newly stored. False when there was nothing
        to store, when one is already stored under that name — the stored one
        wins, because it is the one this build wrote — or when the write failed,
        which is logged rather than raised so that a read-only config directory
        degrades to "keeps reading the old location" instead of a crash.
    """
    cleaned = (value or "").strip()
    if not cleaned:
        return False
    if read_secret(name, config_path=config_path) is not None:
        return False
    try:
        write_secret(name, cleaned, config_path=config_path)
    except (OSError, ValueError) as e:
        logger.debug("Could not migrate credential %s: %s", name, e, exc_info=True)
        return False
    logger.info("Moved %s into the credentials directory.", name)
    return True


def ensure_directory(*, config_path: Optional[Path] = None) -> Path:
    """Create the credentials directory with owner-only permissions.

    Args:
        config_path: The resolved ``config.yml`` path, or None to resolve it.

    Returns:
        The directory path.

    Raises:
        OSError: If the directory cannot be created.
    """
    directory = credentials_dir(config_path)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        # A directory the user owns differently on purpose; every file inside
        # is still written 0600.
        logger.debug("Could not tighten %s", directory, exc_info=True)
    return directory
