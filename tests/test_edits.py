"""Tests for whole-file extraction + containment writes (``claude_local.edits``).

``extract_file`` is a pure text-to-value function, so every expected ``WholeFileReply`` is
hand-derived from the loop's reply-format contract, never from running the parser. ``apply_file``
is the only writer: its tests assert persisted bytes on success and, on every refusal, that no
unintended file was written through the real ``paths.resolve_within`` containment boundary.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from factories import build_whole_file_reply

from claude_local.edits import WholeFileReply, apply_file, extract_file
from claude_local.paths import KeepOnlyViolation

FIXTURES = Path(__file__).parent / "fixtures" / "outputs"


def load_output(name: str) -> str:
    """Decode one raw reply fixture without text-I/O newline translation."""
    return (FIXTURES / name).read_bytes().decode("utf-8")


# --- extract_file: single-file frame ----------------------------------------------


def test_a_frame_carrying_no_byte_count_is_accepted() -> None:
    """The frame is a path line, a blank line, and the payload to the end of the reply.

    Oracle: the reply-format contract in the rules card, which asks for no count. The requirement
    it replaced asked the model to declare its payload's exact UTF-8 length, which measured 1 pass
    in 5 against gpt-oss-20b on a 15-line file and degrades as the payload grows — it gated out an
    implementation that passed its oracle 7/7. Truncation now surfaces where it is actionable: the
    short file reaches the immutable oracle, which reports the SyntaxError (D-EDITS-002).
    """
    assert extract_file("FILE: src/claude_local/foo.py\n\nVALUE = 1\n") == WholeFileReply(
        path="src/claude_local/foo.py", payload=b"VALUE = 1\n"
    )


def test_complete_frame_preserves_payload_without_terminal_newline() -> None:
    assert extract_file(load_output("no_terminal_newline.txt")) == WholeFileReply(
        path="src/claude_local/basic.py", payload=b"VALUE = 1"
    )


def test_fixture_payload_preserves_fence_and_header_looking_lines() -> None:
    implementation_source = 'DOC = """\n```python\nFILE: inner/marker.py\n```\n"""\nVALUE = 42'

    assert extract_file(load_output("payload_with_header_looking_lines.txt")) == WholeFileReply(
        path="src/claude_local/fenced.py", payload=implementation_source.encode("utf-8")
    )


def test_fixture_payload_preserves_unicode_and_terminal_newline() -> None:
    implementation_source = 'TEXT = "世界"\n```\nFILE: inner/marker.py\n```\n'

    assert extract_file(load_output("unicode_with_terminal_newline.txt")) == WholeFileReply(
        path="src/claude_local/unicode.py", payload=implementation_source.encode("utf-8")
    )


def test_a_cut_off_payload_parses_as_a_short_file() -> None:
    """A truncated reply is a short file, not a parse failure — the oracle is what reports it.

    Oracle: the frame grammar, which declares no length, so nothing in the text distinguishes a
    complete short file from a cut-off long one. This is the one cost of retiring the byte count,
    and it lands where the loop is stronger: this payload reaches the immutable oracle as an
    unterminated expression, and pytest names the line (D-EDITS-002).
    """
    available_source = "def trunc() -> int:\n    return 1 +"

    assert extract_file(load_output("truncated_payload.txt")) == WholeFileReply(
        path="src/claude_local/trunc.py", payload=available_source.encode("utf-8")
    )


def test_trailing_prose_is_absorbed_into_the_payload() -> None:
    """Commentary after the file is payload, because the frame declares no terminator.

    Oracle: the frame grammar — the file runs to the end of the reply. D-EDITS-002 keeps rejecting
    an end-sentinel (source may contain any textual delimiter), so there is nothing to find; the
    prose becomes a syntax error the oracle reports rather than a silent BLOCKED.
    """
    reply = "FILE: src/claude_local/foo.py\n\nVALUE = 1\nThat's the implementation."

    assert extract_file(reply) == WholeFileReply(
        path="src/claude_local/foo.py", payload=b"VALUE = 1\nThat's the implementation."
    )


def test_a_second_frame_is_absorbed_into_the_payload() -> None:
    """Only the first blank line splits; a second framed record is payload text of the first."""
    second_frame_as_payload = "FIRST = 1\nFILE: src/claude_local/second.py\n\nSECOND = 2\n"

    assert extract_file(load_output("second_frame.txt")) == WholeFileReply(
        path="src/claude_local/first.py", payload=second_frame_as_payload.encode("utf-8")
    )


def test_unicode_payload_is_preserved_byte_for_byte() -> None:
    implementation_source = 'GREETING = "héllø 世界"'

    assert extract_file(
        build_whole_file_reply("src/claude_local/foo.py", implementation_source)
    ) == WholeFileReply(
        path="src/claude_local/foo.py", payload=implementation_source.encode("utf-8")
    )


def test_extracted_reply_owns_validated_utf8_payload_bytes() -> None:
    implementation_source = 'GREETING = "héllø 世界"'

    reply = extract_file(build_whole_file_reply("src/claude_local/foo.py", implementation_source))

    assert reply is not None
    assert reply.payload == implementation_source.encode("utf-8")


@pytest.mark.parametrize(
    "implementation_source",
    [
        pytest.param("", id="empty-source"),
        pytest.param("```python\nVALUE = 1\n```", id="fence-looking-lines"),
        pytest.param("FILE: inner.py\n\nVALUE = 1\n", id="header-looking-lines"),
        pytest.param("VALUE = 1\n", id="one-terminal-newline"),
        pytest.param("VALUE = 1\n\n\n", id="many-terminal-newlines"),
        pytest.param("\nVALUE = 1\n", id="leading-blank-line"),
        pytest.param("SECTION_A = 1\n\nSECTION_B = 2\n", id="interior-blank-line"),
    ],
)
def test_complete_frame_preserves_arbitrary_payload_text(implementation_source: str) -> None:
    assert extract_file(
        build_whole_file_reply("src/claude_local/foo.py", implementation_source)
    ) == WholeFileReply(
        path="src/claude_local/foo.py", payload=implementation_source.encode("utf-8")
    )


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param("XFILE: src/claude_local/foo.py\n\n", id="leading-prose"),
        pytest.param(load_output("no_marker_single_fence.txt"), id="legacy-single-fence"),
        pytest.param(load_output("no_marker_multiple_fences.txt"), id="legacy-multiple-fences"),
        pytest.param(load_output("prose_no_blocks.txt"), id="prose-fixture"),
        pytest.param("FILE: src/claude_local/foo.py", id="missing-separator"),
        pytest.param("FILE: src/claude_local/foo.py\nVALUE = 1\n", id="single-newline-separator"),
        pytest.param("src/claude_local/foo.py\n\nX", id="missing-file-header"),
        pytest.param("FILE: \n\n", id="empty-path"),
        pytest.param("FILE:    \n\n", id="whitespace-path"),
        pytest.param("FILE: src/\ud800.py\n\n", id="unencodable-path"),
        pytest.param("FILE:src/claude_local/foo.py\n\nX", id="file-space"),
        pytest.param(
            "FILE: src/claude_local/foo.py\nUTF8-BYTES: 9\n\nVALUE = 1",
            id="retired-byte-count-header",
        ),
        pytest.param("FILE: src/claude_local/foo.py\nOTHER: x\n\n", id="extra-header"),
        pytest.param("FILE: src/claude_local/foo.py\r\n\r\nX", id="crlf"),
    ],
)
def test_invalid_or_ambiguous_reply_is_blocked(reply: str) -> None:
    """The header is exactly one ``FILE: `` line; anything else is refused before a write.

    ``retired-byte-count-header`` is load-bearing rather than historical: a model that still emits
    the retired ``UTF8-BYTES`` line — from habit, a cached prefix, or a stale prompt — must be
    refused, never written with a stray header line silently prepended to its source.
    """
    assert extract_file(reply) is None


def test_unencodable_payload_is_blocked() -> None:
    assert extract_file("FILE: src/foo.py\n\n\ud800") is None


# --- apply_file: containment + persisted state ------------------------------------


def _permitted_root(tmp_path: Path) -> tuple[Path, str]:
    """A worktree root with the impl file's parent present, and the permitted relative path."""
    (tmp_path / "src" / "claude_local").mkdir(parents=True)
    return tmp_path, "src/claude_local/foo.py"


def test_apply_writes_the_declared_path_and_persists_bytes(tmp_path: Path) -> None:
    root, permitted = _permitted_root(tmp_path)
    target = root / "src" / "claude_local" / "foo.py"

    written = apply_file(WholeFileReply(permitted, b"VALUE = 1\n"), root, permitted)

    assert written == target
    assert target.read_bytes() == b"VALUE = 1\n"


def test_apply_refuses_a_nonpermitted_path_and_writes_nothing(tmp_path: Path) -> None:
    root, permitted = _permitted_root(tmp_path)

    with pytest.raises(KeepOnlyViolation):
        apply_file(WholeFileReply("src/claude_local/other.py", b"X = 1\n"), root, permitted)

    assert not (root / "src" / "claude_local" / "other.py").exists()
    assert not (root / "src" / "claude_local" / "foo.py").exists()


def test_apply_refuses_a_path_escaping_the_root_and_writes_nothing(tmp_path: Path) -> None:
    root, permitted = _permitted_root(tmp_path)

    with pytest.raises(KeepOnlyViolation):
        apply_file(WholeFileReply("../escape.py", b"X = 1\n"), root, permitted)

    assert not (tmp_path / "escape.py").exists()
    assert not (root / "src" / "claude_local" / "foo.py").exists()


def test_extract_then_apply_round_trips_exact_utf8_bytes(tmp_path: Path) -> None:
    root, permitted = _permitted_root(tmp_path)
    implementation_source = 'DOC = """\n```python\nFILE: inner.py\n```\n"""\nLABEL = "世界"'
    reply = extract_file(build_whole_file_reply(permitted, implementation_source))
    assert reply is not None

    written = apply_file(reply, root, permitted)

    target = root / "src" / "claude_local" / "foo.py"
    assert written == target
    assert target.read_bytes() == implementation_source.encode("utf-8")


def test_extract_then_apply_rejects_a_wrong_framed_path_without_write(tmp_path: Path) -> None:
    root, permitted = _permitted_root(tmp_path)
    reply = extract_file("FILE: src/claude_local/other.py\n\nVALUE = 2")
    assert reply is not None

    with pytest.raises(KeepOnlyViolation):
        apply_file(reply, root, permitted)

    assert not (root / "src" / "claude_local" / "foo.py").exists()
    assert not (root / "src" / "claude_local" / "other.py").exists()
