"""The three-tier matcher — the part that decides what counts as a *new* error."""

from __future__ import annotations

from mypy_diff.matching import build_line_map, diff_messages, normalise_message
from mypy_diff.models import DiffResult, MypyMessage, RevisionInfo, Severity

BASE = RevisionInfo(commit_id="a" * 40, short_id="aaaaaaaa")
TARGET = RevisionInfo(commit_id="b" * 40, short_id="bbbbbbbb", is_working_copy=True)


def message(
    path: str = "src/app.py",
    line: int = 10,
    column: int | None = 4,
    code: str = "return-value",
    text: str = "Incompatible return value type",
) -> MypyMessage:
    return MypyMessage(
        path=path,
        line=line,
        column=column,
        severity=Severity.ERROR,
        message=text,
        code=code,
    )


def diff(
    base: list[MypyMessage],
    target: list[MypyMessage],
    files: dict[str, tuple[str, str]] | None = None,
) -> DiffResult:
    """Run the matcher; `files` maps a path to its (base, target) contents."""
    files = files or {}
    return diff_messages(
        BASE,
        TARGET,
        base,
        target,
        lambda path: files[path][0] if path in files else None,
        lambda path: files[path][1] if path in files else None,
    )


# ------------------------------------------------------------------ line maps


def test_line_map_tracks_an_insertion_at_the_top() -> None:
    base = "a\nb\nc\n"
    target = "new\na\nb\nc\n"
    assert build_line_map(base, target) == {1: 2, 2: 3, 3: 4}


def test_line_map_omits_rewritten_lines() -> None:
    # Line 2 changed, so it deliberately gets no mapping and cannot match by position.
    mapping = build_line_map("a\nb\nc\n", "a\nCHANGED\nc\n")
    assert 2 not in mapping
    assert mapping[1] == 1
    assert mapping[3] == 3


# ------------------------------------------------------------------ tier 1


def test_identical_messages_are_unchanged() -> None:
    result = diff([message()], [message()])
    assert result.new == ()
    assert result.fixed == ()
    assert len(result.unchanged) == 1


def test_a_brand_new_error_is_new() -> None:
    result = diff([], [message()])
    assert len(result.new) == 1
    assert result.fixed == ()


def test_a_removed_error_is_fixed() -> None:
    result = diff([message()], [])
    assert len(result.fixed) == 1
    assert result.new == ()


def test_a_file_deleted_entirely_reports_its_errors_as_fixed() -> None:
    result = diff([message(path="src/gone.py")], [])
    assert len(result.fixed) == 1


# ------------------------------------------------------------------ tier 2


def test_an_error_pushed_down_by_an_insertion_is_not_reported_as_new() -> None:
    """The regression this whole tool exists to avoid.

    Adding an import at the top shifts every line below; a naive matcher would call every
    pre-existing error both fixed and new.
    """
    base_text = "\n".join(f"line{i}" for i in range(1, 21)) + "\n"
    target_text = "import os\n" + base_text

    result = diff(
        [message(line=10)],
        [message(line=11)],
        files={"src/app.py": (base_text, target_text)},
    )
    assert result.new == ()
    assert result.fixed == ()
    assert len(result.unchanged) == 1
    assert result.stats["moved"] == 1


def test_shifted_match_needs_the_same_code_and_text() -> None:
    base_text = "a\nb\nc\n"
    target_text = "x\na\nb\nc\n"
    result = diff(
        [message(line=2, code="return-value")],
        [message(line=3, code="arg-type")],
        files={"src/app.py": (base_text, target_text)},
    )
    # Different error code on the moved line: genuinely a different problem.
    assert len(result.new) == 1
    assert len(result.fixed) == 1


def test_falls_back_when_file_contents_are_unavailable() -> None:
    # No `files` entry, so tier 2 is skipped and tier 3 has to carry the match.
    result = diff([message(line=10)], [message(line=99)])
    assert result.new == ()
    assert result.fixed == ()
    assert len(result.unchanged) == 1


# ------------------------------------------------------------------ tier 3


def test_same_error_moved_into_rewritten_code_still_matches_by_content() -> None:
    base_text = "a\nb\nc\n"
    target_text = "TOTALLY\nDIFFERENT\nFILE\n"
    result = diff(
        [message(line=2)],
        [message(line=1)],
        files={"src/app.py": (base_text, target_text)},
    )
    assert len(result.unchanged) == 1


def test_repeated_errors_pair_up_one_for_one() -> None:
    base = [message(line=1), message(line=2), message(line=3)]
    target = [message(line=50), message(line=51)]
    result = diff(base, target)
    # Three became two: two match, one is genuinely fixed.
    assert len(result.unchanged) == 2
    assert len(result.fixed) == 1
    assert result.new == ()


def test_message_text_whitespace_differences_do_not_split_a_match() -> None:
    result = diff(
        [message(line=1, text="Incompatible  return   value")],
        [message(line=9, text="Incompatible return value")],
    )
    assert len(result.unchanged) == 1


# ------------------------------------------------------------------ mixed


def test_new_fixed_and_unchanged_together() -> None:
    result = diff(
        [message(line=1, code="a"), message(line=2, code="b")],
        [message(line=1, code="a"), message(line=3, code="c")],
    )
    assert [m.code for m in result.new] == ["c"]
    assert [m.code for m in result.fixed] == ["b"]
    assert [m.code for m in result.unchanged] == ["a"]
    assert result.has_new


def test_notes_are_excluded_from_the_diff() -> None:
    note = MypyMessage(
        path="src/app.py", line=5, column=0, severity=Severity.NOTE, message="just a note"
    )
    result = diff([], [note])
    assert result.new == ()
    assert not result.has_new


def test_errors_in_different_files_never_match_each_other() -> None:
    result = diff([message(path="src/a.py")], [message(path="src/b.py")])
    assert len(result.new) == 1
    assert len(result.fixed) == 1


def test_output_is_sorted_by_position() -> None:
    result = diff([], [message(line=30), message(line=10), message(line=20)])
    assert [m.line for m in result.new] == [10, 20, 30]


# ------------------------------------------------------------------ unstable message text
#
# The texts below are the real shapes mypy prints. Each of them changes although the diagnostic
# did not, and each used to come out as one bogus FIXED plus one bogus NEW.


def test_normalise_message_masks_position_dependent_text() -> None:
    assert normalise_message('Name "f" already defined on line 12') == (
        'Name "f" already defined on line #'
    )
    assert normalise_message('variable has type "pkg.mod.C@9"') == 'variable has type "pkg.mod.C@#"'
    assert normalise_message('"Job" has no attribute "statsu"; maybe "stats" or "status"?') == (
        '"Job" has no attribute "statsu"'
    )
    assert normalise_message(
        'Module has no attribute "proces"; maybe "process", "processes", or "proceed"?'
    ) == ('Module has no attribute "proces"')
    assert normalise_message('Name "resutl" is not defined; did you mean "result"?') == (
        'Name "resutl" is not defined'
    )


def test_redefinition_pushed_down_by_an_insertion_stays_unchanged() -> None:
    """`[no-redef]` names the line of the first definition, which moves with the insertion."""
    base_text = "def f() -> None: ...\n\n\ndef f() -> None: ...\n"
    target_text = "import os\n" + base_text
    result = diff(
        [message(line=4, code="no-redef", text='Name "f" already defined on line 1')],
        [message(line=5, code="no-redef", text='Name "f" already defined on line 2')],
        files={"src/app.py": (base_text, target_text)},
    )
    assert result.new == ()
    assert result.fixed == ()
    assert len(result.unchanged) == 1


def test_redefinition_in_moved_code_stays_unchanged() -> None:
    """Moved code has no line mapping; the content tier must still see through `on line N`."""
    result = diff(
        [message(line=9, code="no-redef", text='Name "f" already defined on line 1')],
        [message(line=30, code="no-redef", text='Name "f" already defined on line 22')],
    )
    assert result.new == ()
    assert len(result.unchanged) == 1


def test_a_redefinition_of_another_name_is_still_new() -> None:
    result = diff(
        [message(line=9, code="no-redef", text='Name "f" already defined on line 1')],
        [message(line=9, code="no-redef", text='Name "g" already defined on line 1')],
    )
    assert len(result.new) == 1
    assert len(result.fixed) == 1


def test_local_class_line_suffix_does_not_split_a_match() -> None:
    base_text = "a\nb\nc\n"
    target_text = "import os\n" + base_text
    text = (
        'Incompatible types in assignment (expression has type "b.C", variable has type "b.C@{}")'
    )
    result = diff(
        [message(line=2, code="assignment", text=text.format(9))],
        [message(line=3, code="assignment", text=text.format(10))],
        files={"src/app.py": (base_text, target_text)},
    )
    assert result.new == ()
    assert len(result.unchanged) == 1


def test_a_longer_suggestion_list_does_not_make_an_error_new() -> None:
    """Adding a similar name anywhere, even in another file, extends mypy's 'maybe' list."""
    result = diff(
        [message(code="attr-defined", text='"Job" has no attribute "statsu"; maybe "status"?')],
        [
            message(
                code="attr-defined",
                text='"Job" has no attribute "statsu"; maybe "stats" or "status"?',
            )
        ],
    )
    assert result.new == ()
    assert len(result.unchanged) == 1


def test_a_renumbered_argument_on_an_unedited_line_stays_unchanged() -> None:
    """A parameter was added and its call site updated on another line: 'Argument 2' -> '3'."""
    base_text = "g(\n    1,\n    'x',\n)\n"
    target_text = "g(\n    0,\n    1,\n    'x',\n)\n"
    result = diff(
        [message(line=3, code="arg-type", text='Argument 2 to "g" has incompatible type "str"')],
        [message(line=4, code="arg-type", text='Argument 3 to "g" has incompatible type "str"')],
        files={"src/app.py": (base_text, target_text)},
    )
    assert result.new == ()
    assert result.fixed == ()
    assert len(result.unchanged) == 1


def test_a_renumbered_list_item_on_an_unedited_line_stays_unchanged() -> None:
    base_text = "xs: list[int] = [\n    'a',\n    1,\n]\n"
    target_text = "xs: list[int] = [\n    0,\n    'a',\n    1,\n]\n"
    result = diff(
        [message(line=2, code="list-item", text='List item 0 has incompatible type "str"')],
        [message(line=3, code="list-item", text='List item 1 has incompatible type "str"')],
        files={"src/app.py": (base_text, target_text)},
    )
    assert result.new == ()
    assert len(result.unchanged) == 1


def test_indices_only_relax_the_match_on_an_unedited_line() -> None:
    """Away from a pinned position, 'Argument 1' and 'Argument 2' are different errors."""
    result = diff(
        [message(line=3, code="arg-type", text='Argument 1 to "g" has incompatible type "str"')],
        [message(line=40, code="arg-type", text='Argument 2 to "g" has incompatible type "str"')],
    )
    assert len(result.new) == 1
    assert len(result.fixed) == 1


def test_masked_indices_on_one_line_pair_by_column() -> None:
    base_text = "a\ng('x', 'y')\n"
    target_text = "import os\n" + base_text
    base = [
        message(line=2, column=2, code="arg-type", text='Argument 1 to "g" has type "str"'),
        message(line=2, column=7, code="arg-type", text='Argument 2 to "g" has type "str"'),
    ]
    target = [
        message(line=3, column=2, code="arg-type", text='Argument 1 to "g" has type "str"'),
        message(line=3, column=7, code="arg-type", text='Argument 2 to "g" has type "str"'),
    ]
    result = diff(base, target, files={"src/app.py": (base_text, target_text)})
    assert result.new == ()
    assert len(result.unchanged) == 2
