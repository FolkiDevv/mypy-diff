"""Locate the Python project inside the repository.

The project is not necessarily the repository root: a monorepo keeps its Python code — and the
pyproject.toml that configures mypy — in a subdirectory such as ``backend/``. mypy reads its
config, and resolves the ``files`` it lists, relative to the directory it runs in, so both
revisions must be checked from that subdirectory, not from the root.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from pathlib import Path

from . import MypyDiffError

#: Files that mark a directory as the one mypy should run in. The same set mypy itself looks
#: for when it searches for a config file.
CONFIG_FILES = ("mypy.ini", ".mypy.ini", "pyproject.toml", "setup.cfg")


@dataclass(frozen=True, slots=True)
class Project:
    """Where the Python project lives, relative to the repository root."""

    root: Path
    #: POSIX path of the project relative to ``root``; ``"."`` when they are the same.
    subdir: str = "."

    @property
    def directory(self) -> Path:
        """The project directory in the working copy on disk."""
        return self.root if self.subdir == "." else self.root / self.subdir

    def directory_in(self, checkout: Path) -> Path:
        """The project directory inside a temporary checkout of another revision."""
        return checkout if self.subdir == "." else checkout / self.subdir

    def repo_path(self, path: str) -> str:
        """Turn a path mypy printed (relative to the project) into a repository path."""
        if self.subdir == ".":
            return path
        return posixpath.normpath(posixpath.join(self.subdir, path))


def find_project(root: Path, cwd: Path, explicit: Path | None = None) -> Project:
    """Pick the project directory.

    ``explicit`` (``--project``) wins. Otherwise walk up from ``cwd`` and take the nearest
    directory holding a mypy config file, stopping at the repository root — which is also the
    answer when nothing is found, so a plain single-project repository behaves as before.
    """
    root = root.resolve()
    if explicit is not None:
        directory = (cwd / explicit).resolve()
        if not directory.is_dir():
            raise MypyDiffError(f"--project {explicit}: not a directory.")
        return Project(root, _relative(directory, root, f"--project {explicit}"))

    current = cwd.resolve()
    if not current.is_relative_to(root):
        return Project(root)
    while current != root:
        if any((current / name).is_file() for name in CONFIG_FILES):
            return Project(root, _relative(current, root, str(current)))
        current = current.parent
    return Project(root)


def _relative(directory: Path, root: Path, what: str) -> str:
    try:
        return directory.relative_to(root).as_posix()
    except ValueError:
        raise MypyDiffError(
            f"{what} is outside the repository.",
            f"The repository root is {root}.",
        ) from None
