"""``living-ink config`` — change any setting, and nothing the schema has not declared.

The menu is **generated**, not written. One row per
:data:`~living_ink.config.schema.LIVE_SETTINGS` entry, grouped by
:attr:`~living_ink.config.schema.Setting.menu_section`, labelled with the same
name ``living-ink info`` prints and edited with the widget its
:attr:`~living_ink.config.schema.Setting.kind` calls for. Adding a setting to
the schema adds it here; there is no list of settings in this file, and a
setting that appeared in ``info`` but not in the menu would be a setting the
user can be told about and cannot change.

Four rules shape it.

**Nothing reaches disk until Save.** Every edit lands in :class:`Edits`, an
ordinary dictionary keyed by field name. ``Discard and exit`` writes nothing,
Ctrl+C writes nothing, and a crash writes nothing. The save is one atomic
:func:`living_ink.safeio.write_secret_atomic` for the file plus one credential
write per secret, and the summary shown before it names every change.

**A save starts from the file, not from the resolved settings.** The menu shows
*effective* values — the ones ``info`` shows, merged across the precedence
ladder — but it writes back only the keys the file already had plus the ones
the user actually changed. Writing every effective value would materialise
thirty-eight defaults into the file, freezing today's defaults into a config
that is supposed to track them, and would bake a temporary environment variable
in as if it had been typed.

**A row says where its value comes from, and an edit that will not take effect
says so.** The precedence ladder is flag > env > credentials > file > default,
so editing a setting an environment variable is currently supplying changes the
file and changes nothing else. The menu says that at the moment of the edit,
which is the only moment the user can act on it.

**Ctrl+C means two different things, deliberately.** At a menu it leaves, at an
edit prompt it abandons that one edit and returns to the menu. They are the two
things a user means by it, and the alternative — one rule — costs either a way
out of the menu or the whole session's edits every time someone changes their
mind about a single value.
"""

import argparse
import copy
import logging
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from living_ink import ui
from living_ink.cli.base import BaseCommand
from living_ink.config.schema import (
    ACTIVE,
    CHOICE,
    FLAG,
    LIST,
    LIVE_SETTINGS,
    NUMBER,
    PATH,
    SECTIONS,
    STORE_CREDENTIALS,
    WHOLE,
    Setting,
)
from living_ink.logs import console

logger = logging.getLogger(__name__)

#: Menu answers that are not a section name. A byte no section or field name
#: can contain, so a sentinel can never collide with a real row.
PROMPTS = "\x00prompts"
ADVANCED = "\x00advanced"
SAVE = "\x00save"
DISCARD = "\x00discard"
BACK = "\x00back"
RESET = "\x00reset"

#: The editors to fall back on, in order, when neither ``$VISUAL`` nor
#: ``$EDITOR`` is set. ``nano`` first: a user with no editor preference is
#: least likely to be stranded inside it.
FALLBACK_EDITORS = ("nano", "vi")


class Removed:
    """A pending change that deletes a key rather than setting one.

    ``None`` cannot carry this: it is a legitimate value for several settings,
    and a dictionary cannot distinguish "the user set it to nothing" from "the
    user asked for the default back" if both are spelled the same way.
    """

    def __repr__(self) -> str:
        """Return a form that reads as itself in a test failure.

        Returns:
            The sentinel's name.
        """
        return "<removed>"


#: The one instance. Identity, not equality, is what the save checks.
REMOVED_VALUE = Removed()


class ConfigMenu:
    """The interactive settings menu, from opening it to writing the file.

    Attributes:
        root: Repository root, only used to resolve the config path.
        config_file: The ``config.yml`` this session reads and may write.
        raw: The file as parsed, never mutated — the save copies it.
        edits: Field name to new value, or :data:`REMOVED_VALUE`.
    """

    def __init__(self, root: Optional[Path] = None) -> None:
        """Read the current configuration without interpreting it.

        Args:
            root: Repository root, passed to
                :func:`living_ink.config.get_config_path`.
        """
        from living_ink.config import get_config_path, read_config_file

        self.root = root
        self.config_file = get_config_path(root)
        self.raw: Dict[str, Any] = read_config_file(self.config_file)
        self.edits: Dict[str, Any] = {}

    # -- reading ------------------------------------------------------------

    def pending_config(self) -> Dict[str, Any]:
        """Return the configuration mapping as the edits would leave it.

        A copy: the menu can be abandoned at any point, and a mutation of
        :attr:`raw` would survive the abandonment.

        Returns:
            The parsed file with every pending config edit applied and every
            reset key removed. Credential edits are not in it — they are not
            in the file.
        """
        values = copy.deepcopy(self.raw)
        for setting in LIVE_SETTINGS:
            if setting.field not in self.edits or setting.key is None:
                continue
            section, leaf = setting.section, setting.leaf
            new = self.edits[setting.field]
            target = values if section == "" else values.setdefault(section or "", {})
            if not isinstance(target, dict):
                # A section the user wrote as a scalar. Replacing it is the
                # only way the edit can land, and the save summary said so.
                target = {}
                values[section or ""] = target
            if new is REMOVED_VALUE:
                target.pop(leaf, None)
            else:
                target[leaf] = new
        return values

    def origins(self) -> Dict[str, Any]:
        """Resolve every setting against the pending configuration.

        The same call ``info`` makes, so a row in this menu and a row in that
        table cannot disagree about what a setting currently is.

        Returns:
            Field name to :class:`living_ink.settings.SettingOrigin`.
        """
        from living_ink.settings import Settings

        found = Settings.explain(self.pending_config(), config_path=self.config_file)
        return {origin.name: origin for origin in found}

    # -- the main menu ------------------------------------------------------

    def run(self) -> bool:
        """Show the menu until the user saves or leaves.

        Returns:
            True if the configuration was written, False if it was discarded.
        """
        console("")
        console(ui.bold("Living Ink settings"))
        console(ui.dim(f"  {self.config_file}"))

        while True:
            answer = ui.required(ui.select("What would you like to change?", self.main_choices()))
            if answer in (SAVE, DISCARD):
                # None is "the user backed out of the confirmation", which is a
                # request to carry on editing and not a quiet way to lose every
                # pending change.
                outcome = self.save() if answer == SAVE else self.discard()
                if outcome is not None:
                    return outcome
                continue
            if answer == PROMPTS:
                self.prompts_menu()
            elif answer == ADVANCED:
                self.advanced_menu()
            else:
                self.section_menu(answer)

    def main_choices(self) -> List[ui.Choice]:
        """Build the top-level rows.

        Returns:
            One choice per populated active section, then the three entries
            that are not sections.
        """
        origins = self.origins()
        rows = [
            ui.Choice(
                name,
                f"{name:<12}{SECTIONS[name].help}",
                description=self._section_summary(name, origins),
            )
            for name in SECTIONS
            if SECTIONS[name].status == ACTIVE and settings_in(name)
        ]
        rows.append(
            ui.Choice(
                PROMPTS,
                "Prompts…",
                description="Edit what the model is told, in your editor.",
            )
        )
        rows.append(
            ui.Choice(
                ADVANCED,
                "Advanced…",
                description="Caches and the sync database.",
            )
        )
        pending = len(self.edits)
        rows.append(
            ui.Choice(
                SAVE,
                f"Save and exit ({pending} change{'' if pending == 1 else 's'})"
                if pending
                else "Save and exit (nothing changed)",
            )
        )
        rows.append(ui.Choice(DISCARD, "Discard and exit"))
        return rows

    def _section_summary(self, name: str, origins: Dict[str, Any]) -> str:
        """Describe a section in one line, for the row's second line.

        Args:
            name: Section name.
            origins: Field name to resolved origin.

        Returns:
            How many of the section's settings are not at their default, and
            how many of them this session has changed.
        """
        from living_ink.settings import SOURCE_DEFAULT

        fields = [s.field for s in settings_in(name)]
        set_here = sum(1 for f in fields if origins[f].source != SOURCE_DEFAULT)
        changed = sum(1 for f in fields if f in self.edits)
        parts = [f"{len(fields)} settings", f"{set_here} not at default"]
        if changed:
            parts.append(f"{changed} edited")
        return ", ".join(parts)

    # -- one section --------------------------------------------------------

    def section_menu(self, name: str) -> None:
        """Show one section's settings until the user goes back.

        Args:
            name: Section name, as written in ``config.yml``.
        """
        while True:
            settings = settings_in(name)
            origins = self.origins()
            width = max(len(label_for(s)) for s in settings)
            rows = [
                ui.Choice(
                    s.field,
                    f"{label_for(s):<{width}}  {self._value_column(s, origins[s.field])}",
                    description=s.help,
                )
                for s in settings
            ]
            if any(s.field in self.edits for s in settings):
                rows.append(ui.Choice(RESET, "Undo the changes made here"))
            rows.append(ui.Choice(BACK, "Back"))

            answer = ui.required(ui.select(f"{name} — {SECTIONS[name].help}", rows))
            if answer == BACK:
                return
            if answer == RESET:
                for setting in settings:
                    self.edits.pop(setting.field, None)
                console(ui.dim(f"  Reverted every pending change in '{name}'."))
                continue
            self.edit(next(s for s in settings if s.field == answer))

    def _value_column(self, setting: Setting, origin: Any) -> str:
        """Render a setting's current value and where it comes from.

        Args:
            setting: The setting being described.
            origin: Its resolved :class:`~living_ink.settings.SettingOrigin`.

        Returns:
            The value, then the layer that supplied it, then an edited marker.
        """
        from living_ink.settings import SOURCE_DEFAULT, SOURCE_ENV

        text = origin.display()
        if origin.source == SOURCE_DEFAULT:
            note = ui.dim("(default)")
        elif origin.source == SOURCE_ENV:
            note = ui.yellow(f"({origin.origin_detail})")
        else:
            note = ui.dim(f"({origin.source})")
        marker = ui.cyan("  • edited") if setting.field in self.edits else ""
        return f"{text}  {note}{marker}"

    # -- one setting --------------------------------------------------------

    def edit(self, setting: Setting) -> None:
        """Ask for a new value and record it, or record nothing.

        Args:
            setting: The setting to change.
        """
        origin = self.origins()[setting.field]
        console("")
        console(f"{ui.bold(label_for(setting))} — {setting.help}")
        console(ui.dim(f"  currently {origin.display()} ({origin.source})"))
        console(ui.dim(f"  default   {render_default(setting)}"))

        answer = (
            self._edit_secret(setting)
            if setting.store == STORE_CREDENTIALS
            else self._edit_value(setting, origin.value)
        )
        if answer is None:
            console(ui.dim("  Unchanged."))
            return

        self.edits[setting.field] = answer
        self._warn_if_shadowed(setting)

    def _edit_value(self, setting: Setting, current: Any) -> Any:
        """Ask for a config-file value with the widget its kind calls for.

        Args:
            setting: The setting to change.
            current: Its currently effective value, used as the prompt default.

        Returns:
            The new value, :data:`REMOVED_VALUE` to fall back to the default,
            or None if the user cancelled.
        """
        if setting.kind == FLAG:
            return ui.confirm(f"{label_for(setting)}?", default=bool(current))

        if setting.kind == CHOICE:
            rows = [ui.Choice(c.value, c.label or c.value) for c in setting.choices]
            rows.append(ui.Choice(RESET, f"Use the default ({render_default(setting)})"))
            picked = ui.select("Which one?", rows, default=str(current))
            return None if picked is None else _reset_or(picked)

        if setting.kind == LIST:
            return self._edit_list(setting, current)

        if setting.kind == PATH:
            typed = ui.path("Path (empty to use the default)", default=str(current or ""))
        else:
            typed = ui.text(
                "New value (empty to use the default)",
                default="" if current is None else str(current),
                validate=validator_for(setting),
            )
        if typed is None:
            return None
        typed = typed.strip()
        return REMOVED_VALUE if not typed else coerce(setting, typed)

    def _edit_list(self, setting: Setting, current: Any) -> Any:
        """Ask for a list value, by ticking or by typing.

        A list with declared choices is a checkbox, because the set of answers
        is knowable and typing one of them out is a chance to misspell it. A
        list without them — the exclude folders — is free text, and a comma is
        the separator an environment variable already has to use.

        Args:
            setting: The setting to change.
            current: Its currently effective value.

        Returns:
            The new tuple, :data:`REMOVED_VALUE`, or None if cancelled.
        """
        held = tuple(current or ())
        if setting.choices:
            rows = [ui.Choice(c.value, c.label or c.value) for c in setting.choices]
            picked = ui.checkbox("Tick the ones to include", rows, selected=held)
            return None if picked is None else picked
        typed = ui.text("Comma-separated (empty to use the default)", default=", ".join(held))
        if typed is None:
            return None
        items = tuple(part.strip() for part in typed.split(",") if part.strip())
        return items if items else REMOVED_VALUE

    def _edit_secret(self, setting: Setting) -> Any:
        """Offer to replace or remove a credential, never to display one.

        A blank password is not "remove": a user who pressed Enter by mistake
        must not silently lose a key that costs a trip to a website to replace.
        Removing one is its own menu row.

        Args:
            setting: The credential setting to change.

        Returns:
            The new secret, :data:`REMOVED_VALUE` to delete it, or None.
        """
        if self.credential_name(setting) is None:
            # Said here rather than only at the save, because here is where the
            # user can still go and set the provider before typing the key.
            console(ui.yellow("  ⚠ The API key is kept per provider; set ai_provider first."))

        rows = [
            ui.Choice("set", "Enter a new value"),
            ui.Choice(RESET, "Remove the stored value"),
            ui.Choice(BACK, "Leave it alone"),
        ]
        picked = ui.select("What would you like to do?", rows)
        if picked in (None, BACK):
            return None
        if picked == RESET:
            return REMOVED_VALUE
        typed = ui.password(f"{label_for(setting)}")
        if typed is None:
            return None
        typed = typed.strip()
        if not typed:
            console(ui.yellow("  Nothing entered; the stored value is untouched."))
            return None
        return typed

    def _warn_if_shadowed(self, setting: Setting) -> None:
        """Say so when the edit will be overridden by something stronger.

        Args:
            setting: The setting just changed.
        """
        from living_ink.settings import SOURCE_CONFIG, SOURCE_CREDENTIALS, SOURCE_ENV

        origin = self.origins()[setting.field]
        expected = SOURCE_CREDENTIALS if setting.store == STORE_CREDENTIALS else SOURCE_CONFIG
        if self.edits[setting.field] is REMOVED_VALUE or origin.source == expected:
            console(ui.green(f"  → {origin.display()}"))
            return
        if origin.source == SOURCE_ENV:
            console(ui.yellow(f"  ⚠ {origin.origin_detail} is set and outranks the config file."))
            console(ui.dim(f"    Until you unset it, this stays {origin.display()}."))

    # -- prompts ------------------------------------------------------------

    def prompts_menu(self) -> None:
        """Open one of the two prompt files in the user's editor.

        The spec asks for the file to be *opened*, not for its path to be
        printed: a user told where a file is has been given homework, and the
        two prompts are the most useful thing in the product to tune.
        """
        from living_ink import clean

        files = (
            ("OCR", clean.OCR_PROMPT_FILE, "What the model is told when it reads a page."),
            ("Cleanup", clean.PROMPT_FILE, "What it is told when it tidies the text up."),
        )
        rows = [
            ui.Choice(str(path), f"{name:<8} {path.name}", description=note)
            for name, path, note in files
        ]
        rows.append(ui.Choice(BACK, "Back"))
        picked = ui.required(ui.select("Which prompt?", rows))
        if picked == BACK:
            return
        self.open_in_editor(Path(picked))

    def open_in_editor(self, target: Path) -> None:
        """Hand a file to ``$VISUAL``/``$EDITOR`` and wait for it.

        Args:
            target: The file to open.
        """
        command = editor_command()
        if command is None:
            console(ui.yellow("  No editor found. Set $EDITOR, or edit it by hand:"))
            console(ui.dim(f"    {target}"))
            return

        console(ui.dim(f"  Opening {target.name} in {command[0]}..."))
        try:
            subprocess.run([*command, str(target)], check=True)
        except (OSError, subprocess.CalledProcessError) as error:
            console(ui.yellow(f"  ⚠ {command[0]} did not finish cleanly: {error}"))
            console(ui.dim(f"    The file is at {target}"))
            return
        # The prompts are part of the transcription fingerprint, so an edit
        # correctly invalidates every cached page. Saying so here is the
        # difference between "the next sync is slow" and "something is wrong".
        console(ui.green("  ✓ Saved."))
        console(ui.dim("    Pages already transcribed will be read again on the next sync."))

    # -- advanced -----------------------------------------------------------

    def advanced_menu(self) -> None:
        """Offer the destructive maintenance the read-only ``info`` refuses.

        ``cache --clear`` and ``state --repair`` used to be flags on the
        commands that reported those things, which put "make it worse" one
        keystroke from "find out what is wrong". They are here instead, behind
        a menu and a confirmation each.
        """
        actions = (
            ("clear", "Clear the caches", "Delete every cached page render and transcription."),
            ("prune", "Prune old cache entries", "Delete only what is past its expiry."),
            ("check", "Check the sync database", "Read-only integrity check."),
            ("vacuum", "Compact the sync database", "Rebuild it to reclaim space."),
        )
        while True:
            rows = [ui.Choice(key, label, description=note) for key, label, note in actions]
            rows.append(ui.Choice(BACK, "Back"))
            picked = ui.required(ui.select("Advanced", rows))
            if picked == BACK:
                return
            self.run_advanced(picked)

    def run_advanced(self, action: str) -> None:
        """Carry out one maintenance action, after confirming it.

        Args:
            action: One of ``clear``, ``prune``, ``check`` or ``vacuum``.
        """
        from living_ink.cli import caches as caches_api

        if action == "clear":
            if not ui.confirm("Delete every cached render and transcription?", default=False):
                return
            for cache in caches_api.all_caches():
                console(ui.green(f"  ✓ Removed {cache.clear()} {cache.noun} entries."))
            console(
                ui.dim("    The next sync re-renders and re-transcribes; that costs API calls.")
            )
            return

        if action == "prune":
            for cache in caches_api.all_caches():
                console(ui.green(f"  ✓ Pruned {cache.prune()} expired {cache.noun} entries."))
            return

        self._database_action(action)

    def _database_action(self, action: str) -> None:
        """Check or compact ``state.db``.

        Args:
            action: ``check`` or ``vacuum``.
        """
        from living_ink import state
        from living_ink.cli import caches as caches_api

        path = caches_api.state_db_path()
        if not path.exists():
            console(ui.dim("  No sync database yet; nothing to do."))
            return
        if action == "vacuum" and not ui.confirm(f"Rebuild {path.name}?", default=False):
            return
        try:
            store = state.StateStore(path)
            if action == "check":
                result = store.integrity_check()
                ok = result.strip().lower() == "ok"
                console((ui.green if ok else ui.red)(f"  {'✓' if ok else '✗'} {result}"))
            else:
                store.vacuum()
                console(ui.green("  ✓ Compacted."))
        except Exception as error:  # noqa: BLE001 - sqlite raises several shapes
            console(ui.red(f"  ✗ {error}"))

    # -- leaving ------------------------------------------------------------

    def discard(self) -> Optional[bool]:
        """Leave without writing, warning first if that costs something.

        Returns:
            False once nothing is going to be written, True if the user changed
            their mind and saved instead, or None if they changed their mind
            twice and should be back in the menu.
        """
        if self.edits and not ui.confirm(
            f"Throw away {len(self.edits)} unsaved change{'' if len(self.edits) == 1 else 's'}?",
            default=False,
        ):
            return self.save()
        console(ui.dim("Nothing was written."))
        return False

    def save(self) -> Optional[bool]:
        """Show what will change, then write the file and the credentials.

        Returns:
            True if anything was written, False if there was nothing to write,
            or None if the user declined the confirmation — which is a request
            to keep editing, not to throw the edits away. Returning False for
            both made "no, not yet" close the menu and lose every change.
        """
        if not self.edits:
            console(ui.dim("No changes to save."))
            return False

        console("")
        console(ui.bold("These changes will be written:"))
        for line in self.change_lines():
            console(f"  {line}")
        console(ui.dim(f"  → {self.config_file}"))
        console(ui.dim("    Saving rewrites the file; hand-written comments are lost."))
        if not ui.confirm("Save?", default=True):
            return None

        self.write_config()
        self.write_credentials()
        return True

    def change_lines(self) -> List[str]:
        """Describe each pending change in one line, secrets masked.

        Returns:
            One line per edit, in schema order.
        """
        from living_ink.config.credentials import mask

        lines = []
        for setting in LIVE_SETTINGS:
            if setting.field not in self.edits:
                continue
            new = self.edits[setting.field]
            if new is REMOVED_VALUE:
                shown = ui.dim(f"removed (back to {render_default(setting)})")
            elif setting.secret:
                shown = mask(new)
            elif isinstance(new, bool):
                shown = "true" if new else "false"
            elif isinstance(new, tuple):
                shown = ", ".join(str(item) for item in new) or "(none)"
            else:
                shown = str(new)
            lines.append(f"{label_for(setting):<28} {shown}")
        return lines

    def write_config(self) -> None:
        """Write ``config.yml`` atomically, or say why it could not be.

        The mapping written is the file that was read plus this session's
        edits, so a section a plugin owns and a key the schema has never heard
        of both survive the save — :func:`~living_ink.config.render_config`
        preserves them under their own heading.
        """
        from living_ink import safeio
        from living_ink.config import SCHEMA_VERSION, render_config

        values = self.pending_config()
        values.setdefault("schema_version", SCHEMA_VERSION)
        try:
            self.config_file.parent.mkdir(parents=True, exist_ok=True)
            safeio.write_secret_atomic(self.config_file, render_config(values))
        except OSError as error:
            console(ui.red(f"  ✗ Could not write {self.config_file}: {error}"))
            return
        console(ui.green(f"✓ Saved {self.config_file}"))

    def write_credentials(self) -> None:
        """Store or delete each edited secret, one independent write each.

        Each is attempted on its own: failing to store an API key must not also
        cost the pairing token, which needs a trip to the website to redo.
        """
        from living_ink.config import credentials

        for setting in LIVE_SETTINGS:
            if setting.store != STORE_CREDENTIALS or setting.field not in self.edits:
                continue
            name = self.credential_name(setting)
            if name is None:
                console(ui.yellow(f"  ⚠ Nowhere to store {label_for(setting)}."))
                console(ui.dim("    The API key is kept per provider; set ai_provider first."))
                continue
            new = self.edits[setting.field]
            try:
                if new is REMOVED_VALUE:
                    removed = credentials.delete_secret(name, config_path=self.config_file)
                    console(
                        ui.green(f"  ✓ Removed {name}")
                        if removed
                        else ui.dim(f"  {name} was not stored.")
                    )
                else:
                    credentials.write_secret(name, new, config_path=self.config_file)
                    console(ui.green(f"  ✓ Stored {name} ({credentials.mask(new)})"))
            except (OSError, ValueError) as error:
                console(ui.yellow(f"  ⚠ Could not store {name}: {error}"))

    def credential_name(self, setting: Setting) -> Optional[str]:
        """Return the credential file a secret setting is stored under.

        The AI key is the one whose name is not fixed: it is kept per provider,
        so switching provider and back does not destroy the first key. The
        provider it is composed from is the **pending** one, so changing the
        provider and the key in one session stores the key against the provider
        the user just chose rather than the one they left.

        The composing is :func:`living_ink.settings._credential_name`'s and not
        this module's, deliberately. It is the function the *reader* uses, and
        a second copy here would be free to disagree with it — which is exactly
        what a naive ``ai_key_name(str(provider))`` did: with no provider set it
        composed ``ai.api_key.none`` and stored a key somewhere nothing looks,
        so the run that followed reported no API key moments after one was
        entered.

        Args:
            setting: A :data:`~living_ink.config.schema.STORE_CREDENTIALS`
                setting.

        Returns:
            The credential name, or None when there is nowhere to put it yet.
        """
        from living_ink.settings import _credential_name

        origins = self.origins()
        return _credential_name(setting, {"ai_provider": origins["ai_provider"].value})


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------


def settings_in(section: str) -> Tuple[Setting, ...]:
    """Return the live settings the menu files under a section.

    Not :func:`~living_ink.config.schema.settings_for_section`, which answers
    the narrower question the config *file* asks: it keys off the dotted path,
    so it cannot see a credential, which deliberately has no path.

    Args:
        section: Section name.

    Returns:
        The settings, in schema declaration order.
    """
    return tuple(s for s in LIVE_SETTINGS if s.menu_section == section)


def label_for(setting: Setting) -> str:
    """Return the name a setting is shown under.

    The **field** name, which is what ``living-ink info`` prints in its left
    column. The leaf key would read better inside a section — ``vault_path``
    rather than ``obsidian_vault_path`` under the ``obsidian`` heading — and it
    is the wrong answer anyway. A user comes to this menu holding a line they
    read in ``info``, and a menu that renamed the thing on the way in would
    make them search for a row that is not there under that name.

    Args:
        setting: The setting to name.

    Returns:
        The field name, i.e. the attribute on
        :class:`living_ink.settings.Settings`.
    """
    return setting.field


def render_default(setting: Setting) -> str:
    """Render a setting's shipped default for display.

    Args:
        setting: The setting to describe.

    Returns:
        A short printable form, never the value itself for a secret.
    """
    value = setting.default
    if setting.secret:
        return "not set"
    if value is None or value == "":
        return "not set"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (tuple, list)):
        return ", ".join(str(item) for item in value) or "not set"
    return str(value)


def validator_for(setting: Setting) -> Optional[ui.Validator]:
    """Return a keystroke validator for a setting whose kind constrains it.

    Args:
        setting: The setting being typed into.

    Returns:
        A validator, or None when any text is acceptable. An empty answer is
        always acceptable: it is how the user asks for the default back, and a
        validator that refused it would make a value un-unset.
    """
    if setting.kind not in (WHOLE, NUMBER):
        return None
    parse, complaint = (
        (int, "Enter a whole number.")
        if setting.kind == WHOLE
        else (
            float,
            "Enter a number.",
        )
    )

    def check(answer: str) -> Any:
        """Accept an empty answer or one that parses.

        Args:
            answer: What has been typed so far.

        Returns:
            True when acceptable, otherwise the problem to show.
        """
        if not answer.strip():
            return True
        try:
            parse(answer.strip())
        except ValueError:
            return complaint
        return True

    return check


def coerce(setting: Setting, typed: str) -> Any:
    """Turn typed text into the value the config file should hold.

    YAML would read ``5`` back as an integer and ``"5"`` as a string, so
    writing the typed string for a numeric setting produces a file whose value
    has a different type from the default it replaced.

    Args:
        setting: The setting being changed.
        typed: The stripped text the user entered, never empty.

    Returns:
        The value to store.
    """
    try:
        if setting.kind == WHOLE:
            return int(typed)
        if setting.kind == NUMBER:
            return float(typed)
    except ValueError:
        # The validator already refused this; a paste past it lands here and a
        # string is closer to what the user meant than a crash.
        logger.debug("Could not read %r as %s; storing as text", typed, setting.kind)
    return typed


def _reset_or(picked: str) -> Any:
    """Translate the reset row's sentinel into the removal sentinel.

    Args:
        picked: A choice value.

    Returns:
        :data:`REMOVED_VALUE` for the reset row, otherwise ``picked``.
    """
    return REMOVED_VALUE if picked == RESET else picked


def editor_command() -> Optional[Sequence[str]]:
    """Return the editor to open a file with, as an argument list.

    ``$VISUAL`` and ``$EDITOR`` are read with :func:`shlex.split`, because both
    conventionally carry arguments — ``code --wait``, ``subl -w`` — and a
    single-string exec would look for a program with a space in its name.

    Returns:
        The command and its arguments, or None if nothing is available.
    """
    from shutil import which

    for variable in ("VISUAL", "EDITOR"):
        declared = os.environ.get(variable, "").strip()
        if declared:
            parts = shlex.split(declared)
            if parts:
                return parts
    for candidate in FALLBACK_EDITORS:
        if which(candidate):
            return [candidate]
    return None


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


class ConfigCommand(BaseCommand):
    """``living-ink config`` — the settings menu."""

    name = "config"
    help = "Change settings, edit the prompts, clear the caches"
    description = (
        "An interactive menu over every setting Living Ink has. Nothing is "
        "written until you save, and the summary before the save names every "
        "change. Run 'living-ink info' to see the same settings without the "
        "ability to change them."
    )
    interactive = True

    @classmethod
    def register_args(cls, parser: argparse.ArgumentParser) -> None:
        """Register the command's flags — it has none.

        Every answer is a question the menu asks. A flag here would be a second
        way to set a setting, competing with the environment variable and the
        config key the schema already declares for it.

        Args:
            parser: Subparser to attach arguments to.
        """

    def run(self, args: argparse.Namespace) -> int:
        """Open the menu.

        Args:
            args: Parsed arguments, unused.

        Returns:
            0 whether the user saved or left; leaving is not a failure. 1 if
            the configuration could not be read at all.
        """
        import yaml

        from living_ink.config import get_config_path

        try:
            menu = ConfigMenu(self.root)
        except (OSError, TypeError, ValueError, yaml.YAMLError) as error:
            console(ui.red(f"Could not read {get_config_path(self.root)}: {error}"))
            console(ui.dim("Fix the file by hand, or delete it and run 'living-ink setup'."))
            return 1

        try:
            menu.run()
        except ui.Cancelled:
            if menu.edits:
                console("")
                console(
                    ui.yellow(f"Left with {len(menu.edits)} unsaved change(s); nothing written.")
                )
            raise
        return 0
