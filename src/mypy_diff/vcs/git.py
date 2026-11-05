"""Git backend."""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .. import MypyDiffError
from ..models import RevisionInfo
from .base import Runner, checkout_name, temporary_parent

_LOG_FORMAT = "%H%x09%h%x09%D%x09%s"
_FALLBACK_TRUNKS = ("main", "master", "develop", "trunk")
#: More pushed bases than this are not reduced pairwise (one git call per pair).
_MAX_BASES = 8
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
        #: How the last base was chosen, for the report header.
        self.base_reason = ""

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
        """The latest pushed state ``target`` is built on.

        ``target`` defaults to the working copy. For any other target (``--to X``) everything is
        computed from X, not from HEAD: HEAD may be on another branch altogether.

        Usually that is one commit: the parent of the oldest unpushed commit. Merges make it
        several — `git merge origin/main` into a pushed branch builds on both the pushed branch
        and main — and picking one of them would report everything the other brought in as the
        user's own work. Then the base is their merge, made on the fly: exactly what is pushed.
        """
        self.root()
        target = target or self.working_copy()
        tip = target.commit_id
        if self.verbose:
            self.notes.append(
                "Base is derived from remote-tracking refs; run `git fetch` if they are stale."
            )

        if self._has_remote_refs():
            heads = [tip]
            if target.is_working_copy:
                # In the middle of a merge, the working copy already holds the other side.
                merge_head = self._git.try_out("rev-parse", "-q", "--verify", "MERGE_HEAD")
                if merge_head:
                    heads.append(merge_head)
            pushed = self._pushed_bases(heads)
            if pushed == [tip] and not target.is_working_copy:
                # A pushed revision other than the working copy would be compared with itself.
                # Use where it forked from the trunk instead.
                pushed = []
            if len(pushed) == 1:
                base = self._describe(pushed[0])
                if base is not None:
                    self.base_reason = (
                        "everything is pushed: uncommitted changes only"
                        if base.commit_id == tip
                        else "latest pushed ancestor"
                    )
                    return base
            elif len(pushed) > 1:
                merged = self._virtual_merge(pushed)
                if merged is not None:
                    self.base_reason = "merge of the pushed commits your work builds on"
                    return merged
                self.warnings.append(
                    "Your unpushed work builds on several pushed commits ("
                    + ", ".join(commit[:7] for commit in pushed)
                    + ") that cannot be merged cleanly; comparing against the fork point with "
                    "the trunk instead."
                )
            # Nothing pushed underneath (a root commit): fall through to the trunk guesses.

        for revspec in self._trunk_candidates():
            merge_base = self._git.try_out("merge-base", revspec, tip)
            if merge_base and (target.is_working_copy or merge_base != tip):
                info = self._describe(merge_base)
                if info is not None:
                    self.base_reason = f"fork point with {revspec}"
                    return info

        parent = self._describe(f"{tip}~1")
        if parent is not None:
            self.base_reason = "parent commit"
            return parent
        raise MypyDiffError(
            "Could not determine a base revision automatically.",
            f"{target.short_id} has no pushed ancestor, no trunk branch and no parent commit.",
            "Pass one explicitly, e.g. `-b main`.",
        )

    def _pushed_bases(self, heads: list[str]) -> list[str]:
        """The newest pushed commits the heads are built on, none an ancestor of another.

        `rev-list --boundary` lists what is not pushed, then (prefixed with "-") the pushed
        commits that work sits on. A head that is pushed itself is its own base.
        """
        lines = self._git.lines("rev-list", "--boundary", *heads, "--not", "--remotes")
        unpushed = {line for line in lines if not line.startswith("-")}
        candidates = [line[1:] for line in lines if line.startswith("-")]
        candidates += [head for head in heads if head not in unpushed]
        unique = list(dict.fromkeys(candidates))
        if len(unique) > _MAX_BASES:
            return unique  # too tangled to reduce; the caller falls back to the trunk
        return [
            commit
            for commit in unique
            if not any(other != commit and self._is_ancestor(commit, other) for other in unique)
        ]

    def _virtual_merge(self, commits: list[str]) -> RevisionInfo | None:
        """A commit merging ``commits``, or None when they conflict (or git is too old).

        The commit is made with fixed author, committer and dates so that the same merge always
        gets the same id: the result cache and mypy's incremental cache key on it. It is not on
        any branch; git's garbage collection removes it eventually.
        """
        if len(commits) != 2:
            return None
        tree = self._git.try_out("merge-tree", "--write-tree", *commits)
        if not tree:
            return None  # a conflict, or git < 2.38 without --write-tree
        tree = tree.splitlines()[0].strip()
        message = "mypy-diff: merge of " + " and ".join(commit[:7] for commit in commits)
        commit = self._synthetic_commit(tree, commits, message)
        return None if commit is None else self._describe(commit)

    def _synthetic_commit(self, tree: str, parents: list[str], message: str) -> str | None:
        """A commit of ``tree`` that no branch points at, with a reproducible id.

        Fixed author, committer and dates: the same tree on the same parents always gets the
        same id, which the result cache and mypy's incremental cache key on. git's garbage
        collection removes it eventually.
        """
        completed = self._git.run(
            "commit-tree",
            "--no-gpg-sign",  # a signature would prompt, and its timestamp would change the id
            tree,
            *(arg for parent in parents for arg in ("-p", parent)),
            "-m",
            message,
            check=False,
            env=_SYNTHETIC_IDENTITY,
        )
        if completed.returncode != 0:
            return None
        return completed.stdout.strip()

    def staged(self) -> RevisionInfo:
        """What the next commit would hold: the index, as a commit on top of HEAD.

        Inside a pre-commit hook git points GIT_INDEX_FILE at the index of the commit being
        made — with `git commit -a` or `git commit <paths>`, a temporary one. Every other git
        call here leaves that variable out (see `subprocess_env`); this one must read exactly
        that index.
        """
        index = os.environ.get("GIT_INDEX_FILE")
        if index:
            # Relative to where mypy-diff was started; git here runs at the repository root.
            index = os.path.abspath(index)
        completed = self._git.run(
            "write-tree", check=False, env={"GIT_INDEX_FILE": index} if index else None
        )
        if completed.returncode != 0:
            raise MypyDiffError(
                "Could not read the staged changes.",
                *(line for line in completed.stderr.strip().splitlines() if line.strip()),
                "Resolve the conflicts in the index first (`git status` lists them).",
            )
        head = self._git.try_out("rev-parse", "-q", "--verify", "HEAD^{commit}")
        if not head:
            raise MypyDiffError(
                "--staged compares the staged changes with HEAD, and there is no commit yet.",
                "Make the first commit without the check, or pass a base with -b.",
            )
        tree = completed.stdout.strip()
        commit = self._synthetic_commit(tree, [head], "mypy-diff: staged changes")
        if commit is None:
            raise MypyDiffError("Could not make a commit of the staged changes.")
        return RevisionInfo(
            commit_id=commit, short_id=commit[:8], description="staged changes", is_staged=True
        )

    def is_shallow(self) -> bool:
        """Whether this is a shallow clone, which may lack the fork point."""
        return self._git.try_out("rev-parse", "--is-shallow-repository") == "true"

    def has_uncommitted_changes(self) -> bool:
        """Whether the working copy differs from HEAD: edits, staged or not, or new files."""
        status = self._git.try_out("status", "--porcelain", "--untracked-files=normal")
        return status is None or bool(status)

    def same_tree(self, base: RevisionInfo, target: RevisionInfo) -> bool:
        """Same files: no uncommitted changes, and commits with one tree (an empty commit, a
        revert of a revert)."""
        if target.is_working_copy and self.has_uncommitted_changes():
            return False
        if base.commit_id == target.commit_id:
            return True
        trees = [
            self._git.try_out("rev-parse", "-q", "--verify", f"{rev.commit_id}^{{tree}}")
            for rev in (base, target)
        ]
        return trees[0] is not None and trees[0] == trees[1]

    def remote_branch(self, name: str) -> str:
        """How to name the remote-tracking copy of a branch, e.g. the CI's target branch."""
        return f"origin/{name}"

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
            self.base_reason = f"tip of {revspec}"
            return rev
        best = self.fork_point(rev, target)
        self.base_reason = f"fork point with {revspec}"
        if not self._names_a_branch(revspec, rev):
            # A tag or a commit id means exactly that revision.
            return best
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
            self.base_reason = f"fork point with {shown}"
            self.warnings.append(
                f"`{revspec}` ({rev.short_id}) is behind `{shown}`: using the fork point with "
                f"`{shown}` ({best.short_id}), so commits already there do not show up as "
                f"yours. To use {rev.short_id} anyway, pass `-b {rev.short_id}`."
            )
        return best

    def _names_a_branch(self, revspec: str, rev: RevisionInfo) -> bool:
        """Whether ``revspec`` resolved to ``rev`` through a branch, local or remote-tracking."""
        if not _spellings_of(revspec):
            return False
        if self._git.try_out("show-ref", "--verify", f"refs/tags/{revspec}") is not None:
            return False
        for ref in (f"refs/heads/{revspec}", *self._remote_refs_named(revspec)):
            info = self._describe(ref)
            if info is not None and info.commit_id == rev.commit_id:
                return True
        return False

    def fork_point(self, rev: RevisionInfo, target: RevisionInfo | None = None) -> RevisionInfo:
        """The common ancestor of ``rev`` and ``target`` (default: the working copy)."""
        target = target or self.working_copy()
        merge_base = self._git.try_out("merge-base", rev.commit_id, target.commit_id)
        if merge_base and target.is_working_copy:
            # Mid-merge, the working copy already contains MERGE_HEAD: `git merge main` brought
            # main's changes in, and they must not count as the user's.
            merge_head = self._git.try_out("rev-parse", "-q", "--verify", "MERGE_HEAD")
            other = (
                self._git.try_out("merge-base", rev.commit_id, merge_head) if merge_head else None
            )
            if other and other != merge_base and self._is_ancestor(merge_base, other):
                merge_base = other
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

    def ignored_python_files(self, limit: int = 5) -> list[str]:
        """Python files in the working copy that git ignores: a clean checkout lacks them."""
        return _ignored_python_files(self._git, limit)


#: Author and committer of the commits mypy-diff makes itself: fixed, so that their ids are too.
_SYNTHETIC_IDENTITY = {
    "GIT_AUTHOR_NAME": "mypy-diff",
    "GIT_AUTHOR_EMAIL": "mypy-diff@localhost",
    "GIT_AUTHOR_DATE": "@0 +0000",
    "GIT_COMMITTER_NAME": "mypy-diff",
    "GIT_COMMITTER_EMAIL": "mypy-diff@localhost",
    "GIT_COMMITTER_DATE": "@0 +0000",
}


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


#: Ignored directories that are environments or build output, not code a checkout would need.
_NOT_SOURCE = frozenset(
    {"site-packages", "node_modules", "__pycache__", "venv", "env", "build", "dist"}
)


def _ignored_python_files(git: Runner, limit: int) -> list[str]:
    completed = git.run(
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        "-z",
        "--",
        "*.py",
        "*.pyi",
        check=False,
    )
    if completed.returncode != 0:
        return []
    found: list[str] = []
    for path in completed.stdout.split("\0"):
        parts = path.split("/")
        if not path or any(part.startswith(".") or part in _NOT_SOURCE for part in parts[:-1]):
            continue
        found.append(path)
        if len(found) >= limit:
            break
    return found
