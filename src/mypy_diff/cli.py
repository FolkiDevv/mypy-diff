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
from contextlib import ExitStack
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.text import Text

from . import MypyDiffError, __version__
from .cache import ResultCache
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
from .project import Project, find_project
from .render import Only, OutputFormat, make_console, render
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
) -> int:
    check_mypy_args(mypy_args, prog=f"mypy-diff-{backend_type.name}")

    with activity:
        activity.step("Checking tools")
        vcs_tool = _resolve_vcs(backend_type.name, skip_version_check)
        backend = backend_type(vcs_tool.command[0], verbose=verbose)
        project = find_project(backend.root(), Path.cwd(), project_dir)

        mypy, mypy_source = resolve_mypy(project.directory, mypy_cmd, project.root)

        if doctor:
            _print_doctor(console, backend_type.name, vcs_tool, mypy, mypy_source, project)
            return EXIT_OK

        activity.step("Resolving revisions")
        base_rev, target_rev = _resolve_revisions(backend, base, to, tip)
        if verbose:
            _print_verbose_header(
                console, backend, base_rev, target_rev, mypy, mypy_source, project
            )
        for warning in backend.warnings:
            errors.print(Text("warning: ", style="bold yellow") + Text(warning))

        cache = ResultCache(cache_dir, enabled=not no_cache)
        environment = _EnvironmentProbe(mypy, project.directory)
        checked = functools.partial(
            _messages_for,
            backend=backend,
            mypy=mypy,
            mypy_args=mypy_args,
            cache=cache,
            environment=environment,
            project=project,
            console=console,
            verbose=verbose,
            activity=activity,
        )
        try:
            base_run = checked(revision=base_rev)
        except BlockingErrors as blocked:
            raise MypyDiffError(
                blocked.message,
                *blocked.details,
                "",
                "mypy cannot analyse the base revision past this error, so there is nothing to",
                "compare against. Pick another base with -b, or leave the file out:",
                "    -- --exclude <path> <targets>",
            ) from blocked

        try:
            target_run = checked(revision=target_rev)
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

    if target_rev.is_working_copy:
        hint = _asymmetry_warning(backend, result, project)
        if hint:
            errors.print(Text("warning: ", style="bold yellow") + Text(hint))

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
            console.print(f"[dim]note: {note}[/dim]")

    if result.has_new or (fail_on_changed and result.changed):
        return EXIT_NEW_ERRORS
    return EXIT_OK


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
) -> tuple[RevisionInfo, RevisionInfo]:
    """Resolve the target first: the base is always derived from it, never from HEAD/@."""
    target = backend.working_copy() if to is None else backend.resolve(to)

    if base is None:
        # The default base is the latest pushed named ancestor, which is already a fork point.
        return backend.default_base(target), target

    return backend.resolve_base(base, target, tip), target


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
) -> _Checked:
    """Run mypy for one revision, using the cache when the revision is immutable."""
    what = "working copy" if revision.is_working_copy else revision.short_id
    activity.step(f"Checking {what}")

    cache_key: str | None = None
    inspected = environment.get() if not revision.is_working_copy else None
    fingerprint = inspected.fingerprint if inspected is not None and cache.enabled else None
    if not revision.is_working_copy and cache.enabled and fingerprint is None and verbose:
        # Without knowing what is installed, a cached base could be from another environment.
        console.print("[dim]result cache off: could not inspect mypy's environment[/dim]")
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
                console.print(f"[dim]cache hit for {revision.short_id}[/dim]")
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
                console.print(f"[dim]checked out {revision.short_id} into {checkout}[/dim]")
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
                    console.print(
                        f"[dim]importing {', '.join(import_dirs)} from the checkout[/dim]"
                    )

        activity.detail(f"Running mypy on {what}")

        # The stack owns the mypy cache directory too: with --no-cache it is a temporary
        # directory that disappears with the worktree.
        run = run_mypy(
            mypy,
            workdir,
            mypy_args
            if checkout is None
            else move_targets_into(mypy_args, project.root, checkout, prefix),
            cache.mypy_cache_dir(revision, stack, project.subdir),
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
        console.print(f"[dim]skipped {run.skipped_lines} non-JSON line(s) from mypy[/dim]")
    if verbose:
        for path in run.dropped_targets:
            console.print(f"[dim]{path} does not exist in {what}: checked without it[/dim]")

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


def _print_verbose_header(
    console: Console,
    backend: VcsBackend,
    base_rev: RevisionInfo,
    target_rev: RevisionInfo,
    mypy: ToolInfo,
    mypy_source: str,
    project: Project,
) -> None:
    console.print(f"[dim]base   {base_rev.commit_id}  {base_rev.label()}[/dim]")
    console.print(f"[dim]target {target_rev.commit_id}  {target_rev.label()}[/dim]")
    command = " ".join(mypy.command)
    console.print(f"[dim]mypy   {command}  ({mypy_source}, {mypy.display_version})[/dim]")
    console.print(f"[dim]in     {project.subdir}[/dim]")
    for command in backend.command_log():
        console.print(f"[dim]$ {command}[/dim]")


def _print_doctor(
    console: Console,
    vcs_name: str,
    vcs_tool: ToolInfo,
    mypy: ToolInfo,
    mypy_source: str,
    project: Project,
) -> None:
    console.print(f"mypy-diff {__version__}")
    console.print(f"  repository root  {project.root}")
    console.print(f"  {'project':<15}  {project.subdir}")
    console.print(f"  {vcs_name:<15}  {vcs_tool.display_version}  ({vcs_tool.command[0]})")
    console.print(f"  {'mypy':<15}  {mypy.display_version}  ({' '.join(mypy.command)})")
    console.print(f"  {'mypy found via':<15}  {mypy_source}")
    console.print(f"  {'python':<15}  {sys.version.split()[0]}")


def jj_main() -> None:
    build_app(JjBackend)()


def git_main() -> None:
    build_app(GitBackend)()
