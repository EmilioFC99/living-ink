"""``living-ink setup`` — first run, from nothing to a working sync.

The *flow* is here; the *facts* are in :mod:`living_ink.setup_wizard`. Which
questions get asked, in what order, and what the answers mean is a property of
this command; how to detect an Obsidian vault, whether a token still works and
what a launch agent looks like are properties of the machine, and they stay
where every other caller can reach them.

Two rules shape everything below.

**Nothing reaches disk until the user confirms the summary.** Every answer
lands in :class:`Answers`, which is an ordinary object in memory. The old
wizard wrote the config, then stored the credentials, then installed the
launcher, then asked about background sync — so abandoning it at the last
question left a half-configured machine, and there was no point at which the
user could see what they had agreed to. :meth:`Wizard.commit` is the only code
here that writes anything, and it runs after one confirmation.

**Every question is a widget.** :mod:`living_ink.ui` owns the prompts, so the
wizard cannot invent its own input handling, cannot echo an API key, and cannot
disagree with the config menu about what a yes/no question looks like. A widget
returns ``None`` when the user cancels; :func:`living_ink.ui.required` turns
that into :class:`~living_ink.ui.Cancelled`, which is a ``KeyboardInterrupt``,
so leaving the wizard at question seven exits 130 like leaving anything else.
"""

import argparse
import logging
import platform
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from living_ink import ui
from living_ink.cli.base import BaseCommand
from living_ink.cli.commands.sync import SyncCommand
from living_ink.logs import console

logger = logging.getLogger(__name__)

#: Offered first, because a free key and a fast model is the shortest path from
#: "installed" to "a note in the vault". The rest are one keystroke further on.
HEADLINE_PROVIDERS = ("gemini", "openai", "ollama")

#: What a vault gets called when the user has no opinion.
DEFAULT_ROOT_FOLDER = "Living Ink"

#: Chosen in the folder menu to mean "not one of these". A byte no folder
#: name can contain, so it cannot collide with a real answer.
NEW_FOLDER = "\x00new"

#: The interval a background sync runs at when the user says yes. An hour is
#: short enough to feel automatic and long enough that a tablet left plugged in
#: is not re-listed every few minutes.
BACKGROUND_INTERVAL_SECONDS = 3600


@dataclass
class Answers:
    """Every decision the wizard collected, and not one of them on disk yet.

    This is the whole reason the commit can be atomic: a step's job is to fill
    in fields, never to act on them. A field left at its default is a question
    the flow did not need to ask — a Cloud-only setup never asks for an SSH
    host — so the defaults are the values a config would have had anyway.

    Attributes:
        preferred_connection: ``"ssh"`` or ``"cloud"``.
        use_ssh: Whether USB SSH is available as a transport at all.
        ssh_host: Address of the tablet over USB.
        ssh_port: SSH port on the tablet.
        remarkable_token: A freshly paired or reused Cloud token, or "".
        ai_provider: Provider preset name, ``"custom"``, or ``"none"``.
        ai_model: Model name, or "" for a provider that needs none.
        ai_base_url: Endpoint for a provider with no preset, or "".
        ai_key: The API key, held in memory only until :meth:`Wizard.commit`.
        obsidian_enabled: Whether to publish to Obsidian.
        obsidian_vault_path: Absolute path to the vault.
        obsidian_root_folder: Folder inside the vault, "" for the vault root.
        obsidian_mirror_folders: Whether to mirror the tablet's folder tree.
        background_sync: Whether to install the hourly launch agent.
        warnings: Things that did not verify, repeated in the summary so the
            user confirms them knowingly rather than having watched them
            scroll past ten questions ago.
    """

    preferred_connection: str = "ssh"
    use_ssh: bool = True
    ssh_host: str = ""
    ssh_port: int = 22
    remarkable_token: str = ""
    ai_provider: str = "gemini"
    ai_model: str = ""
    ai_base_url: str = ""
    ai_key: str = ""
    obsidian_enabled: bool = True
    obsidian_vault_path: str = ""
    obsidian_root_folder: str = DEFAULT_ROOT_FOLDER
    obsidian_mirror_folders: bool = True
    background_sync: bool = False
    warnings: List[str] = field(default_factory=list)


@dataclass
class WizardResult:
    """What the wizard did, for the command that has to return an exit code.

    Attributes:
        saved: Whether a configuration was written.
        run_sync_requested: Whether the user asked to sync straight away.
    """

    saved: bool = False
    run_sync_requested: bool = False


class Wizard:
    """The first-run conversation, in the order it is held.

    Attributes:
        root: Project root, for locating the config. None runs the full
            lookup.
        bin_dir: Where to install the ``living-ink`` launcher. None uses the
            default.
        answers: Everything collected so far.
    """

    def __init__(self, root: Optional[Path] = None, bin_dir: Optional[Path] = None) -> None:
        """Set up a wizard with nowhere written to yet.

        Args:
            root: Project root, for locating the config.
            bin_dir: Override for the launcher's install directory.
        """
        self.root = root
        self.bin_dir = bin_dir
        self.answers = Answers()

    # -- the flow ----------------------------------------------------------

    def run(self) -> WizardResult:
        """Ask everything, show what it adds up to, then write it once.

        Returns:
            What happened, for the caller to turn into an exit code.

        Raises:
            Cancelled: If the user pressed Ctrl+C at any prompt. It is a
                ``KeyboardInterrupt``, so it travels to ``main`` untouched and
                becomes exit 130 without this ever converting it.
        """
        self._welcome()
        self.step_connection()
        self.step_ai()
        self.step_destination()

        if not self.review():
            console("")
            console(ui.yellow("Nothing was written. Run 'living-ink setup' again when ready."))
            return WizardResult(saved=False)

        self.commit()
        self.estimate()

        console("")
        first = ui.confirm("Run your first sync now?", default=True)
        return WizardResult(saved=True, run_sync_requested=bool(first))

    def _welcome(self) -> None:
        """Print the banner, and say how long this is going to take."""
        console("")
        console(ui.bold(ui.cyan("Living Ink setup")))
        console(ui.dim("Three questions, a summary, and nothing is saved until you say so."))
        console("")

    # -- step 1: the tablet -------------------------------------------------

    def step_connection(self) -> None:
        """Ask how to reach the tablet, and check that it answers.

        A failed check is a warning, never a stop: a user who has not yet
        enabled the USB interface can still finish setting up and fix the cable
        afterwards, and refusing to save their AI key over it would be absurd.
        """
        from living_ink.config.schema import DEFAULT_SSH_HOST

        console(ui.bold("1. Your tablet"))
        choice = ui.required(
            ui.select(
                "How should Living Ink reach your reMarkable?",
                [
                    ui.Choice(
                        "ssh",
                        "USB cable",
                        "Free, fast, works offline. Needs the USB web interface on.",
                    ),
                    ui.Choice(
                        "cloud",
                        "reMarkable Cloud",
                        "Wireless. Needs a one-time pairing code from the website.",
                    ),
                ],
                default="ssh",
            )
        )

        self.answers.preferred_connection = choice
        if choice == "ssh":
            self._explain_usb()
            self.answers.use_ssh = True
            self.answers.ssh_host = ui.required(
                ui.text("SSH host", default=DEFAULT_SSH_HOST)
            ).strip()
            self._check_ssh()
            if ui.confirm("Also pair with the Cloud, as a fallback when unplugged?"):
                self.answers.remarkable_token = self._pair_cloud()
            return

        self.answers.remarkable_token = self._pair_cloud()
        self.answers.use_ssh = bool(
            ui.confirm("Also set up the USB cable, as a fallback when the Cloud is unreachable?")
        )
        if self.answers.use_ssh:
            self.answers.ssh_host = ui.required(
                ui.text("SSH host", default=DEFAULT_SSH_HOST)
            ).strip()
            self._check_ssh()
        else:
            self.answers.ssh_host = DEFAULT_SSH_HOST

    def _explain_usb(self) -> None:
        """Say what has to be switched on before the cable works."""
        console("")
        console("  On the tablet:")
        console(f"    1. Settings → Storage → turn {ui.bold('USB web interface')} on.")
        console("    2. Connect the tablet to this computer with a USB-C cable.")
        console(ui.dim("       (On a MacBook, the other port is worth trying.)"))
        console("")

    def _check_ssh(self) -> None:
        """Try the tablet over USB and report, without blocking the wizard."""
        from living_ink.setup_wizard import verify_remarkable_ssh

        console(ui.dim("  Checking the cable..."))
        ok, message = verify_remarkable_ssh(host=self.answers.ssh_host, port=self.answers.ssh_port)
        if ok:
            console(ui.green(f"  ✓ {message}"))
            return

        console(ui.yellow(f"  ⚠ {message}"))
        console(f"    Authorise this computer with: {ui.bold(self._ssh_copy_id())}")
        self.answers.warnings.append(f"USB: {message}")

    def _ssh_copy_id(self) -> str:
        """Return the command that authorises this computer on the tablet.

        Returns:
            An ``ssh-copy-id`` invocation naming the configured host.
        """
        from living_ink.config.schema import DEFAULT_SSH_USER

        return f"ssh-copy-id {DEFAULT_SSH_USER}@{self.answers.ssh_host}"

    def _pair_cloud(self) -> str:
        """Obtain a Cloud token, by reusing one or by pairing.

        Pairing is a network call and not a write: the token it returns is held
        in :class:`Answers` like every other answer and only stored by
        :meth:`commit`, so a user who backs out at the summary has not had a
        credential written behind them.

        Returns:
            A working token, or "" if the user skipped pairing.
        """
        from living_ink.setup_wizard import (
            get_existing_remarkable_token,
            pair_remarkable_device,
            verify_remarkable_token,
        )

        existing = get_existing_remarkable_token()
        reuse = existing and ui.confirm(
            "Reuse the reMarkable pairing already on this computer?", default=True
        )
        if reuse:
            console(ui.dim("  Checking it with reMarkable..."))
            ok, message = verify_remarkable_token(existing)
            if ok:
                console(ui.green(f"  ✓ {message}"))
                return existing
            console(ui.yellow(f"  ⚠ {message}"))

        console("")
        console("  To pair:")
        console(f"    1. Visit {ui.bold('https://my.remarkable.com/device/desktop/connect')}")
        console("    2. Sign in and copy the 8-letter code.")
        console("")

        while True:
            entered = ui.required(
                ui.text("8-letter code (or paste a token; empty to skip)")
            ).strip()
            if not entered:
                return ""

            if len(entered) == 8:
                console(ui.dim("  Pairing..."))
                ok, token, message = pair_remarkable_device(entered)
            else:
                console(ui.dim("  Checking the token..."))
                ok, message = verify_remarkable_token(entered)
                token = entered

            if ok:
                console(ui.green(f"  ✓ {message}"))
                return token

            console(ui.red(f"  ✗ {message}"))
            if not ui.confirm("Try again?", default=True):
                return ""

    # -- step 2: the model --------------------------------------------------

    def step_ai(self) -> None:
        """Ask which model reads the handwriting, and check the key works."""
        from living_ink.providers import PROVIDER_PRESETS

        console("")
        console(ui.bold("2. Reading your handwriting"))

        provider = ui.required(
            ui.select(
                "Which AI provider?",
                [
                    ui.Choice("gemini", "Google Gemini", "Free tier, fast, accurate. Recommended."),
                    ui.Choice("openai", "OpenAI", "GPT-4o and friends."),
                    ui.Choice("ollama", "Ollama", "Runs on this machine. No key, no cost."),
                    ui.Choice("other", "Something else", "Groq, OpenRouter, Mistral, Together…"),
                    ui.Choice("none", "None", "Publish raw OCR with no cleanup pass."),
                ],
                default="gemini",
            )
        )

        if provider == "other":
            provider = self._choose_other_provider()
        self.answers.ai_provider = provider

        if provider == "none":
            self.answers.ai_model = ""
            return

        preset = PROVIDER_PRESETS.get(provider, {})
        if not preset:
            self.answers.ai_base_url = ui.required(
                ui.text("API base URL (OpenAI-compatible)")
            ).strip()

        self.answers.ai_model = ui.required(
            ui.text("Model", default=str(preset.get("default_model", "")))
        ).strip()

        # Ollama is the one provider that authenticates by being local.
        if preset.get("auth_header") is None and provider in PROVIDER_PRESETS:
            console(ui.green("  No API key needed — Ollama runs here."))
            return

        self._ask_for_key()

    def _choose_other_provider(self) -> str:
        """Ask which of the remaining presets, or a custom endpoint.

        Returns:
            The chosen provider name.
        """
        from living_ink.providers import PROVIDER_PRESETS

        rest = [name for name in sorted(PROVIDER_PRESETS) if name not in HEADLINE_PROVIDERS]
        choices = [ui.Choice(name, name.capitalize()) for name in rest]
        choices.append(
            ui.Choice("custom", "Custom endpoint", "Anything that speaks the OpenAI chat API.")
        )
        return ui.required(ui.select("Which one?", choices))

    def _ask_for_key(self) -> None:
        """Ask for an API key until one is accepted, or accepted anyway.

        The key is never echoed and never written here. A key that fails its
        check can still be kept — a provider can be down, a quota can be
        exhausted for the day, and refusing to save a key the user knows is
        good would make the wizard unfinishable.
        """
        from living_ink.setup_wizard import verify_ai_provider

        while True:
            key = ui.required(ui.password(f"{self.answers.ai_provider} API key")).strip()
            if not key:
                console(ui.red("  An API key is required for this provider."))
                continue

            console(ui.dim("  Checking the key..."))
            ok, message = verify_ai_provider(self.answers.ai_provider, key, self.answers.ai_model)
            if ok:
                console(ui.green(f"  ✓ {message}"))
                self.answers.ai_key = key
                return

            console(ui.yellow(f"  ⚠ {message}"))
            if ui.confirm("Keep this key anyway?"):
                self.answers.ai_key = key
                self.answers.warnings.append(f"AI key: {message}")
                return

    # -- step 3: the vault --------------------------------------------------

    def step_destination(self) -> None:
        """Ask where the notes go, offering the vaults already on this Mac."""
        from living_ink.setup_wizard import detect_obsidian_vaults

        console("")
        console(ui.bold("3. Where the notes go"))

        self.answers.obsidian_enabled = bool(ui.confirm("Publish to Obsidian?", default=True))
        if not self.answers.obsidian_enabled:
            self.answers.warnings.append("No destination is enabled, so a sync will refuse to run.")
            return

        self.answers.obsidian_vault_path = self._choose_vault(detect_obsidian_vaults())
        self.answers.obsidian_root_folder = self._choose_root_folder()
        self.answers.obsidian_mirror_folders = bool(
            ui.confirm("Mirror the tablet's folder structure inside that folder?", default=True)
        )

    def _choose_vault(self, detected: List[dict]) -> str:
        """Pick a vault from the ones found, or ask for a path.

        Args:
            detected: Vaults read out of Obsidian's own config.

        Returns:
            An absolute path to the vault, as the user gave it.
        """
        if detected:
            choices = [ui.Choice(vault["path"], vault["name"], vault["path"]) for vault in detected]
            choices.append(ui.Choice("", "Somewhere else", "Type a path."))
            chosen = ui.required(ui.select("Which vault?", choices, default=detected[0]["path"]))
            if chosen:
                return chosen

        return ui.required(ui.path("Path to your Obsidian vault", must_exist=True)).strip()

    def _choose_root_folder(self) -> str:
        """Pick the folder inside the vault that Living Ink writes into.

        Returns:
            A folder name, or "" for the vault root.
        """
        from living_ink.setup_wizard import list_vault_folders

        existing = list_vault_folders(Path(self.answers.obsidian_vault_path).expanduser())
        ordered = [DEFAULT_ROOT_FOLDER] if DEFAULT_ROOT_FOLDER in existing else []
        ordered += [name for name in existing if name != DEFAULT_ROOT_FOLDER]

        choices = [ui.Choice(name, name, "Existing folder") for name in ordered]
        choices.append(ui.Choice(NEW_FOLDER, "Create a new folder", "Named by you."))
        choices.append(ui.Choice("", "The vault root", "No subfolder at all."))

        chosen = ui.required(
            ui.select(
                "Which folder inside the vault?",
                choices,
                default=DEFAULT_ROOT_FOLDER if ordered else NEW_FOLDER,
            )
        )
        if chosen != NEW_FOLDER:
            return chosen
        return ui.required(ui.text("New folder name", default=DEFAULT_ROOT_FOLDER)).strip()

    # -- the cut ------------------------------------------------------------

    def review(self) -> bool:
        """Show what is about to be written, and ask once.

        Returns:
            True if the user confirmed. False means nothing is written at all.
        """
        answers = self.answers
        console("")
        console(ui.bold("Ready to save"))
        for label, value in self._summary_rows():
            console(f"  {label.ljust(14)} {value}")

        if answers.warnings:
            console("")
            for warning in answers.warnings:
                console(ui.yellow(f"  ⚠ {warning}"))

        console("")
        if platform.system() == "Darwin":
            answers.background_sync = bool(ui.confirm("Sync automatically every hour?"))

        return bool(ui.confirm("Save this configuration?", default=True))

    def _summary_rows(self) -> List[Tuple[str, str]]:
        """Describe the pending configuration, one line per decision.

        Returns:
            ``(label, value)`` pairs, in the order the questions were asked.
        """
        from living_ink.config import credentials

        answers = self.answers
        transport = "USB cable" if answers.preferred_connection == "ssh" else "reMarkable Cloud"
        if answers.preferred_connection == "ssh" and answers.remarkable_token:
            transport += ", Cloud as fallback"
        elif answers.preferred_connection == "cloud" and answers.use_ssh:
            transport += ", USB as fallback"

        model = answers.ai_provider
        if answers.ai_model:
            model += f" / {answers.ai_model}"

        rows = [("Tablet", transport), ("Model", model)]
        if answers.ai_key:
            rows.append(("API key", credentials.mask(answers.ai_key)))
        if answers.remarkable_token:
            rows.append(("Pairing", credentials.mask(answers.remarkable_token)))

        if answers.obsidian_enabled:
            where = answers.obsidian_vault_path
            rows.append(("Vault", where))
            rows.append(("Folder", answers.obsidian_root_folder or ui.dim("(vault root)")))
            rows.append(("Structure", "mirrored" if answers.obsidian_mirror_folders else "flat"))
        else:
            rows.append(("Vault", "none — no destination enabled"))
        return rows

    # -- the write ----------------------------------------------------------

    def commit(self) -> Path:
        """Write the configuration, the credentials and the launcher.

        The order matters: the config file is what the credentials directory is
        derived from, so it goes first, and each credential is stored
        independently — failing to store the API key must not also cost the
        pairing, which needs a trip to the website to redo.

        Returns:
            The path the configuration was written to.
        """
        from living_ink import safeio
        from living_ink.config import credentials, get_config_path
        from living_ink.setup_wizard import (
            generate_config_yaml,
            install_cli_command,
            install_launch_agent,
        )

        answers = self.answers
        config_file = get_config_path(self.root)
        config_file.parent.mkdir(parents=True, exist_ok=True)

        # Owner-only and written in one step. The secrets have moved out, but a
        # config names a vault path and a tablet, and a half-written one would
        # lose the answers the user just gave.
        safeio.write_secret_atomic(
            config_file,
            generate_config_yaml(
                ai_provider=answers.ai_provider,
                ai_model=answers.ai_model,
                ai_base_url=answers.ai_base_url,
                preferred_connection=answers.preferred_connection,
                use_ssh=answers.use_ssh,
                ssh_host=answers.ssh_host,
                ssh_port=answers.ssh_port,
                obsidian_enabled=answers.obsidian_enabled,
                obsidian_vault_path=answers.obsidian_vault_path,
                obsidian_root_folder=answers.obsidian_root_folder,
                obsidian_mirror_folders=answers.obsidian_mirror_folders,
            ),
        )
        console("")
        console(ui.green(f"✓ Configuration saved to {ui.bold(str(config_file))}"))

        pending = [
            (credentials.ai_key_name(answers.ai_provider), answers.ai_key),
            (credentials.CLOUD_TOKEN, answers.remarkable_token),
        ]
        for name, secret in pending:
            if not secret:
                continue
            try:
                credentials.write_secret(name, secret, config_path=config_file)
            except (OSError, ValueError) as error:
                console(ui.yellow(f"  ⚠ Could not store {name}: {error}"))
                continue
            console(ui.green(f"  ✓ Stored {name} ({credentials.mask(secret)})"))

        ok, message = install_cli_command(repo_dir=self.root, bin_dir=self.bin_dir)
        if ok:
            console(ui.green(f"  ✓ {message}"))

        if answers.background_sync:
            ok, message = install_launch_agent(
                repo_dir=self.root, interval_seconds=BACKGROUND_INTERVAL_SECONDS
            )
            console(ui.green(f"  ✓ {message}") if ok else ui.yellow(f"  ⚠ {message}"))

        return config_file

    # -- the closing estimate -----------------------------------------------

    def estimate(self) -> None:
        """Say what the first sync will actually do, by asking the tablet.

        This is a real ``--preview``, not a guess: the same listing call and the
        same selector the run uses, so the counts printed here are the counts
        the next command acts on.

        What it deliberately does not do is put a price on it. Pages per
        document is unknowable before a single document has been read, no
        provider publishes a price table this can rely on, and an invented
        figure would be worse than none. What the user gets instead is the
        shape of the job and the one fact that makes the size bearable: an
        interrupted sync keeps every page it already paid for.
        """
        from living_ink.cli import inventory as inventory_api
        from living_ink.cli.app import LivingInkCLI
        from living_ink.pipeline import ConfigurationMissing

        console("")
        console(ui.dim("Asking the tablet what a first sync would do..."))

        args = LivingInkCLI(root=self.root).build_parser().parse_args(["sync", "--preview"])
        try:
            rows, _orphans, _device = inventory_api.compare_with_device(args, self.root)
        except (ConfigurationMissing, OSError) as error:
            console(ui.yellow(f"  ⚠ Could not reach the tablet: {error}"))
            console(ui.dim("  Run 'living-ink sync --preview' once it is connected."))
            return
        except Exception as error:  # pragma: no cover - transport-specific
            # A preview is a courtesy at the end of a successful setup. Nothing
            # about a transport failing here invalidates the config that was
            # just written, so it is reported and stepped over.
            logger.debug("Preview failed during setup", exc_info=True)
            console(ui.yellow(f"  ⚠ Could not reach the tablet: {error}"))
            return

        pending = [row for row in rows if row.get("pending")]
        folders = {row.get("folder") for row in pending if row.get("folder")}

        console("")
        console(ui.bold("Your first sync"))
        console(f"  {len(pending)} document{'' if len(pending) == 1 else 's'} to transcribe")
        console(f"  across {len(folders)} folder{'' if len(folders) == 1 else 's'}")
        console("")
        console(
            ui.dim(
                "  Cost is one AI call per handwritten page, and how many pages that is\n"
                "  is only knowable once they have been read. Transcriptions are cached,\n"
                "  so stopping with Ctrl+C costs the page in flight and nothing else."
            )
        )


class SetupCommand(BaseCommand):
    """Launch the interactive onboarding setup wizard."""

    name = "setup"
    help = "Launch the interactive setup wizard"
    description = "Configure reMarkable connection, AI provider, and note destinations."
    #: Every step is a prompt, so a terminal is a precondition rather than a
    #: thing to discover partway through. ``dispatch`` refuses without one.
    interactive = True

    @classmethod
    def register_args(cls, parser: argparse.ArgumentParser) -> None:
        """Register arguments for the setup wizard.

        Args:
            parser: Subparser to attach arguments to.
        """
        pass

    def run(self, args: argparse.Namespace) -> int:
        """Run the interactive setup wizard, then optionally the first sync.

        Args:
            args: Parsed arguments for setup.

        Returns:
            0 on completion, 1 if the user declined to save — ``setup`` exists
            to produce a configuration, and a caller that chains it into a sync
            must not treat "nothing written" as success — or the sync's own
            exit code if the user asked to sync straight away.
        """
        result = Wizard(root=self.root, bin_dir=getattr(args, "bin_dir", None)).run()
        if not result.saved:
            return 1
        if result.run_sync_requested:
            console("")
            sync = SyncCommand(root=self.root)
            # Config was just written; if it is still unusable, reporting the
            # problem beats looping back into the wizard that produced it.
            sync.offer_setup_on_missing_config = False
            return sync.run(args)
        return 0
