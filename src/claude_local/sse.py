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


def decode_sse(chunks: Iterable[bytes]) -> Iterator[SSEEvent]:
    """Decode an SSE byte stream into a lazy sequence of typed events.

    Bytes may be split at any boundary and use LF or CRLF line endings; frames are
    reassembled across chunks. An event is dispatched on the blank line that
    terminates its ``data:`` block, so a stream truncated mid-frame yields the events
    seen so far and no phantom terminator. ``data: [DONE]`` stops the stream. A frame
    whose JSON will not parse is folded into an ``Error`` event rather than raised —
    the decoder must never crash the loop it feeds.

    Args:
        chunks: The raw response body, delivered in arbitrary byte slices.

    Yields:
        ``Delta`` / ``Finish`` / ``Usage`` / ``Error`` events, in stream order.
    """
    buffer = bytearray()
    data_lines: list[str] = []
    for chunk in chunks:
        buffer += chunk
        while (newline := buffer.find(b"\n")) >= 0:
            line = bytes(buffer[:newline]).rstrip(b"\r").decode("utf-8", "replace")
            del buffer[: newline + 1]
            if line:
                if line.startswith("data:"):
                    data_lines.append(line[len("data:") :].removeprefix(" "))
                # Comment (":"), "event:", "id:" and other fields carry no payload here.
                continue
            # Blank line: dispatch the accumulated block, if any.
            if not data_lines:
                continue
            payload = "\n".join(data_lines)
            data_lines.clear()
            if payload == _DONE:
                return
            yield from _events(payload)
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
    for choice in frame.get("choices", []):
        content = (choice.get("delta") or {}).get("content")
        if content:
            yield Delta(content)
        reason = choice.get("finish_reason")
        if reason is not None:
            yield Finish(reason)
    usage = frame.get("usage")
    if usage is not None and (completion := usage.get("completion_tokens")) is not None:
        yield Usage(completion)


def _error_message(error: object) -> str:
    """Extract a human message from an ``{"error": ...}`` frame, however it's shaped."""
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str):
            return message
    return str(error)
