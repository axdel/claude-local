"""Tests for the SSE streaming decoder (``claude_local.sse``).

The decoder is an external-wire parser, so these tests hold to Boundary Fixture
Fidelity: the ``.bytes`` fixtures are schema-derived from the published OpenAI /
mlx_lm streaming format (see ``tests/fixtures/sse/README.md``), never authored from
a mental model. The strongest oracles here are *metamorphic* — decoding must be
invariant to how the byte stream is chunked and to LF-vs-CRLF line endings — and a
*lifecycle invariant* — a stream cut off mid-frame yields the deltas seen so far and
no phantom terminator. Both are relations between runs, so they cannot be satisfied
by a decoder that happens to reproduce one hand-listed output.
"""

from __future__ import annotations

from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st
from sse_wire import sse_frame, sse_frame_json

from claude_local.sse import _MAX_FRAME_BYTES, Delta, Error, Finish, SSEEvent, Usage, decode_sse

_FIXTURES = Path(__file__).parent / "fixtures" / "sse"


def load_fixture(name: str) -> bytes:
    """Read a raw SSE byte fixture verbatim (no decoding, no normalization)."""
    return (_FIXTURES / name).read_bytes()


def feed(data: bytes, chunk_size: int) -> list[SSEEvent]:
    """Decode ``data`` delivered in fixed-size byte chunks — models arbitrary splits."""
    chunks = [data[i : i + chunk_size] for i in range(0, len(data), chunk_size)]
    return list(decode_sse(chunks))


# --- Known-fixture oracle: the full happy path -----------------------------------


def test_complete_stream_decodes_full_event_sequence() -> None:
    events = feed(load_fixture("complete_stream.bytes"), chunk_size=4096)
    # Oracle: the fixture's frames are schema-derived and hand-listed — role chunk
    # (empty content, no event), two content deltas, a stop finish, a usage trailer.
    assert events == [
        Delta("Artificial"),
        Delta(" intelligence"),
        Finish("stop"),
        Usage(2),
    ]


# --- Metamorphic oracles: the heart of a stream decoder ---------------------------


def test_decoding_is_invariant_to_byte_chunking() -> None:
    data = load_fixture("complete_stream.bytes")
    whole = feed(data, chunk_size=len(data) or 1)
    one_byte_at_a_time = feed(data, chunk_size=1)
    # Metamorphic relation: how the transport splits the bytes cannot change the
    # decoded events. A decoder that parses each chunk independently fails this.
    assert one_byte_at_a_time == whole
    assert whole == [Delta("Artificial"), Delta(" intelligence"), Finish("stop"), Usage(2)]


def test_decoding_is_invariant_to_crlf_line_endings() -> None:
    data = load_fixture("complete_stream.bytes")
    crlf = data.replace(b"\n", b"\r\n")  # a spec-faithful transform: SSE allows CRLF
    # Metamorphic relation: LF and CRLF are equivalent SSE line terminators.
    assert feed(crlf, chunk_size=4096) == feed(data, chunk_size=4096)


# --- Lifecycle invariant: truncation yields no phantom terminator -----------------


def test_aborted_stream_yields_deltas_then_no_terminator() -> None:
    events = feed(load_fixture("aborted_midstream.bytes"), chunk_size=4096)
    # The final frame is cut off mid-JSON with no blank-line terminator. Invariant:
    # the decoder emits the completed deltas and NOTHING for the incomplete frame —
    # no Finish, no Usage, no Error conjured from a partial frame.
    assert events == [Delta("Par"), Delta("tial")]


def test_aborted_stream_emits_no_terminal_event() -> None:
    events = feed(load_fixture("aborted_midstream.bytes"), chunk_size=1)
    # Same invariant, stated structurally and under 1-byte chunking: a truncated
    # stream contains no terminal event of any kind.
    assert not any(isinstance(e, (Finish, Usage)) for e in events)


# --- Known-fixture oracle: mid-stream error --------------------------------------


def test_mid_stream_error_becomes_error_event() -> None:
    events = feed(load_fixture("mid_stream_error.bytes"), chunk_size=4096)
    # Oracle: the fixture streams one delta then an {"error": {...}} frame whose
    # message is verbatim "context length exceeded", then [DONE].
    assert events == [Delta("Hello"), Error("context length exceeded")]


# --- Spec oracle: [DONE] terminates the stream -----------------------------------


def test_stops_at_done_sentinel_ignoring_trailing_bytes() -> None:
    stream = (
        sse_frame('{"choices":[{"index":0,"delta":{"content":"kept"},"finish_reason":null}]}')
        + b"data: [DONE]\n\n"
        + sse_frame('{"choices":[{"index":0,"delta":{"content":"dropped"},"finish_reason":null}]}')
    )
    events = feed(stream, chunk_size=4096)
    # Spec: [DONE] is the terminal sentinel. Anything after it must never decode.
    assert events == [Delta("kept")]


# --- Closed variant set: every finish_reason the wire can send --------------------


def test_each_finish_reason_yields_exactly_one_finish() -> None:
    for reason in ("stop", "length", "tool_calls"):
        stream = sse_frame(
            f'{{"choices":[{{"index":0,"delta":{{}},"finish_reason":"{reason}"}}]}}'
        )
        events = feed(stream, chunk_size=4096)
        # Lifecycle invariant over the finish-reason enum: exactly one Finish, carrying
        # the reason verbatim — no reason is dropped, none is duplicated.
        assert events == [Finish(reason)], f"finish_reason={reason!r}"


# --- The fidelity trap: usage frame carries EMPTY choices ------------------------


def test_usage_frame_with_empty_choices_does_not_crash() -> None:
    stream = sse_frame('{"choices":[],"usage":{"prompt_tokens":11,"completion_tokens":7}}')
    events = feed(stream, chunk_size=4096)
    # The real wire puts usage on a trailer frame with choices == [] (NOT on the
    # finish frame). Indexing choices[0] here would crash; the decoder must not.
    assert events == [Usage(7)]


def test_role_chunk_with_empty_content_yields_no_delta() -> None:
    stream = sse_frame(
        '{"choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}'
    )
    events = feed(stream, chunk_size=4096)
    # The opening role chunk carries content == "" — it must produce no Delta.
    assert events == []


# --- Robustness: a boundary decoder never crashes the loop it feeds ---------------


def test_malformed_json_frame_becomes_error_not_crash() -> None:
    stream = sse_frame('{"choices":[{"index":0,"delta":{"content":"oops"')  # truncated JSON
    events = feed(stream, chunk_size=4096)
    # A complete (blank-line-terminated) frame whose JSON will not parse folds into
    # the Error channel — the decoder must not raise into the supervising loop.
    assert len(events) == 1
    assert isinstance(events[0], Error)


def test_multiline_data_frame_is_concatenated() -> None:
    # SSE permits an event to span multiple data: lines, concatenated with "\n".
    stream = (
        b'data: {"choices":[{"index":0,"delta":{"content":"ab"},\n'
        b'data: "finish_reason":null}]}\n\n'
    )
    events = feed(stream, chunk_size=4096)
    # The two data lines join into one valid JSON object -> one Delta("ab").
    assert events == [Delta("ab")]


# --- Memory safety: a delimiter-less flood is capped, not buffered without bound --


def test_delimiterless_flood_beyond_the_cap_yields_error_and_stops() -> None:
    # An untrusted model that streams bytes with no frame delimiter must not grow the buffer
    # without bound. Oracle: the cap is _MAX_FRAME_BYTES; one byte past it with no "\n" MUST
    # surface an Error and end the stream — the memory-safety contract, derived from the
    # constant, never from running the decoder. (A stream with no delimiter would otherwise
    # buffer every byte and yield nothing, so [Error] vs [] is the falsifying difference.)
    flood = b"x" * (_MAX_FRAME_BYTES + 1)
    events = list(decode_sse([flood]))
    assert len(events) == 1
    assert isinstance(events[0], Error)


def test_bytes_up_to_the_cap_without_a_delimiter_do_not_error() -> None:
    # The boundary's other side: exactly _MAX_FRAME_BYTES delimiter-less bytes is within
    # budget — no Error, and (being unterminated) no event at all. Paired with the cap+1
    # case above, this pins the comparison as a strict ">" — a ">=" mutant fires here and
    # is killed. Oracle: an unterminated frame at the limit yields nothing (lifecycle
    # invariant), and the limit itself is derived from the constant.
    at_cap = b"x" * _MAX_FRAME_BYTES
    assert list(decode_sse([at_cap])) == []


def test_many_frames_in_one_chunk_decode_in_order_and_match_one_byte_chunking() -> None:
    # Stress the scan-offset drain: 50 frames in a single chunk must decode in order, and
    # identically to the same bytes fed one byte at a time. A drain that mishandled the scan
    # offset or the per-chunk compaction would drop, duplicate, or reorder frames — and
    # under 1-byte chunking, a broken compaction would re-decode already-drained frames.
    # Oracle: the 50 contents are hand-constructed, so the expected deltas are known without
    # running the decoder; the two chunkings are a metamorphic relation over the same bytes.
    stream = b"".join(
        sse_frame(
            f'{{"choices":[{{"index":0,"delta":{{"content":"d{i}"}},"finish_reason":null}}]}}'
        )
        for i in range(50)
    )
    expected = [Delta(f"d{i}") for i in range(50)]
    assert feed(stream, chunk_size=len(stream)) == expected
    assert feed(stream, chunk_size=1) == expected


# --- Property: chunking-invariance generalized over all inputs and splits ---------


@given(
    contents=st.lists(
        st.text(alphabet=st.characters(exclude_categories=("Cs",)), min_size=1, max_size=24),
        min_size=1,
        max_size=8,
    ),
    chunk_size=st.integers(min_value=1, max_value=48),
)
def test_content_deltas_survive_any_chunking_property(
    contents: list[str], chunk_size: int
) -> None:
    """Each non-empty content delta decodes to exactly ``Delta(content)``, in order, for ANY
    split of the byte stream.

    The two example tests above fix the chunk size at 1 and 4096; this generalizes the
    chunking-invariance relation over generated contents and every chunk size in [1, 48],
    killing any frame-boundary bug those miss. The expected events come from the wire shape
    (one content chunk yields one Delta), never from running the decoder. Surrogates ('Cs')
    are excluded: a real UTF-8 model wire never carries lone surrogates (Boundary Fixture
    Fidelity), and ``json.dumps`` escapes every other character — quotes, backslashes,
    newlines — so the framing stays intact.
    """
    stream = b"".join(
        sse_frame_json({"choices": [{"index": 0, "delta": {"content": c}}]}) for c in contents
    )
    assert feed(stream, chunk_size) == [Delta(c) for c in contents]
