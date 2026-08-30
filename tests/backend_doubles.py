"""Test doubles standing in for the model backend.

Shared backend stand-ins several loop tests need, so no test re-implements one — the
backend counterpart of ``factories`` for value objects and ``sse_wire`` for wire bytes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from claude_local.backend import ReplayBackend

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from claude_local.types import Budget


class RecordingReplayBackend:
    """Replay model responses while retaining the exact prefix and tail for each request."""

    def __init__(self, scripts: Sequence[bytes]) -> None:
        self._replay = ReplayBackend(scripts)
        self.calls: list[tuple[str, str]] = []

    def generate(self, prefix: str, tail: str, budget: Budget) -> Iterator[bytes]:
        """Record one request and return its next replayed response stream."""
        self.calls.append((prefix, tail))
        return self._replay.generate(prefix, tail, budget)
