"""Jujutsu backend (jj on top of a git backing store)."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .. import MypyDiffError
from ..models import RevisionInfo
from .base import Runner, checkout_name, temporary_parent, unique_worktree_name

# One call gets us both the ids and the metadata we display.
_TEMPLATE = (
    'change_id.short() ++ "\\t" ++ commit_id ++ "\\t" '
    '++ bookmarks.join(",") ++ "\\t" ++ description.first_line() ++ "\\n"'
)

#: Tried in order; the first revset that resolves to anything wins.
#:
#: ``{ancestors}`` is ``::@`` for the working copy (its pushed state is the base when all of it is
#: pushed), ``{proper}`` is ``::@-``; for any other target X both are X's *proper* ancestors — a
#: pushed `--to X` would else be its own base. ``{parent}`` is ``@-`` / ``X-``.
#:
#: Pushed means *reachable from* a remote bookmark, not pointed at by one: after `jj git fetch`
#: moves main@origin past your fork point, no ancestor of yours is a bookmark target any more,
#: but the fork point is still pushed.
_DEFAULT_BASE_REVSETS = (
    # The nearest pushed ancestor — the common case.
    "heads({ancestors} & ::remote_bookmarks())",
    # Nothing pushed yet: the nearest locally named ancestor (not the target itself).
    "heads({proper} & (bookmarks() | tags()))",
    # Where the target forked from the trunk. `trunk()` alone may not be an ancestor at all.
    "heads({proper} & ::trunk())",
    "{parent}",
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
        self._git: Runner | None = None
        self._git_store: Path | None = None
        #: Shown with --verbose only.
        self.notes: list[str] = []
        #: Shown always: something about the comparison the user should know.
        self.warnings: list[str] = []

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
            # Snapshot once, up front. Everything after this reads the repository with
            # --ignore-working-copy, which would otherwise see the state of the last jj command:
            # an @ that is not what is on disk, and — in a colocated repository — git HEAD and
            # refs that `git pull` or an IDE moved but jj has not imported yet. A stale
            # workspace fails here, with jj's own explanation.
            self._jj.run("log", "-r", "@", "--no-graph", "-T", "commit_id")
        return self._root

    def command_log(self) -> Iterator[str]:
        yield from self._jj.log
        if self._git is not None:
            yield from self._git.log

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
        if len(found) > 1:
            self.warnings.append(
                f"{revspec!r} resolves to {len(found)} revisions ("
                + ", ".join(r.short_id for r in found)
                + f"); using {found[0].short_id}. Pass a single revision to choose."
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

    def default_base(self, target: RevisionInfo | None = None) -> RevisionInfo:
        """The latest pushed (or else named) ancestor of ``target``, default the working copy."""
        if target is None or target.is_working_copy:
            ancestors, proper, parent = "::@", "::@-", "@-"
        else:
            proper = f"::({target.commit_id}-)"
            ancestors, parent = proper, f"{target.commit_id}-"
        for template in _DEFAULT_BASE_REVSETS:
            revset = template.format(ancestors=ancestors, proper=proper, parent=parent)
            # `trunk()` resolves to the root commit in a repo without a trunk; an empty tree
            # would report every single error as new.
            found = [r for r in self._log(revset, check=False) if r.commit_id != _ROOT_COMMIT_ID]
            if found:
                if self.verbose:
                    self.notes.append(f"Base resolved via revset `{revset}`.")
                if len(found) > 1:
                    # A merge: several lines of pushed (or named) work meet below @.
                    self.warnings.append(
                        f"The base is ambiguous: {len(found)} revisions qualify ("
                        + ", ".join(r.short_id for r in found)
                        + f"); using {found[0].short_id}. Pass -b to choose."
                    )
                return found[0]
        raise MypyDiffError(
            "Could not determine a base revision automatically.",
            "The target has no pushed, named or parent ancestor.",
            "Pass one explicitly, e.g. `-b main`.",
        )

    def resolve_base(self, revspec: str, target: RevisionInfo, tip: bool) -> RevisionInfo:
        """The base for ``-b REVSPEC``: its fork point with ``target``, or its tip with --tip."""
        rev = self.resolve(revspec)
        return rev if tip else self.fork_point(rev, target)

    def fork_point(self, rev: RevisionInfo, target: RevisionInfo | None = None) -> RevisionInfo:
        """The common ancestor of ``rev`` and ``target`` (default: the working copy, ``@``).

        fork_point(x) == heads(::x_1 & ... & ::x_N); when ``rev`` is already an ancestor of the
        target this resolves to ``rev`` itself, which is what we want.
        """
        other = "@" if target is None or target.is_working_copy else target.commit_id
        found = self._log(f"fork_point({rev.commit_id} | {other})", check=False)
        return found[0] if found else rev

    # ------------------------------------------------------------- materials

    @contextmanager
    def worktree(self, rev: RevisionInfo) -> Iterator[Path]:
        """A temporary checkout of ``rev``, always removed on exit.

        Made with git from jj's git store when git is available: a jj workspace would snapshot
        the user's working copy, write operations to the op log (so `jj undo` right after a run
        undid mypy-diff's work, not the user's), and could revive a commit the snapshot had just
        rewritten. jj commit ids are git commit ids, and jj keeps its commits reachable in the
        store, so git can check out any of them.
        """
        store = self._store()
        if store is None or self._git is None:
            with self._workspace(rev) as checkout:
                yield checkout
            return
        git = self._git
        parent = temporary_parent()
        # Named like the repository: see GitBackend.worktree.
        target = parent / checkout_name(self.root())
        git_dir = f"--git-dir={store}"
        try:
            git.run(git_dir, "worktree", "add", "--detach", "--quiet", str(target), rev.commit_id)
            yield target
        finally:
            git.run(git_dir, "worktree", "remove", "--force", str(target), check=False)
            shutil.rmtree(parent, ignore_errors=True)
            git.run(git_dir, "worktree", "prune", check=False)

    def _store(self) -> Path | None:
        """jj's git store, when git is installed to read it; None otherwise."""
        if self._git_store is None and self._git is None:
            executable = shutil.which("git")
            if executable is None:
                return None
            location = self._jj.try_out("git", "root", "--ignore-working-copy")
            if not location:
                return None
            self._git = Runner(executable, cwd=self.root(), verbose=self.verbose)
            self._git_store = Path(location)
        return self._git_store

    @contextmanager
    def _workspace(self, rev: RevisionInfo) -> Iterator[Path]:
        """Add a temporary jj workspace at ``rev``, always forgotten and removed on exit."""
        self.cleanup_stale()
        parent = temporary_parent()
        # Named like the repository: see GitBackend.worktree.
        target = parent / checkout_name(self.root())
        workspace = unique_worktree_name(_WORKSPACE_PREFIX.rstrip("-"))
        try:
            # `-r` supplies the *parents* of the new working-copy commit, so the checked-out
            # tree is exactly the tree of `rev`.
            self._jj.run("workspace", "add", "--name", workspace, "-r", rev.commit_id, str(target))
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

    def ignored_python_files(self, limit: int = 5) -> list[str]:
        """Python files git ignores in a colocated workspace; jj itself cannot list them."""
        git = shutil.which("git")
        root = self.root()
        if git is None or not (root / ".git").exists():
            return []
        from .git import _ignored_python_files

        return _ignored_python_files(Runner(git, cwd=root), limit)

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
