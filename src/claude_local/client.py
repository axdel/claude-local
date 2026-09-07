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

An optional ``on_delta`` observer makes a decode watchable: it receives each content delta as it
arrives, so a caller can render generation live instead of waiting minutes for one result object.
Deltas are reported RAW — before ``assistant_content`` recovers the reply from a channel
transcript — because the markup is what a reasoning model spends most of its decode on, and a
watcher that only saw the recovered reply would see nothing until the very end (D-PROGRESS-002).
Each delta is reported BEFORE the guard judges it, so the delta that trips a derail is the last
thing the observer sees rather than the one thing it misses.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from claude_local.backend import GenerationSilent
from claude_local.derail import CHARS_PER_TOKEN, DerailGuard, DerailReason
from claude_local.harmony import assistant_content
from claude_local.sse import Delta, Error, Finish, Reasoning, Usage, decode_sse

_LENGTH_FINISH_REASON = "length"

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

    from claude_local.backend import Backend
    from claude_local.types import Budget


@dataclass(frozen=True, slots=True)
class GenerationResult:
    """The metered outcome of one generation — the local half of the economy record.

    ``completion_tokens`` is the server's own count on a clean finish, else a char-count proxy
    with ``tokens_estimated`` set. ``derail_reason`` is the bound that cut the stream, or ``None``
    when the stream ended on its own (clean finish, transport truncation, or an upstream error).
    ``fault`` carries the message from an upstream SSE error frame when one stopped the stream,
    else ``None`` — a server-side fault the loop surfaces as ``FAULTED``, kept distinct from a
    ``derail_reason`` (the model failing) and from silent truncation. Both name a *cause*
    (mirroring ``derail_reason``), and ``fault`` keeps its single name across the client, loop,
    and outcome — the value flows through unrenamed, pairing with the ``FAULTED`` status.

    ``finish_reason`` is the server's own terminal reason from the SSE ``Finish`` frame —
    ``"stop"`` on a clean finish, ``"length"`` when the server hit *its* token cap — or ``None``
    when no such frame arrived (a derail broke before it, transport truncation cut it off, or an
    error frame ended the stream). It is the SERVER's account of why generation ended, distinct on
    both sides: from ``derail_reason``, the CLIENT-side guard abort, and from the char-proxy
    ``truncation`` that fires when the bytes were cut off with no terminal frame at all. Telemetry
    reads it to count how often the server capped a generation at ``length``.
    """

    text: str
    completion_tokens: int
    tokens_estimated: bool
    seconds: float
    derail_reason: DerailReason | None
    fault: str | None = None
    finish_reason: str | None = None

    @property
    def is_length_capped(self) -> bool:
        """Whether the server stopped this generation at its own token limit."""
        return self.finish_reason == _LENGTH_FINISH_REASON

    @property
    def tokens_per_second(self) -> float | None:
        """This generation's decode rate, or ``None`` when no wall-clock time elapsed.

        Both terms are owned here, so the quotient is too — a reader that wants a speed asks
        rather than dividing someone else's fields. ``None`` for a zero-elapsed generation, the
        same guard ``LocalEconomyRecord`` applies to the run-wide mean.
        """
        return self.completion_tokens / self.seconds if self.seconds > 0 else None


class ModelClient:
    """Drives one generation at a time through an injected ``Backend``, metering each call.

    Construct once and reuse: the backend holds the warm connection, and this client only owns
    the per-call orchestration and the ``total_calls`` ledger. The clock and the guard factory
    are injected so timing and derail behavior are deterministic under test. ``on_delta`` is the
    optional live-decode observer; the client neither formats nor throttles what it reports —
    rendering is the caller's, which is what keeps this module free of I/O.
    """

    def __init__(
        self,
        backend: Backend,
        derail_factory: Callable[[Budget, Callable[[], float]], DerailGuard] = DerailGuard,
        now: Callable[[], float] = time.monotonic,
        on_delta: Callable[[str], None] | None = None,
    ) -> None:
        self._backend = backend
        self._derail_factory = derail_factory
        self._now = now
        self._on_delta = on_delta
        self._total_calls = 0

    @property
    def total_calls(self) -> int:
        """Logical generations attempted — the count the economy record reconciles against."""
        return self._total_calls

    @staticmethod
    def _ticking(chunks: Iterable[bytes], guard: DerailGuard) -> Iterator[bytes]:
        """Yield transport chunks, driving the guard's clock on each and stopping when one trips.

        The guard's time bounds are otherwise checked only inside ``feed``, which cannot run until
        a chunk decodes to content. A server holding the socket open with keepalive comments, or
        with ``event:``/``id:`` lines the SSE decoder drops, therefore keeps every bound asleep —
        measured as one content token in 447.2s against a 120s budget. Judging arrival here closes
        that gap at the only layer that sees the bytes.

        The tick precedes the yield so it judges the gap this chunk is ending, and the stream
        stops rather than raising: the caller's loop then finishes normally and reads the latched
        verdict, keeping the abort path identical to the one every other bound already uses.
        """
        for chunk in chunks:
            if guard.tick() is not None:
                return
            yield chunk

    def generate(self, prefix: str, tail: str, budget: Budget) -> GenerationResult:
        """Stream one generation, aborting on the first derail; return its metered result.

        The stream iterator is consumed inline with no lingering reference, so an early abort
        drops its last reference at the break and CPython refcount finalization closes the
        transport synchronously — no explicit close is needed on this runtime (D-CLIENT-001).
        """
        self._total_calls += 1  # a logical call — counted at entry so it survives any later raise
        start = self._now()
        guard = self._derail_factory(budget, self._now)
        parts: list[str] = []
        chars = 0
        server_tokens: int | None = None
        derail_reason: DerailReason | None = None
        fault: str | None = None
        finish_reason: str | None = None
        chunks = self._ticking(self._backend.generate(prefix, tail, budget), guard)
        try:
            for event in decode_sse(chunks):
                if isinstance(event, Delta | Reasoning):
                    # Reasoning is metered and watched exactly like content — it is decode the
                    # model really performed, which the server's own usage trailer bills — but it
                    # is never appended to the reply, or the file parser would read
                    # chain-of-thought as source.
                    if isinstance(event, Delta):
                        parts.append(event.text)
                    chars += len(event.text)
                    if self._on_delta is not None:  # before the verdict, so a derail's cause shows
                        self._on_delta(event.text)
                    derail_reason = guard.feed(event.text)
                    if derail_reason is not None:
                        break  # abort early — stop decoding the moment a bound trips
                elif isinstance(event, Usage):
                    server_tokens = event.completion_tokens
                elif isinstance(event, Finish):
                    finish_reason = event.reason  # the server's own terminal reason (stop/length)
                elif isinstance(event, Error):
                    # An upstream error frame is terminal: surface its message and stop decoding,
                    # so content streamed after it is never read (a server fault, not a derail).
                    fault = event.message
                    break
        except GenerationSilent:
            # The transport reporting the one silence the guard cannot: with no chunk ever
            # arriving, tick never ran, so there is no latched verdict to read below. Whatever
            # bytes did arrive stay in parts and chars, so a partial decode is still metered.
            derail_reason = DerailReason.SILENT
        seconds = self._now() - start
        # A silence trips in the chunk layer, which ends the stream without ever reaching an event,
        # so the loop above has no verdict to report. Reading the latch is what makes a generation
        # that produced nothing at all distinguishable from one that ended cleanly and empty.
        derail_reason = derail_reason or guard.tripped
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
            # A server that leaks the model's channel transcript instead of its user-facing
            # message is normalized here, at the one place the reply text is assembled. Text from
            # a server that behaves passes through byte-identically.
            text=assistant_content("".join(parts)),
            completion_tokens=completion_tokens,
            tokens_estimated=estimated,
            seconds=seconds,
            derail_reason=derail_reason,
            fault=fault,
            finish_reason=finish_reason,
        )
