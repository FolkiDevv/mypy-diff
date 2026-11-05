"""[tool.mypy-diff]: where it is found, what it may hold, and how mistakes are reported."""

from __future__ import annotations

from pathlib import Path

import pytest

from mypy_diff import MypyDiffError
from mypy_diff.config import KNOWN_KEYS, Settings, find_config, load_settings
from mypy_diff.render import Only, OutputFormat


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_no_config_is_empty_settings(tmp_path: Path) -> None:
    assert load_settings(tmp_path, tmp_path) == Settings()


def test_a_pyproject_without_the_table_is_passed_over(tmp_path: Path) -> None:
    """A packaging-only pyproject.toml in the project says nothing about mypy-diff."""
    write(tmp_path / "pyproject.toml", '[tool.mypy-diff]\nbase = "main"\n')
    write(tmp_path / "backend" / "pyproject.toml", '[project]\nname = "backend"\n')

    settings = load_settings(tmp_path / "backend", tmp_path)

    assert settings.path == tmp_path / "pyproject.toml"
    assert settings.base == "main"


def test_the_nearest_table_wins(tmp_path: Path) -> None:
    write(tmp_path / "pyproject.toml", '[tool.mypy-diff]\nbase = "main"\n')
    write(tmp_path / "backend" / "pyproject.toml", '[tool.mypy-diff]\nbase = "develop"\n')

    assert load_settings(tmp_path / "backend", tmp_path).base == "develop"


def test_the_search_stops_at_the_repository_root(tmp_path: Path) -> None:
    write(tmp_path / "pyproject.toml", '[tool.mypy-diff]\nbase = "main"\n')
    repository = tmp_path / "repo"
    repository.mkdir()

    assert find_config(repository, repository) is None


def test_an_unparseable_pyproject_is_passed_over(tmp_path: Path) -> None:
    write(tmp_path / "pyproject.toml", '[tool.mypy-diff]\nbase = "main"\n')
    write(tmp_path / "backend" / "pyproject.toml", "[tool.mypy-diff\n")

    assert load_settings(tmp_path / "backend", tmp_path).base == "main"


def test_every_key_is_read(tmp_path: Path) -> None:
    write(
        tmp_path / "pyproject.toml",
        "[tool.mypy-diff]\n"
        'base = "origin/main"\n'
        'format = "github"\n'
        'only = "new"\n'
        "show-unchanged = true\n"
        'mypy-cmd = "uv run mypy"\n'
        "fail-on-changed = true\n"
        "parallel = false\n"
        'args = ["src", "--strict"]\n'
        "isolated = true\n"
        'prepare = "make proto"\n',
    )

    settings = load_settings(tmp_path, tmp_path)

    assert settings == Settings(
        path=tmp_path / "pyproject.toml",
        base="origin/main",
        output_format=OutputFormat.GITHUB,
        only=Only.NEW,
        show_unchanged=True,
        mypy_cmd="uv run mypy",
        fail_on_changed=True,
        parallel=False,
        args=("src", "--strict"),
        isolated=True,
        prepare="make proto",
    )
    assert len(KNOWN_KEYS) == 10


def test_an_unknown_key_is_an_error(tmp_path: Path) -> None:
    """Ignored silently, a typo would leave CI comparing against the wrong base."""
    write(tmp_path / "pyproject.toml", '[tool.mypy-diff]\nbse = "main"\n')

    with pytest.raises(MypyDiffError) as raised:
        load_settings(tmp_path, tmp_path)

    assert "unknown key 'bse'" in raised.value.message
    assert "pyproject.toml" in raised.value.message
    assert any("Known keys: base" in detail for detail in raised.value.details)


def test_a_snake_case_key_gets_a_hint(tmp_path: Path) -> None:
    write(tmp_path / "pyproject.toml", "[tool.mypy-diff]\nshow_unchanged = true\n")

    with pytest.raises(MypyDiffError) as raised:
        load_settings(tmp_path, tmp_path)

    assert any("'show-unchanged'" in detail for detail in raised.value.details)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("base = 1", "'base' must be a non-empty string"),
        ('base = ""', "'base' must be a non-empty string"),
        ('parallel = "yes"', "'parallel' must be true or false"),
        ('format = "html"', "'format' must be one of: pretty, plain, json, github"),
        ('only = "all-of-it"', "'only' must be one of: all, new, fixed"),
        ('args = "src"', "'args' must be a list of strings"),
        ("args = [1, 2]", "'args' must be a list of strings"),
    ],
)
def test_a_wrong_type_is_an_error(tmp_path: Path, line: str, expected: str) -> None:
    write(tmp_path / "pyproject.toml", f"[tool.mypy-diff]\n{line}\n")

    with pytest.raises(MypyDiffError) as raised:
        load_settings(tmp_path, tmp_path)

    assert expected in raised.value.message


def test_the_table_must_be_a_table(tmp_path: Path) -> None:
    write(tmp_path / "pyproject.toml", '[tool]\nmypy-diff = "main"\n')

    with pytest.raises(MypyDiffError, match="must be a table"):
        load_settings(tmp_path, tmp_path)
