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
        # A payload wrapped END TO END in a fence is transport, not source, and is unwrapped
        # instead — see the whole-file-reply tests below. What stays preserved is every fence
        # that is genuinely part of the file: an inner one, and an unbalanced one.
        pytest.param("VALUE = 1\n```\nstill source\n", id="unbalanced-inner-fence"),
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


def test_a_fence_wrapping_the_whole_payload_is_unwrapped() -> None:
    """A reply fenced end to end yields the file the fence contains, not the fence.

    Oracle: measured against a real model. Qwen3-Coder-30B wraps its whole-file reply in a
    ```python fence on every attempt; taken byte for byte that payload does not compile
    (``SyntaxError`` at line 1), so the oracle collects nothing and the case scores 0/N however
    good the code inside is. All seven benchmark cases scored zero on their first attempt for
    exactly this reason. A fence is transport — the same class of artifact as a run fact in the
    feedback tail — and a benchmark that scores it is measuring the wrapper, not the model.
    """
    fenced = build_whole_file_reply("src/claude_local/foo.py", "```python\nVALUE = 1\n```")

    assert extract_file(fenced) == WholeFileReply(
        path="src/claude_local/foo.py", payload=b"VALUE = 1\n"
    )


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param("```\nVALUE = 1\n```", b"VALUE = 1\n", id="no-language-tag"),
        pytest.param("```py\nVALUE = 1\n```\n\n", b"VALUE = 1\n", id="trailing-blank-lines"),
        pytest.param("```python\nA = 1\n\nB = 2\n```", b"A = 1\n\nB = 2\n", id="interior-blank"),
        pytest.param("```python\n```", b"", id="empty-body"),
        pytest.param(
            '```python\nDOC = """\n```py\ninner\n```\n"""\n```',
            b'DOC = """\n```py\ninner\n```\n"""\n',
            id="inner-fence-survives-the-outer-unwrap",
        ),
    ],
)
def test_fence_unwrapping_keeps_the_file_between_the_fences(payload: str, expected: bytes) -> None:
    """Only the outermost balanced pair is transport; everything between it is the file."""
    reply = extract_file(build_whole_file_reply("src/claude_local/foo.py", payload))

    assert reply is not None
    assert reply.payload == expected


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param("```python\nVALUE = 1", id="opener-with-no-closer"),
        pytest.param("VALUE = 1\n```", id="closer-with-no-opener"),
        pytest.param("    ```python\nVALUE = 1\n    ```", id="indented-so-inside-a-block"),
        pytest.param("```python\nVALUE = 1\n``` trailing", id="closer-is-not-alone-on-its-line"),
    ],
)
def test_an_unbalanced_fence_is_left_in_the_file(payload: str) -> None:
    """Unwrapping needs BOTH ends, so a fence that is part of the source is never eaten.

    The rules card promises to preserve fence-looking lines that belong to the implementation, and
    only a wrap of the entire payload can be read as transport with any confidence. Half a fence is
    ambiguous, and the safe reading of an ambiguous payload is the literal one.
    """
    reply = extract_file(build_whole_file_reply("src/claude_local/foo.py", payload))

    assert reply is not None
    assert reply.payload == payload.encode("utf-8")


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param("XFILE: src/claude_local/foo.py\n\n", id="leading-prose"),
        pytest.param(load_output("no_marker_single_fence.txt"), id="legacy-single-fence"),
        pytest.param(load_output("no_marker_multiple_fences.txt"), id="legacy-multiple-fences"),
        pytest.param(load_output("prose_no_blocks.txt"), id="prose-fixture"),
        pytest.param("FILE: src/claude_local/foo.py", id="missing-separator"),
        pytest.param("src/claude_local/foo.py\n\nX", id="missing-file-header"),
        pytest.param("FILE: \n\n", id="empty-path"),
        pytest.param("FILE:    \n\n", id="whitespace-path"),
        pytest.param("FILE: src/\ud800.py\n\n", id="unencodable-path"),
        pytest.param("FILE:src/claude_local/foo.py\n\nX", id="file-space"),
        pytest.param("FILE: src/claude_local/foo.py\r\n\r\nX", id="crlf"),
        pytest.param("\n\nXFILE: src/claude_local/foo.py\n\n", id="blank-lines-then-prose"),
        pytest.param("\n\n  FILE: src/claude_local/foo.py\n\nX", id="blank-lines-then-indented"),
        pytest.param("\r\nFILE: src/claude_local/foo.py\n\nX", id="leading-carriage-return"),
    ],
)
def test_invalid_or_ambiguous_reply_is_blocked(reply: str) -> None:
    """The header is one ``FILE: `` line naming a usable path; anything else is refused.

    ``crlf`` stays refused rather than trimmed: the header runs to the first newline, so under CRLF
    the path would carry a trailing carriage return and name a different file than the model meant.
    """
    assert extract_file(reply) is None


def test_the_blank_line_after_the_header_is_optional() -> None:
    """A reply that starts the file on the very next line yields the same file as a spaced one.

    Oracle: measured against a real model. Qwen3-Coder-Next-4bit writes the header and then the
    file with no blank line between, and the ladder scored BLOCKED on cases 01, 02, and 04 for that
    alone — all three with ``length_capped: 0``, so the replies were complete and the parser, not
    the model, refused them. BLOCKED is the worst terminal outcome the loop has: it burns the
    attempt and leaves the feedback tail with nothing to repair from, while the same model scored
    13/13 on case 06. The blank line is a convention of the rules card, not information — the
    header ends where the line ends either way.
    """
    spaced = "FILE: src/claude_local/foo.py\n\nVALUE = 1\n"
    tight = "FILE: src/claude_local/foo.py\nVALUE = 1\n"

    assert extract_file(tight) == extract_file(spaced)
    assert extract_file(tight) == WholeFileReply(
        path="src/claude_local/foo.py", payload=b"VALUE = 1\n"
    )


def test_blank_lines_before_the_header_do_not_refuse_the_frame() -> None:
    """The mirror of the optional blank line after the header, and the same trade decides it.

    Oracle: measured against a real model, and the reply is kept as a fixture rather than
    described. Qwen3.8-27B-abliterated, served with its thinking channel left on, emitted a
    complete and well-formed frame preceded by two newlines — the residue a reasoning model left
    in content — and the whole attempt was refused for those two bytes, with ``finish_reason:
    stop`` and no
    derail. That is the same BLOCKED-with-nothing-to-repair-from outcome D-EDITS-004 weighed, at
    the other end of the header.

    Leading blank lines carry no information: nothing in the wire schema makes them ambiguous, and
    the refusal that IS load-bearing survives untouched, because dropping newlines leaves a leading
    WORD exactly where it was and an indented header still indented. Both are pinned as refusals
    alongside the prose case.
    """
    captured = load_output("leading_blank_lines_before_frame.txt")

    reply = extract_file(captured)

    assert reply is not None
    assert reply.path == "src/quicksort.py"
    # The file is every byte after the header line — read off the capture, not off a parse of it.
    assert reply.payload.startswith(b'"""Ascending integer sort')
    assert reply.payload.endswith(b"return quicksort(left) + middle + quicksort(right)")
    # And it is that suffix verbatim: a payload that is not a tail of the reply has been mangled.
    assert captured.encode("utf-8").endswith(reply.payload)


def test_only_one_blank_line_is_separator_and_the_rest_is_the_file() -> None:
    """A second blank line belongs to the source, so a file may legitimately start blank."""
    reply = extract_file("FILE: src/claude_local/foo.py\n\n\nVALUE = 1\n")

    assert reply is not None
    assert reply.payload == b"\nVALUE = 1\n"


@pytest.mark.parametrize(
    "stray_line",
    [
        pytest.param("UTF8-BYTES: 9", id="retired-byte-count-header"),
        pytest.param("OTHER: x", id="extra-header"),
    ],
)
def test_a_stray_header_line_reaches_the_oracle_instead_of_blocking(stray_line: str) -> None:
    """A second header-shaped line is source now, so the oracle names it and the model repairs it.

    This replaces the guarantee that such a reply is refused at the parser. Nothing structural
    separates ``FILE: p`` + ``UTF8-BYTES: 9`` from ``FILE: p`` + a line of code — telling them
    apart means guessing whether line two is a header, which is the delimiter ambiguity the
    whole-file frame exists to avoid. So the choice is which failure to take, and D-EDITS-002
    already settled it: a refused reply produces a silent BLOCKED with nothing to repair from,
    while a written one produces a ``SyntaxError`` naming the exact line. The strictness defended
    a line no model has been observed emitting — the card forbids it and D-EDITS-002 retired it —
    against a shape a real model sends on 3 of 7 cases.
    """
    reply = extract_file(f"FILE: src/claude_local/foo.py\n{stray_line}\n\nVALUE = 1")

    assert reply is not None
    assert reply.path == "src/claude_local/foo.py"
    assert reply.payload.decode("utf-8").startswith(stray_line)


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
