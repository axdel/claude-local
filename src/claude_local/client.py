"""The model client — one warm generation call: stream, decode, watch, and meter.

``ModelClient.generate`` runs a single logical generation: it streams raw bytes from a
``Backend``, decodes them with ``sse``, feeds each content delta to a fresh ``DerailGuard``,
and aborts the stream the instant a bound trips. Every call yields a ``GenerationResult`` — the
decoded text, a completion-token count, and wall-clock timing — for the local half of the
economy record.

The token count is never guessed away: a cleanly finished stream carries the server's own
``usage`` count; a stream with no usage block (transport truncation, an upstream error frame, or
a derail cut before the trailer) falls back to a char-count proxy flagged ``tokens_estimated``.
An aborted call still cost decode time, so its tokens are counted, never dropped (D-TELEMETRY-001).
``total_calls`` counts logical generations — incremented at entry so it survives a mid-call raise.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from claude_local.derail import CHARS_PER_TOKEN, DerailGuard
from claude_local.sse import Delta, Usage, decode_sse

if TYPE_CHECKING:
    from collections.abc import Callable

    from claude_local.backend import Backend
    from claude_local.derail import DerailReason
    from claude_local.types import Budget


@dataclass(frozen=True, slots=True)
class GenerationResult:
    """The metered outcome of one generation — the local half of the economy record.

    ``completion_tokens`` is the server's own count on a clean finish, else a char-count proxy
    with ``tokens_estimated`` set. ``derail_reason`` is the bound that cut the stream, or ``None``
    when the stream ended on its own (clean finish, transport truncation, or an upstream error).
    """

    text: str
    completion_tokens: int
    tokens_estimated: bool
    seconds: float
    derail_reason: DerailReason | None


class ModelClient:
    """Drives one generation at a time through an injected ``Backend``, metering each call.

    Construct once and reuse: the backend holds the warm connection, and this client only owns
    the per-call orchestration and the ``total_calls`` ledger. The clock and the guard factory
    are injected so timing and derail behavior are deterministic under test.
    """

    def __init__(
        self,
        backend: Backend,
        derail_factory: Callable[[Budget, Callable[[], float]], DerailGuard] = DerailGuard,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._backend = backend
        self._derail_factory = derail_factory
        self._now = now
        self._total_calls = 0

    @property
    def total_calls(self) -> int:
        """Logical generations attempted — the count the economy record reconciles against."""
        return self._total_calls

    def generate(self, prefix: str, tail: str, budget: Budget) -> GenerationResult:
        """Stream one generation, aborting on the first derail; return its metered result."""
        self._total_calls += 1  # a logical call — counted at entry so it survives any later raise
        start = self._now()
        guard = self._derail_factory(budget, self._now)
        parts: list[str] = []
        chars = 0
        server_tokens: int | None = None
        derail_reason: DerailReason | None = None
        for event in decode_sse(self._backend.generate(prefix, tail, budget)):
            if isinstance(event, Delta):
                parts.append(event.text)
                chars += len(event.text)
                derail_reason = guard.feed(event.text)
                if derail_reason is not None:
                    break  # abort early — stop decoding the moment a bound trips
            elif isinstance(event, Usage):
                server_tokens = event.completion_tokens
        seconds = self._now() - start
        if server_tokens is None:
            # No trustworthy server count (truncation, upstream error, or a derail cut the stream
            # before the trailer). Proxy from decoded chars, ceil so any content reports >= 1 token
            # and only a truly empty decode reports 0 (D-TELEMETRY-001 — never silently drop cost).
            completion_tokens = (chars + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN
            estimated = True
        else:
            completion_tokens = server_tokens
            estimated = False
        return GenerationResult(
            text="".join(parts),
            completion_tokens=completion_tokens,
            tokens_estimated=estimated,
            seconds=seconds,
            derail_reason=derail_reason,
        )
