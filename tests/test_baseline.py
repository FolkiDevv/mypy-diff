"""The baseline file: what it records, and how a bad one is reported."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mypy_diff import MypyDiffError
from mypy_diff.baseline import BASELINE_SCHEMA_VERSION, read_baseline, write_baseline
from mypy_diff.models import MypyMessage, RevisionInfo, Severity

REVISION = RevisionInfo(commit_id="c" * 40, short_id="cccccccc", refs=("main",), description="x")


def message(path: str, line: int, severity: Severity = Severity.ERROR) -> MypyMessage:
    return MypyMessage(
        path=path, line=line, column=0, severity=severity, message="Bad", code="misc"
    )


def test_a_baseline_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    messages = (message("src/b.py", 3), message("src/a.py", 9), message("src/a.py", 2))

    count = write_baseline(path, messages, REVISION, "mypy 2.3.0", ("src",), "backend")
    loaded = read_baseline(path)

    assert count == 3
    assert [(m.path, m.line) for m in loaded.messages] == [
        ("src/a.py", 2),
        ("src/a.py", 9),
        ("src/b.py", 3),
    ]
    assert loaded.revision == REVISION
    assert loaded.mypy_version == "mypy 2.3.0"
    assert loaded.mypy_args == ("src",)
    assert loaded.project == "backend"
    assert loaded.has_file_contents


def test_standalone_notes_are_not_recorded(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    write_baseline(
        path, (message("a.py", 1), message("a.py", 2, Severity.NOTE)), REVISION, "m", (), "."
    )
    assert len(read_baseline(path).messages) == 1


def test_the_file_is_stable_and_diffable(tmp_path: Path) -> None:
    """Sorted and indented: committing a new baseline changes one line block per error."""
    path = tmp_path / "baseline.json"
    write_baseline(path, (message("a.py", 1),), REVISION, "m", (), ".")
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert json.loads(text)["schema_version"] == BASELINE_SCHEMA_VERSION
    assert '\n  "messages": [\n' in text


def test_a_working_copy_baseline_has_no_file_contents(tmp_path: Path) -> None:
    """Its errors are from files on disk that no commit holds."""
    path = tmp_path / "baseline.json"
    dirty = RevisionInfo(commit_id="c" * 40, short_id="cccccccc", is_working_copy=True)
    write_baseline(path, (), dirty, "m", (), ".")

    loaded = read_baseline(path)

    assert not loaded.has_file_contents
    assert not loaded.as_base().is_working_copy


def _baseline_with(**fields: object) -> str:
    """A baseline whose one message has ``fields`` changed."""
    entry = {**message("a.py", 1).to_json(), **fields}
    revision = {"commit_id": "c" * 40, "short_id": "cccccccc"}
    return json.dumps(
        {"schema_version": 1, "mypy_version": "m", "revision": revision, "messages": [entry]}
    )


def test_a_missing_baseline_says_how_to_make_one(tmp_path: Path) -> None:
    with pytest.raises(MypyDiffError) as raised:
        read_baseline(tmp_path / "nope.json", prog="mypy-diff")
    assert "No baseline at" in raised.value.message
    assert any("--write-baseline" in detail for detail in raised.value.details)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("{not json", "is not a mypy-diff baseline"),
        ("[]", "has no schema_version"),
        ('{"schema_version": 1}', "is not a valid mypy-diff baseline"),
        ('{"schema_version": 99}', "schema version 99"),
        # Valid JSON, but not what --write-baseline writes: rejected here, not mid-matching.
        (_baseline_with(message=None), "is not a valid mypy-diff baseline"),
        (_baseline_with(line="3"), "is not a valid mypy-diff baseline"),
        (_baseline_with(line=True), "is not a valid mypy-diff baseline"),
        (_baseline_with(notes=[1]), "is not a valid mypy-diff baseline"),
    ],
)
def test_a_bad_baseline_is_an_error(tmp_path: Path, content: str, expected: str) -> None:
    path = tmp_path / "baseline.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(MypyDiffError) as raised:
        read_baseline(path)
    assert expected in raised.value.message


def test_a_newer_baseline_asks_for_an_upgrade(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    path.write_text('{"schema_version": 2}', encoding="utf-8")
    with pytest.raises(MypyDiffError) as raised:
        read_baseline(path)
    assert raised.value.details == ("Upgrade mypy-diff.",)


def test_mismatches_name_what_differs(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    write_baseline(path, (), REVISION, "mypy 2.3.0", ("src",), ".")
    loaded = read_baseline(path)

    assert loaded.mismatches("mypy 2.3.0", ("src",), ".") == []
    found = loaded.mismatches("mypy 2.4.0", ("src", "tests"), "backend")
    assert len(found) == 3
    assert "mypy 2.3.0" in found[0] and "mypy 2.4.0" in found[0]
    assert "src tests" in found[1]
    assert "`backend`" in found[2]
