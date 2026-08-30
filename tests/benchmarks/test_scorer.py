"""Tests for the suite scorer (``benchmarks.harness.scorer``).

The scorer reduces a suite's ``CaseResult`` list to one comparable ``Scorecard``: a per-rung
pass/fail table plus economy totals summed from each rung's ``LocalEconomyRecord``. Every expected
total here is hand-derived from a fixed set of records (sum, count, guarded quotient), never read
back from ``score_suite``, so a swapped field or broken sum cannot survive. ``write`` round-trips
through a REAL temp dir (the filesystem is local-substitutable — no mocks), asserting the reloaded
JSON and the ``scorecard-<slug>-<ms>.json`` filename.
"""

import json
from pathlib import Path

import pytest
from factories import build_local_economy_record

from benchmarks.harness import CaseResult, RungScore, score_suite
from claude_local import Outcome, Status


def _case_result(
    case_id: str,
    status: Status,
    *,
    attempts: int,
    completion_tokens: int,
    model_seconds: float,
    model: str = "local/candidate-7b",
) -> CaseResult:
    """A ``CaseResult`` carrying a hand-specified economy record — the scorer's sole input.

    The record's own ``mean_tokens_per_second`` is left at the factory default; the scorer never
    reads it, recomputing the suite mean from the summed totals instead.
    """
    record = build_local_economy_record(
        model=model,
        total_completion_tokens=completion_tokens,
        total_model_seconds=model_seconds,
        status=status,
        attempts=attempts,
    )
    outcome = Outcome(
        status=status,
        code="IMPLEMENTED\n" if status is Status.DONE else None,
        impl_path=f"app/{case_id}.py",
        files_changed=(f"app/{case_id}.py",) if status is Status.DONE else (),
        record=record,
    )
    return CaseResult(case_id=case_id, outcome=outcome)


def _mixed_suite() -> list[CaseResult]:
    """Two greens and one red with distinct token/second counts — totals hand-computable."""
    return [
        _case_result(
            "01_scaffold", Status.DONE, attempts=1, completion_tokens=120, model_seconds=2.0
        ),
        _case_result(
            "02_schemas", Status.DONE, attempts=2, completion_tokens=240, model_seconds=4.0
        ),
        _case_result(
            "03_repositories",
            Status.EXHAUSTED,
            attempts=3,
            completion_tokens=40,
            model_seconds=2.0,
        ),
    ]


def test_score_suite_aggregates_hand_computed_totals() -> None:
    """Every scorecard total is the hand-derived sum/count/quotient of the input records."""
    scorecard = score_suite(_mixed_suite())

    # Oracle: tokens 120+240+40=400; seconds 2.0+4.0+2.0=8.0; mean 400/8.0=50.0; 2 of 3 DONE.
    assert scorecard.model == "local/candidate-7b"
    assert scorecard.total_completion_tokens == 400
    assert scorecard.total_model_seconds == 8.0
    assert scorecard.mean_tokens_per_second == 50.0
    assert scorecard.rungs_passed == 2
    assert scorecard.rungs_total == 3
    assert scorecard.rungs == (
        RungScore(case_id="01_scaffold", status=Status.DONE, attempts=1),
        RungScore(case_id="02_schemas", status=Status.DONE, attempts=2),
        RungScore(case_id="03_repositories", status=Status.EXHAUSTED, attempts=3),
    )


def test_score_suite_mean_is_none_when_no_model_seconds() -> None:
    """No model-seconds elapsed → mean is None (never a crash), but tokens still sum."""
    suite = [
        _case_result("a", Status.DONE, attempts=1, completion_tokens=10, model_seconds=0.0),
        _case_result("b", Status.DONE, attempts=1, completion_tokens=20, model_seconds=0.0),
    ]

    scorecard = score_suite(suite)

    assert scorecard.mean_tokens_per_second is None
    assert scorecard.total_completion_tokens == 30
    assert scorecard.total_model_seconds == 0.0


def test_score_suite_rejects_an_empty_result_list() -> None:
    """An empty suite has no model and no rungs — a caller error, not a 0/0 card."""
    with pytest.raises(ValueError, match="empty suite"):
        score_suite([])


def test_score_suite_rejects_a_mixed_model_suite() -> None:
    """One scorecard describes one model; records naming two models is a caller error."""
    suite = [
        _case_result("a", Status.DONE, attempts=1, completion_tokens=10, model_seconds=1.0),
        _case_result(
            "b",
            Status.DONE,
            attempts=1,
            completion_tokens=20,
            model_seconds=1.0,
            model="other/model",
        ),
    ]

    with pytest.raises(ValueError, match="one model"):
        score_suite(suite)


def test_scorecard_write_round_trips_to_json(tmp_path: Path) -> None:
    """The written JSON reloads to the hand-derived mapping, under a scorecard-prefixed name."""
    scorecard = score_suite(_mixed_suite())

    path = scorecard.write(tmp_path)

    assert path.parent == tmp_path
    assert path.name.startswith("scorecard-local-candidate-7b-")
    assert path.suffix == ".json"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "model": "local/candidate-7b",
        "rungs_passed": 2,
        "rungs_total": 3,
        "total_completion_tokens": 400,
        "total_model_seconds": 8.0,
        "mean_tokens_per_second": 50.0,
        "rungs": [
            {"case_id": "01_scaffold", "status": "done", "attempts": 1},
            {"case_id": "02_schemas", "status": "done", "attempts": 2},
            {"case_id": "03_repositories", "status": "exhausted", "attempts": 3},
        ],
    }


def test_scorecard_write_creates_the_directory_when_absent(tmp_path: Path) -> None:
    """``write`` creates a missing output directory rather than failing on it."""
    scorecard = score_suite(_mixed_suite())
    destination = tmp_path / "nested" / "scorecards"

    path = scorecard.write(destination)

    assert path.parent == destination
    assert path.is_file()


def test_scorecard_is_immutable() -> None:
    """A scorecard is a frozen value object — a scored result is never mutated after the fact."""
    scorecard = score_suite(_mixed_suite())

    with pytest.raises(AttributeError):
        scorecard.model = "tampered/model"  # type: ignore[misc]
