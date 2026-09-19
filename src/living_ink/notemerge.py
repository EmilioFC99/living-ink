"""Splice generated content into a note without destroying what a user wrote.

Living Ink used to regenerate an Obsidian note from scratch on every sync. If
you opened a synced note, added a heading, a link to another note, or a line of
your own commentary, the next run silently replaced the file and your writing
was gone. Nothing warned you, and because the transcript looked correct
afterwards there was no reason to suspect a crash or a bug.

The fix is a contract written into the file itself. Living Ink owns a fixed set
of YAML frontmatter keys — :data:`OWNED_FRONTMATTER_KEYS` — and a set of
**named blocks**, each one delimited by a pair of HTML comments:

.. code-block:: markdown

    <!-- living-ink:begin page-77 h=8f3c1a2e -->
    …the transcription of page 77…
    <!-- living-ink:end page-77 -->

    Cross-reference: this is the same taxonomy [[DMBOK]] uses.

**Everything between one block's end and the next block's begin is yours,
forever.** That is what makes one region per *page* rather than one per note:
the unit a reader wants to react to is a single page, and the place the
reaction belongs is directly underneath it — not at the bottom of a forty-page
dump. A transcript you cannot annotate is a read-only export.

Four properties, in priority order:

1. **Free text is never written to.** No branch deletes or rewrites a segment
   with no block id. The worst reachable failure is a block appearing in the
   wrong position, never a lost sentence.
2. **A block this run did not generate is not deleted.** A page that failed
   OCR, a provider that rate-limited, a model that returned fewer annotations
   than last time — none of them may erase a block and strand the commentary
   underneath it. Deletion needs ``--prune``.
3. **An edit inside a block is preserved, not overwritten.** The ``h=`` on the
   begin marker is a digest of what Living Ink last wrote there; a mismatch
   means somebody typed inside the boundaries. Both versions survive.
4. **The divider lives inside the block**, as its first line. Between blocks it
   would be free text, so deleting one would be permanent.

Notes written by older versions have no named blocks. Rather than guess, the
merge looks at the frontmatter: a ``source: Remarkable/...`` key means Living
Ink wrote the whole file and it is safe to regenerate. A file without it is
somebody else's, and its content is kept above the blocks Living Ink adds.
"""

import hashlib
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

#: A begin marker: the block's id, and the digest of what was last written in
#: it. HTML comments, so Obsidian renders nothing and the note looks no
#: different to the reader.
_BEGIN_LINE = re.compile(
    r"^<!--\s*living-ink:begin\s+(?P<id>[A-Za-z0-9][A-Za-z0-9_.\-]*)"
    r"(?:\s+h=(?P<hash>[0-9a-f]+))?\s*-->$"
)

#: Prefix of the comment that introduces a version of a block the user edited.
#: Deliberately *not* a begin marker: parked text is free text, so the next
#: sync leaves it alone rather than reclaiming it.
PARKED_PREFIX = "<!-- living-ink: your edit to"

#: Reserved block id for the list of original page images, which is not a page.
ATTACHMENTS_BLOCK = "attachments"

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


def frontmatter_list(lines: List[str], key: str) -> List[str]:
    """Read a list-valued frontmatter key, in either YAML spelling.

    Args:
        lines: Frontmatter lines, without the ``---`` fences.
        key: Key to look for, case-insensitively.

    Returns:
        The values in the order they appear, or an empty list. Both the block
        sequence Living Ink writes and the inline flow a user may have typed by
        hand are read; the note is theirs to format.
    """
    values: List[str] = []
    collecting = False
    for line in lines:
        match = _KEY_LINE.match(line)
        if match:
            if match.group(1).lower() != key.lower():
                collecting = False
                continue
            collecting = True
            inline = line.split(":", 1)[1].strip()
            if inline.startswith("[") and inline.endswith("]"):
                values.extend(
                    part.strip().strip("\"'") for part in inline[1:-1].split(",") if part.strip()
                )
                collecting = False
            elif inline:
                values.append(inline.strip("\"'"))
                collecting = False
            continue
        if collecting:
            item = line.strip()
            if item.startswith("-"):
                cleaned = item[1:].strip().strip("\"'")
                if cleaned:
                    values.append(cleaned)
            elif item:
                collecting = False
    return values


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


@dataclass(frozen=True)
class Segment:
    """One run of a note: either the user's prose or a block Living Ink owns.

    Attributes:
        block_id: The block's id, or None for free text. Free text is never
            written to, never reordered and never dropped.
        content: The segment's text. For a block this is what sits between the
            markers, stripped; for free text it is the lines verbatim, because
            a blank line the user left is spacing they chose.
        written_hash: The ``h=`` from the begin marker — the digest of what
            Living Ink last wrote there. None for free text, and None for a
            block written before digests existed.
    """

    block_id: Optional[str] = None
    content: str = ""
    written_hash: Optional[str] = None


def block_digest(content: str) -> str:
    """Digest the content of one block, and nothing else.

    Deterministic by construction: the input is the block's own stripped text,
    so no timestamp, no page count in a surrounding heading and no dict
    iteration order can reach it. If anything run-to-run variable got in, every
    block would look user-edited on every sync and the merge would park a
    duplicate copy of it below itself each time — the note would grow without
    bound, which is worse than the problem blocks solve.

    Args:
        content: The block's text.

    Returns:
        Eight hex characters. Short enough to read in a diff; this is a
        change detector, not a security boundary.
    """
    return hashlib.sha256(content.strip().encode("utf-8")).hexdigest()[:8]


def begin_marker(block_id: str, digest: Optional[str] = None) -> str:
    """Render a block's opening comment.

    Args:
        block_id: The block's id.
        digest: The digest of its content, if it is known.

    Returns:
        The marker line.
    """
    suffix = f" h={digest}" if digest else ""
    return f"<!-- living-ink:begin {block_id}{suffix} -->"


def end_marker(block_id: str) -> str:
    """Render a block's closing comment.

    Args:
        block_id: The block's id.

    Returns:
        The marker line.
    """
    return f"<!-- living-ink:end {block_id} -->"


def parse_segments(body: str) -> List[Segment]:
    """Split a note body into ordered free-text and block segments.

    A begin marker with no matching end is left as free text rather than
    swallowing the rest of the note: the file was truncated by something other
    than us, and the conservative answer is to keep every line.

    Args:
        body: Note body, below any frontmatter.

    Returns:
        The segments, in document order. Adjacent free text is one segment.
    """
    lines = body.split("\n")
    segments: List[Segment] = []
    free: List[str] = []
    index = 0

    def flush() -> None:
        if free:
            segments.append(Segment(content="\n".join(free)))
            free.clear()

    while index < len(lines):
        match = _BEGIN_LINE.match(lines[index].strip())
        if not match:
            free.append(lines[index])
            index += 1
            continue

        closing = end_marker(match.group("id"))
        close_at = None
        for ahead in range(index + 1, len(lines)):
            if lines[ahead].strip() == closing:
                close_at = ahead
                break
        if close_at is None:
            free.append(lines[index])
            index += 1
            continue

        flush()
        segments.append(
            Segment(
                block_id=match.group("id"),
                content="\n".join(lines[index + 1 : close_at]).strip(),
                written_hash=match.group("hash"),
            )
        )
        index = close_at + 1

    flush()
    return segments


def render_segments(segments: Iterable[Segment]) -> str:
    """Put parsed segments back together.

    Args:
        segments: The segments, in order.

    Returns:
        The body text. Round-trips: ``parse_segments(render_segments(x)) == x``.
    """
    parts: List[str] = []
    for segment in segments:
        if segment.block_id is None:
            parts.append(segment.content)
            continue
        # A blank line before a begin marker, so a divider as the block's first
        # line is a divider and not a setext underline for whatever precedes it.
        if parts and parts[-1] != "" and not parts[-1].endswith("\n"):
            parts.append("")
        parts.append(
            "\n".join(
                [
                    begin_marker(segment.block_id, segment.written_hash),
                    segment.content,
                    end_marker(segment.block_id),
                ]
            )
        )
    return "\n".join(parts)


def _insert_point(segments: Sequence[Segment], after: Optional[int]) -> int:
    """Find where a block with no home yet belongs.

    Before the next block, never immediately after the previous one: the free
    text between two blocks is the user's commentary on the *earlier* one, so
    inserting a new block straight after its predecessor would wedge it between
    a page and the note somebody wrote about that page.

    Args:
        segments: The segments so far.
        after: Index of the last block placed, or None if none has been.

    Returns:
        The index to insert at.
    """
    start = 0 if after is None else after + 1
    for index in range(start, len(segments)):
        if segments[index].block_id is not None:
            return index
    return len(segments)


def merge_segments(
    segments: Sequence[Segment], generated: Sequence[Tuple[str, str]]
) -> Tuple[List[Segment], List[str]]:
    """Splice this run's blocks into a note, preserving everything else.

    Args:
        segments: The note as parsed, in order.
        generated: This run's blocks as ``(block_id, content)``, in the order
            they should appear.

    Returns:
        Tuple of (the merged segments, one human-readable line per surprise).
    """
    merged = list(segments)
    warnings: List[str] = []
    generated_ids = {block_id for block_id, _ in generated}

    seen: Dict[str, int] = {}
    for index, segment in enumerate(merged):
        if segment.block_id is None:
            continue
        if segment.block_id in seen:
            warnings.append(
                f"'{segment.block_id}' appears more than once in the note; "
                f"only the first copy was updated."
            )
        else:
            seen[segment.block_id] = index

    anchor: Optional[int] = None
    for block_id, content in generated:
        digest = block_digest(content)
        at = seen.get(block_id)

        if at is None:
            at = _insert_point(merged, anchor)
            merged.insert(at, Segment(block_id, content.strip(), digest))
            seen = {key: (value + 1 if value >= at else value) for key, value in seen.items()}
            seen[block_id] = at
            anchor = at
            continue

        current = merged[at]
        edited = (
            current.written_hash is not None
            and block_digest(current.content) != current.written_hash
        )
        merged[at] = Segment(block_id, content.strip(), digest)
        anchor = at

        if edited:
            # Writing the fresh block and dropping their version would punish a
            # mistake with a deletion. Both survive; the user decides.
            parked = f"{PARKED_PREFIX} {block_id}, kept below -->\n{current.content}"
            merged.insert(at + 1, Segment(content=parked))
            seen = {key: (value + 1 if value > at else value) for key, value in seen.items()}
            anchor = at + 1
            warnings.append(
                f"You had edited inside the '{block_id}' block. It was rewritten "
                f"and your version kept directly below it."
            )

    for segment in merged:
        if segment.block_id is not None and segment.block_id not in generated_ids:
            warnings.append(
                f"'{segment.block_id}' was not part of this sync, so the note "
                f"keeps what it already had."
            )

    return merged, warnings


def render(
    owned_frontmatter: List[str],
    blocks: Sequence[Tuple[str, str]],
    existing: Optional[str] = None,
) -> Tuple[str, List[str]]:
    """Compose the note to write, preserving everything Living Ink does not own.

    Args:
        owned_frontmatter: Frontmatter lines Living Ink generates, without the
            ``---`` fences.
        blocks: This run's content as ordered ``(block_id, content)`` pairs.
        existing: Current contents of the note, if it already exists.

    Returns:
        Tuple of (the full text to write, one line per surprise the merge hit).
    """
    kept_frontmatter: List[str] = []
    segments: List[Segment] = []

    if existing:
        front, body = split_frontmatter(existing)
        kept_frontmatter = foreign_frontmatter(front)
        segments = parse_segments(body)
        if not any(segment.block_id for segment in segments):
            # Nothing of ours in there. Either somebody else's file sharing our
            # name — keep all of it and add the transcript underneath — or one
            # of ours from before blocks existed, which is safe to regenerate.
            segments = (
                [Segment(content=body.rstrip("\n") + "\n")]
                if body.strip() and not looks_generated(existing)
                else []
            )

    merged, warnings = merge_segments(segments, blocks)
    head = "\n".join(["---", *owned_frontmatter, *kept_frontmatter, "---", ""])
    return f"{head}{render_segments(merged)}".rstrip("\n") + "\n", warnings


@dataclass(frozen=True)
class ExistingNote:
    """What is at a note's path right now.

    Three states, not two. "There is no file" and "there is a file I cannot
    read" are the same answer to :func:`read_existing` and were the same answer
    to every caller: the name is free, write over it. A ``.md`` holding invalid
    UTF-8, or one the user has locked, was destroyed without a warning.

    Attributes:
        text: The note's content, or None when there is nothing readable there.
        unreadable: True when a file exists but could not be read. A caller
            must treat this as occupied, never as free.
    """

    text: Optional[str] = None
    unreadable: bool = False

    @property
    def occupied(self) -> bool:
        """True when something is at that path, readable or not."""
        return self.text is not None or self.unreadable


def inspect_existing(path) -> ExistingNote:
    """Look at a note's path and say which of the three states it is in.

    Args:
        path: Note file.

    Returns:
        An :class:`ExistingNote` describing what is there.
    """
    try:
        return ExistingNote(text=path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ExistingNote()
    except (OSError, UnicodeDecodeError):
        # A directory, a permission denial, a broken symlink, bytes that are
        # not UTF-8 — all of them mean something is there and we cannot judge
        # whether it is ours.
        return ExistingNote(unreadable=True)


def read_existing(path) -> Optional[str]:
    """Read a note if it is there, treating an unreadable one as absent.

    Prefer :func:`inspect_existing` anywhere the answer decides whether a file
    gets overwritten; this is for callers that only want the text.

    Args:
        path: Note file.

    Returns:
        The note text, or None when it does not exist or cannot be decoded.
    """
    return inspect_existing(path).text


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
