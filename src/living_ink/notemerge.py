"""Splice generated content into a note without destroying what a user wrote.

Living Ink used to regenerate an Obsidian note from scratch on every sync. If
you opened a synced note, added a heading, a link to another note, or a line of
your own commentary, the next run silently replaced the file and your writing
was gone. Nothing warned you, and because the transcript looked correct
afterwards there was no reason to suspect a crash or a bug.

The fix is a contract written into the file itself. Living Ink owns two
regions and nothing else:

* a fixed set of YAML frontmatter keys — :data:`OWNED_FRONTMATTER_KEYS`
* everything between :data:`MANAGED_BEGIN` and :data:`MANAGED_END`

Any other frontmatter key, and every line outside the markers, is copied
through verbatim. Text you add below the transcript stays below the
transcript; text you add above it stays above.

Notes written by older versions have no markers. Rather than guess, the merge
looks at the frontmatter: a ``source: Remarkable/...`` key means Living Ink
wrote the whole file and it is safe to regenerate. A file without it is
somebody else's, and its content is kept above the block Living Ink adds.
"""

import re
from typing import Dict, List, Optional, Tuple

#: Delimiters around the region Living Ink regenerates. HTML comments, so
#: Obsidian renders nothing and the note looks no different to the reader.
MANAGED_BEGIN = "<!-- living-ink:begin -->"
MANAGED_END = "<!-- living-ink:end -->"

#: Frontmatter keys Living Ink writes. Everything else in the block is the
#: user's and is preserved in its original order and formatting.
OWNED_FRONTMATTER_KEYS = (
    # First, so the identity of the note is the first thing anyone reading the
    # raw file sees. It is also what lets a moved note still be recognised.
    "living_ink_id",
    "created",
    "updated",
    "synced",
    "source",
    "type",
    "document",
    "tags",
)

#: A key line in a YAML block: not indented, not a list item, not a comment.
_KEY_LINE = re.compile(r"^([A-Za-z0-9_-]+)\s*:")

#: What marks a note as one Living Ink generated in full, before markers existed.
_OURS_MARKER = re.compile(r"^source\s*:\s*Remarkable/", re.MULTILINE)


def split_frontmatter(text: str) -> Tuple[List[str], str]:
    """Separate a YAML frontmatter block from the body below it.

    Args:
        text: Full note text.

    Returns:
        Tuple of (frontmatter lines without the ``---`` fences, body). The
        list is empty when the note has no frontmatter.
    """
    if not text.startswith("---"):
        return [], text

    lines = text.split("\n")
    if lines[0].strip() != "---":
        return [], text

    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            body = "\n".join(lines[index + 1 :])
            return lines[1:index], body.lstrip("\n")

    # An unterminated fence is not frontmatter; treat the whole file as body
    # rather than swallowing it.
    return [], text


def foreign_frontmatter(lines: List[str]) -> List[str]:
    """Return the frontmatter lines Living Ink does not own.

    Keys are matched at the top level only, and a key's indented continuation
    lines travel with it, so a multi-line ``tags:`` list is dropped or kept as
    a unit.

    Args:
        lines: Frontmatter lines, without the ``---`` fences.

    Returns:
        The lines belonging to keys outside :data:`OWNED_FRONTMATTER_KEYS`.
    """
    kept: List[str] = []
    dropping = False
    for line in lines:
        match = _KEY_LINE.match(line)
        if match:
            dropping = match.group(1).lower() in OWNED_FRONTMATTER_KEYS
        elif not line.strip():
            # A blank line ends nothing on its own; keep it with whichever
            # side it was already travelling on.
            pass
        if not dropping:
            kept.append(line)
    return kept


def frontmatter_value(lines: List[str], key: str) -> Optional[str]:
    """Read a single-line frontmatter value.

    Args:
        lines: Frontmatter lines, without the ``---`` fences.
        key: Key to look for, case-insensitively.

    Returns:
        The value with surrounding whitespace and quotes removed, or None.
    """
    for line in lines:
        match = _KEY_LINE.match(line)
        if match and match.group(1).lower() == key.lower():
            return line.split(":", 1)[1].strip().strip("\"'") or None
    return None


def looks_generated(text: str) -> bool:
    """Say whether a note without markers was written by Living Ink.

    Args:
        text: Full note text.

    Returns:
        True if its frontmatter carries the ``source: Remarkable/`` key every
        generated note has always had.
    """
    front, _ = split_frontmatter(text)
    return bool(_OURS_MARKER.search("\n".join(front)))


def split_managed_region(body: str) -> Tuple[str, Optional[str], str]:
    """Split a note body around the managed markers.

    Args:
        body: Note body, below any frontmatter.

    Returns:
        Tuple of (text before the block, the block's current content or None
        if there is no block, text after the block).
    """
    start = body.find(MANAGED_BEGIN)
    if start == -1:
        return body, None, ""
    end = body.find(MANAGED_END, start)
    if end == -1:
        # A begin with no end means the file was truncated mid-write by
        # something other than us; regenerate from the marker onwards rather
        # than treating the rest of the note as user content.
        return body[:start], "", ""
    return (
        body[:start],
        body[start + len(MANAGED_BEGIN) : end],
        body[end + len(MANAGED_END) :],
    )


def render(
    owned_frontmatter: List[str],
    managed_body: str,
    existing: Optional[str] = None,
) -> str:
    """Compose the note to write, preserving everything Living Ink does not own.

    Args:
        owned_frontmatter: Frontmatter lines Living Ink generates, without the
            ``---`` fences.
        managed_body: Generated content for the region between the markers.
        existing: Current contents of the note, if it already exists.

    Returns:
        The full text to write.
    """
    prefix, suffix = "", ""
    kept_frontmatter: List[str] = []

    if existing:
        front, body = split_frontmatter(existing)
        kept_frontmatter = foreign_frontmatter(front)
        before, current, after = split_managed_region(body)
        if current is not None:
            prefix, suffix = before, after
        elif not looks_generated(existing):
            # Somebody else's file sharing our name. Keep all of it and add
            # the transcript underneath rather than replacing their work.
            prefix = body.rstrip("\n") + "\n\n" if body.strip() else ""

    lines = ["---", *owned_frontmatter, *kept_frontmatter, "---", ""]
    head = "\n".join(lines)

    managed = f"{MANAGED_BEGIN}\n{managed_body.strip()}\n{MANAGED_END}"
    # A blank line after the block, so a heading the user put underneath is
    # still a heading once Obsidian renders it.
    tail = f"\n\n{suffix.strip()}" if suffix.strip() else ""
    return f"{head}{prefix}{managed}{tail}".rstrip("\n") + "\n"


def read_existing(path) -> Optional[str]:
    """Read a note if it is there, treating an unreadable one as absent.

    Args:
        path: Note file.

    Returns:
        The note text, or None when it does not exist or cannot be decoded.
    """
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def owned_frontmatter_lines(values: Dict[str, object]) -> List[str]:
    """Render Living Ink's frontmatter keys in a stable order.

    Args:
        values: Mapping of owned key to its value. A list value becomes a YAML
            block sequence; a None or empty value is omitted.

    Returns:
        Frontmatter lines, without the ``---`` fences.
    """
    lines: List[str] = []
    for key in OWNED_FRONTMATTER_KEYS:
        value = values.get(key)
        if value is None or value == "" or value == []:
            continue
        if isinstance(value, (list, tuple)):
            lines.append(f"{key}:")
            lines.extend(f"  - {item}" for item in value)
        else:
            lines.append(f"{key}: {value}")
    return lines
