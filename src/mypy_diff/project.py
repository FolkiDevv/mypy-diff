"""Locate the Python project inside the repository.

The project is not necessarily the repository root: a monorepo keeps its Python code — and the
pyproject.toml that configures mypy — in a subdirectory such as ``backend/``. mypy reads its
config, and resolves the ``files`` it lists, relative to the directory it runs in, so both
revisions must be checked from that subdirectory, not from the root.
"""

from __future__ import annotations

import configparser
import posixpath
import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import MypyDiffError

#: Files that mark a directory as the one mypy should run in. The same set mypy itself looks
#: for when it searches for a config file.
CONFIG_FILES = ("mypy.ini", ".mypy.ini", "pyproject.toml", "setup.cfg")


def has_mypy_config(directory: Path) -> bool:
    """Whether ``directory`` holds a config file mypy would actually use.

    Like mypy itself, pyproject.toml and setup.cfg only count when they have a mypy section —
    a pyproject.toml that just packages a library says nothing about where mypy should run.
    """
    for name in CONFIG_FILES:
        path = directory / name
        if not path.is_file():
            continue
        if name == "pyproject.toml":
            if _has_tool_mypy(path):
                return True
        elif name == "setup.cfg":
            if _has_mypy_section(path):
                return True
        else:
            return True
    return False


def _has_tool_mypy(path: Path) -> bool:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return False
    tool = data.get("tool")
    return isinstance(tool, dict) and "mypy" in tool


def _has_mypy_section(path: Path) -> bool:
    parser = configparser.RawConfigParser()
    try:
        # No encoding on purpose: mypy reads INI configs with the default one (locale, or UTF-8
        # in UTF-8 mode), and we must agree with it on whether the file is readable.
        parser.read(path)
    except (configparser.Error, UnicodeDecodeError):
        return False
    return parser.has_section("mypy")


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
        if has_mypy_config(current):
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


def mypy_config_file(directory: Path, root: Path) -> Path | None:
    """The config file mypy will read when run in ``directory``, if any.

    The same search as mypy's own: in each directory from ``directory`` up to the repository
    root, the first of ``CONFIG_FILES`` that is a mypy config.
    """
    root = root.resolve()
    current = directory.resolve()
    while True:
        for name in CONFIG_FILES:
            path = current / name
            if path.is_file() and has_mypy_config_file(path):
                return path
        if current == root or current.parent == current:
            return None
        current = current.parent


def has_mypy_config_file(path: Path) -> bool:
    if path.name == "pyproject.toml":
        return _has_tool_mypy(path)
    if path.name == "setup.cfg":
        return _has_mypy_section(path)
    return True


def configured_targets(path: Path) -> list[str]:
    """The `files`, `packages` and `modules` a mypy config names, as written."""
    if path.name == "pyproject.toml":
        try:
            section = (
                tomllib.loads(path.read_text(encoding="utf-8")).get("tool", {}).get("mypy", {})
            )
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            return []
        found: list[str] = []
        for key in ("files", "packages", "modules"):
            value = section.get(key)
            if isinstance(value, str):
                found += [part.strip() for part in value.split(",") if part.strip()]
            elif isinstance(value, list):
                found += [str(part) for part in value]
        return found
    parser = configparser.RawConfigParser()
    try:
        parser.read(path)
    except (configparser.Error, UnicodeDecodeError):
        return []
    if not parser.has_section("mypy"):
        return []
    return [
        part.strip()
        for key in ("files", "packages", "modules")
        for part in parser.get("mypy", key, fallback="").split(",")
        if part.strip()
    ]
