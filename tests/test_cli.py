"""End-to-end CLI runs against real repositories, for both backends.

These invoke the real mypy, so they are the slowest tests in the suite — but they are the
only ones that prove the whole pipeline (resolve -> worktree -> mypy -> match -> render).
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from mypy_diff.cli import EXIT_FAILURE, EXIT_NEW_ERRORS, EXIT_OK, build_app
from mypy_diff.vcs import GitBackend, JjBackend

from .conftest import GIT, JJ, Repo, run

runner = CliRunner()

#: mypy from this project's own environment; the fixture repos have no venv of their own.
MYPY_CMD = f'"{sys.executable}" -m mypy'

BROKEN_SOURCE = """\
def add(a: int, b: int) -> int:
    return a + b


def greet(name: str) -> str:
    return 42
"""


@pytest.fixture(params=["git", "jj"])
def repo(request: pytest.FixtureRequest) -> Repo:
    """The same scenario on both backends."""
    if request.param == "git" and GIT is None:
        pytest.skip("git is not installed")
    if request.param == "jj" and JJ is None:
        pytest.skip("jj is not installed")
    return request.getfixturevalue(f"{request.param}_repo")  # type: ignore[no-any-return]


def invoke(repo: Repo, *args: str, cache: Path | None = None) -> Result:
    """Run the CLI in-process via CliRunner. Caching is off unless a directory is given."""
    app = build_app(JjBackend if repo.vcs == "jj" else GitBackend)
    cache_args = ["--no-cache"] if cache is None else ["--cache-dir", str(cache)]
    return runner.invoke(app, ["--mypy-cmd", MYPY_CMD, *cache_args, *args], catch_exceptions=False)


@pytest.fixture(autouse=True)
def _in_repo(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Run each test from inside its repository, the way a user would."""
    if "repo" in request.fixturenames:
        monkeypatch.chdir(request.getfixturevalue("repo").root)


# ------------------------------------------------------------------ basics


def test_doctor_reports_the_tools(repo: Repo) -> None:
    result = invoke(repo, "--doctor")
    assert result.exit_code == EXIT_OK
    assert repo.vcs in result.output
    assert "mypy" in result.output
    assert "repository root" in result.output


def test_clean_working_copy_reports_no_change(repo: Repo) -> None:
    result = invoke(repo, "--", "src")
    assert result.exit_code == EXIT_OK
    assert "No change in mypy errors." in result.output


def test_a_new_error_is_reported_and_exits_one(repo: Repo) -> None:
    repo.write("src/clean.py", BROKEN_SOURCE)
    result = invoke(repo, "--", "src")
    assert result.exit_code == EXIT_NEW_ERRORS
    assert "+ NEW (1)" in result.output
    assert "src/clean.py:6" in result.output
    assert "[return-value]" in result.output


def test_a_fixed_error_is_reported(repo: Repo) -> None:
    # src/legacy.py ships with one deliberate error; correcting it must show as fixed.
    repo.write("src/legacy.py", "def broken() -> str:\n    return 'now correct'\n")
    result = invoke(repo, "--", "src")
    assert result.exit_code == EXIT_OK
    assert "- FIXED (1)" in result.output


def test_a_preexisting_error_is_unchanged_not_new(repo: Repo) -> None:
    """The error in src/legacy.py exists in both revisions and must not be reported."""
    repo.write("src/clean.py", BROKEN_SOURCE)
    result = invoke(repo, "--", "src")
    assert "+ NEW (1)" in result.output
    assert "1 unchanged error" in result.output
    assert "legacy.py" not in result.output.split("unchanged")[0]


def test_exit_zero_suppresses_the_failure_code(repo: Repo) -> None:
    repo.write("src/clean.py", BROKEN_SOURCE)
    assert invoke(repo, "--exit-zero", "--", "src").exit_code == EXIT_OK


# ------------------------------------------------------------------ shifting lines


def test_shifting_every_line_does_not_invent_new_errors(repo: Repo) -> None:
    """The core promise: unrelated edits above an error must not resurface it."""
    repo.write("src/legacy.py", "# padding\n" * 40 + repo.read("src/legacy.py"))
    result = invoke(repo, "--", "src")
    assert result.exit_code == EXIT_OK
    assert "+ NEW" not in result.output
    assert "- FIXED" not in result.output
    assert "1 unchanged error" in result.output


# ------------------------------------------------------------------ formats


def test_json_format_is_parseable(repo: Repo) -> None:
    repo.write("src/clean.py", BROKEN_SOURCE)
    result = invoke(repo, "--format", "json", "--", "src")
    payload = json.loads(result.output)
    assert payload["summary"]["new"] == 1
    assert payload["new"][0]["path"] == "src/clean.py"
    assert payload["new"][0]["code"] == "return-value"


def test_github_format_emits_annotations(repo: Repo) -> None:
    repo.write("src/clean.py", BROKEN_SOURCE)
    result = invoke(repo, "--format", "github", "--", "src")
    assert "::error file=src/clean.py,line=6" in result.output


def test_only_new_filters_the_output(repo: Repo) -> None:
    repo.write("src/legacy.py", "def broken() -> str:\n    return 'now correct'\n")
    result = invoke(repo, "--only", "new", "--", "src")
    assert "FIXED" not in result.output


# ------------------------------------------------------------------ revisions


def test_explicit_base_by_branch_name(repo: Repo) -> None:
    repo.write("src/clean.py", BROKEN_SOURCE)
    result = invoke(repo, "-b", "main", "--", "src")
    assert result.exit_code == EXIT_NEW_ERRORS
    assert "+ NEW (1)" in result.output


def test_unknown_base_fails_cleanly(repo: Repo) -> None:
    result = invoke(repo, "-b", "no-such-branch", "--", "src")
    assert result.exit_code == EXIT_FAILURE
    assert "Could not resolve revision" in result.output


def test_comparing_a_revision_against_itself_is_empty(repo: Repo) -> None:
    """Exercises the temporary-worktree path on both sides."""
    tip = "main"
    result = invoke(repo, "-b", tip, "--to", tip, "--", "src")
    assert result.exit_code == EXIT_OK
    assert "No change in mypy errors." in result.output


def registered_worktrees(repo: Repo) -> str:
    if repo.vcs == "jj":
        assert JJ is not None
        return run([JJ, "workspace", "list", "--ignore-working-copy"], cwd=repo.root).stdout
    assert GIT is not None
    return run([GIT, "worktree", "list"], cwd=repo.root).stdout


def temp_worktrees() -> set[Path]:
    """Scratch directories both backends create via mkdtemp(prefix="mypy-diff-")."""
    return set(Path(tempfile.gettempdir()).glob("mypy-diff-*"))


def test_worktrees_are_not_left_behind(repo: Repo) -> None:
    before = temp_worktrees()
    invoke(repo, "-b", "main", "--to", "main", "--", "src")

    assert "mypy-diff" not in registered_worktrees(repo)
    assert temp_worktrees() - before == set()


def test_worktrees_are_cleaned_up_when_mypy_dies(repo: Repo) -> None:
    """The failure path the happy-path test cannot reach.

    A bogus mypy flag makes mypy exit 2, which raises out of the middle of the ExitStack that
    owns the worktree — exactly where a leak would happen.
    """
    before = temp_worktrees()
    result = invoke(repo, "-b", "main", "--to", "main", "--", "--not-a-real-mypy-flag", "src")

    assert result.exit_code == EXIT_FAILURE
    assert "mypy-diff" not in registered_worktrees(repo)
    assert temp_worktrees() - before == set()


# ------------------------------------------------------------------ cache


def test_base_results_are_cached_and_reused(repo: Repo, short_tmp_path: Path) -> None:
    cache = short_tmp_path / "cache"
    repo.write("src/clean.py", BROKEN_SOURCE)

    first = invoke(repo, "--", "src", cache=cache)
    assert first.exit_code == EXIT_NEW_ERRORS
    assert list(cache.glob("*.json")), "the base result should have been written to the cache"

    second = invoke(repo, "-v", "--", "src", cache=cache)
    assert second.exit_code == EXIT_NEW_ERRORS
    assert "cache hit" in second.output


def test_no_cache_leaves_nothing_behind(repo: Repo, isolated_cache: Path) -> None:
    """--no-cache must disable mypy's incremental cache too, not just the result cache."""
    repo.write("src/clean.py", BROKEN_SOURCE)

    assert invoke(repo, "--", "src").exit_code == EXIT_NEW_ERRORS

    leftovers = list(isolated_cache.rglob("*")) if isolated_cache.exists() else []
    assert leftovers == [], f"--no-cache wrote to the cache directory: {leftovers}"


def test_a_normal_run_keeps_mypy_caches_apart(repo: Repo, short_tmp_path: Path) -> None:
    """Base and working copy must not share one incremental cache directory."""
    cache = short_tmp_path / "cache"
    invoke(repo, "--", "src", cache=cache)

    directories = sorted(p.name for p in (cache / "mypy-cache").iterdir())
    assert len(directories) == 2, directories
    assert any(name.endswith("-wc") for name in directories)


def test_retention_caps_the_incremental_cache(
    repo: Repo, short_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keeping 1 run means at most 2 directories survive, however many revisions we check."""
    monkeypatch.setenv("MYPY_DIFF_CACHE_KEEP", "1")
    cache = short_tmp_path / "cache"

    for index in range(3):
        repo.write(f"src/extra{index}.py", f"value{index}: int = {index}\n")
        invoke(repo, "--", "src", cache=cache)

    assert len(list((cache / "mypy-cache").iterdir())) <= 2


def test_clean_cache_empties_the_directory(repo: Repo, short_tmp_path: Path) -> None:
    cache = short_tmp_path / "cache"
    invoke(repo, "--", "src", cache=cache)
    assert list(cache.rglob("*")), "expected the run to populate the cache"

    app = build_app(JjBackend if repo.vcs == "jj" else GitBackend)
    result = runner.invoke(
        app, ["--cache-dir", str(cache), "--clean-cache"], catch_exceptions=False
    )

    assert result.exit_code == EXIT_OK
    assert "Removed" in result.output
    assert list(cache.glob("*.json")) == []
    assert not (cache / "mypy-cache").exists()


def test_clean_cache_works_outside_a_repository(
    short_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clearing the cache must not require resolving a repository or finding mypy."""
    outside = short_tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.chdir(outside)

    result = runner.invoke(
        build_app(GitBackend),
        ["--cache-dir", str(short_tmp_path / "cache"), "--clean-cache"],
        catch_exceptions=False,
    )
    assert result.exit_code == EXIT_OK


# ------------------------------------------------------------------ failures


def test_no_targets_reports_an_actionable_error(repo: Repo) -> None:
    """A bare run in a project whose mypy config names no files.

    The fixture's [tool.mypy] has no `files` key, so this is exactly what a user hits when
    they type `mypy-diff-jj` with nothing after it.
    """
    result = invoke(repo)

    assert result.exit_code == EXIT_FAILURE
    assert "mypy has no files to check" in result.output
    assert f"mypy-diff-{repo.vcs} -- src tests" in result.output
    assert 'files = ["src", "tests"]' in result.output
    # The raw usage dump is noise and must not survive.
    assert "usage: mypy" not in result.output
    # Regression: rich parses `[tool.mypy]` as markup and drops it unless the text is escaped.
    assert "[tool.mypy]" in result.output


def test_missing_mypy_reports_an_actionable_error(repo: Repo) -> None:
    app = build_app(JjBackend if repo.vcs == "jj" else GitBackend)
    result = runner.invoke(
        app, ["--mypy-cmd", "definitely-not-a-real-mypy-binary"], catch_exceptions=False
    )
    assert result.exit_code == EXIT_FAILURE
    assert "Could not find a usable `mypy`" in result.output
    assert "--mypy-cmd" in result.output


def test_running_outside_a_repository_fails_cleanly(
    short_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = short_tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.chdir(outside)
    for backend, needed in ((GitBackend, GIT), (JjBackend, JJ)):
        if needed is None:
            continue
        result = runner.invoke(build_app(backend), ["--mypy-cmd", MYPY_CMD], catch_exceptions=False)
        assert result.exit_code == EXIT_FAILURE
        assert "Not a" in result.output


def test_non_ascii_commit_messages_are_handled(repo: Repo) -> None:
    """Regression: subprocess output must be decoded as UTF-8, not the locale codepage.

    On Windows the default is cp1252, which raises UnicodeDecodeError inside subprocess's
    reader thread — stdout silently comes back as None and revision resolution explodes.
    """
    if repo.vcs == "jj":
        assert JJ is not None
        run([JJ, "describe", "-r", "main", "-m", "фича: парсер — готово ✓"], cwd=repo.root)
    else:
        assert GIT is not None
        run([GIT, "commit", "-q", "--amend", "-m", "фича: парсер — готово ✓"], cwd=repo.root)

    result = invoke(repo, "-b", "main", "--", "src")

    assert result.exit_code == EXIT_OK, result.output
    assert "error:" not in result.output


def test_version_flag(repo: Repo) -> None:
    result = invoke(repo, "--version")
    assert result.exit_code == EXIT_OK
    assert "mypy-diff" in result.output


@pytest.mark.parametrize("factory", [JjBackend, GitBackend])
def test_help_mentions_the_right_vcs(factory: Callable[..., object]) -> None:
    result = runner.invoke(build_app(factory), ["--help"])  # type: ignore[arg-type]
    assert result.exit_code == EXIT_OK
    assert factory.name in result.output  # type: ignore[attr-defined]


def test_skip_version_check_still_requires_the_tool_to_exist(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    app = build_app(JjBackend if repo.vcs == "jj" else GitBackend)
    result = runner.invoke(app, ["--skip-version-check"], catch_exceptions=False)
    assert result.exit_code == EXIT_FAILURE
    assert "not found on PATH" in result.output
