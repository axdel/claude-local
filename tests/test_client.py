"""Tests for the model client (``claude_local.client``).

One ``generate`` call streams backend bytes, decodes them, watches for a derail, and always
produces a token count + wall-clock timing for the economy record. Every expected value is
derived independently of the implementation: the SSE fixtures are schema-derived captures with
documented provenance, the clean-path token count is the server's own ``usage`` number, and each
abort's proxy is a hand-calculated ``ceil(chars / 4)`` (7, 5, 10 chars → 2, 2, 3 — none a multiple
of four, so a floor mutant diverges). Time is an injected clock, so elapsed seconds is exact.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from backend_doubles import FramedReplayBackend
from factories import build_budget, build_generation_result

from claude_local.backend import ReplayBackend, ReplayExhausted
from claude_local.client import GenerationResult, ModelClient
from claude_local.derail import DerailReason
from claude_local.types import Budget

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
        finish_reason="stop",
    )


# --- A leaked channel transcript is normalized at this seam -----------------------


def test_a_streamed_harmony_transcript_yields_only_the_assistant_message() -> None:
    """The captured wire, end to end: transcript in over SSE, assistant content out.

    `harmony_channel_stream.bytes` is a real recorded session, so this is the seam test the
    `harmony` unit tests cannot be: they call the function directly and would all stay green if
    the call were deleted from `generate`. Oracle for the text is OpenAI's published harmony
    grammar — the answer is the `final` channel's content — and for the count, the server's own
    `usage.completion_tokens=53`, which meters the whole transcript because the model really did
    decode all of it.

    The fixture also pins the reason this defect was invisible: the server reports a clean
    `stop` and never hit its token cap, so every completion signal reads healthy and nothing
    upstream of here could tell that the reply was unusable.
    """
    client = ModelClient(
        ReplayBackend([load_bytes("harmony_channel_stream.bytes")]), now=ScriptedClock(0.0)
    )

    # The exact prompt the capture was recorded with (see the fixtures README).
    result = client.generate(
        "You are a terse assistant.", "Reply with exactly: OK", build_budget()
    )

    assert result.text == "OK"
    assert result.finish_reason == "stop"
    assert result.is_length_capped is False
    assert result.completion_tokens == 53
    assert result.tokens_estimated is False


# --- Reasoning channel: thinking beside the reply, not inside it -------------------


def test_a_streamed_reasoning_channel_yields_only_the_answer() -> None:
    """The structured twin of the harmony capture: a separate wire field, not inline markup.

    ``reasoning_channel_stream.bytes`` is a real recorded session whose server streams
    chain-of-thought on ``choices[].delta.reasoning_content`` while ``delta.content`` stays null.
    Oracle for the text: the recording carries exactly two content deltas, ``"\\n\\n"`` and
    ``"OK"``, so the reply is their concatenation — counted off the recorded bytes, never read
    from the decoder's output. Oracle for the count: the server's own
    ``usage.completion_tokens=29``, which meters the 25 reasoning tokens too, because the model
    really did decode them.

    The clock is frozen deliberately. This pins what the stream assembles to; whether the guard
    stays awake through it is the separate concern below, and conflating them would let one
    assertion pass for the other's reason.
    """
    client = ModelClient(
        ReplayBackend([load_bytes("reasoning_channel_stream.bytes")]), now=ScriptedClock(0.0)
    )

    # The exact prompt the capture was recorded with (see the fixtures README).
    result = client.generate(
        "You are a terse assistant.", "Reply with exactly: OK", build_budget()
    )

    assert result.text == "\n\nOK"
    assert result.finish_reason == "stop"
    assert result.completion_tokens == 29
    assert result.tokens_estimated is False


def test_reasoning_tokens_are_metered_against_the_token_cap() -> None:
    """Thinking is cost, so the guard's cap must see it — the server already bills it.

    Oracle: the recording carries 89 characters of reasoning against 4 of content, and the cap is
    ``max_tokens * CHARS_PER_TOKEN`` = 20 x 4 = 80. Reasoning alone therefore crosses it and
    content alone cannot come close, so a TOKEN_CAP verdict here can only mean the reasoning was
    metered. Both numbers are counted off the fixture; neither comes from running the client.

    Without this, a model that thinks for 30k tokens and answers in 200 is billed for 200 by
    every bound the loop owns, and the one guarantee the derail guard exists to make — that
    decode is bounded by construction — quietly stops holding for reasoning models.
    """
    client = ModelClient(
        ReplayBackend([load_bytes("reasoning_channel_stream.bytes")]), now=ScriptedClock(0.0)
    )

    result = client.generate("prefix", "tail", build_budget(max_tokens=20))

    assert result.derail_reason is DerailReason.TOKEN_CAP


def test_a_model_streaming_only_reasoning_is_not_judged_silent() -> None:
    """Reasoning is arrival: a model decoding chain-of-thought is working, not hung.

    This is the defect that scored a working flagship model 0/7. The stall bound measures the gap
    since the last *content*, and reasoning deltas never reached it, so seven benchmark cases
    died at exactly 180.0s apiece while the server streamed normally throughout.

    Oracle: the recording opens with 25 consecutive reasoning deltas before its first content
    delta, and the frames are replayed one per chunk, as a transport delivers them. Under a clock
    advancing 10s per reading, total elapsed passes the 180s bound long before that first content
    arrives, while no single gap between deltas approaches it. A stream whose deltas keep landing
    is not silent by the bound's own definition. The deadline is set far past the run so TIMEOUT
    cannot stand in for the verdict, and the default 2048-token cap is far above the recording's
    93 fed characters so TOKEN_CAP cannot either.
    """
    client = ModelClient(
        FramedReplayBackend(load_bytes("reasoning_channel_stream.bytes")),
        now=AdvancingClock(step=10.0),
    )

    result = client.generate("prefix", "tail", build_budget(timeout_s=100_000.0))

    assert result.derail_reason is None
    assert result.text == "\n\nOK"


# --- Finish frame: the server's own terminal reason -------------------------------


@pytest.mark.parametrize(
    ("finish_reason", "expected_length_capped"),
    [
        pytest.param(None, False, id="no-finish"),
        pytest.param("length", True, id="length-cap"),
        pytest.param("stop", False, id="clean-stop"),
        pytest.param("tool_calls", False, id="other-terminal-reason"),
        pytest.param([], False, id="unhashable-defense-in-depth"),
    ],
)
def test_generation_result_owns_length_cap_semantics_without_hashing(
    finish_reason: object,
    expected_length_capped: bool,
) -> None:
    """Only an exact ``"length"`` is the server's own token cap; every other reason is not.

    Oracle: the OpenAI chat-completions ``finish_reason`` vocabulary — ``length`` alone means the
    server truncated at its cap. The unhashable case is defense in depth: the comparison must stay
    an ``==`` against one string, never a ``in {...}`` membership test that would raise on a list.
    """
    generation = build_generation_result(finish_reason=finish_reason)

    assert generation.is_length_capped is expected_length_capped


def test_finish_reason_length_is_captured_from_the_terminal_event() -> None:
    # A server that stops at its own token cap emits Finish("length"); the client records that
    # reason verbatim. Oracle: the OpenAI SSE contract — finish_reason "length" means the server
    # hit max_tokens — read off the wire, never from the client. A second, distinct reason
    # ("length" here vs "stop" in the clean-finish test) proves the client copies event.reason
    # rather than pinning one value. No usage frame, so tokens fall back to the char proxy — that
    # is orthogonal; a server length cap is not a client derail (derail_reason stays None).
    stream = (
        b'data: {"choices":[{"delta":{"content":"Partial"},"finish_reason":"length"}]}\n\n'
        b"data: [DONE]\n\n"
    )
    client = ModelClient(ReplayBackend([stream]), now=ScriptedClock(0.0))
    result = client.generate("prefix", "tail", build_budget())
    assert result.finish_reason == "length"
    assert result.derail_reason is None


def test_derail_before_the_finish_frame_leaves_finish_reason_unset() -> None:
    # The guard aborts on "Artificial" (10 chars > cap 8) — BEFORE the terminal frame decodes — so
    # the server never gets to report a reason. Oracle: a client-side derail and a server-side
    # finish_reason are distinct signals; cutting the stream early means finish_reason is None, not
    # inherited from a frame that was never read. Pins that the two never conflate.
    client = ModelClient(
        ReplayBackend([load_bytes("complete_stream.bytes")]), now=ScriptedClock(0.0)
    )
    result = client.generate("prefix", "tail", build_budget(max_tokens=2))
    assert result.derail_reason is DerailReason.TOKEN_CAP
    assert result.finish_reason is None


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


# --- Per-generation decode rate ---------------------------------------------------


@pytest.mark.parametrize(
    ("completion_tokens", "seconds", "expected"),
    [(100, 4.0, 25.0), (3, 0.5, 6.0), (0, 2.0, 0.0), (7, 0.0, None)],
)
def test_a_generation_reports_its_own_decode_rate(
    completion_tokens: int, seconds: float, expected: float | None
) -> None:
    """Oracle: a rate is tokens over seconds — 100 in 4.0s is 25.0/s, 3 in 0.5s is 6.0/s.

    The result already owns both terms, so the quotient belongs here rather than in every reader
    that wants to show a speed. Zero elapsed seconds has no rate to report, and is guarded to
    ``None`` rather than raising — the same guard the aggregate record applies to its own mean.
    """
    generation = build_generation_result(completion_tokens=completion_tokens, seconds=seconds)

    assert generation.tokens_per_second == expected


# --- Live decode observation: deltas as they arrive -------------------------------


def test_every_content_delta_reaches_the_observer_in_stream_order() -> None:
    """Oracle: the captured stream's own content fields — "Artificial", then " intelligence".

    Each arrives as its own call, which is what makes a decode watchable: a client that buffered
    the reply and announced it once at the end would report a single joined chunk and pass any
    assertion written against the concatenation.
    """
    seen: list[str] = []
    client = ModelClient(
        ReplayBackend([load_bytes("complete_stream.bytes")]),
        now=ScriptedClock(0.0),
        on_delta=seen.append,
    )

    client.generate("prefix", "tail", build_budget())

    assert seen == ["Artificial", " intelligence"]


def test_the_delta_that_trips_the_guard_is_reported_before_the_stream_aborts() -> None:
    """A watcher must see the text that caused a derail — that text is the whole diagnostic.

    Oracle: the cap is max_tokens(2) x CHARS_PER_TOKEN(4) = 8 chars and the first delta
    "Artificial" is 10, so the guard trips on it. Reporting only after the guard's verdict would
    withhold exactly the delta a reader needs; the second delta is never decoded, so it must not
    appear either.
    """
    seen: list[str] = []
    client = ModelClient(
        ReplayBackend([load_bytes("complete_stream.bytes")]),
        now=ScriptedClock(0.0),
        on_delta=seen.append,
    )

    result = client.generate("prefix", "tail", build_budget(max_tokens=2))

    assert result.derail_reason is DerailReason.TOKEN_CAP
    assert seen == ["Artificial"]


def test_deltas_are_reported_raw_rather_than_channel_normalised() -> None:
    """The live view shows what the model emits, not the reply recovered from it afterwards.

    Oracle: this captured gpt-oss session opens its channel transcript with the literal
    ``<|channel|>`` marker and carries ``OK`` as its final channel's message. ``assistant_content``
    collapses the whole transcript to that reply, so deltas taken from the normalised text could
    not begin with the marker — and a watcher would lose the analysis channel, which is most of
    what there is to watch on a reasoning model.
    """
    seen: list[str] = []
    client = ModelClient(
        ReplayBackend([load_bytes("harmony_channel_stream.bytes")]),
        now=ScriptedClock(0.0),
        on_delta=seen.append,
    )

    result = client.generate("prefix", "tail", build_budget(max_tokens=4096))

    assert seen[0] == "<|channel|>"
    assert result.text == "OK"


def test_an_unobserved_generation_is_unchanged() -> None:
    """The observer is optional: omitting it must leave the metered result byte-identical.

    Oracle: the clean-path expectations already pinned above — the server's own usage count of 2
    and the two deltas' concatenation — neither of which involves an observer.
    """
    client = ModelClient(
        ReplayBackend([load_bytes("complete_stream.bytes")]), now=ScriptedClock(0.0)
    )

    result = client.generate("prefix", "tail", build_budget())

    assert result.text == "Artificial intelligence"
    assert result.completion_tokens == 2


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


def test_a_stream_of_content_free_bytes_is_cut_at_the_stall_bound() -> None:
    """Bytes that decode to nothing still drive the guard, so a warm socket cannot outlast it.

    The stream is SSE keepalive comments — real wire bytes a server sends to hold a connection
    open, which ``decode_sse`` drops (``sse.py:105``) because a comment carries no payload. The
    client's event loop therefore runs zero iterations and can never call ``feed``, so unless the
    chunk itself drives the guard, no bound is reachable: this is the shape that produced 1 content
    token in 447.2s under a 120s budget.

    The frames arrive separately because that is the claim: a socket that KEPT delivering. One
    chunk cannot state it — the first arrival only starts the silence clock, so a stream judged
    from a single tick is indistinguishable from one that never began (``derail.py``).

    Oracle: the guard's stall bound is 180s of silence measured from the first byte, so the second
    keepalive landing 200s after the first is past it, and the deadline is set far beyond that so
    the verdict can only be STALLED. The expected text is empty because a comment yields no delta —
    derived from the SSE contract, not from running the decoder.
    """
    client = ModelClient(
        FramedReplayBackend(b": keep-alive\n\n: keep-alive\n\n"),
        now=ScriptedClock(0.0, 0.0, 0.0, 200.0),
    )

    result = client.generate("prefix", "tail", build_budget(timeout_s=100_000.0))

    assert result.derail_reason is DerailReason.STALLED
    assert result.text == ""


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


# --- Resource lifecycle: the transport is released on abort ------------------------


def test_aborted_stream_releases_the_transport_before_generate_returns() -> None:
    # Resource-lifecycle contract (D-CLIENT-001): on an early abort the client must release the
    # byte stream by the time generate() returns. HttpxBackend keeps ONE warm connection reused
    # across generations, so a stream left pinned until a later GC would starve the next call.
    # The backend's generator records GeneratorExit in a finally — the exact analog of
    # HttpxBackend's `with client.stream(...)` __exit__; consuming it with no lingering reference
    # closes it synchronously at the abort. The oracle is the confinement requirement (an aborted
    # read closes its source), derived without running the client.
    closed: list[bool] = []

    def recording_stream() -> Iterator[bytes]:
        try:
            yield b'data: {"choices":[{"delta":{"content":"Artificial"}}]}\n\n'  # 10 chars > cap
            yield b'data: {"choices":[{"delta":{"content":"never"}}]}\n\n'  # unreached
        finally:
            closed.append(True)

    class RecordingBackend:
        def generate(self, prefix: str, tail: str, budget: Budget) -> Iterator[bytes]:
            del prefix, tail, budget  # the stream is scripted, not derived from the request
            return recording_stream()

    client = ModelClient(RecordingBackend(), now=ScriptedClock(0.0))
    result = client.generate("prefix", "tail", build_budget(max_tokens=2))
    assert result.derail_reason is DerailReason.TOKEN_CAP  # the abort actually fired
    assert closed == [True]  # and the transport was released by the time generate() returned


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
