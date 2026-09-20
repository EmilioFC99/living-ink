"""Tests for the generated shell completion scripts.

Three questions, and only the first is about any particular shell.

**Is it generated?** Every assertion that a flag appears is worthless if the
flag was typed into this module too, so the tests that check the walk build
their own parser and add a flag nothing in Living Ink has. A completion script
that goes stale is the failure this row exists to prevent, and a test that
hard-codes the flag list is the way that failure gets in.

**Is it valid?** ``bash -n``, ``zsh -n`` and ``fish --no-execute`` parse the
generated script without running it, which is the only check that catches an
unescaped quote in a help string. bash is everywhere; the other two are skipped
where they are not installed, which on CI means zsh runs on macOS and fish
usually runs nowhere — so the escaping tests below assert on the text as well,
rather than leaning on an interpreter that may not be there.

**Is it safe?** No secret has a flag, so none can reach a script. That is
enforced in :mod:`living_ink.cli.flags`, and asserted here against the real
parser because it is the kind of thing a later flag could quietly undo.
"""

import argparse
import shutil
import subprocess

import pytest

from living_ink.cli import completions
from living_ink.cli.app import LivingInkCLI
from living_ink.cli.commands.completions import CompletionsCommand


@pytest.fixture
def parser():
    """Return the real top-level parser, with every command registered."""
    return LivingInkCLI().build_parser()


@pytest.fixture
def spec(parser):
    """Return the described form of the real parser."""
    return completions.describe(parser)


def toy_parser():
    """Build a small parser holding one of each thing a renderer must handle.

    Returns:
        A parser with a global switch, one subcommand, a value-taking flag, a
        flag with choices, a path flag and a positional with choices — none of
        which exists in Living Ink, so a renderer cannot pass by coincidence.
    """
    parser = argparse.ArgumentParser(prog="toy", description="A toy.")
    parser.add_argument("--loud", action="store_true", help="Be loud")
    subparsers = parser.add_subparsers(dest="command")
    sub = subparsers.add_parser("jump", description="Jump over it. Twice, if asked.")
    sub.add_argument("--height", help="How high")
    sub.add_argument("--style", choices=["hop", "leap"], help="Which way")
    # ``config`` is the one dest the module treats as a path without the
    # schema saying so, which makes it the one worth pinning here.
    sub.add_argument("--config", help="Where from")
    sub.add_argument("target", choices=["fence", "puddle"], help="What to jump")
    return parser


class TestTheWalk:
    """What :func:`completions.describe` reads off a parser."""

    def test_every_command_is_described(self, spec):
        """The spec names every command the CLI registers, and no others."""
        assert set(spec.command_names) == set(LivingInkCLI().commands)

    def test_a_command_keeps_the_order_help_lists_it_in(self, spec):
        """Commands arrive in registration order, not alphabetically."""
        assert spec.command_names[0] == "sync"
        assert spec.command_names[-1] == "uninstall"

    def test_a_flag_the_program_does_not_have_is_still_described(self):
        """The walk reads the parser, so an invented flag appears too."""
        described = completions.describe(toy_parser())
        jump = next(c for c in described.commands if c.name == "jump")
        assert "--height" in {s for option in jump.options for s in option.spellings}

    def test_a_switch_is_told_apart_from_a_valued_flag(self):
        """``nargs == 0`` is what says a flag takes nothing after it."""
        described = completions.describe(toy_parser())
        loud = next(o for o in described.options if "--loud" in o.spellings)
        assert loud.takes == completions.SWITCH

    def test_a_choice_carries_its_words(self):
        """A flag with choices completes them, not nothing."""
        described = completions.describe(toy_parser())
        jump = next(c for c in described.commands if c.name == "jump")
        style = next(o for o in jump.options if "--style" in o.spellings)
        assert style.takes == completions.CHOICES
        assert style.choices == ("hop", "leap")

    def test_a_path_setting_completes_filenames(self, spec):
        """A PATH-kind setting's flag is marked for file completion."""
        sync = next(c for c in spec.commands if c.name == "sync")
        vault = next(o for o in sync.options if "--destination" in o.spellings)
        assert vault.takes == completions.FILE

    def test_a_value_nothing_can_list_completes_nothing(self, spec):
        """A notebook title lives on the tablet, so no word is offered."""
        sync = next(c for c in spec.commands if c.name == "sync")
        notebook = next(o for o in sync.options if "--notebook" in o.spellings)
        assert notebook.takes == completions.OPAQUE

    def test_a_positional_choice_becomes_a_completable_word(self, spec):
        """``completions <TAB>`` offers the shells it can write."""
        command = next(c for c in spec.commands if c.name == "completions")
        assert set(command.words) == set(completions.SHELLS)

    def test_a_positional_without_choices_offers_nothing(self):
        """A free-text positional adds no words rather than a guess."""
        parser = argparse.ArgumentParser(prog="toy")
        subparsers = parser.add_subparsers(dest="command")
        sub = subparsers.add_parser("open", description="Open it.")
        sub.add_argument("path", help="What to open")
        described = completions.describe(parser)
        assert described.commands[0].words == ()

    def test_a_hidden_flag_is_left_out(self):
        """``help=SUPPRESS`` means nobody is meant to find it, including here."""
        parser = argparse.ArgumentParser(prog="toy")
        parser.add_argument("--secret-door", help=argparse.SUPPRESS)
        described = completions.describe(parser)
        assert "--secret-door" not in {s for o in described.options for s in o.spellings}

    def test_a_command_is_summarised_in_one_sentence(self, spec):
        """A menu line is one sentence, not the whole ``--help`` paragraph."""
        watch = next(c for c in spec.commands if c.name == "watch")
        assert watch.description == "Sync on the schedule set in config.yml, until stopped."

    def test_a_parser_with_no_subcommands_describes_fine(self):
        """The walk does not assume a subparsers action exists."""
        parser = argparse.ArgumentParser(prog="bare")
        parser.add_argument("--loud", action="store_true", help="Be loud")
        described = completions.describe(parser)
        assert described.commands == ()
        assert described.prog == "bare"


class TestEveryShell:
    """What all three scripts have to say, whatever their syntax."""

    @pytest.mark.parametrize("shell", completions.SHELLS)
    def test_every_command_appears(self, parser, shell):
        """A command the CLI registers is completable."""
        script = completions.render(parser, shell)
        for name in completions.describe(parser).command_names:
            assert name in script

    @pytest.mark.parametrize("shell", completions.SHELLS)
    def test_every_flag_appears(self, parser, shell):
        """No flag the parser accepts is missing from the script."""
        described = completions.describe(parser)
        spellings = {s for o in described.options for s in o.spellings}
        for command in described.commands:
            spellings |= {s for o in command.options for s in o.spellings}
        script = completions.render(parser, shell)
        missing = {s for s in spellings if s.lstrip("-") not in script}
        assert not missing

    @pytest.mark.parametrize("shell", completions.SHELLS)
    def test_a_new_flag_shows_up_without_editing_anything(self, shell):
        """The script is generated, which is the whole point of the row.

        Matched undashed, because fish spells a flag ``-l height`` and the
        question here is whether the walk found it, not how it is written.
        """
        assert "height" in completions.render(toy_parser(), shell)

    @pytest.mark.parametrize("shell", completions.SHELLS)
    def test_the_install_line_is_inside_the_script(self, parser, shell):
        """Instructions survive a redirect, so they are comments."""
        script = completions.render(parser, shell)
        assert script.startswith("#") or script.splitlines()[1].startswith("#")
        assert f"living-ink completions {shell}" in script

    @pytest.mark.parametrize("shell", completions.SHELLS)
    def test_no_credential_reaches_a_script(self, parser, shell):
        """A secret has no flag, so it cannot be completed into a shell."""
        script = completions.render(parser, shell)
        assert "api-key" not in script
        assert "api_key" not in script

    def test_an_unknown_shell_is_refused(self, parser):
        """A library caller gets a named error, not a KeyError."""
        with pytest.raises(ValueError, match="powershell"):
            completions.render(parser, "powershell")


class TestBash:
    """The bash function, which is the one script CI can always parse."""

    def test_it_parses(self, parser, tmp_path):
        """``bash -n`` accepts the generated script."""
        script = tmp_path / "living-ink.bash"
        script.write_text(completions.render(parser, "bash"))
        subprocess.run(["bash", "-n", str(script)], check=True)

    def test_a_toy_parser_also_parses(self, tmp_path):
        """Escaping holds for a parser this module has never seen."""
        script = tmp_path / "toy.bash"
        script.write_text(completions.render(toy_parser(), "bash"))
        subprocess.run(["bash", "-n", str(script)], check=True)

    def test_a_quote_in_a_help_string_does_not_end_the_word_list(self, tmp_path):
        """A help line carrying an apostrophe still parses."""
        parser = argparse.ArgumentParser(prog="toy")
        parser.add_argument("--it", action="store_true", help="Mind it's an apostrophe")
        script = tmp_path / "quote.bash"
        script.write_text(completions.render(parser, "bash"))
        subprocess.run(["bash", "-n", str(script)], check=True)

    def test_it_registers_itself(self, parser):
        """The last thing the script does is attach the function."""
        assert (
            completions.render(parser, "bash")
            .rstrip()
            .endswith("complete -F _living_ink living-ink")
        )

    def test_a_value_taking_global_flag_is_skipped_when_finding_the_command(self, parser):
        """``living-ink -c foo.yml sync`` must not read foo.yml as the command."""
        script = completions.render(parser, "bash")
        assert "--config|-c) i=$((i+1)) ;;" in script

    def test_a_branch_of_only_switches_has_no_empty_case(self, parser):
        """An empty ``case`` is legal and reads like a mistake."""
        script = completions.render(parser, "bash")
        assert 'case "$prev" in\n            esac' not in script

    def test_a_path_flag_completes_filenames(self, parser):
        """``--destination <TAB>`` offers what is on disk."""
        script = completions.render(parser, "bash")
        assert '--destination) COMPREPLY=( $(compgen -f -- "$cur") ); return ;;' in script


class TestZsh:
    """The zsh function, and the two ways it can be loaded."""

    @pytest.mark.skipif(not shutil.which("zsh"), reason="zsh is not installed")
    def test_it_parses(self, parser, tmp_path):
        """``zsh -n`` accepts the generated script."""
        script = tmp_path / "_living-ink"
        script.write_text(completions.render(parser, "zsh"))
        subprocess.run(["zsh", "-n", str(script)], check=True)

    @pytest.mark.skipif(not shutil.which("zsh"), reason="zsh is not installed")
    def test_a_toy_parser_also_parses(self, tmp_path):
        """Escaping holds for a parser this module has never seen."""
        script = tmp_path / "_toy"
        script.write_text(completions.render(toy_parser(), "zsh"))
        subprocess.run(["zsh", "-n", str(script)], check=True)

    def test_compdef_comes_first(self, parser):
        """``compinit`` only reads the tag on the very first line."""
        assert completions.render(parser, "zsh").splitlines()[0] == "#compdef living-ink"

    def test_it_works_autoloaded_and_evalled(self, parser):
        """Sourced, it calls itself; eval'd, it registers itself."""
        script = completions.render(parser, "zsh")
        assert '_living_ink "$@"' in script
        assert "compdef _living_ink living-ink" in script

    def test_a_colon_in_a_description_is_escaped(self, parser):
        """An unescaped colon ends the spec and eats the argument action."""
        script = completions.render(parser, "zsh")
        assert "Remove what Living Ink installed\\:" in script

    def test_a_bracket_in_a_description_is_escaped(self):
        """An unescaped bracket ends the description early."""
        parser = argparse.ArgumentParser(prog="toy")
        parser.add_argument("--odd", action="store_true", help="Takes [a thing]")
        script = completions.render(parser, "zsh")
        assert "\\[a thing\\]" in script

    def test_two_spellings_of_one_flag_exclude_each_other(self, parser):
        """Offering ``--quiet`` stops offering ``-q``: one answer, not two."""
        assert "'(--quiet -q)--quiet[" in completions.render(parser, "zsh")

    def test_a_valued_flag_with_nothing_to_offer_still_expects_a_value(self, parser):
        """An empty action stops zsh completing the next flag as the value."""
        assert "--notebook[" in completions.render(parser, "zsh")
        assert "]:value:'" in completions.render(parser, "zsh")


class TestFish:
    """The fish completions, which are a list of independent rules."""

    @pytest.mark.skipif(not shutil.which("fish"), reason="fish is not installed")
    def test_it_parses(self, parser, tmp_path):
        """``fish --no-execute`` accepts the generated script."""
        script = tmp_path / "living-ink.fish"
        script.write_text(completions.render(parser, "fish"))
        subprocess.run(["fish", "--no-execute", str(script)], check=True)

    def test_file_completion_is_off_by_default(self, parser):
        """Without this, every flag suggests the working directory."""
        assert "complete -c living-ink -f" in completions.render(parser, "fish")

    def test_a_path_flag_turns_file_completion_back_on(self, parser):
        """``-F`` is the per-flag exception to the line above."""
        script = completions.render(parser, "fish")
        assert "-l destination -r -F" in script

    def test_a_global_flag_is_not_offered_after_a_subcommand(self, parser):
        """``living-ink sync --version`` is not something the parser accepts."""
        script = completions.render(parser, "fish")
        assert "not __fish_seen_subcommand_from" in script
        version = next(line for line in script.splitlines() if "-l version" in line)
        assert version.startswith("complete -c living-ink -n 'not __fish_seen_subcommand_from")

    def test_an_apostrophe_in_a_description_is_escaped(self, parser):
        """fish honours the backslash inside single quotes; bash does not."""
        script = completions.render(parser, "fish")
        assert "program\\'s version number" in script

    def test_a_positional_choice_is_offered(self, parser):
        """``living-ink completions <TAB>`` names the three shells."""
        script = completions.render(parser, "fish")
        assert "-n '__fish_seen_subcommand_from completions' -a 'bash zsh fish'" in script


class TestTheCommand:
    """``living-ink completions`` itself."""

    def test_it_prints_the_script_and_succeeds(self, capsys):
        """The script goes to stdout so it can be redirected or eval'd."""
        code = CompletionsCommand().run(argparse.Namespace(shell="bash"))
        assert code == 0
        assert "complete -F _living_ink living-ink" in capsys.readouterr().out

    def test_it_describes_the_command_it_is_part_of(self, capsys):
        """The parser it reads is the full one, ``completions`` included."""
        CompletionsCommand().run(argparse.Namespace(shell="zsh"))
        assert "completions" in capsys.readouterr().out

    def test_an_unknown_shell_is_a_usage_error(self):
        """argparse refuses the word before the command ever runs."""
        with pytest.raises(SystemExit) as exit_code:
            LivingInkCLI().build_parser().parse_args(["completions", "powershell"])
        assert exit_code.value.code == 2

    def test_it_is_registered(self):
        """The command ships, which is what makes any of this reachable."""
        assert LivingInkCLI().commands["completions"] is CompletionsCommand

    def test_it_asks_nothing(self):
        """No terminal is needed; a redirect has no terminal on the far end."""
        assert CompletionsCommand.interactive is False
