"""Reports for code-review tools: SARIF 2.1.0 and GitLab Code Quality.

Both describe the *target*: every result has a location in the files under review. That is why
fixed errors are not results — their line numbers belong to the base revision, so on the
reviewed code they would point at whatever now sits there (or into a file that is gone). They
are counted in the SARIF run's properties instead.

Each result carries a fingerprint that survives the code around it moving: file, error code,
the message with its position-dependent parts masked (see
:func:`matching.normalise_message`), and which occurrence of that diagnostic in the file it is.
Review tools use it to follow one finding from report to report.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from . import __version__
from .matching import normalise_message
from .models import DiffResult, MypyMessage, Severity

SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
SARIF_VERSION = "2.1.0"
INFORMATION_URI = "https://github.com/FolkiDevv/mypy-diff"

#: ruleId of a diagnostic mypy gave no error code (a standalone note, say).
_NO_CODE = "mypy"


@dataclass(frozen=True, slots=True)
class Entry:
    """One diagnostic in the target, and what became of it."""

    #: "new", "changed" or "unchanged".
    kind: str
    message: MypyMessage
    #: For "changed": the base's wording of the same error.
    was: MypyMessage | None = None


def fingerprints(messages: Iterable[MypyMessage]) -> dict[MypyMessage, str]:
    """A stable, unique fingerprint for each diagnostic of the target.

    Computed over *all* of the target's diagnostics, whatever a report then shows, so that one
    error keeps its fingerprint whether or not `--only` or `--show-unchanged` let its
    neighbours in.
    """
    seen: Counter[tuple[str, str, str]] = Counter()
    found: dict[MypyMessage, str] = {}
    for message in sorted(messages, key=lambda m: (m.path, m.line, m.column or 0)):
        identity = (message.path, message.code or "", normalise_message(message.message))
        occurrence = seen[identity]
        seen[identity] += 1
        raw = "\0".join((*identity, str(occurrence)))
        found[message] = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return found


def target_messages(result: DiffResult) -> list[MypyMessage]:
    """Every diagnostic of the target: new, changed (its new wording) and unchanged."""
    return [*result.new, *(new for _, new in result.changed), *result.unchanged]


def _text(entry: Entry) -> str:
    lines = [entry.message.message, *(f"note: {note}" for note in entry.message.note_lines())]
    if entry.was is not None:
        lines.append(f"was: {entry.was.message}")
    return "\n".join(lines)


# ------------------------------------------------------------------ SARIF

_SARIF_LEVELS = {Severity.ERROR: "error", Severity.WARNING: "warning", Severity.NOTE: "note"}
_BASELINE_STATES = {"new": "new", "changed": "updated", "unchanged": "unchanged"}


def sarif(
    result: DiffResult, entries: Sequence[Entry], properties: dict[str, Any]
) -> dict[str, Any]:
    """A SARIF 2.1.0 log with one run. ``baselineState`` says new, updated or unchanged."""
    marks = fingerprints(target_messages(result))
    rules = sorted({entry.message.code or _NO_CODE for entry in entries})
    run: dict[str, Any] = {
        "tool": {
            "driver": {
                "name": "mypy-diff",
                "version": __version__,
                "informationUri": INFORMATION_URI,
                "rules": [
                    {"id": rule, "shortDescription": {"text": f"mypy [{rule}]"}} for rule in rules
                ],
            }
        },
        "results": [_sarif_result(entry, marks.get(entry.message, "")) for entry in entries],
        "properties": properties,
    }
    if result.incomplete:
        run["invocations"] = [
            {
                "executionSuccessful": True,
                "toolExecutionNotifications": [
                    {"level": "warning", "message": {"text": result.incomplete}}
                ],
            }
        ]
    return {"$schema": SARIF_SCHEMA, "version": SARIF_VERSION, "runs": [run]}


def _sarif_result(entry: Entry, fingerprint: str) -> dict[str, Any]:
    message = entry.message
    physical: dict[str, Any] = {"artifactLocation": {"uri": message.path, "uriBaseId": "%SRCROOT%"}}
    if message.line >= 1:
        region: dict[str, int] = {"startLine": message.line}
        if message.column is not None and message.column >= 0:
            # mypy's columns are 0-based; SARIF's are 1-based.
            region["startColumn"] = message.column + 1
        physical["region"] = region
    result: dict[str, Any] = {
        "ruleId": message.code or _NO_CODE,
        "level": _SARIF_LEVELS[message.severity],
        "message": {"text": _text(entry)},
        "locations": [{"physicalLocation": physical}],
        "baselineState": _BASELINE_STATES[entry.kind],
    }
    if fingerprint:
        result["partialFingerprints"] = {"mypyDiff/v1": fingerprint}
    return result


# ------------------------------------------------------------------ GitLab Code Quality

_GITLAB_SEVERITIES = {Severity.ERROR: "major", Severity.WARNING: "minor", Severity.NOTE: "info"}


def gitlab(result: DiffResult, entries: Sequence[Entry]) -> list[dict[str, Any]]:
    """A GitLab Code Quality report: a JSON array of issues, each with a unique fingerprint."""
    marks = fingerprints(target_messages(result))
    issues: list[dict[str, Any]] = []
    used: set[str] = set()
    for entry in entries:
        message = entry.message
        fingerprint = marks.get(message) or _fallback_fingerprint(message)
        # GitLab drops an issue whose fingerprint it has already seen in the report. Two equal
        # diagnostics (mypy does repeat itself) must both stay.
        while fingerprint in used:
            fingerprint = hashlib.sha256(fingerprint.encode("ascii")).hexdigest()[:32]
        used.add(fingerprint)
        issues.append(
            {
                "type": "issue",
                "check_name": message.code or _NO_CODE,
                "description": _text(entry),
                "categories": ["Bug Risk"],
                "severity": _GITLAB_SEVERITIES[message.severity],
                "fingerprint": fingerprint,
                "location": {"path": message.path, "lines": {"begin": max(message.line, 1)}},
            }
        )
    return issues


def _fallback_fingerprint(message: MypyMessage) -> str:
    raw = "\0".join((message.path, str(message.line), message.code or "", message.message))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
