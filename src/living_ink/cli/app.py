"""Parser assembly, command registration, and the process exit code.

``main`` is the only thing that exits; a command returns a code and the app
decides what to do with it.
"""

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Optional, Type

from living_ink.cli.base import BaseCommand
from living_ink.cli.commands.config import ConfigCommand
from living_ink.cli.commands.info import InfoCommand
from living_ink.cli.commands.setup import SetupCommand
from living_ink.cli.commands.sync import SyncCommand
from living_ink.cli.commands.uninstall import UninstallCommand
from living_ink.cli.commands.watch import WatchCommand
from living_ink.cli.flags import register_global_flags
from living_ink.config import get_config_path, read_config_file
from living_ink.settings import Settings

logger = logging.getLogger(__name__)


def resolve_output_settings(args: argparse.Namespace, root: Optional[Path] = None) -> Settings:
    """Work out how loud this run should be, from every layer that has a say.

    ``output.verbosity`` is an ordinary setting, so ``--quiet`` is the top
    layer of a ladder and not the whole of it: a config file that says
    ``verbosity: quiet`` and an exported ``LIVING_INK_VERBOSITY`` have to be
    honoured by a bare ``living-ink sync`` too. Reading only the flag made the
    key inert — declared, documented, reported by ``status``, and ignored.

    Args:
        args: Parsed arguments. Read with ``getattr`` defaults: this runs
            before dispatch, for every command, including ones with no output
            flags at all.
        root: Project root, for pinning config lookup in tests.

    Returns:
        The resolved settings. A config that cannot be parsed resolves from
        the flags and the environment alone — the command about to run reports
        the parse error properly, and this is the one caller that cannot,
        because logging is what it is about to configure.
    """
    flags = {
        field: getattr(args, field, None)
        for field in ("verbosity", "output_json")
        if getattr(args, field, None) is not None
    }
    config_path = get_config_path(root)
    try:
        raw = read_config_file(config_path)
    except Exception:
        logger.debug("Could not read %s while configuring logging", config_path, exc_info=True)
        raw = {}
    return Settings.resolve(raw, flags=flags, config_path=config_path)


def configure_logging(args: argparse.Namespace, root: Optional[Path] = None) -> None:
    """Install the package log handlers for this invocation.

    Done once here rather than per command, so the modules that know most
    about a failure — providers, transports, the renderer — reach the log file
    no matter which subcommand is running.

    Args:
        args: Parsed arguments; ``--verbose`` and ``--quiet`` arrive as the
            one ``verbosity`` value they both set.
        root: Project root, forwarded to the config lookup.
    """
    from living_ink import logs

    settings = resolve_output_settings(args, root)
    logs.configure(
        logs.LOG_PATH,
        verbose=settings.verbosity == "verbose",
        quiet=settings.verbosity == "quiet",
        # ``info`` registers a ``--json`` of its own, with no setting behind
        # it, because it prints a document rather than a run report. It keeps
        # stdout clean the same way.
        json_output=settings.output_json or getattr(args, "json", False),
    )


def add_verbosity_args(parser: argparse.ArgumentParser) -> None:
    """Add the ``--verbose`` / ``--quiet`` pair to a parser.

    Generated from ``output.verbosity``, so the two flags and the config key
    are one declaration: a run told to be quiet by the file and a run told so
    on the command line reach the same setting, and neither can exist without
    the other.

    Args:
        parser: Parser or subparser to extend.
    """
    register_global_flags(parser.add_argument_group("output"))


class LivingInkCLI:
    """Unified command-line interface orchestrator for Living Ink.

    Manages command registration, argument parsing, configuration path
    resolution, and subcommand dispatching.

    Attributes:
        root: Project root directory Path.
        commands: Dictionary mapping command names to BaseCommand classes.
    """

    #: The six commands 1.0 ships, in the order ``--help`` lists them: what a
    #: user does daily first, then what they set up, and ``uninstall`` last
    #: because it is the one nobody is looking for until they are.
    DEFAULT_COMMANDS: list[Type[BaseCommand]] = [
        SyncCommand,
        WatchCommand,
        SetupCommand,
        InfoCommand,
        ConfigCommand,
        UninstallCommand,
    ]

    def __init__(
        self,
        root: Optional[Path] = None,
        commands: Optional[list[Type[BaseCommand]]] = None,
    ) -> None:
        """Initialize CLI with optional root path and command classes.

        Args:
            root: Project root path. Leave None to let ``get_config_path()``
                run its full XDG resolution; passing a root pins config lookup
                to that directory, which is mainly useful in tests. Use
                :func:`living_ink.config.find_repo_root` to discover one.
            commands: Optional list of BaseCommand subclasses to register.
        """
        self.root = root
        self.commands: dict[str, Type[BaseCommand]] = {}
        for cmd_cls in commands or self.DEFAULT_COMMANDS:
            self.register_command(cmd_cls)

    def register_command(self, cmd_cls: Type[BaseCommand]) -> None:
        """Register a new command subclass.

        Args:
            cmd_cls: BaseCommand subclass to register.

        Raises:
            TypeError: If cmd_cls does not subclass BaseCommand.
        """
        if not issubclass(cmd_cls, BaseCommand):
            raise TypeError(f"{cmd_cls} must subclass BaseCommand")
        self.commands[cmd_cls.name] = cmd_cls

    def build_parser(self) -> argparse.ArgumentParser:
        """Construct the CLI argument parser with all registered subcommands.

        Returns:
            Configured argparse.ArgumentParser instance.
        """
        from living_ink import __version__

        parser = argparse.ArgumentParser(
            prog="living-ink",
            description="Sync handwritten reMarkable notebooks to an Obsidian vault.",
            # Documented rather than merely implemented: these are what a
            # scheduler, a supervisor and a shell script branch on, so they
            # are part of the interface. 2 is both a usage error and
            # argparse's own code for one; the overlap is deliberate.
            epilog=(
                "exit codes:\n"
                "  0   success\n"
                "  1   failure — something the user has to fix\n"
                "  2   usage error\n"
                "  130 interrupted with Ctrl+C\n"
            ),
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        parser.add_argument(
            "-v",
            "--version",
            action="version",
            version=f"%(prog)s {__version__}",
        )
        parser.add_argument(
            "-c",
            "--config",
            help="Path to custom config.yml file",
        )

        # Attached to the top-level parser *and* to every subparser, so both
        # `living-ink --verbose sync` and `living-ink sync --verbose` work;
        # people reach for the second form and argparse does not allow it
        # otherwise.
        add_verbosity_args(parser)

        subparsers = parser.add_subparsers(dest="command", help="Available commands")

        for cmd_name, cmd_cls in self.commands.items():
            subparser = subparsers.add_parser(
                cmd_name,
                help=cmd_cls.help,
                description=cmd_cls.description or cmd_cls.help,
            )
            add_verbosity_args(subparser)
            cmd_cls.register_args(subparser)

        return parser

    def dispatch(self, args: argparse.Namespace) -> int:
        """Dispatch parsed arguments to the appropriate command handler.

        Args:
            args: Parsed command-line arguments.

        Returns:
            Exit code integer.
        """
        if getattr(args, "config", None):
            os.environ["LIVING_INK_CONFIG"] = str(Path(args.config).resolve())

        configure_logging(args, self.root)

        if args.command is None:
            # Default behavior: if config exists, sync; otherwise setup
            config_file = get_config_path(self.root)
            cmd_cls = (
                self.commands.get("sync", SyncCommand)
                if config_file.exists()
                else self.commands.get("setup", SetupCommand)
            )
            return self._start(cmd_cls, args)

        cmd_cls = self.commands.get(args.command)
        if cmd_cls:
            return self._start(cmd_cls, args)

        return 1

    def _start(self, cmd_cls: Type[BaseCommand], args: argparse.Namespace) -> int:
        """Run a command, refusing an interactive one with nowhere to ask.

        The terminal test lives here, at the boundary, and not inside the
        wizard: a command that discovers halfway through step three that it
        cannot ask anything has already written a config directory, printed a
        banner and burned a network call. Refusing before the first step is
        also the only way the refusal can be a clean exit code rather than an
        ``EOFError`` traceback out of a prompt reading a closed pipe — which is
        what ``living-ink setup < /dev/null`` in a Dockerfile used to produce.

        Args:
            cmd_cls: The command class to instantiate and run.
            args: Parsed command-line arguments.

        Returns:
            The command's exit code, or 2 when an interactive command has no
            terminal — a usage error, because the user asked for the wrong
            command rather than for something that failed.
        """
        from living_ink import ui

        if cmd_cls.interactive and not ui.is_tty():
            print(f"living-ink {cmd_cls.name}: {ui.NO_TTY_MESSAGE}", file=sys.stderr)
            return 2
        return cmd_cls(root=self.root).run(args)

    def run(self, argv: Optional[list[str]] = None) -> int:
        """Parse arguments and run the corresponding command.

        Args:
            argv: Optional list of CLI argument strings (defaults to sys.argv[1:]).

        Returns:
            Exit code integer.
        """
        parser = self.build_parser()
        args = parser.parse_args(argv)
        return self.dispatch(args)


def main(argv: Optional[list[str]] = None) -> int:
    """Main CLI entry point, and the only place the process exits.

    The four codes — 0 success, 1 failure, 2 usage error, 130 interrupt — are
    decided here and nowhere else. A command returns one; it never calls
    :func:`sys.exit`, because ``watch`` runs ``sync`` in a loop and a library
    caller runs both, and neither can catch a process exit.

    Args:
        argv: Optional list of CLI arguments (defaults to sys.argv[1:]).

    Returns:
        Exit code integer.
    """
    cli = LivingInkCLI()
    try:
        code = cli.run(argv)
    except KeyboardInterrupt:
        # Ctrl+C is how a user ends a long sync or a watch, and every loop
        # below re-raises rather than converting it. A traceback would suggest
        # something broke; 130 is what a shell expects from SIGINT, and what a
        # supervisor can be told not to restart.
        sys.exit(130)
    if isinstance(code, int) and code != 0:
        sys.exit(code)
    return code or 0


if __name__ == "__main__":
    main()
