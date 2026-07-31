"""Output formats.

Diagnostic lines are printed byte-for-byte the way mypy prints them, prefixed with `+`/`-`.
That keeps the output greppable and keeps `file:line:col` clickable in terminals.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

from rich.console import Console
from rich.text import Text

from .models import DiffResult, MypyMessage


class OutputFormat(StrEnum):
    PRETTY = "pretty"
    PLAIN = "plain"
    JSON = "json"
    GITHUB = "github"


class Only(StrEnum):
    ALL = "all"
    NEW = "new"
    FIXED = "fixed"


def make_console(color: bool) -> Console:
    # highlight=False and soft_wrap=True keep rich from reformatting mypy's own text.
    return Console(highlight=False, soft_wrap=True, no_color=not color)


def render(
    result: DiffResult,
    console: Console,
    output_format: OutputFormat,
    only: Only = Only.ALL,
    show_unchanged: bool = False,
) -> None:
    match output_format:
        case OutputFormat.JSON:
            console.print_json(json.dumps(_as_json(result, only)))
        case OutputFormat.GITHUB:
            _render_github(result, console, only)
        case _:
            _render_human(result, console, only, show_unchanged)


# ------------------------------------------------------------------ human output


def _render_human(result: DiffResult, console: Console, only: Only, show_unchanged: bool) -> None:
    header = Text()
    header.append("mypy-diff", style="bold")
    header.append("  base: ", style="dim")
    header.append(result.base.label())
    console.print(header)
    console.print(Text("             to: ", style="dim") + Text(result.target.label()))
    console.print()

    if only in (Only.ALL, Only.NEW):
        _render_section(console, "+ NEW", result.new, "+", "red")
    if only in (Only.ALL, Only.FIXED):
        _render_section(console, "- FIXED", result.fixed, "-", "green")

    if show_unchanged and result.unchanged:
        _render_section(console, "  UNCHANGED", result.unchanged, " ", "dim")
    elif result.unchanged:
        files = result.unchanged_files
        console.print(
            Text(
                f"  {len(result.unchanged)} unchanged "
                f"{_plural(len(result.unchanged), 'error')} "
                f"in {files} {_plural(files, 'file')}",
                style="dim",
            )
        )
        console.print()

    console.print(_summary(result))


def _render_section(
    console: Console, title: str, messages: tuple[MypyMessage, ...], prefix: str, style: str
) -> None:
    if not messages:
        return
    console.print(Text(f"{title} ({len(messages)})", style=f"bold {style}"))
    for message in messages:
        console.print(Text(f"{prefix} {message.format()}", style=style))
        for note in message.notes:
            console.print(Text(f"{prefix}   note: {note}", style="dim"))
    console.print()


def _summary(result: DiffResult) -> Text:
    if not result.new and not result.fixed:
        return Text("No change in mypy errors.", style="bold green")

    net = len(result.new) - len(result.fixed)
    sign = "+" if net > 0 else ""
    text = Text("Found ", style="bold")
    text.append(f"{len(result.new)} new", style="bold red" if result.new else "bold")
    text.append(", ")
    text.append(f"{len(result.fixed)} fixed", style="bold green" if result.fixed else "bold")
    text.append(f", {len(result.unchanged)} unchanged", style="bold")
    text.append(f"  (net {sign}{net})", style="bold red" if net > 0 else "bold green")
    return text


def _plural(count: int, word: str) -> str:
    return word if count == 1 else f"{word}s"


# ------------------------------------------------------------------ machine output


def _as_json(result: DiffResult, only: Only) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "base": _revision_json(result.base),
        "target": _revision_json(result.target),
        "summary": {
            "new": len(result.new),
            "fixed": len(result.fixed),
            "unchanged": len(result.unchanged),
            "net": len(result.new) - len(result.fixed),
        },
    }
    if only in (Only.ALL, Only.NEW):
        payload["new"] = [m.to_json() for m in result.new]
    if only in (Only.ALL, Only.FIXED):
        payload["fixed"] = [m.to_json() for m in result.fixed]
    return payload


def _revision_json(revision: Any) -> dict[str, Any]:
    return {
        "commit_id": revision.commit_id,
        "short_id": revision.short_id,
        "change_id": revision.change_id or None,
        "refs": list(revision.refs),
        "description": revision.description,
        "working_copy": revision.is_working_copy,
    }


def _render_github(result: DiffResult, console: Console, only: Only) -> None:
    """GitHub Actions annotations. Fixed errors become notices so they show up too."""
    if only in (Only.ALL, Only.NEW):
        for message in result.new:
            console.print(_annotation("error", message, "new"), markup=False, highlight=False)
    if only in (Only.ALL, Only.FIXED):
        for message in result.fixed:
            console.print(_annotation("notice", message, "fixed"), markup=False, highlight=False)


def _annotation(level: str, message: MypyMessage, kind: str) -> str:
    parts = [f"file={message.path}", f"line={message.line}"]
    if message.column is not None:
        # mypy columns are 0-based; GitHub annotations are 1-based.
        parts.append(f"col={message.column + 1}")
    if message.code:
        parts.append(f"title=mypy [{message.code}] ({kind})")
    else:
        parts.append(f"title=mypy ({kind})")
    # Newlines would terminate the annotation early.
    body = message.message.replace("\n", "%0A")
    return f"::{level} {','.join(parts)}::{body}"
