"""``living-ink completions`` — the interface, written out for a shell.

It prints and nothing else. Installing a completion script means writing to a
directory whose location depends on the shell, the platform and the package
manager, and getting it wrong leaves a stale file shadowing the right one
forever; a redirect is something the user can see, repeat and undo. So the
output is the product, the installation line is a comment inside it, and the
command has no ``--install``.
"""

import argparse

from living_ink.cli import completions
from living_ink.cli.base import BaseCommand


class CompletionsCommand(BaseCommand):
    """Print the completion script for one shell.

    The script is generated from the parser this very process built, so it
    describes the version that is installed rather than the version that was
    current when somebody last hand-edited a file of flags.
    """

    name = "completions"
    help = "Print the tab-completion script for your shell"
    description = (
        "Print a tab-completion script for bash, zsh or fish. "
        "Generated from this version's own commands and flags, so it is never out of date. "
        "Redirect it to the file named in the comment at the top."
    )

    @classmethod
    def register_args(cls, parser: argparse.ArgumentParser) -> None:
        """Register the one argument: which shell.

        Args:
            parser: Subparser to attach arguments to.
        """
        parser.add_argument(
            "shell",
            choices=list(completions.SHELLS),
            help="The shell to generate for",
        )

    def run(self, args: argparse.Namespace) -> int:
        """Write the script to stdout.

        Args:
            args: Parsed arguments; ``shell`` is the only one read.

        Returns:
            0. There is nothing here that can fail — no config is read, no
            file is written, nothing is contacted — which is also why this is
            the one command that works before ``setup`` has ever run.
        """
        # Imported here rather than at module scope: the app imports every
        # command to build its parser, and the app is what this would import
        # back. Function-level is the same answer the renderers give.
        from living_ink.cli.app import LivingInkCLI

        parser = LivingInkCLI(root=self.root).build_parser()
        # print, not ``console``: this is a document on stdout, and the log
        # layer is free to be quiet, prefixed or coloured.
        print(completions.render(parser, args.shell), end="")
        return 0
