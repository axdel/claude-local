"""Best-passing snapshot store — locked improvement (a) of the loop.

REPAIR feedback can regress a passing attempt, so the loop must never end worse than the best
state it reached mid-run. ``SnapshotStore`` captures the FULL permitted subtree after each
attempt, ranks every capture by the D-SNAPSHOT-001 key — ``(passed desc, errors asc, failed
asc, index asc)`` — and on exit restores the subtree to the best capture EXACTLY: overwrite
changed files, remove ones a worse attempt added, prune the dirs those removals emptied.
Whole-subtree capture avoids partial-file drift, and a collection-errored attempt (passed 0)
ranks strictly below any runnable partial, so the loop keeps the most-passing code it produced.

Cold path by construction: capture runs once per attempt and restore once on exit — off the
inference hot path — so the store favours a plain in-memory list and ``min`` over any structure
built for speed (E6: do not optimize a cold path).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from claude_local.paths import resolve_within

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from claude_local.runner import TestScore


@dataclass(frozen=True, slots=True)
class SnapshotEntry:
    """One captured attempt: its index, its oracle score, and the subtree bytes at capture time.

    ``files`` maps each captured file's POSIX path (relative to the permitted subtree) to its
    bytes — a value snapshot taken eagerly at ``record`` time, never a live filesystem handle,
    so a later attempt overwriting the same file cannot mutate this entry.
    """

    index: int
    score: TestScore
    files: Mapping[str, bytes]


def _rank_key(entry: SnapshotEntry) -> tuple[int, int, int, int]:
    """The D-SNAPSHOT-001 ordering as a ``min``-able tuple — smaller is better.

    ``-passed`` so more-passing sorts first; then ``errors`` then ``failed`` ascending (a crash
    is worse than a clean assertion failure); then ``index`` ascending so the earliest attempt
    wins an exact tie. A collection-errored attempt has ``passed == 0``, so it ranks strictly
    below any attempt with even one passing test — the loop never keeps a crash over runnable code.
    """
    score = entry.score
    return (-score.passed, score.errors, score.failed, entry.index)


class SnapshotStore:
    """Captures the permitted subtree after each attempt and restores the best on loop exit.

    Single writer of the writable-subtree snapshot (RESOURCE_OWNERSHIP). Containment is validated
    once at construction by ``resolve_within`` — the subtree is the model's permitted root — and
    every capture and restore stays within that realpath-resolved subtree by construction.
    """

    def __init__(self, root: Path, permitted: str) -> None:
        self._subtree = resolve_within(root, permitted)
        self._entries: list[SnapshotEntry] = []

    def record(self, index: int, score: TestScore) -> None:
        """Snapshot the full permitted subtree as attempt ``index``, scored ``score``.

        Reads every file's bytes eagerly (a value snapshot), so a later attempt writing the same
        path never mutates this entry.
        """
        files = {
            path.relative_to(self._subtree).as_posix(): path.read_bytes()
            for path in self._subtree.rglob("*")
            if path.is_file()
        }
        self._entries.append(SnapshotEntry(index=index, score=score, files=files))

    def best(self) -> SnapshotEntry | None:
        """The highest-ranked capture by the D-SNAPSHOT-001 key, or ``None`` if none recorded."""
        if not self._entries:
            return None
        return min(self._entries, key=_rank_key)

    def restore_best(self) -> None:
        """Sync the subtree to exactly the best capture: overwrite, remove extras, prune dirs.

        A no-op when nothing was recorded. Otherwise every file in the best capture is written
        (parents created), every on-disk file NOT in the capture is removed, and every directory
        emptied by those removals is pruned — so the subtree ends byte-identical to the best
        attempt, never a partial mix of it and a later regression.
        """
        best = self.best()
        if best is None:
            return
        wanted = {self._subtree / rel: content for rel, content in best.files.items()}
        for target, content in wanted.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        for path in self._subtree.rglob("*"):
            if path.is_file() and path not in wanted:
                path.unlink()
        self._prune_empty_dirs()

    def _prune_empty_dirs(self) -> None:
        """Remove directories emptied by a restore, deepest first — never the subtree root."""
        by_depth = sorted(self._subtree.rglob("*"), key=lambda p: len(p.parts), reverse=True)
        for path in by_depth:
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()
