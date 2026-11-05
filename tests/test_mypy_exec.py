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
    BlockingErrors,
    Environment,
    MissingTarget,
    MypyRun,
    _fatal_error,
    _interpreter_command,
    _interpreter_report,
    _mypy_candidates,
    anchor_command,
    check_mypy_args,
    checkout_environment,
    config_files,
    environment_fingerprint,
    move_targets_into,
    repository_import_dirs,
    resolve_mypy,
    run_mypy,
    split_command,
    working_copy_reads,
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

    def test_a_configured_path_that_does_not_exist_is_named(self) -> None:
        """Not "no files to check": the config names a path this checkout does not have."""
        error = self.fatal("mypy: error: Cannot read file 'nope.py': No such file")
        assert isinstance(error, MissingTarget)
        assert error.path == "nope.py"
        text = self.rendered(error)
        assert "`nope.py` does not exist in the working copy" in text
        assert "no files to check" not in text

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

    def test_a_blocking_error_carries_its_diagnostics(self, tmp_path: Path) -> None:
        blocking = json.dumps(
            {"file": "src/broken.py", "line": 1, "message": "Invalid syntax", "code": "syntax"}
        )
        with pytest.raises(BlockingErrors, match="blocking error in base 1234") as caught:
            run_fake(tmp_path, f"print({blocking!r}); sys.exit(2)")
        assert [m.path for m in caught.value.messages] == ["src/broken.py"]
        assert "src/broken.py:1: error: Invalid syntax  [syntax]" in caught.value.details

    def test_a_target_missing_from_this_checkout_is_left_out(self, tmp_path: Path) -> None:
        """The user's new file does not exist in the base yet: it has no errors there."""
        body = (
            "missing = [a for a in sys.argv[1:] if a.endswith('.py') and not os.path.exists(a)]\n"
            "if missing:\n"
            "    print(f\"mypy: error: Cannot read file '{missing[0]}': No such file\","
            " file=sys.stderr)\n"
            "    sys.exit(2)\n"
            f"print({DIAGNOSTIC!r}); sys.exit(1)\n"
        )
        (tmp_path / "old.py").write_text("", encoding="utf-8")
        run = run_fake(tmp_path, body, "old.py", "new.py", "newer.py")
        assert run.dropped_targets == ("new.py", "newer.py")
        assert len(run.messages) == 1

    def test_when_every_target_is_missing_the_report_is_empty(self, tmp_path: Path) -> None:
        """Running mypy with no targets would check what the config names instead."""
        body = (
            "if 'new.py' in sys.argv:\n"
            "    print(\"mypy: error: Cannot read file 'new.py': No such file\", file=sys.stderr)\n"
            "    sys.exit(2)\n"
            "sys.exit(3)\n"
        )
        run = run_fake(tmp_path, body, "--strict", "--python-version", "3.12", "new.py")
        assert run.messages == ()
        assert run.dropped_targets == ("new.py",)

    def test_an_existing_file_mypy_cannot_read_is_not_left_out(self, tmp_path: Path) -> None:
        """Permission denied is a failure, not "this revision has no such file": leaving it out
        would report its errors as FIXED."""
        (tmp_path / "locked.py").write_text("", encoding="utf-8")
        body = (
            "print(\"mypy: error: Cannot read file 'locked.py': Permission denied\","
            " file=sys.stderr)\nsys.exit(2)\n"
        )
        with pytest.raises(MypyDiffError, match="exit code 2") as caught:
            run_fake(tmp_path, body, "locked.py")
        assert not isinstance(caught.value, MissingTarget)

    def test_reads_are_traced_from_the_verbose_log(self, tmp_path: Path) -> None:
        log = (
            "LOG:  Mypy Version:           2.3.0\n"
            "LOG:  Parsing src/uses.py (uses)\n"
            "LOG:  Metadata fresh for shared: file /work/lib/shared/__init__.py\n"
            "LOG:  Parsing src/uses.py (uses)\n"
        )
        body = f"assert '--verbose' in sys.argv\nsys.stderr.write({log!r})\nsys.exit(0)\n"
        run = run_mypy(
            fake_mypy(tmp_path, body), tmp_path, (), tmp_path / "cache", trace_reads=True
        )
        assert run.read_files == ("src/uses.py", "/work/lib/shared/__init__.py")

    def test_verbose_log_lines_stay_out_of_error_messages(self, tmp_path: Path) -> None:
        body = "sys.stderr.write('LOG:  Parsing a.py (a)\\nplugin exploded\\n')\nsys.exit(2)\n"
        with pytest.raises(MypyDiffError) as caught:
            run_mypy(fake_mypy(tmp_path, body), tmp_path, (), tmp_path / "c", trace_reads=True)
        assert "plugin exploded" in caught.value.details
        assert not any(line.startswith("LOG:") for line in caught.value.details)

    def test_a_missing_path_named_only_by_the_config_is_an_error(self, tmp_path: Path) -> None:
        body = "print(\"mypy: error: Cannot read file 'gen': No such file\", file=sys.stderr)\n"
        with pytest.raises(MissingTarget, match="`gen` does not exist in base 1234"):
            run_fake(tmp_path, body + "sys.exit(2)", "src")

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

    def test_a_relative_executable_from_a_file_is_relative_to_that_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mypy-cmd = ".venv/bin/mypy"` in the root pyproject.toml, run from a subdirectory."""
        (tmp_path / "src").mkdir()
        monkeypatch.chdir(tmp_path / "src")
        relative = os.path.join(".venv", "bin", "mypy")
        assert anchor_command((relative,), tmp_path, relative_to=tmp_path) == (
            str(tmp_path / ".venv" / "bin" / "mypy"),
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

    def test_an_option_with_an_inline_path_named_mypy_is_not_the_program(self) -> None:
        command = ("uv", "run", "--project=/tools/mypy", "mypy")
        assert (_interpreter_command(command) or ())[:5] == (
            "uv",
            "run",
            "--project=/tools/mypy",
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


# ------------------------------------------------------------------ imports from the checkout


class TestCheckoutImports:
    """The base must import the repository's packages from its own checkout, not the working copy.

    An editable install (`uv sync` makes one for every workspace member) or an absolute PYTHONPATH
    points into the working copy; left alone, the base is checked against current code and a
    breaking change in a sibling package hides behind "unchanged".
    """

    def environment(self, root: Path, *entries: str) -> Environment:
        return Environment("f" * 64, ("", *entries), str(root / ".venv"))

    def test_entries_inside_the_repository_are_found(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        venv = root / ".venv" / "lib" / "python3.12" / "site-packages"
        environment = self.environment(
            root,
            str(root / "packages" / "core" / "src"),
            str(venv),
            str(tmp_path / "elsewhere"),
            str(root / "src"),
            str(root / "packages" / "core" / "src"),
        )
        assert repository_import_dirs(environment, root) == ("packages/core/src", "src")

    def test_site_packages_outside_the_venv_are_not_project_code(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        environment = Environment(
            "f" * 64, (str(root / "env" / "lib" / "site-packages"),), str(tmp_path / "other")
        )
        assert repository_import_dirs(environment, root) == ()

    def test_pythonpath_points_into_the_checkout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root, checkout = tmp_path / "repo", tmp_path / "checkout"
        monkeypatch.setenv(
            "PYTHONPATH", os.pathsep.join([str(root / "lib"), str(tmp_path / "tools")])
        )
        monkeypatch.delenv("MYPYPATH", raising=False)
        overrides = checkout_environment(("packages/core/src", "lib"), root, checkout)
        assert overrides["PYTHONPATH"].split(os.pathsep) == [
            str(checkout / "packages/core/src"),
            str(checkout / "lib"),
            str(tmp_path / "tools"),
        ]
        assert "MYPYPATH" not in overrides

    def test_absolute_mypypath_into_the_repository_is_moved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root, checkout = tmp_path / "repo", tmp_path / "checkout"
        monkeypatch.setenv("MYPYPATH", os.pathsep.join([str(root / "stubs"), "relative"]))
        overrides = checkout_environment((), root, checkout)
        assert overrides["MYPYPATH"].split(os.pathsep) == [str(checkout / "stubs"), "relative"]
        assert "PYTHONPATH" not in overrides

    def test_a_directory_holding_mypy_itself_is_left_alone(self, tmp_path: Path) -> None:
        """On PYTHONPATH it would replace the mypy that is running (when working on mypy)."""
        root, checkout = tmp_path / "repo", tmp_path / "checkout"
        (checkout / "src" / "mypy").mkdir(parents=True)
        assert "PYTHONPATH" not in checkout_environment(("src",), root, checkout)


class TestCheckoutOnlyReads:
    """What a base run read must come from its checkout — installed code excepted."""

    def test_a_source_file_of_the_working_copy_is_a_leak(self, tmp_path: Path) -> None:
        root, checkout = tmp_path / "repo", tmp_path / "checkout"
        reads = working_copy_reads(
            [str(root / "lib" / "shared.py"), "src/app.py"], checkout, checkout, root
        )
        assert reads == [Path("lib/shared.py")]

    def test_installed_dependencies_inside_the_repository_are_not(self, tmp_path: Path) -> None:
        """`uv` keeps `.venv` in the project: its site-packages are shared on purpose."""
        root, checkout = tmp_path / "repo", tmp_path / "checkout"
        venv = root / ".venv"
        installed = venv / "lib" / "python3.12" / "site-packages" / "attrs" / "__init__.pyi"
        elsewhere = root / "env" / "lib" / "site-packages" / "yaml" / "__init__.pyi"
        reads = working_copy_reads(
            [str(installed), str(elsewhere)], checkout, checkout, root, str(venv)
        )
        assert reads == []

    def test_only_targets_are_moved_into_the_checkout(self, tmp_path: Path) -> None:
        root, checkout = tmp_path / "repo", tmp_path / "checkout"
        python = str(root / ".venv" / "bin" / "python")
        arguments = ("--python-executable", python, str(root / "src"), "--strict")
        assert move_targets_into(arguments, root, checkout) == (
            "--python-executable",
            python,
            str(checkout / "src"),
            "--strict",
        )

    def test_targets_inside_the_environment_stay(self, tmp_path: Path) -> None:
        root, checkout = tmp_path / "repo", tmp_path / "checkout"
        stub = str(root / ".venv" / "lib" / "stubs" / "x.pyi")
        assert move_targets_into((stub,), root, checkout, str(root / ".venv")) == (stub,)


class TestConfigFileInTheCheckout:
    """A revision is checked with its own config, however `--config-file` was spelled."""

    def test_a_repository_config_moves_into_the_checkout(self, tmp_path: Path) -> None:
        root, checkout = tmp_path / "repo", tmp_path / "checkout"
        (checkout).mkdir()
        (checkout / "mypy.ini").write_text("[mypy]\n", encoding="utf-8")
        config = str(root / "mypy.ini")
        assert move_targets_into(("--config-file", config), root, checkout) == (
            "--config-file",
            str(checkout / "mypy.ini"),
        )
        assert move_targets_into((f"--config-file={config}",), root, checkout) == (
            f"--config-file={checkout / 'mypy.ini'}",
        )

    def test_a_config_the_revision_lacks_stays_where_it_is(self, tmp_path: Path) -> None:
        root, checkout = tmp_path / "repo", tmp_path / "checkout"
        config = str(root / "local.ini")
        arguments = ("--config-file", config)
        assert move_targets_into(arguments, root, checkout) == arguments

    def test_a_config_outside_the_repository_stays_where_it_is(self, tmp_path: Path) -> None:
        root, checkout = tmp_path / "repo", tmp_path / "checkout"
        arguments = ("--config-file", str(tmp_path / "shared" / "mypy.ini"))
        assert move_targets_into(arguments, root, checkout) == arguments

    def test_config_files_are_found_in_both_spellings(self) -> None:
        assert config_files(("--config-file", "a.ini", "--config-file=b.ini", "src")) == [
            "a.ini",
            "b.ini",
        ]
