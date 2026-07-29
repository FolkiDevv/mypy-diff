"""Pair up base-revision and target-revision diagnostics.

Comparing messages by ``(file, line, column, message)`` alone is useless in practice: adding
one import at the top of a file shifts every line below it, and every error in that file
would be reported as simultaneously fixed and new. So matching runs in three tiers, from
strictest to loosest, per file.
"""

from __future__ import annotations

import difflib
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence

from .models import DiffResult, MypyMessage, RevisionInfo, Severity

#: Reads the contents of ``path`` in the base revision, or None if it is not there.
BaseFileReader = Callable[[str], str | None]
#: Reads the contents of ``path`` in the target revision, or None if it is not there.
TargetFileReader = Callable[[str], str | None]


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
    """Classify every diagnostic as new, fixed or unchanged."""
    old_by_file = _group(m for m in base_messages if m.severity is not Severity.NOTE)
    new_by_file = _group(m for m in target_messages if m.severity is not Severity.NOTE)

    new: list[MypyMessage] = []
    fixed: list[MypyMessage] = []
    unchanged: list[MypyMessage] = []
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

        if olds and news:
            shifted, olds, news = _match_shifted(path, olds, news, read_base_file, read_target_file)
            unchanged.extend(shifted)
            moved += len(shifted)

        if olds and news:
            by_content, olds, news = _match_by_content(olds, news)
            unchanged.extend(by_content)
            moved += len(by_content)

        fixed.extend(olds)
        new.extend(news)

    return DiffResult(
        base=base,
        target=target,
        new=tuple(_sorted(new)),
        fixed=tuple(_sorted(fixed)),
        unchanged=tuple(_sorted(unchanged)),
        stats={"moved": moved},
    )


def _group(messages: Iterable[MypyMessage]) -> dict[str, list[MypyMessage]]:
    grouped: dict[str, list[MypyMessage]] = defaultdict(list)
    for message in messages:
        grouped[message.path].append(message)
    return grouped


def _sorted(messages: Iterable[MypyMessage]) -> list[MypyMessage]:
    return sorted(messages, key=lambda m: (m.path, m.line, m.column or 0, m.message))


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
    path: str,
    olds: Sequence[MypyMessage],
    news: Sequence[MypyMessage],
    read_base_file: BaseFileReader,
    read_target_file: TargetFileReader,
) -> tuple[list[MypyMessage], list[MypyMessage], list[MypyMessage]]:
    """Tier 2: same diagnostic, on a line that merely moved.

    Uses a real text diff of the file between the two revisions, so this only ever matches
    diagnostics sitting on lines that were not themselves edited.
    """
    base_text = read_base_file(path)
    target_text = read_target_file(path)
    if base_text is None or target_text is None:
        return [], list(olds), list(news)

    line_map = build_line_map(base_text, target_text)

    index = _index_by(news, lambda m: (m.line, m.code or "", " ".join(m.message.split())))
    kept: list[MypyMessage] = []
    unmatched_old: list[MypyMessage] = []

    for old in olds:
        mapped = line_map.get(old.line)
        match = (
            None
            if mapped is None
            else _pop(index, (mapped, old.code or "", " ".join(old.message.split())))
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
    index = _index_by(news, lambda m: m.content_key)
    kept: list[MypyMessage] = []
    unmatched_old: list[MypyMessage] = []

    for old in olds:
        match = _pop(index, old.content_key)
        if match is None:
            unmatched_old.append(old)
        else:
            kept.append(match)

    return kept, unmatched_old, _flatten(index)


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


def _flatten[K](index: dict[K, list[MypyMessage]]) -> list[MypyMessage]:
    return [message for bucket in index.values() for message in bucket]
