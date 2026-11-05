"""Pair up base-revision and target-revision diagnostics.

Comparing messages by ``(file, line, column, message)`` alone is useless in practice: adding
one import at the top of a file shifts every line below it, and every error in that file
would be reported as simultaneously fixed and new. So matching runs in tiers, from strictest to
loosest:

1. exact — same file, position and text;
2. shifted — the line merely moved (a text diff of the file maps it);
3. by content — same file, code and text, anywhere in the file;
4. changed — same unedited line, column and code, different text;
5. across files — the code moved to another file, with its errors.

Whatever is left is genuinely new or fixed.
"""

from __future__ import annotations

import difflib
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence

from .models import DiffResult, MypyMessage, RevisionInfo, Severity

#: Reads the contents of ``path`` in the base revision, or None if it is not there.
BaseFileReader = Callable[[str], str | None]
#: Reads the contents of ``path`` in the target revision, or None if it is not there.
TargetFileReader = Callable[[str], str | None]

#: Parts of mypy's message text that change although the diagnostic did not. Comparing raw text
#: would report such an error as fixed in the base and new in the target.
#:
#: mypy's "maybe"/"did you mean" advice lists every similar name in scope, so it grows whenever an
#: unrelated name is added, even in another file. `pretty_seq` renders the names as '"a"',
#: '"a" or "b"' or '"a", "b", or "c"'.
_SUGGESTION = re.compile(r'; (?:maybe|did you mean) "[^"]*"(?:, "[^"]*")*(?:,? or "[^"]*")?\?')
#: `[no-redef]`: 'Name "x" already defined on line 12'. The number moves with every edit above it.
_ON_LINE = re.compile(r"\bon line \d+\b")
#: Function-scoped classes are printed as `mod.C@12`, after the line they are defined on.
_SCOPED_NAME = re.compile(r"(\b[A-Za-z_][\w.]*)@\d+\b")
#: Positional indices: 'Argument 2 to "f"', 'List item 3', 'Dict entry 1', 'Overloaded function
#: signature 2 will never be matched: signature 1's ...'. Adding a parameter, a list element or an
#: overload renumbers every diagnostic after it, on lines nobody touched.
_INDEX = re.compile(r"\b(Argument|item|entry|signatures?) \d+(?: and \d+)?")


def build_line_map(base_text: str, target_text: str) -> dict[int, int]:
    """Map 1-based line numbers in the base file onto the target file.

    Only lines that survived unchanged get an entry; inserted, deleted and rewritten lines
    are intentionally absent, so a diagnostic on genuinely changed code will not be matched
    by tier 2 and falls through to the content-based tier.
    """
    matcher = difflib.SequenceMatcher(
        a=base_text.splitlines(), b=target_text.splitlines(), autojunk=False
    )
    mapping: dict[int, int] = {}
    for tag, i1, i2, j1, _ in matcher.get_opcodes():
        if tag != "equal":
            continue
        for offset in range(i2 - i1):
            mapping[i1 + offset + 1] = j1 + offset + 1
    return mapping


def diff_messages(
    base: RevisionInfo,
    target: RevisionInfo,
    base_messages: Iterable[MypyMessage],
    target_messages: Iterable[MypyMessage],
    read_base_file: BaseFileReader,
    read_target_file: TargetFileReader,
) -> DiffResult:
    """Classify every diagnostic as new, fixed, changed or unchanged."""
    old_by_file = _group(m for m in base_messages if m.severity is not Severity.NOTE)
    new_by_file = _group(m for m in target_messages if m.severity is not Severity.NOTE)

    new: list[MypyMessage] = []
    fixed: list[MypyMessage] = []
    unchanged: list[MypyMessage] = []
    changed: list[tuple[MypyMessage, MypyMessage]] = []
    moved = 0

    for path in sorted(old_by_file.keys() | new_by_file.keys()):
        olds = old_by_file.get(path, [])
        news = new_by_file.get(path, [])

        if not olds:
            new.extend(news)
            continue
        if not news:
            fixed.extend(olds)
            continue

        kept, olds, news = _match_exact(olds, news)
        unchanged.extend(kept)

        line_map: dict[int, int] | None = None
        if olds and news:
            line_map = _line_map(path, read_base_file, read_target_file)
            if line_map is not None:
                shifted, olds, news = _match_shifted(olds, news, line_map)
                unchanged.extend(shifted)
                moved += len(shifted)

        if olds and news:
            by_content, olds, news = _match_by_content(olds, news)
            unchanged.extend(by_content)
            moved += len(by_content)

        if olds and news and line_map is not None:
            pairs, olds, news = _match_changed(olds, news, line_map)
            changed.extend(pairs)

        fixed.extend(olds)
        new.extend(news)

    relocated, fixed, new = _match_across_files(fixed, new, read_base_file, read_target_file)
    unchanged.extend(relocated)

    return DiffResult(
        base=base,
        target=target,
        new=tuple(_sorted(new)),
        fixed=tuple(_sorted(fixed)),
        unchanged=tuple(_sorted(unchanged)),
        changed=tuple(sorted(changed, key=lambda pair: _position(pair[1]))),
        stats={"moved": moved + len(relocated), "moved_between_files": len(relocated)},
    )


def _line_map(
    path: str, read_base_file: BaseFileReader, read_target_file: TargetFileReader
) -> dict[int, int] | None:
    base_text = read_base_file(path)
    target_text = read_target_file(path)
    if base_text is None or target_text is None:
        return None
    return build_line_map(base_text, target_text)


def _group(messages: Iterable[MypyMessage]) -> dict[str, list[MypyMessage]]:
    grouped: dict[str, list[MypyMessage]] = defaultdict(list)
    for message in messages:
        grouped[message.path].append(message)
    return grouped


def _sorted(messages: Iterable[MypyMessage]) -> list[MypyMessage]:
    return sorted(messages, key=_position)


def _position(message: MypyMessage) -> tuple[str, int, int, str]:
    return (message.path, message.line, message.column or 0, message.message)


def _match_exact(
    olds: Sequence[MypyMessage], news: Sequence[MypyMessage]
) -> tuple[list[MypyMessage], list[MypyMessage], list[MypyMessage]]:
    """Tier 1: identical file, position and text."""
    remaining_new = list(news)
    kept: list[MypyMessage] = []
    unmatched_old: list[MypyMessage] = []

    index = _index_by(remaining_new, lambda m: m.identity)
    for old in olds:
        match = _pop(index, old.identity)
        if match is None:
            unmatched_old.append(old)
        else:
            kept.append(match)

    return kept, unmatched_old, _flatten(index)


def _match_shifted(
    olds: Sequence[MypyMessage], news: Sequence[MypyMessage], line_map: dict[int, int]
) -> tuple[list[MypyMessage], list[MypyMessage], list[MypyMessage]]:
    """Tier 2: same diagnostic, on a line that merely moved.

    Uses a real text diff of the file between the two revisions, so this only ever matches
    diagnostics sitting on lines that were not themselves edited. Because the position is pinned
    to an unedited line, the text may be compared more loosely than in tier 3: positional indices
    are ignored as well (see :func:`_positional_text`).
    """
    index = _index_by(news, lambda m: (m.line, m.code or "", _positional_text(m.message)))
    kept: list[MypyMessage] = []
    unmatched_old: list[MypyMessage] = []

    for old in olds:
        mapped = line_map.get(old.line)
        match = (
            None
            if mapped is None
            else _pop_nearest(
                index, (mapped, old.code or "", _positional_text(old.message)), old.column
            )
        )
        if match is None:
            unmatched_old.append(old)
        else:
            kept.append(match)

    return kept, unmatched_old, _flatten(index)


def _match_by_content(
    olds: Sequence[MypyMessage], news: Sequence[MypyMessage]
) -> tuple[list[MypyMessage], list[MypyMessage], list[MypyMessage]]:
    """Tier 3: same file, same error code and text, position ignored.

    Greedy and order-preserving: the Nth occurrence of a repeated diagnostic in the base
    pairs with the Nth occurrence in the target.
    """
    index = _index_by(news, _content_key)
    kept: list[MypyMessage] = []
    unmatched_old: list[MypyMessage] = []

    for old in olds:
        match = _pop(index, _content_key(old))
        if match is None:
            unmatched_old.append(old)
        else:
            kept.append(match)

    return kept, unmatched_old, _flatten(index)


def _match_changed(
    olds: Sequence[MypyMessage], news: Sequence[MypyMessage], line_map: dict[int, int]
) -> tuple[list[tuple[MypyMessage, MypyMessage]], list[MypyMessage], list[MypyMessage]]:
    """Tier 4: the same diagnostic at the same place, worded differently.

    The line was not edited, the column and the error code are the same, only the text differs:
    mypy now infers another type there, because code it depends on changed — often in another
    file. Reporting it as one FIXED plus one NEW would say an old error went away and a new one
    appeared; it is one pre-existing error whose message changed.
    """
    index = _index_by(news, lambda m: (m.line, m.column, m.code or ""))
    pairs: list[tuple[MypyMessage, MypyMessage]] = []
    unmatched_old: list[MypyMessage] = []

    for old in olds:
        mapped = line_map.get(old.line)
        match = None if mapped is None else _pop(index, (mapped, old.column, old.code or ""))
        if match is None:
            unmatched_old.append(old)
        else:
            pairs.append((old, match))

    return pairs, unmatched_old, _flatten(index)


def _match_across_files(
    olds: Sequence[MypyMessage],
    news: Sequence[MypyMessage],
    read_base_file: BaseFileReader,
    read_target_file: TargetFileReader,
) -> tuple[list[MypyMessage], list[MypyMessage], list[MypyMessage]]:
    """Tier 5: code that moved to another file, with its errors.

    A renamed module, or functions extracted into a new one, take their pre-existing errors
    along. Per file those look fixed in one place and new in another. They pair up only with
    evidence that the code itself moved:

    * the error code and the (normalised) text match, and so does the source line;
    * that line left the old file (it occurs there fewer times than before) and arrived in the
      new one (more times than before) — two untouched `return value` lines in different
      modules, one fixed and one broken by the same change, are not a move;
    * a neighbouring line moved along with it — or the whole file did: a renamed module that
      is otherwise unchanged (a one-line file has no neighbours to compare).
    """
    if not olds or not news:
        return [], list(olds), list(news)

    base = _LineReader(read_base_file)
    target = _LineReader(read_target_file)

    def key(message: MypyMessage, lines: _LineReader) -> tuple[str, str, str] | None:
        source = lines.get(message.path, message.line)
        if not source:
            return None
        return (message.code or "", normalise_message(message.message), source)

    def moved(old: MypyMessage, new: MypyMessage, source: str) -> bool:
        left = base.count(old.path, source) > target.count(old.path, source)
        arrived = target.count(new.path, source) > base.count(new.path, source)
        if not (left and arrived):
            return False
        if base.text(old.path) == target.text(new.path):
            return True
        before = base.neighbours(old.path, old.line)
        after = target.neighbours(new.path, new.line)
        return any(a and a == b for a, b in zip(before, after, strict=True))

    index: dict[tuple[str, str, str], list[MypyMessage]] = defaultdict(list)
    unkeyed_new: list[MypyMessage] = []
    for message in news:
        new_key = key(message, target)
        if new_key is None:
            unkeyed_new.append(message)
        else:
            index[new_key].append(message)

    kept: list[MypyMessage] = []
    unmatched_old: list[MypyMessage] = []
    for old in olds:
        old_key = key(old, base)
        bucket = index.get(old_key, []) if old_key is not None else []
        match = next((new for new in bucket if old_key and moved(old, new, old_key[2])), None)
        if match is None:
            unmatched_old.append(old)
        else:
            bucket.remove(match)
            kept.append(match)

    return kept, unmatched_old, unkeyed_new + _flatten(index)


class _LineReader:
    """Source lines by path and 1-based number, whitespace-normalised, read once per file."""

    def __init__(self, read: Callable[[str], str | None]) -> None:
        self._read = read
        self._files: dict[str, list[str]] = {}

    def _lines(self, path: str) -> list[str]:
        if path not in self._files:
            text = self._read(path)
            lines = [] if text is None else text.splitlines()
            self._files[path] = [" ".join(line.split()) for line in lines]
        return self._files[path]

    def get(self, path: str, line: int) -> str:
        lines = self._lines(path)
        return lines[line - 1] if 1 <= line <= len(lines) else ""

    def text(self, path: str) -> tuple[str, ...]:
        return tuple(self._lines(path))

    def count(self, path: str, source: str) -> int:
        return self._lines(path).count(source)

    def neighbours(self, path: str, line: int) -> tuple[str, str]:
        """The nearest non-blank lines before and after ``line``, or "" at the file's edge."""
        lines = self._lines(path)
        before = next((text for text in reversed(lines[: max(line - 1, 0)]) if text), "")
        after = next((text for text in lines[line:] if text), "")
        return before, after


def normalise_message(text: str) -> str:
    """The part of a message that identifies the diagnostic, wherever it sits in the file.

    Whitespace is collapsed, and text that depends on the *rest* of the code is masked: embedded
    line numbers, and the list of similar names mypy suggests. The displayed message is untouched.
    """
    text = " ".join(text.split())
    text = _SUGGESTION.sub("", text)
    text = _ON_LINE.sub("on line #", text)
    return _SCOPED_NAME.sub(r"\1@#", text)


def _positional_text(text: str) -> str:
    """Tier-2 text: :func:`normalise_message` minus positional indices.

    Only safe while the position is pinned by the line map: away from it, 'Argument 1' and
    'Argument 2' of the same call are genuinely different diagnostics.
    """
    return _INDEX.sub(r"\1 #", normalise_message(text))


def _content_key(message: MypyMessage) -> tuple[str, str]:
    """Tier-3 key: position-independent, so it holds once line numbers stop matching."""
    return (message.code or "", normalise_message(message.message))


def _index_by[K](
    messages: Iterable[MypyMessage], key: Callable[[MypyMessage], K]
) -> dict[K, list[MypyMessage]]:
    """Bucket messages by key, preserving order within each bucket."""
    index: dict[K, list[MypyMessage]] = defaultdict(list)
    for message in messages:
        index[key(message)].append(message)
    return index


def _pop[K](index: dict[K, list[MypyMessage]], key: K) -> MypyMessage | None:
    bucket = index.get(key)
    if not bucket:
        return None
    return bucket.pop(0)


def _pop_nearest[K](
    index: dict[K, list[MypyMessage]], key: K, column: int | None
) -> MypyMessage | None:
    """Like :func:`_pop`, but prefer the candidate closest to ``column``.

    Masking indices can put several diagnostics of one line into the same bucket (`Argument 1`
    and `Argument 2` of one call); the column tells them apart.
    """
    bucket = index.get(key)
    if not bucket:
        return None
    best = min(bucket, key=lambda m: abs((m.column or 0) - (column or 0)))
    bucket.remove(best)
    return best


def _flatten[K](index: dict[K, list[MypyMessage]]) -> list[MypyMessage]:
    return [message for bucket in index.values() for message in bucket]
