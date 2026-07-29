"""On-disk cache of mypy results for immutable revisions.

Only the base side is ever cached, and only when it resolves to a concrete commit: a commit
is immutable, so its result stays valid forever. The dirty working copy is never cached.

Because both backends key on a git SHA, the cache is shared between `mypy-diff-jj` and
`mypy-diff-git` for the same repository.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from platformdirs import user_cache_dir

from .models import MypyMessage

CACHE_VERSION = 1

#: mypy configuration files that change what a run reports, and so belong in the cache key.
_CONFIG_FILES = ("mypy.ini", ".mypy.ini", "setup.cfg", "pyproject.toml")


def default_cache_dir() -> Path:
    return Path(user_cache_dir("mypy-diff", appauthor=False))


class ResultCache:
    """Maps (commit, mypy version, mypy args, mypy config) to a list of diagnostics."""

    def __init__(self, directory: Path | None, enabled: bool = True) -> None:
        self.directory = directory or default_cache_dir()
        self.enabled = enabled

    def key(
        self, commit_id: str, mypy_version: str, mypy_args: tuple[str, ...], root: Path
    ) -> str:
        digest = hashlib.sha256()
        for part in (str(CACHE_VERSION), commit_id, mypy_version, "\x00".join(mypy_args)):
            digest.update(part.encode("utf-8"))
            digest.update(b"\x1f")
        digest.update(_config_fingerprint(root))
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
            return tuple(MypyMessage.from_json(item) for item in raw["messages"])
        except (KeyError, TypeError, ValueError):
            # A cache entry written by an incompatible version: ignore and re-run.
            return None

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

    def revision_mypy_cache(self, commit_id: str) -> Path:
        """Per-revision incremental mypy cache directory.

        Keeping these apart means the base run and the target run never invalidate each
        other, and the developer's own `.mypy_cache` is left alone.
        """
        return self.directory / "mypy-cache" / commit_id[:16]


def _config_fingerprint(root: Path) -> bytes:
    digest = hashlib.sha256()
    for name in _CONFIG_FILES:
        candidate = root / name
        try:
            digest.update(candidate.read_bytes())
        except OSError:
            digest.update(b"")
        digest.update(b"\x1f")
    return digest.digest()
