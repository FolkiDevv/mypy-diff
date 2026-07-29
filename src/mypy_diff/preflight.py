"""Detect the external tools we shell out to and enforce minimum versions.

Runs before anything else so a missing or too-old tool produces one readable list of
problems instead of a confusing failure five subprocess calls later.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass

from . import MypyDiffError

# jj: `fork_point()` and `jj file show` are the newest revset/command features we rely on.
MIN_JJ = (0, 28, 0)
MIN_GIT = (2, 25, 0)
# mypy 1.11 is the first release with `--output=json`, which is the only output format we parse.
MIN_MYPY = (1, 11, 0)

_VERSION_RE = re.compile(r"(\d+(?:\.\d+)*)")


def parse_version(text: str) -> tuple[int, ...] | None:
    """Pull a version tuple out of a `--version` line.

    Tolerant on purpose — the real-world strings we have to handle are
    ``jj 0.43.0-89f62ede8c1c611eaf134c0c49252efd65c7945d``,
    ``git version 2.55.0.windows.3`` and ``mypy 1.13.0 (compiled: yes)``.
    """
    match = _VERSION_RE.search(text)
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))[:3]


def format_version(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)


def _install_hint(tool: str) -> tuple[str, ...]:
    windows = sys.platform == "win32"
    match tool:
        case "jj":
            install = (
                "winget install jj-vcs.jj"
                if windows
                else "brew install jj  (or: cargo binstall jj-cli)"
            )
            return (
                f"Install: {install}",
                "Docs: https://jj-vcs.github.io/jj/latest/install-and-setup/",
                "After installing, restart your terminal so PATH picks it up.",
            )
        case "git":
            install = "winget install Git.Git" if windows else "brew install git"
            return (f"Install: {install}", "Docs: https://git-scm.com/downloads")
        case "mypy":
            return (
                "Install it into the project you are checking, e.g. `uv add --dev mypy`,",
                "or point mypy-diff at one explicitly: --mypy-cmd 'uv run mypy'",
            )
        case _:  # pragma: no cover - defensive
            return ()


@dataclass(frozen=True, slots=True)
class ToolInfo:
    """What we found on disk for one external tool."""

    name: str
    command: tuple[str, ...]
    version: tuple[int, ...] | None
    raw_version: str

    @property
    def display_version(self) -> str:
        if self.version is None:
            return self.raw_version.strip() or "unknown"
        return format_version(self.version)


def probe(name: str, command: tuple[str, ...]) -> ToolInfo | None:
    """Run ``command --version`` and return what it reports, or None if unrunnable."""
    try:
        completed = subprocess.run(
            [*command, "--version"],
            capture_output=True,
            check=False,
            timeout=60,
            # Decode as UTF-8, not the locale codepage: a version banner with a non-ASCII
            # byte must not crash the preflight check.
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    raw = (completed.stdout or completed.stderr).strip()
    return ToolInfo(name=name, command=command, version=parse_version(raw), raw_version=raw)


def find_tool(name: str) -> ToolInfo | None:
    """Locate ``name`` on PATH and probe its version."""
    executable = shutil.which(name)
    if executable is None:
        return None
    return probe(name, (executable,))


def check(name: str, tool: ToolInfo | None, minimum: tuple[int, ...]) -> list[str]:
    """Return the problems (as printable lines) with one tool. Empty means it is fine."""
    want = format_version(minimum)
    if tool is None:
        return [
            f"`{name}` was not found on PATH (mypy-diff needs >= {want}).",
            *_install_hint(name),
        ]
    if tool.version is None:
        # Unparseable version is not worth blocking on; --verbose surfaces it.
        return []
    if tool.version < minimum:
        return [
            f"`{name}` {format_version(tool.version)} is too old (mypy-diff needs >= {want}).",
            *_install_hint(name),
        ]
    return []


def require(name: str, tool: ToolInfo | None, minimum: tuple[int, ...]) -> ToolInfo:
    """Like :func:`check`, but raises on the first problem and returns the tool otherwise."""
    problems = check(name, tool, minimum)
    if problems:
        raise MypyDiffError(problems[0], *problems[1:])
    assert tool is not None
    return tool


def require_vcs(backend_name: str) -> ToolInfo:
    """Preflight the VCS one command needs. ``mypy`` is checked later, once resolved."""
    minimum = MIN_JJ if backend_name == "jj" else MIN_GIT
    return require(backend_name, find_tool(backend_name), minimum)
