"""Command line interface.

Both commands are the same program over a different backend, so everything lives in one
factory and the two entry points only pick a backend.
"""

# NOTE: no `from __future__ import annotations` here. Typer resolves the Annotated[...]
# metadata at runtime via inspect, using module globals — stringified annotations would fail
# to see `backend_type`, which is a closure variable of build_app().

import sys
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
from .mypy_exec import resolve_mypy, run_mypy
from .preflight import ToolInfo, find_tool, require_vcs
from .progress import Activity
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

    @app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
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
                mypy_cmd=mypy_cmd,
                cache_dir=cache_dir,
                no_cache=no_cache,
                skip_version_check=skip_version_check,
                doctor=doctor,
                verbose=verbose,
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

        if exit_zero:
            code = EXIT_OK
        raise typer.Exit(code)

    return app


def _run(
    *,
    backend_type: type[GitBackend] | type[JjBackend],
    console: Console,
    activity: Activity,
    mypy_args: tuple[str, ...],
    base: str | None,
    to: str | None,
    tip: bool,
    output_format: OutputFormat,
    only: Only,
    show_unchanged: bool,
    mypy_cmd: str | None,
    cache_dir: Path | None,
    no_cache: bool,
    skip_version_check: bool,
    doctor: bool,
    verbose: bool,
) -> int:
    with activity:
        activity.step("Checking tools")
        vcs_tool = _resolve_vcs(backend_type.name, skip_version_check)
        backend = backend_type(vcs_tool.command[0], verbose=verbose)
        root = backend.root()

        mypy, mypy_source = resolve_mypy(root, mypy_cmd)

        if doctor:
            _print_doctor(console, backend_type.name, vcs_tool, mypy, mypy_source, root)
            return EXIT_OK

        activity.step("Resolving revisions")
        base_rev, target_rev = _resolve_revisions(backend, base, to, tip)
        if verbose:
            _print_verbose_header(console, backend, base_rev, target_rev, mypy, mypy_source)

        cache = ResultCache(cache_dir, enabled=not no_cache)
        base_messages = _messages_for(
            backend, base_rev, mypy, mypy_args, cache, root, console, verbose, activity
        )
        target_messages = _messages_for(
            backend, target_rev, mypy, mypy_args, cache, root, console, verbose, activity
        )

        activity.step("Comparing results")
        result = _diff(backend, base_rev, target_rev, base_messages, target_messages, root)

    render(result, console, output_format, only, show_unchanged)

    # Retention runs once the results are in hand, so a failed run never evicts anything.
    cache.prune()

    if verbose:
        for note in backend.notes:
            console.print(f"[dim]note: {note}[/dim]")

    return EXIT_NEW_ERRORS if result.has_new else EXIT_OK


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
    target = backend.working_copy() if to is None else backend.resolve(to)

    if base is None:
        # The default base is the latest pushed named ancestor, which is already a fork point.
        return backend.default_base(), target

    resolved = backend.resolve(base)
    if not tip:
        resolved = backend.fork_point(resolved)
    return resolved, target


def _messages_for(
    backend: VcsBackend,
    revision: RevisionInfo,
    mypy: ToolInfo,
    mypy_args: tuple[str, ...],
    cache: ResultCache,
    root: Path,
    console: Console,
    verbose: bool,
    activity: Activity,
) -> tuple[MypyMessage, ...]:
    """Run mypy for one revision, using the cache when the revision is immutable."""
    what = "working copy" if revision.is_working_copy else revision.short_id
    activity.step(f"Checking {what}")

    cache_key: str | None = None
    if not revision.is_working_copy:
        cache_key = cache.key(revision.commit_id, mypy.display_version, mypy_args)
        cached = cache.load(cache_key)
        if cached is not None:
            if verbose:
                console.print(f"[dim]cache hit for {revision.short_id}[/dim]")
            activity.detail(f"Checking {what} (cached)")
            return cached

    with ExitStack() as stack:
        if revision.is_working_copy:
            # Run in place: it is faster and it is what the developer actually sees on disk.
            workdir = root
        else:
            activity.detail(f"Checking out {what}")
            workdir = stack.enter_context(backend.worktree(revision))
            if verbose:
                console.print(f"[dim]checked out {revision.short_id} into {workdir}[/dim]")

        activity.detail(f"Running mypy on {what}")

        # The stack owns the mypy cache directory too: with --no-cache it is a temporary
        # directory that disappears with the worktree.
        run = run_mypy(
            mypy,
            workdir,
            mypy_args,
            cache.mypy_cache_dir(revision, stack),
            prog=f"mypy-diff-{backend.name}",
        )

    if verbose and run.skipped_lines:
        console.print(f"[dim]skipped {run.skipped_lines} non-JSON line(s) from mypy[/dim]")

    if cache_key is not None:
        cache.store(cache_key, run.messages)
    return run.messages


def _diff(
    backend: VcsBackend,
    base_rev: RevisionInfo,
    target_rev: RevisionInfo,
    base_messages: tuple[MypyMessage, ...],
    target_messages: tuple[MypyMessage, ...],
    root: Path,
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
                    target_cache[path] = (root / path).read_text(encoding="utf-8", errors="replace")
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
) -> None:
    console.print(f"[dim]base   {base_rev.commit_id}  {base_rev.label()}[/dim]")
    console.print(f"[dim]target {target_rev.commit_id}  {target_rev.label()}[/dim]")
    command = " ".join(mypy.command)
    console.print(f"[dim]mypy   {command}  ({mypy_source}, {mypy.display_version})[/dim]")
    for command in backend.command_log():
        console.print(f"[dim]$ {command}[/dim]")


def _print_doctor(
    console: Console,
    vcs_name: str,
    vcs_tool: ToolInfo,
    mypy: ToolInfo,
    mypy_source: str,
    root: Path,
) -> None:
    console.print(f"mypy-diff {__version__}")
    console.print(f"  repository root  {root}")
    console.print(f"  {vcs_name:<15}  {vcs_tool.display_version}  ({vcs_tool.command[0]})")
    console.print(f"  {'mypy':<15}  {mypy.display_version}  ({' '.join(mypy.command)})")
    console.print(f"  {'mypy found via':<15}  {mypy_source}")
    console.print(f"  {'python':<15}  {sys.version.split()[0]}")


def jj_main() -> None:
    build_app(JjBackend)()


def git_main() -> None:
    build_app(GitBackend)()
