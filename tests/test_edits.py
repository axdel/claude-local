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


# --- extract_file: byte-counted single-file frame ---------------------------------


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


def test_short_frame_is_blocked_by_default() -> None:
    assert extract_file(load_output("truncated_payload.txt")) is None


def test_explicit_incomplete_mode_retains_the_exact_available_payload() -> None:
    available_source = "def trunc() -> int:\n    return 1 +"

    assert extract_file(load_output("truncated_payload.txt"), incomplete=True) == WholeFileReply(
        path="src/claude_local/trunc.py", payload=available_source.encode("utf-8")
    )


def test_explicit_incomplete_mode_still_blocks_overlong_payload() -> None:
    reply = "FILE: src/claude_local/foo.py\nUTF8-BYTES: 0\n\nX"

    assert extract_file(reply, incomplete=True) is None


def test_utf8_byte_count_preserves_unicode_payload() -> None:
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


def test_code_point_count_is_rejected_when_utf8_payload_is_overlong() -> None:
    implementation_source = 'GREETING = "héllø 世界"'
    reply = (
        f"FILE: src/claude_local/foo.py\n"
        f"UTF8-BYTES: {len(implementation_source)}\n\n"
        f"{implementation_source}"
    )

    assert extract_file(reply) is None


def test_non_ascii_decimal_count_is_rejected() -> None:
    reply = "FILE: src/claude_local/foo.py\nUTF8-BYTES: \N{ARABIC-INDIC DIGIT ONE}\n\nX"

    assert extract_file(reply) is None


@pytest.mark.parametrize(
    "implementation_source",
    [
        pytest.param("", id="empty-source"),
        pytest.param("```python\nVALUE = 1\n```", id="fence-looking-lines"),
        pytest.param("FILE: inner.py\nUTF8-BYTES: 0\n\n", id="header-looking-lines"),
        pytest.param("VALUE = 1\n", id="one-terminal-newline"),
        pytest.param("VALUE = 1\n\n\n", id="many-terminal-newlines"),
    ],
)
def test_complete_frame_preserves_arbitrary_payload_text(implementation_source: str) -> None:
    assert extract_file(
        build_whole_file_reply("src/claude_local/foo.py", implementation_source)
    ) == WholeFileReply(
        path="src/claude_local/foo.py", payload=implementation_source.encode("utf-8")
    )


def test_leading_zero_count_is_valid_decimal() -> None:
    assert extract_file("FILE: src/claude_local/foo.py\nUTF8-BYTES: 0001\n\nX") == WholeFileReply(
        path="src/claude_local/foo.py", payload=b"X"
    )


def test_enormous_declared_count_retains_incomplete_payload_without_integer_conversion() -> None:
    reply = f"FILE: src/claude_local/foo.py\nUTF8-BYTES: {'9' * 5_000}\n\nX"

    assert extract_file(reply, incomplete=True) == WholeFileReply(
        path="src/claude_local/foo.py", payload=b"X"
    )


def test_enormous_declared_count_is_rejected_by_default_without_integer_conversion() -> None:
    # The reject branch (short payload, no incomplete evidence) must also avoid int() — a
    # 5000-digit count would raise ValueError past CPython's conversion limit (D-EDITS-001).
    reply = f"FILE: src/claude_local/foo.py\nUTF8-BYTES: {'9' * 5_000}\n\nX"

    assert extract_file(reply) is None


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param("XFILE: src/claude_local/foo.py\nUTF8-BYTES: 0\n\n", id="leading-prose"),
        pytest.param(load_output("second_frame.txt"), id="second-frame-fixture"),
        pytest.param(load_output("no_marker_single_fence.txt"), id="legacy-single-fence"),
        pytest.param(load_output("no_marker_multiple_fences.txt"), id="legacy-multiple-fences"),
        pytest.param(load_output("prose_no_blocks.txt"), id="prose-fixture"),
        pytest.param("FILE: src/claude_local/foo.py\nUTF8-BYTES: 0\n\nX", id="overlong"),
        pytest.param(
            "FILE: src/claude_local/foo.py\nUTF8-BYTES: 1\n\nXtrailing prose",
            id="trailing-prose",
        ),
        pytest.param(
            "FILE: src/claude_local/foo.py\nUTF8-BYTES: 1\n\nX"
            "FILE: src/claude_local/second.py\nUTF8-BYTES: 1\n\nY",
            id="second-frame",
        ),
        pytest.param("FILE: src/claude_local/foo.py\nUTF8-BYTES: 0", id="missing-separator"),
        pytest.param("UTF8-BYTES: 0\n\n", id="missing-file-header"),
        pytest.param("FILE: \nUTF8-BYTES: 0\n\n", id="empty-path"),
        pytest.param("FILE:    \nUTF8-BYTES: 0\n\n", id="whitespace-path"),
        pytest.param("FILE: src/\ud800.py\nUTF8-BYTES: 0\n\n", id="unencodable-path"),
        pytest.param("FILE: src/claude_local/foo.py\n\n", id="missing-count-header"),
        pytest.param("FILE:src/claude_local/foo.py\nUTF8-BYTES: 0\n\n", id="file-space"),
        pytest.param("FILE: src/claude_local/foo.py\nUTF8-BYTES: \n\n", id="empty-count"),
        pytest.param("FILE: src/claude_local/foo.py\nUTF8-BYTES: -1\n\n", id="negative-count"),
        pytest.param("FILE: src/claude_local/foo.py\nUTF8-BYTES: +1\n\nX", id="signed-count"),
        pytest.param("FILE: src/claude_local/foo.py\nUTF8-BYTES: 1.0\n\nX", id="float-count"),
        pytest.param(
            "FILE: src/claude_local/foo.py\nOTHER: x\nUTF8-BYTES: 0\n\n",
            id="extra-header",
        ),
        pytest.param("FILE: src/claude_local/foo.py\r\nUTF8-BYTES: 0\r\n\r\n", id="crlf"),
    ],
)
def test_invalid_or_ambiguous_reply_is_blocked(reply: str) -> None:
    assert extract_file(reply) is None


def test_unencodable_payload_is_blocked() -> None:
    assert extract_file("FILE: src/foo.py\nUTF8-BYTES: 3\n\n\ud800") is None


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
    reply = extract_file("FILE: src/claude_local/other.py\nUTF8-BYTES: 9\n\nVALUE = 2")
    assert reply is not None

    with pytest.raises(KeepOnlyViolation):
        apply_file(reply, root, permitted)

    assert not (root / "src" / "claude_local" / "foo.py").exists()
    assert not (root / "src" / "claude_local" / "other.py").exists()
