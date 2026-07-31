"""The result cache, the incremental mypy cache, retention and clearing."""

from __future__ import annotations

import os
from contextlib import ExitStack
from pathlib import Path

import pytest

from mypy_diff.cache import (
    DEFAULT_KEEP_RUNS,
    KEEP_ENV_VAR,
    ResultCache,
    keep_runs,
)
from mypy_diff.models import MypyMessage, RevisionInfo, Severity

COMMIT = "a" * 40
MESSAGES = (
    MypyMessage(
        path="src/app.py",
        line=1,
        column=0,
        severity=Severity.ERROR,
        message="Bad thing",
        code="return-value",
    ),
)


def revision(commit_id: str = COMMIT, working_copy: bool = False) -> RevisionInfo:
    return RevisionInfo(commit_id=commit_id, short_id=commit_id[:8], is_working_copy=working_copy)


def cache(tmp_path: Path, enabled: bool = True) -> ResultCache:
    return ResultCache(tmp_path / "cache", enabled=enabled)


# ------------------------------------------------------------------ result cache


def test_store_then_load_roundtrips(tmp_path: Path) -> None:
    subject = cache(tmp_path)
    key = subject.key(COMMIT, "mypy 2.3.0", ("src",))
    subject.store(key, MESSAGES)
    assert subject.load(key) == MESSAGES


def test_load_misses_for_a_different_key(tmp_path: Path) -> None:
    subject = cache(tmp_path)
    subject.store(subject.key(COMMIT, "mypy 2.3.0", ("src",)), MESSAGES)
    assert subject.load(subject.key(COMMIT, "mypy 2.4.0", ("src",))) is None
    assert subject.load(subject.key(COMMIT, "mypy 2.3.0", ("tests",))) is None
    assert subject.load(subject.key("b" * 40, "mypy 2.3.0", ("src",))) is None


def test_key_ignores_the_working_copy_config(tmp_path: Path) -> None:
    """Cached runs happen in a worktree, so commit_id already pins the mypy config.

    Hashing the working copy's pyproject.toml would only cause false misses.
    """
    subject = cache(tmp_path)
    before = subject.key(COMMIT, "mypy 2.3.0", ("src",))
    (tmp_path / "pyproject.toml").write_text("[tool.mypy]\nstrict = true\n", encoding="utf-8")
    assert subject.key(COMMIT, "mypy 2.3.0", ("src",)) == before


def test_disabled_cache_neither_reads_nor_writes(tmp_path: Path) -> None:
    subject = cache(tmp_path, enabled=False)
    key = subject.key(COMMIT, "mypy 2.3.0", ("src",))
    subject.store(key, MESSAGES)
    assert subject.load(key) is None
    assert not subject.directory.exists()


def test_load_refreshes_the_entry_mtime(tmp_path: Path) -> None:
    """mtime is the last-used stamp that retention sorts on."""
    subject = cache(tmp_path)
    key = subject.key(COMMIT, "mypy 2.3.0", ("src",))
    subject.store(key, MESSAGES)
    path = subject.directory / f"{key}.json"
    os.utime(path, (0, 0))

    subject.load(key)

    assert path.stat().st_mtime > 0


def test_corrupt_entry_is_a_miss_not_a_crash(tmp_path: Path) -> None:
    subject = cache(tmp_path)
    key = subject.key(COMMIT, "mypy 2.3.0", ("src",))
    subject.directory.mkdir(parents=True)
    (subject.directory / f"{key}.json").write_text("{not json", encoding="utf-8")
    assert subject.load(key) is None


# ------------------------------------------------------------------ mypy cache dir


def test_mypy_cache_dir_is_ephemeral_when_disabled(tmp_path: Path) -> None:
    subject = cache(tmp_path, enabled=False)
    with ExitStack() as stack:
        directory = subject.mypy_cache_dir(revision(), stack)
        assert directory.exists()
        # Nothing under the real cache directory.
        assert subject.directory not in directory.parents

    assert not directory.exists()


def test_mypy_cache_dir_separates_working_copy_from_its_parent(tmp_path: Path) -> None:
    """A working copy carries its *parent's* commit id.

    When everything is pushed, base == HEAD, and without the suffix both runs would share one
    directory and thrash each other's incremental cache.
    """
    subject = cache(tmp_path)
    with ExitStack() as stack:
        base = subject.mypy_cache_dir(revision(), stack)
        working = subject.mypy_cache_dir(revision(working_copy=True), stack)

    assert base != working
    assert working.name.endswith("-wc")


def test_mypy_cache_dir_is_stable_across_calls(tmp_path: Path) -> None:
    subject = cache(tmp_path)
    with ExitStack() as stack:
        assert subject.mypy_cache_dir(revision(), stack) == subject.mypy_cache_dir(
            revision(), stack
        )


# ------------------------------------------------------------------ retention


def test_keep_runs_defaults_to_three(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(KEEP_ENV_VAR, raising=False)
    assert keep_runs() == DEFAULT_KEEP_RUNS == 3


def test_keep_runs_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(KEEP_ENV_VAR, "7")
    assert keep_runs() == 7


def test_keep_runs_falls_back_on_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(KEEP_ENV_VAR, "lots")
    assert keep_runs() == DEFAULT_KEEP_RUNS


def _seed_mypy_dirs(subject: ResultCache, count: int) -> list[Path]:
    """Create `count` revision directories, oldest first by mtime."""
    made: list[Path] = []
    for index in range(count):
        directory = subject.directory / "mypy-cache" / f"rev{index:02d}"
        directory.mkdir(parents=True)
        (directory / "data.bin").write_bytes(b"x")
        os.utime(directory, (index * 1000, index * 1000))
        made.append(directory)
    return made


def test_prune_keeps_two_entries_per_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One run touches at most a base and a target, so `keep` runs == 2*keep entries."""
    monkeypatch.setenv(KEEP_ENV_VAR, "3")
    subject = cache(tmp_path)
    made = _seed_mypy_dirs(subject, 10)

    subject.prune()

    survivors = sorted(p.name for p in (subject.directory / "mypy-cache").iterdir())
    assert survivors == ["rev04", "rev05", "rev06", "rev07", "rev08", "rev09"]
    assert not made[0].exists()


def test_prune_respects_the_environment_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(KEEP_ENV_VAR, "1")
    subject = cache(tmp_path)
    _seed_mypy_dirs(subject, 6)

    subject.prune()

    assert sorted(p.name for p in (subject.directory / "mypy-cache").iterdir()) == [
        "rev04",
        "rev05",
    ]


def test_prune_disabled_by_non_positive_keep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(KEEP_ENV_VAR, "0")
    subject = cache(tmp_path)
    _seed_mypy_dirs(subject, 8)

    subject.prune()

    assert len(list((subject.directory / "mypy-cache").iterdir())) == 8


def test_prune_trims_result_entries_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(KEEP_ENV_VAR, "1")
    subject = cache(tmp_path)
    subject.directory.mkdir(parents=True)
    for index in range(5):
        path = subject.directory / f"{index:064x}.json"
        path.write_text('{"version": 2, "messages": []}', encoding="utf-8")
        os.utime(path, (index * 1000, index * 1000))

    subject.prune()

    assert len(list(subject.directory.glob("*.json"))) == 2


def test_prune_is_a_noop_when_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(KEEP_ENV_VAR, "1")
    subject = cache(tmp_path, enabled=False)
    _seed_mypy_dirs(ResultCache(subject.directory), 6)

    subject.prune()

    assert len(list((subject.directory / "mypy-cache").iterdir())) == 6


def test_prune_survives_a_missing_directory(tmp_path: Path) -> None:
    cache(tmp_path).prune()  # must not raise


# ------------------------------------------------------------------ clear


def test_clear_removes_known_content_and_reports_bytes(tmp_path: Path) -> None:
    subject = cache(tmp_path)
    key = subject.key(COMMIT, "mypy 2.3.0", ("src",))
    subject.store(key, MESSAGES)
    _seed_mypy_dirs(subject, 3)

    freed = subject.clear()

    assert freed > 0
    assert not (subject.directory / "mypy-cache").exists()
    assert list(subject.directory.glob("*.json")) == []


def test_clear_leaves_unrelated_files_alone(tmp_path: Path) -> None:
    """--cache-dir could point anywhere; a blanket rmtree would be dangerous."""
    subject = cache(tmp_path)
    subject.directory.mkdir(parents=True)
    bystander = subject.directory / "important.txt"
    bystander.write_text("do not delete me", encoding="utf-8")
    subject.store(subject.key(COMMIT, "mypy 2.3.0", ("src",)), MESSAGES)

    subject.clear()

    assert bystander.read_text(encoding="utf-8") == "do not delete me"


def test_clear_on_a_missing_directory_is_zero(tmp_path: Path) -> None:
    assert cache(tmp_path).clear() == 0
