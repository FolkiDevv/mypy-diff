"""Resolving and invoking mypy."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from mypy_diff import MypyDiffError
from mypy_diff.mypy_exec import resolve_mypy, split_command


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
