"""Tests for whole-file extraction + containment writes (``claude_local.edits``).

``extract_files`` is a pure text->structure function, so every expected ``FileBlock`` is
hand-derived from the loop's reply-format contract (the ``outputs/`` fixtures), never from
running the parser. ``apply_files`` is the only writer: its tests assert the persisted file
content on success and — on every refusal — that no unintended file was written, exercising the
real ``paths.resolve_within`` containment boundary against a real temp filesystem (no mocks).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_local.edits import FileBlock, apply_files, extract_files
from claude_local.paths import KeepOnlyViolation

FIXTURES = Path(__file__).parent / "fixtures" / "outputs"


def load_output(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# --- extract_files: FILE:-marker blocks -------------------------------------------


def test_fenced_marker_block_extracts_only_the_fenced_body() -> None:
    # Oracle: the FILE: marker names the path; the fenced ```python body is the content;
    # the "That's the whole file." prose AFTER the closing fence is excluded (fence-preferred
    # body). Derived from the reply-format contract, not from running the parser.
    assert extract_files(load_output("fenced_with_marker.txt")) == [
        FileBlock(path="src/claude_local/foo.py", content="def foo() -> int:\n    return 42\n")
    ]


def test_unfenced_marker_block_takes_all_lines_as_body() -> None:
    # Oracle: with no fence, the body is every line after the marker to EOF.
    assert extract_files(load_output("unfenced_with_marker.txt")) == [
        FileBlock(path="src/claude_local/bar.py", content="BAR = 7\n")
    ]


def test_a_file_marker_inside_a_fence_is_content_not_a_split() -> None:
    # Oracle: the inner `FILE: inner/marker.py` sits inside a fenced string, so it is code, not
    # a delimiter. Exactly ONE block results, and the inner line survives verbatim in the body.
    assert extract_files(load_output("marker_inside_fence.txt")) == [
        FileBlock(
            path="src/claude_local/baz.py",
            content='DOC = """\nFILE: inner/marker.py\n"""\ndef baz() -> str:\n    return DOC\n',
        )
    ]


def test_truncated_final_fence_is_tolerated() -> None:
    # Oracle: the stream was cut before the closing ```; the partial fenced body is still
    # recovered as one block (a truncated final block is tolerated, never discarded).
    assert extract_files(load_output("truncated_final_block.txt")) == [
        FileBlock(
            path="src/claude_local/trunc.py", content="def trunc() -> int:\n    return 1 +\n"
        )
    ]


def test_two_markers_yield_two_blocks_in_order() -> None:
    # Oracle: two FILE: markers -> two blocks in reply order. Exercises the close-previous-block
    # path a single-marker reply never reaches, and the multi-file shape apply_files must refuse.
    assert extract_files(load_output("two_markers.txt")) == [
        FileBlock(path="src/claude_local/first.py", content="FIRST = 1\n"),
        FileBlock(path="src/claude_local/second.py", content="SECOND = 2\n"),
    ]


# --- extract_files: no-marker fallback --------------------------------------------


def test_no_marker_single_fenced_region_targets_the_permitted_path() -> None:
    # Oracle: no FILE: marker but exactly one fenced region -> one block whose path is None,
    # the signal that apply_files must route it to the single permitted impl path. Surrounding
    # prose (both sides of the fence) is dropped.
    assert extract_files(load_output("no_marker_single_fence.txt")) == [
        FileBlock(path=None, content="ANSWER = 42\n")
    ]


def test_no_marker_multiple_fenced_regions_is_ambiguous() -> None:
    # Oracle: two fenced regions and no marker -> the extractor cannot know which is the impl,
    # so it yields nothing (feeds BLOCKED) rather than guessing. Pins "exactly one".
    assert extract_files(load_output("no_marker_multiple_fences.txt")) == []


def test_prose_without_markers_or_fences_yields_no_blocks() -> None:
    # Oracle: a refusal/clarification reply has no code at all -> [] -> the loop reports BLOCKED.
    assert extract_files(load_output("prose_no_blocks.txt")) == []


# --- apply_files: containment + persisted state -----------------------------------


def _permitted_root(tmp_path: Path) -> tuple[Path, str]:
    """A worktree root with the impl file's parent present, and the permitted relative path."""
    (tmp_path / "src" / "claude_local").mkdir(parents=True)
    return tmp_path, "src/claude_local/foo.py"


def test_apply_writes_the_marker_path_and_persists_content(tmp_path: Path) -> None:
    root, permitted = _permitted_root(tmp_path)
    written = apply_files([FileBlock(permitted, "VALUE = 1\n")], root, permitted)
    # Response: the resolved target is returned. Persisted state: the file holds exactly the body.
    assert written == [root / "src" / "claude_local" / "foo.py"]
    assert (root / "src" / "claude_local" / "foo.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_apply_routes_a_none_path_to_the_permitted_impl_path(tmp_path: Path) -> None:
    root, permitted = _permitted_root(tmp_path)
    written = apply_files([FileBlock(None, "VALUE = 2\n")], root, permitted)
    assert written == [root / "src" / "claude_local" / "foo.py"]
    assert (root / "src" / "claude_local" / "foo.py").read_text(encoding="utf-8") == "VALUE = 2\n"


def test_apply_refuses_a_nonpermitted_path_and_writes_nothing(tmp_path: Path) -> None:
    root, permitted = _permitted_root(tmp_path)
    # A block naming a real, contained, but NOT-permitted file must be refused (single-file mode).
    with pytest.raises(KeepOnlyViolation):
        apply_files([FileBlock("src/claude_local/other.py", "X = 1\n")], root, permitted)
    # The denied write left no artifact behind — a refused mutation must not persist.
    assert not (root / "src" / "claude_local" / "other.py").exists()


def test_apply_refuses_a_path_escaping_the_root_and_writes_nothing(tmp_path: Path) -> None:
    root, permitted = _permitted_root(tmp_path)
    with pytest.raises(KeepOnlyViolation):
        apply_files([FileBlock("../escape.py", "X = 1\n")], root, permitted)
    assert not (tmp_path / "escape.py").exists()


def test_apply_is_atomic_a_forbidden_block_prevents_all_writes(tmp_path: Path) -> None:
    root, permitted = _permitted_root(tmp_path)
    # The first block is permitted, the second escapes the root. Two-phase validation must refuse
    # the whole reply BEFORE any write — so even the permitted file must not exist afterward.
    with pytest.raises(KeepOnlyViolation):
        apply_files(
            [FileBlock(permitted, "A = 1\n"), FileBlock("../escape.py", "B = 2\n")],
            root,
            permitted,
        )
    assert not (root / "src" / "claude_local" / "foo.py").exists()
    assert not (tmp_path / "escape.py").exists()


def test_extract_then_apply_round_trips_the_permitted_file(tmp_path: Path) -> None:
    # The two functions compose: a real reply extracts to one block, which applies to the file.
    root, permitted = _permitted_root(tmp_path)
    blocks = extract_files(load_output("fenced_with_marker.txt"))
    written = apply_files(blocks, root, permitted)
    assert written == [root / "src" / "claude_local" / "foo.py"]
    assert (root / "src" / "claude_local" / "foo.py").read_text(
        encoding="utf-8"
    ) == "def foo() -> int:\n    return 42\n"
