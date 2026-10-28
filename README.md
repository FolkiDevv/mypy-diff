# mypy-diff

[![CI](https://github.com/FolkiDevv/mypy-diff/actions/workflows/ci.yml/badge.svg)](https://github.com/FolkiDevv/mypy-diff/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/mypy-diff.svg)](https://pypi.org/project/mypy-diff/)
[![Python](https://img.shields.io/pypi/pyversions/mypy-diff.svg)](https://pypi.org/project/mypy-diff/)

Show only the mypy errors **your** work introduced.

On any non-trivial codebase `mypy` prints a wall of hundreds of errors, and the one question
that matters — *which of these did I just add?* — is impossible to answer. `mypy-diff` runs mypy
on two revisions and prints the difference, in mypy's own output format.

```
mypy-diff  base: zqxlrpvw  a1b2c3d4  main@origin  "feat: add parser"
             to: working copy (uncommitted)

+ NEW (2)
+ src/parser.py:42:5: error: Incompatible return value type (got "str", expected "int")  [return-value]
+ src/parser.py:88:1: error: Missing return statement  [return]

- FIXED (1)
- src/legacy.py:10:9: error: Name "foo" is not defined  [name-defined]

  147 unchanged errors in 31 files

Found 2 new, 1 fixed, 147 unchanged  (net +1)
```

Two commands, same behaviour, different version control:

| Command | For |
|---|---|
| `mypy-diff-jj` | [Jujutsu](https://jj-vcs.github.io/jj/) workspaces (jj on its git backend) |
| `mypy-diff-git` | plain git repositories |

## Install

```bash
uv tool install mypy-diff
```

That installs **both** commands. Or with pipx:

```bash
pipx install mypy-diff
```

<details>
<summary>Installing from source</summary>

Straight from the repository:

```bash
uv tool install --from git+https://github.com/FolkiDevv/mypy-diff.git mypy-diff
```

Or from a local checkout — add `--force` to overwrite an existing install:

```bash
uv tool install --from . mypy-diff
```

</details>

Requirements:

- Python 3.12+
- **mypy 1.11+** in the project you are checking — that is the first release with
  `--output=json`, the only format `mypy-diff` parses
- **jj 0.28+** (for `mypy-diff-jj`) or **git 2.25+** (for `mypy-diff-git`)

`mypy-diff` deliberately does *not* bundle mypy. It is installed as an isolated uv tool, so its
own environment has none of your project's dependencies or stubs; running its own mypy would
report a flood of spurious `import-untyped` and `attr-defined` errors. It finds your project's
mypy instead — see [Which mypy gets run](#which-mypy-gets-run).

Check your setup at any time:

```bash
mypy-diff-jj --doctor
```

## Usage

```bash
mypy-diff-jj                      # default base, check whatever the mypy config lists
mypy-diff-jj -- src tests         # everything after `--` goes to mypy, for both revisions
mypy-diff-git -b main -- src      # compare against where you branched off main
```

### What gets checked

mypy-diff never invents targets — it passes them straight through, so the same rules apply as for
running `mypy` yourself. Either your mypy config names them:

```toml
[tool.mypy]
files = ["src", "tests"]
```

…or you give them after `--`. With neither, mypy has nothing to analyse and the run stops with a
message telling you so.

Targets are deliberately not defaulted to `.`: the base revision is checked in a clean worktree
while the target is your working copy, so `.` would drag `.venv` into one side only and report the
whole virtualenv as new errors.

### Projects in a subdirectory

In a monorepo the Python code and its `pyproject.toml` often live below the repository root,
say in `backend/`. mypy reads its config, and resolves the `files` it lists, relative to the
directory it runs in, so mypy-diff runs it in the **project directory** for both revisions:

- run mypy-diff from anywhere inside `backend/` and it picks the nearest directory above you
  with a mypy config (`mypy.ini`, `.mypy.ini`, `pyproject.toml` or `setup.cfg`), stopping at
  the repository root;
- or name it explicitly: `mypy-diff-git --project backend`. A relative path starts at the
  current directory, like any other path argument, so this example is run from the repository
  root.

Targets after `--` are then relative to that directory, as they would be for mypy itself.
Reported paths are always relative to the repository root (`backend/src/app.py`), so
`--format github` annotations land on the right files.

### Choosing revisions

**Base** (`-b`, `--base`) defaults to *the latest named ancestor that has been pushed to git*.
That is almost always the point you branched from, so the diff shows exactly your unpushed work.

| | `mypy-diff-jj` | `mypy-diff-git` |
|---|---|---|
| default base | nearest ancestor of `@` with a remote bookmark | parent of your oldest unpushed commit |
| falls back to | local bookmark or tag → `trunk()` → `@-` | `origin/HEAD` → `main`/`master` → `HEAD~1` |
| `-b NAME` accepts | a bookmark name or any revset | a branch, tag or any git revision |

`-b NAME` compares against the **fork point** — the common ancestor of `NAME` and where you are.
This is what you want: if `main` moved ahead after you branched, unrelated fixes that landed on
`main` would otherwise show up as "new errors" in your diff. Pass `--tip` for the literal branch
head.

**Target** (`--to`) defaults to the working copy *on disk*, including uncommitted changes. That
run happens in place, so it is fast and reflects exactly what you are looking at. Any other
revision is checked out into a temporary worktree (`jj workspace add` / `git worktree add`) that
is always removed afterwards, even if the run fails.

### Options

```
  -b, --base REV         Base revision. Default: latest pushed named ancestor.
      --to REV           Target revision. Default: the working copy on disk.
      --tip              With -b NAME, use the branch tip instead of the fork point.
      --format FMT       pretty | plain | json | github            [pretty]
      --only WHAT        new | fixed | all                         [all]
      --show-unchanged   List unchanged errors instead of counting them.
      --project DIR      Directory to run mypy in. Default: nearest mypy config above you.
      --mypy-cmd TEXT    Command used to run mypy, e.g. 'uv run mypy'.
      --cache-dir PATH   Where to keep cached results.
      --no-cache         Disable both caches for this run; leave nothing behind.
      --clean-cache      Delete the whole mypy-diff cache, then exit.
      --no-progress      Do not show the progress indicator.
      --exit-zero        Always exit 0, even when new errors appear.
      --skip-version-check
      --doctor           Print detected tools and versions, then exit.
  -v, --verbose          Show resolved revisions and every command run.
      --version
```

**Exit codes:** `0` no new errors · `1` new errors found · `2` mypy-diff itself failed.

### Progress

Two mypy runs take a while, so an interactive run shows which step it is on and how long it has
been going:

```
⠹ [3/5] Running mypy on 4c37600f 0:00:12
```

It is indeterminate on purpose — mypy emits its whole report at the end of the build, so a filling
bar would be fiction. The line erases itself when the run finishes.

The indicator goes to **stderr** and only when stderr is a terminal, so `--format json > out.json`
and pipes into `jq` stay clean. It is also off under `--verbose` (which already narrates each step)
and under `--no-progress`.

### In CI

```yaml
- run: uv tool install mypy-diff
- run: mypy-diff-git -b origin/main --format github -- src
```

The job fails only when the branch *adds* type errors; the pre-existing backlog is ignored, and
`--format github` turns each new error into an inline annotation on the pull request.

## How matching works

Comparing diagnostics by `(file, line, column, message)` is useless in practice — adding one
import at the top of a file shifts every line below it, and every error in that file would be
reported as simultaneously fixed and new. Matching therefore runs in three tiers, per file:

1. **Exact** — same file, position and text.
2. **Shifted** — the file's contents in both revisions are diffed with `difflib`, producing a
   base-line → target-line map. A diagnostic whose line merely moved still matches. Lines that
   were *themselves* edited get no mapping, so a real change is never hidden.
3. **By content** — same file, same error code and message text, position ignored. Repeated
   diagnostics pair up in order of appearance.

Whatever is left over is genuinely **new** (only in the target) or **fixed** (only in the base).

## Which mypy gets run

First match wins, and `--doctor` tells you which one was used:

1. `--mypy-cmd 'uv run mypy'`
2. `$MYPY_DIFF_MYPY`
3. the active virtualenv (`$VIRTUAL_ENV`)
4. the project's `.venv`
5. `uv run --project <project> mypy` (for a uv workspace member, uv uses the workspace's
   environment)
6. the repository root's `.venv`, when the project is in a subdirectory and uv is not available
7. `mypy` on `PATH`

Both revisions are always checked with the same mypy and the same arguments, and each revision
gets its own incremental mypy cache — so the two runs never invalidate each other, and your own
`.mypy_cache` is left untouched.

## Caching

Two caches live side by side, in a per-user directory from
[platformdirs](https://pypi.org/project/platformdirs/):

| Platform | Location |
|---|---|
| Windows | `%LOCALAPPDATA%\mypy-diff\Cache` |
| macOS | `~/Library/Caches/mypy-diff` |
| Linux | `~/.cache/mypy-diff` |

- **Result cache** — the diagnostics of one run, as `<hash>.json`. Keyed by commit id, mypy
  version and mypy arguments. Written for any revision that resolves to a concrete commit; a
  commit is immutable, so the entry stays valid forever and repeat runs skip that analysis
  entirely. The dirty working copy is never cached. Tiny — a few hundred bytes per entry.
- **Incremental mypy cache** — what mypy itself keeps, one directory per revision, handed over
  with `--cache-dir`. This keeps the two runs from invalidating each other and leaves your own
  `.mypy_cache` untouched. This is the part with real weight: **roughly 2 MB per revision.**

Both backends key on a git SHA, so `mypy-diff-jj` and `mypy-diff-git` share one cache.

### Retention

By default the cache holds the **last 3 runs** and older entries are evicted automatically after
each run (least-recently-used first). Since one run touches at most two revisions — the base and
the target — that works out to at most 6 revision directories, or roughly 12 MB.

Override it with an environment variable:

```bash
MYPY_DIFF_CACHE_KEEP=10   # keep the last 10 runs
MYPY_DIFF_CACHE_KEEP=0    # keep everything, never evict
```

### Clearing it

```bash
mypy-diff-jj --clean-cache
```

Prints how much was freed and exits. It works from anywhere — no repository needed — and honours
`--cache-dir`. Only files this tool created are removed, so pointing `--cache-dir` at a directory
of your own will not wipe its other contents.

`--no-cache` disables **both** caches for a single run: nothing is read, nothing is written, and
mypy's incremental cache goes to a temporary directory that is deleted afterwards.

## Development

```bash
uv sync
uv run pytest
uv run mypy
uv run ruff check
```

The test suite builds real jj workspaces and git repositories in temp directories; tests for a
VCS that is not installed skip themselves.

## Limitations

- **Renamed files are not tracked.** An error in a file that was renamed shows up as one fixed
  plus one new.
- Notes (`note:` lines) are attached to their error and printed with it, but are not diffed on
  their own.
- The base revision is checked out into a temporary worktree, which does not include your
  virtualenv. Dependencies still resolve because mypy runs from your project's environment, but
  a project relying on files that are *not* committed may report differently on the base side.

## License

MIT
