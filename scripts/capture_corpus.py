#!/usr/bin/env python3
"""Copy the tablet's document store to a local directory, for tests to read.

The committed fixture corpus under ``tests/fixtures/corpus`` is synthetic: it
is bytes this project generated, so it can only contain what we already believe
about the format. This script produces the other tier — a copy of what the
device actually wrote — so a test can check the belief against reality. Four
facts about the on-disk format were wrong in this project's fixtures until a
capture disproved them, which is the whole argument for having it.

The capture is **never committed**. It is the user's own notebooks and the
repository is public, so it lands under ``~/.local/share/living-ink/test-corpus``
and the suite skips every test that needs one when it is absent.

**This script only reads.** It has no delete, no write-back, no rename, and no
``--force``: nothing here can modify the tablet, and nothing here can overwrite
a previous capture either. A second capture on the same day gets a new numbered
directory rather than replacing the first. That is not a guard that could be
switched off — the operations simply are not present.

Usage:
    uv run python scripts/capture_corpus.py
    uv run python scripts/capture_corpus.py --host 10.11.99.1 --out /tmp/capture
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from living_ink.ssh import (  # noqa: E402 - needs the path above
    DEFAULT_SSH_HOST,
    DEFAULT_SSH_PORT,
    DEFAULT_SSH_USER,
    XOCHITL_PATH,
)

#: Where captures live. Outside the checkout, because the checkout is public.
DEFAULT_OUT = Path.home() / ".local" / "share" / "living-ink" / "test-corpus"

#: Options every ``ssh`` invocation gets: never prompt, fail fast when the
#: tablet is not plugged in.
SSH_OPTIONS = [
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=5",
    "-o",
    "StrictHostKeyChecking=accept-new",
]


def _unused(base: Path, stem: str) -> Path:
    """Return a path under ``base`` that nothing occupies yet.

    The suffix is zero-padded so that the newest of a day's captures is also
    the last in sort order, which is how the ``device_corpus`` fixture picks
    one.

    Args:
        base: The directory to name a child of.
        stem: The preferred name.

    Returns:
        ``base/stem``, or ``base/stem-02``, ``-03`` and so on if taken.
    """
    candidate = base / stem
    counter = 2
    while candidate.exists():
        candidate = base / f"{stem}-{counter:02d}"
        counter += 1
    return candidate


def next_capture_dir(base: Path) -> Path:
    """Pick a capture directory that does not exist yet.

    Never returns a path that is already occupied, so a capture can never
    overwrite an earlier one.

    Args:
        base: The directory holding all captures.

    Returns:
        A fresh ``device-YYYYMMDD`` path.
    """
    return _unused(base, f"device-{date.today().strftime('%Y%m%d')}")


def staging_dir(destination: Path) -> Path:
    """Pick the scratch directory a capture streams into before it is named.

    Also never reuses an occupied path. That matters more than it looks:
    because this script deletes nothing, a fixed staging name would mean the
    leftovers of one failed capture block every retry until someone cleans up
    by hand. A tablet that is merely asleep is the common case, so a retry has
    to just work.

    Args:
        destination: The final capture directory.

    Returns:
        A fresh sibling ``.partial-<name>`` path.
    """
    return _unused(destination.parent, f".partial-{destination.name}")


def capture(host: str, user: str, port: int, destination: Path) -> Path:
    """Stream the tablet's xochitl directory into a local directory.

    Uses a single ``tar`` over ``ssh`` rather than one ``cat`` per file: the
    store is thousands of small files, and a round trip each would take
    minutes. The remote side of the pipe is ``tar c``, which reads; there is no
    remote command here that writes.

    The bytes land in a sibling ``.partial-`` directory and are renamed into
    place only once both ends succeed. A capture that dies halfway — the tablet
    asleep, the cable pulled — therefore never leaves a ``device-*`` directory
    behind for the test fixture to pick up as the newest capture and then find
    empty. The partial directory is left where it is rather than removed,
    because this script deletes nothing; :func:`staging_dir` is what keeps that
    leftover from blocking the retry.

    Args:
        host: Tablet address.
        user: SSH user.
        port: SSH port.
        destination: Local directory to fill, once the transfer succeeds.

    Returns:
        The directory that was written.

    Raises:
        RuntimeError: If either side of the pipe fails.
    """
    staging = staging_dir(destination)
    staging.mkdir(parents=True)

    remote = subprocess.Popen(
        [
            "ssh",
            *SSH_OPTIONS,
            "-p",
            str(port),
            f"{user}@{host}",
            f"tar cf - -C {XOCHITL_PATH} .",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    local = subprocess.run(
        ["tar", "xf", "-", "-C", str(staging)],
        stdin=remote.stdout,
        capture_output=True,
    )
    if remote.stdout is not None:
        remote.stdout.close()
    remote.wait()

    if remote.returncode != 0:
        detail = (remote.stderr.read().decode(errors="replace") if remote.stderr else "").strip()
        raise RuntimeError(f"Reading from the tablet failed: {detail or remote.returncode}")
    if local.returncode != 0:
        detail = local.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"Unpacking the capture failed: {detail or local.returncode}")

    staging.rename(destination)
    return destination


def summarise(destination: Path) -> str:
    """Describe what a capture contains.

    Args:
        destination: The capture directory.

    Returns:
        A one-line summary of documents, pages and total size.
    """
    metadata = list(destination.glob("*.metadata"))
    pages = list(destination.rglob("*.rm"))
    total = sum(p.stat().st_size for p in destination.rglob("*") if p.is_file())
    return f"{len(metadata)} items, {len(pages)} pages, {total / 1_048_576:.1f} MB"


def main(argv: list[str] | None = None) -> int:
    """Capture the tablet and report where it landed.

    Args:
        argv: Command-line arguments; defaults to ``sys.argv[1:]``.

    Returns:
        A process exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default=DEFAULT_SSH_HOST, help="tablet address")
    parser.add_argument("--user", default=DEFAULT_SSH_USER, help="SSH user")
    parser.add_argument("--port", type=int, default=DEFAULT_SSH_PORT, help="SSH port")
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"where captures are kept (default: {DEFAULT_OUT})",
    )
    args = parser.parse_args(argv)

    destination = next_capture_dir(args.out)
    print(f"Reading {args.user}@{args.host}:{XOCHITL_PATH}", flush=True)
    try:
        capture(args.host, args.user, args.port, destination)
    except (RuntimeError, OSError) as error:
        print(f"Capture failed: {error}", file=sys.stderr)
        return 1

    print(f"Captured {summarise(destination)}")
    print(f"  -> {destination}")
    print("Nothing on the tablet was modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
