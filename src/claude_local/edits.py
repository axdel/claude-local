"""Whole-file extraction + containment writes — raw model text becomes one safe file write.

A weak model returns a whole implementation file as messy raw text. ``extract_files`` turns that
text into ``FileBlock`` records (path + content) with a fence-aware line scan, and ``apply_files``
writes each block to disk through ``paths.resolve_within`` ONLY — the single security boundary of
the loop (D-KEEP-001). In single-file mode a block may target only the one permitted impl path;
anything else, or any path escaping the root, is refused with ``KeepOnlyViolation`` and nothing
is written.

Extraction rules, in precedence order:
  * ``FILE:`` at column 0 (outside a fence) delimits a block and names its path.
  * A block's body is its first fenced ```` ```lang ```` region if present, else its raw lines —
    so prose after the closing fence never leaks into the file.
  * A ``FILE:`` line INSIDE a fence is content, never a split (it is code, e.g. a string).
  * A fence cut off mid-stream (no closing ```` ``` ````) is tolerated: its partial body is kept.
  * Zero markers + exactly ONE fenced region -> one block with ``path=None`` (route to permitted).
  * No markers and no single region (prose only, or 2+ ambiguous regions) -> no blocks -> BLOCKED.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from claude_local.paths import KeepOnlyViolation, resolve_within

if TYPE_CHECKING:
    from pathlib import Path

# A marker is this token at column 0 (a strict protocol token). A fence is this token after
# optional indentation (models often indent fences), matched on either delimiter line.
_MARKER = "FILE:"
_FENCE = "```"


@dataclass(frozen=True, slots=True)
class FileBlock:
    """One extracted file: its declared ``path`` (or ``None`` = the single permitted impl path)
    and the whole-file ``content`` to write."""

    path: str | None
    content: str


def extract_files(text: str) -> list[FileBlock]:
    """Parse raw model text into whole-file blocks; ``[]`` when nothing is extractable."""
    blocks: list[tuple[str, list[str]]] = []  # (path, all lines of the block, fences included)
    loose: list[str] = []  # lines before any marker — the no-marker fallback's search space
    path: str | None = None
    lines: list[str] = []
    in_fence = False
    for line in text.split("\n"):
        if line.lstrip().startswith(_FENCE):
            in_fence = not in_fence  # track fences ONLY to suppress an in-fence FILE: marker
            (lines if path is not None else loose).append(line)
        elif not in_fence and line.startswith(_MARKER):
            if path is not None:
                blocks.append((path, lines))  # close the previous block before opening this one
            path, lines = line[len(_MARKER) :].strip(), []
        else:
            (lines if path is not None else loose).append(line)
    if path is not None:
        blocks.append((path, lines))  # close the final (possibly truncated) block at EOF

    if blocks:
        return [FileBlock(block_path, _body(block_lines)) for block_path, block_lines in blocks]
    regions = _fenced_regions(loose)
    return [FileBlock(None, _render(regions[0]))] if len(regions) == 1 else []


def apply_files(blocks: list[FileBlock], root: Path, permitted: str) -> list[Path]:
    """Write each block through ``resolve_within``; refuse any target but ``permitted``.

    ``root`` is the worktree; ``permitted`` is the one relative impl path the model may write.
    A ``FileBlock`` with ``path=None`` is routed to ``permitted``. Resolution is two-phase — every
    target is validated BEFORE any file is written — so a reply naming even one forbidden path
    writes nothing at all (atomic containment). Returns the resolved paths written, in order.

    Raises:
        KeepOnlyViolation: a block resolves outside ``root`` or to any path but ``permitted`` —
            a fail-closed refusal that writes nothing (D-KEEP-001).
    """
    permitted_target = resolve_within(root, permitted)
    targets: list[Path] = []
    for block in blocks:
        candidate = permitted if block.path is None else block.path
        target = resolve_within(root, candidate)
        if target != permitted_target:
            raise KeepOnlyViolation(candidate, "only the permitted impl path may be written")
        targets.append(target)
    for target, block in zip(targets, blocks, strict=True):
        target.write_text(block.content, encoding="utf-8")  # write only after ALL validate
    return targets


def _fenced_regions(lines: list[str]) -> list[list[str]]:
    """The content lines of each ```` ``` ````-delimited region, in order.

    A region opened but never closed (a truncated final fence) is kept — the loop is bounded by
    a real stream that can be cut anywhere, so a missing terminator is expected, not an error.
    """
    regions: list[list[str]] = []
    current: list[str] | None = None
    for line in lines:
        if line.lstrip().startswith(_FENCE):
            if current is None:
                current = []  # opening a region
            else:
                regions.append(current)  # closing it
                current = None
        elif current is not None:
            current.append(line)
    if current is not None:
        regions.append(current)  # tolerate a truncated (unclosed) final region
    return regions


def _body(lines: list[str]) -> str:
    """A block's whole-file text: its first fenced region if any, else all its raw lines."""
    regions = _fenced_regions(lines)
    return _render(regions[0] if regions else lines)


def _render(lines: list[str]) -> str:
    """Join body lines into file text with exactly one trailing newline (empty stays empty)."""
    text = "\n".join(lines).rstrip("\n")
    return text + "\n" if text else ""
