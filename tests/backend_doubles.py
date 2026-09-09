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


class FramedReplayBackend:
    """Replay one captured stream frame by frame, the way a real transport delivers it.

    ``ReplayBackend`` hands the whole capture over as a single chunk, which is the right shape
    for asserting what a stream decodes to but the wrong one for anything about time: the client
    drives the derail guard's clock once per *chunk*, so a one-chunk replay produces exactly one
    tick and no test over it can distinguish a stream that kept arriving from one that went
    quiet. Splitting on the SSE frame delimiter reproduces the real cadence — one tick per frame,
    which is what a server that is decoding normally actually looks like from here.
    """

    _DELIMITER = b"\n\n"

    def __init__(self, script: bytes) -> None:
        self._frames = tuple(
            frame + self._DELIMITER for frame in script.split(self._DELIMITER) if frame
        )

    def generate(self, prefix: str, tail: str, budget: Budget) -> Iterator[bytes]:
        """Serve the captured stream as its own frames, in order."""
        del prefix, tail, budget  # a recording answers whatever it was recorded against
        return iter(self._frames)
