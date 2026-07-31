"""JjBackend against a real Jujutsu workspace."""

from __future__ import annotations

from pathlib import Path

import pytest

from mypy_diff import MypyDiffError
from mypy_diff.vcs import JjBackend

from .conftest import JJ, Repo, needs_jj, run

pytestmark = [needs_jj, pytest.mark.jj]


def backend(repo: Repo) -> JjBackend:
    assert JJ is not None
    return JjBackend(JJ, cwd=repo.root)


def test_root_points_at_the_workspace(jj_repo: Repo) -> None:
    assert backend(jj_repo).root().resolve() == jj_repo.root.resolve()


def test_root_fails_outside_a_workspace(short_tmp_path: Path) -> None:
    assert JJ is not None
    outside = short_tmp_path / "not-a-workspace"
    outside.mkdir()
    with pytest.raises(MypyDiffError, match="Not a Jujutsu workspace"):
        JjBackend(JJ, cwd=outside).root()


def test_resolve_finds_a_bookmark(jj_repo: Repo) -> None:
    jj = backend(jj_repo)
    main = jj.resolve("main")
    assert main.description == "initial"
    assert "main" in main.refs
    assert main.change_id


def test_resolve_raises_for_an_unknown_revision(jj_repo: Repo) -> None:
    with pytest.raises(MypyDiffError, match="Could not resolve revision"):
        backend(jj_repo).resolve("no-such-bookmark")


def test_default_base_is_the_nearest_named_ancestor(jj_repo: Repo) -> None:
    """With nothing pushed, the local bookmark is the base — never jj's root commit."""
    jj = backend(jj_repo)
    base = jj.default_base()
    assert base.commit_id == jj.resolve("main").commit_id
    assert set(base.commit_id) != {"0"}


def test_default_base_skips_the_root_commit(jj_repo: Repo) -> None:
    """`present(trunk())` degrades to the all-zeros root commit; comparing to an empty
    tree would report every error in the project as new."""
    assert JJ is not None
    run([JJ, "bookmark", "delete", "main"], cwd=jj_repo.root)
    base = backend(jj_repo).default_base()
    assert set(base.commit_id) != {"0"}


def test_fork_point_of_an_ancestor_is_that_ancestor(jj_repo: Repo) -> None:
    jj = backend(jj_repo)
    main = jj.resolve("main")
    assert jj.fork_point(main).commit_id == main.commit_id


def test_fork_point_of_a_sibling_branch(jj_repo: Repo) -> None:
    assert JJ is not None
    jj = backend(jj_repo)
    fork = jj.resolve("main").commit_id

    # A sibling line of development off `main`.
    run([JJ, "new", "main", "-m", "sibling"], cwd=jj_repo.root)
    run([JJ, "bookmark", "create", "other", "-r", "@"], cwd=jj_repo.root)
    run([JJ, "new", "main", "-m", "mine"], cwd=jj_repo.root)

    assert jj.fork_point(jj.resolve("other")).commit_id == fork


def test_working_copy_is_flagged_as_such(jj_repo: Repo) -> None:
    working = backend(jj_repo).working_copy()
    assert working.is_working_copy
    assert working.change_id


def test_file_at_reads_the_committed_content(jj_repo: Repo) -> None:
    jj = backend(jj_repo)
    jj_repo.write("src/clean.py", "COMPLETELY DIFFERENT\n")
    assert "def add(" in (jj.file_at(jj.resolve("main"), "src/clean.py") or "")


def test_file_at_returns_none_for_a_missing_file(jj_repo: Repo) -> None:
    jj = backend(jj_repo)
    assert jj.file_at(jj.resolve("main"), "src/never_existed.py") is None


def test_worktree_checks_out_the_revision_and_cleans_up(jj_repo: Repo) -> None:
    assert JJ is not None
    jj = backend(jj_repo)
    main = jj.resolve("main")

    jj_repo.write("src/clean.py", "REWRITTEN\n")

    with jj.worktree(main) as tree:
        # `jj workspace add -r X` makes X the *parent*, so the tree is exactly X's tree.
        assert "def add(" in (tree / "src" / "clean.py").read_text(encoding="utf-8")
        checkout = tree

    assert not checkout.exists()
    listed = run([JJ, "workspace", "list", "--ignore-working-copy"], cwd=jj_repo.root).stdout
    assert "mypy-diff" not in listed


def test_worktree_is_forgotten_even_if_the_body_raises(jj_repo: Repo) -> None:
    assert JJ is not None
    jj = backend(jj_repo)
    with pytest.raises(RuntimeError), jj.worktree(jj.resolve("main")) as tree:
        checkout = tree
        raise RuntimeError("boom")

    assert not checkout.exists()
    listed = run([JJ, "workspace", "list", "--ignore-working-copy"], cwd=jj_repo.root).stdout
    assert "mypy-diff" not in listed


def test_cleanup_stale_forgets_workspaces_whose_directory_is_gone(jj_repo: Repo) -> None:
    """Simulates a run that was killed before its `finally` could forget the workspace."""
    assert JJ is not None
    import shutil

    jj = backend(jj_repo)
    orphan = jj_repo.root.parent / "orphan"
    run(
        [JJ, "workspace", "add", "--name", "mypy-diff-orphan", "-r", "main", str(orphan)],
        cwd=jj_repo.root,
    )
    shutil.rmtree(orphan)
    assert (
        "mypy-diff-orphan"
        in run([JJ, "workspace", "list", "--ignore-working-copy"], cwd=jj_repo.root).stdout
    )

    jj.cleanup_stale()

    listed = run([JJ, "workspace", "list", "--ignore-working-copy"], cwd=jj_repo.root).stdout
    assert "mypy-diff-orphan" not in listed


def test_cleanup_stale_keeps_a_live_workspace(jj_repo: Repo) -> None:
    """A concurrent mypy-diff run must never have its worktree pulled out from under it."""
    assert JJ is not None
    jj = backend(jj_repo)
    live = jj_repo.root.parent / "live"
    run(
        [JJ, "workspace", "add", "--name", "mypy-diff-live", "-r", "main", str(live)],
        cwd=jj_repo.root,
    )

    jj.cleanup_stale()

    listed = run([JJ, "workspace", "list", "--ignore-working-copy"], cwd=jj_repo.root).stdout
    assert "mypy-diff-live" in listed
