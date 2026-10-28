"""On-disk caches for mypy results.

Two separate things live under the same directory:

* **Result cache** — the parsed diagnostics of one run, as ``<dir>/<sha256>.json``. Written for
  any revision that resolves to a concrete commit; a commit is immutable, so the entry stays
  valid forever. The dirty working copy is never cached.
* **Incremental mypy cache** — ``<dir>/mypy-cache/<commit16>[-wc]``, handed to mypy via
  ``--cache-dir``. One per run so the two runs never invalidate each other, and so the
  developer's own ``.mypy_cache`` is left alone. This is the part that takes real space
  (~2 MB per revision), so it is what retention is really about.

Both backends key on a git SHA, so the cache is shared between `mypy-diff-jj` and
`mypy-diff-git` for the same repository.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import tempfile
from contextlib import ExitStack
from pathlib import Path

from platformdirs import user_cache_dir

from .models import MypyMessage, RevisionInfo

CACHE_VERSION = 3

#: How many recent *runs* to keep. Overridable so CI can keep more, or nothing at all.
KEEP_ENV_VAR = "MYPY_DIFF_CACHE_KEEP"
DEFAULT_KEEP_RUNS = 3

#: A single run touches at most two entries of each kind: the base and the target.
_ENTRIES_PER_RUN = 2

_MYPY_CACHE_SUBDIR = "mypy-cache"


def default_cache_dir() -> Path:
    return Path(user_cache_dir("mypy-diff", appauthor=False))


def keep_runs() -> int:
    """Retention in runs. Non-positive (or unparseable) disables pruning entirely."""
    raw = os.environ.get(KEEP_ENV_VAR)
    if raw is None:
        return DEFAULT_KEEP_RUNS
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_KEEP_RUNS


class ResultCache:
    """Maps (commit, mypy version, mypy args) to a list of diagnostics."""

    def __init__(self, directory: Path | None, enabled: bool = True) -> None:
        self.directory = directory or default_cache_dir()
        self.enabled = enabled

    # -------------------------------------------------------------- result cache

    def key(
        self,
        commit_id: str,
        mypy_version: str,
        mypy_args: tuple[str, ...],
        project: str = ".",
    ) -> str:
        """Cache key for one run.

        ``project`` is the directory mypy ran in, relative to the repository root: one commit
        of a monorepo holds several projects, each with its own config and its own results.

        The mypy configuration is deliberately *not* part of this. Cached runs always happen
        inside a worktree, where the config file comes from the commit itself — so
        ``commit_id`` already pins it, and hashing the working copy's config would only cause
        false misses every time pyproject.toml is touched.
        """
        digest = hashlib.sha256()
        parts = (str(CACHE_VERSION), commit_id, project, mypy_version, "\x00".join(mypy_args))
        for part in parts:
            digest.update(part.encode("utf-8"))
            digest.update(b"\x1f")
        return digest.hexdigest()

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def load(self, key: str) -> tuple[MypyMessage, ...] | None:
        if not self.enabled:
            return None
        path = self._path(key)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        try:
            messages = tuple(MypyMessage.from_json(item) for item in raw["messages"])
        except (KeyError, TypeError, ValueError):
            # A cache entry written by an incompatible version: ignore and re-run.
            return None
        _touch(path)
        return messages

    def store(self, key: str, messages: tuple[MypyMessage, ...]) -> None:
        if not self.enabled:
            return
        path = self._path(key)
        payload = {"version": CACHE_VERSION, "messages": [m.to_json() for m in messages]}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write-then-rename so a crash mid-write cannot leave a truncated entry behind.
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            temporary.replace(path)
        except OSError:
            # A cache we cannot write to is a slowdown, never a failure.
            pass

    # ----------------------------------------------------------- incremental cache

    def mypy_cache_dir(self, revision: RevisionInfo, stack: ExitStack, project: str = ".") -> Path:
        """Directory to hand mypy as ``--cache-dir`` for this revision.

        With caching disabled this is a temporary directory tied to ``stack``, so ``--no-cache``
        really means *nothing is left behind* — not just "skip the result cache". mypy is never
        allowed to fall back to its default ``.mypy_cache``, which would land in the user's
        working copy.
        """
        if not self.enabled:
            temporary = tempfile.TemporaryDirectory(prefix="mypy-diff-", ignore_cleanup_errors=True)
            return Path(stack.enter_context(temporary))

        # The working copy's commit_id is its *parent's*. Without this suffix, a run whose base
        # is HEAD would point both runs at one directory and thrash it.
        suffix = "-wc" if revision.is_working_copy else ""
        if project != ".":
            # Projects of one monorepo commit must not share an incremental cache either.
            suffix = f"-{hashlib.sha256(project.encode('utf-8')).hexdigest()[:8]}{suffix}"
        directory = self.directory / _MYPY_CACHE_SUBDIR / f"{revision.commit_id[:16]}{suffix}"
        # mypy writes deep inside, which never updates the top directory's mtime — and mtime is
        # what retention sorts on. Stamp it explicitly on every use.
        try:
            directory.mkdir(parents=True, exist_ok=True)
            _touch(directory)
        except OSError:
            pass
        return directory

    # ---------------------------------------------------------------- maintenance

    def prune(self, keep: int | None = None) -> None:
        """Drop everything beyond the most recent ``keep`` runs, by last use.

        Best-effort throughout: a concurrent run deleting the same entry must never fail this
        one.
        """
        if not self.enabled:
            return
        limit = keep_runs() if keep is None else keep
        if limit <= 0:
            return
        budget = limit * _ENTRIES_PER_RUN
        _trim(sorted(self.directory.glob("*.json"), key=_mtime, reverse=True)[budget:])
        mypy_cache = self.directory / _MYPY_CACHE_SUBDIR
        if mypy_cache.is_dir():
            stale = sorted(
                (child for child in mypy_cache.iterdir() if child.is_dir()),
                key=_mtime,
                reverse=True,
            )[budget:]
            _trim(stale)

    def clear(self) -> int:
        """Delete everything this tool created here and return the bytes freed.

        Only known content is removed — never a blanket rmtree of ``directory``, which could be
        anything the user passed to ``--cache-dir``.
        """
        freed = 0
        if not self.directory.is_dir():
            return 0
        for pattern in ("*.json", "*.json.tmp"):
            for path in self.directory.glob(pattern):
                freed += _size(path)
                _remove(path)
        mypy_cache = self.directory / _MYPY_CACHE_SUBDIR
        if mypy_cache.is_dir():
            freed += _size(mypy_cache)
            _remove(mypy_cache)
        return freed


def _touch(path: Path) -> None:
    with contextlib.suppress(OSError):
        os.utime(path)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _trim(paths: list[Path]) -> None:
    for path in paths:
        _remove(path)


def _remove(path: Path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def _size(path: Path) -> int:
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            with contextlib.suppress(OSError):
                total += child.stat().st_size
    return total
