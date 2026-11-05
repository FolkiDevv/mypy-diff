"""Resolving and invoking mypy."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mypy_diff import MypyDiffError
from mypy_diff.mypy_exec import (
    MypyRun,
    _fatal_error,
    _interpreter_command,
    _interpreter_report,
    _mypy_candidates,
    anchor_command,
    check_mypy_args,
    environment_fingerprint,
    resolve_mypy,
    run_mypy,
    split_command,
)
from mypy_diff.preflight import ToolInfo


class TestSplitCommand:
    """Regression cover for --mypy-cmd / $MYPY_DIFF_MYPY parsing.

    `shlex.split` in its default POSIX mode treats the backslash as an escape character, so a
    plain Windows path used to be mangled beyond recognition before it ever reached exec.
    """

    def test_multi_word_command(self) -> None:
        assert split_command("uv run mypy") == ("uv", "run", "mypy")

    def test_bare_existing_path_is_taken_verbatim(self, tmp_path: Path) -> None:
        executable = tmp_path / "mypy.exe"
        executable.write_text("", encoding="utf-8")
        assert split_command(str(executable)) == (str(executable),)

    def test_quoted_existing_path_loses_only_the_quotes(self, tmp_path: Path) -> None:
        executable = tmp_path / "mypy.exe"
        executable.write_text("", encoding="utf-8")
        assert split_command(f'"{executable}"') == (str(executable),)

    @pytest.mark.skipif(os.name != "nt", reason="backslash paths are Windows-specific")
    def test_windows_path_keeps_its_separators(self) -> None:
        raw = r"C:\tools\mypy-1.13\Scripts\mypy.exe"
        assert split_command(raw) == (raw,)

    @pytest.mark.skipif(os.name != "nt", reason="backslash paths are Windows-specific")
    def test_windows_path_with_arguments(self) -> None:
        assert split_command(r'"C:\py 3.12\python.exe" -m mypy') == (
            r"C:\py 3.12\python.exe",
            "-m",
            "mypy",
        )

    def test_empty_command(self) -> None:
        assert split_command("   ") == ()


class TestFatalError:
    """How a mypy exit code >= 2 is turned into a message.

    The "no targets" case used to slip through: the detection looked for "no files"/"Cannot
    read file" while mypy actually prints "Missing target module, package, files, or command."
    """

    #: Verbatim from mypy 1.x for a bare invocation with no targets configured.
    MISSING_TARGET = (
        "usage: mypy [-h] [-v] [-V] [more flags]\n"
        "            [-m MODULE] [-p PACKAGE] [-c PROGRAM_TEXT]\n"
        "mypy: error: Missing target module, package, files, or command.\n"
    )

    def fatal(self, stderr: str, prog: str = "mypy-diff-git") -> MypyDiffError:
        completed = subprocess.CompletedProcess(["mypy"], 2, stdout="", stderr=stderr)
        return _fatal_error(completed, prog)

    def rendered(self, error: MypyDiffError) -> str:
        return "\n".join([error.message, *error.details])

    def test_missing_target_is_explained(self) -> None:
        text = self.rendered(self.fatal(self.MISSING_TARGET))
        assert "mypy has no files to check" in text
        assert "-- src tests" in text
        assert 'files = ["src", "tests"]' in text

    def test_missing_target_drops_the_usage_dump(self) -> None:
        """The usage block buries the real problem; it must not reach the user."""
        text = self.rendered(self.fatal(self.MISSING_TARGET))
        assert "usage: mypy" not in text
        assert "-m MODULE" not in text

    @pytest.mark.parametrize("prog", ["mypy-diff-jj", "mypy-diff-git"])
    def test_hint_names_the_command_actually_run(self, prog: str) -> None:
        text = self.rendered(self.fatal(self.MISSING_TARGET, prog=prog))
        assert f"{prog} -- src tests" in text

    def test_unreadable_configured_path_is_also_explained(self) -> None:
        text = self.rendered(self.fatal("mypy: error: Cannot read file 'nope.py': No such file"))
        assert "mypy has no files to check" in text

    def test_other_failures_keep_their_output(self) -> None:
        text = self.rendered(self.fatal("Traceback...\nAssertionError: plugin exploded\n"))
        assert "mypy failed with exit code 2" in text
        assert "plugin exploded" in text
        assert "no files to check" not in text


class TestResolveMypy:
    def test_explicit_command_wins(self, tmp_path: Path) -> None:
        tool, source = resolve_mypy(tmp_path, f'"{sys.executable}" -m mypy')
        assert source == "--mypy-cmd"
        assert tool.version is not None

    def test_bare_executable_path_resolves(self, tmp_path: Path) -> None:
        """The shape that used to break: an unquoted absolute path."""
        binaries = Path(sys.executable).parent
        mypy = binaries / ("mypy.exe" if os.name == "nt" else "mypy")
        if not mypy.is_file():
            pytest.skip("no mypy executable next to the interpreter")

        tool, source = resolve_mypy(tmp_path, str(mypy))

        assert source == "--mypy-cmd"
        assert tool.command == (str(mypy),)

    def test_environment_variable_is_used(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MYPY_DIFF_MYPY", f'"{sys.executable}" -m mypy')
        _, source = resolve_mypy(tmp_path)
        assert source == "$MYPY_DIFF_MYPY"

    def test_explicit_command_does_not_fall_back(self, tmp_path: Path) -> None:
        """A bad --mypy-cmd must fail loudly, not silently pick some other mypy."""
        with pytest.raises(MypyDiffError, match="Could not find a usable `mypy`"):
            resolve_mypy(tmp_path, "definitely-not-a-real-mypy")

    def test_subproject_looks_in_its_own_venv_then_uv_then_the_repository_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The root .venv is only a fallback: the root may be an unrelated project.

        uv knows whether the subproject is a workspace member using the root environment or a
        project of its own, so it gets asked first.
        """
        monkeypatch.delenv("MYPY_DIFF_MYPY", raising=False)
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        monkeypatch.setattr("shutil.which", lambda name: f"/fake/bin/{name}")
        project = tmp_path / "backend"
        project.mkdir()
        (project / "pyproject.toml").write_text("[project]\nname = 'backend'\n", encoding="utf-8")

        sources = [source for _, source in _mypy_candidates(project, None, tmp_path)]

        assert sources == [
            "project .venv",
            "uv run --project",
            "repository root .venv",
            "PATH",
        ]
        assert "repository root .venv" not in [
            source for _, source in _mypy_candidates(tmp_path, None, tmp_path)
        ]


# ------------------------------------------------------------------ running mypy


def fake_mypy(tmp_path: Path, body: str) -> ToolInfo:
    """A stand-in for mypy: a Python script whose behaviour the test spells out."""
    script = tmp_path / "fake_mypy.py"
    script.write_text("import json, os, signal, sys\n" + body, encoding="utf-8")
    return ToolInfo("mypy", (sys.executable, str(script)), (2, 3, 0), "mypy 2.3.0")


def run_fake(tmp_path: Path, body: str, *args: str) -> MypyRun:
    return run_mypy(fake_mypy(tmp_path, body), tmp_path, args, tmp_path / "cache", what="base 1234")


DIAGNOSTIC = json.dumps(
    {
        "file": "src/app.py",
        "line": 2,
        "column": 11,
        "message": "Incompatible return value type",
        "hint": None,
        "code": "return-value",
        "severity": "error",
    }
)


class TestRunMypy:
    """A run is either a complete report or an error — never a silently empty result.

    An empty result is diffed (and cached) as "this revision has no errors", which turns every
    error on the other side into NEW or FIXED.
    """

    def test_a_normal_report_is_parsed(self, tmp_path: Path) -> None:
        run = run_fake(tmp_path, f"print({DIAGNOSTIC!r}); sys.exit(1)")
        assert [m.code for m in run.messages] == ["return-value"]

    def test_a_clean_run_is_accepted(self, tmp_path: Path) -> None:
        assert run_fake(tmp_path, "sys.exit(0)").messages == ()

    def test_exit_one_with_only_notes_is_accepted(self, tmp_path: Path) -> None:
        """reveal_type() alone makes mypy exit 1 with a single note."""
        note = json.dumps(
            {"file": "a.py", "line": 1, "message": "Revealed type", "severity": "note"}
        )
        assert len(run_fake(tmp_path, f"print({note!r}); sys.exit(1)").messages) == 1

    def test_exit_one_without_diagnostics_is_an_error(self, tmp_path: Path) -> None:
        """`poetry run mypy` in a fresh worktree: 'Command not found: mypy', exit 1, no JSON."""
        with pytest.raises(MypyDiffError, match="base 1234 but reported no diagnostics") as caught:
            run_fake(tmp_path, "print('Command not found: mypy', file=sys.stderr); sys.exit(1)")
        assert "Command not found: mypy" in caught.value.details

    @pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
    def test_a_killed_mypy_is_an_error_not_an_empty_report(self, tmp_path: Path) -> None:
        """The OOM killer used to produce an empty base that was then cached for good."""
        with pytest.raises(MypyDiffError, match="killed by SIGKILL while checking base 1234"):
            run_fake(tmp_path, "os.kill(os.getpid(), signal.SIGKILL)")

    def test_diagnostics_on_stderr_are_used(self, tmp_path: Path) -> None:
        """With --non-interactive mypy prints its report on stderr instead of stdout."""
        run = run_fake(tmp_path, f"print({DIAGNOSTIC!r}, file=sys.stderr); sys.exit(1)")
        assert len(run.messages) == 1

    def test_mypy_never_waits_for_input(self, tmp_path: Path) -> None:
        run = run_fake(tmp_path, "sys.exit(0 if sys.stdin.read() == '' else 3)")
        assert run.returncode == 0

    def test_install_types_is_made_non_interactive(self, tmp_path: Path) -> None:
        body = "sys.exit(0 if '--non-interactive' in sys.argv else 3)"
        assert run_fake(tmp_path, body, "--install-types").returncode == 0

    def test_a_blocking_error_is_shown_like_mypy_prints_it(self, tmp_path: Path) -> None:
        blocking = json.dumps(
            {"file": "src/broken.py", "line": 1, "message": "Invalid syntax", "code": "syntax"}
        )
        with pytest.raises(MypyDiffError, match="exit code 2 on base 1234") as caught:
            run_fake(tmp_path, f"print({blocking!r}); sys.exit(2)")
        assert "src/broken.py:1: error: Invalid syntax  [syntax]" in caught.value.details

    def test_a_missing_executable_is_an_error(self, tmp_path: Path) -> None:
        tool = ToolInfo("mypy", (str(tmp_path / "nope" / "mypy"),), (2, 3, 0), "mypy 2.3.0")
        with pytest.raises(MypyDiffError, match="Could not run mypy on base 1234"):
            run_mypy(tool, tmp_path, (), tmp_path / "cache", what="base 1234")


@pytest.mark.parametrize("argument", ["-h", "--help", "-V", "--version"])
def test_arguments_that_suppress_the_report_are_rejected(argument: str) -> None:
    with pytest.raises(MypyDiffError, match="would then print no report"):
        check_mypy_args(("src", argument))


def test_ordinary_arguments_are_accepted() -> None:
    check_mypy_args(("src", "--strict", "--python-version", "3.12"))


# ------------------------------------------------------------------ anchoring the command


class TestAnchorCommand:
    """The base runs in a temporary checkout, so the command must not depend on the cwd."""

    def test_a_relative_executable_becomes_absolute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        relative = os.path.join(".venv", "bin", "mypy")
        assert anchor_command((relative, "--strict"), tmp_path) == (
            str(tmp_path / ".venv" / "bin" / "mypy"),
            "--strict",
        )

    def test_a_bare_name_is_left_to_path_lookup(self, tmp_path: Path) -> None:
        assert anchor_command(("mypy",), tmp_path) == ("mypy",)

    def test_uv_run_is_pinned_to_the_project(self, tmp_path: Path) -> None:
        """Otherwise uv builds a new environment from the base revision's lock file."""
        assert anchor_command(("uv", "run", "mypy"), tmp_path) == (
            "uv",
            "run",
            "--project",
            str(tmp_path),
            "mypy",
        )

    @pytest.mark.parametrize("flag", ["--project", "--directory", "--project=elsewhere"])
    def test_uv_run_with_an_explicit_project_is_left_alone(self, tmp_path: Path, flag: str) -> None:
        command = ("uv", "run", flag, "mypy")
        assert anchor_command(command, tmp_path) == command


# ------------------------------------------------------------------ environment fingerprint


class TestEnvironmentFingerprint:
    MYPY = ToolInfo("mypy", (sys.executable, "-m", "mypy"), (2, 3, 0), "mypy 2.3.0")

    def test_interpreter_command_for_the_common_shapes(self) -> None:
        assert (_interpreter_command(("python", "-m", "mypy")) or ())[:2] == ("python", "-c")
        assert (_interpreter_command(("uv", "run", "mypy")) or ())[:4] == (
            "uv",
            "run",
            "python",
            "-c",
        )
        assert _interpreter_command(("black",)) is None

    def test_a_venv_script_uses_the_interpreter_next_to_it(self, tmp_path: Path) -> None:
        binaries = Path(sys.executable).parent
        mypy = binaries / ("mypy.exe" if os.name == "nt" else "mypy")
        if not mypy.is_file():
            pytest.skip("no mypy executable next to the interpreter")
        command = _interpreter_command((str(mypy),))
        assert command is not None
        assert Path(command[0]).parent == binaries

    def test_the_installed_packages_are_part_of_it(self, tmp_path: Path) -> None:
        report = _interpreter_report(self.MYPY.command, tmp_path)
        assert "mypy==" in report

    def test_it_is_stable(self, tmp_path: Path) -> None:
        assert environment_fingerprint(self.MYPY, tmp_path) == environment_fingerprint(
            self.MYPY, tmp_path
        )

    def test_mypypath_changes_it(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MYPYPATH", raising=False)
        before = environment_fingerprint(self.MYPY, tmp_path)
        monkeypatch.setenv("MYPYPATH", str(tmp_path / "stubs"))
        assert environment_fingerprint(self.MYPY, tmp_path) != before

    def test_another_mypy_build_changes_it(self, tmp_path: Path) -> None:
        dev = dataclasses.replace(self.MYPY, raw_version="mypy 2.3.0+dev.abc123")
        assert environment_fingerprint(dev, tmp_path) != environment_fingerprint(
            self.MYPY, tmp_path
        )

    def test_an_unaskable_interpreter_gives_no_fingerprint(self, tmp_path: Path) -> None:
        """No fingerprint, so the caller turns the result cache off rather than guess."""
        tool = ToolInfo("mypy", (str(tmp_path / "gone" / "mypy"),), None, "")
        assert environment_fingerprint(tool, tmp_path) is None

    def test_arguments_after_mypy_are_ignored(self) -> None:
        assert (_interpreter_command(("python", "-m", "mypy", "--strict")) or ())[:2] == (
            "python",
            "-c",
        )
        assert (_interpreter_command(("uv", "run", "mypy", "--strict", "src/mypy")) or ())[:4] == (
            "uv",
            "run",
            "python",
            "-c",
        )

    def test_a_project_path_named_mypy_is_not_the_program(self) -> None:
        command = ("uv", "run", "--project", "/src/mypy", "mypy")
        assert (_interpreter_command(command) or ())[:6] == (
            "uv",
            "run",
            "--project",
            "/src/mypy",
            "python",
            "-c",
        )

    def test_a_bare_name_is_looked_up_on_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`--mypy-cmd mypy` used to look for ./python and ./mypy in the current directory."""
        binaries = tmp_path / "bin"
        binaries.mkdir()
        (binaries / "mypy").write_text("", encoding="utf-8")
        interpreter = binaries / ("python.exe" if os.name == "nt" else "python")
        interpreter.write_text("", encoding="utf-8")
        monkeypatch.setattr(
            "mypy_diff.mypy_exec.shutil.which",
            lambda name: str(binaries / name) if name == "mypy" else None,
        )
        monkeypatch.chdir(tmp_path)
        assert (_interpreter_command(("mypy", "--strict")) or ())[:2] == (str(interpreter), "-c")
