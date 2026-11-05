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

from mypy_diff.cli import EXIT_FAILURE, EXIT_NEW_ERRORS, EXIT_OK, build_app, detect_backend, main
from mypy_diff.vcs import GitBackend, JjBackend

from .conftest import GIT, JJ, SUBPROJECT, Repo, run

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


def test_doctor_shows_what_a_run_would_use(repo: Repo, short_tmp_path: Path) -> None:
    """The config mypy reads, what it would check, the base and why, and the cache."""
    result = invoke(repo, "--doctor", "-b", "main", cache=short_tmp_path / "cache")
    assert "mypy config      pyproject.toml" in result.stdout
    # The fixture's config names no files: exactly the "no files to check" trap.
    assert "none configured: pass them after `--`" in result.stdout
    assert "(fork point with main)" in result.stdout
    assert str(short_tmp_path / "cache") in result.stdout


def test_doctor_warns_about_a_foreign_virtualenv(
    repo: Repo, short_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An activated virtualenv wins over the project's own .venv; say so when they differ."""
    own = repo.root / ".venv" / ("Scripts" if sys.platform == "win32" else "bin")
    own.mkdir(parents=True)
    other = short_tmp_path / "other-venv"
    binaries = other / ("Scripts" if sys.platform == "win32" else "bin")
    binaries.mkdir(parents=True)
    mypy = binaries / ("mypy.exe" if sys.platform == "win32" else "mypy")
    if sys.platform == "win32":
        pytest.skip("needs an executable script")
    mypy.write_text(f'#!/bin/sh\nexec {sys.executable} -m mypy "$@"\n', encoding="utf-8")
    mypy.chmod(0o755)
    monkeypatch.setenv("VIRTUAL_ENV", str(other))
    monkeypatch.delenv("MYPY_DIFF_MYPY", raising=False)

    app = build_app(JjBackend if repo.vcs == "jj" else GitBackend)
    result = runner.invoke(app, ["--doctor"], catch_exceptions=False)

    assert "mypy found via   active virtualenv" in result.stdout
    assert "not from the project's own" in result.stdout


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


def commit_as_main(repo: Repo, message: str) -> None:
    """Record the working copy as a new commit and point `main` at it."""
    if repo.vcs == "jj":
        assert JJ is not None
        run([JJ, "describe", "-m", message], cwd=repo.root)
        run([JJ, "bookmark", "set", "main", "-r", "@"], cwd=repo.root)
        run([JJ, "new"], cwd=repo.root)
    else:
        assert GIT is not None
        run([GIT, "add", "-A"], cwd=repo.root)
        run([GIT, "commit", "-q", "-m", message], cwd=repo.root)


REDEFINED_SOURCE = """\
def handler() -> None: ...


def handler() -> None: ...
"""


def test_a_redefinition_pushed_down_by_an_insertion_is_not_new(repo: Repo) -> None:
    """mypy's text names the first definition's line ('already defined on line 1').

    Inserting one line above changes that number, and the pre-existing error used to come out
    as one NEW plus one FIXED.
    """
    repo.write("src/redefined.py", REDEFINED_SOURCE)
    commit_as_main(repo, "legacy redefinition")
    repo.write("src/redefined.py", "import os\n" + REDEFINED_SOURCE)

    result = invoke(repo, "-b", "main", "--", "src")

    assert result.exit_code == EXIT_OK, result.output
    assert "No change in mypy errors." in result.output


def test_a_repository_root_that_is_a_package_can_be_checked_out(repo: Repo) -> None:
    """mypy names modules after the directories above them when they are packages.

    The temporary checkout used to be called `mypy-diff-<pid>-0` (git) or `tree` (jj): an
    invalid package name, or a different one than in the working copy.
    """
    repo.write("__init__.py", "")
    repo.write("src/__init__.py", "")
    commit_as_main(repo, "the repository is a package")

    result = invoke(repo, "-b", "main", "--to", "main", "--", "src")

    assert result.exit_code == EXIT_OK, result.output
    assert "No change in mypy errors." in result.output


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_absolute_paths_through_a_symlinked_temp_dir_still_match(
    repo: Repo, short_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """macOS's temp dir is /var/..., a symlink to /private/var/....

    mypy prints absolute paths through the physical directory, so they did not relativise
    against the unresolved checkout path, and every error of the base looked fixed (and its
    twin in the working copy new).
    """
    real = short_tmp_path / "real-tmp"
    real.mkdir()
    link = short_tmp_path / "link-tmp"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(tempfile, "tempdir", str(link))
    repo.write("src/legacy.py", "# padding\n" + repo.read("src/legacy.py"))

    result = invoke(repo, "-b", "main", "--", "--show-absolute-path", "src")

    assert result.exit_code == EXIT_OK, result.output
    assert "1 unchanged error" in result.output


# ------------------------------------------------------------------ formats


def test_json_format_is_parseable(repo: Repo) -> None:
    repo.write("src/clean.py", BROKEN_SOURCE)
    result = invoke(repo, "--format", "json", "--", "src")
    payload = json.loads(result.output)
    assert payload["summary"]["new"] == 1
    assert payload["new"][0]["path"] == "src/clean.py"
    assert payload["new"][0]["code"] == "return-value"


def test_verbose_output_stays_off_stdout(repo: Repo) -> None:
    """`--format json -v` used to put the header and `$ git ...` lines into the JSON."""
    repo.write("src/clean.py", BROKEN_SOURCE)
    result = invoke(repo, "-v", "--format", "json", "--", "src")
    assert json.loads(result.stdout)["summary"]["new"] == 1
    assert "base " in result.stderr


def test_forced_colours_never_reach_machine_output(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CI often exports FORCE_COLOR; it used to put escape codes into JSON and plain output."""
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("TTY_COMPATIBLE", "1")
    repo.write("src/clean.py", BROKEN_SOURCE)
    assert json.loads(invoke(repo, "--format", "json", "--", "src").stdout)["summary"]["new"] == 1
    assert "\x1b" not in invoke(repo, "--format", "plain", "--", "src").stdout


def test_columns_are_one_based_like_mypy_prints_them(repo: Repo) -> None:
    repo.write("src/clean.py", BROKEN_SOURCE)
    # `    return 42`: mypy reports column 12, counting from 1.
    assert "src/clean.py:6:12: error:" in invoke(repo, "--", "src").output


def test_the_header_says_how_the_base_was_chosen(repo: Repo) -> None:
    header = invoke(repo, "-b", "main", "--", "src").stdout.splitlines()[0]
    assert "(fork point with main)" in header


def test_github_format_writes_the_job_summary_and_a_verdict(
    repo: Repo, short_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary = short_tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    repo.write("src/clean.py", BROKEN_SOURCE)

    result = invoke(repo, "--format", "github", "--", "src")

    text = summary.read_text(encoding="utf-8")
    assert "## mypy-diff" in text
    assert "src/clean.py:6:12" in text
    assert "Found 1 new" in result.stderr
    assert result.stdout.startswith("::error file=src/clean.py,line=6")


@pytest.mark.skipif(GIT is None, reason="git is not installed")
def test_a_pull_request_job_compares_against_its_target_branch(
    git_repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In CI everything is pushed, so the default base used to be HEAD itself: "No change"."""
    assert GIT is not None
    monkeypatch.chdir(git_repo.root)
    run([GIT, "checkout", "-q", "-b", "feature"], cwd=git_repo.root)
    git_repo.write("src/clean.py", BROKEN_SOURCE)
    run([GIT, "commit", "-qam", "feature adds an error"], cwd=git_repo.root)
    run([GIT, "push", "-q", "-u", "origin", "feature"], cwd=git_repo.root)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_BASE_REF", "main")

    result = invoke(git_repo, "--", "src")

    assert result.exit_code == EXIT_NEW_ERRORS, result.output
    assert "+ NEW (1)" in result.output
    assert "GITHUB_BASE_REF" in result.output.splitlines()[0]


def test_github_format_emits_annotations(repo: Repo) -> None:
    repo.write("src/clean.py", BROKEN_SOURCE)
    result = invoke(repo, "--format", "github", "--", "src")
    assert "::error file=src/clean.py,line=6" in result.output


@pytest.mark.parametrize("output_format", ["sarif", "gitlab"])
def test_report_formats_write_pure_json_and_log_the_verdict(repo: Repo, output_format: str) -> None:
    """The report goes to a file the CI system picks up; the verdict to the job log."""
    repo.write("src/clean.py", BROKEN_SOURCE)
    result = invoke(repo, "--format", output_format, "--", "src")

    assert result.exit_code == EXIT_NEW_ERRORS
    report = json.loads(result.stdout)
    issues = report["runs"][0]["results"] if output_format == "sarif" else report
    assert len(issues) == 1
    assert "1 new" in result.stderr


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


@pytest.mark.skipif(GIT is None, reason="git is not installed")
def test_base_is_derived_from_the_target_not_from_head(
    git_repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reviewing a branch from main: `-b main --to feature`.

    main fixed the legacy error after feature forked. The fork point used to be computed
    against HEAD (main), so feature's untouched legacy error came out NEW.
    """
    assert GIT is not None
    monkeypatch.chdir(git_repo.root)
    run([GIT, "checkout", "-q", "-b", "feature"], cwd=git_repo.root)
    git_repo.write("src/clean.py", BROKEN_SOURCE)
    run([GIT, "commit", "-qam", "feature adds an error"], cwd=git_repo.root)
    run([GIT, "checkout", "-q", "main"], cwd=git_repo.root)
    git_repo.write("src/legacy.py", "def broken() -> str:\n    return 'fixed on main'\n")
    run([GIT, "commit", "-qam", "main fixes legacy"], cwd=git_repo.root)

    for args in (["-b", "main", "--to", "feature"], ["--to", "feature"]):
        result = invoke(git_repo, *args, "--", "src")
        assert result.exit_code == EXIT_NEW_ERRORS, result.output
        assert "+ NEW (1)" in result.output
        assert "src/clean.py:6" in result.output
        assert "FIXED" not in result.output
        assert "1 unchanged error" in result.output


@pytest.mark.skipif(GIT is None, reason="git is not installed")
def test_merging_main_into_a_pushed_branch_reports_nothing_of_mains(
    git_repo: Repo, short_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The base used to be the merge's first parent: main's fixes and errors came out as yours."""
    assert GIT is not None
    monkeypatch.chdir(git_repo.root)
    root = git_repo.root
    run([GIT, "checkout", "-q", "-b", "feature"], cwd=root)
    git_repo.write("src/clean.py", BROKEN_SOURCE)
    run([GIT, "commit", "-qam", "feature adds an error"], cwd=root)
    run([GIT, "push", "-q", "-u", "origin", "feature"], cwd=root)

    other = short_tmp_path / "colleague"
    remote = str(root.parent / "remote.git")
    run([GIT, "clone", "-q", "--branch", "main", remote, str(other)], cwd=short_tmp_path)
    run([GIT, "config", "user.email", "c@example.com"], cwd=other)
    run([GIT, "config", "user.name", "Colleague"], cwd=other)
    (other / "src" / "legacy.py").write_text(
        "def broken() -> str:\n    return 'ok'\n", encoding="utf-8"
    )
    (other / "src" / "colleague.py").write_text(
        "def c() -> int:\n    return 'c'\n", encoding="utf-8"
    )
    run([GIT, "add", "-A"], cwd=other)
    run([GIT, "commit", "-qm", "colleague fixes legacy, adds an error"], cwd=other)
    run([GIT, "push", "-q", "origin", "main"], cwd=other)

    run([GIT, "fetch", "-q"], cwd=root)
    run([GIT, "merge", "-q", "--no-ff", "-m", "merge main", "origin/main"], cwd=root)

    result = invoke(git_repo, "--", "src")

    assert result.exit_code == EXIT_OK, result.output
    assert "No change in mypy errors." in result.output


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
    """Base and working copy must not share one incremental cache directory; across runs each
    side keeps its own, whatever commit it checks."""
    cache = short_tmp_path / "cache"
    repo.write("src/clean.py", BROKEN_SOURCE)
    invoke(repo, "--", "src", cache=cache)

    projects = list((cache / "mypy-cache").iterdir())
    assert len(projects) == 1, projects
    assert sorted(p.name for p in projects[0].iterdir()) == ["base", "target"]


def test_an_unchanged_working_copy_is_checked_once(repo: Repo) -> None:
    """Base and target are the same tree: one mypy run, and a word that nothing is compared.

    In CI with everything pushed, the default base is HEAD itself, which used to cost two full
    runs and say nothing but "No change".
    """
    result = invoke(repo, "-v", "--", "src")
    assert result.exit_code == EXIT_OK, result.output
    assert "the same files as the base" in result.stderr
    assert "checked out" not in result.stderr
    assert "1 unchanged error" in result.stdout


@pytest.mark.parametrize("flag", ["--parallel", "--no-parallel"])
def test_parallel_and_sequential_runs_agree(repo: Repo, flag: str) -> None:
    repo.write("src/clean.py", BROKEN_SOURCE)
    repo.write("src/legacy.py", "# padding\n" + repo.read("src/legacy.py"))
    result = invoke(repo, flag, "--", "src")
    assert result.exit_code == EXIT_NEW_ERRORS, result.output
    assert "+ NEW (1)" in result.stdout
    assert "1 unchanged error" in result.stdout


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


def test_progress_never_reaches_machine_output(repo: Repo) -> None:
    """The indicator lives on stderr so `--format json` can be piped safely.

    CliRunner is not a terminal, so it is off here anyway; this pins the contract that stdout
    stays parseable regardless.
    """
    repo.write("src/clean.py", BROKEN_SOURCE)

    with_progress = invoke(repo, "--format", "json", "--", "src")
    without_progress = invoke(repo, "--no-progress", "--format", "json", "--", "src")

    left, right = json.loads(with_progress.output), json.loads(without_progress.output)
    # Not the whole payload: jj re-snapshots the working copy on every invocation, so its
    # commit_id legitimately differs between two runs. The diagnostics are the invariant.
    for key in ("new", "fixed", "summary"):
        assert left[key] == right[key]
    assert with_progress.exit_code == without_progress.exit_code == EXIT_NEW_ERRORS


def test_no_progress_flag_is_accepted(repo: Repo) -> None:
    result = invoke(repo, "--no-progress", "--", "src")
    assert result.exit_code == EXIT_OK
    assert "No change in mypy errors." in result.output


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


# ------------------------------------------------------------------ project in a subdirectory


@pytest.fixture(params=["git", "jj"])
def monorepo(request: pytest.FixtureRequest) -> Repo:
    """A repository whose Python project, config included, lives in `backend/`."""
    if request.param == "git" and GIT is None:
        pytest.skip("git is not installed")
    if request.param == "jj" and JJ is None:
        pytest.skip("jj is not installed")
    return request.getfixturevalue(f"{request.param}_monorepo")  # type: ignore[no-any-return]


@pytest.mark.parametrize("where", [SUBPROJECT, f"{SUBPROJECT}/src"])
def test_project_in_a_subdirectory_is_found_from_inside_it(
    monorepo: Repo, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    """Regression: mypy used to run from the repository root and never saw backend's config."""
    monorepo.write(f"{SUBPROJECT}/src/clean.py", BROKEN_SOURCE)
    monkeypatch.chdir(monorepo.root / where)

    result = invoke(monorepo)

    assert result.exit_code == EXIT_NEW_ERRORS, result.output
    assert "+ NEW (1)" in result.output
    assert f"{SUBPROJECT}/src/clean.py:6" in result.output
    assert "1 unchanged error" in result.output


def test_project_option_selects_the_subdirectory(
    monorepo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monorepo.write(f"{SUBPROJECT}/src/clean.py", BROKEN_SOURCE)
    monkeypatch.chdir(monorepo.root)

    result = invoke(monorepo, "--project", SUBPROJECT, "--format", "github")

    assert result.exit_code == EXIT_NEW_ERRORS, result.output
    # Annotations must carry repository-relative paths, or GitHub cannot place them.
    assert f"::error file={SUBPROJECT}/src/clean.py,line=6" in result.output


def test_subproject_line_shifts_are_matched(
    monorepo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shift tier reads both revisions' files, which needs repository-relative paths."""
    legacy = f"{SUBPROJECT}/src/legacy.py"
    monorepo.write(legacy, "# padding\n" * 40 + monorepo.read(legacy))
    monkeypatch.chdir(monorepo.root / SUBPROJECT)

    result = invoke(monorepo, "-b", "main", "--to", "main")
    assert result.exit_code == EXIT_OK, result.output

    result = invoke(monorepo)
    assert result.exit_code == EXIT_OK, result.output
    assert "+ NEW" not in result.output
    assert "1 unchanged error" in result.output


def test_subproject_doctor_names_the_project(
    monorepo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(monorepo.root / SUBPROJECT)
    result = invoke(monorepo, "--doctor")
    assert result.exit_code == EXIT_OK
    assert f"project          {SUBPROJECT}" in result.output


def test_subproject_results_are_cached_apart_from_the_root(
    monorepo: Repo, monkeypatch: pytest.MonkeyPatch, short_tmp_path: Path
) -> None:
    """One commit, two projects: a cached root run must not answer for backend/."""
    cache = short_tmp_path / "cache"
    monkeypatch.chdir(monorepo.root)
    invoke(monorepo, "--", f"{SUBPROJECT}/src/clean.py", cache=cache)

    monkeypatch.chdir(monorepo.root / SUBPROJECT)
    result = invoke(monorepo, "-v", cache=cache)

    assert "cache hit" not in result.output
    assert "1 unchanged error" in result.output


def test_project_missing_from_the_base_fails_cleanly(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.write("newproject/pyproject.toml", '[tool.mypy]\nfiles = ["."]\n')
    repo.write("newproject/mod.py", "x: int = 1\n")
    monkeypatch.chdir(repo.root / "newproject")

    result = invoke(repo)

    assert result.exit_code == EXIT_FAILURE
    assert "`newproject` does not exist" in result.output


def test_project_option_must_be_a_directory_in_the_repository(
    repo: Repo, short_tmp_path: Path
) -> None:
    missing = invoke(repo, "--project", "no-such-dir")
    assert missing.exit_code == EXIT_FAILURE
    assert "not a directory" in missing.output

    outside = invoke(repo, "--project", str(short_tmp_path))
    assert outside.exit_code == EXIT_FAILURE
    assert "outside the repository" in outside.output


def test_short_help_flag_is_ours_not_mypys(repo: Repo) -> None:
    """`-h` used to be forwarded to mypy: two full runs, then 'No change in mypy errors.'"""
    app = build_app(JjBackend if repo.vcs == "jj" else GitBackend)
    result = runner.invoke(app, ["-h"], catch_exceptions=False)
    assert result.exit_code == EXIT_OK
    assert "--base" in result.output
    assert "No change" not in result.output


@pytest.mark.parametrize("argument", ["--help", "--version"])
def test_mypy_arguments_that_print_no_report_are_rejected(repo: Repo, argument: str) -> None:
    result = invoke(repo, "--", "src", argument)
    assert result.exit_code == EXIT_FAILURE
    assert "would then print no report" in result.output


def test_a_crash_exits_with_the_failure_code_not_one(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit code 1 means 'new errors found'; an uncaught exception must not look like that."""

    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("mypy_diff.cli.diff_messages", explode)
    result = invoke(repo, "--exit-zero", "--", "src")
    assert result.exit_code == EXIT_FAILURE
    assert "mypy-diff crashed" in result.output


def test_an_environment_change_misses_the_cache(
    repo: Repo, short_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A base cached under one environment must not be compared with a target checked under
    another: after installing stubs, pre-existing errors used to come out NEW."""
    cache = short_tmp_path / "cache"
    monkeypatch.delenv("MYPYPATH", raising=False)
    repo.write("src/extra.py", "extra: int = 1\n")  # a change, so the base is checked too
    invoke(repo, "--", "src", cache=cache)
    assert "cache hit" in invoke(repo, "-v", "--", "src", cache=cache).output

    stubs = short_tmp_path / "stubs"
    stubs.mkdir()
    monkeypatch.setenv("MYPYPATH", str(stubs))
    assert "cache hit" not in invoke(repo, "-v", "--", "src", cache=cache).output


def test_an_uninspectable_environment_turns_the_result_cache_off(
    repo: Repo, short_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key that cannot tell environments apart would hand back a stale base."""
    monkeypatch.setattr("mypy_diff.cli.inspect_environment", lambda *args: None)
    cache = short_tmp_path / "cache"
    invoke(repo, "--", "src", cache=cache)
    assert list(cache.glob("*.json")) == []
    assert "cache hit" not in invoke(repo, "-v", "--", "src", cache=cache).output


# ------------------------------------------------------------------ targets and blocking errors


def test_a_new_file_named_after_the_separator_is_new_not_an_error(repo: Repo) -> None:
    """The file the user asks about does not exist in the base yet: it has no errors there."""
    repo.write("src/fresh.py", "def f() -> int:\n    return 'x'\n")
    result = invoke(repo, "--", "src/legacy.py", "src/fresh.py")
    assert result.exit_code == EXIT_NEW_ERRORS, result.output
    assert "+ NEW (1)" in result.output
    assert "src/fresh.py:2" in result.output
    assert "1 unchanged error" in result.output


def test_only_a_new_file_named_compares_against_nothing(repo: Repo) -> None:
    repo.write("src/fresh.py", "def f() -> int:\n    return 'x'\n")
    result = invoke(repo, "--", "src/fresh.py")
    assert result.exit_code == EXIT_NEW_ERRORS, result.output
    assert "Found 1 new, 0 fixed, 0 unchanged" in result.output


def test_a_path_missing_from_both_revisions_is_an_error(repo: Repo) -> None:
    """Leaving a typo out on both sides would report "No change" for nothing."""
    result = invoke(repo, "--", "src/legacy.py", "src/typo.py")
    assert result.exit_code == EXIT_FAILURE
    assert "`src/typo.py` exists neither in" in result.output


def test_a_syntax_error_in_the_working_copy_is_reported_as_new(repo: Repo) -> None:
    """mypy reports nothing else past a blocking error, but the error itself is the news."""
    repo.write("src/clean.py", "def broken(:\n")
    result = invoke(repo, "--", "src")
    assert result.exit_code == EXIT_NEW_ERRORS, result.output
    assert "+ NEW (1)" in result.output
    assert "src/clean.py:1" in result.output
    assert "[syntax]" in result.output
    assert "blocking error in the working copy" in result.output
    assert "FIXED" not in result.output


def test_a_syntax_error_in_the_base_is_explained(repo: Repo) -> None:
    repo.write("src/clean.py", "def broken(:\n")
    commit_as_main(repo, "broken base")
    repo.write("src/clean.py", "def fixed() -> None: ...\n")
    result = invoke(repo, "-b", "main", "--", "src")
    assert result.exit_code == EXIT_FAILURE
    assert "blocking error" in result.output
    assert "src/clean.py:1" in result.output
    assert "--exclude" in result.output


# ------------------------------------------------------------------ imports from the working copy

SHARED_STR = "def value() -> str:\n    return 's'\n"
USES_SHARED = "from shared import value\n\n\ndef count() -> int:\n    return value()\n"


def test_the_base_imports_from_its_own_checkout(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absolute PYTHONPATH (or an editable install) points into the working copy.

    The base used to import `shared` from there, saw the working copy's new return type, and
    the error the change caused was reported as "unchanged".
    """
    # A typed package (PEP 561), like an editable workspace member.
    repo.write("lib/shared/__init__.py", SHARED_STR)
    repo.write("lib/shared/py.typed", "")
    repo.write("src/uses.py", USES_SHARED)
    commit_as_main(repo, "uses shared")
    repo.write("lib/shared/__init__.py", "def value() -> bytes:\n    return b's'\n")
    monkeypatch.setenv("PYTHONPATH", str(repo.root / "lib"))

    result = invoke(repo, "-b", "main", "--", "src")

    assert "~ CHANGED (1)" in result.output, result.output
    assert 'got "bytes"' in result.output
    assert 'was: Incompatible return value type (got "str"' in result.output


def test_a_base_check_that_reads_the_working_copy_is_refused(repo: Repo) -> None:
    """An absolute mypy_path in the config cannot be moved; the result must not be trusted.

    The dependency it reaches reports no errors of its own, so only the log of files mypy read
    shows that the base saw the working copy's version of it.
    """
    repo.write("lib/shared.py", "def value() -> int:\n    return 1\n")
    repo.write("src/uses.py", USES_SHARED)
    config = repo.read("pyproject.toml")
    repo.write("pyproject.toml", config + f'mypy_path = "{(repo.root / "lib").as_posix()}"\n')
    commit_as_main(repo, "absolute mypy_path")
    repo.write("lib/shared.py", "def value() -> str:\n    return 's'\n")

    result = invoke(repo, "-b", "main", "--", "src")

    assert result.exit_code == EXIT_FAILURE, result.output
    assert "read files from your working copy" in result.output
    assert "lib/shared.py" in result.output


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_an_absolute_target_through_a_symlink_still_matches(
    repo: Repo, short_tmp_path: Path
) -> None:
    """macOS's temp dir is /var/..., a symlink to /private/var/.... mypy prints a target as it
    was given, while the working copy runs in the resolved directory: the paths used to come out
    as '../../../var/...', and the pre-existing error as NEW plus FIXED."""
    link = short_tmp_path / "link"
    link.symlink_to(repo.root, target_is_directory=True)
    repo.write("src/legacy.py", "# padding\n" + repo.read("src/legacy.py"))

    result = invoke(repo, "--", str(link / "src"))

    assert result.exit_code == EXIT_OK, result.output
    assert "1 unchanged error" in result.output


def test_installed_packages_in_a_venv_inside_the_repository_are_fine(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """uv keeps `.venv` in the project. Its site-packages are read by both runs on purpose;
    they are not working-copy sources the base must not see."""
    site = repo.root / ".venv" / "lib" / "python3.12" / "site-packages"
    repo.write(".gitignore", ".venv/\n")
    repo.write(".venv/lib/python3.12/site-packages/typedpkg/__init__.py", SHARED_STR)
    repo.write(".venv/lib/python3.12/site-packages/typedpkg/py.typed", "")
    repo.write("src/uses.py", USES_SHARED.replace("shared", "typedpkg"))
    commit_as_main(repo, "uses an installed package")
    repo.write("src/clean.py", BROKEN_SOURCE)
    monkeypatch.setenv("PYTHONPATH", str(site))

    result = invoke(repo, "-b", "main", "--", "src")

    assert result.exit_code == EXIT_NEW_ERRORS, result.output
    assert "+ NEW (1)" in result.output
    assert "2 unchanged errors" in result.output


def test_an_absolute_config_file_is_the_revisions_own(repo: Repo) -> None:
    """`--config-file /repo/mypy.ini` must mean what `--config-file mypy.ini` means: the base is
    checked with the config of its own commit, not with today's."""
    repo.write("mypy.ini", "[mypy]\npython_version = 3.12\n")
    repo.write("src/untyped.py", "def legacy(x):\n    return x\n")
    commit_as_main(repo, "a lenient config")
    repo.write("mypy.ini", "[mypy]\npython_version = 3.12\ndisallow_untyped_defs = True\n")

    relative = invoke(repo, "-b", "main", "--", "--config-file", "mypy.ini", "src")
    absolute = invoke(repo, "-b", "main", "--", "--config-file", str(repo.root / "mypy.ini"), "src")

    assert "+ NEW (1)" in relative.output, relative.output
    assert "src/untyped.py:1" in relative.output
    assert absolute.output == relative.output


def test_an_outside_config_file_is_part_of_the_cache_key(repo: Repo, short_tmp_path: Path) -> None:
    """A config outside the repository is read as it is now; editing it must not hand back a
    base that was checked with the old settings."""
    cache = short_tmp_path / "cache"
    config = short_tmp_path / "shared.ini"
    config.write_text("[mypy]\npython_version = 3.12\n", encoding="utf-8")
    arguments = ("--", "--config-file", str(config), "src")
    repo.write("src/extra.py", "extra: int = 1\n")  # a change, so the base is checked too
    invoke(repo, *arguments, cache=cache)
    assert "cache hit" in invoke(repo, "-v", *arguments, cache=cache).output

    config.write_text("[mypy]\npython_version = 3.12\nstrict = True\n", encoding="utf-8")
    assert "cache hit" not in invoke(repo, "-v", *arguments, cache=cache).output


def test_an_absolute_target_is_checked_in_the_checkout(repo: Repo) -> None:
    """`-- $PWD/src` used to make the base check the working copy's files."""
    repo.write("src/legacy.py", "# padding\n" + repo.read("src/legacy.py"))
    result = invoke(repo, "--", str(repo.root / "src"))
    assert result.exit_code == EXIT_OK, result.output
    assert "1 unchanged error" in result.output


@pytest.mark.skipif(GIT is None, reason="git is not installed")
def test_a_generated_module_the_base_lacks_is_pointed_out(
    git_repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ignored, generated code is missing from the clean checkout of the base."""
    monkeypatch.chdir(git_repo.root)
    git_repo.write(".gitignore", "gen_pb2.py\n")
    git_repo.write("src/app.py", 'import gen_pb2\n\nlabel = "v" + gen_pb2.VERSION\n')
    commit_as_main(git_repo, "uses generated code")
    git_repo.write("src/gen_pb2.py", "VERSION: int = 1\n")
    git_repo.write("src/extra.py", "extra: int = 1\n")  # some work, so the base is checked

    result = invoke(git_repo, "-b", "main", "--", "src")

    assert "import error exists only in the base" in result.output
    assert "src/gen_pb2.py" in result.output


# ------------------------------------------------------------------ changed errors


def test_a_reworded_error_is_changed_and_does_not_fail(repo: Repo) -> None:
    """legacy.py's error names the declared return type; widening it rewords the old error."""
    repo.write("src/legacy.py", 'def broken() -> float:\n    return "already wrong"\n')
    result = invoke(repo, "--", "src")
    assert result.exit_code == EXIT_OK, result.output
    assert "~ CHANGED (1)" in result.output
    assert 'expected "float"' in result.output
    assert 'was: Incompatible return value type (got "str", expected "int")' in result.output

    strict = invoke(repo, "--fail-on-changed", "--", "src")
    assert strict.exit_code == EXIT_NEW_ERRORS


def test_version_flag(repo: Repo) -> None:
    result = invoke(repo, "--version")
    assert result.exit_code == EXIT_OK
    assert "mypy-diff" in result.output


@pytest.mark.parametrize("factory", [JjBackend, GitBackend])
def test_help_mentions_the_right_vcs(factory: Callable[..., object]) -> None:
    result = runner.invoke(build_app(factory), ["--help"])  # type: ignore[arg-type]
    assert result.exit_code == EXIT_OK
    assert factory.name in result.output  # type: ignore[attr-defined]


def test_help_shows_examples_and_exit_codes() -> None:
    result = runner.invoke(build_app(GitBackend, prog="mypy-diff"), ["--help"])
    assert result.exit_code == EXIT_OK
    assert "mypy-diff -b main --tip" in result.output
    assert "Exit codes:" in result.output
    assert "2  mypy-diff itself failed" in result.output


def test_tip_without_a_branch_is_an_error(repo: Repo) -> None:
    """It used to be ignored: the run compared with the fork point and said nothing."""
    result = invoke(repo, "--tip", "--", "src")
    assert result.exit_code == EXIT_FAILURE
    assert "--tip needs a branch" in result.stderr


# ------------------------------------------------------------------ [tool.mypy-diff]


def configure(repo: Repo, table: str) -> None:
    """Add a [tool.mypy-diff] table to the fixture's pyproject.toml in the working copy."""
    repo.write("pyproject.toml", repo.read("pyproject.toml") + "\n[tool.mypy-diff]\n" + table)


def test_the_config_supplies_the_base_and_the_targets(repo: Repo) -> None:
    configure(repo, 'base = "main"\nargs = ["src"]\n')
    repo.write("src/clean.py", BROKEN_SOURCE)

    result = invoke(repo)  # no `--`: the targets come from the config

    assert result.exit_code == EXIT_NEW_ERRORS, result.output
    assert "+ NEW (1)" in result.stdout
    assert "fork point with main, from [tool.mypy-diff]" in result.stdout.splitlines()[0]


def test_the_command_line_beats_the_config(repo: Repo) -> None:
    configure(repo, 'format = "json"\nonly = "new"\n')
    repo.write("src/legacy.py", "def broken() -> str:\n    return 'now correct'\n")

    from_config = invoke(repo, "--", "src")
    assert json.loads(from_config.stdout)["only"] == "new"

    overridden = invoke(repo, "--format", "plain", "--", "src")
    assert not overridden.stdout.lstrip().startswith("{")
    # What the command line leaves alone still comes from the file.
    assert "FIXED" not in overridden.stdout


def test_a_paired_flag_turns_a_configured_option_off(repo: Repo) -> None:
    configure(repo, "fail-on-changed = true\n")
    repo.write("src/legacy.py", 'def broken() -> float:\n    return "already wrong"\n')

    assert invoke(repo, "--", "src").exit_code == EXIT_NEW_ERRORS
    assert invoke(repo, "--no-fail-on-changed", "--", "src").exit_code == EXIT_OK


def test_a_config_mistake_fails_the_run(repo: Repo) -> None:
    configure(repo, 'bse = "main"\n')
    result = invoke(repo, "--", "src")
    assert result.exit_code == EXIT_FAILURE
    assert "unknown key 'bse'" in result.stderr


def test_the_config_mypy_command_is_used(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MYPY_DIFF_MYPY", raising=False)
    configure(repo, f"mypy-cmd = '\"{sys.executable}\" -m mypy'\n")

    app = build_app(JjBackend if repo.vcs == "jj" else GitBackend)
    result = runner.invoke(app, ["--doctor"], catch_exceptions=False)

    assert "mypy found via   mypy-cmd in [tool.mypy-diff]" in result.stdout
    assert "settings         pyproject.toml  [tool.mypy-diff]" in result.stdout


def test_on_a_pull_request_its_target_beats_the_configured_base(
    git_repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PR into a release branch must not be compared with the configured main."""
    monkeypatch.chdir(git_repo.root)
    configure(git_repo, 'base = "no-such-branch"\n')
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_BASE_REF", "main")

    result = invoke(git_repo, "--", "src")

    assert result.exit_code == EXIT_OK, result.output
    assert "GITHUB_BASE_REF" in result.stdout.splitlines()[0]


# ------------------------------------------------------------------ mypy-diff


@pytest.mark.parametrize(
    ("markers", "jj_installed", "expected"),
    [
        ([".git"], True, GitBackend),
        ([".jj", ".git"], True, JjBackend),
        ([".jj", ".git"], False, GitBackend),
        ([".jj"], False, JjBackend),
        ([], True, GitBackend),
    ],
)
def test_mypy_diff_picks_the_backend_of_the_repository(
    short_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    markers: list[str],
    jj_installed: bool,
    expected: type[object],
) -> None:
    for marker in markers:
        (short_tmp_path / marker).mkdir()
    nested = short_tmp_path / "src" / "pkg"
    nested.mkdir(parents=True)
    monkeypatch.setattr(shutil, "which", lambda name: f"/bin/{name}" if jj_installed else None)

    assert detect_backend(nested) is expected


def test_the_nearest_repository_wins(short_tmp_path: Path) -> None:
    """A git repository checked out inside a jj workspace is a git repository."""
    (short_tmp_path / ".jj").mkdir()
    inner = short_tmp_path / "vendor" / "lib"
    (inner / ".git").mkdir(parents=True)

    assert detect_backend(inner) is GitBackend


def test_mypy_diff_runs_with_the_detected_backend(
    git_repo: Repo, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(git_repo.root)
    monkeypatch.setattr(sys, "argv", ["mypy-diff", "--mypy-cmd", MYPY_CMD, "--doctor"])

    with pytest.raises(SystemExit) as exited:
        main()

    assert exited.value.code == EXIT_OK
    assert "git" in capsys.readouterr().out


def test_skip_version_check_still_requires_the_tool_to_exist(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    app = build_app(JjBackend if repo.vcs == "jj" else GitBackend)
    result = runner.invoke(app, ["--skip-version-check"], catch_exceptions=False)
    assert result.exit_code == EXIT_FAILURE
    assert "not found on PATH" in result.output
