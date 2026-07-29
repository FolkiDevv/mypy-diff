"""Git backend."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .. import MypyDiffError
from ..models import RevisionInfo
from .base import Runner, unique_worktree_name

_LOG_FORMAT = "%H%x09%h%x09%D%x09%s"
_FALLBACK_TRUNKS = ("main", "master", "develop", "trunk")


class GitBackend:
    """Compare revisions in a plain git repository."""

    name = "git"
    working_copy_revspec = "HEAD"
    base_help = "Base revision: a branch name, tag, SHA or any git revision."
    to_help = (
        "Target revision. Defaults to the working tree on disk, including uncommitted changes."
    )

    def __init__(self, executable: str, cwd: Path | None = None, verbose: bool = False) -> None:
        self._git = Runner(executable, cwd=cwd, verbose=verbose)
        self.verbose = verbose
        self._root: Path | None = None
        self.notes: list[str] = []

    # ---------------------------------------------------------------- basics

    def root(self) -> Path:
        if self._root is None:
            top = self._git.try_out("rev-parse", "--show-toplevel")
            if top is None:
                raise MypyDiffError(
                    "Not a git repository (or no git repository above the current directory).",
                    "Run mypy-diff-git from inside a git repository,",
                    "or use mypy-diff-jj if this is a Jujutsu workspace.",
                )
            self._root = Path(top)
            self._git.cwd = self._root
        return self._root

    def command_log(self) -> Iterator[str]:
        yield from self._git.log

    # ------------------------------------------------------------- revisions

    def _describe(self, revspec: str) -> RevisionInfo | None:
        self.root()
        raw = self._git.try_out("log", "-1", f"--format={_LOG_FORMAT}", revspec, "--")
        if not raw:
            return None
        commit_id, short_id, refs, description = [*raw.split("\t", 3), "", "", ""][:4]
        return RevisionInfo(
            commit_id=commit_id,
            short_id=short_id,
            refs=tuple(r.strip() for r in refs.split(",") if r.strip()),
            description=description,
        )

    def resolve(self, revspec: str) -> RevisionInfo:
        """Resolve a revision, trying the remote-tracking spelling of a bare branch name."""
        candidates = [revspec]
        if "/" not in revspec and not revspec.startswith("HEAD"):
            candidates.append(f"origin/{revspec}")
            candidates.extend(self._remote_refs_named(revspec))
        for candidate in candidates:
            info = self._describe(candidate)
            if info is not None:
                return info
        raise MypyDiffError(
            f"Could not resolve revision {revspec!r}.",
            "Check the name with `git branch -a`, or pass a SHA.",
        )

    def _remote_refs_named(self, name: str) -> list[str]:
        refs = self._git.try_out(
            "for-each-ref", "--format=%(refname:short)", f"refs/remotes/*/{name}"
        )
        return [line for line in (refs or "").splitlines() if line.strip()]

    def working_copy(self) -> RevisionInfo:
        head = self._describe("HEAD")
        if head is None:
            raise MypyDiffError(
                "This repository has no commits yet — there is nothing to compare against."
            )
        return RevisionInfo(
            commit_id=head.commit_id,
            short_id=head.short_id,
            refs=head.refs,
            description=head.description,
            is_working_copy=True,
        )

    def default_base(self) -> RevisionInfo:
        """The latest ancestor of HEAD that has been pushed to any remote."""
        self.root()
        if self.verbose:
            self.notes.append(
                "Base is derived from remote-tracking refs; run `git fetch` if they are stale."
            )

        # Commits reachable from HEAD but from no remote ref == everything not yet pushed.
        unpushed = self._git.lines(
            "rev-list", "--topo-order", "--reverse", "HEAD", "--not", "--remotes"
        )
        if self._has_remote_refs():
            if not unpushed:
                # Everything is pushed: HEAD itself is the latest pushed revision.
                return self.resolve("HEAD")
            parent = self._describe(f"{unpushed[0]}^")
            if parent is not None:
                return parent
            # The oldest unpushed commit is a root commit — fall through to the trunk guesses.

        for revspec in self._trunk_candidates():
            merge_base = self._git.try_out("merge-base", revspec, "HEAD")
            if merge_base:
                info = self._describe(merge_base)
                if info is not None:
                    return info

        parent = self._describe("HEAD~1")
        if parent is not None:
            return parent
        raise MypyDiffError(
            "Could not determine a base revision automatically.",
            "HEAD has no pushed ancestor, no trunk branch and no parent commit.",
            "Pass one explicitly, e.g. `-b main`.",
        )

    def _has_remote_refs(self) -> bool:
        return bool(self._git.try_out("for-each-ref", "--format=%(refname)", "refs/remotes/"))

    def _trunk_candidates(self) -> list[str]:
        candidates: list[str] = []
        origin_head = self._git.try_out("symbolic-ref", "--short", "refs/remotes/origin/HEAD")
        if origin_head:
            candidates.append(origin_head)
        for name in _FALLBACK_TRUNKS:
            candidates.append(f"origin/{name}")
            candidates.append(name)
        return candidates

    def fork_point(self, rev: RevisionInfo) -> RevisionInfo:
        merge_base = self._git.try_out("merge-base", rev.commit_id, "HEAD")
        if not merge_base:
            # Unrelated histories: comparing against the revision itself is the honest answer.
            return rev
        info = self._describe(merge_base)
        return info if info is not None else rev

    # ------------------------------------------------------------- materials

    @contextmanager
    def worktree(self, rev: RevisionInfo) -> Iterator[Path]:
        """Check ``rev`` out into a temporary worktree, always cleaned up on exit."""
        self.cleanup_stale()
        parent = Path(tempfile.mkdtemp(prefix="mypy-diff-"))
        target = parent / unique_worktree_name()
        try:
            self._git.run("worktree", "add", "--detach", "--quiet", str(target), rev.commit_id)
            yield target
        finally:
            self._git.run("worktree", "remove", "--force", str(target), check=False)
            shutil.rmtree(parent, ignore_errors=True)
            self._git.run("worktree", "prune", check=False)

    def file_at(self, rev: RevisionInfo, path: str) -> str | None:
        completed = self._git.run("show", f"{rev.commit_id}:{path}", check=False)
        if completed.returncode != 0:
            return None
        return completed.stdout

    def cleanup_stale(self) -> None:
        self._git.run("worktree", "prune", check=False)
