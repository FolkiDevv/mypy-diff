"""Output formats.

Diagnostic lines are printed byte-for-byte the way mypy prints them, prefixed with `+`/`-`.
That keeps the output greppable and keeps `file:line:col` clickable in terminals.
"""

from __future__ import annotations

import dataclasses
import json
from enum import StrEnum
from typing import Any

from rich.console import Console
from rich.text import Text

from . import reports
from .models import DiffResult, MypyMessage


class OutputFormat(StrEnum):
    PRETTY = "pretty"
    PLAIN = "plain"
    JSON = "json"
    GITHUB = "github"
    SARIF = "sarif"
    GITLAB = "gitlab"


class Only(StrEnum):
    ALL = "all"
    NEW = "new"
    FIXED = "fixed"


def make_console(color: bool) -> Console:
    """The stdout console. highlight=False and soft_wrap=True keep rich from reformatting
    mypy's own text.

    Without colour there is no styling at all: ``no_color`` alone keeps bold and dim, and
    FORCE_COLOR / TTY_COMPATIBLE in a CI environment would put escape codes into plain output.
    """
    if color:
        return Console(highlight=False, soft_wrap=True)
    return Console(highlight=False, soft_wrap=True, color_system=None)


def render(
    result: DiffResult,
    console: Console,
    output_format: OutputFormat,
    only: Only = Only.ALL,
    show_unchanged: bool = False,
) -> None:
    match output_format:
        case OutputFormat.JSON:
            _write_json(console, _as_json(result, only, show_unchanged))
        case OutputFormat.SARIF:
            properties = {
                "base": {**_revision_json(result.base), "reason": result.base_reason or None},
                "target": _revision_json(result.target),
                "summary": _summary_counts(result),
            }
            entries = _report_entries(result, only, show_unchanged)
            _write_json(console, reports.sarif(result, entries, properties))
        case OutputFormat.GITLAB:
            entries = _report_entries(result, only, show_unchanged)
            _write_json(console, reports.gitlab(result, entries))
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
    if result.base_reason:
        header.append(f"  ({result.base_reason})", style="dim")
    console.print(header)
    console.print(Text("             to: ", style="dim") + Text(result.target.label()))
    console.print()

    if only in (Only.ALL, Only.NEW):
        _render_section(console, "+ NEW", result.new, "+", "red")
    if only in (Only.ALL, Only.FIXED):
        _render_section(console, "- FIXED", result.fixed, "-", "green")
    if only is Only.ALL:
        _render_changed(console, result.changed)

    if result.incomplete:
        console.print(Text(f"! {result.incomplete}", style="bold yellow"))
        console.print()

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
        for note in message.note_lines():
            console.print(Text(f"{prefix}   note: {note}", style="dim"))
    console.print()


def _render_changed(console: Console, pairs: tuple[tuple[MypyMessage, MypyMessage], ...]) -> None:
    """Pre-existing errors whose text changed in place: the new wording, then the old one."""
    if not pairs:
        return
    console.print(Text(f"~ CHANGED ({len(pairs)})", style="bold yellow"))
    for old, new in pairs:
        console.print(Text(f"~ {new.format()}", style="yellow"))
        was = f"line {old.line}: " if old.line != new.line else ""
        console.print(Text(f"~   was: {was}{old.message}", style="dim"))
    console.print()


def summary_text(result: DiffResult) -> Text:
    """The one-line verdict, for any format that wants to add it (on stderr, say)."""
    return _summary(result)


def _summary(result: DiffResult) -> Text:
    if not result.new and not result.fixed and not result.changed:
        return Text("No change in mypy errors.", style="bold green")

    net = len(result.new) - len(result.fixed)
    sign = "+" if net > 0 else ""
    text = Text("Found ", style="bold")
    text.append(f"{len(result.new)} new", style="bold red" if result.new else "bold")
    text.append(", ")
    text.append(f"{len(result.fixed)} fixed", style="bold green" if result.fixed else "bold")
    if result.changed:
        text.append(f", {len(result.changed)} changed", style="bold yellow")
    text.append(f", {len(result.unchanged)} unchanged", style="bold")
    text.append(f"  (net {sign}{net})", style="bold red" if net > 0 else "bold green")
    return text


def _plural(count: int, word: str) -> str:
    return word if count == 1 else f"{word}s"


# ------------------------------------------------------------------ machine output


def _write_json(console: Console, payload: object) -> None:
    """Straight to the stream, not through rich: whatever the environment says about colours,
    a consumer must get JSON and nothing else."""
    console.file.write(json.dumps(payload, indent=2) + "\n")


def _report_entries(result: DiffResult, only: Only, show_unchanged: bool) -> list[reports.Entry]:
    """What a code-review report lists: diagnostics of the target, so never the fixed ones."""
    entries: list[reports.Entry] = []
    if only in (Only.ALL, Only.NEW):
        entries += [reports.Entry("new", message) for message in result.new]
    if only is Only.ALL:
        entries += [reports.Entry("changed", new, was=old) for old, new in result.changed]
    if show_unchanged:
        entries += [reports.Entry("unchanged", message) for message in result.unchanged]
    return entries


def _summary_counts(result: DiffResult) -> dict[str, int]:
    return {
        "new": len(result.new),
        "fixed": len(result.fixed),
        "changed": len(result.changed),
        "unchanged": len(result.unchanged),
        "net": len(result.new) - len(result.fixed),
    }


#: Bumped on any incompatible change to the JSON output.
JSON_SCHEMA_VERSION = 1


def _as_json(result: DiffResult, only: Only, show_unchanged: bool = False) -> dict[str, Any]:
    """The JSON report. Every key is always present, whatever the options: ``--only`` empties a
    list rather than dropping it, and says so in ``only``; ``unchanged`` is null unless
    ``--show-unchanged`` asked for the list."""
    payload: dict[str, Any] = {
        "schema_version": JSON_SCHEMA_VERSION,
        "only": only.value,
        "incomplete": result.incomplete or None,
        "base": {**_revision_json(result.base), "reason": result.base_reason or None},
        "target": _revision_json(result.target),
        "summary": _summary_counts(result),
        "stats": {
            "moved": result.stats.get("moved", 0),
            "moved_between_files": result.stats.get("moved_between_files", 0),
        },
        "new": [m.to_json() for m in result.new] if only in (Only.ALL, Only.NEW) else [],
        "fixed": [m.to_json() for m in result.fixed] if only in (Only.ALL, Only.FIXED) else [],
        "changed": [{"old": old.to_json(), "new": new.to_json()} for old, new in result.changed]
        if only is Only.ALL
        else [],
        "unchanged": [m.to_json() for m in result.unchanged] if show_unchanged else None,
    }
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
    """GitHub Actions workflow commands.

    New errors are annotations on their line. Changed ones too, as notices: their line is in
    the target. A fixed error's line number is the *base* revision's, which on the pull request's
    head points at unrelated code (or at a file that is gone), so it becomes a notice on the file
    alone. The full lists go to the job summary, see :func:`step_summary`.
    """
    if result.incomplete:
        console.print(f"::warning title=mypy-diff::{_data(result.incomplete)}", markup=False)
    if only in (Only.ALL, Only.NEW):
        for message in result.new:
            console.print(_annotation("error", message, "new"), markup=False, highlight=False)
    if only in (Only.ALL, Only.FIXED):
        for message in result.fixed:
            console.print(
                _annotation("notice", message, "fixed", with_position=False),
                markup=False,
                highlight=False,
            )
    if only is Only.ALL:
        for old, new in result.changed:
            changed = dataclasses.replace(new, message=f"{new.message}\nwas: {old.message}")
            console.print(_annotation("notice", changed, "changed"), markup=False, highlight=False)


def _annotation(level: str, message: MypyMessage, kind: str, with_position: bool = True) -> str:
    parts = [f"file={_property(message.path)}"]
    if with_position:
        parts.append(f"line={message.line}")
        if message.column is not None and message.column >= 0:
            # mypy columns are 0-based; GitHub annotations are 1-based.
            parts.append(f"col={message.column + 1}")
    title = f"mypy [{message.code}] ({kind})" if message.code else f"mypy ({kind})"
    parts.append(f"title={_property(title)}")
    body = message.message
    notes = message.note_lines()
    if notes:
        body += "\n" + "\n".join(f"note: {note}" for note in notes)
    return f"::{level} {','.join(parts)}::{_data(body)}"


def _data(text: str) -> str:
    """Escape a workflow command's message, as GitHub's own toolkit does."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _property(text: str) -> str:
    """Escape a workflow command property: `,` and `:` would end it early."""
    return _data(text).replace(":", "%3A").replace(",", "%2C")


# ------------------------------------------------------------------ job summary

#: The job summary lists at most this many errors per section; the rest is counted.
_SUMMARY_LIMIT = 50


def step_summary(result: DiffResult) -> str:
    """Markdown for `$GITHUB_STEP_SUMMARY`: the verdict, then the errors themselves."""
    net = len(result.new) - len(result.fixed)
    lines = [
        "## mypy-diff",
        "",
        f"Base: `{result.base.short_id}` {_markdown_label(result.base)}",
        "",
        "| New | Fixed | Changed | Unchanged | Net |",
        "|---:|---:|---:|---:|---:|",
        f"| {len(result.new)} | {len(result.fixed)} | {len(result.changed)} "
        f"| {len(result.unchanged)} | {net:+d} |",
        "",
    ]
    if result.incomplete:
        lines += [f"> [!WARNING]\n> {result.incomplete}", ""]
    for title, messages, open_ in (
        ("New errors", result.new, True),
        ("Fixed errors", result.fixed, False),
    ):
        if not messages:
            continue
        lines.append(
            f"<details{' open' if open_ else ''}><summary>{title} ({len(messages)})</summary>\n"
        )
        lines.append("```")
        lines += [message.format() for message in messages[:_SUMMARY_LIMIT]]
        if len(messages) > _SUMMARY_LIMIT:
            lines.append(f"... and {len(messages) - _SUMMARY_LIMIT} more")
        lines += ["```", "</details>", ""]
    if result.changed:
        lines.append(f"<details><summary>Changed errors ({len(result.changed)})</summary>\n")
        lines.append("```")
        for old, new in result.changed[:_SUMMARY_LIMIT]:
            lines += [new.format(), f"  was: {old.message}"]
        lines += ["```", "</details>", ""]
    return "\n".join(lines) + "\n"


def _markdown_label(revision: Any) -> str:
    description = revision.description.replace("`", "'")
    refs = " ".join(revision.refs)
    return " ".join(part for part in (refs, f"— {description}" if description else "") if part)
