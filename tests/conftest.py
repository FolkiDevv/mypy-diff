"""Shared fixtures, including real jj and git repositories built on the fly."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

JJ = shutil.which("jj")
GIT = shutil.which("git")

needs_jj = pytest.mark.skipif(JJ is None, reason="jj is not installed")
needs_git = pytest.mark.skipif(GIT is None, reason="git is not installed")

#: A file that type-checks cleanly, used as the base revision content.
CLEAN_SOURCE = """\
def add(a: int, b: int) -> int:
    return a + b


def greet(name: str) -> str:
    return "hello " + name
"""

#: One pre-existing error, present in *both* revisions, so it must come out "unchanged".
PREEXISTING_SOURCE = """\
def broken() -> int:
    return "already wrong"
"""

PROJECT_CONFIG = """\
[tool.mypy]
python_version = "3.12"
"""


@dataclass(frozen=True)
class Repo:
    """A throwaway repository plus the paths the tests care about."""

    root: Path
    vcs: str

    def write(self, relative: str, content: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def read(self, relative: str) -> str:
        return (self.root / relative).read_text(encoding="utf-8")


def run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        check=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "LC_ALL": "C", "NO_COLOR": "1"},
    )


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the default cache directory at a temp dir, for every single test.

    Without this the suite writes into the user's real `%LOCALAPPDATA%\\mypy-diff\\Cache`: each
    fixture repo has fresh commit ids, so every run used to leave ~2 MB behind permanently.
    Autouse so a test that forgets `--no-cache` still cannot escape.
    """
    directory = tmp_path / "mypy-diff-cache"
    monkeypatch.setattr("mypy_diff.cache.default_cache_dir", lambda: directory)
    return directory


@pytest.fixture
def short_tmp_path() -> Iterator[Path]:
    """A temp directory with a *short* path.

    jj writes deeply nested index segment files; under pytest's default tmp_path the
    combined length trips the Windows path limit and `jj git init` fails outright.
    """
    parent = Path(tempfile.gettempdir()) / "mdt"
    parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(dir=parent))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


#: The monorepo's config names its targets, relative to the project directory — so mypy only
#: finds them when it runs there, not from the repository root.
SUBPROJECT_CONFIG = """\
[tool.mypy]
python_version = "3.12"
files = ["src"]
"""

#: Where the monorepo fixtures keep their Python project.
SUBPROJECT = "backend"


def _seed(root: Path, project: str | None = None) -> None:
    """Write the fixture project at the root, or in the ``project`` subdirectory."""
    directory = root if project is None else root / project
    (directory / "src").mkdir(parents=True, exist_ok=True)
    config = PROJECT_CONFIG if project is None else SUBPROJECT_CONFIG
    (directory / "pyproject.toml").write_text(config, encoding="utf-8")
    (directory / "src" / "clean.py").write_text(CLEAN_SOURCE, encoding="utf-8")
    (directory / "src" / "legacy.py").write_text(PREEXISTING_SOURCE, encoding="utf-8")
    if project is not None:
        # Something non-Python at the root, the way a real monorepo looks.
        (root / "README.md").write_text("monorepo\n", encoding="utf-8")


@pytest.fixture
def git_repo(short_tmp_path: Path) -> Repo:
    """A git repo with one commit on `main`, pushed to a bare remote.

    The remote matters: the default base is derived from remote-tracking refs.
    """
    return _make_git_repo(short_tmp_path)


@pytest.fixture
def git_monorepo(short_tmp_path: Path) -> Repo:
    """Like `git_repo`, but the Python project lives in `backend/`."""
    return _make_git_repo(short_tmp_path, SUBPROJECT)


def _make_git_repo(short_tmp_path: Path, project: str | None = None) -> Repo:
    assert GIT is not None
    root = short_tmp_path / "work"
    root.mkdir()

    run([GIT, "init", "--initial-branch=main", "--quiet"], cwd=root)
    run([GIT, "config", "user.email", "test@example.com"], cwd=root)
    run([GIT, "config", "user.name", "Test"], cwd=root)
    run([GIT, "config", "commit.gpgsign", "false"], cwd=root)
    _seed(root, project)
    run([GIT, "add", "-A"], cwd=root)
    run([GIT, "commit", "-q", "-m", "initial"], cwd=root)

    remote = short_tmp_path / "remote.git"
    run([GIT, "init", "--bare", "--quiet", str(remote)], cwd=short_tmp_path)
    run([GIT, "remote", "add", "origin", str(remote)], cwd=root)
    run([GIT, "push", "-q", "-u", "origin", "main"], cwd=root)

    return Repo(root=root, vcs="git")


@pytest.fixture
def jj_repo(short_tmp_path: Path) -> Repo:
    """A jj workspace with one described commit bookmarked `main`, and a fresh empty `@`."""
    return _make_jj_repo(short_tmp_path)


@pytest.fixture
def jj_monorepo(short_tmp_path: Path) -> Repo:
    """Like `jj_repo`, but the Python project lives in `backend/`."""
    return _make_jj_repo(short_tmp_path, SUBPROJECT)


def _make_jj_repo(short_tmp_path: Path, project: str | None = None) -> Repo:
    assert JJ is not None
    root = short_tmp_path / "work"
    root.mkdir()

    run([JJ, "git", "init"], cwd=root)
    run([JJ, "config", "set", "--repo", "user.name", "Test"], cwd=root)
    run([JJ, "config", "set", "--repo", "user.email", "test@example.com"], cwd=root)
    _seed(root, project)
    run([JJ, "describe", "-m", "initial"], cwd=root)
    run([JJ, "bookmark", "create", "main", "-r", "@"], cwd=root)
    run([JJ, "new"], cwd=root)

    return Repo(root=root, vcs="jj")
