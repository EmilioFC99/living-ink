"""Canonical body text in, one destination's native content out.

Every destination performs the same six steps — parse the canonical text into
blocks, map the metadata, map the blocks, resolve the attachments, degrade what
the target cannot express, assemble the payload. Steps 1 and 6 are shared code;
steps 2 to 5 are per-destination instructions against one contract.

**This is a different axis from the publish lifecycle.** Placement, atomicity,
retry and state are *not* shared — Apple Notes creates its attachments inside
the same ``osascript`` call as the note, so a base class sequencing "attachments,
then body" is unimplementable for it (which is why
:class:`~living_ink.destinations.filesystem.FileSystemDestination` exists
separately and Apple Notes does not extend it). Content transformation is
shared, and that is what lives here.

**The vocabulary is derived, not invented.** A :class:`BlockKind` exists when
both hold: the canonical body can plausibly contain it, *and* at least one
destination cannot represent it natively. The first test is answered by
``ocr_prompt.txt``, the only thing that writes canonical text; the second by the
fidelity matrix. Footnote is the instructive exclusion — it degrades in three of
the four destinations and is still not a kind, because nothing transcribing a
handwritten page emits one. **The prompt file and this enum move together.**

**The canonical dialect is Obsidian's.** Obsidian is native on nineteen of the
matrix's twenty elements, so this is not a neutral interchange format and
pretending otherwise would be dishonest: every other destination is a lossy
projection of it. That is deliberate — Obsidian is the 1.0 destination, and a
neutral dialect nobody ships against is a worse bet than an honest one.
"""

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Callable, ClassVar, Dict, List, Mapping, Optional, Sequence, Tuple

from living_ink.core.document import Page

if TYPE_CHECKING:  # pragma: no cover - annotation only
    from living_ink.settings import Settings


class BlockKind(str, Enum):
    """Every element the canonical body can contain.

    Derived from the fidelity matrix, not invented here: the matrix says which
    elements each destination preserves, degrades or drops, and this enum is its
    column headings. Adding a kind means adding a row to the matrix and a branch
    to every writer — which is the point. A writer cannot silently ignore a new
    kind.

    Frozen at eleven for 1.0. Inline elements (links, emphasis, inline code) are
    **not** kinds: they ride inside :attr:`Block.text` as CommonMark and each
    writer re-parses them, because splitting inline content into blocks would
    mean reimplementing a Markdown inline parser in every writer.

    A ``str`` mixin rather than ``StrEnum`` because the floor is Python 3.10.
    """

    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST = "list"
    TASK_LIST = "task_list"
    QUOTE = "quote"
    CALLOUT = "callout"
    CODE = "code"
    MATH = "math"
    IMAGE = "image"
    DIVIDER = "divider"
    TABLE = "table"


@dataclass(frozen=True)
class Block:
    """One destination-neutral body element.

    Attributes:
        kind: Which element this is.
        text: Inline content, still CommonMark for emphasis and links. A writer
            that cannot accept inline Markdown re-parses it; most can.
        level: Heading depth, or list nesting depth.
        children: Nested blocks. Non-empty only for ``LIST``, ``TASK_LIST`` and
            ``CALLOUT``.
        attrs: Kind-specific extras — ``callout_type``, the code block's
            ``language``, the image's ``page``, ``LIST``'s ``ordered`` flag and
            its ``number`` when ordered, ``TASK_LIST``'s ``checked`` flag,
            ``MATH``'s ``display`` flag.
    """

    kind: BlockKind
    text: str = ""
    level: int = 0
    children: Tuple["Block", ...] = ()
    attrs: Mapping[str, object] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Step 1 — parse
# ---------------------------------------------------------------------------

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_DIVIDER = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*(\S*)\s*$")
_CALLOUT = re.compile(r"^>\s*\[!(?P<type>[^\]]+)\]-?\+?\s*(?P<title>.*)$")
_QUOTE_LINE = re.compile(r"^>\s?(.*)$")
_TASK_ITEM = re.compile(r"^(?P<indent>\s*)[-*+]\s+\[(?P<mark>[ xX])\]\s+(?P<text>.*)$")
_BULLET_ITEM = re.compile(r"^(?P<indent>\s*)[-*+]\s+(?P<text>.*)$")
_ORDERED_ITEM = re.compile(r"^(?P<indent>\s*)(?P<number>\d+)[.)]\s+(?P<text>.*)$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_MATH_FENCE = re.compile(r"^\s*\$\$\s*$")

#: How many spaces of indentation count as one list nesting level. Two is what
#: every Markdown writer in common use emits for a nested bullet.
_INDENT_WIDTH = 2


def _is_list_line(line: str) -> bool:
    """Report whether a line opens a list item of any kind.

    Args:
        line: One line of canonical text.

    Returns:
        True for a bullet, a numbered item or a checkbox.
    """
    return bool(_TASK_ITEM.match(line) or _BULLET_ITEM.match(line) or _ORDERED_ITEM.match(line))


def _parse_list_item(line: str) -> Tuple[int, Block]:
    """Turn one list line into a childless block and its nesting depth.

    Args:
        line: A line matching one of the three list patterns.

    Returns:
        ``(depth, block)``. The block's children are filled in by the caller,
        which is the only thing that can see the lines below it.
    """
    task = _TASK_ITEM.match(line)
    if task:
        depth = len(task.group("indent")) // _INDENT_WIDTH
        checked = task.group("mark").lower() == "x"
        return depth, Block(
            kind=BlockKind.TASK_LIST,
            text=task.group("text").strip(),
            level=depth,
            attrs={"checked": checked},
        )

    ordered = _ORDERED_ITEM.match(line)
    if ordered:
        depth = len(ordered.group("indent")) // _INDENT_WIDTH
        # The number is kept rather than recomputed. A transcription of a
        # handwritten page can legitimately start at 3, or skip one, and a
        # writer that renumbers from 1 silently contradicts the page.
        return depth, Block(
            kind=BlockKind.LIST,
            text=ordered.group("text").strip(),
            level=depth,
            attrs={"ordered": True, "number": int(ordered.group("number"))},
        )

    bullet = _BULLET_ITEM.match(line)
    assert bullet is not None, "caller checked _is_list_line first"
    depth = len(bullet.group("indent")) // _INDENT_WIDTH
    return depth, Block(
        kind=BlockKind.LIST,
        text=bullet.group("text").strip(),
        level=depth,
        attrs={"ordered": False},
    )


def _nest(items: Sequence[Tuple[int, Block]]) -> Tuple[Block, ...]:
    """Fold a flat run of ``(depth, block)`` pairs into a tree.

    Args:
        items: List items in document order, with their nesting depths.

    Returns:
        The top-level items, each carrying its descendants as children.
    """
    roots: List[Block] = []
    # One open list per depth: appending at depth N re-parents everything
    # deeper than N that is still open.
    stack: List[Tuple[int, List[Block]]] = []

    for depth, block in items:
        while stack and stack[-1][0] >= depth:
            done_depth, done_children = stack.pop()
            parent_list = stack[-1][1] if stack else roots
            if parent_list:
                last = parent_list[-1]
                parent_list[-1] = Block(
                    kind=last.kind,
                    text=last.text,
                    level=last.level,
                    children=tuple(done_children),
                    attrs=last.attrs,
                )
            elif done_depth:
                # Deeper than anything above it; keep it rather than drop it.
                roots.extend(done_children)
        target = stack[-1][1] if stack else roots
        target.append(block)
        stack.append((depth, []))

    while stack:
        _, done_children = stack.pop()
        parent_list = stack[-1][1] if stack else roots
        if parent_list and done_children:
            last = parent_list[-1]
            parent_list[-1] = Block(
                kind=last.kind,
                text=last.text,
                level=last.level,
                children=tuple(done_children),
                attrs=last.attrs,
            )

    return tuple(roots)


def _parse_callout(lines: Sequence[str]) -> Block:
    """Parse a run of ``>``-prefixed lines that opens with a callout marker.

    Args:
        lines: The quoted lines, marker line first.

    Returns:
        A ``CALLOUT`` block whose children are the parsed inner content.
    """
    head = _CALLOUT.match(lines[0])
    assert head is not None, "caller matched the marker first"
    inner = [_QUOTE_LINE.match(line).group(1) if _QUOTE_LINE.match(line) else "" for line in lines]
    return Block(
        kind=BlockKind.CALLOUT,
        text=head.group("title").strip(),
        children=to_blocks("\n".join(inner[1:])),
        attrs={"callout_type": head.group("type").strip().lower()},
    )


def to_blocks(text: str) -> Tuple[Block, ...]:
    """Parse canonical page text into blocks.

    The canonical dialect is CommonMark **plus Obsidian callouts** —
    ``ocr_prompt.txt`` instructs the model to emit ``> [!quote]``, so a plain
    CommonMark parser sees a blockquote and the callout type is lost before any
    writer runs.

    ``IMAGE`` is never produced here. Page images are attachments the
    destination places and names, so an image block is constructed by the
    publish stage that knows where the file landed, not recovered from text.

    Args:
        text: The canonical body, as the transcription produced it.

    Returns:
        The blocks, in document order. Empty for empty input.
    """
    lines = text.replace("\r\n", "\n").split("\n")
    blocks: List[Block] = []
    paragraph: List[str] = []
    index = 0

    def flush_paragraph() -> None:
        """Close the paragraph being accumulated, if there is one."""
        joined = "\n".join(paragraph).strip()
        paragraph.clear()
        if joined:
            blocks.append(Block(kind=BlockKind.PARAGRAPH, text=joined))

    while index < len(lines):
        line = lines[index]

        if not line.strip():
            flush_paragraph()
            index += 1
            continue

        fence = _FENCE.match(line)
        if fence:
            flush_paragraph()
            closer = fence.group(1)[0] * 3
            body: List[str] = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith(closer):
                body.append(lines[index])
                index += 1
            index += 1  # step over the closing fence, present or not
            blocks.append(
                Block(
                    kind=BlockKind.CODE,
                    text="\n".join(body),
                    attrs={"language": fence.group(2)},
                )
            )
            continue

        if _MATH_FENCE.match(line):
            flush_paragraph()
            body = []
            index += 1
            while index < len(lines) and not _MATH_FENCE.match(lines[index]):
                body.append(lines[index])
                index += 1
            index += 1
            blocks.append(
                Block(kind=BlockKind.MATH, text="\n".join(body).strip(), attrs={"display": True})
            )
            continue

        if _DIVIDER.match(line):
            flush_paragraph()
            blocks.append(Block(kind=BlockKind.DIVIDER))
            index += 1
            continue

        heading = _HEADING.match(line)
        if heading:
            flush_paragraph()
            blocks.append(
                Block(
                    kind=BlockKind.HEADING,
                    text=heading.group(2).strip(),
                    level=len(heading.group(1)),
                )
            )
            index += 1
            continue

        if line.lstrip().startswith(">"):
            flush_paragraph()
            quoted: List[str] = []
            while index < len(lines) and lines[index].lstrip().startswith(">"):
                quoted.append(lines[index].lstrip())
                index += 1
            if _CALLOUT.match(quoted[0]):
                blocks.append(_parse_callout(quoted))
            else:
                inner = [
                    _QUOTE_LINE.match(q).group(1) if _QUOTE_LINE.match(q) else "" for q in quoted
                ]
                blocks.append(Block(kind=BlockKind.QUOTE, text="\n".join(inner).strip()))
            continue

        if _TABLE_ROW.match(line):
            flush_paragraph()
            rows: List[str] = []
            while index < len(lines) and _TABLE_ROW.match(lines[index]):
                rows.append(lines[index].strip())
                index += 1
            blocks.append(Block(kind=BlockKind.TABLE, text="\n".join(rows)))
            continue

        if _is_list_line(line):
            flush_paragraph()
            items: List[Tuple[int, Block]] = []
            while index < len(lines) and _is_list_line(lines[index]):
                items.append(_parse_list_item(lines[index]))
                index += 1
            blocks.extend(_nest(items))
            continue

        paragraph.append(line)
        index += 1

    flush_paragraph()
    return tuple(blocks)


def image_block(page: Page) -> Block:
    """Build the image block for a page whose attachment has been placed.

    The block names the page, not the file. Where that file ended up is the
    destination's answer and reaches the writer through
    :attr:`WriteContext.resolve_attachment` — which is the whole reason that
    callable exists, and the reason an image block is portable to a destination
    that uploads rather than copies.

    Args:
        page: The page the image was rendered from.

    Returns:
        An ``IMAGE`` block.
    """
    return Block(kind=BlockKind.IMAGE, text=page.label, attrs={"page": page})


# ---------------------------------------------------------------------------
# Steps 2 to 6 — the writer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Degradation:
    """A record that a block could not be represented natively.

    Attributes:
        kind: The block kind that could not be rendered.
        rendered_as: What it became instead, in the user's words —
            ``"blockquote"``.
        lossy: Whether information was lost, as opposed to merely restyled.
    """

    kind: BlockKind
    rendered_as: str
    lossy: bool

    def describe(self) -> str:
        """Phrase the degradation as the one line a user should read.

        Returns:
            A sentence naming what happened, ending in a period.
        """
        loss = "" if not self.lossy else ", losing detail"
        return f"{self.kind.value} became {self.rendered_as}{loss}."


@dataclass(frozen=True)
class WriteContext:
    """What a writer needs that is not the blocks.

    Carries the **attachment resolver**: a callable turning a :class:`Page` into
    whatever reference this destination uses. Without it the transformation is
    not reusable — a writer cannot emit an image reference without knowing what
    the destination will name and place the file as, and that answer differs
    between a filesystem destination and an API destination.

    Attributes:
        resolve_attachment: Page → the reference this destination uses for it.
        settings: The run's resolved settings, when the writer needs one.
    """

    resolve_attachment: Callable[[Page], str] = lambda page: ""
    settings: Optional["Settings"] = None


class MarkupWriter:
    """Turns blocks into one destination's native content.

    The pandoc/writer model: one method per block kind. Named ``MarkupWriter``
    and **not** ``Renderer`` because ``extract.py`` already owns that name for
    ``.rm`` → PNG; two things called Renderer at opposite ends of the same
    pipeline is a genuine confusion.

    Not an ``ABC`` with eleven abstract methods, because a writer that only
    handles two kinds would then have to write nine stubs that are never
    reached. :attr:`handles` is the declaration instead: a kind inside it must
    have a method, a kind outside it routes to :meth:`degrade`, and
    :meth:`render` raises rather than silently dropping a block if a writer
    claims a kind it has no method for.

    Class attributes:
        format: ``"markdown"`` | ``"html"`` | ``"enml"`` | ``"notion-blocks"``.
        handles: The kinds this writer renders natively. Anything outside it
            routes through :meth:`degrade`.
        tight_kinds: Kinds that must **not** be separated when they are
            adjacent. Empty by default, because most formats have no such
            notion; a list is the case that has one, and putting the blank line
            :meth:`assemble` would otherwise insert between two sibling items
            turns a tight list into a loose one.
    """

    format: ClassVar[str] = ""
    handles: ClassVar[frozenset] = frozenset()
    tight_kinds: ClassVar[frozenset] = frozenset()

    def tight_key(self, block: Block) -> Optional[object]:
        """Say which run this block belongs to, or None if it starts its own.

        Two adjacent blocks with the same non-None key are one run. Kind alone
        is the default; a writer overrides this when a kind splits into runs
        that must not merge.

        Args:
            block: The block about to be rendered.

        Returns:
            The run key, or None for a block that is always its own part.
        """
        return block.kind if block.kind in self.tight_kinds else None

    def join_tight(self, previous: object, current: object) -> object:
        """Join two adjacent parts that share a :meth:`tight_key`.

        Args:
            previous: The part already accumulated.
            current: The part to fold into it.

        Returns:
            The two, joined with no separation.
        """
        return f"{previous}\n{current}"

    def degrade(self, block: Block, ctx: WriteContext) -> Tuple[object, Degradation]:
        """Represent a block this writer cannot render natively.

        **Never returns silently.** A writer either renders a kind or declares
        what it did instead. :meth:`render` collects every :class:`Degradation`
        and the publish stage routes them into
        :attr:`~living_ink.core.document.PublishResult.warnings`, so a user is
        told "callouts became blockquotes" once — rather than discovering it by
        reading their own notes weeks later. Silent loss is the product's main
        failure mode; this contract makes it impossible to write by accident.

        Args:
            block: The block that has no native representation.
            ctx: The writer's context.

        Returns:
            The rendered stand-in and the record of what was given up.

        Raises:
            NotImplementedError: The subclass declared a kind unhandled and
                then did not say what to do with it.
        """
        raise NotImplementedError(f"{type(self).__name__} cannot render {block.kind.value}.")

    def assemble(self, parts: Sequence[object], ctx: WriteContext) -> object:
        """Join rendered blocks into the final payload.

        Overridden rather than shared because joining is format-specific and
        not always concatenation: Markdown joins with blank lines, ENML wraps in
        a doctype, Notion returns a JSON array whose nesting the string formats
        do not have.

        Args:
            parts: One rendered value per block, in order.
            ctx: The writer's context.

        Returns:
            The payload.
        """
        raise NotImplementedError(f"{type(self).__name__} does not assemble.")

    def render(
        self, blocks: Sequence[Block], ctx: Optional[WriteContext] = None
    ) -> Tuple[object, Tuple[Degradation, ...]]:
        """Render every block and assemble the result.

        The one dispatch point, so that "which kinds degrade" is answered by
        :attr:`handles` in exactly one place rather than by eleven ``if``
        statements inside each writer.

        Args:
            blocks: The parsed body.
            ctx: The writer's context. A default one is built when the caller
                has nothing to say, which is what a text-only writer wants.

        Returns:
            The assembled payload and every degradation that happened,
            de-duplicated: a note with forty callouts warns once.

        Raises:
            NotImplementedError: A writer claims a kind it has no method for.
        """
        ctx = ctx if ctx is not None else WriteContext()
        parts: List[object] = []
        seen: Dict[Tuple[BlockKind, str], Degradation] = {}
        previous_key: Optional[object] = None

        for block in blocks:
            if block.kind in self.handles:
                method = getattr(self, block.kind.value, None)
                if method is None:
                    raise NotImplementedError(
                        f"{type(self).__name__} handles {block.kind.value} "
                        f"but has no {block.kind.value}() method."
                    )
                rendered = method(block, ctx)
            else:
                rendered, degradation = self.degrade(block, ctx)
                seen.setdefault((degradation.kind, degradation.rendered_as), degradation)

            # Two adjacent blocks of one run are one part, so assemble() never
            # sees a seam it could widen. Done here rather than in assemble()
            # because this is the only place that still knows a part's block.
            key = self.tight_key(block)
            if parts and key is not None and key == previous_key:
                parts[-1] = self.join_tight(parts[-1], rendered)
            else:
                parts.append(rendered)
            previous_key = key

        return self.assemble(parts, ctx), tuple(seen.values())


class ObsidianWriter(MarkupWriter):
    """Renders blocks as the Obsidian-flavoured Markdown they came from.

    Nearly a passthrough, and wired up anyway. An abstraction with zero users at
    ship time is an abstraction that is wrong, and the first real writer would
    then be paying to fix a contract nobody had ever exercised. The cost is one
    parse-and-render round trip over content that was already in the target
    format; the benefit is that the contract is known to work before anyone
    depends on it.
    """

    format: ClassVar[str] = "markdown"

    # Written out rather than `frozenset(BlockKind)`. Deriving it means a
    # twelfth kind is silently claimed as handled, with no method to render it;
    # spelling it out means the twelfth kind routes to degrade() until somebody
    # writes ObsidianWriter.footnote().
    handles: ClassVar[frozenset] = frozenset(
        {
            BlockKind.HEADING,
            BlockKind.PARAGRAPH,
            BlockKind.LIST,
            BlockKind.TASK_LIST,
            BlockKind.QUOTE,
            BlockKind.CALLOUT,
            BlockKind.CODE,
            BlockKind.MATH,
            BlockKind.IMAGE,
            BlockKind.DIVIDER,
            BlockKind.TABLE,
        }
    )

    #: Markdown reads a blank line between two items as "loose list", and
    #: renders every item wrapped in its own paragraph. Sibling items are one
    #: part.
    tight_kinds: ClassVar[frozenset] = frozenset({BlockKind.LIST, BlockKind.TASK_LIST})

    def tight_key(self, block: Block) -> Optional[object]:
        """Group list items by kind *and* marker style.

        A bullet list that sits directly under a numbered one is two lists, and
        gluing them into one run would drop the blank line that says so. Two
        bullet lists separated only by a blank line do merge — Markdown treats
        them as one list anyway, so there is nothing to preserve.

        Args:
            block: The block about to be rendered.

        Returns:
            ``(kind, ordered)`` for a list item, None for anything else.
        """
        key = super().tight_key(block)
        return None if key is None else (key, bool(block.attrs.get("ordered")))

    def heading(self, block: Block, ctx: WriteContext) -> str:
        """Render an ATX heading at the block's level."""
        level = min(max(block.level, 1), 6)
        return f"{'#' * level} {block.text}"

    def paragraph(self, block: Block, ctx: WriteContext) -> str:
        """Render a paragraph unchanged; inline Markdown is already correct."""
        return block.text

    def list(self, block: Block, ctx: WriteContext) -> str:
        """Render a bullet or numbered item and everything nested under it."""
        if not block.attrs.get("ordered"):
            return self._item(block, "-", ctx)
        return self._item(block, f"{block.attrs.get('number', 1)}.", ctx)

    def task_list(self, block: Block, ctx: WriteContext) -> str:
        """Render a checkbox item and everything nested under it."""
        mark = "x" if block.attrs.get("checked") else " "
        return self._item(block, f"- [{mark}]", ctx)

    def _item(self, block: Block, marker: str, ctx: WriteContext) -> str:
        """Render one list item, indenting its children one level deeper.

        Args:
            block: The item.
            marker: The bullet, number or checkbox that opens it.
            ctx: The writer's context.

        Returns:
            The item and its descendants, newline-separated.
        """
        indent = " " * (_INDENT_WIDTH * block.level)
        lines = [f"{indent}{marker} {block.text}"]
        for child in block.children:
            renderer = self.task_list if child.kind is BlockKind.TASK_LIST else self.list
            lines.append(renderer(child, ctx))
        return "\n".join(lines)

    def quote(self, block: Block, ctx: WriteContext) -> str:
        """Render a blockquote."""
        return "\n".join(f"> {line}" for line in block.text.split("\n"))

    def callout(self, block: Block, ctx: WriteContext) -> str:
        """Render an Obsidian callout, marker line and quoted body."""
        kind = str(block.attrs.get("callout_type") or "note")
        title = f" {block.text}" if block.text else ""
        inner, _ = self.render(block.children, ctx)
        lines = [f"> [!{kind}]{title}"]
        for line in str(inner).split("\n"):
            lines.append(f"> {line}" if line else ">")
        return "\n".join(lines)

    def code(self, block: Block, ctx: WriteContext) -> str:
        """Render a fenced code block, keeping the language if one was given."""
        language = str(block.attrs.get("language") or "")
        return f"```{language}\n{block.text}\n```"

    def math(self, block: Block, ctx: WriteContext) -> str:
        """Render display math as a ``$$`` fence, inline math unchanged."""
        if block.attrs.get("display"):
            return f"$$\n{block.text}\n$$"
        return f"${block.text}$"

    def image(self, block: Block, ctx: WriteContext) -> str:
        """Render a page image as a WikiLink, embedded or merely linked.

        ``obsidian.embed_images`` is what decides. Until this method existed the
        link was hand-built as ``- [[target|label]]`` with no ``!``, so rendered
        pages landed as a bulleted list of links rather than as images.

        Args:
            block: An ``IMAGE`` block naming the page it was rendered from.
            ctx: The writer's context — its resolver says where the file went,
                its settings hold the preference.

        Returns:
            ``![[target|label]]`` when embedding, ``- [[target|label]]``
            otherwise.
        """
        page = block.attrs.get("page")
        target = ctx.resolve_attachment(page) if isinstance(page, Page) else ""
        link = f"[[{target}|{block.text}]]" if block.text else f"[[{target}]]"
        embed = getattr(ctx.settings, "obsidian_embed_images", True)
        return f"!{link}" if embed else f"- {link}"

    def divider(self, block: Block, ctx: WriteContext) -> str:
        """Render a horizontal rule."""
        return "---"

    def table(self, block: Block, ctx: WriteContext) -> str:
        """Render a pipe table unchanged; it is already Markdown."""
        return block.text

    def assemble(self, parts: Sequence[object], ctx: WriteContext) -> str:
        """Join rendered blocks with a blank line between them.

        Args:
            parts: The rendered blocks.
            ctx: The writer's context.

        Returns:
            The Markdown body, with no trailing newline.
        """
        return "\n\n".join(str(part) for part in parts if str(part).strip())


__all__ = [
    "Block",
    "BlockKind",
    "Degradation",
    "MarkupWriter",
    "ObsidianWriter",
    "WriteContext",
    "image_block",
    "to_blocks",
]
