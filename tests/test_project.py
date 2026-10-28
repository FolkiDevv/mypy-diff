"""Finding the Python project inside the repository."""

from __future__ import annotations

from pathlib import Path

import pytest

from mypy_diff import MypyDiffError
from mypy_diff.project import Project, find_project


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "backend" / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    (tmp_path / "backend" / "pyproject.toml").write_text("[tool.mypy]\n", encoding="utf-8")
    return tmp_path.resolve()


def test_the_root_is_the_project_when_run_from_it(root: Path) -> None:
    assert find_project(root, root) == Project(root)


def test_the_nearest_config_above_the_cwd_wins(root: Path) -> None:
    project = find_project(root, root / "backend" / "src" / "pkg")
    assert project.subdir == "backend"
    assert project.directory == root / "backend"


def test_a_directory_without_any_config_falls_back_to_the_root(root: Path) -> None:
    assert find_project(root, root / "docs").subdir == "."


def test_a_root_level_config_is_not_skipped_over(root: Path) -> None:
    (root / "setup.cfg").write_text("[mypy]\n", encoding="utf-8")
    assert find_project(root, root / "docs").subdir == "."


@pytest.mark.parametrize("name", ["mypy.ini", ".mypy.ini", "setup.cfg"])
def test_every_mypy_config_file_counts(root: Path, name: str) -> None:
    (root / "docs" / name).write_text("[mypy]\n", encoding="utf-8")
    assert find_project(root, root / "docs").subdir == "docs"


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("pyproject.toml", "[project]\nname = 'pkg'\n\n[tool.ruff]\nline-length = 100\n"),
        ("setup.cfg", "[metadata]\nname = pkg\n\n[flake8]\nmax-line-length = 100\n"),
        ("pyproject.toml", "this is [not toml"),
    ],
)
def test_a_config_without_a_mypy_section_does_not_count(
    root: Path, name: str, content: str
) -> None:
    """mypy ignores these, so running from their directory would resolve `files` wrongly."""
    (root / "mypy.ini").write_text("[mypy]\nfiles = backend/src\n", encoding="utf-8")
    (root / "docs" / name).write_text(content, encoding="utf-8")
    assert find_project(root, root / "docs").subdir == "."


def test_explicit_project_is_relative_to_the_cwd(root: Path) -> None:
    assert find_project(root, root / "docs", Path("../backend")).subdir == "backend"


def test_explicit_project_may_be_the_root(root: Path) -> None:
    assert find_project(root, root / "backend", Path("..")).subdir == "."


def test_explicit_project_must_exist(root: Path) -> None:
    with pytest.raises(MypyDiffError, match="not a directory"):
        find_project(root, root, Path("nope"))


def test_explicit_project_must_be_inside_the_repository(root: Path, tmp_path: Path) -> None:
    with pytest.raises(MypyDiffError, match="outside the repository"):
        find_project(root / "backend", root, root / "docs")


def test_cwd_outside_the_repository_falls_back_to_the_root(root: Path) -> None:
    assert find_project(root / "backend", root / "docs").subdir == "."


class TestRepoPath:
    def test_root_project_leaves_paths_alone(self, root: Path) -> None:
        assert Project(root).repo_path("src/a.py") == "src/a.py"

    def test_subproject_paths_get_the_prefix(self, root: Path) -> None:
        assert Project(root, "backend").repo_path("src/a.py") == "backend/src/a.py"

    def test_paths_leaving_the_project_are_normalized(self, root: Path) -> None:
        assert Project(root, "backend").repo_path("../shared/b.py") == "shared/b.py"

    def test_directory_in_a_checkout(self, root: Path, tmp_path: Path) -> None:
        checkout = tmp_path / "checkout"
        assert Project(root, "backend").directory_in(checkout) == checkout / "backend"
        assert Project(root).directory_in(checkout) == checkout
