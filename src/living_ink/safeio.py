"""Crash-safe and permission-aware file writes.

Two hazards are handled here so that no caller has to remember them:

**Torn writes.** Truncating a file and then writing it leaves a window in which
the file on disk is neither the old content nor the new one. A crash, a
``SIGINT``, or a full disk inside that window destroys the file. Every write in
this module goes to a sibling temporary file which is flushed, ``fsync``-ed, and
only then renamed into place, so a reader sees either the old file or the new
one and never something in between.

**Over-permissive secrets.** Files holding an API key or a device token must not
be readable by other accounts on the machine. Temporary files are created at
``0600`` before any byte is written, so a secret is never briefly world-readable
even when the final mode is wider.
"""

import os
import stat
from pathlib import Path
from typing import Union

#: Owner read/write only — the mode for any file holding a credential.
SECRET_MODE = 0o600

#: Owner read/write, everyone else read — the mode for ordinary output.
PUBLIC_MODE = 0o644

#: Owner-only directory, for the directory a secret lives in.
SECRET_DIR_MODE = 0o700

#: The permission bits that must never be set on a file holding a secret.
_OTHER_BITS = 0o077

PathLike = Union[str, Path]


def _sync_directory(directory: Path) -> None:
    """Flush a directory entry so a completed rename survives a power loss.

    The rename itself is atomic, but the directory entry recording it may still
    be buffered. Not every filesystem supports this, so failure is ignored
    rather than propagated: the write already succeeded.

    Args:
        directory: Directory whose entries should be flushed.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_text_atomic(
    path: PathLike,
    text: str,
    *,
    mode: int = PUBLIC_MODE,
    encoding: str = "utf-8",
) -> Path:
    """Write text to a path so that readers never observe a partial file.

    The content goes to a temporary sibling first, because :func:`os.replace` is
    only atomic within a single filesystem. Missing parent directories are
    created.

    Args:
        path: Destination file. Overwritten if it exists.
        text: Content to write.
        mode: Permission bits for the finished file. Defaults to
            :data:`PUBLIC_MODE`; pass :data:`SECRET_MODE` for credentials, or
            use :func:`write_secret_atomic`.
        encoding: Text encoding.

    Returns:
        The destination path, for convenience when chaining.

    Raises:
        OSError: If the file cannot be written or renamed. The destination is
            left untouched and the temporary file is removed.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")

    try:
        # Create restricted, widen afterwards: a secret must never exist on
        # disk at a mode broader than its final one, not even momentarily.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, SECRET_MODE)
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        # Also catches KeyboardInterrupt, which is the interruption this
        # function exists to survive; the exception is always re-raised.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise

    _sync_directory(path.parent)
    return path


def write_secret_atomic(path: PathLike, text: str, *, encoding: str = "utf-8") -> Path:
    """Write a file containing a credential, readable only by its owner.

    Also tightens the containing directory to :data:`SECRET_DIR_MODE`, since a
    ``0600`` file inside a world-writable directory can still be replaced.

    Args:
        path: Destination file.
        text: Content to write.
        encoding: Text encoding.

    Returns:
        The destination path.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(SECRET_DIR_MODE)
    except OSError:
        # A directory we do not own; the file mode below is still applied.
        pass
    return write_text_atomic(path, text, mode=SECRET_MODE, encoding=encoding)


def restrict_permissions(path: PathLike) -> bool:
    """Remove group and other access from an existing file.

    Used to repair a config file written before Living Ink set permissions, or
    loosened by hand. Owner bits are preserved.

    Args:
        path: File to tighten. A missing file is not an error.

    Returns:
        True if the permissions were changed, False if they were already
        owner-only or the file does not exist.
    """
    path = Path(path)
    try:
        current = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return False

    if not current & _OTHER_BITS:
        return False

    try:
        path.chmod(current & ~_OTHER_BITS)
    except OSError:
        return False
    return True
