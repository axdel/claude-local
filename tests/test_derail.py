"""Tests for the derail guard (``claude_local.derail``).

The guard watches one generation's decoded text deltas and reports the first of three bounds
that trips: REPETITION (a large-n line repeated consecutively past a warmup), TOKEN_CAP (a
char-proxy backstop on the decode length), and TIMEOUT (an injected wall clock). Every expected
value is derived independently of the implementation: the boundary probes hardcode the contract
constants (24 / 6 / 40 chars) so a mutated constant diverges the expectation; the verdict is
asserted invariant to how the stream is chunked (a metamorphic oracle); and the two-fixture
oracle pins the review's explicit ask — a real derail fires, valid-but-repetitive output does not.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from factories import build_budget

from claude_local.derail import SILENCE_TIMEOUT_S, DerailGuard, DerailReason

FIXTURES = Path(__file__).parent / "fixtures" / "derail"

# Distinct, large-n lines totalling comfortably more than WARMUP_CHARS — arms the detector
# without itself repeating, so a following run of identical lines is what trips it.
WARMUP_FILLER = "".join(f"warm-up line {i:02d} with distinct padding zzz\n" for i in range(12))

# A plausible stuck-decode line: substantial (39 chars, well past the large-n floor) and exact.
LOOP_LINE = "        result.append(transform(item))"


class FakeClock:
    """A settable stand-in for ``time.monotonic`` — drives the timeout path deterministically."""

    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _guard(
    *, max_tokens: int = 1_000_000, generation_timeout_s: float = 1_000_000.0
) -> DerailGuard:
    """A guard with a frozen clock and generous caps, so only REPETITION can trip by default."""
    return DerailGuard(
        build_budget(max_tokens=max_tokens, generation_timeout_s=generation_timeout_s),
        now=FakeClock(),
    )


def _repeat(line: str, count: int) -> str:
    return (line + "\n") * count


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def drive(guard: DerailGuard, text: str, chunk_size: int | None = None) -> DerailReason | None:
    """Feed ``text`` whole or in fixed-size chunks; return the first reason (or None)."""
    if chunk_size is None:
        deltas = [text]
    else:
        deltas = [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]
    for delta in deltas:
        reason = guard.feed(delta)
        if reason is not None:
            return reason
    return None


# --- DerailReason: the closed set of bounds ---------------------------------------


def test_reason_enumerates_exactly_the_four_bounds() -> None:
    # Oracle: the guard enforces exactly these four bounds — nothing more, nothing less.
    assert {r.name for r in DerailReason} == {"REPETITION", "TOKEN_CAP", "TIMEOUT", "SILENT"}


@pytest.mark.parametrize(
    ("member", "value"),
    [
        (DerailReason.REPETITION, "repetition"),
        (DerailReason.TOKEN_CAP, "token_cap"),
        (DerailReason.TIMEOUT, "timeout"),
        (DerailReason.SILENT, "silent"),
    ],
)
def test_reason_values_are_stable_lowercase(member: DerailReason, value: str) -> None:
    # Oracle: stable serialized values — telemetry writes these into the economy record.
    assert member.value == value


# --- REPETITION: consecutive exact repeats of a large-n line ----------------------


def test_consecutive_identical_large_lines_after_warmup_trip_repetition() -> None:
    reason = drive(_guard(), WARMUP_FILLER + _repeat(LOOP_LINE, 6))
    assert reason is DerailReason.REPETITION


def test_five_consecutive_repeats_stay_below_threshold() -> None:
    # Five identical large-n lines — one short of the six-repeat threshold (pins THRESHOLD=6).
    assert drive(_guard(), WARMUP_FILLER + _repeat(LOOP_LINE, 5)) is None


def test_a_different_large_line_resets_the_consecutive_run() -> None:
    other = "        result.append(fallback(item))"  # distinct large-n line
    # Five repeats, an interruption, five more — no run of six *consecutive* identical lines.
    text = WARMUP_FILLER + _repeat(LOOP_LINE, 5) + other + "\n" + _repeat(LOOP_LINE, 5)
    assert drive(_guard(), text) is None


def test_line_of_exactly_ngram_length_counts_as_large_n() -> None:
    # A line of exactly REPETITION_NGRAM (24) chars is large enough to count.
    assert drive(_guard(), WARMUP_FILLER + _repeat("x" * 24, 6)) is DerailReason.REPETITION


def test_line_one_below_ngram_length_never_counts() -> None:
    # A 23-char line is below the large-n floor: ignored no matter how often it repeats.
    assert drive(_guard(), WARMUP_FILLER + _repeat("x" * 23, 6)) is None


def test_short_lines_between_repeats_do_not_break_the_run() -> None:
    # A blank line between each repeat is sub-threshold, so it neither counts nor resets the
    # run; the six large-n repeats remain consecutive and trip.
    text = WARMUP_FILLER + (LOOP_LINE + "\n\n") * 6
    assert drive(_guard(), text) is DerailReason.REPETITION


def test_repetition_straddling_warmup_needs_the_threshold_past_it() -> None:
    # Ten identical 30-char lines straddle the warmup boundary; only the four ending past
    # WARMUP_CHARS (200) count, which is short of the six-repeat threshold — so no trip.
    assert drive(_guard(), _repeat("y" * 30, 10)) is None


# --- TOKEN_CAP: the char-proxy decode backstop ------------------------------------


def test_token_cap_fires_just_past_the_char_budget() -> None:
    guard = _guard(max_tokens=10)  # cap = max_tokens(10) * CHARS_PER_TOKEN(4) = 40 chars
    assert guard.feed("z" * 41) is DerailReason.TOKEN_CAP


def test_token_cap_not_reached_at_exactly_the_char_budget() -> None:
    guard = _guard(max_tokens=10)
    # Exactly 40 chars == the cap; the boundary is exclusive (pins CHARS_PER_TOKEN=4 and `>`).
    assert guard.feed("z" * 40) is None


def test_no_newline_runaway_is_caught_by_token_cap_not_repetition() -> None:
    guard = _guard(max_tokens=10)
    # A single ever-growing line forms no repeated slice; the char cap is its only backstop.
    assert guard.feed("x" * 50) is DerailReason.TOKEN_CAP


# --- TIMEOUT: the injected wall clock ---------------------------------------------


def test_timeout_fires_when_the_clock_passes_the_deadline() -> None:
    clock = FakeClock(0.0)
    guard = DerailGuard(build_budget(generation_timeout_s=30.0, max_tokens=1_000_000), now=clock)
    clock.value = 30.001  # deadline = start(0) + generation_timeout_s(30)
    assert guard.feed("x") is DerailReason.TIMEOUT


def test_timeout_not_reached_at_exactly_the_deadline() -> None:
    clock = FakeClock(0.0)
    guard = DerailGuard(build_budget(generation_timeout_s=30.0, max_tokens=1_000_000), now=clock)
    clock.value = 30.0  # exactly at the deadline; the bound is exclusive
    assert guard.feed("x") is None


def test_timeout_is_checked_before_content() -> None:
    clock = FakeClock(0.0)
    guard = DerailGuard(build_budget(generation_timeout_s=5.0, max_tokens=1_000_000), now=clock)
    clock.value = 5.001
    # Even the first tiny delta trips TIMEOUT — the clock bound needs no warmup or content.
    assert guard.feed("a") is DerailReason.TIMEOUT


# --- SILENT: silence, measured independently of total elapsed time ---------------


def test_silence_past_the_bound_is_cut_even_while_the_socket_stays_warm() -> None:
    """A stream delivering bytes but no content is cut once the silence bound passes.

    Oracle: the bound is ``SILENCE_TIMEOUT_S`` since the last content, so one hundredth of a second
    past it trips and the expected value is that constant — a mutated constant diverges it. The
    measured pathology this encodes: 1 content token in 447.2s under a 120s budget, because the
    only bound that could have cut it was checked solely on content that never came.
    """
    clock = FakeClock(0.0)
    guard = DerailGuard(
        build_budget(generation_timeout_s=1_000_000.0, max_tokens=1_000_000), now=clock
    )
    guard.feed("def main():\n")  # last content — silence is measured from here

    clock.value = SILENCE_TIMEOUT_S + 0.01

    assert guard.tick() is DerailReason.SILENT


def test_silence_is_not_yet_a_cut_at_exactly_the_bound() -> None:
    """The bound is exclusive, matching every other clock bound in this guard.

    Oracle: ``test_timeout_not_reached_at_exactly_the_deadline`` fixes the guard's convention that
    a clock bound trips strictly past it, so this bound is exclusive by the same contract rather
    than by whatever the comparison happens to be.
    """
    clock = FakeClock(0.0)
    guard = DerailGuard(
        build_budget(generation_timeout_s=1_000_000.0, max_tokens=1_000_000), now=clock
    )
    guard.feed("def main():\n")

    clock.value = SILENCE_TIMEOUT_S

    assert guard.tick() is None


def test_the_wait_for_the_first_byte_is_not_judged_as_silence() -> None:
    """A slow start is the transport's window; this guard judges only silence it can attribute.

    Oracle: the wait before the first byte is a lazy weight load, not a model going quiet.
    ``model_server`` reports ready off ``/v1/models``, which answers as soon as the port binds,
    while mlx_vlm faults the weights in on the first inference request — measured on the registry's
    largest model (24 GB) at the benchmark's own token budget as 50.1s to first byte with a cold
    page cache, of which only 9.1s was prefill (``scripts/measure_first_byte.py``). That load
    exceeded this bound outright under the memory pressure of a full sweep, so a guard whose clock
    ran from construction would report a model that was loading normally as silent — the same
    false verdict the reasoning-channel fix removed, re-entering through the clock instead of the
    decoder.
    """
    clock = FakeClock(0.0)
    guard = DerailGuard(
        build_budget(generation_timeout_s=1_000_000.0, max_tokens=1_000_000), now=clock
    )

    clock.value = SILENCE_TIMEOUT_S * 2  # the whole load, twice over — still nothing has arrived

    assert guard.tick() is None, "the first byte starts the silence clock, it cannot end one"


def test_silence_is_measured_from_the_first_byte_once_the_stream_has_started() -> None:
    """Arming on first arrival delays the clock; it does not disable it.

    Oracle: the bound is ``SILENCE_TIMEOUT_S`` measured from the first byte, so a gap that long
    after arrival trips even though nothing before arrival counted. This is the other half of the
    contract — a guard that armed and then never judged would let the 447.2s pathology through.
    """
    clock = FakeClock(0.0)
    guard = DerailGuard(
        build_budget(generation_timeout_s=1_000_000.0, max_tokens=1_000_000), now=clock
    )

    clock.value = SILENCE_TIMEOUT_S * 2
    assert guard.tick() is None, "arrival arms the clock"
    clock.value += SILENCE_TIMEOUT_S + 0.01

    assert guard.tick() is DerailReason.SILENT


def test_a_stream_that_keeps_producing_survives_far_past_the_silence_bound() -> None:
    """Total elapsed time does not cut a producing stream — only silence does.

    Oracle: the whole point of the bound is that throughput, not elapsed time, separates a model
    that is cooking from one that is hung. Ten rounds of just-under-the-bound silence accumulate
    to nine times the bound, so a guard measuring total elapsed time instead of the gap since the
    last content fails here — which is precisely the confusion the bound exists to remove.
    """
    clock = FakeClock(0.0)
    guard = DerailGuard(
        build_budget(generation_timeout_s=1_000_000.0, max_tokens=1_000_000), now=clock
    )

    for _ in range(10):
        clock.value += SILENCE_TIMEOUT_S * 0.9
        assert guard.tick() is None, "a gap under the bound is not a silence"
        assert guard.feed("more generated code\n") is None

    # Ten gaps of nine tenths of the bound: nine full bounds of total time, never one of silence.
    assert clock.value == pytest.approx(SILENCE_TIMEOUT_S * 9)


def test_a_tick_enforces_the_total_deadline_during_silence() -> None:
    """The total deadline binds even when no content ever arrives.

    Oracle: ``Budget.generation_timeout_s`` is documented as a wall-clock bound on the generation,
    so a
    generation that produces nothing must still end at it. Checked only on content, the bound is
    unenforceable exactly when it matters most — against a stream that produces no content.
    """
    clock = FakeClock(0.0)
    guard = DerailGuard(build_budget(generation_timeout_s=30.0, max_tokens=1_000_000), now=clock)

    clock.value = 30.001

    assert guard.tick() is DerailReason.TIMEOUT


def test_a_tripped_tick_is_latched_and_readable_without_another_delta() -> None:
    """The verdict survives for a caller that never receives another delta to feed.

    Oracle: the latch contract (``test_verdict_latches_after_the_first_trip``) says the first
    verdict is terminal. A silence's defining case is that no further content arrives, so a caller
    can only learn the verdict by reading it — a latch reachable solely through ``feed`` would be
    unreadable in exactly the situation the silence bound exists to report.
    """
    clock = FakeClock(0.0)
    guard = DerailGuard(
        build_budget(generation_timeout_s=1_000_000.0, max_tokens=1_000_000), now=clock
    )
    guard.feed("def main():\n")
    clock.value = SILENCE_TIMEOUT_S + 0.01

    guard.tick()

    assert guard.tripped is DerailReason.SILENT


# --- Latching: once derailed, stays derailed --------------------------------------


def test_verdict_latches_after_the_first_trip() -> None:
    guard = _guard(max_tokens=10)
    assert guard.feed("z" * 41) is DerailReason.TOKEN_CAP
    # A later benign delta still reports the original reason — the caller aborts once.
    assert guard.feed("more") is DerailReason.TOKEN_CAP


# --- Chunking invariance: the verdict depends only on the concatenated text --------


@pytest.mark.parametrize("chunk_size", [None, 1, 3, 13])
def test_repetition_verdict_is_invariant_to_chunking(chunk_size: int | None) -> None:
    derail_text = WARMUP_FILLER + _repeat(LOOP_LINE, 6)
    assert drive(_guard(), derail_text, chunk_size) is DerailReason.REPETITION


@pytest.mark.parametrize("chunk_size", [None, 1, 3, 13])
def test_non_repetition_verdict_is_invariant_to_chunking(chunk_size: int | None) -> None:
    assert drive(_guard(), load_fixture("valid_repetitive.txt"), chunk_size) is None


# --- The two-fixture oracle (the review's explicit ask) ---------------------------


def test_derail_loop_fixture_trips_repetition() -> None:
    assert drive(_guard(), load_fixture("derail_loop.txt")) is DerailReason.REPETITION


def test_valid_repetitive_fixture_does_not_trip() -> None:
    # The false-positive guard: a long, structurally-repetitive but content-distinct table
    # must never be read as a stuck decode.
    assert drive(_guard(), load_fixture("valid_repetitive.txt")) is None
