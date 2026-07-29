"""Live step indicator for the long parts of a run.

Two mypy runs and a worktree checkout can take a minute with nothing on screen, which reads as
a hang. This shows which step is running and how long it has been going.

The indicator is deliberately indeterminate: mypy writes its whole `--output=json` stream at the
end of the build, so there is no honest way to show a filling bar for the part that actually
takes the time. What *can* be counted is the pipeline step, so that is what the counter shows.

Everything goes to stderr and only when stderr is a terminal — `--format json` piped to a file
must never see any of this.
"""

from __future__ import annotations

from types import TracebackType

from rich.console import Console
from rich.markup import escape
from rich.progress import Progress, SpinnerColumn, TaskID, TextColumn, TimeElapsedColumn


class Activity:
    """Step indicator over `rich.progress`, or a no-op when disabled.

    Disabled is the common case (not a terminal, `--verbose`, `--no-progress`), so it is a real
    no-op rather than an optional object the callers have to test for.
    """

    def __init__(self, console: Console, total: int, enabled: bool) -> None:
        self.total = total
        self.enabled = enabled
        self._completed = 0
        self._progress: Progress | None = None
        self._task_id: TaskID | None = None
        if enabled:
            self._progress = Progress(
                SpinnerColumn(),
                TextColumn("[dim][{task.completed}/{task.total}][/dim]"),
                TextColumn("{task.description}"),
                TimeElapsedColumn(),
                console=console,
                # Erase the indicator on exit so the diff below it stays clean.
                transient=True,
            )

    def __enter__(self) -> Activity:
        if self._progress is not None:
            self._progress.start()
            self._task_id = self._progress.add_task("Starting", total=self.total)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._progress is not None:
            # Stops the live region before any error reaches the console below it.
            self._progress.stop()

    def step(self, description: str) -> None:
        """Advance to the next step and relabel."""
        # Never overshoot: rich renders a bogus counter, and a miscount here is a display
        # detail that must not affect the run.
        self._completed = min(self._completed + 1, self.total)
        self._update(description, self._completed)

    def detail(self, description: str) -> None:
        """Relabel the current step without advancing it."""
        self._update(description, self._completed)

    def _update(self, description: str, completed: int) -> None:
        if self._progress is None or self._task_id is None:
            return
        # Descriptions carry revision labels and bookmark names; rich would eat anything in
        # square brackets, the same way it once ate `[tool.mypy]` out of an error message.
        self._progress.update(self._task_id, description=escape(description), completed=completed)
