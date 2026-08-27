"""Tests for the local economy record (``claude_local.telemetry``).

``LocalEconomyRecord`` is the LOCAL half of the per-task economy record (D-TELEMETRY-001): the
telemetry module is its single writer (RESOURCE_OWNERSHIP). ``from_run`` AGGREGATES a timeline of
per-attempt ``GenerationResult`` into scalars; ``total_calls`` and ``attempts`` are passed in,
never derived from the timeline length — the review's off-by-one. Every aggregate below is
hand-derived from a fixed timeline, never read from ``from_run``. ``write`` round-trips through
a REAL temp dir (the filesystem is local-substitutable — no mocks), asserting the reloaded JSON.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from factories import build_generation_result

from claude_local.telemetry import LocalEconomyRecord
from claude_local.types import Status

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from claude_local.client import GenerationResult


def _record(**overrides: object) -> LocalEconomyRecord:
    """A fully-specified record for the write tests — construction independent of ``from_run``."""
    # Every scalar is numerically distinct (3 / 300 / 6.0 / 50.0 / 2 / 4) so a field-swap mutant in
    # write cannot survive the round-trip equality — 3 == 3.0 would mask a calls/seconds swap.
    fields: dict[str, object] = {
        "model": "mlx-community/Qwen2.5-Coder-7B",
        "total_calls": 3,
        "total_completion_tokens": 300,
        "total_model_seconds": 6.0,
        "mean_tokens_per_second": 50.0,
        "tokens_estimated": True,
        "length_capped": 2,
        "status": Status.DONE,
        "attempts": 4,
    }
    fields.update(overrides)
    return LocalEconomyRecord(**fields)  # type: ignore[arg-type]


def _from_run(
    results: Sequence[GenerationResult],
    *,
    total_calls: int,
    attempts: int,
    status: Status = Status.DONE,
) -> LocalEconomyRecord:
    """Invoke the SUT over a timeline, defaulting only what no ``from_run`` test varies.

    ``total_calls`` and ``attempts`` stay REQUIRED and are never derived from ``len(results)`` —
    that client-count-vs-timeline-length gap is the off-by-one this suite guards. ``model`` is
    fixed to ``"m"``; the lone test that asserts model is carried through constructs directly.
    """
    return LocalEconomyRecord.from_run(
        model="m", results=results, total_calls=total_calls, attempts=attempts, status=status
    )


# --- from_run: aggregation over the timeline --------------------------------------


def test_from_run_sums_completion_tokens_and_model_seconds_over_the_timeline() -> None:
    # Hand-derived: 60 + 40 = 100 tokens; 1.5 + 0.5 = 2.0 seconds.
    results = [
        build_generation_result(completion_tokens=60, seconds=1.5),
        build_generation_result(completion_tokens=40, seconds=0.5),
    ]
    record = _from_run(results, total_calls=2, attempts=2)
    assert record.total_completion_tokens == 100
    assert record.total_model_seconds == 2.0


def test_mean_tokens_per_second_is_total_completion_over_total_seconds() -> None:
    # Hand-derived: 100 completion tokens / 2.0 model-seconds = 50.0 tokens/sec (completion only).
    results = [
        build_generation_result(completion_tokens=60, seconds=1.5),
        build_generation_result(completion_tokens=40, seconds=0.5),
    ]
    record = _from_run(results, total_calls=2, attempts=2)
    assert record.mean_tokens_per_second == 50.0


def test_mean_tokens_per_second_is_none_when_total_model_seconds_is_zero() -> None:
    # Div-by-zero guard: nonzero tokens over 0.0 seconds must yield None, never a crash or 0.0.
    results = [build_generation_result(completion_tokens=100, seconds=0.0)]
    record = _from_run(results, total_calls=1, attempts=1)
    assert record.total_model_seconds == 0.0
    assert record.mean_tokens_per_second is None


def test_tokens_estimated_is_true_when_any_attempt_used_the_proxy() -> None:
    # any(): one estimated attempt among exact ones flags the whole record estimated.
    results = [
        build_generation_result(tokens_estimated=False),
        build_generation_result(tokens_estimated=True),
    ]
    record = _from_run(results, total_calls=2, attempts=2)
    assert record.tokens_estimated is True


def test_tokens_estimated_is_false_when_every_attempt_was_server_counted() -> None:
    # any(): all-exact attempts leave the record exact — kills a constant-True mutant.
    results = [
        build_generation_result(tokens_estimated=False),
        build_generation_result(tokens_estimated=False),
    ]
    record = _from_run(results, total_calls=2, attempts=2)
    assert record.tokens_estimated is False


def test_length_capped_counts_only_the_server_length_finishes() -> None:
    # Hand-derived: of these 5 attempts, exactly 3 carry finish_reason "length" (the server hit its
    # own token cap). "stop" (a clean finish) and None (a derail cut the stream before any terminal
    # frame) must NOT count. The answer 3 is chosen ASYMMETRICALLY — the complement (non-"length")
    # is 2, so an ``==`` → ``!=`` mutant reads 2, not 3, and cannot hide behind an equal split. It
    # is also distinct from the count of non-None reasons (4), of every attempt (5), and of any
    # single wrong reason ("stop" → 1), so counting the wrong terminal or dropping the predicate
    # all diverge. Oracle: the tally is defined by the SSE contract (finish_reason=="length"),
    # derived without running from_run.
    results = [
        build_generation_result(finish_reason="length"),
        build_generation_result(finish_reason="stop"),
        build_generation_result(finish_reason="length"),
        build_generation_result(finish_reason=None),
        build_generation_result(finish_reason="length"),
    ]
    record = _from_run(results, total_calls=5, attempts=5, status=Status.EXHAUSTED)
    assert record.length_capped == 3


def test_total_calls_is_the_client_count_not_the_timeline_length() -> None:
    # The off-by-one guard: a call that raised produced no result, so total_calls (3, from the
    # client) exceeds len(results) (2). The record must carry the passed count, not len().
    results = [build_generation_result(), build_generation_result()]
    record = _from_run(results, total_calls=3, attempts=3)
    assert record.total_calls == 3


def test_model_attempts_and_status_are_carried_through() -> None:
    # attempts (5) is deliberately != total_calls (1) so a field-swap mutant cannot pass.
    results = [build_generation_result()]
    record = LocalEconomyRecord.from_run(
        model="qwen", results=results, total_calls=1, attempts=5, status=Status.DERAILED
    )
    assert record.model == "qwen"
    assert record.attempts == 5
    assert record.status is Status.DERAILED


# --- write: JSON serialization round-trip -----------------------------------------


def test_write_round_trips_every_field_as_json_and_returns_the_path(tmp_path: Path) -> None:
    # write returns where it wrote; the reloaded JSON must equal the record field-for-field, with
    # status as its lowercase enum VALUE ("done", per D-TELEMETRY-001), not the member name.
    path = _record().write(tmp_path)
    assert path.exists()
    assert path.suffix == ".json"
    assert path.parent == tmp_path
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "model": "mlx-community/Qwen2.5-Coder-7B",
        "total_calls": 3,
        "total_completion_tokens": 300,
        "total_model_seconds": 6.0,
        "mean_tokens_per_second": 50.0,
        "tokens_estimated": True,
        "length_capped": 2,
        "status": "done",
        "attempts": 4,
    }


def test_write_serializes_a_none_mean_as_json_null(tmp_path: Path) -> None:
    # A run with zero model-seconds carries mean=None; JSON null must survive the round-trip.
    path = _record(mean_tokens_per_second=None).write(tmp_path)
    assert json.loads(path.read_text(encoding="utf-8"))["mean_tokens_per_second"] is None


def test_write_creates_the_economy_directory_when_absent(tmp_path: Path) -> None:
    # The orchestrator's target economy dir may not exist yet; write must create it.
    target = tmp_path / "economy" / "claude-local"
    path = _record().write(target)
    assert target.is_dir()
    assert path.parent == target
