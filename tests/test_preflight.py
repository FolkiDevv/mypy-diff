"""Version parsing and the minimum-version gate."""

from __future__ import annotations

import pytest

from mypy_diff import MypyDiffError
from mypy_diff.preflight import (
    MIN_MYPY,
    ToolInfo,
    check,
    format_version,
    parse_version,
    require,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Every one of these is a real string observed from the tool itself.
        ("jj 0.43.0-89f62ede8c1c611eaf134c0c49252efd65c7945d", (0, 43, 0)),
        ("git version 2.55.0.windows.3", (2, 55, 0)),
        ("mypy 2.3.0 (compiled: yes)", (2, 3, 0)),
        ("mypy 1.13.0 (compiled: yes)", (1, 13, 0)),
        ("jj 0.28", (0, 28)),
        ("uv 0.11.32 (3010295ae 2026-07-23 x86_64-pc-windows-msvc)", (0, 11, 32)),
    ],
)
def test_parse_version_handles_real_output(raw: str, expected: tuple[int, ...]) -> None:
    assert parse_version(raw) == expected


def test_parse_version_returns_none_without_digits() -> None:
    assert parse_version("command not found") is None


def test_format_version_roundtrip() -> None:
    assert format_version((1, 11, 0)) == "1.11.0"


def _tool(version: tuple[int, ...] | None, raw: str = "") -> ToolInfo:
    return ToolInfo(name="mypy", command=("mypy",), version=version, raw_version=raw)


def test_check_passes_for_new_enough_tool() -> None:
    assert check("mypy", _tool((2, 3, 0)), MIN_MYPY) == []


def test_check_reports_missing_tool_with_install_hint() -> None:
    problems = check("jj", None, (0, 28, 0))
    assert "was not found on PATH" in problems[0]
    assert any("nstall" in line for line in problems[1:])


def test_check_reports_old_mypy_because_json_output_is_required() -> None:
    problems = check("mypy", _tool((1, 10, 0)), MIN_MYPY)
    assert "1.10.0 is too old" in problems[0]
    assert ">= 1.11.0" in problems[0]


def test_check_does_not_block_on_unparseable_version() -> None:
    # A custom wrapper that prints something odd should not stop the run.
    assert check("mypy", _tool(None, raw="mypy (dev build)"), MIN_MYPY) == []


def test_require_raises_with_details() -> None:
    with pytest.raises(MypyDiffError) as caught:
        require("mypy", _tool((1, 0, 0)), MIN_MYPY)
    assert "too old" in caught.value.message
    assert caught.value.details


def test_require_returns_the_tool_when_fine() -> None:
    tool = _tool((2, 3, 0))
    assert require("mypy", tool, MIN_MYPY) is tool
