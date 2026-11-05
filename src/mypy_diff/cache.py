"""On-disk caches for mypy results.

Two separate things live under the same directory:

* **Result cache** — the parsed diagnostics of one run, as ``<dir>/<sha256>.json``. Written for
  any revision that resolves to a concrete commit. A commit is immutable, but the environment
  mypy resolves imports in is not, so the key also carries a fingerprint of that environment.
  The dirty working copy is never cached.
* **Incremental mypy cache** — ``<dir>/mypy-cache/<project16>/{base,target}``, handed to mypy
  via ``--cache-dir``. One per side of a run, so the two runs never invalidate each other, and
  so the developer's own ``.mypy_cache`` is left alone; kept across commits, because mypy
  re-validates every module by content hash. This is the part that takes real space (megabytes
  per side, growing with the project), so it is what retention is really about.

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

from . import __version__
from .models import MypyMessage

#: Bumped whenever a cached result may no longer be what this version would compute for the same
#: key. 5: the base imports the repository's packages from its own checkout.
CACHE_VERSION = 5

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
    """Maps (commit, mypy version, mypy args, environment) to a list of diagnostics."""

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
        environment: str = "",
    ) -> str:
        """Cache key for one run.

        ``project`` is the directory mypy ran in, relative to the repository root: one commit
        of a monorepo holds several projects, each with its own config and its own results.

        ``environment`` is :func:`mypy_exec.environment_fingerprint`: what is installed where mypy
        resolves imports. A commit does not pin that, and a base result computed before a stub
        package was installed would otherwise be compared with a target computed after it.

        The mypy configuration is deliberately *not* part of this. Cached runs always happen
        inside a worktree, where the config file comes from the commit itself — so
        ``commit_id`` already pins it, and hashing the working copy's config would only cause
        false misses every time pyproject.toml is touched.
        """
        digest = hashlib.sha256()
        parts = (
            str(CACHE_VERSION),
            __version__,
            commit_id,
            project,
            mypy_version,
            "\x00".join(mypy_args),
            environment,
        )
        for part in parts:
            digest.update(part.encode("utf-8"))
            digest.update(b"\x1f")
        return digest.hexdigest()

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def load(self, key: str) -> tuple[MypyMessage, ...] | None:
        entry = self.load_entry(key)
        return None if entry is None else entry[0]

    def load_entry(self, key: str) -> tuple[tuple[MypyMessage, ...], tuple[str, ...]] | None:
        """The cached diagnostics, and the targets that run had to leave out (see run_mypy)."""
        if not self.enabled:
            return None
        path = self._path(key)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        try:
            messages = tuple(MypyMessage.from_json(item) for item in raw["messages"])
            dropped = tuple(str(item) for item in raw.get("dropped", ()))
        except (KeyError, TypeError, ValueError, AttributeError):
            # A cache entry written by an incompatible version: ignore and re-run.
            return None
        _touch(path)
        return messages, dropped

    def store(
        self, key: str, messages: tuple[MypyMessage, ...], dropped: tuple[str, ...] = ()
    ) -> None:
        if not self.enabled:
            return
        path = self._path(key)
        payload = {
            "version": CACHE_VERSION,
            "messages": [m.to_json() for m in messages],
            "dropped": list(dropped),
        }
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

    def mypy_cache_dir(self, role: str, stack: ExitStack, location: str) -> Path:
        """Directory to hand mypy as ``--cache-dir`` for one side of a run.

        ``role`` is "base" or "target"; ``location`` identifies the repository and project (see
        :func:`location`). The two sides never share a directory, so they never invalidate each
        other. Across runs, though, each side keeps its directory, whatever commit it checks:
        mypy validates every module by its content hash, so the cache of the previous base is a
        warm start for the next one. Keyed by commit, every commit, amend or push used to mean a
        cold, full mypy run.

        With caching disabled this is a temporary directory tied to ``stack``, so ``--no-cache``
        really means *nothing is left behind* — not just "skip the result cache". mypy is never
        allowed to fall back to its default ``.mypy_cache``, which would land in the user's
        working copy.
        """
        if not self.enabled:
            temporary = tempfile.TemporaryDirectory(prefix="mypy-diff-", ignore_cleanup_errors=True)
            return Path(stack.enter_context(temporary))

        place = self._place(location)
        directory = place / role
        # mypy writes deep inside, which never updates the top directory's mtime — and mtime is
        # what retention sorts on. Stamp it explicitly on every use.
        try:
            directory.mkdir(parents=True, exist_ok=True)
            _touch(directory)
            _touch(place)
        except OSError:
            pass
        return directory

    def seed(self, location: str) -> None:
        """Start a side that has no incremental cache yet from the other side's.

        The first base run after the target has run (or the other way round) then finds most
        modules unchanged. Called before either side runs, never while one does: copying a
        cache mypy is writing could copy it half-written.

        Another mypy-diff run on the same project may be seeding at the same moment. So the copy
        goes to a directory of this run's own and is renamed into place in one step: whichever
        run renames first wins, and the other only ever removes its own copy — never a cache the
        winner may already be using.
        """
        if not self.enabled:
            return
        place = self._place(location)
        for role, other in (("base", "target"), ("target", "base")):
            source, destination = place / other, place / role
            if destination.exists() or not source.is_dir():
                continue
            try:
                staging = Path(tempfile.mkdtemp(prefix=f".{role}-", dir=place))
            except OSError:
                continue
            try:
                shutil.copytree(source, staging, dirs_exist_ok=True)
                staging.rename(destination)
            except OSError:
                pass
            finally:
                # Gone already after a successful rename; this run's own copy otherwise.
                shutil.rmtree(staging, ignore_errors=True)

    def _place(self, location: str) -> Path:
        digest = hashlib.sha256(location.encode("utf-8")).hexdigest()[:16]
        return self.directory / _MYPY_CACHE_SUBDIR / digest

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
            # One directory per project (holding its base and target caches): keep the projects
            # of the last `limit` runs. Directories of the old per-commit layout are the oldest
            # and go first.
            stale = sorted(
                (child for child in mypy_cache.iterdir() if child.is_dir()),
                key=_mtime,
                reverse=True,
            )[limit:]
            _trim(stale)

    def size(self) -> int:
        """Bytes this tool's content takes up in the cache directory."""
        if not self.directory.is_dir():
            return 0
        total = sum(_size(path) for path in self.directory.glob("*.json"))
        mypy_cache = self.directory / _MYPY_CACHE_SUBDIR
        return total + (_size(mypy_cache) if mypy_cache.is_dir() else 0)

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


def location(root: Path, project: str) -> str:
    """What identifies one project's incremental caches: its repository and subdirectory."""
    return f"{root.resolve()}\0{project}"
