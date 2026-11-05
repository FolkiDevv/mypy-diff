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

One program, for either version control system:

| Command | For |
|---|---|
| `mypy-diff` | whichever of the two manages the current directory |
| `mypy-diff-jj` | [Jujutsu](https://jj-vcs.github.io/jj/) workspaces (jj on its git backend) |
| `mypy-diff-git` | plain git repositories |

`mypy-diff` looks for the nearest `.jj` or `.git` above the current directory. A colocated
repository has both and gets jj — unless jj is not installed, in which case git reads the same
commits. The two named commands are for scripts that should not guess.

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
mypy-diff --doctor
```

It prints the tools it found and everything a run would use, without running mypy: the mypy
config it would read and the targets that config names (or that it names none), the
`[tool.mypy-diff]` settings in effect, the base it would compare against and why, whether the
clone is shallow, and the cache directory with its size. It warns when an activated virtualenv
overrides the project's own `.venv`.

## Usage

```bash
mypy-diff                      # default base, check whatever the mypy config lists
mypy-diff -- src tests         # everything after `--` goes to mypy, for both revisions
mypy-diff -b main -- src       # compare against where you branched off main
mypy-diff -b main --tip        # compare against main as it is now
```

`mypy-diff --help` lists these examples, every option and the exit codes.

### What gets checked

mypy-diff never invents targets — it passes them straight through, so the same rules apply as for
running `mypy` yourself. Either your mypy config names them:

```toml
[tool.mypy]
files = ["src", "tests"]
```

…or you give them after `--`. With neither, mypy has nothing to analyse and the run stops with a
message telling you so.

A path after `--` that exists in only one of the two revisions — the file you just created, or
one you deleted — is simply left out on the side that lacks it: it has no errors there. A path
that exists in neither is reported as an error, so a typo cannot pass as "No change".

A syntax error (or any other error mypy cannot analyse past) in the working copy is reported as
**NEW**, with a note that nothing else could be compared. In the base revision it stops the run:
pick another base, or leave the file out with `-- --exclude <path>`.

Targets are deliberately not defaulted to `.`: the base revision is checked in a clean worktree
while the target is your working copy, so `.` would drag `.venv` into one side only and report the
whole virtualenv as new errors.

### Projects in a subdirectory

In a monorepo the Python code and its `pyproject.toml` often live below the repository root,
say in `backend/`. mypy reads its config, and resolves the `files` it lists, relative to the
directory it runs in, so mypy-diff runs it in the **project directory** for both revisions:

- run mypy-diff from anywhere inside `backend/` and it picks the nearest directory above you
  with a mypy config — `mypy.ini`, `.mypy.ini`, or a `pyproject.toml` / `setup.cfg` that has a
  `[tool.mypy]` / `[mypy]` section — stopping at the repository root;
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
| default base | nearest ancestor of `@` reachable from a remote bookmark | parent of your oldest unpushed commit |
| falls back to | local bookmark or tag below `@` → fork point with `trunk()` → `@-` | `origin/HEAD` → `main`/`master` → `HEAD~1` |
| `-b NAME` accepts | a bookmark name or any revset | a branch, tag or any git revision |

`-b NAME` compares against the **fork point** — the common ancestor of `NAME` and the target.
This is what you want: if `main` moved ahead after you branched, unrelated fixes that landed on
`main` would otherwise show up as "new errors" in your diff. Pass `--tip` for the literal branch
head. `--tip` needs a branch to take the tip of — from `-b`, `base` in the config, or the pull
request — and is an error without one, rather than being silently ignored.

With `mypy-diff-git`, a bare branch name also stands for its upstream and for `origin/NAME` /
`upstream/NAME`. If your local `main` is behind one of them — you branched from `origin/main` and
never fast-forwarded `main` — the newer fork point is used and a warning says so; otherwise every
commit in between would be reported as yours. `-b <sha>` pins a commit exactly.

The base is always derived from the **target**: with `--to feature`, both the fork point and the
default base are computed from `feature`, not from whatever is checked out.

**Merges.** After `git merge origin/main` into a branch you already pushed, your unpushed work
builds on two pushed commits: your branch and main. Taking either one alone would report
everything the other brought in as yours, so the default base is their merge, made on the fly
(a commit on no branch, with fixed metadata so the cache recognises it). The same holds in the
middle of a merge (`MERGE_HEAD`). If the two conflict, the base falls back to the fork point with
the trunk and a warning says so.

`mypy-diff-jj` snapshots the working copy once before resolving anything, so `@` is what is on
disk, and a colocated repository's git refs are imported. When a revset matches several commits
(a merge), the first is used and a warning lists them.

**Target** (`--to`) defaults to the working copy *on disk*, including uncommitted changes. That
run happens in place, so it is fast and reflects exactly what you are looking at. Any other
revision is checked out into a temporary worktree (`git worktree add`; for `mypy-diff-jj`, from
jj's git store, so nothing lands in the operation log) that is always removed afterwards, even if
the run fails.

### Clean checkouts on both sides: `--isolated` and `--prepare`

By default the working copy is checked where it is, and only the base in a clean checkout. That is
fast, and it is what you see — but the two sides do not see the same files. What git ignores is on
disk but in no checkout: generated code (`*_pb2.py`, `_version.py`), a local settings module. The
base cannot import it, and the comparison is lopsided; mypy-diff warns when the results look like
it.

`--isolated` removes the difference: the working copy — tracked and untracked files as they are on
disk, deletions included, ignored files not — becomes a commit of its own and is checked out like
the base. Your staging area is not touched. With jj, that commit is simply `@`. The cost is a second
checkout, and the two runs no longer happen side by side.

`--prepare CMD` runs a shell command in every checkout before mypy does, in the project directory:
the step that recreates what a clean checkout lacks.

```bash
mypy-diff --isolated --prepare "make proto" -- src
```

The command runs with your environment, and its output is shown only when it fails (which ends the
run with exit code `2`). It never runs in the working copy itself. It is part of the result cache
key: another command means another result. Both options can live in `[tool.mypy-diff]`:
`isolated = true`, `prepare = "make proto"`.

### Pre-commit hooks

`--staged` checks what the next commit will hold — git's index, not the working copy — and
compares it with `HEAD`, so the question becomes "does this commit add errors?". Unstaged edits
next to it play no part: an unstaged fix does not hide a staged error, and an unstaged mess does
not block a clean commit. Inside a hook of `git commit -a` or `git commit <paths>` git hands the
hook a temporary index; `--staged` reads that one.

The index becomes a commit of its own (on no branch, with a fixed identity, so the same index on
the same `HEAD` always gets the same id and the caches hit) and is checked in a temporary
worktree. `-b` still picks another base; `--to` and `--staged` exclude each other. jj has no index,
so `mypy-diff-jj --staged` is an error — and in a colocated repository `mypy-diff --staged` uses
git, whose index is what a git commit records. The pre-commit hook runs on every commit, not only
on commits that touch Python files: one that only tightens the mypy config can add errors too.

A plain hook, `.git/hooks/pre-commit`:

```sh
#!/bin/sh
exec mypy-diff --staged --no-progress
```

Or with [pre-commit](https://pre-commit.com):

```yaml
repos:
  - repo: https://github.com/FolkiDevv/mypy-diff
    rev: v0.2.0
    hooks:
      - id: mypy-diff
        args: [--, src]   # what to check, if the mypy config does not say
```

pre-commit installs mypy-diff into an environment of its own; mypy still comes from your project
(its `.venv`, `uv run`, or `mypy-cmd` in `[tool.mypy-diff]`), so that imports resolve.

### Baseline files

Instead of a base revision, the comparison can be against a recorded list of errors:

```bash
mypy-diff --write-baseline mypy-baseline.json -- src   # check the working copy, record its errors
mypy-diff --baseline mypy-baseline.json -- src         # later: what is new since the recording?
```

Comparing with a baseline needs no history — a shallow clone in CI is fine — and only the target
is checked, so it is one mypy run instead of two. Commit the file, and "no new errors" means the
same thing on every machine and in every job; record it again when you have fixed some. The file is
sorted JSON, so re-recording shows up as one small diff per error that came or went.

It records the errors, the revision they are from, the mypy version, the mypy arguments and the
project directory. A run that differs in any of these warns: another mypy version may word errors
differently, and those would show up as changed or new.

When the baseline was recorded from a clean tree, it names that commit, and as long as the
repository has it, matching reads the files from there: errors are followed through edited lines
and reworded ones show up as CHANGED, just as with `-b`. Recorded from uncommitted changes, the
files the errors were found in exist nowhere any more; then an error matches when it is on the same
line or has the same message somewhere in the same file, and the header says
`recorded from uncommitted changes`. `-b` and `--tip` do not combine with either option.

### Options

```
  -b, --base REV         Base revision. Default: latest pushed named ancestor.
      --to REV           Target revision. Default: the working copy on disk.
      --tip              Use the tip of the -b branch instead of the fork point.
      --staged           Check the staged changes against HEAD (git only).
      --isolated / --no-isolated
                         Check the working copy in a clean checkout, like the base.
      --prepare CMD      Run CMD in every checkout before mypy, e.g. 'make proto'.
      --format FMT       pretty | plain | json | github | sarif | gitlab   [pretty]
      --only WHAT        new | fixed | all                         [all]
      --show-unchanged / --no-show-unchanged
                         List unchanged errors instead of counting them.
      --project DIR      Directory to run mypy in. Default: nearest mypy config above you.
      --mypy-cmd TEXT    Command used to run mypy, e.g. 'uv run mypy'.
      --cache-dir PATH   Where to keep cached results.
      --no-cache         Disable both caches for this run; leave nothing behind.
      --clean-cache      Delete the whole mypy-diff cache, then exit.
      --no-progress      Do not show the progress indicator.
      --exit-zero        Always exit 0, even when new errors appear.
      --fail-on-changed / --no-fail-on-changed
                         Also exit 1 when a pre-existing error changed its message.
      --parallel / --no-parallel
                         Run mypy on base and working copy at once. Default: with 4+ CPUs.
      --baseline FILE    Compare with the errors recorded in FILE instead of a base revision.
      --write-baseline FILE
                         Check the target, record its errors in FILE, then exit.
      --skip-version-check
      --doctor           Print detected tools and versions, then exit.
  -v, --verbose          Show resolved revisions and every command run (on stderr).
      --version
```

**Exit codes:** `0` no new errors · `1` new errors found · `2` mypy-diff itself failed.

### Project defaults: `[tool.mypy-diff]`

What a team passes on every run belongs in `pyproject.toml`:

```toml
[tool.mypy-diff]
base = "origin/main"          # default for -b
args = ["src", "tests"]       # mypy arguments when none are given after `--`
mypy-cmd = "uv run mypy"      # default for --mypy-cmd
format = "pretty"             # pretty | plain | json | github | sarif | gitlab
only = "all"                  # all | new | fixed
show-unchanged = false
fail-on-changed = false
parallel = true               # unset: with 4+ CPUs
isolated = false              # check the working copy in a clean checkout too
prepare = "make proto"        # run in every checkout before mypy
```

Keys are spelt like the long options. The table is looked for in the project directory and in
every directory above it up to the repository root; the nearest `pyproject.toml` that has one
wins, so a monorepo can keep one table at the root or one per project. The working copy's file is
used, for both revisions.

The command line always beats the file, and every on/off option has a `--no-` form to turn off
what the file turns on. Two things beat the file as well: `$MYPY_DIFF_MYPY` beats `mypy-cmd` (one
machine overriding the team's choice), and in a pull-request job the branch the pull request
merges into beats `base` — a pull request into a release branch is never compared with main.

An unknown key or a value of the wrong type stops the run with exit code `2`: a misspelt `bse`
that was quietly ignored would leave CI comparing against the wrong base.

`2` also covers every run whose result cannot be trusted: mypy killed by a signal (out of
memory), mypy exiting 1 without printing a single diagnostic (a wrapper such as `poetry run`
that could not find mypy), or an internal crash. None of them is ever read as "no errors".

### Progress

Two mypy runs take a while. With 4 or more CPUs they run at the same time — the working copy in
place, the base in its checkout — which nearly halves the wall-clock time of a cold run (3.8 s
instead of 7.2 s on mypy-diff's own code); `--no-parallel` runs them one after the other, which
needs half the memory. An interactive run shows which step it is on and how long it has been
going:

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
- uses: actions/checkout@v7
  with:
    fetch-depth: 0   # the fork point must be in the history
- run: uv tool install mypy-diff
- run: mypy-diff --format github -- src
```

The job fails only when the branch *adds* type errors; the pre-existing backlog is ignored.

- In a pull-request job (`GITHUB_BASE_REF` is set), the default base is the fork point with the
  branch the pull request merges into (with `--tip`, its tip), even over `base` in the config.
  Outside CI the default is the latest pushed ancestor — which in CI, where everything is pushed,
  would be the commit itself. `-b` still wins.
- `--format github` turns each new error into an inline annotation, and each changed one into a
  notice on its line. A fixed error's line number belongs to the base revision, so it becomes a
  notice on the file only.
- With `--format github`, the verdict also goes to the log (stderr), and a report — counts plus
  the errors themselves — to the job summary (`$GITHUB_STEP_SUMMARY`).

When base and target are the same tree (everything pushed, nothing changed), mypy runs once, and a
warning says there is nothing to compare.

A shallow clone (the `actions/checkout` default) usually lacks the fork point. mypy-diff then
stops with an error instead of silently comparing against the tip of `main`, which would report
everything `main` did since you branched as your change. The one shallow case that works is a
pull-request merge commit checked against its own base branch: its first parent is recorded in
the commit itself.

### Code scanning and code quality reports

`--format sarif` writes a [SARIF 2.1.0](https://docs.oasis-open.org/sarif/sarif/v2.1.0/) log for
GitHub code scanning (or any SARIF viewer); `--format gitlab` a
[Code Quality](https://docs.gitlab.com/ci/testing/code_quality/) report for GitLab merge requests.
Both go to stdout, so redirect them to a file; the verdict goes to the log.

```yaml
# GitHub Actions
- run: mypy-diff --format sarif -- src > mypy-diff.sarif
- uses: github/codeql-action/upload-sarif@v4
  if: always()
  with:
    sarif_file: mypy-diff.sarif
```

```yaml
# GitLab CI
mypy-diff:
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
  variables:
    GIT_DEPTH: "0"   # the fork point must be in the history
  script:
    - git fetch origin "$CI_MERGE_REQUEST_TARGET_BRANCH_NAME"
    - mypy-diff -b "origin/$CI_MERGE_REQUEST_TARGET_BRANCH_NAME" --format gitlab -- src > gl-code-quality-report.json
  artifacts:
    when: always
    reports:
      codequality: gl-code-quality-report.json
```

Both reports describe the target, so every entry points into the code under review:

- new errors are listed (SARIF `baselineState: "new"`), and so are changed ones with their old
  wording (`"updated"`), unless `--only new` leaves them out;
- unchanged errors only with `--show-unchanged` (`"unchanged"`);
- fixed errors never: their line numbers belong to the base revision. SARIF counts them in the
  run's `properties.summary`, next to the base and target revisions.

Each entry has a fingerprint built from the file, the error code, the message with its
position-dependent parts masked, and which occurrence of that diagnostic in the file it is — so
an error keeps its fingerprint when the code around it moves, and two identical errors in one file
still get two. In SARIF it is `partialFingerprints["mypyDiff/v1"]`; GitLab requires one per issue.
Severities map to SARIF levels `error`/`warning`/`note` and GitLab `major`/`minor`/`info`.

### JSON output

`--format json` prints one object, schema version 1. Every key is always present:

| Key | |
|---|---|
| `schema_version` | `1`; bumped on any incompatible change |
| `base`, `target` | `commit_id`, `short_id`, `change_id` (jj), `refs`, `description`, `working_copy`, `staged`, `snapshot` (`--isolated`); the base also has `reason`, how it was chosen |
| `summary` | counts: `new`, `fixed`, `changed`, `unchanged`, `net` — always all of them |
| `new`, `fixed` | diagnostics: `path` (repository-relative), `line`, `column` (0-based, as mypy reports it), `severity`, `message`, `code`, `hint`, `notes` |
| `changed` | pairs of `{"old": …, "new": …}` |
| `unchanged` | the list with `--show-unchanged`, else `null` |
| `only` | the `--only` filter; filtered-out lists are empty, never missing |
| `stats` | `moved`, `moved_between_files`: unchanged errors that matched at another position |
| `incomplete` | why the comparison is partial (a blocking error), else `null` |

Human output prints columns 1-based, the way mypy does, so `file:line:col` opens at the right
place. Everything that is not the report — `--verbose` tracing, warnings, the progress indicator
— goes to stderr, and the JSON is written without any styling, whatever `FORCE_COLOR` says.

## How matching works

Comparing diagnostics by `(file, line, column, message)` is useless in practice — adding one
import at the top of a file shifts every line below it, and every error in that file would be
reported as simultaneously fixed and new. Matching therefore runs in tiers, from strictest to
loosest:

1. **Exact** — same file, position and text.
2. **Shifted** — the file's contents in both revisions are diffed with `difflib`, producing a
   base-line → target-line map. A diagnostic whose line merely moved still matches. Lines that
   were *themselves* edited get no mapping, so a real change is never hidden.
3. **By content** — same file, same error code and message text, position ignored. Repeated
   diagnostics pair up in order of appearance.
4. **Changed** — the same unedited line, column and error code, but different text: mypy now
   infers another type there because code it depends on changed, often in another file. That is
   one pre-existing error worded differently, shown as `~ CHANGED` with its old text. It does not
   fail the run unless you pass `--fail-on-changed`.
5. **Across files** — code that moved to another module (a renamed file, functions extracted
   into a new one) takes its errors along. They match when the code, the text *and* the source
   line the error sits on are the same, so unrelated look-alike errors stay apart.

Tiers 2 and 3 compare a normalised message, because some of mypy's text depends on code that did
not change: `already defined on line 12` and local classes such as `mod.C@12` carry line numbers,
and `; maybe "a" or "b"?` lists every similar name in scope, so it grows when an unrelated name is
added anywhere. On a line that merely moved (tier 2) positional indices are ignored as well —
`Argument 2` becomes `Argument 3` when a parameter is added on another line. The original text
is what gets printed.

Whatever is left over is genuinely **new** (only in the target) or **fixed** (only in the base).

## Which mypy gets run

First match wins, and `--doctor` tells you which one was used:

1. `--mypy-cmd 'uv run mypy'`
2. `$MYPY_DIFF_MYPY`
3. `mypy-cmd` in `[tool.mypy-diff]` (a relative path there is relative to that `pyproject.toml`,
   wherever you run mypy-diff from)

   All three are pinned to the working copy, because the base runs in a temporary checkout: a relative
   path such as `.venv/bin/mypy` is made absolute, and `uv run` gets `--project <project>` so that
   it does not build a fresh environment from the base revision's lock file.
4. the active virtualenv (`$VIRTUAL_ENV`)
5. the project's `.venv`
6. `uv run --project <project> mypy` (for a uv workspace member, uv uses the workspace's
   environment)
7. the repository root's `.venv`, when the project is in a subdirectory and uv is not available
8. `mypy` on `PATH`

Both revisions are always checked with the same mypy and the same arguments, and each revision
gets its own incremental mypy cache — so the two runs never invalidate each other, and your own
`.mypy_cache` is left untouched.

### Packages from your own repository

An editable install points at the working copy: `uv sync` installs every workspace member (and
the project itself) that way, and an absolute `PYTHONPATH` does the same. Left alone, the base
revision would import its sibling packages from your working copy — checked against your *new*
code, a change that breaks it would come out "unchanged". So mypy-diff asks mypy's interpreter
for its import path, and every entry inside the repository is taken from the base checkout
instead (via `PYTHONPATH`, which mypy treats exactly like installed code). Absolute `MYPYPATH`
entries into the working copy are moved the same way. If mypy still reads working-copy files
while checking the base — an absolute `mypy_path` in the config, say — the run stops with an
explanation instead of reporting a wrong result.

## Caching

Two caches live side by side, in a per-user directory from
[platformdirs](https://pypi.org/project/platformdirs/):

| Platform | Location |
|---|---|
| Windows | `%LOCALAPPDATA%\mypy-diff\Cache` |
| macOS | `~/Library/Caches/mypy-diff` |
| Linux | `~/.cache/mypy-diff` |

- **Result cache** — the diagnostics of one run, as `<hash>.json`. Keyed by commit id, mypy
  version, mypy arguments and a fingerprint of mypy's environment: the installed packages and
  their versions (dependencies, stubs, plugins), the Python version, `MYPYPATH` and the user-level
  mypy config. A commit is immutable, but what mypy resolves its imports against is not —
  installing a stub package must not leave a stale base behind. If mypy's interpreter cannot be
  asked what is installed, the result cache is skipped for that run. Written for any revision that
  resolves to a concrete commit, so repeat runs skip that analysis entirely. The dirty working
  copy is never cached, and neither is a run that did not finish cleanly. Tiny — a few hundred
  bytes per entry.
- **Incremental mypy cache** — what mypy itself keeps, handed over with `--cache-dir`: one
  directory per project for the base side and one for the target side. The two never share, so
  the runs do not invalidate each other, and your own `.mypy_cache` is left untouched. Each side
  keeps its directory from one run to the next, whatever commit it checks — mypy re-validates
  every module by its content hash — and a side that has none yet starts from a copy of the
  other's. So the first run after a commit, or against a new base, is warm instead of a full
  analysis (on mypy-diff's own code: 1.6 s instead of 7.2 s). This is the part with real weight:
  megabytes per side, growing with the size of the project and its dependencies.

Both backends key on a git SHA, so `mypy-diff-jj` and `mypy-diff-git` share one cache.

### Retention

By default the cache holds the **last 3 runs** and older entries are evicted automatically after
each run (least-recently-used first): two result entries per run, and the incremental caches of
the last 3 projects checked.

Override it with an environment variable:

```bash
MYPY_DIFF_CACHE_KEEP=10   # keep the last 10 runs
MYPY_DIFF_CACHE_KEEP=0    # keep everything, never evict
```

### Clearing it

```bash
mypy-diff --clean-cache
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

- Notes (`note:` lines) are attached to their error and printed with it, but are not diffed on
  their own.
- A file that was renamed *and* edited on the erroneous line shows up as one fixed plus one new:
  moved errors are recognised by their unchanged source line.
- The base revision is checked out into a clean temporary worktree, without files git ignores
  (generated code such as `*_pb2.py` or `_version.py`) and without submodules. Imports of them
  fail in the base only, so errors in code that uses them can come out NEW. mypy-diff warns when
  it sees this: an import error that exists only in the base, on an import line you kept.

## License

MIT
