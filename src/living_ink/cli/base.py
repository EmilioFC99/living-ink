"""The contract every command implements.

A command owns its own flags and its own exit code, and the app knows nothing
about either. That is what makes adding a command a new module rather than an
edit to a dispatch table.
"""

import argparse
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional


class BaseCommand(ABC):
    """Abstract base class for all Living Ink CLI commands.

    Subclasses encapsulate argument definition (`register_args`) and execution
    logic (`run`) for a specific CLI subcommand.

    Attributes:
        name: Subcommand name used on the command line.
        help: Short one-line summary for CLI help listings.
        description: Extended description for command-specific help.
        interactive: Whether the command asks the user questions. Declared
            here, and checked once by :meth:`LivingInkCLI.dispatch` before the
            command runs, so the terminal test happens in one place instead of
            inside every step that happens to prompt. A command that says yes
            may assume a terminal; one that says no must never prompt.
        root: Optional repository root path.
    """

    name: str = ""
    help: str = ""
    description: Optional[str] = None
    interactive: bool = False

    def __init__(self, root: Optional[Path] = None) -> None:
        """Initialize command with an optional project root path.

        Args:
            root: Root path of the project or repository.
        """
        self.root = root

    @classmethod
    @abstractmethod
    def register_args(cls, parser: argparse.ArgumentParser) -> None:
        """Register command-specific CLI flags and options.

        Args:
            parser: Subparser to attach arguments to.
        """
        pass

    @abstractmethod
    def run(self, args: argparse.Namespace) -> int:
        """Execute the command logic.

        Args:
            args: Parsed command-line arguments namespace.

        Returns:
            Exit code (0 for success, non-zero for error).
        """
        pass
