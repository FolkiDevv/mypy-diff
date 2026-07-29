"""GitBackend against a real git repository."""

from __future__ import annotations

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
