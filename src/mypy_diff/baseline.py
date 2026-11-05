"""Baseline files: the errors of one revision, recorded, to compare later runs against.

`--write-baseline FILE` checks the target and records its errors; `--baseline FILE` compares a
later target with that record instead of with a checkout of a base revision. That needs no
history at all — a shallow clone in CI is fine — and the record can be committed next to the
code, so "no new errors" means the same thing on every machine.

The matching tiers that follow a line through an edit need the base's file contents. A baseline
written from a clean commit records that commit, and when the repository has it the files are
read from there. A baseline written from uncommitted changes records which commit it was based on
but not the edits, so its files are unknown: only the tiers that need no text apply (exact
position, then same message anywhere in the file).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import MypyDiffError, __version__
from .models import MypyMessage, RevisionInfo, Severity

#: Bumped on any incompatible change to the file.
BASELINE_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class Baseline:
    path: Path
    messages: tuple[MypyMessage, ...]
    #: The revision the errors are from.
    revision: RevisionInfo
    mypy_version: str
    mypy_args: tuple[str, ...]
    #: The project directory, relative to the repository root.
    project: str
    #: Text of the files with errors that the recorded revision does not hold, by path: what
    #: --prepare made or rewrote in the checkout. Matching follows lines through it.
    generated: Mapping[str, str] = field(default_factory=dict)

    @property
    def has_file_contents(self) -> bool:
        """Whether the recorded revision's files are the files the errors were found in."""
        return bool(self.revision.commit_id) and not self.revision.is_working_copy

    def as_base(self) -> RevisionInfo:
        """The recorded revision, as the base of a comparison.

        Never flagged as the working copy, even when it was written from one: that flag means
        "what is on disk now", which a recording is not.
        """
        return RevisionInfo(
            commit_id=self.revision.commit_id,
            short_id=self.revision.short_id,
            refs=self.revision.refs,
            description=self.revision.description,
            change_id=self.revision.change_id,
        )

    def mismatches(self, mypy_version: str, mypy_args: tuple[str, ...], project: str) -> list[str]:
        """Differences between how the baseline was made and how this run checks."""
        found: list[str] = []
        if mypy_version != self.mypy_version:
            found.append(
                f"{self.path} was written with {self.mypy_version}; this run uses {mypy_version}. "
                "Errors whose wording changed between the two show up as changed or new."
            )
        if mypy_args != self.mypy_args:
            found.append(
                f"{self.path} was written with other mypy arguments "
                f"({' '.join(self.mypy_args) or 'none'}; now {' '.join(mypy_args) or 'none'})."
            )
        if project != self.project:
            found.append(
                f"{self.path} records the project in `{self.project}`; this run checks `{project}`."
            )
        return found


def write_baseline(
    path: Path,
    messages: tuple[MypyMessage, ...],
    revision: RevisionInfo,
    mypy_version: str,
    mypy_args: tuple[str, ...],
    project: str,
    generated: Mapping[str, str] | None = None,
) -> int:
    """Record ``messages`` in ``path``; returns how many were recorded.

    ``generated`` is the text of the files whose errors were found in text the revision does
    not hold (``--prepare`` made or rewrote them). It is recorded too: a later comparison could
    not read it from the revision, and the revision's text, where it has one, is the wrong one.

    Notes on their own are left out: they are never compared. The file is sorted and indented so
    that a committed baseline changes by one line per error.
    """
    kept = sorted(
        (m for m in messages if m.severity is not Severity.NOTE),
        key=lambda m: (m.path, m.line, m.column if m.column is not None else -1, m.message),
    )
    payload: dict[str, Any] = {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "written_by": f"mypy-diff {__version__}",
        "mypy_version": mypy_version,
        "mypy_args": list(mypy_args),
        "project": project,
        "revision": {
            "commit_id": revision.commit_id,
            "short_id": revision.short_id,
            "change_id": revision.change_id or None,
            "refs": list(revision.refs),
            "description": revision.description,
            "working_copy": revision.is_working_copy,
        },
        "generated": dict(sorted((generated or {}).items())),
        "messages": [message.to_json() for message in kept],
    }
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    except OSError as failure:
        temporary.unlink(missing_ok=True)
        raise MypyDiffError(f"Could not write the baseline {path}: {failure}") from failure
    return len(kept)


def _texts(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(path, str) and isinstance(text, str) for path, text in value.items()
    ):
        raise TypeError("`generated` must map paths to file contents")
    return value


def read_baseline(path: Path, prog: str = "mypy-diff") -> Baseline:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise MypyDiffError(
            f"No baseline at {path}.", f"Record one with `{prog} --write-baseline {path}`."
        ) from None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as failure:
        raise MypyDiffError(f"{path} is not a mypy-diff baseline: {failure}") from None

    if not isinstance(raw, dict) or "schema_version" not in raw:
        raise MypyDiffError(f"{path} is not a mypy-diff baseline: it has no schema_version.")
    version = raw["schema_version"]
    if version != BASELINE_SCHEMA_VERSION:
        newer = isinstance(version, int) and version > BASELINE_SCHEMA_VERSION
        raise MypyDiffError(
            f"{path} has baseline schema version {version!r}; this mypy-diff reads "
            f"{BASELINE_SCHEMA_VERSION}.",
            "Upgrade mypy-diff." if newer else f"Record it again with `--write-baseline {path}`.",
        )
    try:
        revision = raw["revision"]
        return Baseline(
            path=path,
            messages=tuple(MypyMessage.from_json(item) for item in raw["messages"]),
            revision=RevisionInfo(
                commit_id=str(revision["commit_id"]),
                short_id=str(revision["short_id"]),
                refs=tuple(str(ref) for ref in revision.get("refs", ())),
                description=str(revision.get("description", "")),
                is_working_copy=bool(revision.get("working_copy", False)),
                change_id=str(revision.get("change_id") or ""),
            ),
            mypy_version=str(raw["mypy_version"]),
            mypy_args=tuple(str(argument) for argument in raw.get("mypy_args", ())),
            project=str(raw.get("project", ".")),
            generated=_texts(raw.get("generated", {})),
        )
    except (KeyError, TypeError, ValueError, AttributeError) as failure:
        raise MypyDiffError(f"{path} is not a valid mypy-diff baseline: {failure!r}") from None
