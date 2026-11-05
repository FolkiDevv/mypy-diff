"""Core data types shared by every layer of mypy-diff."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Self

# Sentinel revspec meaning "whatever is on disk right now, including uncommitted changes".
WORKING_COPY = "<working-copy>"


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    NOTE = "note"


@dataclass(frozen=True, slots=True)
class RevisionInfo:
    """A single resolved revision, in backend-agnostic terms.

    ``commit_id`` is a git SHA for both backends (jj uses a git backend), which is what
    makes the on-disk result cache shared between ``mypy-diff-jj`` and ``mypy-diff-git``.
    """

    commit_id: str
    short_id: str
    refs: tuple[str, ...] = ()
    description: str = ""
    #: True when this "revision" is really the dirty working copy on disk.
    is_working_copy: bool = False
    #: jj change id; empty for the git backend.
    change_id: str = ""
    #: True when this is a commit mypy-diff made of git's index (``--staged``).
    is_staged: bool = False

    def label(self) -> str:
        if self.is_working_copy:
            return "working copy (uncommitted)"
        if self.is_staged:
            return f"{self.short_id}  staged changes"
        parts = [self.change_id, self.short_id] if self.change_id else [self.short_id]
        if self.refs:
            parts.append(" ".join(self.refs))
        if self.description:
            parts.append(f'"{self.description}"')
        return "  ".join(p for p in parts if p)


@dataclass(frozen=True, slots=True)
class MypyMessage:
    """One diagnostic emitted by mypy.

    ``path`` is always POSIX-style and relative to the repository root, so that a run against
    the working copy and a run inside a temporary worktree produce comparable keys.
    """

    path: str
    line: int
    column: int | None
    severity: Severity
    message: str
    code: str | None = None
    hint: str = ""
    #: Follow-up ``note:`` diagnostics attached to this message. Not diffed on their own.
    notes: tuple[str, ...] = ()

    @property
    def identity(self) -> tuple[str, int, int | None, str, str]:
        """Exact-match key: same file, same position, same diagnostic."""
        return (self.path, self.line, self.column, self.code or "", self.message)

    def format(self) -> str:
        """Render exactly the way mypy itself would, so output stays greppable and clickable.

        mypy's JSON column is 0-based; its text output, and every editor, count from 1.
        """
        position = f"{self.path}:{self.line}"
        if self.column is not None and self.column >= 0:
            position += f":{self.column + 1}"
        suffix = f"  [{self.code}]" if self.code else ""
        return f"{position}: {self.severity.value}: {self.message}{suffix}"

    def note_lines(self) -> list[str]:
        """mypy's notes for this diagnostic: its hint (same location), then attached notes."""
        hint = [line for line in self.hint.splitlines() if line.strip()]
        return [*hint, *self.notes]

    def to_json(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "line": self.line,
            "column": self.column,
            "severity": self.severity.value,
            "message": self.message,
            "code": self.code,
            "hint": self.hint,
            "notes": list(self.notes),
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Self:
        return cls(
            path=raw["path"],
            line=raw["line"],
            column=raw["column"],
            severity=Severity(raw["severity"]),
            message=raw["message"],
            code=raw["code"],
            hint=raw.get("hint", ""),
            notes=tuple(raw.get("notes", ())),
        )


@dataclass(frozen=True, slots=True)
class DiffResult:
    base: RevisionInfo
    target: RevisionInfo
    new: tuple[MypyMessage, ...] = ()
    fixed: tuple[MypyMessage, ...] = ()
    unchanged: tuple[MypyMessage, ...] = ()
    #: Pre-existing errors whose text changed in place: (base message, target message).
    changed: tuple[tuple[MypyMessage, MypyMessage], ...] = ()
    #: Set when the comparison could not be done in full; says why. The lists are then partial.
    incomplete: str = ""
    #: How the base was chosen, for display: "fork point with main", "latest pushed ancestor".
    base_reason: str = ""
    #: Matching statistics: "moved" (unchanged errors that matched at another position),
    #: "moved_between_files" (the subset that moved to another file).
    stats: dict[str, int] = field(default_factory=dict)

    @property
    def unchanged_files(self) -> int:
        return len({m.path for m in self.unchanged})

    @property
    def has_new(self) -> bool:
        return bool(self.new)
