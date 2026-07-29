"""Output formats."""

from __future__ import annotations

import io
import json

from rich.console import Console

from mypy_diff.models import DiffResult, MypyMessage, RevisionInfo, Severity
from mypy_diff.render import Only, OutputFormat, render

BASE = RevisionInfo(
    commit_id="a" * 40,
    short_id="aaaaaaaa",
    refs=("main",),
    description="feat: add parser",
    change_id="zqxlrpvw",
)
TARGET = RevisionInfo(commit_id="b" * 40, short_id="bbbbbbbb", is_working_copy=True)


def message(line: int, code: str, text: str, path: str = "src/app.py") -> MypyMessage:
    return MypyMessage(
        path=path, line=line, column=4, severity=Severity.ERROR, message=text, code=code
    )


RESULT = DiffResult(
    base=BASE,
    target=TARGET,
    new=(message(42, "return-value", "Bad return"),),
    fixed=(message(10, "name-defined", "Undefined name"),),
    unchanged=(message(7, "arg-type", "Bad arg"), message(9, "arg-type", "Bad arg 2")),
)


def capture(result: DiffResult, output_format: OutputFormat, **kwargs: object) -> str:
    stream = io.StringIO()
    console = Console(file=stream, highlight=False, soft_wrap=True, no_color=True, width=200)
    render(result, console, output_format, **kwargs)  # type: ignore[arg-type]
    return stream.getvalue()


def test_pretty_shows_both_sections_and_a_summary() -> None:
    output = capture(RESULT, OutputFormat.PRETTY)
    assert "+ NEW (1)" in output
    assert "- FIXED (1)" in output
    assert "+ src/app.py:42:4: error: Bad return  [return-value]" in output
    assert "- src/app.py:10:4: error: Undefined name  [name-defined]" in output
    assert "2 unchanged errors in 1 file" in output
    assert "Found 1 new, 1 fixed, 2 unchanged  (net 0)" in output


def test_summary_signs_a_positive_net() -> None:
    worse = DiffResult(base=BASE, target=TARGET, new=RESULT.new + RESULT.new)
    assert "(net +2)" in capture(worse, OutputFormat.PRETTY)


def test_summary_shows_a_negative_net_when_errors_were_fixed() -> None:
    better = DiffResult(base=BASE, target=TARGET, fixed=RESULT.fixed + RESULT.fixed)
    assert "(net -2)" in capture(better, OutputFormat.PRETTY)


def test_pretty_reports_a_clean_diff() -> None:
    quiet = DiffResult(base=BASE, target=TARGET, unchanged=RESULT.unchanged)
    assert "No change in mypy errors." in capture(quiet, OutputFormat.PRETTY)


def test_show_unchanged_lists_them() -> None:
    output = capture(RESULT, OutputFormat.PRETTY, show_unchanged=True)
    assert "UNCHANGED (2)" in output
    assert "src/app.py:7:4: error: Bad arg" in output


def test_only_new_hides_the_fixed_section() -> None:
    output = capture(RESULT, OutputFormat.PRETTY, only=Only.NEW)
    assert "+ NEW (1)" in output
    assert "FIXED" not in output


def test_only_fixed_hides_the_new_section() -> None:
    output = capture(RESULT, OutputFormat.PRETTY, only=Only.FIXED)
    assert "- FIXED (1)" in output
    assert "NEW" not in output


def test_notes_are_printed_under_their_error() -> None:
    with_note = DiffResult(
        base=BASE,
        target=TARGET,
        new=(
            MypyMessage(
                path="src/app.py",
                line=1,
                column=0,
                severity=Severity.ERROR,
                message="Bad call",
                code="call-arg",
                notes=("Possible overload variant:",),
            ),
        ),
    )
    assert "note: Possible overload variant:" in capture(with_note, OutputFormat.PRETTY)


def test_json_is_machine_readable() -> None:
    payload = json.loads(capture(RESULT, OutputFormat.JSON))
    assert payload["summary"] == {"new": 1, "fixed": 1, "unchanged": 2, "net": 0}
    assert payload["base"]["commit_id"] == "a" * 40
    assert payload["base"]["change_id"] == "zqxlrpvw"
    assert payload["target"]["working_copy"] is True
    assert payload["new"][0]["code"] == "return-value"
    assert payload["fixed"][0]["code"] == "name-defined"


def test_json_respects_only() -> None:
    payload = json.loads(capture(RESULT, OutputFormat.JSON, only=Only.NEW))
    assert "new" in payload
    assert "fixed" not in payload


def test_github_annotations() -> None:
    output = capture(RESULT, OutputFormat.GITHUB)
    # GitHub columns are 1-based while mypy's are 0-based.
    assert (
        "::error file=src/app.py,line=42,col=5,title=mypy [return-value] (new)::Bad return"
    ) in output
    assert "::notice file=src/app.py,line=10,col=5,title=mypy [name-defined] (fixed)::" in output


def test_github_escapes_newlines_in_messages() -> None:
    multiline = DiffResult(
        base=BASE,
        target=TARGET,
        new=(
            MypyMessage(
                path="a.py",
                line=1,
                column=None,
                severity=Severity.ERROR,
                message="first\nsecond",
                code=None,
            ),
        ),
    )
    output = capture(multiline, OutputFormat.GITHUB)
    # A raw newline would terminate the annotation early.
    assert "first%0Asecond" in output


def test_plain_has_no_ansi_escapes() -> None:
    output = capture(RESULT, OutputFormat.PLAIN)
    assert "\x1b[" not in output
    assert "+ NEW (1)" in output
