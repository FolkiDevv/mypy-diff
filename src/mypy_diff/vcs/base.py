"""The contract every VCS backend implements, plus subprocess plumbing they share."""

from __future__ import annotations

import itertools
import os
import subprocess
from collections.abc import Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol

from .. import MypyDiffError
from ..models import RevisionInfo


class CommandError(MypyDiffError):
    """A VCS subprocess exited non-zero."""

    def __init__(self, argv: list[str], returncode: int, stderr: str) -> None:
        super().__init__(
            f"`{' '.join(argv[:2])}` failed with exit code {returncode}",
            *(line for line in stderr.strip().splitlines() if line.strip()),
        )
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr


class Runner:
    """Thin subprocess wrapper with a fixed executable, cwd and verbosity."""

    def __init__(self, executable: str, cwd: Path | None = None, verbose: bool = False) -> None:
        self.executable = executable
        self.cwd = cwd
        self.verbose = verbose
        self.log: list[str] = []

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        argv = [self.executable, *args]
        self.log.append(" ".join(argv))
        completed = subprocess.run(
            argv,
            cwd=self.cwd,
            capture_output=True,
            text=True,
            check=False,
            # Force machine-readable, locale-independent output.
            env={**os.environ, "LC_ALL": "C", "NO_COLOR": "1"},
        )
        if check and completed.returncode != 0:
            raise CommandError(argv, completed.returncode, completed.stderr)
        return completed

    def out(self, *args: str) -> str:
        """Run and return stripped stdout."""
        return self.run(*args).stdout.strip()

    def lines(self, *args: str) -> list[str]:
        """Run and return non-empty stdout lines."""
        return [line for line in self.run(*args).stdout.splitlines() if line.strip()]

    def try_out(self, *args: str) -> str | None:
        """Run and return stripped stdout, or None if the command failed."""
        completed = self.run(*args, check=False)
        if completed.returncode != 0:
            return None
        return completed.stdout.strip()


class VcsBackend(Protocol):
    """What the rest of mypy-diff needs from a version control system."""

    name: str

    def root(self) -> Path:
        """Absolute path to the repository/workspace root."""
        ...

    def resolve(self, revspec: str) -> RevisionInfo:
        """Turn a user-supplied revision (branch name, revset, SHA) into a concrete revision."""
        ...

    def default_base(self) -> RevisionInfo:
        """The latest named ancestor that has been pushed to git."""
        ...

    def fork_point(self, rev: RevisionInfo) -> RevisionInfo:
        """The common ancestor of ``rev`` and the current revision."""
        ...

    def working_copy(self) -> RevisionInfo:
        """The dirty on-disk state; its ``commit_id`` is the commit it is based on."""
        ...

    def worktree(self, rev: RevisionInfo) -> AbstractContextManager[Path]:
        """A temporary checkout of ``rev``, removed on exit."""
        ...

    def file_at(self, rev: RevisionInfo, path: str) -> str | None:
        """Contents of one file in ``rev``, or None if it does not exist there."""
        ...

    def cleanup_stale(self) -> None:
        """Drop leftovers from runs that crashed before their worktree was removed."""
        ...

    def command_log(self) -> Iterator[str]:
        """Every VCS command this backend ran, for --verbose."""
        ...


def unique_worktree_name(prefix: str = "mypy-diff") -> str:
    """A worktree name that will not collide with a concurrent run of the tool."""
    return f"{prefix}-{os.getpid()}-{next(_worktree_counter)}"


_worktree_counter = itertools.count()
