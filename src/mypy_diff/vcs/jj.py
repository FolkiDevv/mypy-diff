"""Jujutsu backend (jj on top of a git backing store)."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .. import MypyDiffError
from ..models import RevisionInfo
from .base import Runner, unique_worktree_name

# One call gets us both the ids and the metadata we display.
_TEMPLATE = (
    'change_id.short() ++ "\\t" ++ commit_id ++ "\\t" '
    '++ bookmarks.join(",") ++ "\\t" ++ description.first_line() ++ "\\n"'
)

#: Tried in order; the first revset that resolves to anything wins.
_DEFAULT_BASE_REVSETS = (
    # The nearest ancestor of @ that has been pushed to a remote — the common case.
    "heads(::@ & remote_bookmarks())",
    # Nothing pushed yet: fall back to the nearest locally named ancestor.
    "heads(::@ & (bookmarks() | tags()))",
    "present(trunk())",
    "@-",
)

_WORKSPACE_PREFIX = "mypy-diff-"

#: Template for `jj workspace list`; `.root()` is the absolute workspace directory.
_WORKSPACE_TEMPLATE = 'name ++ "\\t" ++ root ++ "\\n"'

#: jj's virtual root commit. `trunk()` degrades to it when there is no trunk, and comparing
#: against an empty tree is never what the user meant.
_ROOT_COMMIT_ID = "0" * 40


class JjBackend:
    """Compare revisions in a Jujutsu workspace."""

    name = "jj"
    working_copy_revspec = "@"
    base_help = "Base revision: a bookmark name or any jj revset."
    to_help = (
        "Target revision. Defaults to @ — the working copy on disk, including uncommitted changes."
    )

    def __init__(self, executable: str, cwd: Path | None = None, verbose: bool = False) -> None:
        self._jj = Runner(executable, cwd=cwd, verbose=verbose)
        self.verbose = verbose
        self._root: Path | None = None
        self.notes: list[str] = []

    # ---------------------------------------------------------------- basics

    def root(self) -> Path:
        if self._root is None:
            top = self._jj.try_out("workspace", "root")
            if top is None:
                raise MypyDiffError(
                    "Not a Jujutsu workspace (or no workspace above the current directory).",
                    "Run mypy-diff-jj from inside a jj workspace,",
                    "or use mypy-diff-git if this is a plain git repository.",
                )
            self._root = Path(top)
            self._jj.cwd = self._root
        return self._root

    def command_log(self) -> Iterator[str]:
        yield from self._jj.log

    def _log(self, revset: str, *, check: bool = True) -> list[RevisionInfo]:
        """Resolve a revset to revisions. `--ignore-working-copy` keeps this side-effect free."""
        self.root()
        completed = self._jj.run(
            "log", "-r", revset, "--no-graph", "-T", _TEMPLATE, "--ignore-working-copy", check=check
        )
        if completed.returncode != 0:
            return []
        revisions: list[RevisionInfo] = []
        for line in completed.stdout.splitlines():
            if not line.strip():
                continue
            change_id, commit_id, bookmarks, description = [*line.split("\t", 3), "", "", ""][:4]
            revisions.append(
                RevisionInfo(
                    commit_id=commit_id,
                    short_id=commit_id[:8],
                    refs=tuple(b for b in bookmarks.split(",") if b),
                    description=description,
                    change_id=change_id,
                )
            )
        return revisions

    # ------------------------------------------------------------- revisions

    def resolve(self, revspec: str) -> RevisionInfo:
        """Resolve a revset. A bare name that is not a revset is retried as a bookmark."""
        found = self._log(revspec, check=False)
        if not found:
            escaped = revspec.replace('"', '\\"')
            found = self._log(
                f'bookmarks(exact:"{escaped}") | remote_bookmarks(exact:"{escaped}")', check=False
            )
        if not found:
            raise MypyDiffError(
                f"Could not resolve revision {revspec!r}.",
                "Check the name with `jj bookmark list --all-remotes`, or pass a revset.",
            )
        if len(found) > 1 and self.verbose:
            self.notes.append(
                f"{revspec!r} resolved to {len(found)} revisions; using {found[0].short_id}."
            )
        return found[0]

    def working_copy(self) -> RevisionInfo:
        found = self._log("@")
        if not found:
            raise MypyDiffError("Could not resolve the working-copy revision `@`.")
        current = found[0]
        return RevisionInfo(
            commit_id=current.commit_id,
            short_id=current.short_id,
            refs=current.refs,
            description=current.description,
            is_working_copy=True,
            change_id=current.change_id,
        )

    def default_base(self) -> RevisionInfo:
        for revset in _DEFAULT_BASE_REVSETS:
            # `present(trunk())` resolves to the root commit in a repo without a trunk;
            # an empty tree would report every single error as new.
            found = [r for r in self._log(revset, check=False) if r.commit_id != _ROOT_COMMIT_ID]
            if found:
                if self.verbose:
                    self.notes.append(f"Base resolved via revset `{revset}`.")
                return found[0]
        raise MypyDiffError(
            "Could not determine a base revision automatically.",
            "@ has no pushed, named or parent ancestor.",
            "Pass one explicitly, e.g. `-b main`.",
        )

    def fork_point(self, rev: RevisionInfo) -> RevisionInfo:
        # fork_point(x) == heads(::x_1 & ... & ::x_N); when the bookmark is already an
        # ancestor of @ this resolves to the bookmark itself, which is what we want.
        found = self._log(f"fork_point({rev.commit_id} | @)", check=False)
        return found[0] if found else rev

    # ------------------------------------------------------------- materials

    @contextmanager
    def worktree(self, rev: RevisionInfo) -> Iterator[Path]:
        """Add a temporary jj workspace at ``rev``, always forgotten and removed on exit."""
        self.cleanup_stale()
        parent = Path(tempfile.mkdtemp(prefix="mypy-diff-"))
        target = parent / "tree"
        workspace = unique_worktree_name(_WORKSPACE_PREFIX.rstrip("-"))
        try:
            # `-r` supplies the *parents* of the new working-copy commit, so the checked-out
            # tree is exactly the tree of `rev`.
            self._jj.run(
                "workspace", "add", "--name", workspace, "-r", rev.commit_id, str(target)
            )
            yield target
        finally:
            self._jj.run("workspace", "forget", workspace, check=False)
            shutil.rmtree(parent, ignore_errors=True)

    def file_at(self, rev: RevisionInfo, path: str) -> str | None:
        completed = self._jj.run(
            "file", "show", "-r", rev.commit_id, path, "--ignore-working-copy", check=False
        )
        if completed.returncode != 0:
            return None
        return completed.stdout

    def cleanup_stale(self) -> None:
        """Forget mypy-diff workspaces whose directory is gone (left by a crashed run).

        Keying on directory existence rather than on the name means a concurrent mypy-diff
        run, whose worktree is still on disk, is never pulled out from under it.
        """
        completed = self._jj.run(
            "workspace", "list", "--ignore-working-copy", "-T", _WORKSPACE_TEMPLATE, check=False
        )
        if completed.returncode != 0:
            return
        for line in completed.stdout.splitlines():
            name, _, location = line.partition("\t")
            if not name.startswith(_WORKSPACE_PREFIX) or not location.strip():
                continue
            if Path(location.strip()).exists():
                continue
            self._jj.run("workspace", "forget", name, check=False)
