"""Whole-file extraction and containment writes.

``extract_file`` accepts one strict ``FILE`` frame — a path line, a blank line, then the payload
to the end of the reply — and preserves those bytes without markdown interpretation or newline
normalization. ``apply_file`` retains the loop's keep-only boundary through
``paths.resolve_within``.

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

_FILE_PREFIX = "FILE: "
_HEADER_SEPARATOR = "\n\n"


@dataclass(frozen=True, slots=True)
class WholeFileReply:
    """One framed implementation: its declared relative path and validated UTF-8 payload bytes."""

    path: str
    payload: bytes


def extract_file(text: str) -> WholeFileReply | None:
    """Parse one whole-file frame: a single path line, a blank line, then the payload.

    The header must be exactly one ``FILE: `` line, so a reply that adds a second header line, a
    leading word, or a second frame ahead of the first is refused rather than half-read. Everything
    after the blank line is the file, byte for byte, with no delimiter to find at the end — source
    may contain any textual terminator, which is why D-EDITS-002 keeps rejecting one.

    Args:
        text: The complete decoded model reply available to the caller.

    Returns:
        The framed whole-file reply, or ``None`` when the reply is not one well-formed frame.
    """
    header, separator, payload = text.partition(_HEADER_SEPARATOR)
    if not separator or "\n" in header or not header.startswith(_FILE_PREFIX):
        return None
    path = header.removeprefix(_FILE_PREFIX)
    if not path.strip():
        return None
    try:
        path.encode("utf-8")
        payload_bytes = payload.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return WholeFileReply(path, payload_bytes)


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
