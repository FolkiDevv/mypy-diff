"""Output formats."""

from __future__ import annotations

import dataclasses
import io
import json

from rich.console import Console

from mypy_diff.models import DiffResult, MypyMessage, RevisionInfo, Severity
from mypy_diff.render import Only, OutputFormat, render, step_summary

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
    assert "+ src/app.py:42:5: error: Bad return  [return-value]" in output
    assert "- src/app.py:10:5: error: Undefined name  [name-defined]" in output
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
    assert "src/app.py:7:5: error: Bad arg" in output


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
    assert payload["summary"] == {"new": 1, "fixed": 1, "changed": 0, "unchanged": 2, "net": 0}
    assert payload["base"]["commit_id"] == "a" * 40
    assert payload["base"]["change_id"] == "zqxlrpvw"
    assert payload["target"]["working_copy"] is True
    assert payload["new"][0]["code"] == "return-value"
    assert payload["fixed"][0]["code"] == "name-defined"


def test_json_respects_only_but_keeps_every_key() -> None:
    """Consumers can rely on the keys; --only empties a list and says so."""
    payload = json.loads(capture(RESULT, OutputFormat.JSON, only=Only.NEW))
    assert payload["only"] == "new"
    assert len(payload["new"]) == 1
    assert payload["fixed"] == []
    assert payload["changed"] == []
    assert payload["summary"]["fixed"] == 1


def test_github_annotations() -> None:
    output = capture(RESULT, OutputFormat.GITHUB)
    # GitHub columns are 1-based while mypy's are 0-based.
    assert (
        "::error file=src/app.py,line=42,col=5,title=mypy [return-value] (new)::Bad return"
    ) in output
    # A fixed error's line is the base revision's; on the PR head it would point elsewhere.
    assert "::notice file=src/app.py,title=mypy [name-defined] (fixed)::" in output


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


CHANGED = DiffResult(
    base=BASE,
    target=TARGET,
    changed=(
        (
            message(3, "arg-type", 'Argument 1 has type "str"; expected "int"'),
            message(4, "arg-type", 'Argument 1 has type "str"; expected "float"'),
        ),
    ),
)


def test_pretty_shows_changed_errors_with_their_old_text() -> None:
    output = capture(CHANGED, OutputFormat.PRETTY)
    assert "~ CHANGED (1)" in output
    assert '~ src/app.py:4:5: error: Argument 1 has type "str"; expected "float"' in output
    assert '~   was: line 3: Argument 1 has type "str"; expected "int"' in output
    assert "1 changed" in output
    assert "No change" not in output


def test_json_lists_changed_pairs() -> None:
    payload = json.loads(capture(CHANGED, OutputFormat.JSON))
    assert payload["summary"]["changed"] == 1
    assert payload["changed"][0]["old"]["line"] == 3
    assert payload["changed"][0]["new"]["line"] == 4


def test_github_reports_changed_errors_as_notices() -> None:
    output = capture(CHANGED, OutputFormat.GITHUB)
    assert output.startswith("::notice file=src/app.py,line=4,")
    assert "(changed)" in output
    assert "%0Awas: " in output


def test_only_new_hides_changed_errors() -> None:
    assert "CHANGED" not in capture(CHANGED, OutputFormat.PRETTY, only=Only.NEW)


# ------------------------------------------------------------------ JSON schema v1


def test_json_carries_a_schema_version_and_stats() -> None:
    payload = json.loads(capture(RESULT, OutputFormat.JSON))
    assert payload["schema_version"] == 1
    assert payload["stats"] == {"moved": 0, "moved_between_files": 0}
    assert payload["unchanged"] is None
    assert payload["incomplete"] is None


def test_json_lists_unchanged_errors_on_request() -> None:
    payload = json.loads(capture(RESULT, OutputFormat.JSON, show_unchanged=True))
    assert [m["line"] for m in payload["unchanged"]] == [7, 9]


def test_json_says_how_the_base_was_chosen() -> None:
    explained = dataclasses.replace(RESULT, base_reason="fork point with main")
    assert json.loads(capture(explained, OutputFormat.JSON))["base"]["reason"] == (
        "fork point with main"
    )


def test_the_header_says_how_the_base_was_chosen() -> None:
    explained = dataclasses.replace(RESULT, base_reason="fork point with main")
    assert "(fork point with main)" in capture(explained, OutputFormat.PRETTY).splitlines()[0]


def test_hints_are_printed_as_notes() -> None:
    hinted = DiffResult(
        base=BASE,
        target=TARGET,
        new=(
            MypyMessage(
                path="src/app.py",
                line=1,
                column=0,
                severity=Severity.ERROR,
                message='Library stubs not installed for "yaml"',
                code="import-untyped",
                hint='Hint: "python3 -m pip install types-PyYAML"',
            ),
        ),
    )
    output = capture(hinted, OutputFormat.PRETTY)
    assert '+   note: Hint: "python3 -m pip install types-PyYAML"' in output


# ------------------------------------------------------------------ GitHub workflow commands


def test_github_escapes_percent_and_property_separators() -> None:
    """GitHub decodes %25, %0D, %0A in messages, and also %3A, %2C in properties."""
    tricky = DiffResult(
        base=BASE,
        target=TARGET,
        new=(message(3, "misc", "100% sure, see %0A", path="src/a,b:c.py"),),
    )
    output = capture(tricky, OutputFormat.GITHUB)
    assert "file=src/a%2Cb%3Ac.py," in output
    assert output.rstrip().endswith("::100%25 sure, see %250A")


def test_step_summary_has_the_verdict_and_the_errors() -> None:
    summary = step_summary(RESULT)
    assert summary.startswith("## mypy-diff")
    assert "| 1 | 1 | 0 | 2 | +0 |" in summary
    assert "src/app.py:42:5: error: Bad return  [return-value]" in summary
    assert "<details open><summary>New errors (1)</summary>" in summary


def test_step_summary_caps_long_lists() -> None:
    many = DiffResult(
        base=BASE, target=TARGET, new=tuple(message(n, "misc", f"e{n}") for n in range(1, 61))
    )
    assert "... and 10 more" in step_summary(many)
