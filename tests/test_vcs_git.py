"""GitBackend against a real git repository."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from mypy_diff import MypyDiffError
from mypy_diff.vcs import GitBackend

from .conftest import GIT, Repo, needs_git, run

pytestmark = [needs_git, pytest.mark.git]


def backend(repo: Repo) -> GitBackend:
    assert GIT is not None
    return GitBackend(GIT, cwd=repo.root)


def commit(repo: Repo, message: str) -> None:
    assert GIT is not None
    run([GIT, "add", "-A"], cwd=repo.root)
    run([GIT, "commit", "-q", "-m", message], cwd=repo.root)


def test_root_points_at_the_repository(git_repo: Repo) -> None:
    assert backend(git_repo).root().resolve() == git_repo.root.resolve()


def test_root_fails_outside_a_repository(short_tmp_path: Path) -> None:
    assert GIT is not None
    outside = short_tmp_path / "not-a-repo"
    outside.mkdir()
    with pytest.raises(MypyDiffError, match="Not a git repository"):
        GitBackend(GIT, cwd=outside).root()


def test_default_base_is_the_last_pushed_commit(git_repo: Repo) -> None:
    """Two local commits on top of what was pushed: the base must be the pushed one."""
    git = backend(git_repo)
    pushed = git.resolve("HEAD").commit_id

    git_repo.write("src/one.py", "x: int = 1\n")
    commit(git_repo, "local one")
    git_repo.write("src/two.py", "y: int = 2\n")
    commit(git_repo, "local two")

    assert git.default_base().commit_id == pushed


def test_default_base_is_head_when_everything_is_pushed(git_repo: Repo) -> None:
    git = backend(git_repo)
    assert git.default_base().commit_id == git.resolve("HEAD").commit_id


def test_resolve_finds_a_branch_by_bare_name(git_repo: Repo) -> None:
    git = backend(git_repo)
    assert git.resolve("main").commit_id == git.resolve("HEAD").commit_id


def test_resolve_falls_back_to_the_remote_tracking_ref(git_repo: Repo) -> None:
    """`origin/main` exists even after the local branch moves on."""
    assert GIT is not None
    git = backend(git_repo)
    pushed = git.resolve("HEAD").commit_id
    run([GIT, "checkout", "-q", "-b", "feature"], cwd=git_repo.root)
    run([GIT, "branch", "-D", "main", "-q"], cwd=git_repo.root)
    # No local `main` any more, but `origin/main` still points at the pushed commit.
    assert git.resolve("main").commit_id == pushed


def test_resolve_raises_for_an_unknown_revision(git_repo: Repo) -> None:
    with pytest.raises(MypyDiffError, match="Could not resolve revision"):
        backend(git_repo).resolve("no-such-branch")


def test_fork_point_is_the_merge_base(git_repo: Repo) -> None:
    assert GIT is not None
    git = backend(git_repo)
    fork = git.resolve("HEAD").commit_id

    run([GIT, "checkout", "-q", "-b", "feature"], cwd=git_repo.root)
    git_repo.write("src/feature.py", "z: int = 3\n")
    commit(git_repo, "feature work")

    # main also moved on, so tip != fork point.
    run([GIT, "checkout", "-q", "main"], cwd=git_repo.root)
    git_repo.write("src/mainline.py", "w: int = 4\n")
    commit(git_repo, "mainline work")
    main_tip = git.resolve("main").commit_id

    run([GIT, "checkout", "-q", "feature"], cwd=git_repo.root)
    assert git.fork_point(git.resolve("main")).commit_id == fork
    assert main_tip != fork


def test_working_copy_is_flagged_as_such(git_repo: Repo) -> None:
    working = backend(git_repo).working_copy()
    assert working.is_working_copy
    assert working.commit_id == backend(git_repo).resolve("HEAD").commit_id


def test_file_at_reads_the_committed_content(git_repo: Repo) -> None:
    git = backend(git_repo)
    head = git.resolve("HEAD")
    git_repo.write("src/clean.py", "COMPLETELY DIFFERENT\n")
    assert "def add(" in (git.file_at(head, "src/clean.py") or "")


def test_file_at_returns_none_for_a_missing_file(git_repo: Repo) -> None:
    git = backend(git_repo)
    assert git.file_at(git.resolve("HEAD"), "src/never_existed.py") is None


def test_worktree_checks_out_the_revision_and_cleans_up(git_repo: Repo) -> None:
    assert GIT is not None
    git = backend(git_repo)
    original = git.resolve("HEAD")

    git_repo.write("src/clean.py", "REWRITTEN\n")
    commit(git_repo, "rewrite")

    with git.worktree(original) as tree:
        assert "def add(" in (tree / "src" / "clean.py").read_text(encoding="utf-8")
        checkout = tree

    # Nothing left behind: neither on disk nor registered with git.
    assert not checkout.exists()
    listed = run([GIT, "worktree", "list"], cwd=git_repo.root).stdout
    assert "mypy-diff" not in listed


def test_worktree_is_removed_even_if_the_body_raises(git_repo: Repo) -> None:
    assert GIT is not None
    git = backend(git_repo)
    with pytest.raises(RuntimeError), git.worktree(git.resolve("HEAD")) as tree:
        checkout = tree
        raise RuntimeError("boom")

    assert not checkout.exists()
    assert "mypy-diff" not in run([GIT, "worktree", "list"], cwd=git_repo.root).stdout


# ------------------------------------------------------------------ hooks


@pytest.mark.parametrize("spelling", ["relative", "absolute"])
def test_worktree_leaves_a_hooks_index_alone(
    git_repo: Repo, monkeypatch: pytest.MonkeyPatch, spelling: str
) -> None:
    """git exports GIT_INDEX_FILE to a pre-commit hook.

    With plain `git commit` it is the relative `.git/index`, and `git worktree add` used to fail
    on it. With `git commit -a` it is a temporary index that becomes the commit: the checkout
    wrote the base revision's tree into it, so the commit silently recorded the old tree.
    """
    git = backend(git_repo)
    original = git.resolve("HEAD")
    git_repo.write("src/clean.py", "x: int = 1\n")
    commit(git_repo, "change")

    if spelling == "relative":
        monkeypatch.setenv("GIT_INDEX_FILE", ".git/index")
        hook_index = git_repo.root / ".git" / "index"
    else:
        hook_index = git_repo.root.parent / "hook-index"
        shutil.copyfile(git_repo.root / ".git" / "index", hook_index)
        monkeypatch.setenv("GIT_INDEX_FILE", str(hook_index))
    before = hook_index.read_bytes()

    with git.worktree(original) as tree:
        assert "def add(" in (tree / "src" / "clean.py").read_text(encoding="utf-8")

    assert hook_index.read_bytes() == before


# ------------------------------------------------------------------ the base follows the target


def diverge(repo: Repo) -> tuple[str, str]:
    """`feature` forks from the pushed commit, then `main` moves on. Returns (fork, main tip).

    Leaves `main` checked out: the shape of reviewing someone's branch with `--to feature`.
    """
    assert GIT is not None
    fork = backend(repo).resolve("HEAD").commit_id
    run([GIT, "checkout", "-q", "-b", "feature"], cwd=repo.root)
    repo.write("src/feature.py", "z: int = 3\n")
    commit(repo, "feature work")
    run([GIT, "checkout", "-q", "main"], cwd=repo.root)
    repo.write("src/mainline.py", "w: int = 4\n")
    commit(repo, "mainline work")
    run([GIT, "push", "-q", "origin", "main"], cwd=repo.root)
    return fork, backend(repo).resolve("main").commit_id


def test_fork_point_is_computed_against_the_target(git_repo: Repo) -> None:
    """On main, `-b main --to feature` must use feature's fork point, not main's tip."""
    git = backend(git_repo)
    fork, main_tip = diverge(git_repo)
    assert git.fork_point(git.resolve("main"), git.resolve("feature")).commit_id == fork
    # Against the working copy (main itself) it is main's tip, as before.
    assert git.fork_point(git.resolve("main")).commit_id == main_tip


def test_default_base_is_computed_from_an_explicit_target(git_repo: Repo) -> None:
    git = backend(git_repo)
    fork, _ = diverge(git_repo)
    assert git.default_base(git.resolve("feature")).commit_id == fork


def test_a_pushed_explicit_target_is_not_its_own_base(git_repo: Repo) -> None:
    """Every commit of `feature` is pushed, but comparing it with itself would show nothing."""
    assert GIT is not None
    git = backend(git_repo)
    fork, _ = diverge(git_repo)
    run([GIT, "push", "-q", "origin", "feature"], cwd=git_repo.root)
    assert git.default_base(git.resolve("feature")).commit_id == fork


def colleague_pushes_to_main(repo: Repo, short_tmp_path: Path) -> str:
    """Someone else pushes a commit to main. Returns its id."""
    assert GIT is not None
    other = short_tmp_path / "colleague"
    remote = str(repo.root.parent / "remote.git")
    run([GIT, "clone", "-q", "--branch", "main", remote, str(other)], cwd=short_tmp_path)
    run([GIT, "config", "user.email", "c@example.com"], cwd=other)
    run([GIT, "config", "user.name", "Colleague"], cwd=other)
    (other / "src" / "colleague.py").write_text("c: int = 5\n", encoding="utf-8")
    run([GIT, "add", "-A"], cwd=other)
    run([GIT, "commit", "-q", "-m", "colleague work"], cwd=other)
    run([GIT, "push", "-q", "origin", "main"], cwd=other)
    return run([GIT, "rev-parse", "HEAD"], cwd=other).stdout.strip()


def test_base_prefers_a_newer_upstream_over_a_stale_local_branch(
    git_repo: Repo, short_tmp_path: Path
) -> None:
    """Branching from origin/main and never fast-forwarding local main is the norm.

    Local main's fork point is then older than the real one, and the colleague's commit in
    between used to show up as the user's own NEW and FIXED errors.
    """
    assert GIT is not None
    stale = backend(git_repo).resolve("main").commit_id
    pushed = colleague_pushes_to_main(git_repo, short_tmp_path)
    run([GIT, "fetch", "-q"], cwd=git_repo.root)
    run([GIT, "checkout", "-q", "-b", "feature", "origin/main"], cwd=git_repo.root)
    git_repo.write("src/feature.py", "z: int = 3\n")
    commit(git_repo, "feature work")

    git = backend(git_repo)
    target = git.working_copy()
    assert git.resolve_base("main", target, tip=False).commit_id == pushed
    assert any("is behind" in warning for warning in git.warnings)
    # --tip still means the literal ref.
    assert git.resolve_base("main", target, tip=True).commit_id == stale


def test_a_remote_that_already_contains_the_branch_is_not_preferred(git_repo: Repo) -> None:
    """The branch was pushed to the remote's main: its fork point there is the branch itself."""
    assert GIT is not None
    fork = backend(git_repo).resolve("HEAD").commit_id
    run([GIT, "checkout", "-q", "-b", "feature"], cwd=git_repo.root)
    git_repo.write("src/feature.py", "z: int = 3\n")
    commit(git_repo, "feature work")
    run([GIT, "push", "-q", "origin", "feature:main"], cwd=git_repo.root)

    git = backend(git_repo)
    assert git.resolve_base("main", git.working_copy(), tip=False).commit_id == fork
    assert git.warnings == []


# ------------------------------------------------------------------ shallow clones


def shallow_clone(repo: Repo, short_tmp_path: Path, branch: str) -> GitBackend:
    """What CI checks out: one commit of `branch`, plus one commit of main fetched on top."""
    assert GIT is not None
    remote = (repo.root.parent / "remote.git").as_uri()
    clone = short_tmp_path / "ci"
    run(
        [GIT, "clone", "-q", "--depth", "1", "--branch", branch, remote, str(clone)],
        cwd=short_tmp_path,
    )
    run(
        [GIT, "fetch", "-q", "--depth", "1", "origin", "+refs/heads/main:refs/remotes/origin/main"],
        cwd=clone,
    )
    return GitBackend(GIT, cwd=clone)


def test_a_shallow_clone_without_the_fork_point_is_an_error(
    git_repo: Repo, short_tmp_path: Path
) -> None:
    """Silently comparing against main's tip used to invert everything main did since."""
    assert GIT is not None
    diverge(git_repo)
    run([GIT, "push", "-q", "origin", "feature"], cwd=git_repo.root)

    git = shallow_clone(git_repo, short_tmp_path, "feature")
    main = git.resolve("origin/main")
    assert "grafted" not in main.refs
    with pytest.raises(MypyDiffError, match="shallow clone"):
        git.resolve_base("origin/main", git.working_copy(), tip=False)


def test_a_shallow_merge_commit_forks_from_its_recorded_parent(
    git_repo: Repo, short_tmp_path: Path
) -> None:
    """A pull-request checkout is a merge commit whose first parent is the base branch."""
    assert GIT is not None
    _, main_tip = diverge(git_repo)
    run([GIT, "checkout", "-q", "-b", "pr-merge", "main"], cwd=git_repo.root)
    run([GIT, "merge", "-q", "--no-ff", "-m", "merge feature", "feature"], cwd=git_repo.root)
    run([GIT, "push", "-q", "origin", "pr-merge"], cwd=git_repo.root)

    git = shallow_clone(git_repo, short_tmp_path, "pr-merge")
    base = git.resolve_base("origin/main", git.working_copy(), tip=False)
    assert base.commit_id == main_tip
