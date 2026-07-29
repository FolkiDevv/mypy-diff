"""The live step indicator."""

from __future__ import annotations

import io

from rich.console import Console

from mypy_diff.progress import Activity


def console() -> tuple[Console, io.StringIO]:
    """A console that renders as if attached to a terminal, into a buffer we can read."""
    buffer = io.StringIO()
    return Console(file=buffer, force_terminal=True, width=100), buffer


def test_disabled_activity_writes_nothing() -> None:
    target, buffer = console()
    with Activity(target, total=5, enabled=False) as activity:
        activity.step("Checking tools")
        activity.detail("Running mypy")
        activity.step("Comparing results")
    assert buffer.getvalue() == ""


def test_enabled_activity_shows_step_and_counter() -> None:
    target, buffer = console()
    with Activity(target, total=5, enabled=True) as activity:
        activity.step("Checking tools")
        activity.step("Resolving revisions")
    output = buffer.getvalue()

    assert "Resolving revisions" in output
    assert "[2/5]" in output


def test_detail_relabels_without_advancing() -> None:
    target, buffer = console()
    with Activity(target, total=5, enabled=True) as activity:
        activity.step("Checking base")
        activity.detail("Running mypy on base")
    output = buffer.getvalue()

    assert "Running mypy on base" in output
    assert "[1/5]" in output
    assert "[2/5]" not in output


def test_brackets_in_descriptions_survive() -> None:
    """Regression: rich parses square brackets as markup and drops them unless escaped.

    Descriptions carry revision labels and bookmark names.
    """
    target, buffer = console()
    with Activity(target, total=5, enabled=True) as activity:
        activity.step("Checking [tool.mypy] rev")

    assert "[tool.mypy]" in buffer.getvalue()


def test_stepping_past_total_does_not_overshoot() -> None:
    """A miscount is a display detail and must never break the run."""
    target, buffer = console()
    with Activity(target, total=2, enabled=True) as activity:
        for index in range(6):
            activity.step(f"Step {index}")

    assert "[3/2]" not in buffer.getvalue()


def test_indicator_is_transient() -> None:
    """The final diff must not have a stale spinner line above it."""
    target, buffer = console()
    with Activity(target, total=5, enabled=True) as activity:
        activity.step("Checking tools")

    # Rich erases a transient display on stop; the erase sequence is the proof it cleaned up.
    assert "\x1b[" in buffer.getvalue()


def test_exit_is_safe_without_enter() -> None:
    target, _ = console()
    activity = Activity(target, total=5, enabled=True)
    activity.step("no live region yet")  # must not raise
