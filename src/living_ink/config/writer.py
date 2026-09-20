"""Turn a configuration mapping back into a file a person can read.

Two commands write ``config.yml`` — ``setup`` at the end of the wizard and
``config`` when the user saves the menu — and before this there was one
hand-numbered serialiser inside the wizard that only knew the handful of keys
the wizard asked about. The menu can change any setting in the schema, so it
would have needed a second writer, and two writers is two answers to "what does
a Living Ink config look like".

The section comments come from :data:`~living_ink.config.schema.SECTIONS`, so
the file explains itself out of the same declaration that validates it. A
section nobody has an opinion about is omitted rather than written empty: a key
absent from the file is a key at its default, and spelling out every default
turns a twelve-line file into a hundred-line one that looks like a hundred
decisions.

**Writing is the one thing that loses a hand-written comment.** Loading never
rewrites (see :func:`~living_ink.config.validate.apply_status`); only an
explicit save does, and the commands that save say so first.
"""

from typing import Any, Dict, List, Mapping

import yaml

from living_ink.config.schema import ACTIVE, SECTIONS

#: Printed above the first line, so a file found on disk in a year says what
#: put it there and what is safe to edit by hand.
HEADER = (
    "# Living Ink configuration",
    "#",
    "# Written by 'living-ink setup' and 'living-ink config'. Editing it by",
    "# hand is fine; saving from the menu rewrites it and drops your comments.",
    "# Anything not named here is at its default — run 'living-ink info' to see",
    "# every setting and where its value came from.",
)


def render_config(values: Mapping[str, Any]) -> str:
    """Serialise a configuration mapping as commented YAML.

    Args:
        values: Section name to section mapping, plus any top-level scalars
            such as ``schema_version``. Empty sections are dropped.

    Returns:
        The file's contents, ending in a newline.
    """
    parts: List[str] = [*HEADER, ""]

    if "schema_version" in values:
        parts.append("# Config format version — do not edit.")
        parts.append(_dump({"schema_version": values["schema_version"]}))
        parts.append("")

    written = {"schema_version"}
    for name, section in SECTIONS.items():
        if section.status != ACTIVE or name not in values:
            continue
        written.add(name)
        body = values[name]
        if not body:
            continue
        if section.help:
            parts.append(f"# {section.help}")
        parts.append(_dump({name: body}))
        parts.append("")

    # Anything the schema has never heard of is still the user's, and a save
    # must not be a way to lose it. A destination plugin's section and a
    # retired one both land here, unchanged and unexplained.
    leftovers: Dict[str, Any] = {key: value for key, value in values.items() if key not in written}
    if leftovers:
        parts.append("# Not part of the Living Ink schema; preserved as found.")
        parts.append(_dump(leftovers))
        parts.append("")

    return "\n".join(parts).rstrip() + "\n"


def _dump(fragment: Mapping[str, Any]) -> str:
    """Render one fragment of the file.

    Values go through PyYAML rather than string interpolation, so a vault path
    or a folder name containing quotes, backslashes or colons round-trips.

    Args:
        fragment: The mapping to serialise.

    Returns:
        YAML with no trailing newline.
    """
    return yaml.safe_dump(
        dict(fragment),
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    ).rstrip()
