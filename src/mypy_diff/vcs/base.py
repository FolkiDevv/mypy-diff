"""The contract every VCS backend implements, plus subprocess plumbing they share."""

from __future__ import annotations

import itertools
import os
import subprocess
import tempfile
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


#: Variables git exports to hooks that must not leak into our own git calls. Inside a pre-commit
#: hook GIT_INDEX_FILE names the index of the commit being made — with `git commit -a` a
#: temporary one. `git worktree add` would then write the base revision's tree into it, and the
#: commit would silently record that tree instead of the user's changes.
_HOOK_VARIABLES = ("GIT_INDEX_FILE",)


def subprocess_env() -> dict[str, str]:
    """The environment for VCS subprocesses: machine-readable, locale-independent, hook-safe."""
    env = {key: value for key, value in os.environ.items() if key not in _HOOK_VARIABLES}
    env.update({"LC_ALL": "C", "NO_COLOR": "1"})
    return env


class Runner:
    """Thin subprocess wrapper with a fixed executable, cwd and verbosity."""

    def __init__(self, executable: str, cwd: Path | None = None, verbose: bool = False) -> None:
        self.executable = executable
        self.cwd = cwd
        self.verbose = verbose
        self.log: list[str] = []

    def run(
        self, *args: str, check: bool = True, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        argv = [self.executable, *args]
        self.log.append(" ".join(argv))
        completed = subprocess.run(
            argv,
            cwd=self.cwd,
            capture_output=True,
            check=False,
            # jj and git emit UTF-8. `text=True` alone decodes with the locale codepage
            # (cp1252 on a stock Windows box), which dies on the first non-ASCII byte — a
            # Cyrillic commit message, or a Unicode arrow in a jj hint. The decode happens in
            # subprocess's reader thread, so the failure surfaces as stdout being None.
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**subprocess_env(), **(env or {})},
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
    #: Things worth knowing about the comparison, shown with --verbose only.
    notes: list[str]
    #: Things the user must know about the comparison, shown on every run.
    warnings: list[str]
    #: How the last base was chosen ("latest pushed ancestor", "fork point with main").
    base_reason: str

    def root(self) -> Path:
        """Absolute path to the repository/workspace root."""
        ...

    def resolve(self, revspec: str) -> RevisionInfo:
        """Turn a user-supplied revision (branch name, revset, SHA) into a concrete revision."""
        ...

    def default_base(self, target: RevisionInfo | None = None) -> RevisionInfo:
        """The latest named ancestor of ``target`` that has been pushed to git.

        ``target`` defaults to the working copy. It matters: with ``--to X`` the base must be
        derived from X, not from wherever the working copy happens to be.
        """
        ...

    def resolve_base(self, revspec: str, target: RevisionInfo, tip: bool) -> RevisionInfo:
        """The base for ``-b REVSPEC``: its fork point with ``target``, or its tip with ``tip``."""
        ...

    def fork_point(self, rev: RevisionInfo, target: RevisionInfo | None = None) -> RevisionInfo:
        """The common ancestor of ``rev`` and ``target`` (default: the working copy)."""
        ...

    def remote_branch(self, name: str) -> str:
        """A revision spelling for the remote copy of branch ``name``."""
        ...

    def has_uncommitted_changes(self) -> bool:
        """Whether the working copy on disk differs from its commit (``working_copy()``)."""
        ...

    def staged(self) -> RevisionInfo:
        """The staged changes, as a revision of their own (git's index; jj has none)."""
        ...

    def same_tree(self, base: RevisionInfo, target: RevisionInfo) -> bool:
        """Whether ``target`` (possibly the working copy) holds exactly the files of ``base``."""
        ...

    def is_shallow(self) -> bool:
        """Whether the repository is a shallow clone."""
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

    def ignored_python_files(self, limit: int = 5) -> list[str]:
        """Up to ``limit`` Python files in the working copy that a clean checkout lacks."""
        ...

    def command_log(self) -> Iterator[str]:
        """Every VCS command this backend ran, for --verbose."""
        ...


def temporary_parent() -> Path:
    """A fresh directory to hold one temporary checkout.

    Resolved, because mypy reports absolute paths through the *physical* working directory:
    with a symlinked temp dir (macOS: /var -> /private/var) the paths would not relativise
    against the unresolved name, and every error of the checkout would look new.
    """
    return Path(tempfile.mkdtemp(prefix="mypy-diff-")).resolve()


def checkout_name(root: Path) -> str:
    """Directory name for a checkout of ``root``: the same as the repository's own."""
    return root.name or "tree"


def unique_worktree_name(prefix: str = "mypy-diff") -> str:
    """A worktree name that will not collide with a concurrent run of the tool."""
    return f"{prefix}-{os.getpid()}-{next(_worktree_counter)}"


_worktree_counter = itertools.count()
