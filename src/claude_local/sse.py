"""Server-Sent-Events decoder for the streaming chat-completions wire.

The local model server streams its completion as OpenAI-compatible SSE: a sequence
of ``data: <json>\\n\\n`` frames terminated by ``data: [DONE]``. ``decode_sse`` turns
an arbitrary byte-chunk iterable — split at *any* boundary, LF or CRLF — into a lazy
stream of typed events: ``Delta`` (text), ``Finish`` (a terminal reason), ``Usage``
(token accounting), and ``Error`` (an upstream error frame or an unparseable frame).

Laziness is the point: the client consumes events as tokens decode, so the derail
guard can stop reading mid-stream the instant a budget or repetition cap trips —
no full completion is ever buffered. This module owns its event vocabulary (it is
SSE-specific, not shared domain state) and depends on nothing in the package: a
leaf the client reads through.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Delta:
    """A chunk of generated text — the ``choices[].delta.content`` of one frame."""

    text: str


@dataclass(frozen=True, slots=True)
class Finish:
    """A terminal frame — carries the ``finish_reason`` (``stop`` / ``length`` / ...)."""

    reason: str


@dataclass(frozen=True, slots=True)
class Usage:
    """Token accounting from the ``include_usage`` trailer frame (empty ``choices``)."""

    completion_tokens: int


@dataclass(frozen=True, slots=True)
class Error:
    """An upstream ``{"error": ...}`` frame, or a frame the decoder could not parse."""

    message: str


SSEEvent = Delta | Finish | Usage | Error
"""The closed set of events the decoder emits. Every frame maps to one of these."""


_DONE = "[DONE]"

# The decode buffer holds at most one incomplete frame between chunks; a delimiter-less run of
# bytes larger than this is a runaway stream, not real traffic. Capping it bounds decoder memory
# against an untrusted model that never sends a frame terminator. 1 MiB dwarfs any legitimate
# frame (deltas are token-sized), so the cap never rejects real data — it only stops a flood.
_MAX_FRAME_BYTES = 1 << 20


def decode_sse(chunks: Iterable[bytes]) -> Iterator[SSEEvent]:
    """Decode an SSE byte stream into a lazy sequence of typed events.

    Bytes may be split at any boundary and use LF or CRLF line endings; frames are
    reassembled across chunks. An event is dispatched on the blank line that
    terminates its ``data:`` block, so a stream truncated mid-frame yields the events
    seen so far and no phantom terminator. ``data: [DONE]`` stops the stream. A frame
    whose JSON will not parse is folded into an ``Error`` event rather than raised —
    the decoder must never crash the loop it feeds.

    Buffered bytes drain in linear time: the newline search resumes past the
    already-scanned tail, so bytes are scanned once across chunks (never re-scanned
    from the start of every chunk), and the buffer is compacted once per chunk, never
    per line. Memory is bounded on both axes an untrusted stream can grow: if the
    retained incomplete frame plus the pending ``data:`` block (a run of ``data:``
    lines never closed by a blank line) together exceed ``_MAX_FRAME_BYTES``, the
    decoder yields an ``Error`` and stops — a stream that never terminates a frame
    cannot exhaust memory.

    Args:
        chunks: The raw response body, delivered in arbitrary byte slices.

    Yields:
        ``Delta`` / ``Finish`` / ``Usage`` / ``Error`` events, in stream order.
    """
    buffer = bytearray()
    data_lines: list[str] = []
    data_bytes = 0  # size of the pending data block; bounded together with the buffer
    search_from = 0  # newline search resumes here — the retained tail is already scanned
    for chunk in chunks:
        buffer += chunk
        start = 0  # line-start / compaction offset, reset per chunk; drained in one shift
        while (newline := buffer.find(b"\n", search_from)) >= 0:
            line = bytes(buffer[start:newline]).rstrip(b"\r").decode("utf-8", "replace")
            start = newline + 1
            search_from = newline + 1
            if line:
                if line.startswith("data:"):
                    payload_line = line[len("data:") :].removeprefix(" ")
                    data_lines.append(payload_line)
                    data_bytes += len(payload_line)
                # Comment (":"), "event:", "id:" and other fields carry no payload here.
                continue
            # Blank line: dispatch the accumulated block, if any.
            if not data_lines:
                continue
            payload = "\n".join(data_lines)
            data_lines.clear()
            data_bytes = 0
            if payload == _DONE:
                return
            yield from _events(payload)
        del buffer[:start]  # compact drained lines in one shift, not one per line (avoids O(n^2))
        search_from = len(buffer)  # the retained tail has no newline; only new bytes need scanning
        if len(buffer) + data_bytes > _MAX_FRAME_BYTES:
            yield Error(f"SSE frame exceeded {_MAX_FRAME_BYTES} bytes with no delimiter")
            return
    # End of stream: an unterminated trailing frame is intentionally NOT flushed.


def _events(payload: str) -> Iterator[SSEEvent]:
    """Map one complete data-frame payload to its events, folding parse errors in."""
    try:
        frame = json.loads(payload)
    except json.JSONDecodeError:
        yield Error(f"malformed SSE frame: {payload[:120]}")
        return
    if not isinstance(frame, dict):
        yield Error(f"unexpected SSE frame shape: {type(frame).__name__}")
        return
    if "error" in frame:
        yield Error(_error_message(frame["error"]))
        return
    choice_events = _choice_events(frame.get("choices", []))
    if isinstance(choice_events, Error):
        yield choice_events
        return
    usage_event = _usage_event(frame.get("usage"))
    if isinstance(usage_event, Error):
        yield usage_event
        return
    yield from choice_events
    if usage_event is not None:
        yield usage_event


def _choice_events(choices: object) -> list[SSEEvent] | Error:
    """Validate and translate a frame's choices without emitting partial events."""
    if not isinstance(choices, list):
        return _unexpected_shape("choices", "array", choices)
    events: list[SSEEvent] = []
    for choice in choices:
        if not isinstance(choice, dict):
            return _unexpected_shape("choice", "object", choice)
        reason = choice.get("finish_reason")
        if reason is not None and not isinstance(reason, str):
            return _unexpected_shape("finish_reason", "string or null", reason)
        delta = choice.get("delta", {})
        if not isinstance(delta, dict):
            return _unexpected_shape("delta", "object", delta)
        content = delta.get("content")
        if content is not None and not isinstance(content, str):
            return _unexpected_shape("content", "string or null", content)
        if content:
            events.append(Delta(content))
        if reason is not None:
            events.append(Finish(reason))
    return events


def _usage_event(usage: object) -> Usage | Error | None:
    """Validate and translate optional usage accounting from one complete frame."""
    if usage is None:
        return None
    if not isinstance(usage, dict):
        return _unexpected_shape("usage", "object or null", usage)
    completion = usage.get("completion_tokens")
    if completion is None:
        return None
    if isinstance(completion, bool) or not isinstance(completion, int) or completion < 0:
        return _unexpected_shape("completion_tokens", "non-negative integer", completion)
    return Usage(completion)


def _unexpected_shape(field: str, expected: str, value: object) -> Error:
    """Describe one off-contract nested SSE field through the decoder's typed error channel."""
    return Error(f"unexpected {field} shape: expected {expected}, got {type(value).__name__}")


def _error_message(error: object) -> str:
    """Extract a human message from an ``{"error": ...}`` frame, however it's shaped."""
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str):
            return message
    return str(error)
