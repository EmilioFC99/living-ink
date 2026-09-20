"""Tests for installing tab completion — the half that touches the machine.

:mod:`tests.test_completions` covers the *generator*: what the script says for
a given parser. This file covers everything after that — where the file goes,
whether the shell would ever read it, and the one line in an rc file that is
asked about because the user owns that file.

Two of these functions are patched for the whole suite by
``conftest.completions_stay_home`` — ``completion_dirs``, so no test can write
into ``/opt/homebrew``, and ``completion_is_live``, so no test spawns an
interactive shell. The real ones are bound by the module-level imports below:
test modules are imported at collection, before any fixture runs, so a direct
``from ... import`` captures the function rather than the patch. That is
deliberate, and it is the only way to test either one.
"""

import platform
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from living_ink import setup_wizard
from living_ink.setup_wizard import (
    RC_END,
    RC_START,
    CompletionTarget,
    _without_rc_block,
    completion_dirs,
    completion_is_live,
    completion_rc_snippet,
    completion_target,
    detect_shell,
    enable_completions_in_rc,
    install_completions,
    shell_rc_path,
    uninstall_completions,
)


@pytest.fixture
def real_dirs(monkeypatch):
    """Undo the suite-wide redirection of :func:`completion_dirs`.

    Args:
        monkeypatch: Pytest's patcher.

    Returns:
        The real function, also installed back onto the module so that
        ``completion_target`` and ``uninstall_completions`` see it.
    """
    monkeypatch.setattr(setup_wizard, "completion_dirs", completion_dirs)
    return completion_dirs


class TestWhichShellThisIs:
    """``detect_shell`` reads ``$SHELL`` and refuses to guess past it."""

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("/bin/zsh", "zsh"),
            ("/usr/local/bin/bash", "bash"),
            ("/opt/homebrew/bin/fish", "fish"),
        ],
    )
    def test_a_supported_shell_is_named(self, monkeypatch, value, expected):
        """The basename is the answer, whatever directory it came from."""
        monkeypatch.setenv("SHELL", value)

        assert detect_shell() == expected

    @pytest.mark.parametrize("value", ["/bin/ksh", "/usr/bin/nu", ""])
    def test_anything_else_is_nobody(self, monkeypatch, value):
        """An unsupported or unset shell is None, not a default.

        Guessing zsh for a user of ksh writes a file that shell will never
        read, and the user has no reason to connect the two.
        """
        monkeypatch.setenv("SHELL", value)

        assert detect_shell() is None


class TestWhereTheScriptGoes:
    """``completion_target`` prefers a directory that already exists."""

    def test_a_directory_the_shell_already_searches_wins(self, monkeypatch, tmp_path):
        """An existing, writable, searched directory beats the home fallback.

        The point of the whole feature: an install that edits nothing the user
        owns. Only when no such directory exists does anything else happen.
        """
        site = tmp_path / "site-functions"
        site.mkdir()
        monkeypatch.setattr(
            setup_wizard,
            "completion_dirs",
            lambda shell: ((site, True), (Path.home() / ".zfunc", False)),
        )

        target = completion_target("zsh")

        assert target == CompletionTarget("zsh", site / "_living-ink", True)

    def test_a_directory_that_does_not_exist_is_skipped(self, monkeypatch, tmp_path):
        """A missing system directory is not created.

        Creating ``/usr/local/share/zsh/site-functions`` on a machine that has
        no such thing invents a convention rather than following one, and
        nothing would ever read it.
        """
        monkeypatch.setattr(
            setup_wizard,
            "completion_dirs",
            lambda shell: ((tmp_path / "absent", True), (Path.home() / ".zfunc", False)),
        )

        target = completion_target("zsh")

        assert target is not None
        assert target.path == Path.home() / ".zfunc" / "_living-ink"
        assert target.searched is False

    def test_an_unwritable_directory_is_skipped(self, monkeypatch, tmp_path):
        """Existing is not enough — a root-owned directory is somebody else's."""
        site = tmp_path / "site-functions"
        site.mkdir()
        monkeypatch.setattr(
            setup_wizard,
            "completion_dirs",
            lambda shell: ((site, True), (Path.home() / ".zfunc", False)),
        )
        with patch("os.access", return_value=False):
            target = completion_target("zsh")

        assert target is not None
        assert target.path.parent == Path.home() / ".zfunc"

    def test_nowhere_under_home_is_nowhere(self, monkeypatch, tmp_path):
        """With no home-relative fallback left, there is no answer.

        None is the honest result rather than the first candidate: writing
        into a directory that does not exist and is not the user's is how an
        installer needs sudo it never asked for.
        """
        monkeypatch.setattr(
            setup_wizard, "completion_dirs", lambda shell: ((tmp_path / "x", True),)
        )

        assert completion_target("zsh") is None

    def test_an_unknown_shell_has_nowhere(self):
        """A shell with no filename and no directories resolves to None."""
        assert completion_target("ksh") is None

    def test_every_shell_has_somewhere_under_home(self, real_dirs):
        """Each supported shell keeps a fallback the user can always write.

        This is what makes the suite-wide fixture safe: it filters the real
        candidates down to the home-relative ones, so a shell that lost its
        last one would make that fixture silently stop testing anything.
        """
        home = Path.home()
        for shell in ("zsh", "bash", "fish"):
            candidates = real_dirs(shell)
            assert any(home in directory.parents for directory, _ in candidates), shell


class TestWhetherTheShellWouldEverReadIt:
    """``completion_is_live`` asks the shell instead of assuming.

    The reason the feature is not just a file write: on macOS neither
    ``/etc/zshrc`` nor ``/etc/zprofile`` runs ``compinit``, so a correctly
    installed script sits inert and Tab does nothing.
    """

    def test_a_named_completion_means_yes(self):
        """Output from the probe is the shell saying it has one."""
        completed = subprocess.CompletedProcess([], 0, stdout="_living-ink\n", stderr="")
        with patch("subprocess.run", return_value=completed) as run:
            assert completion_is_live("zsh") is True

        assert run.call_args.args[0][:2] == ["zsh", "-i"]

    def test_silence_means_no(self):
        """An empty answer is the macOS default, and the case this exists for."""
        completed = subprocess.CompletedProcess([], 0, stdout="\n", stderr="")
        with patch("subprocess.run", return_value=completed):
            assert completion_is_live("zsh") is False

    @pytest.mark.parametrize(
        "boom",
        [
            subprocess.TimeoutExpired(cmd="zsh", timeout=15),
            FileNotFoundError("no zsh here"),
        ],
    )
    def test_a_shell_that_cannot_answer_is_a_no(self, boom):
        """A hung or missing shell is not a reason to crash the wizard.

        False is the safe way to be wrong: the cost is offering help that was
        not needed, against a traceback in the middle of setup.
        """
        with patch("subprocess.run", side_effect=boom):
            assert completion_is_live("zsh") is False

    def test_an_unknown_shell_is_never_probed(self):
        """No probe exists for ksh, so no subprocess is started for it."""
        with patch("subprocess.run") as run:
            assert completion_is_live("ksh") is False

        run.assert_not_called()


class TestTheLinesThatMakeItWork:
    """``completion_rc_snippet`` says the least that can possibly help."""

    def test_zsh_in_a_searched_directory_only_needs_compinit(self):
        """No ``fpath`` line when the shell already looks in that directory."""
        snippet = completion_rc_snippet(CompletionTarget("zsh", Path("/x/_living-ink"), True))

        assert "fpath+=" not in snippet
        assert "autoload -Uz compinit && compinit" in snippet

    def test_zsh_elsewhere_needs_the_directory_named(self):
        """The fallback directory has to be added to ``fpath`` to be found."""
        snippet = completion_rc_snippet(
            CompletionTarget("zsh", Path.home() / ".zfunc" / "_living-ink", False)
        )

        assert f"fpath+=({Path.home() / '.zfunc'})" in snippet
        # Order matters: `compinit` reads `fpath` when it runs, so a line added
        # after it would take effect one shell session late.
        assert snippet.index("fpath+=") < snippet.index("compinit")

    def test_bash_sources_the_file_directly(self):
        """bash has no search path for this, so the file is sourced by name."""
        snippet = completion_rc_snippet(CompletionTarget("bash", Path("/x/living-ink"), True))

        assert "source /x/living-ink" in snippet

    def test_fish_needs_nothing(self):
        """fish reads its completions directory unprompted, always."""
        assert completion_rc_snippet(CompletionTarget("fish", Path("/x/a.fish"), True)) == ""

    def test_every_snippet_is_wrapped_in_both_markers(self):
        """``uninstall`` finds the block by these two strings and nothing else."""
        for target in (
            CompletionTarget("zsh", Path("/x/_living-ink"), True),
            CompletionTarget("bash", Path("/x/living-ink"), True),
        ):
            snippet = completion_rc_snippet(target)
            assert snippet.startswith(RC_START)
            assert snippet.endswith(RC_END)


class TestWhichFileTheShellReadsAtStartup:
    """``shell_rc_path`` knows the one macOS difference that matters."""

    def test_zsh_is_zshrc_everywhere(self):
        """zsh reads ``.zshrc`` for interactive shells on every platform."""
        assert shell_rc_path("zsh") == Path.home() / ".zshrc"

    def test_bash_reads_bash_profile_on_a_mac(self):
        """Terminal.app starts login shells, which skip ``.bashrc``."""
        with patch.object(platform, "system", return_value="Darwin"):
            assert shell_rc_path("bash") == Path.home() / ".bash_profile"

    def test_bash_reads_bashrc_elsewhere(self):
        """Everywhere else an interactive bash reads ``.bashrc``."""
        with patch.object(platform, "system", return_value="Linux"):
            assert shell_rc_path("bash") == Path.home() / ".bashrc"

    def test_fish_has_no_file_to_edit(self):
        """Nothing to add, so nothing to name — and nothing to ask about."""
        assert shell_rc_path("fish") is None


class TestWritingTheScript:
    """``install_completions`` asks nobody, and says where it went."""

    def test_it_writes_a_script_the_shell_could_load(self, tmp_path):
        """The file exists, and it is the generated script rather than a stub."""
        ok, message, target = install_completions("zsh")

        assert ok is True
        assert target is not None
        assert target.path.name == "_living-ink"
        text = target.path.read_text(encoding="utf-8")
        assert text.startswith("#compdef living-ink")
        assert str(target.path) in message

    def test_it_creates_the_directory_it_needs(self):
        """``~/.zfunc`` does not exist on a fresh machine and is made here."""
        assert not (Path.home() / ".zfunc").exists()

        ok, _message, target = install_completions("zsh")

        assert ok is True
        assert target.path.parent.is_dir()

    def test_an_unknown_shell_installs_nothing(self, monkeypatch):
        """No shell, no file, and a message saying which of the two failed."""
        monkeypatch.setenv("SHELL", "/bin/ksh")

        ok, message, target = install_completions()

        assert ok is False
        assert target is None
        assert "which shell" in message

    def test_nowhere_to_write_is_reported_as_such(self, monkeypatch, tmp_path):
        """A target of None is a different failure from a failed write."""
        monkeypatch.setattr(setup_wizard, "completion_dirs", lambda shell: ())

        ok, message, target = install_completions("zsh")

        assert ok is False
        assert target is None
        assert "nowhere writable" in message

    def test_a_refused_write_is_a_message_not_a_traceback(self):
        """An OSError mid-install ends the step, not the wizard.

        Completions are the least important thing ``setup`` does; failing the
        whole run over one is out of proportion.
        """
        with patch.object(Path, "write_text", side_effect=OSError("read-only")):
            ok, message, target = install_completions("zsh")

        assert ok is False
        assert target is None
        assert "read-only" in message

    def test_the_detected_shell_is_the_default(self, monkeypatch):
        """Called with no shell, it installs for whatever ``$SHELL`` names."""
        monkeypatch.setenv("SHELL", "/opt/homebrew/bin/fish")

        ok, _message, target = install_completions()

        assert ok is True
        assert target.path.name == "living-ink.fish"


class TestTheOnePartThatAsksFirst:
    """``enable_completions_in_rc`` is the edit to a file the user owns."""

    def test_the_block_is_appended(self):
        """The lines land at the end, wrapped in their markers."""
        rc = Path.home() / ".zshrc"
        rc.write_text("export PATH=/usr/bin\n", encoding="utf-8")
        target = CompletionTarget("zsh", Path.home() / ".zfunc" / "_living-ink", False)

        ok, message = enable_completions_in_rc(target)

        text = rc.read_text(encoding="utf-8")
        assert ok is True
        assert str(rc) in message
        assert text.startswith("export PATH=/usr/bin\n")
        assert RC_START in text and RC_END in text

    def test_a_second_run_changes_nothing(self):
        """``setup`` is a command people re-run; this has to be idempotent."""
        rc = Path.home() / ".zshrc"
        rc.write_text("export PATH=/usr/bin\n", encoding="utf-8")
        target = CompletionTarget("zsh", Path.home() / ".zfunc" / "_living-ink", False)
        enable_completions_in_rc(target)
        once = rc.read_text(encoding="utf-8")

        ok, message = enable_completions_in_rc(target)

        assert ok is True
        assert "already" in message
        assert rc.read_text(encoding="utf-8") == once

    def test_an_rc_file_that_does_not_exist_yet_is_created(self):
        """A machine with no ``.zshrc`` gets one starting at the marker.

        No leading blank line: a shell config whose first line is empty reads
        as something having gone wrong.
        """
        rc = Path.home() / ".zshrc"
        assert not rc.exists()
        target = CompletionTarget("zsh", Path.home() / ".zfunc" / "_living-ink", False)

        ok, _message = enable_completions_in_rc(target)

        assert ok is True
        assert rc.read_text(encoding="utf-8").startswith(RC_START)

    def test_a_file_with_no_trailing_newline_is_not_run_into(self):
        """The last line of a hand-edited rc file often has no newline."""
        rc = Path.home() / ".zshrc"
        rc.write_text("export PATH=/usr/bin", encoding="utf-8")
        target = CompletionTarget("zsh", Path.home() / ".zfunc" / "_living-ink", False)

        enable_completions_in_rc(target)

        text = rc.read_text(encoding="utf-8")
        assert text.startswith("export PATH=/usr/bin\n")
        assert RC_START in text.splitlines()

    def test_fish_is_a_success_with_nothing_written(self):
        """No snippet and no rc file is not a failure — it is fish working."""
        ok, message = enable_completions_in_rc(
            CompletionTarget("fish", Path.home() / "c" / "a.fish", True)
        )

        assert ok is True
        assert "no changes" in message

    def test_a_refused_write_is_reported(self):
        """An rc file that cannot be appended to is a warning, not a crash."""
        target = CompletionTarget("zsh", Path.home() / ".zfunc" / "_living-ink", False)
        with patch.object(Path, "open", side_effect=OSError("permission denied")):
            ok, message = enable_completions_in_rc(target)

        assert ok is False
        assert "permission denied" in message


class TestTakingItBackOut:
    """``uninstall_completions`` removes the file and un-edits the rc file."""

    def test_the_script_and_the_block_both_go(self):
        """Install then uninstall leaves the machine as it was found."""
        rc = Path.home() / ".zshrc"
        rc.write_text("export PATH=/usr/bin\n", encoding="utf-8")
        _ok, _message, target = install_completions("zsh")
        enable_completions_in_rc(target)

        removed = uninstall_completions("zsh")

        assert not target.path.exists()
        assert rc.read_text(encoding="utf-8") == "export PATH=/usr/bin\n"
        assert any(str(target.path) in line for line in removed)
        assert any(str(rc) in line for line in removed)

    def test_nothing_installed_removes_nothing(self):
        """An empty list is how the caller knows to print nothing at all."""
        assert uninstall_completions("zsh") == []

    def test_it_cleans_up_after_a_shell_the_user_no_longer_has(self, monkeypatch):
        """With no shell named, every shell's file is looked for.

        The shell someone had at ``setup`` is not necessarily the one they
        have at ``uninstall``, and the file left behind is the one nobody
        would ever find.
        """
        install_completions("bash")
        install_completions("fish")
        monkeypatch.setenv("SHELL", "/bin/zsh")

        removed = uninstall_completions()

        assert len(removed) == 2
        assert not (Path.home() / ".bash_completion.d" / "living-ink").exists()
        assert not (Path.home() / ".config" / "fish" / "completions" / "living-ink.fish").exists()

    def test_a_write_it_cannot_undo_is_reported_not_swallowed(self):
        """A failure line is what ``uninstall`` turns into a warning."""
        _ok, _message, target = install_completions("zsh")
        with patch.object(Path, "unlink", side_effect=OSError("busy")):
            removed = uninstall_completions("zsh")

        assert any(line.startswith("Could not") for line in removed)


class TestFindingTheBlockExactly:
    """``_without_rc_block`` only ever deletes between its own markers."""

    def test_the_lines_around_it_survive(self):
        """Everything above and below the block is left byte for byte."""
        text = f"before\n{RC_START}\nfpath+=(x)\n{RC_END}\nafter\n"

        assert _without_rc_block(text) == "before\nafter\n"

    def test_the_blank_line_the_install_added_goes_with_it(self):
        """Otherwise a few install/uninstall cycles pad somebody's rc file.

        ``enable_completions_in_rc`` writes a blank line above the block to
        keep it readable, so removal has to take it back.
        """
        text = f"before\n\n{RC_START}\nfpath+=(x)\n{RC_END}\n"

        assert _without_rc_block(text) == "before\n"

    def test_a_file_without_the_block_is_untouched(self):
        """No marker, no edit — including the trailing newline."""
        text = "export PATH=/usr/bin\n"

        assert _without_rc_block(text) is text

    def test_an_unterminated_block_is_left_alone(self):
        """Deleting to the end of a shell config on a guess is not a recovery.

        A user who deleted the end marker by hand keeps their file; the
        orphaned lines are visible and harmless, which a truncated ``.zshrc``
        is not.
        """
        text = f"before\n{RC_START}\nfpath+=(x)\nunrelated\n"

        assert _without_rc_block(text) == text

    def test_indented_markers_still_match(self):
        """The markers are compared stripped, so whitespace does not orphan one."""
        text = f"  {RC_START}\n  fpath+=(x)\n  {RC_END}\nafter\n"

        assert _without_rc_block(text) == "after\n"
