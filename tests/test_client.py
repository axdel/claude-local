"""Tests for the model client (``claude_local.client``).

One ``generate`` call streams backend bytes, decodes them, watches for a derail, and always
produces a token count + wall-clock timing for the economy record. Every expected value is
derived independently of the implementation: the SSE fixtures are schema-derived captures with
documented provenance, the clean-path token count is the server's own ``usage`` number, and each
abort's proxy is a hand-calculated ``ceil(chars / 4)`` (7, 5, 10 chars → 2, 2, 3 — none a multiple
of four, so a floor mutant diverges). Time is an injected clock, so elapsed seconds is exact.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from factories import build_budget

from claude_local.backend import ReplayBackend, ReplayExhausted
from claude_local.client import GenerationResult, ModelClient
from claude_local.derail import DerailReason

FIXTURES = Path(__file__).parent / "fixtures" / "sse"


def load_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class ScriptedClock:
    """Returns each scripted instant in turn, repeating the last — deterministic elapsed time."""

    def __init__(self, *times: float) -> None:
        self._times = times or (0.0,)
        self._i = 0

    def __call__(self) -> float:
        value = self._times[self._i]
        self._i = min(self._i + 1, len(self._times) - 1)
        return value


class AdvancingClock:
    """Advances a fixed step on every call — drives the guard's deadline past deterministically."""

    def __init__(self, step: float) -> None:
        self._t = 0.0
        self._step = step

    def __call__(self) -> float:
        t = self._t
        self._t += self._step
        return t


# --- Clean completion: the server's own usage count -------------------------------


def test_clean_stream_reports_server_usage_and_full_text() -> None:
    client = ModelClient(
        ReplayBackend([load_bytes("complete_stream.bytes")]), now=ScriptedClock(0.0)
    )
    result = client.generate("prefix", "tail", build_budget())
    # Oracle: the two deltas concatenate to "Artificial intelligence"; the server's own
    # usage.completion_tokens=2 is trusted verbatim (not estimated); nothing derailed.
    assert result == GenerationResult(
        text="Artificial intelligence",
        completion_tokens=2,
        tokens_estimated=False,
        seconds=0.0,
        derail_reason=None,
    )


# --- No usage block: fall back to the char proxy ----------------------------------


def test_truncated_stream_falls_back_to_char_proxy() -> None:
    client = ModelClient(
        ReplayBackend([load_bytes("aborted_midstream.bytes")]), now=ScriptedClock(0.0)
    )
    result = client.generate("prefix", "tail", build_budget())
    # Oracle: two deltas decode to "Partial" (7 chars); the trailing frame is truncated, so
    # no Usage arrives — count falls back to ceil(7/4)=2, estimated. Truncation is not a derail.
    assert result == GenerationResult(
        text="Partial",
        completion_tokens=2,
        tokens_estimated=True,
        seconds=0.0,
        derail_reason=None,
    )


def test_mid_stream_error_is_surfaced_and_stops_the_stream() -> None:
    client = ModelClient(
        ReplayBackend([load_bytes("mid_stream_error.bytes")]), now=ScriptedClock(0.0)
    )
    result = client.generate("prefix", "tail", build_budget())
    # Oracle: the fixture streams delta "Hello" then an upstream error frame whose message is
    # "context length exceeded" (schema-derived provenance — tests/fixtures/sse/README.md).
    # The client must SURFACE that message on fault, not silently fold it into a proxy count as
    # if it were a truncation. The error is not a derail (derail_reason stays None); "Hello"
    # (5 chars) with no usage frame still proxies to ceil(5/4)=2, estimated.
    assert result == GenerationResult(
        text="Hello",
        completion_tokens=2,
        tokens_estimated=True,
        seconds=0.0,
        derail_reason=None,
        fault="context length exceeded",
    )


def test_server_error_frame_stops_decoding_the_rest_of_the_stream() -> None:
    # An error frame is terminal: content the server streams AFTER it must not be decoded. A delta
    # past the error frame proves the client breaks on the error rather than reading on — had it
    # merely captured the message and continued, text would be "beforeAFTER".
    stream = (
        b'data: {"choices":[{"delta":{"content":"before"}}]}\n\n'
        b'data: {"error":{"message":"overloaded"}}\n\n'
        b'data: {"choices":[{"delta":{"content":"AFTER"}}]}\n\n'
        b"data: [DONE]\n\n"
    )
    client = ModelClient(ReplayBackend([stream]), now=ScriptedClock(0.0))
    result = client.generate("prefix", "tail", build_budget())
    # Oracle: "before" precedes the error; "AFTER" is past the terminal error frame and excluded.
    assert result.text == "before"
    assert result.fault == "overloaded"
    assert result.derail_reason is None


def test_char_proxy_is_exact_at_a_token_multiple() -> None:
    # A single 8-char delta, no usage frame: 8/4 is exactly 2 with no rounding. An exact multiple
    # pins the ceil offset — a +1/-1 drift in the rounding term would read 3 — which the
    # non-multiple cases above (7, 5, 10 chars) cannot see. The content is arbitrary padding.
    stream = b'data: {"choices":[{"delta":{"content":"abcdefgh"}}]}\n\n'
    client = ModelClient(ReplayBackend([stream]), now=ScriptedClock(0.0))
    result = client.generate("prefix", "tail", build_budget())
    assert result == GenerationResult(
        text="abcdefgh",
        completion_tokens=2,
        tokens_estimated=True,
        seconds=0.0,
        derail_reason=None,
    )


# --- Derail aborts: stop the stream, estimate the count ---------------------------


def test_token_cap_derail_stops_the_stream_and_estimates() -> None:
    # cap = max_tokens(2) * CHARS_PER_TOKEN(4) = 8 chars; the first delta "Artificial" (10)
    # exceeds it, so the guard trips after that delta — the client stops before the trailer.
    client = ModelClient(
        ReplayBackend([load_bytes("complete_stream.bytes")]), now=ScriptedClock(0.0)
    )
    result = client.generate("prefix", "tail", build_budget(max_tokens=2))
    # Oracle: only "Artificial" (10 chars) was consumed; proxy ceil(10/4)=3; derailed → estimated.
    assert result == GenerationResult(
        text="Artificial",
        completion_tokens=3,
        tokens_estimated=True,
        seconds=0.0,
        derail_reason=DerailReason.TOKEN_CAP,
    )


def test_timeout_derail_uses_the_clients_injected_clock() -> None:
    # The client must feed ITS clock to the guard: an advancing clock (step >> timeout) is
    # already past the deadline by the first feed. Had the client wired time.monotonic instead,
    # no timeout would fire in-test and derail_reason would be None — so this pins the wiring.
    client = ModelClient(
        ReplayBackend([load_bytes("complete_stream.bytes")]),
        now=AdvancingClock(step=1_000_000.0),
    )
    result = client.generate("prefix", "tail", build_budget(timeout_s=1.0))
    assert result.derail_reason is DerailReason.TIMEOUT
    assert result.tokens_estimated is True


# --- Timing and the logical-call ledger -------------------------------------------


def test_seconds_is_the_elapsed_wall_clock() -> None:
    # The injected clock reads 10.0 at entry, 15.0 thereafter → elapsed 5.0, derived from the
    # clock, not the implementation under test.
    client = ModelClient(
        ReplayBackend([load_bytes("complete_stream.bytes")]), now=ScriptedClock(10.0, 15.0)
    )
    result = client.generate("prefix", "tail", build_budget())
    assert result.seconds == 5.0


def test_total_calls_counts_each_logical_generation() -> None:
    client = ModelClient(
        ReplayBackend([load_bytes("complete_stream.bytes"), load_bytes("complete_stream.bytes")]),
        now=ScriptedClock(0.0),
    )
    assert client.total_calls == 0
    client.generate("prefix", "tail", build_budget())
    client.generate("prefix", "tail", build_budget())
    assert client.total_calls == 2


def test_total_calls_counts_a_generation_that_raises() -> None:
    # An over-read raises ReplayExhausted the moment the client touches the backend; the
    # logical call still happened, so it is counted at entry — the increment precedes the raise.
    client = ModelClient(ReplayBackend([]), now=ScriptedClock(0.0))
    with pytest.raises(ReplayExhausted):
        client.generate("prefix", "tail", build_budget())
    assert client.total_calls == 1
