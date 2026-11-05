"""Locate mypy, run it against a directory, and turn its JSON output into messages.

We deliberately run the *project's* mypy rather than one of our own: mypy-diff is installed
as an isolated uv tool, so its own environment has none of the project's dependencies or
stubs and would report a flood of spurious import/attribute errors.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import MypyDiffError
from .models import MypyMessage, Severity
from .preflight import MIN_MYPY, ToolInfo, probe, require

#: Flags we always add. `--output=json` is the only format we parse.
_FORCED_ARGS = (
    "--output=json",
    "--no-error-summary",
    "--no-pretty",
    "--no-color-output",
)

_BIN_DIR = "Scripts" if sys.platform == "win32" else "bin"
_MYPY_EXE = "mypy.exe" if sys.platform == "win32" else "mypy"


@dataclass(frozen=True, slots=True)
class MypyRun:
    """The outcome of one mypy invocation."""

    messages: tuple[MypyMessage, ...]
    returncode: int
    stdout: str
    stderr: str
    skipped_lines: int


def resolve_mypy(
    project: Path, explicit: str | None = None, repo_root: Path | None = None
) -> tuple[ToolInfo, str]:
    """Find a usable mypy. Returns the tool and a human-readable description of how.

    Order: --mypy-cmd, $MYPY_DIFF_MYPY, the active virtualenv, the project's .venv,
    `uv run --project`, the repository root's .venv (when the project lives in a
    subdirectory), then PATH.
    """
    for command, source in _mypy_candidates(project, explicit, repo_root):
        tool = probe("mypy", command)
        if tool is not None:
            return require("mypy", tool, MIN_MYPY), source

    raise MypyDiffError(
        "Could not find a usable `mypy`.",
        "mypy-diff runs the project's own mypy so that its dependencies and stubs resolve.",
        "Point it at one explicitly:",
        "  --mypy-cmd 'uv run mypy'      (or set MYPY_DIFF_MYPY)",
        "or install mypy into the project: `uv add --dev mypy`",
    )


def split_command(raw: str) -> tuple[str, ...]:
    """Split a user-supplied command string into argv.

    `shlex.split` defaults to POSIX rules, where a backslash is an escape character — that
    silently destroys any Windows path (``C:\\tools\\mypy.exe`` becomes ``C:toolsmypy.exe``).
    So a bare path to an existing file is taken verbatim, and anything else is split with the
    backslash treated as an ordinary character on Windows.
    """
    stripped = raw.strip()
    if not stripped:
        return ()
    unquoted = stripped.strip('"')
    # An unquoted path that exists is unambiguous — never try to tokenize it.
    if Path(unquoted).is_file():
        return (unquoted,)
    if os.name == "nt":
        return tuple(token.strip('"') for token in shlex.split(stripped, posix=False))
    return tuple(shlex.split(stripped))


def anchor_command(command: tuple[str, ...], project: Path) -> tuple[str, ...]:
    """Pin a user-supplied command to the working copy, so both revisions run the same mypy.

    The base revision is checked with the temporary checkout as working directory, so anything
    the command resolves relative to its working directory would resolve differently there:

    * a relative executable (``.venv/bin/mypy``) would not exist at all;
    * ``uv run mypy`` would discover the checkout's pyproject.toml and build a fresh environment
      from the *base* revision's lock file — for a workspace member, often one without mypy.

    So a relative path is made absolute against the current directory, where the user typed it,
    and ``uv run`` is pointed at the working copy's project explicitly.
    """
    if not command:
        return command
    first, *rest = command
    separators = tuple(sep for sep in (os.sep, os.altsep) if sep)
    if any(sep in first for sep in separators) and not os.path.isabs(first):
        first = os.path.abspath(first)
    if (
        Path(first).stem.lower() == "uv"
        and rest[:1] == ["run"]
        and not any(_names_project(argument) for argument in rest)
    ):
        rest = ["run", "--project", str(project), *rest[1:]]
    return (first, *rest)


def _names_project(argument: str) -> bool:
    return argument in ("--project", "--directory") or argument.startswith(
        ("--project=", "--directory=")
    )


def _mypy_candidates(
    project: Path, explicit: str | None, repo_root: Path | None = None
) -> list[tuple[tuple[str, ...], str]]:
    candidates: list[tuple[tuple[str, ...], str]] = []

    if explicit:
        candidates.append((anchor_command(split_command(explicit), project), "--mypy-cmd"))
        # An explicit command is a promise; do not silently fall back past it.
        return candidates

    from_env = os.environ.get("MYPY_DIFF_MYPY")
    if from_env:
        candidates.append((anchor_command(split_command(from_env), project), "$MYPY_DIFF_MYPY"))
        return candidates

    active_venv = os.environ.get("VIRTUAL_ENV")
    if active_venv:
        candidates.append(((str(Path(active_venv) / _BIN_DIR / _MYPY_EXE),), "active virtualenv"))

    candidates.append(((str(project / ".venv" / _BIN_DIR / _MYPY_EXE),), "project .venv"))

    uv = shutil.which("uv")
    if uv is not None and (project / "pyproject.toml").exists():
        # uv picks the right environment itself: the workspace's for a workspace member, the
        # project's own otherwise.
        candidates.append(((uv, "run", "--project", str(project), "mypy"), "uv run --project"))

    if repo_root is not None and repo_root != project:
        # Without uv, a workspace member's environment is the root .venv. It goes after uv
        # because the root may just as well be an unrelated project with other dependencies.
        candidates.append(
            ((str(repo_root / ".venv" / _BIN_DIR / _MYPY_EXE),), "repository root .venv")
        )

    on_path = shutil.which("mypy")
    if on_path is not None:
        candidates.append(((on_path,), "PATH"))

    return candidates


def run_mypy(
    mypy: ToolInfo,
    workdir: Path,
    mypy_args: tuple[str, ...],
    cache_dir: Path,
    prog: str = "mypy-diff",
    what: str = "the working copy",
) -> MypyRun:
    """Run mypy inside ``workdir`` and parse its diagnostics.

    ``cache_dir`` is per-revision so the two runs never invalidate each other's incremental
    cache — and so neither of them touches the developer's own ``.mypy_cache``.

    ``prog`` only names the command in error hints, so they show the entry point the user
    actually typed; ``what`` names the revision, so a failure says which side it came from.

    Anything that is not a trustworthy, complete report raises instead of returning: an empty
    result would be diffed — and possibly cached — as "this revision has no errors", which turns
    every error of the other side into NEW or FIXED.
    """
    argv = [
        *mypy.command,
        *_FORCED_ARGS,
        f"--cache-dir={cache_dir}",
        *_with_non_interactive(mypy_args),
    ]
    try:
        completed = subprocess.run(
            argv,
            cwd=workdir,
            capture_output=True,
            check=False,
            # mypy must never wait for an answer: `--install-types` prompts on stdin.
            stdin=subprocess.DEVNULL,
            # mypy's JSON output is UTF-8 (type names and messages can carry any character).
            # Decoding with the locale codepage would break on non-ASCII identifiers.
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "MYPY_FORCE_COLOR": "0", "NO_COLOR": "1"},
        )
    except OSError as failure:
        raise MypyDiffError(
            f"Could not run mypy on {what}: {failure.strerror or failure}.",
            f"Command: {' '.join(mypy.command)}",
        ) from failure

    if completed.returncode not in (0, 1):
        raise _fatal_error(completed, prog, what)

    messages, skipped = parse_json_output(completed.stdout, workdir)
    if not messages and completed.stderr.strip():
        # With --non-interactive mypy reports its diagnostics on stderr, not stdout.
        from_stderr, _ = parse_json_output(completed.stderr, workdir)
        if from_stderr:
            messages = from_stderr

    if completed.returncode == 1 and not messages:
        # Exit code 1 means "found errors". Without a single parsed diagnostic the report is
        # missing, not empty: a wrapper that could not find mypy (`poetry run mypy` in a fresh
        # worktree), a mode that prints no JSON, a refused environment.
        raise MypyDiffError(
            f"mypy exited with code 1 on {what} but reported no diagnostics.",
            *_output_lines(completed),
            f"Command: {' '.join(argv)}",
        )

    return MypyRun(
        messages=messages,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        skipped_lines=skipped,
    )


#: Arguments after `--` that make mypy print something other than a report. Passing them through
#: would turn two full runs into "No change in mypy errors."
_INFORMATIONAL_ARGS = frozenset({"-h", "--help", "-V", "--version"})


def check_mypy_args(mypy_args: tuple[str, ...], prog: str = "mypy-diff") -> None:
    """Reject arguments that would make mypy skip the analysis."""
    for argument in mypy_args:
        if argument in _INFORMATIONAL_ARGS:
            raise MypyDiffError(
                f"`{argument}` after `--` goes to mypy, which would then print no report.",
                f"For mypy-diff's own help run `{prog} --help`; for mypy's, run `mypy --help`.",
            )


def _with_non_interactive(mypy_args: tuple[str, ...]) -> tuple[str, ...]:
    """`--install-types` asks for confirmation on stdin; there is nobody there to answer."""
    if "--install-types" in mypy_args and "--non-interactive" not in mypy_args:
        return (*mypy_args, "--non-interactive")
    return mypy_args


def _output_lines(completed: subprocess.CompletedProcess[str]) -> list[str]:
    """Everything mypy printed, stderr first. Both streams: either may hold the explanation."""
    return [
        line.rstrip()
        for stream in (completed.stderr, completed.stdout)
        for line in (stream or "").splitlines()
        if line.strip()
    ]


#: Ways mypy says "you did not tell me what to analyse". The first is what it actually prints
#: for a bare invocation ("Missing target module, package, files, or command."); the others
#: cover configs that name paths which do not exist.
_NO_TARGET_MARKERS = ("missing target", "cannot read file", "no files")


def _fatal_error(
    completed: subprocess.CompletedProcess[str], prog: str, what: str = "the working copy"
) -> MypyDiffError:
    if completed.returncode < 0:
        try:
            name = signal.Signals(-completed.returncode).name
        except ValueError:
            name = f"signal {-completed.returncode}"
        return MypyDiffError(
            f"mypy was killed by {name} while checking {what}.",
            *(["It most likely ran out of memory."] if name == "SIGKILL" else []),
            *_output_lines(completed),
        )

    details = _output_lines(completed)

    if any(marker in line.lower() for line in details for marker in _NO_TARGET_MARKERS):
        # mypy answers this one with a full usage dump, which buries the actual problem.
        # Drop it and say what to do instead.
        return MypyDiffError(
            "mypy has no files to check.",
            "Your mypy config sets no `files`, `packages` or `modules`, so mypy does not",
            "know what to analyse.",
            "",
            "Either pass the paths explicitly:",
            f"    {prog} -- src tests",
            "",
            "or set them in your mypy config, e.g. in pyproject.toml:",
            "    [tool.mypy]",
            '    files = ["src", "tests"]',
        )

    # A blocking error (a syntax error, a duplicate module) arrives as JSON on stdout. Show it
    # the way mypy would print it, not as a raw JSON object.
    rendered = [_render_line(line) for line in details]
    return MypyDiffError(f"mypy failed with exit code {completed.returncode} on {what}.", *rendered)


def _render_line(line: str) -> str:
    try:
        raw = json.loads(line)
    except json.JSONDecodeError:
        return line
    if not isinstance(raw, dict) or "message" not in raw:
        return line
    position = f"{raw.get('file') or '?'}:{raw.get('line') or 0}"
    code = f"  [{raw['code']}]" if raw.get("code") else ""
    return f"{position}: {raw.get('severity') or 'error'}: {raw['message']}{code}"


def parse_json_output(stdout: str, workdir: Path) -> tuple[tuple[MypyMessage, ...], int]:
    """Parse mypy's `--output=json` stream.

    Each diagnostic is one JSON object per line. Non-JSON lines (a stray summary, a warning
    printed by a plugin) are counted and skipped rather than crashing the run.
    """
    messages: list[MypyMessage] = []
    pending_notes: list[str] = []
    skipped = 0

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if not isinstance(raw, dict) or "message" not in raw:
            skipped += 1
            continue

        severity = Severity(raw.get("severity") or "error")
        if severity is Severity.NOTE and messages:
            # A note is elaboration on the diagnostic above it, not a diagnostic of its own.
            pending_notes.append(str(raw["message"]))
            messages[-1] = _with_notes(messages[-1], tuple(pending_notes))
            continue

        pending_notes = []
        messages.append(
            MypyMessage(
                path=_normalize_path(raw.get("file") or "", workdir),
                line=int(raw.get("line") or 0),
                column=_optional_int(raw.get("column")),
                severity=severity,
                message=str(raw["message"]),
                code=raw.get("code") or None,
                hint=str(raw.get("hint") or ""),
            )
        )

    return tuple(messages), skipped


def _with_notes(message: MypyMessage, notes: tuple[str, ...]) -> MypyMessage:
    return MypyMessage(
        path=message.path,
        line=message.line,
        column=message.column,
        severity=message.severity,
        message=message.message,
        code=message.code,
        hint=message.hint,
        notes=notes,
    )


def _optional_int(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _normalize_path(raw: str, workdir: Path) -> str:
    """Make paths comparable between a working-copy run and a temporary-worktree run.

    Both runs must produce the same key for the same file, so everything becomes a
    POSIX-style path relative to the directory mypy was invoked in.
    """
    if not raw:
        return ""
    path = Path(raw)
    if path.is_absolute():
        try:
            path = path.relative_to(workdir)
        except ValueError:
            try:
                path = Path(os.path.relpath(path, workdir))
            except ValueError:
                # Different drive on Windows — nothing sensible to relativize against.
                return path.as_posix()
    return path.as_posix()


# ------------------------------------------------------------------ environment fingerprint

#: Printed by mypy's own interpreter: everything installed there decides how imports resolve —
#: dependencies, stub packages, plugins — and the Python version is mypy's default target.
_FINGERPRINT_SCRIPT = (
    "import importlib.metadata as m, json, sys\n"
    "print(json.dumps({"
    "'python': list(sys.version_info[:3]), 'prefix': sys.prefix, 'path': sys.path, "
    "'dists': sorted(str(d.metadata['Name']) + '==' + str(d.version) for d in m.distributions())"
    "}))"
)

#: Launcher options whose value may be a path that happens to end in `mypy`
#: (`uv run --project ~/src/mypy mypy`): that value is not the program.
_LAUNCHER_VALUE_OPTIONS = frozenset(
    {"--project", "--directory", "--with", "--python", "--from", "--env-file", "-C", "-p"}
)

#: Where mypy looks for a user-level config when the project has none (mypy/defaults.py).
_USER_CONFIG_FILES = ("~/.config/mypy/config", "~/.mypy.ini")


def environment_fingerprint(mypy: ToolInfo, project: Path) -> str | None:
    """A digest of everything outside the revision that changes what mypy reports.

    The result cache stores a base revision's diagnostics. They stay valid only as long as mypy
    would still produce them: after installing a stub package or upgrading a dependency, the
    cached base and a freshly checked target disagree, and pre-existing errors come out NEW.
    Folding this digest into the cache key turns such a change into a cache miss instead.

    None when mypy's interpreter cannot be asked what is installed. A key without that part
    could not tell two environments apart, so the caller must not use the result cache then.
    """
    report = _interpreter_report(mypy.command, project)
    if not report:
        return None
    parts: dict[str, object] = {
        "command": list(mypy.command),
        "version": mypy.raw_version,
        "environment": report,
        "MYPYPATH": os.environ.get("MYPYPATH", ""),
        "user_config": _user_config_digest(),
    }
    payload = json.dumps(parts, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _interpreter_command(command: tuple[str, ...]) -> tuple[str, ...] | None:
    """Turn the command that runs mypy into one that runs Python in the same environment.

    Arguments after mypy (``mypy --strict``) say nothing about the environment and are dropped.
    """
    for index in range(len(command) - 1):
        if command[index : index + 2] == ("-m", "mypy"):
            # `python -m mypy ...`, `uv run python -m mypy ...`.
            return (*command[:index], "-c", _FINGERPRINT_SCRIPT)

    position = next(
        (
            i
            for i, token in enumerate(command)
            # An option is never the program, not even `--project=/tools/mypy`.
            if not token.startswith("-")
            and Path(token).stem.lower() == "mypy"
            and (i == 0 or command[i - 1] not in _LAUNCHER_VALUE_OPTIONS)
        ),
        None,
    )
    if position is None:
        return None
    if position > 0:
        # A launcher such as `uv run mypy` or `poetry run mypy`: same launcher, other program.
        return (*command[:position], "python", "-c", _FINGERPRINT_SCRIPT)

    executable = Path(command[0])
    if executable.parent == Path("."):
        # A bare name is found on PATH, exactly as running it would.
        found = shutil.which(command[0])
        if found is None:
            return None
        executable = Path(found)
    # A script in an environment's bin/ (Scripts/ on Windows) sits next to its interpreter.
    for name in ("python.exe", "python") if sys.platform == "win32" else ("python", "python3"):
        interpreter = executable.parent / name
        if interpreter.is_file():
            return (str(interpreter), "-c", _FINGERPRINT_SCRIPT)
    shebang = _shebang(executable)
    if shebang:
        return (*shebang, "-c", _FINGERPRINT_SCRIPT)
    return None


def _shebang(executable: Path) -> tuple[str, ...]:
    try:
        with executable.open("rb") as handle:
            first = handle.readline(1024)
    except OSError:
        return ()
    if not first.startswith(b"#!"):
        return ()
    return tuple(first[2:].decode("utf-8", errors="replace").split())


def _interpreter_report(command: tuple[str, ...], project: Path) -> str:
    probe_command = _interpreter_command(command)
    if probe_command is None:
        return ""
    try:
        completed = subprocess.run(
            probe_command,
            cwd=project,
            capture_output=True,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=60,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def _user_config_digest() -> str:
    digest = hashlib.sha256()
    candidates = list(_USER_CONFIG_FILES)
    if os.environ.get("XDG_CONFIG_HOME"):
        candidates.insert(0, os.path.join(os.environ["XDG_CONFIG_HOME"], "mypy/config"))
    for candidate in candidates:
        path = Path(os.path.expanduser(candidate))
        try:
            digest.update(path.read_bytes())
        except OSError:
            continue
        digest.update(str(path).encode("utf-8"))
    return digest.hexdigest()
