"""Byte-counted whole-file extraction and containment writes.

``extract_file`` accepts one strict ``FILE``/``UTF8-BYTES`` frame and preserves its
validated payload bytes without markdown interpretation or newline normalization. Exact byte
length is the default; short-payload recovery requires explicit incomplete-generation evidence.
``apply_file`` retains the loop's keep-only boundary through ``paths.resolve_within``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from claude_local.paths import KeepOnlyViolation, resolve_within

if TYPE_CHECKING:
    from pathlib import Path

_FILE_PREFIX = "FILE: "
_BYTE_COUNT_PREFIX = "UTF8-BYTES: "
_HEADER_SEPARATOR = "\n\n"


@dataclass(frozen=True, slots=True)
class WholeFileReply:
    """One framed implementation: its declared relative path and validated UTF-8 payload bytes."""

    path: str
    payload: bytes


def extract_file(text: str, *, incomplete: bool = False) -> WholeFileReply | None:
    """Parse one byte-counted frame, permitting a short payload only when incomplete.

    Args:
        text: The complete decoded model reply available to the caller.
        incomplete: Whether transport evidence proves generation did not complete normally.

    Returns:
        One exact or explicitly incomplete whole-file reply, otherwise ``None``.
    """
    header, separator, payload = text.partition(_HEADER_SEPARATOR)
    if not separator:
        return None
    lines = header.split("\n")
    if len(lines) != 2:
        return None
    path_line, count_line = lines
    if not path_line.startswith(_FILE_PREFIX) or not count_line.startswith(_BYTE_COUNT_PREFIX):
        return None
    path = path_line.removeprefix(_FILE_PREFIX)
    count_text = count_line.removeprefix(_BYTE_COUNT_PREFIX)
    if not path.strip() or not count_text.isascii() or not count_text.isdecimal():
        return None
    try:
        path.encode("utf-8")
        payload_bytes = payload.encode("utf-8")
    except UnicodeEncodeError:
        return None
    # Byte counts compared as normalized decimal strings, never via int() — see D-EDITS-001.
    available_count_text = str(len(payload_bytes))
    declared_count_text = count_text.lstrip("0") or "0"
    available_count_key = (len(available_count_text), available_count_text)
    declared_count_key = (len(declared_count_text), declared_count_text)
    overlong = available_count_key > declared_count_key
    short = available_count_key < declared_count_key
    if overlong or (short and not incomplete):
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
