"""Whole-file extraction and containment writes.

``extract_file`` accepts one ``FILE`` frame — a path line, an optional blank line, then the payload
to the end of the reply — and preserves those bytes without newline normalization. The single
markdown construct it interprets is a fence wrapping the payload end to end, which is transport
rather than source and is removed (D-EDITS-003). ``apply_file`` retains the loop's keep-only
boundary through ``paths.resolve_within``.

Both of those tolerances were bought by measurement, and both trade the same way. A model emits a
fence and skips the blank line whatever the card asks, and a parser that refuses either produces
BLOCKED — the one terminal outcome carrying no diagnosis, so the attempt is spent and the feedback
tail has nothing to work from. Accepting the reply instead sends whatever is wrong with it to the
immutable oracle, which names the line. Strictness that turns a repairable failure into a silent
one is worth less than the malformed replies it catches (D-EDITS-003, D-EDITS-004).

The frame carried a declared ``UTF8-BYTES`` count until it was measured against a real model:
gpt-oss-20b passed 1 run in 5 on a 15-line file, missing by 16, 10, and 100 bytes, and the failure
rate grows with the payload. A ``--raw`` transcript showed why — the model enumerates the file
character by character, and the enumeration carries a per-character slip rate that no instruction
removes. It was also the weaker of two truncation signals already present: the server reports its
own cap through ``finish_reason``, and the immutable oracle reports a cut-off file exactly, as a
``SyntaxError`` naming the line. Retiring it costs one thing — trailing prose or a second frame is
no longer refused at the parser — and that cost lands where the loop is stronger, because that same
oracle diagnostic is repairable feedback where a parse rejection produced a silent BLOCKED with
nothing to repair from (D-EDITS-002).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from claude_local.paths import KeepOnlyViolation, resolve_within

if TYPE_CHECKING:
    from pathlib import Path

# Public: the replay transport encodes the header this parses; one owner spells it (D-BENCH-002).
WHOLE_FILE_REPLY_PREFIX = "FILE: "
_LINE_BREAK = "\n"
_CARRIAGE_RETURN = "\r"
_FENCE = "```"


@dataclass(frozen=True, slots=True)
class WholeFileReply:
    """One framed implementation: its declared relative path and validated UTF-8 payload bytes."""

    path: str
    payload: bytes


def extract_file(text: str) -> WholeFileReply | None:
    """Parse one whole-file frame: a single path line, a blank line, then the payload.

    The header is the first line, and a leading word or a missing ``FILE: `` prefix refuses the
    reply rather than half-reading it. The blank line the rules card asks for is consumed when
    present but never required: models write the file on the very next line, and refusing that
    costs a whole attempt while the header ends where the line ends either way (D-EDITS-004).
    Blank lines BEFORE the header are dropped for the same reason and at the same cost: a reasoning
    channel leaves newlines ahead of a complete frame, and refusing those bytes burns the attempt
    with nothing in the feedback tail to repair from (D-EDITS-005). Only newlines are dropped, so
    every refusal that carries meaning still holds — a leading word stays a leading word, an
    indented header stays indented, and a leading carriage return still names CRLF.
    Everything after it is the file, byte for byte, with no delimiter to find at the end — source
    may contain any textual terminator, which is why D-EDITS-002 keeps rejecting one. The one
    exception is a fence wrapping the payload end to end, which ``_unwrap_fence`` removes.

    Args:
        text: The complete decoded model reply available to the caller.

    Returns:
        The framed whole-file reply, or ``None`` when the reply is not one well-formed frame.
    """
    header, separator, payload = text.lstrip(_LINE_BREAK).partition(_LINE_BREAK)
    if (
        not separator
        or _CARRIAGE_RETURN in header
        or not header.startswith(WHOLE_FILE_REPLY_PREFIX)
    ):
        return None
    path = header.removeprefix(WHOLE_FILE_REPLY_PREFIX)
    if not path.strip():
        return None
    try:
        path.encode("utf-8")
        payload_bytes = _unwrap_fence(payload.removeprefix(_LINE_BREAK)).encode("utf-8")
    except UnicodeEncodeError:
        return None
    return WholeFileReply(path, payload_bytes)


def _is_fence_opener(line: str) -> bool:
    """Whether ``line`` is a lone fence opener — the marker plus at most an info string.

    Leading whitespace disqualifies it: an indented fence sits inside a block of the file, where a
    transport wrapper never does.
    """
    trailing_trimmed = line.rstrip()
    return trailing_trimmed.startswith(_FENCE) and _FENCE not in trailing_trimmed[len(_FENCE) :]


def _unwrap_fence(payload: str) -> str:
    """Drop a markdown fence that wraps the WHOLE payload; leave every other fence in place.

    Models reliably wrap code in a fence whatever the prompt asks — Qwen3-Coder-30B does it on
    every attempt. Written through byte for byte, that payload is not the file the model meant: it
    does not compile, so the oracle collects nothing and the attempt scores zero however good the
    code inside is. The fence is transport, and scoring transport measures the wrapper rather than
    the model (D-EDITS-003).

    Unwrapping requires BOTH ends — a lone opener first, a bare closer last — because only a wrap
    of the entire payload reads as transport. Half a fence is ambiguous, and an ambiguous payload
    is taken literally, which is what keeps the rules card's promise to preserve fence-looking
    lines that belong to the implementation. Blank lines after the closer are transport too.
    """
    lines = payload.split("\n")
    end = len(lines)
    while end > 0 and not lines[end - 1].strip():
        end -= 1
    if end < 2 or not _is_fence_opener(lines[0]) or lines[end - 1].strip() != _FENCE:
        return payload
    body = "\n".join(lines[1 : end - 1])
    return f"{body}\n" if body else ""


def apply_file(reply: WholeFileReply, root: Path, permitted: str) -> Path:
    """Write one reply through ``resolve_within``; refuse any target but ``permitted``.

    Args:
        reply: The parsed whole-file reply carrying validated UTF-8 payload bytes.
        root: The worktree root.
        permitted: The one relative implementation path the model may write.

    Returns:
        The resolved path written.

    Raises:
        KeepOnlyViolation: The reply resolves outside ``root`` or to any path but ``permitted``;
            the fail-closed refusal occurs before any write (D-KEEP-001).
    """
    permitted_target = resolve_within(root, permitted)
    target = resolve_within(root, reply.path)
    if target != permitted_target:
        raise KeepOnlyViolation(reply.path, "only the permitted impl path may be written")
    target.write_bytes(reply.payload)
    return target
