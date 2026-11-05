"""Command line interface.

Both commands are the same program over a different backend, so everything lives in one
factory and the two entry points only pick a backend.
"""

# NOTE: no `from __future__ import annotations` here. Typer resolves the Annotated[...]
# metadata at runtime via inspect, using module globals — stringified annotations would fail
# to see `backend_type`, which is a closure variable of build_app().

import dataclasses
import functools
import hashlib
import os
import re
import sys
import traceback
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.text import Text

from . import MypyDiffError, __version__
from .cache import ResultCache
from .cache import location as cache_location
from .matching import diff_messages
from .models import DiffResult, MypyMessage, RevisionInfo
from .mypy_exec import (
    BlockingErrors,
    Environment,
    check_mypy_args,
    checkout_environment,
    config_files,
    inspect_environment,
    move_targets_into,
    repository_import_dirs,
    resolve_mypy,
    run_mypy,
    working_copy_reads,
)
from .preflight import ToolInfo, find_tool, require_vcs
from .progress import Activity
from .project import Project, configured_targets, find_project, mypy_config_file
from .render import Only, OutputFormat, make_console, render, step_summary, summary_text
from .vcs import GitBackend, JjBackend, VcsBackend

EXIT_OK = 0
EXIT_NEW_ERRORS = 1
EXIT_FAILURE = 2

#: Tools, revisions, base, target, compare — see `_run`.
_TOTAL_STEPS = 5


def build_app(backend_type: type[GitBackend] | type[JjBackend]) -> typer.Typer:
    """Build the CLI for one backend. The only differences are help texts and preflight."""
    app = typer.Typer(
        add_completion=False,
        rich_markup_mode=None,
        help=(
            f"Diff mypy results between two revisions using {backend_type.name}.\n\n"
            "Everything after `--` is passed to mypy unchanged, for both revisions.\n"
            "Exit code: 0 = no new errors, 1 = new errors, 2 = mypy-diff failed."
        ),
    )

    @app.command(
        context_settings={
            "allow_extra_args": True,
            "ignore_unknown_options": True,
            # Without this, `-h` is an unknown option and goes to mypy, which prints its help
            # instead of a report: two full runs that end in "No change in mypy errors."
            "help_option_names": ["-h", "--help"],
        }
    )
    def main(
        ctx: typer.Context,
        base: Annotated[
            str | None, typer.Option("--base", "-b", help=backend_type.base_help)
        ] = None,
        to: Annotated[str | None, typer.Option("--to", help=backend_type.to_help)] = None,
        tip: Annotated[
            bool,
            typer.Option(
                "--tip",
                help="With -b NAME, compare against the branch tip instead of the fork point.",
            ),
        ] = False,
        output_format: Annotated[
            OutputFormat, typer.Option("--format", help="Output format.")
        ] = OutputFormat.PRETTY,
        only: Annotated[
            Only, typer.Option("--only", help="Show only new errors, only fixed ones, or both.")
        ] = Only.ALL,
        show_unchanged: Annotated[
            bool,
            typer.Option(
                "--show-unchanged", help="List unchanged errors instead of counting them."
            ),
        ] = False,
        project_dir: Annotated[
            Path | None,
            typer.Option(
                "--project",
                help=(
                    "Directory to run mypy in, for a project that lives in a subdirectory of "
                    "the repository. Default: the nearest directory above the current one with "
                    "a mypy config, or the repository root."
                ),
            ),
        ] = None,
        mypy_cmd: Annotated[
            str | None,
            typer.Option("--mypy-cmd", help="Command used to run mypy, e.g. 'uv run mypy'."),
        ] = None,
        cache_dir: Annotated[
            Path | None, typer.Option("--cache-dir", help="Where to keep cached results.")
        ] = None,
        no_cache: Annotated[
            bool,
            typer.Option(
                "--no-cache",
                help="Disable both the result cache and mypy's incremental cache for this run.",
            ),
        ] = False,
        clean_cache: Annotated[
            bool,
            typer.Option("--clean-cache", help="Delete the whole mypy-diff cache, then exit."),
        ] = False,
        no_progress: Annotated[
            bool,
            typer.Option("--no-progress", help="Do not show the progress indicator."),
        ] = False,
        exit_zero: Annotated[
            bool, typer.Option("--exit-zero", help="Always exit 0, even when new errors appear.")
        ] = False,
        parallel: Annotated[
            bool | None,
            typer.Option(
                "--parallel/--no-parallel",
                help=(
                    "Run mypy on base and working copy at the same time. "
                    "Default: when there are at least 4 CPUs."
                ),
            ),
        ] = None,
        fail_on_changed: Annotated[
            bool,
            typer.Option(
                "--fail-on-changed",
                help=(
                    "Also exit 1 when a pre-existing error changed its message in place "
                    "(shown as CHANGED)."
                ),
            ),
        ] = False,
        skip_version_check: Annotated[
            bool, typer.Option("--skip-version-check", help="Do not enforce minimum tool versions.")
        ] = False,
        doctor: Annotated[
            bool, typer.Option("--doctor", help="Print detected tools and versions, then exit.")
        ] = False,
        verbose: Annotated[
            bool, typer.Option("--verbose", "-v", help="Show resolved revisions and commands run.")
        ] = False,
        version: Annotated[
            bool, typer.Option("--version", help="Show the mypy-diff version and exit.")
        ] = False,
    ) -> None:
        console = make_console(color=output_format is OutputFormat.PRETTY)
        errors = Console(stderr=True, highlight=False, soft_wrap=True)

        if version:
            console.print(f"mypy-diff {__version__}")
            raise typer.Exit(EXIT_OK)

        if clean_cache:
            # Deliberately before any repository work: clearing the cache must work from
            # anywhere, not just from inside a repository.
            _clean_cache(console, cache_dir)
            raise typer.Exit(EXIT_OK)

        try:
            code = _run(
                backend_type=backend_type,
                console=console,
                errors=errors,
                # The indicator shares the stderr console so rich draws messages above the live
                # region, and never touches stdout, which may be piped JSON.
                activity=Activity(
                    errors,
                    total=_TOTAL_STEPS,
                    enabled=not verbose and not no_progress and errors.is_terminal,
                ),
                mypy_args=tuple(ctx.args),
                base=base,
                to=to,
                tip=tip,
                output_format=output_format,
                only=only,
                show_unchanged=show_unchanged,
                project_dir=project_dir,
                mypy_cmd=mypy_cmd,
                cache_dir=cache_dir,
                no_cache=no_cache,
                skip_version_check=skip_version_check,
                doctor=doctor,
                verbose=verbose,
                fail_on_changed=fail_on_changed,
                parallel=parallel,
            )
        except MypyDiffError as failure:
            # Text(), not markup strings: messages quote mypy output and config snippets, and
            # rich would silently eat anything bracketed — `[return-value]`, `[tool.mypy]`.
            errors.print(Text("error: ", style="bold red") + Text(failure.message))
            for detail in failure.details:
                errors.print(Text(f"  {detail}") if detail else "")
            raise typer.Exit(EXIT_FAILURE) from failure
        except KeyboardInterrupt:  # pragma: no cover - interactive only
            errors.print("[yellow]interrupted[/yellow]")
            raise typer.Exit(EXIT_FAILURE) from None
        except Exception as failure:
            # Exit code 1 means "new errors found", and that is what Python uses for an
            # uncaught exception. A crash must never read as a verdict, so it is mapped to 2.
            errors.print(
                Text("error: ", style="bold red") + Text(f"mypy-diff crashed: {failure!r}")
            )
            if verbose:
                errors.print(Text("".join(traceback.format_exception(failure))))
            else:
                errors.print(Text("  Run again with --verbose for the traceback."))
            raise typer.Exit(EXIT_FAILURE) from failure

        if exit_zero:
            code = EXIT_OK
        raise typer.Exit(code)

    return app


def _run(
    *,
    backend_type: type[GitBackend] | type[JjBackend],
    console: Console,
    errors: Console,
    activity: Activity,
    mypy_args: tuple[str, ...],
    base: str | None,
    to: str | None,
    tip: bool,
    output_format: OutputFormat,
    only: Only,
    show_unchanged: bool,
    project_dir: Path | None,
    mypy_cmd: str | None,
    cache_dir: Path | None,
    no_cache: bool,
    skip_version_check: bool,
    doctor: bool,
    verbose: bool,
    fail_on_changed: bool = False,
    parallel: bool | None = None,
) -> int:
    check_mypy_args(mypy_args, prog=f"mypy-diff-{backend_type.name}")

    with activity:
        activity.step("Checking tools")
        vcs_tool = _resolve_vcs(backend_type.name, skip_version_check)
        backend = backend_type(vcs_tool.command[0], verbose=verbose)
        project = find_project(backend.root(), Path.cwd(), project_dir)

        mypy, mypy_source = resolve_mypy(project.directory, mypy_cmd, project.root)

        if doctor:
            _print_doctor(
                console,
                backend_type.name,
                vcs_tool,
                mypy,
                mypy_source,
                project,
                backend,
                ResultCache(cache_dir, enabled=not no_cache),
                (base, to, tip),
            )
            return EXIT_OK

        activity.step("Resolving revisions")
        base_rev, target_rev, base_reason = _resolve_revisions(backend, base, to, tip)
        if verbose:
            _print_verbose_header(errors, backend, base_rev, target_rev, mypy, mypy_source, project)
        for warning in backend.warnings:
            errors.print(Text("warning: ", style="bold yellow") + Text(warning))

        cache = ResultCache(cache_dir, enabled=not no_cache)
        environment = _EnvironmentProbe(mypy, project.directory)
        where = cache_location(project.root, project.subdir)
        cache.seed(where)
        checked = functools.partial(
            _messages_for,
            backend=backend,
            mypy=mypy,
            mypy_args=mypy_args,
            cache=cache,
            environment=environment,
            project=project,
            console=errors,
            verbose=verbose,
            location=where,
        )

        def check_base(progress: Activity) -> _Checked:
            try:
                return checked(revision=base_rev, role="base", activity=progress)
            except BlockingErrors as blocked:
                raise MypyDiffError(
                    blocked.message,
                    *blocked.details,
                    "",
                    "mypy cannot analyse the base revision past this error, so there is nothing",
                    "to compare against. Pick another base with -b, or leave the file out:",
                    "    -- --exclude <path> <targets>",
                ) from blocked

        def check_target(progress: Activity) -> _Checked:
            return checked(revision=target_rev, role="target", activity=progress)

        identical = _same_tree(backend, base_rev, target_rev)
        try:
            if identical:
                # Nothing differs between the two: check once, and compare that with itself.
                errors.print(
                    Text("warning: ", style="bold yellow")
                    + Text(
                        f"the target holds the same files as the base ({base_rev.short_id}): "
                        "there is nothing to compare. To compare against a branch, pass it "
                        "with -b, e.g. -b origin/main."
                    )
                )
                activity.step(f"Checking {_describe(target_rev)}")
                target_run = check_target(activity)
                base_run = target_run
            elif _runs_in_parallel(parallel, target_rev):
                # The working copy runs in place and the base in its checkout: nothing shared
                # but the CPU. Running them side by side nearly halves the wall-clock time.
                activity.step(f"Checking {_describe(base_rev)} and the working copy")
                quiet = Activity(errors, total=1, enabled=False)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    base_future = pool.submit(check_base, quiet)
                    target_future = pool.submit(check_target, quiet)
                    base_run = base_future.result()
                    target_run = target_future.result()
                activity.step(f"Checked {_describe(base_rev)} and the working copy")
            else:
                base_run = check_base(activity)
                target_run = check_target(activity)
        except BlockingErrors as blocked:
            # mypy reports nothing else once it hits a blocking error, so there is no full
            # report to diff. The blocking errors themselves are the news, though: report them
            # as NEW (and fail), instead of aborting without a single annotation.
            result = DiffResult(
                base=base_rev,
                target=target_rev,
                new=tuple(_repo_paths(blocked.messages, project)),
                incomplete=(
                    f"mypy stopped at a blocking error in {_describe(target_rev)}; "
                    "nothing else could be compared."
                ),
            )
        else:
            _check_targets_exist(base_run, target_run, base_rev, target_rev)
            activity.step("Comparing results")
            result = _diff(
                backend, base_rev, target_rev, base_run.messages, target_run.messages, project
            )

    result = dataclasses.replace(result, base_reason=base_reason)
    if target_rev.is_working_copy:
        hint = _asymmetry_warning(backend, result, project)
        if hint:
            errors.print(Text("warning: ", style="bold yellow") + Text(hint))

    if output_format is OutputFormat.GITHUB:
        _write_step_summary(result, errors)
        # The annotations carry no verdict of their own; the log should.
        errors.print(summary_text(result))

    try:
        render(result, console, output_format, only, show_unchanged)
        console.file.flush()
    except BrokenPipeError:
        # The reader went away (`| head`). The verdict still stands; silence the rest so the
        # interpreter does not fail again flushing stdout at exit.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())

    # Retention runs once the results are in hand, so a failed run never evicts anything.
    cache.prune()

    if verbose:
        for note in backend.notes:
            _trace(errors, f"note: {note}")

    if result.has_new or (fail_on_changed and result.changed):
        return EXIT_NEW_ERRORS
    return EXIT_OK


def _same_tree(backend: VcsBackend, base: RevisionInfo, target: RevisionInfo) -> bool:
    """Whether base and target are the very same files, so one mypy run serves both."""
    return backend.same_tree(base, target)


def _runs_in_parallel(choice: bool | None, target: RevisionInfo) -> bool:
    """Run both sides at once? By default when there are cores to spare: two mypy processes
    also take twice the memory, and a 2-vCPU CI runner gains little from it.

    Only with the working copy as target: two checkouts at once would also mean two
    concurrent `git worktree add` / `jj` operations on one repository.
    """
    if not target.is_working_copy:
        return False
    if choice is not None:
        return choice
    return (os.cpu_count() or 1) >= _PARALLEL_MIN_CPUS


#: Cores needed before both sides run at once by default.
_PARALLEL_MIN_CPUS = 4


def _write_step_summary(result: DiffResult, errors: Console) -> None:
    """Append the report to the job summary GitHub Actions shows on the run page."""
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if not target:
        return
    try:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(step_summary(result))
    except OSError as failure:
        errors.print(Text(f"warning: could not write the job summary: {failure}", style="yellow"))


def _clean_cache(console: Console, cache_dir: Path | None) -> None:
    cache = ResultCache(cache_dir)
    freed = cache.clear()
    console.print(f"Removed {_human_size(freed)} from {cache.directory}")


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"  # pragma: no cover - unreachable, loop returns at GB


def _resolve_vcs(name: str, skip_version_check: bool) -> ToolInfo:
    if not skip_version_check:
        return require_vcs(name)
    tool = find_tool(name)
    if tool is None:
        raise MypyDiffError(f"`{name}` was not found on PATH.")
    return tool


def _resolve_revisions(
    backend: VcsBackend, base: str | None, to: str | None, tip: bool
) -> tuple[RevisionInfo, RevisionInfo, str]:
    """The base, the target, and how the base was chosen.

    The target is resolved first: the base is always derived from it, never from HEAD/@.
    """
    target = backend.working_copy() if to is None else backend.resolve(to)

    if base is None:
        ci_branch = _ci_target_branch()
        if ci_branch:
            # A pull-request job: the branch it merges into is the base, not the "latest pushed
            # ancestor" — in CI everything is pushed, so that would be HEAD itself.
            resolved = backend.resolve_base(backend.remote_branch(ci_branch), target, tip=False)
            return resolved, target, f"fork point with {ci_branch} (GITHUB_BASE_REF)"
        # The default base is the latest pushed named ancestor, which is already a fork point.
        resolved = backend.default_base(target)
        return resolved, target, backend.base_reason

    resolved = backend.resolve_base(base, target, tip)
    return resolved, target, backend.base_reason


def _ci_target_branch() -> str:
    """The branch a GitHub Actions pull-request job merges into, or ""."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return ""
    return os.environ.get("GITHUB_BASE_REF", "").strip()


class _EnvironmentProbe:
    """mypy's environment, inspected on first use: it costs an interpreter start, and a run
    whose revisions are all the working copy never needs it."""

    def __init__(self, mypy: ToolInfo, project: Path) -> None:
        self._mypy = mypy
        self._project = project
        self._done = False
        self._value: Environment | None = None

    def get(self) -> Environment | None:
        """The environment, or None when it could not be inspected."""
        if not self._done:
            self._value = inspect_environment(self._mypy, self._project)
            self._done = True
        return self._value


@dataclasses.dataclass(frozen=True)
class _Checked:
    """The diagnostics of one revision, and the targets missing from its checkout."""

    messages: tuple[MypyMessage, ...]
    dropped_targets: tuple[str, ...] = ()


def _describe(revision: RevisionInfo) -> str:
    return "the working copy" if revision.is_working_copy else f"revision {revision.short_id}"


def _repo_paths(messages: Iterable[MypyMessage], project: Project) -> list[MypyMessage]:
    """mypy prints paths relative to the project directory; report them repository-relative,
    which is what `git show`, CI annotations and the root-level view expect."""
    return [
        dataclasses.replace(message, path=project.repo_path(message.path)) for message in messages
    ]


def _check_targets_exist(
    base_run: _Checked, target_run: _Checked, base: RevisionInfo, target: RevisionInfo
) -> None:
    """A target missing from one checkout is the user's new (or deleted) file; missing from both,
    it is a typo, and leaving it out on both sides would report "No change" for nothing."""
    nowhere = [path for path in target_run.dropped_targets if path in base_run.dropped_targets]
    if nowhere:
        raise MypyDiffError(
            f"`{nowhere[0]}` exists neither in {_describe(base)} nor in {_describe(target)}.",
            "Paths after `--` are relative to the project directory, as they are for mypy.",
        )


#: Codes mypy uses when an import cannot be resolved.
_IMPORT_CODES = frozenset({"import-not-found", "import-untyped", "import"})
_QUOTED = re.compile(r'"([^"]+)"')


def _asymmetry_warning(backend: VcsBackend, result: DiffResult, project: Project) -> str | None:
    """Say so when the base probably could not see files the working copy has.

    The base is checked in a clean checkout. Files git ignores (generated code: `*_pb2.py`,
    `_version.py`) and submodules are not there, so imports of them fail in the base only: an
    import error shows up as FIXED, and every pre-existing error in code using those modules, now
    that their types are known, shows up as NEW. A FIXED import error whose import line is still
    in the working copy, together with ignored Python files or submodules, is the tell.
    """
    still_imported = [
        message
        for message in result.fixed
        if message.code in _IMPORT_CODES and _line_still_present(backend, result, message, project)
    ]
    if not still_imported:
        return None
    ignored = backend.ignored_python_files()
    submodules = (project.root / ".gitmodules").is_file()
    if not ignored and not submodules:
        return None

    example = still_imported[0]
    module = _QUOTED.search(example.message)
    named = f" (`{module.group(1)}` in {example.path}:{example.line})" if module else ""
    missing = []
    if ignored:
        missing.append("files git ignores, such as " + ", ".join(ignored))
    if submodules:
        missing.append("submodules")
    count = len(still_imported)
    return (
        f"{count} import {'error exists' if count == 1 else 'errors exist'} only in the base"
        f"{named}, on import lines your change kept. The base is checked in a clean checkout, "
        f"without {' and '.join(missing)}. Errors in code that uses those modules may be "
        "reported as NEW."
    )


def _line_still_present(
    backend: VcsBackend, result: DiffResult, message: MypyMessage, project: Project
) -> bool:
    base_text = backend.file_at(result.base, message.path)
    if base_text is None:
        return False
    lines = base_text.splitlines()
    if not 1 <= message.line <= len(lines):
        return False
    wanted = lines[message.line - 1].strip()
    try:
        current = (project.root / message.path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return bool(wanted) and any(line.strip() == wanted for line in current.splitlines())


def _outside_config_digest(
    mypy_args: tuple[str, ...], backend: VcsBackend, revision: RevisionInfo, project: Project
) -> str:
    """A digest of `--config-file`s the commit does not pin.

    A config inside the repository that the revision has is taken from its checkout, so the
    commit id already covers it. Any other — outside the repository, or not committed — is read
    as it is now, and editing it must not hand back a base checked with the old settings.
    """
    root = project.root.resolve()
    digest = hashlib.sha256()
    for value in config_files(mypy_args):
        path = (project.directory / value).resolve()
        if path.is_relative_to(root) and not os.path.isabs(value):
            continue  # relative: resolved inside the checkout, pinned by the commit
        in_revision = path.is_relative_to(root) and (
            backend.file_at(revision, path.relative_to(root).as_posix()) is not None
        )
        if in_revision:
            continue  # moved into the checkout
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
        digest.update(str(path).encode("utf-8"))
    return digest.hexdigest()


def _refuse_working_copy_reads(reads: list[Path], revision: RevisionInfo) -> None:
    """Refuse a result for a revision that was partly checked from the working copy.

    mypy read files of the working copy while checking the checkout — whether or not they
    produced errors: an import path still points there. The result would describe neither
    revision, and caching it would keep it.
    """
    if reads:
        raise MypyDiffError(
            f"Checking {_describe(revision)} read files from your working copy, e.g. "
            f"{reads[0].as_posix()}.",
            "mypy found them through a path that points into the working copy, not into",
            "the checkout of that revision, so its result would be wrong. Typically an",
            "absolute `mypy_path` or `files` entry in the mypy config: make it relative to",
            'the config instead, e.g.  mypy_path = "$MYPY_CONFIG_FILE_DIR/lib".',
        )


def _messages_for(
    backend: VcsBackend,
    revision: RevisionInfo,
    mypy: ToolInfo,
    mypy_args: tuple[str, ...],
    cache: ResultCache,
    environment: _EnvironmentProbe,
    project: Project,
    console: Console,
    verbose: bool,
    activity: Activity,
    role: str,
    location: str,
) -> _Checked:
    """Run mypy for one revision, using the cache when the revision is immutable."""
    what = "working copy" if revision.is_working_copy else revision.short_id
    activity.step(f"Checking {what}")

    cache_key: str | None = None
    inspected = environment.get() if not revision.is_working_copy else None
    fingerprint = inspected.fingerprint if inspected is not None and cache.enabled else None
    if not revision.is_working_copy and cache.enabled and fingerprint is None and verbose:
        # Without knowing what is installed, a cached base could be from another environment.
        _trace(console, "result cache off: could not inspect mypy's environment")
    if fingerprint is not None:
        cache_key = cache.key(
            revision.commit_id,
            mypy.display_version,
            mypy_args,
            project.subdir,
            fingerprint + _outside_config_digest(mypy_args, backend, revision, project),
        )
        cached = cache.load_entry(cache_key)
        if cached is not None:
            if verbose:
                _trace(console, f"cache hit for {revision.short_id}")
            activity.detail(f"Checking {what} (cached)")
            return _Checked(*cached)

    run_env: dict[str, str] = {}
    checkout: Path | None = None
    prefix = inspected.prefix if inspected is not None else ""
    with ExitStack() as stack:
        if revision.is_working_copy:
            # Run in place: it is faster and it is what the developer actually sees on disk.
            workdir = project.directory
        else:
            activity.detail(f"Checking out {what}")
            checkout = stack.enter_context(backend.worktree(revision))
            if verbose:
                _trace(console, f"checked out {revision.short_id} into {checkout}")
            workdir = project.directory_in(checkout)
            if not workdir.is_dir():
                raise MypyDiffError(
                    f"The project directory `{project.subdir}` does not exist in {what}.",
                    "Pick a base revision that already has it, e.g. `-b <rev>`.",
                )
            if inspected is not None:
                import_dirs = repository_import_dirs(inspected, project.root)
                run_env = checkout_environment(import_dirs, project.root, checkout)
                if verbose and import_dirs:
                    _trace(console, f"importing {', '.join(import_dirs)} from the checkout")

        activity.detail(f"Running mypy on {what}")

        # The stack owns the mypy cache directory too: with --no-cache it is a temporary
        # directory that disappears with the worktree.
        run = run_mypy(
            mypy,
            workdir,
            mypy_args
            if checkout is None
            else move_targets_into(mypy_args, project.root, checkout, prefix),
            cache.mypy_cache_dir(role, stack, location),
            prog=f"mypy-diff-{backend.name}",
            what=_describe(revision),
            env=run_env,
            # Every file mypy reads for a checkout must come from that checkout; the log of
            # reads is how a dependency that reports no errors gets checked too.
            trace_reads=checkout is not None,
        )
        if checkout is not None:
            read = [*run.read_files, *(message.path for message in run.messages)]
            reads = working_copy_reads(read, workdir, checkout, project.root, prefix)
            _refuse_working_copy_reads(reads, revision)

    if verbose and run.skipped_lines:
        _trace(console, f"skipped {run.skipped_lines} non-JSON line(s) from mypy")
    if verbose:
        for path in run.dropped_targets:
            _trace(console, f"{path} does not exist in {what}: checked without it")

    messages = tuple(_repo_paths(run.messages, project))
    dropped = run.dropped_targets
    if checkout is not None:
        # Absolute targets were moved into the checkout; report them as the user wrote them.
        prefix = str(checkout)
        dropped = tuple(
            str(project.root / Path(path).relative_to(prefix))
            if os.path.isabs(path) and Path(path).is_relative_to(prefix)
            else path
            for path in dropped
        )
    if cache_key is not None:
        cache.store(cache_key, messages, dropped)
    return _Checked(messages, dropped)


def _diff(
    backend: VcsBackend,
    base_rev: RevisionInfo,
    target_rev: RevisionInfo,
    base_messages: tuple[MypyMessage, ...],
    target_messages: tuple[MypyMessage, ...],
    project: Project,
) -> DiffResult:
    """Diff the two message sets, reading file contents lazily for the line-shift tier."""
    base_cache: dict[str, str | None] = {}
    target_cache: dict[str, str | None] = {}

    def read_base(path: str) -> str | None:
        if path not in base_cache:
            base_cache[path] = backend.file_at(base_rev, path)
        return base_cache[path]

    def read_target(path: str) -> str | None:
        if path not in target_cache:
            if target_rev.is_working_copy:
                try:
                    target_cache[path] = (project.root / path).read_text(
                        encoding="utf-8", errors="replace"
                    )
                except OSError:
                    target_cache[path] = None
            else:
                target_cache[path] = backend.file_at(target_rev, path)
        return target_cache[path]

    return diff_messages(
        base_rev, target_rev, base_messages, target_messages, read_base, read_target
    )


def _trace(console: Console, text: str) -> None:
    """A --verbose line, on stderr. Text, not markup: it quotes commit subjects and paths, and
    rich would eat anything bracketed — or fail on a stray closing tag."""
    console.print(Text(text, style="dim"))


def _print_verbose_header(
    console: Console,
    backend: VcsBackend,
    base_rev: RevisionInfo,
    target_rev: RevisionInfo,
    mypy: ToolInfo,
    mypy_source: str,
    project: Project,
) -> None:
    _trace(console, f"base   {base_rev.commit_id}  {base_rev.label()}")
    _trace(console, f"target {target_rev.commit_id}  {target_rev.label()}")
    command = " ".join(mypy.command)
    _trace(console, f"mypy   {command}  ({mypy_source}, {mypy.display_version})")
    _trace(console, f"in     {project.subdir}")
    for command in backend.command_log():
        _trace(console, f"$ {command}")


def _print_doctor(
    console: Console,
    vcs_name: str,
    vcs_tool: ToolInfo,
    mypy: ToolInfo,
    mypy_source: str,
    project: Project,
    backend: VcsBackend,
    cache: ResultCache,
    revisions: tuple[str | None, str | None, bool],
) -> None:
    """Everything a run would use, and what would go wrong with it — without running mypy."""

    def row(name: str, value: str) -> None:
        console.print(Text(f"  {name:<15}  {value}"))

    console.print(f"mypy-diff {__version__}")
    row("repository root", str(project.root))
    row("project", project.subdir)
    row(vcs_name, f"{vcs_tool.display_version}  ({vcs_tool.command[0]})")
    row("mypy", f"{mypy.display_version}  ({' '.join(mypy.command)})")
    row("mypy found via", mypy_source)
    row("python", sys.version.split()[0])

    config = mypy_config_file(project.directory, project.root)
    if config is None:
        row("mypy config", "none in the repository (mypy may use a user-level config)")
    else:
        row("mypy config", config.relative_to(project.root).as_posix())
    targets = configured_targets(config) if config is not None else []
    row("targets", ", ".join(targets) if targets else "none configured: pass them after `--`")

    base, to, tip = revisions
    try:
        base_rev, target_rev, reason = _resolve_revisions(backend, base, to, tip)
    except MypyDiffError as failure:
        row("base", f"cannot be determined: {failure.message}")
    else:
        row("base", f"{base_rev.label()}  ({reason})")
        row("target", target_rev.label())
    if backend.is_shallow():
        row("shallow clone", "yes: a fork point outside the fetched history cannot be found")

    size = cache.size() if cache.enabled else 0
    row("cache", f"{cache.directory}  ({_human_size(size)})" if cache.enabled else "disabled")

    for warning in [*backend.warnings, *_environment_warnings(mypy_source, project)]:
        console.print(Text("  warning: ", style="bold yellow") + Text(warning))


def _environment_warnings(mypy_source: str, project: Project) -> list[str]:
    """An activated virtualenv wins over the project's own: say so when they differ."""
    active = os.environ.get("VIRTUAL_ENV")
    if mypy_source != "active virtualenv" or not active:
        return []
    for directory in (project.directory, project.root):
        own = directory / ".venv"
        if own.is_dir() and own.resolve() != Path(active).resolve():
            return [
                f"mypy comes from the activated virtualenv {active}, not from the project's own "
                f"{own}. If that is another project's environment, deactivate it or pass "
                "--mypy-cmd."
            ]
    return []


def jj_main() -> None:
    build_app(JjBackend)()


def git_main() -> None:
    build_app(GitBackend)()
