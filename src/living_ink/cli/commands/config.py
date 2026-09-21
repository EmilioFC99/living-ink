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
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from living_ink import ui
from living_ink.cli.base import BaseCommand
from living_ink.config.schema import (
    ACTIVE,
    CHOICE,
    CRON,
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
CUSTOM_CRON = "\x00cron"

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
            Uncoloured, and that is not an oversight: this is a menu row, not
            console output. The provenance note used to be ``ui.dim`` and the
            marker ``ui.cyan``, which reached the terminal as a literal
            ``^[[2m(config file)^[[0m`` — see :func:`living_ink.ui.plain`,
            which now strips it centrally, and which is why a colour helper
            here would be dead weight rather than a fallback.
        """
        from living_ink.settings import SOURCE_DEFAULT, SOURCE_ENV

        text = origin.display()
        if origin.source == SOURCE_DEFAULT:
            note = "(default)"
        elif origin.source == SOURCE_ENV:
            note = f"({origin.origin_detail})"
        else:
            note = f"({origin.source})"
        marker = "  • edited" if setting.field in self.edits else ""
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

        if setting.kind == CRON:
            return self._edit_schedule(current)

        if setting.kind == LIST:
            return self._edit_list(setting, current)

        if setting.field == "ai_provider":
            return self._edit_ai_provider(setting, current)

        if setting.field == "ai_model":
            return self._edit_ai_model(setting, current)

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

    def _edit_ai_provider(self, setting: Setting, current: Any) -> Any:
        """Pick an AI provider and guide through setting its model and API key."""
        from living_ink.config import credentials
        from living_ink.config.credentials import ai_key_name
        from living_ink.providers import (
            PROVIDER_PRESETS,
            PROVIDER_REGISTRY,
            fetch_ollama_models,
        )

        provider_labels = {
            "gemini": "Google Gemini",
            "openai": "OpenAI",
            "ollama": "Ollama (local)",
            "groq": "Groq",
            "openrouter": "OpenRouter",
            "mistral": "Mistral AI",
            "together": "Together AI",
            "custom": "Custom endpoint",
            "none": "None (no AI cleanup pass)",
        }
        all_providers = sorted({*PROVIDER_PRESETS, *PROVIDER_REGISTRY, "custom", "none"})
        headline = ["gemini", "openai", "ollama"]
        ordered = [p for p in headline if p in all_providers]
        ordered += [p for p in all_providers if p not in headline and p not in ("custom", "none")]
        ordered += [p for p in ("custom", "none") if p in all_providers]

        custom_tag = "__custom__"
        rows = [ui.Choice(p, provider_labels.get(p, p.capitalize())) for p in ordered]
        rows.append(ui.Choice(custom_tag, "Other (enter provider name manually)"))
        rows.append(ui.Choice(RESET, f"Use the default ({render_default(setting)})"))

        picked = ui.select("Which AI provider?", rows, default=str(current or "gemini"))
        if picked is None:
            return None
        if picked == RESET:
            return REMOVED_VALUE
        if picked == custom_tag:
            typed = ui.text("Provider name", default=str(current or "custom"))
            if typed is None:
                return None
            typed = typed.strip()
            if not typed:
                return REMOVED_VALUE
            new_provider = typed
        else:
            new_provider = picked

        if new_provider.strip().lower() == str(current or "").strip().lower():
            return new_provider

        self.edits["ai_provider"] = new_provider

        # 1. Custom provider needs base URL
        if new_provider == "custom":
            pending_base = (
                self.edits.get("ai_base_url")
                or self.pending_config().get("ai", {}).get("base_url")
                or self.raw.get("ai", {}).get("base_url")
            )
            base_url = ui.text(
                "API base URL (OpenAI-compatible)",
                default=str(pending_base or "http://localhost:8000/v1"),
            )
            if base_url is not None and base_url.strip():
                self.edits["ai_base_url"] = base_url.strip()

        # 2. Model selection
        if new_provider == "none":
            self.edits["ai_model"] = ""
            console(ui.green("  No AI cleanup pass will be run."))
        elif new_provider == "ollama":
            base_url = (
                self.edits.get("ai_base_url")
                or self.pending_config().get("ai", {}).get("base_url")
                or self.raw.get("ai", {}).get("base_url")
            )
            installed = fetch_ollama_models(base_url)
            preset = PROVIDER_PRESETS.get("ollama", {})
            default_model = str(preset.get("default_model", "llama3.2"))
            if installed:
                custom_tag_model = "__custom__"
                choices = [ui.Choice(name, name) for name in installed]
                choices.append(ui.Choice(custom_tag_model, "Other (enter model name manually)"))
                default_choice = default_model if default_model in installed else installed[0]
                picked_model = ui.select("Which Ollama model?", choices, default=default_choice)
                if picked_model == custom_tag_model:
                    typed_model = ui.text("Model", default=default_model)
                    if typed_model is not None and typed_model.strip():
                        self.edits["ai_model"] = typed_model.strip()
                elif picked_model is not None:
                    self.edits["ai_model"] = str(picked_model).strip()
            else:
                prompt = f"Model ({default_model} recommended)"
                typed_model = ui.text(prompt, default=default_model)
                if typed_model is not None and typed_model.strip():
                    self.edits["ai_model"] = typed_model.strip()
        else:
            preset = PROVIDER_PRESETS.get(new_provider, {})
            default_model = str(preset.get("default_model", ""))
            prompt = f"Model ({default_model} recommended)" if default_model else "Model"
            typed_model = ui.text(prompt, default=default_model)
            if typed_model is not None and typed_model.strip():
                self.edits["ai_model"] = typed_model.strip()

        # 3. API key
        if new_provider in ("ollama", "none"):
            console(ui.green("  No API key needed."))
            self.edits.pop("ai_api_key", None)
        else:
            try:
                secret_name = ai_key_name(new_provider)
            except ValueError:
                secret_name = None

            if secret_name:
                existing_key = credentials.read_secret(secret_name, config_path=self.config_file)
                if existing_key:
                    console(
                        ui.green(f"  ✓ Existing API key found ({credentials.mask(existing_key)}).")
                    )
                    keep = ui.confirm("Keep this API key?", default=True)
                    if not keep:
                        typed_key = ui.password(f"{new_provider.capitalize()} API key")
                        if typed_key is not None and typed_key.strip():
                            self.edits["ai_api_key"] = typed_key.strip()
                    else:
                        self.edits.pop("ai_api_key", None)
                else:
                    typed_key = ui.password(f"{new_provider.capitalize()} API key")
                    if typed_key is not None and typed_key.strip():
                        self.edits["ai_api_key"] = typed_key.strip()

        return new_provider

    def _edit_ai_model(self, setting: Setting, current: Any) -> Any:
        """Edit the AI model, offering installed Ollama models if provider is Ollama."""
        from living_ink.providers import fetch_ollama_models

        pending = self.pending_config()
        provider = (
            self.edits.get("ai_provider")
            or pending.get("ai", {}).get("provider")
            or self.raw.get("ai", {}).get("provider")
            or "gemini"
        )
        if provider == "ollama":
            base_url = (
                self.edits.get("ai_base_url")
                or pending.get("ai", {}).get("base_url")
                or self.raw.get("ai", {}).get("base_url")
            )
            installed = fetch_ollama_models(base_url)
            if installed:
                custom_tag = "__custom__"
                choices = [ui.Choice(name, name) for name in installed]
                choices.append(ui.Choice(custom_tag, "Other (enter model name manually)"))
                default_val = str(current) if current in installed else installed[0]
                picked = ui.select("Which Ollama model?", choices, default=default_val)
                if picked is None:
                    return None
                if picked != custom_tag:
                    return str(picked).strip()

        typed = ui.text(
            "New value (empty to use the default)",
            default="" if current is None else str(current),
            validate=validator_for(setting),
        )
        if typed is None:
            return None
        typed = typed.strip()
        return REMOVED_VALUE if not typed else coerce(setting, typed)

    def _edit_schedule(self, current: Any) -> Any:
        """Pick a schedule from the common ones, or write a cron expression.

        Every option carries the moment it would next fire, and a typed
        expression is echoed back as the next three. That is the only feedback
        that distinguishes ``0 9 1 * *`` from ``0 9 * * 1``: both parse, both
        look like a morning schedule, and one of them runs twelve times a
        year. An expression that does not parse is refused here rather than
        saved and reported later by ``info``.

        Args:
            current: The effective expression, used to preselect a row.

        Returns:
            A cron expression, :data:`REMOVED_VALUE` to unset it, or None if
            the user cancelled.
        """
        from living_ink import scheduler

        held = str(current or "").strip()
        try:
            tz, tz_name = scheduler.resolve_timezone(self._timezone())
        except ValueError as problem:
            # A zone typed into the file by hand, or exported as
            # ``LIVING_INK_WATCH_TIMEZONE``, reaches here unvalidated. Falling
            # back to the host's is the only reading that lets the user carry
            # on: refusing would close the one screen that can fix it.
            console(ui.yellow(f"  {problem} Showing times in this machine's zone."))
            tz, tz_name = scheduler.resolve_timezone(None)
        now = datetime.now(tz)

        rows: List[ui.Choice] = []
        for label, expression in scheduler.SCHEDULE_PRESETS:
            if expression is None:
                rows.append(ui.Choice(RESET, "No schedule"))
                continue
            upcoming = scheduler.next_fire(expression, now, tz)
            rows.append(
                ui.Choice(
                    expression,
                    label,
                    description=f"next: {scheduler.format_moment(upcoming, tz)}",
                )
            )
        rows.append(ui.Choice(CUSTOM_CRON, "Write a cron expression…"))

        picked = ui.select(f"When? (times in {tz_name})", rows, default=held or RESET)
        if picked is None:
            return None
        if picked != CUSTOM_CRON:
            return _reset_or(picked)

        typed = ui.text(
            "Cron expression (minute hour day month weekday)",
            default=held,
            validate=_valid_cron,
        )
        if typed is None:
            return None
        typed = typed.strip()
        if not typed:
            return REMOVED_VALUE

        for moment in scheduler.next_fires(typed, now, tz, count=3):
            console(ui.dim(f"    fires {scheduler.format_moment(moment, tz)}"))
        return typed

    def _timezone(self) -> str:
        """Return the timezone the schedule is read in, edits included.

        Args:
            None.

        Returns:
            The pending or effective ``watch.timezone``, or "" for the host's.
        """
        if "watch_timezone" in self.edits:
            return str(self.edits["watch_timezone"] or "")
        origin = self.origins().get("watch_timezone")
        return str(getattr(origin, "value", "") or "")

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

        It opens what a *run* would read, resolved through ``ai.prompt_dir``,
        rather than the packaged copy. The two are the same until that setting
        is pointed somewhere, and while it is pointed somewhere the packaged
        copy is the one file an edit must not land in — an upgrade replaces the
        installed package, so an edit made there is an edit with an expiry date.
        """
        from living_ink import clean

        prompt_dir = self.origins()["ai_prompt_dir"].value
        ocr_path, cleanup_path = clean.prompt_paths(prompt_dir)
        files = (
            (
                "OCR",
                ocr_path,
                clean.OCR_PROMPT_FILE,
                "What the model is told when it reads a page.",
            ),
            (
                "Cleanup",
                cleanup_path,
                clean.PROMPT_FILE,
                "What it is told when it tidies the text up.",
            ),
        )
        rows = [
            ui.Choice(str(path), f"{name:<8} {path.name}", description=note)
            for name, path, _packaged, note in files
        ]
        rows.append(ui.Choice(BACK, "Back"))
        picked = ui.required(ui.select("Which prompt?", rows))
        if picked == BACK:
            return

        target = Path(picked)
        packaged = next(p for _n, path, p, _d in files if str(path) == picked)
        if prompt_dir and target == packaged:
            # ai.prompt_dir names a directory that does not hold this prompt
            # yet. Seeding it from the packaged copy is the difference between
            # editing the shipped prompt and starting your own from a blank
            # file: neither is what the user asked for by setting the key.
            target = Path(prompt_dir).expanduser() / packaged.name
            if not self._seed_prompt(packaged, target):
                return
        self.open_in_editor(target)

    def _seed_prompt(self, packaged: Path, target: Path) -> bool:
        """Copy the packaged prompt to the user's prompt directory.

        Args:
            packaged: The prompt that ships inside the installed package.
            target: Where ``ai.prompt_dir`` says the user's copy belongs.

        Returns:
            True if ``target`` now holds a prompt to edit.
        """
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(packaged.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError as error:
            console(ui.yellow(f"  ⚠ Could not create {target}: {error}"))
            console(ui.dim("    Check that ai.prompt_dir names a directory you can write to."))
            return False
        console(ui.dim(f"  Copied the shipped prompt to {target}."))
        return True

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


def _valid_cron(answer: str) -> Any:
    """Accept an empty answer, or one ``croniter`` can read.

    A keystroke validator rather than a check after the fact: a schedule that
    is rejected on save has already cost the user the typing, and a schedule
    that is *saved* invalid is a watcher that refuses to start.

    Args:
        answer: What is typed so far.

    Returns:
        True when acceptable, otherwise the problem to show.
    """
    from living_ink import scheduler

    if not answer.strip():
        return True
    return scheduler.validate_expression(answer.strip()) or True


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
