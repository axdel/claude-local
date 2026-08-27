"""The derail guard — cuts a stuck local decode mid-stream, before a full wasted completion.

One guard instance watches one generation. The caller feeds it decoded text deltas as they
stream; ``feed`` returns the first bound that trips (or ``None``), and the caller aborts the
generation on the first non-``None``. Three bounds, checked in order per delta:

  TIMEOUT   — the injected wall clock passed ``start + timeout_s`` (deterministic under a fake
              clock in tests; ``time.monotonic`` in production).
  TOKEN_CAP — decoded chars exceeded ``max_tokens * CHARS_PER_TOKEN``. A lenient *client* backstop
              on decode length; the server's ``max_tokens`` is the primary hard bound (D-PERF-001).
  REPETITION — a large-n line repeated ``REPETITION_THRESHOLD`` times consecutively past a warmup
              (D-DERAIL-001: consecutive exact repetition is a stuck decode; global token-diversity
              is not).

The verdict depends only on the *concatenated* text, never on how the server chunked it into
deltas — a hard invariant (``CLAUDE.md`` → inference hot path; D-DERAIL-002). Token count is
proxied by char count, not delta count; repetition scans lines reconstructed across delta
boundaries, each armed by its absolute stream position — none of which vary with chunking.
"""

from __future__ import annotations

import enum
import time
from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from claude_local.types import Budget


# Minimum chars for a line to count toward repetition. Short lines (`}`, `pass`, blank) repeat
# benignly in valid code, so they are transparent — neither counted nor reset the run.
REPETITION_NGRAM = 24
# Consecutive exact repeats of a large-n line that trip the guard. Also sizes the ring buffer:
# the deque holds exactly this many recent lines, so "full and all-identical" IS the threshold.
REPETITION_THRESHOLD = 6
# Chars of output before the detector arms. A weak model often opens with a repetitive preamble
# (imports, boilerplate) that is not a derail; skipping it kills that false-positive class.
WARMUP_CHARS = 200
# Coarse chars-per-token proxy for the client-side decode backstop. The server's max_tokens
# is the real hard bound (D-PERF-001); this proxy only needs to be chunking-invariant.
CHARS_PER_TOKEN = 4


class DerailReason(enum.Enum):
    """Why a generation was cut short. Stable lowercase values — telemetry records these."""

    REPETITION = "repetition"
    TOKEN_CAP = "token_cap"  # noqa: S105 — enum value, not a credential (name contains "TOKEN")
    TIMEOUT = "timeout"


class DerailGuard:
    """Detects a stuck decode from streamed text deltas; latches on the first bound it trips.

    Construct one per generation with the run's ``Budget`` and (optionally) an injected clock.
    Feed decoded text deltas in order; the first non-``None`` return is terminal and every later
    ``feed`` returns that same reason, so the caller aborts exactly once.
    """

    def __init__(self, budget: Budget, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._deadline = now() + budget.timeout_s
        self._token_cap_chars = budget.max_tokens * CHARS_PER_TOKEN
        self._chars = 0  # total decoded chars fed — the token-cap proxy (chunking-invariant)
        self._pending: list[str] = []  # deltas buffered since the last completed line
        self._line_base = 0  # abs position of _pending's start (warmup by position)
        self._recent: deque[str] = deque(maxlen=REPETITION_THRESHOLD)
        self._tripped: DerailReason | None = None

    def feed(self, text_delta: str) -> DerailReason | None:
        """Consume one decoded text delta; return the first bound tripped, or ``None``."""
        if self._tripped is not None:
            return self._tripped
        if self._now() > self._deadline:
            return self._trip(DerailReason.TIMEOUT)
        self._chars += len(text_delta)
        if self._chars > self._token_cap_chars:
            return self._trip(DerailReason.TOKEN_CAP)
        return self._scan_for_repetition(text_delta)

    def _trip(self, reason: DerailReason) -> DerailReason:
        """Latch the terminal verdict so every later feed reports it."""
        self._tripped = reason
        return reason

    def _scan_for_repetition(self, text_delta: str) -> DerailReason | None:
        """Reconstruct completed lines across delta boundaries and detect a consecutive run.

        Deltas accumulate in ``_pending`` and are joined only when one carries a newline (O(1)
        appends, one join per completed line — no per-char O(n^2)). Each completed line is armed
        by its absolute end position in the stream, so warmup is independent of how deltas split.
        """
        self._pending.append(text_delta)
        if "\n" not in text_delta:
            return None  # no line completed yet — keep buffering, defer the join
        *lines, remainder = "".join(self._pending).split("\n")
        self._pending = [remainder]
        pos = self._line_base
        for line in lines:
            pos += len(line) + 1  # absolute position of the char past this line's newline
            if pos <= WARMUP_CHARS:
                continue  # line ends inside the warmup preamble — detector not yet armed
            if len(line) < REPETITION_NGRAM:
                continue  # short line — transparent: neither counts nor resets the run
            self._recent.append(line)
            if len(self._recent) == REPETITION_THRESHOLD and len(set(self._recent)) == 1:
                return self._trip(DerailReason.REPETITION)
        self._line_base = pos  # remainder begins here; reached only when no run tripped
        return None
