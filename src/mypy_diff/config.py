"""Project defaults for mypy-diff's own options, from ``[tool.mypy-diff]`` in pyproject.toml.

A team runs the tool the same way every time — the same base, the same mypy command, the same
targets — and the command line is the wrong place to keep that. The table is looked for in the
project directory and every directory above it up to the repository root; the nearest
pyproject.toml that has one wins. An option given on the command line always beats the file.

Mistakes are errors, not warnings: a misspelt key that is silently ignored would leave a CI
job comparing against the wrong base without anyone noticing.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import MypyDiffError
from .render import Only, OutputFormat

SECTION = "mypy-diff"


@dataclass(frozen=True, slots=True)
class Settings:
    """What the config file says; ``None`` means "not set there"."""

    #: The pyproject.toml the settings came from, or None when there is none.
    path: Path | None = None
    base: str | None = None
    output_format: OutputFormat | None = None
    only: Only | None = None
    show_unchanged: bool | None = None
    mypy_cmd: str | None = None
    fail_on_changed: bool | None = None
    parallel: bool | None = None
    #: mypy arguments to use when none are given after ``--``.
    args: tuple[str, ...] | None = None
    isolated: bool | None = None
    prepare: str | None = None


def _string(key: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _TypeProblem(f"{key!r} must be a non-empty string")
    return value


def _boolean(key: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise _TypeProblem(f"{key!r} must be true or false")
    return value


def _strings(key: str, value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _TypeProblem(f'{key!r} must be a list of strings, e.g. ["src", "tests"]')
    return tuple(value)


def _choice(kind: type[OutputFormat] | type[Only]) -> Callable[[str, object], Any]:
    def convert(key: str, value: object) -> OutputFormat | Only:
        choices = [member.value for member in kind]
        if not isinstance(value, str) or value not in choices:
            raise _TypeProblem(f"{key!r} must be one of: {', '.join(choices)}")
        return kind(value)

    return convert


class _TypeProblem(Exception):
    pass


#: Key in the file -> (field of Settings, converter). Keys are spelt like the long options.
_KEYS: dict[str, tuple[str, Callable[[str, object], Any]]] = {
    "base": ("base", _string),
    "format": ("output_format", _choice(OutputFormat)),
    "only": ("only", _choice(Only)),
    "show-unchanged": ("show_unchanged", _boolean),
    "mypy-cmd": ("mypy_cmd", _string),
    "fail-on-changed": ("fail_on_changed", _boolean),
    "parallel": ("parallel", _boolean),
    "args": ("args", _strings),
    "isolated": ("isolated", _boolean),
    "prepare": ("prepare", _string),
}

KNOWN_KEYS = tuple(_KEYS)


def find_config(directory: Path, root: Path) -> tuple[Path, dict[str, Any]] | None:
    """The nearest pyproject.toml with a ``[tool.mypy-diff]`` table, and that table.

    Searched from ``directory`` up to ``root``. A pyproject.toml that cannot be parsed is passed
    over here, the way the project search treats it: if it is the one mypy reads, mypy itself
    reports it.
    """
    root = root.resolve()
    current = directory.resolve()
    while True:
        path = current / "pyproject.toml"
        if path.is_file():
            try:
                data = tomllib.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
                data = {}
            tool = data.get("tool")
            if isinstance(tool, dict) and SECTION in tool:
                return path, tool[SECTION]
        if current == root or current.parent == current:
            return None
        current = current.parent


def load_settings(directory: Path, root: Path) -> Settings:
    """Read and validate ``[tool.mypy-diff]``; empty :class:`Settings` when there is none."""
    found = find_config(directory, root)
    if found is None:
        return Settings()
    path, table = found
    where = _display(path, root)
    if not isinstance(table, dict):
        raise MypyDiffError(f"{where}: [tool.{SECTION}] must be a table.")

    values: dict[str, Any] = {}
    for key, value in table.items():
        if key not in _KEYS:
            hint = (
                f"Did you mean {key.replace('_', '-')!r}? Keys are spelt like the options."
                if key.replace("_", "-") in _KEYS
                else f"Known keys: {', '.join(KNOWN_KEYS)}."
            )
            raise MypyDiffError(f"{where}: unknown key {key!r} in [tool.{SECTION}].", hint)
        name, convert = _KEYS[key]
        try:
            values[name] = convert(key, value)
        except _TypeProblem as problem:
            raise MypyDiffError(f"{where}: in [tool.{SECTION}], {problem}.") from None
    return Settings(path=path, **values)


def _display(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)
