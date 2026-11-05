"""Git backend."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .. import MypyDiffError
from ..models import RevisionInfo
from .base import Runner, checkout_name, temporary_parent

_LOG_FORMAT = "%H%x09%h%x09%D%x09%s"
_FALLBACK_TRUNKS = ("main", "master", "develop", "trunk")
#: Decorations `%D` prints that are not refs. A shallow clone marks its boundary "grafted".
_NOT_REFS = frozenset({"grafted"})


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
        #: Shown with --verbose only.
        self.notes: list[str] = []
        #: Shown always: something about the comparison the user should know.
        self.warnings: list[str] = []

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
            refs=tuple(r.strip() for r in refs.split(",") if r.strip() not in _NOT_REFS | {""}),
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

    def default_base(self, target: RevisionInfo | None = None) -> RevisionInfo:
        """The latest ancestor of ``target`` that has been pushed to any remote.

        ``target`` defaults to the working copy. For any other target (``--to X``) everything is
        computed from X, not from HEAD: HEAD may be on another branch altogether.
        """
        self.root()
        target = target or self.working_copy()
        tip = target.commit_id
        if self.verbose:
            self.notes.append(
                "Base is derived from remote-tracking refs; run `git fetch` if they are stale."
            )

        # Commits reachable from the target but from no remote ref == everything not yet pushed.
        unpushed = self._git.lines(
            "rev-list", "--topo-order", "--reverse", tip, "--not", "--remotes"
        )
        if self._has_remote_refs():
            if unpushed:
                parent = self._describe(f"{unpushed[0]}^")
                if parent is not None:
                    return parent
                # The oldest unpushed commit is a root commit — fall through to the trunk guesses.
            elif target.is_working_copy:
                # Everything is pushed: HEAD itself is the latest pushed revision, and the
                # uncommitted changes on disk are the work to check.
                head = self._describe(tip)
                if head is not None:
                    return head
            # A pushed revision other than the working copy would be compared with itself.
            # Use where it forked from the trunk instead.

        for revspec in self._trunk_candidates():
            merge_base = self._git.try_out("merge-base", revspec, tip)
            if merge_base and (target.is_working_copy or merge_base != tip):
                info = self._describe(merge_base)
                if info is not None:
                    return info

        parent = self._describe(f"{tip}~1")
        if parent is not None:
            return parent
        raise MypyDiffError(
            "Could not determine a base revision automatically.",
            f"{target.short_id} has no pushed ancestor, no trunk branch and no parent commit.",
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

    def resolve_base(self, revspec: str, target: RevisionInfo, tip: bool) -> RevisionInfo:
        """The base for ``-b REVSPEC``: its fork point with ``target``, or its tip with --tip.

        A bare branch name can mean several refs: the local branch, its upstream, ``origin/NAME``,
        ``upstream/NAME``. A local branch that was never fast-forwarded is the usual case — the
        feature was branched from ``origin/main`` and local ``main`` is behind. Its fork point is
        then older than the real one, and everything that landed in between would be reported
        as the user's own NEW and FIXED errors. So the newest fork point among them wins.
        """
        rev = self.resolve(revspec)
        if tip:
            return rev
        best = self.fork_point(rev, target)
        better_spelling: str | None = None
        for spelling in _spellings_of(revspec):
            other = self._describe(spelling)
            if other is None or other.commit_id == rev.commit_id:
                continue
            merge_base = self._git.try_out("merge-base", other.commit_id, target.commit_id)
            # A fork point equal to the target means the other ref already contains all of it
            # (the branch was pushed there): comparing against it would hide the whole branch.
            if not merge_base or merge_base in (best.commit_id, target.commit_id):
                continue
            if self._is_ancestor(best.commit_id, merge_base):
                newer = self._describe(merge_base)
                if newer is not None:
                    best, better_spelling = newer, spelling
        if better_spelling is not None:
            shown = self._git.try_out("rev-parse", "--abbrev-ref", better_spelling)
            shown = shown or better_spelling.removeprefix("refs/heads/")
            self.warnings.append(
                f"`{revspec}` ({rev.short_id}) is behind `{shown}`: using the fork point with "
                f"`{shown}` ({best.short_id}), so commits already there do not show up as "
                f"yours. To use {rev.short_id} anyway, pass `-b {rev.short_id}`."
            )
        return best

    def fork_point(self, rev: RevisionInfo, target: RevisionInfo | None = None) -> RevisionInfo:
        """The common ancestor of ``rev`` and ``target`` (default: the working copy)."""
        target = target or self.working_copy()
        merge_base = self._git.try_out("merge-base", rev.commit_id, target.commit_id)
        if merge_base:
            info = self._describe(merge_base)
            return info if info is not None else rev

        # No common ancestor in the history we have. A shallow clone cuts the history off, but a
        # commit still records its parents: a GitHub pull-request checkout is a merge commit
        # whose first parent is the base branch, and that is exactly the right answer.
        if rev.commit_id in self._recorded_parents(target.commit_id):
            return rev
        if self._git.try_out("rev-parse", "--is-shallow-repository") == "true":
            raise MypyDiffError(
                f"Cannot find where {target.short_id} forked from {rev.short_id}: "
                "this is a shallow clone, and the history between them is missing.",
                "In GitHub Actions, check out with `fetch-depth: 0`; elsewhere, run "
                "`git fetch --unshallow`.",
                "To compare against the tip on purpose, pass --tip.",
            )
        self.warnings.append(
            f"{rev.short_id} and {target.short_id} share no history; comparing against "
            f"{rev.short_id} itself."
        )
        return rev

    def _recorded_parents(self, commit_id: str) -> list[str]:
        raw = self._git.try_out("cat-file", "-p", commit_id) or ""
        parents: list[str] = []
        for line in raw.splitlines():
            if not line.strip():
                break  # end of the header; the message follows
            key, _, value = line.partition(" ")
            if key == "parent":
                parents.append(value.strip())
        return parents

    def _is_ancestor(self, ancestor: str, descendant: str) -> bool:
        completed = self._git.run("merge-base", "--is-ancestor", ancestor, descendant, check=False)
        return completed.returncode == 0

    # ------------------------------------------------------------- materials

    @contextmanager
    def worktree(self, rev: RevisionInfo) -> Iterator[Path]:
        """Check ``rev`` out into a temporary worktree, always cleaned up on exit."""
        self.cleanup_stale()
        parent = temporary_parent()
        # Named like the repository, not uniquely: mypy derives module names from directory
        # names when the repository root is itself a package, so both revisions must agree on
        # it. The parent directory is unique already.
        target = parent / checkout_name(self.root())
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


def _spellings_of(revspec: str) -> list[str]:
    """Refs a bare branch name may stand for. Anything else (a SHA, a revision expression, a
    name with a slash) means exactly what it says, so it has none."""
    if not revspec or any(char in revspec for char in "/~^:@{}") or revspec.startswith("HEAD"):
        return []
    return [
        f"refs/heads/{revspec}",
        f"{revspec}@{{upstream}}",
        f"origin/{revspec}",
        f"upstream/{revspec}",
    ]
