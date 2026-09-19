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

**Writes outside the tree they were meant for.** A folder name read off the
tablet, or a root folder read out of ``config.yml``, becomes a path segment. A
segment of ``..`` climbs out of the vault, and the check that used to catch it
ran *after* the file was already written. :func:`contained_path` joins segments
and refuses before anything is created.
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

#: Segments that are a traversal instruction rather than a name. Refused whole
#: rather than sanitized, because there is no sensible sanitized form of "up".
_TRAVERSAL_SEGMENTS = frozenset({"", ".", ".."})


class PathEscapesRoot(ValueError):
    """A path built from untrusted segments would land outside its root.

    A ``ValueError`` and not a destination-specific error on purpose: this
    module is a standard-library-only leaf that :mod:`living_ink.config`
    depends on, so it cannot name an exception defined further up. The caller
    translates it into whatever its own layer reports failures with.
    """


def contained_path(root: PathLike, *segments: str) -> Path:
    """Join segments under a root, or refuse if the result escapes it.

    Every segment is a name, never an instruction. ``.`` and ``..`` are refused
    outright rather than stripped, because a silently sanitized path is one the
    user cannot debug — a notebook that vanishes into a folder they never named
    is worse than one that is reported as unpublishable.

    The result is resolved before it is compared, so a symlinked subfolder
    cannot be used to step out of the tree either.

    Args:
        root: Directory the result must stay inside. Resolved, so it may be
            relative or contain symlinks itself.
        *segments: Path components, innermost last. A segment containing a
            separator is refused: splitting it here would silently create a
            nesting level the caller did not ask for.

    Returns:
        The resolved path, guaranteed to be ``root`` itself or below it.

    Raises:
        PathEscapesRoot: A segment is a traversal instruction, holds a
            separator or a NUL byte, is absolute, or the joined path resolves
            outside ``root``.
    """
    base = Path(root).resolve()

    for segment in segments:
        if segment.strip() in _TRAVERSAL_SEGMENTS:
            raise PathEscapesRoot(f"'{segment}' is a path instruction, not a folder name.")
        if "\x00" in segment:
            raise PathEscapesRoot("A path segment contains a NUL byte.")
        if "/" in segment or "\\" in segment:
            raise PathEscapesRoot(f"'{segment}' contains a path separator.")
        if Path(segment).is_absolute():
            raise PathEscapesRoot(f"'{segment}' is an absolute path.")

    candidate = base.joinpath(*segments).resolve()
    if candidate != base and base not in candidate.parents:
        raise PathEscapesRoot(f"'{candidate}' is outside '{base}'.")
    return candidate


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
