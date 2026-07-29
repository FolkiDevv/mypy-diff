"""Locate mypy, run it against a directory, and turn its JSON output into messages.

We deliberately run the *project's* mypy rather than one of our own: mypy-diff is installed
as an isolated uv tool, so its own environment has none of the project's dependencies or
stubs and would report a flood of spurious import/attribute errors.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import MypyDiffError
from .models import MypyMessage, Severity
from .preflight import MIN_MYPY, ToolInfo, probe, require

#: Flags we always add. `--output=json` is the only format we parse.
_FORCED_ARGS = (
    "--output=json",
    "--no-error-summary",
    "--no-pretty",
    "--no-color-output",
)

_BIN_DIR = "Scripts" if sys.platform == "win32" else "bin"
_MYPY_EXE = "mypy.exe" if sys.platform == "win32" else "mypy"


@dataclass(frozen=True, slots=True)
class MypyRun:
    """The outcome of one mypy invocation."""

    messages: tuple[MypyMessage, ...]
    returncode: int
    stdout: str
    stderr: str
    skipped_lines: int


def resolve_mypy(root: Path, explicit: str | None = None) -> tuple[ToolInfo, str]:
    """Find a usable mypy. Returns the tool and a human-readable description of how.

    Order: --mypy-cmd, $MYPY_DIFF_MYPY, the active virtualenv, the project's .venv,
    `uv run --project`, then PATH.
    """
    for command, source in _mypy_candidates(root, explicit):
        tool = probe("mypy", command)
        if tool is not None:
            return require("mypy", tool, MIN_MYPY), source

    raise MypyDiffError(
        "Could not find a usable `mypy`.",
        "mypy-diff runs the project's own mypy so that its dependencies and stubs resolve.",
        "Point it at one explicitly:",
        "  --mypy-cmd 'uv run mypy'      (or set MYPY_DIFF_MYPY)",
        "or install mypy into the project: `uv add --dev mypy`",
    )


def _mypy_candidates(root: Path, explicit: str | None) -> list[tuple[tuple[str, ...], str]]:
    candidates: list[tuple[tuple[str, ...], str]] = []

    if explicit:
        candidates.append((tuple(shlex.split(explicit)), "--mypy-cmd"))
        # An explicit command is a promise; do not silently fall back past it.
        return candidates

    from_env = os.environ.get("MYPY_DIFF_MYPY")
    if from_env:
        candidates.append((tuple(shlex.split(from_env)), "$MYPY_DIFF_MYPY"))
        return candidates

    active_venv = os.environ.get("VIRTUAL_ENV")
    if active_venv:
        candidates.append(((str(Path(active_venv) / _BIN_DIR / _MYPY_EXE),), "active virtualenv"))

    candidates.append(((str(root / ".venv" / _BIN_DIR / _MYPY_EXE),), "project .venv"))

    uv = shutil.which("uv")
    if uv is not None and (root / "pyproject.toml").exists():
        candidates.append(((uv, "run", "--project", str(root), "mypy"), "uv run --project"))

    on_path = shutil.which("mypy")
    if on_path is not None:
        candidates.append(((on_path,), "PATH"))

    return candidates


def run_mypy(
    mypy: ToolInfo,
    workdir: Path,
    mypy_args: tuple[str, ...],
    cache_dir: Path,
) -> MypyRun:
    """Run mypy inside ``workdir`` and parse its diagnostics.

    ``cache_dir`` is per-revision so the two runs never invalidate each other's incremental
    cache — and so neither of them touches the developer's own ``.mypy_cache``.
    """
    argv = [*mypy.command, *_FORCED_ARGS, f"--cache-dir={cache_dir}", *mypy_args]
    completed = subprocess.run(
        argv,
        cwd=workdir,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "MYPY_FORCE_COLOR": "0", "NO_COLOR": "1"},
    )

    if completed.returncode >= 2:
        raise MypyDiffError(
            f"mypy failed with exit code {completed.returncode}.",
            *_fatal_details(completed),
        )

    messages, skipped = parse_json_output(completed.stdout, workdir)
    return MypyRun(
        messages=messages,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        skipped_lines=skipped,
    )


def _fatal_details(completed: subprocess.CompletedProcess[str]) -> list[str]:
    details = [
        line for line in (completed.stderr or completed.stdout).strip().splitlines() if line.strip()
    ]
    if any("Cannot read file" in line or "no files" in line.lower() for line in details):
        details.append("")
        details.append("Pass the paths to check after `--`, e.g.:  mypy-diff-git -- src tests")
    return details


def parse_json_output(stdout: str, workdir: Path) -> tuple[tuple[MypyMessage, ...], int]:
    """Parse mypy's `--output=json` stream.

    Each diagnostic is one JSON object per line. Non-JSON lines (a stray summary, a warning
    printed by a plugin) are counted and skipped rather than crashing the run.
    """
    messages: list[MypyMessage] = []
    pending_notes: list[str] = []
    skipped = 0

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if not isinstance(raw, dict) or "message" not in raw:
            skipped += 1
            continue

        severity = Severity(raw.get("severity") or "error")
        if severity is Severity.NOTE and messages:
            # A note is elaboration on the diagnostic above it, not a diagnostic of its own.
            pending_notes.append(str(raw["message"]))
            messages[-1] = _with_notes(messages[-1], tuple(pending_notes))
            continue

        pending_notes = []
        messages.append(
            MypyMessage(
                path=_normalize_path(raw.get("file") or "", workdir),
                line=int(raw.get("line") or 0),
                column=_optional_int(raw.get("column")),
                severity=severity,
                message=str(raw["message"]),
                code=raw.get("code") or None,
                hint=str(raw.get("hint") or ""),
            )
        )

    return tuple(messages), skipped


def _with_notes(message: MypyMessage, notes: tuple[str, ...]) -> MypyMessage:
    return MypyMessage(
        path=message.path,
        line=message.line,
        column=message.column,
        severity=message.severity,
        message=message.message,
        code=message.code,
        hint=message.hint,
        notes=notes,
    )


def _optional_int(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _normalize_path(raw: str, workdir: Path) -> str:
    """Make paths comparable between a working-copy run and a temporary-worktree run.

    Both runs must produce the same key for the same file, so everything becomes a
    POSIX-style path relative to the directory mypy was invoked in.
    """
    if not raw:
        return ""
    path = Path(raw)
    if path.is_absolute():
        try:
            path = path.relative_to(workdir)
        except ValueError:
            try:
                path = Path(os.path.relpath(path, workdir))
            except ValueError:
                # Different drive on Windows — nothing sensible to relativize against.
                return path.as_posix()
    return path.as_posix()
