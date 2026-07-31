"""Parsing mypy's `--output=json` stream."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mypy_diff.models import Severity
from mypy_diff.mypy_exec import parse_json_output

# Must be genuinely absolute on the running platform: on Windows a leading-slash path like
# "/repo" is drive-relative, not absolute, so the relativization branch would never run.
WORKDIR = Path.cwd() / "repo"


def line(**overrides: object) -> str:
    """One diagnostic in the exact shape mypy 2.x emits."""
    payload: dict[str, object] = {
        "file": "src/app.py",
        "line": 42,
        "column": 11,
        "end_line": 42,
        "end_column": 17,
        "message": 'Incompatible return value type (got "str", expected "int")',
        "hint": None,
        "code": "return-value",
        "severity": "error",
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_parses_a_basic_error() -> None:
    messages, skipped = parse_json_output(line(), WORKDIR)
    assert skipped == 0
    assert len(messages) == 1
    message = messages[0]
    assert message.path == "src/app.py"
    assert message.line == 42
    assert message.column == 11
    assert message.code == "return-value"
    assert message.severity is Severity.ERROR


def test_formats_back_into_mypy_style() -> None:
    messages, _ = parse_json_output(line(), WORKDIR)
    assert messages[0].format() == (
        'src/app.py:42:11: error: Incompatible return value type (got "str", expected "int")'
        "  [return-value]"
    )


def test_handles_missing_column_and_code() -> None:
    messages, _ = parse_json_output(line(column=None, code=None), WORKDIR)
    message = messages[0]
    assert message.column is None
    assert message.code is None
    assert message.format() == (
        'src/app.py:42: error: Incompatible return value type (got "str", expected "int")'
    )


def test_notes_attach_to_the_preceding_error() -> None:
    stream = "\n".join(
        [
            line(),
            line(severity="note", message="Possible overload variant:", code=None),
            line(severity="note", message="  def f(x: int) -> int", code=None),
        ]
    )
    messages, _ = parse_json_output(stream, WORKDIR)
    # Notes are elaboration, not diagnostics of their own.
    assert len(messages) == 1
    assert messages[0].notes == ("Possible overload variant:", "  def f(x: int) -> int")


def test_leading_note_without_an_error_is_kept_as_its_own_message() -> None:
    messages, _ = parse_json_output(line(severity="note", message="standalone"), WORKDIR)
    assert len(messages) == 1
    assert messages[0].severity is Severity.NOTE


def test_non_json_noise_is_counted_not_fatal() -> None:
    stream = "\n".join(["Found 1 error in 1 file", line(), "not json at all", "{}"])
    messages, skipped = parse_json_output(stream, WORKDIR)
    assert len(messages) == 1
    # summary line, the junk line, and the object with no "message" key
    assert skipped == 3


def test_absolute_paths_are_relativized_to_the_run_directory() -> None:
    absolute = str(WORKDIR / "src" / "app.py")
    messages, _ = parse_json_output(line(file=absolute), WORKDIR)
    # Both revisions must key on the same path, whether run in place or in a worktree.
    assert messages[0].path == "src/app.py"


@pytest.mark.skipif(os.name != "nt", reason="backslash separates paths only on Windows")
def test_windows_separators_are_normalized_to_posix() -> None:
    messages, _ = parse_json_output(line(file=r"src\pkg\app.py"), WORKDIR)
    assert messages[0].path == "src/pkg/app.py"


@pytest.mark.skipif(os.name == "nt", reason="backslash separates paths on Windows")
def test_backslashes_are_left_alone_on_posix() -> None:
    """A backslash is an ordinary filename character on POSIX, not a separator.

    Rewriting it would rename a legitimately-named file into a path that does not exist, and
    the VCS lookups keyed on this path would miss. Normalisation only has to make the two runs
    agree with each other, and both happen on the same machine.
    """
    messages, _ = parse_json_output(line(file=r"src\pkg\app.py"), WORKDIR)
    assert messages[0].path == "src\\pkg\\app.py"


def test_blank_lines_are_ignored() -> None:
    messages, skipped = parse_json_output(f"\n\n{line()}\n\n", WORKDIR)
    assert len(messages) == 1
    assert skipped == 0
