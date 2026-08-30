"""Suite scorecard — the comparable verdict for one model's run over the whole case ladder.

A ``Scorecard`` reduces a suite's ``CaseResult`` list to a per-rung pass/fail table plus the
economy totals a reader compares across models: rungs passed, completion tokens burned, decode
seconds, and mean decode rate. It reads each rung's terminal status and local economy record off
the ``CaseResult`` the driver already produced — the scorer never re-runs a case or re-counts a
token, and it is the single writer of the scorecard artifact.

Cold path by construction: scoring and the JSON write run once after a whole suite completes, off
the inference hot path, so this module favours plain sums and ``json.dumps`` over anything built
for speed (E6).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from claude_local import Status, slug_model_id

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from .driver import CaseResult


@dataclass(frozen=True, slots=True)
class RungScore:
    """One rung's line on the scorecard: the case, its terminal status, attempts, and diagnostics.

    ``length_capped`` counts how many of this rung's attempts the server ended at its own token
    cap (a budget signal, not a failure), and ``fault`` carries the upstream error message when
    the rung ended ``FAULTED`` — both read straight off the driver's ``Outcome``, never
    recomputed. They default to the clean-run values (no fault, nothing capped), so a rung that
    hit neither needs no ceremony to construct.
    """

    case_id: str
    status: Status
    attempts: int
    fault: str | None = None
    length_capped: int = 0


@dataclass(frozen=True, slots=True)
class Scorecard:
    """One model's comparable result over a whole suite — a per-rung table plus economy totals.

    ``rungs`` preserves suite order. The economy totals are summed from each rung's local economy
    record: ``total_completion_tokens`` and ``total_model_seconds`` across every case, and
    ``mean_tokens_per_second`` as their guarded quotient — ``None`` when no model-seconds elapsed,
    mirroring ``LocalEconomyRecord``'s per-task mean (shared formula, not a shared owner: Rule of
    Three notes the second occurrence, extract at the third). ``rungs_passed`` and ``rungs_total``
    are derived from ``rungs``, never stored, so the pass count can never drift from the table.
    """

    model: str
    rungs: tuple[RungScore, ...]
    total_completion_tokens: int
    total_model_seconds: float
    mean_tokens_per_second: float | None

    @property
    def rungs_passed(self) -> int:
        """How many rungs reached ``DONE`` — the headline pass count."""
        return sum(rung.status is Status.DONE for rung in self.rungs)

    @property
    def rungs_total(self) -> int:
        """How many rungs the suite ran."""
        return len(self.rungs)

    def write(self, directory: Path) -> Path:
        """Serialize the scorecard as JSON into ``directory`` (created if absent); return the path.

        The filename is ``scorecard-<slugged model>-<ms timestamp>.json``: the ``scorecard-``
        prefix keeps it distinct from an economy record sharing the directory, and the timestamp
        keeps concurrent runs from clobbering one another. Returns where it wrote.
        """
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"scorecard-{slug_model_id(self.model)}-{int(time.time() * 1000)}.json"
        path.write_text(json.dumps(self._as_dict(), indent=2), encoding="utf-8")
        return path

    def _as_dict(self) -> dict[str, object]:
        """JSON-ready mapping; each rung's ``status`` becomes its lowercase enum value."""
        return {
            "model": self.model,
            "rungs_passed": self.rungs_passed,
            "rungs_total": self.rungs_total,
            "total_completion_tokens": self.total_completion_tokens,
            "total_model_seconds": self.total_model_seconds,
            "mean_tokens_per_second": self.mean_tokens_per_second,
            "rungs": [self._rung_as_dict(rung) for rung in self.rungs],
        }

    @staticmethod
    def _rung_as_dict(rung: RungScore) -> dict[str, object]:
        """One rung's JSON mapping: always ``length_capped``, ``fault`` only when it faulted.

        ``length_capped`` is a stable numeric field (0 or more) so every rung line has the same
        shape for cross-model comparison; ``fault`` is exceptional, so an absent key — not a
        ``null`` on every clean rung — is what says the rung ended without an upstream error frame.
        """
        rung_dict: dict[str, object] = {
            "case_id": rung.case_id,
            "status": rung.status.value,
            "attempts": rung.attempts,
            "length_capped": rung.length_capped,
        }
        if rung.fault is not None:
            rung_dict["fault"] = rung.fault
        return rung_dict


def score_suite(results: Sequence[CaseResult]) -> Scorecard:
    """Reduce a suite's per-case results to one comparable scorecard.

    Every total is derived from the results' local economy records — the suite is never re-run and
    no token re-counted. The model name is read from the records and must be identical across the
    suite (one scorecard describes one model); a mixed-model result list is a caller error, not a
    silently mislabelled card.

    Args:
        results: One ``CaseResult`` per rung, in suite order, as ``run_suite`` returns them.

    Returns:
        A ``Scorecard`` whose rung table preserves the input order and whose economy totals sum the
        per-case records.

    Raises:
        ValueError: ``results`` is empty, or its records name more than one model.
    """
    if not results:
        raise ValueError("cannot score an empty suite")
    records = [result.outcome.record for result in results]
    models = {record.model for record in records}
    if len(models) > 1:
        raise ValueError(f"a scorecard describes one model, but the suite ran {sorted(models)}")
    (model,) = models
    total_completion_tokens = sum(record.total_completion_tokens for record in records)
    total_model_seconds = sum((record.total_model_seconds for record in records), 0.0)
    mean = total_completion_tokens / total_model_seconds if total_model_seconds > 0 else None
    rungs = tuple(
        RungScore(
            case_id=result.case_id,
            status=result.outcome.status,
            attempts=result.outcome.record.attempts,
            fault=result.outcome.fault,
            length_capped=result.outcome.record.length_capped,
        )
        for result in results
    )
    return Scorecard(
        model=model,
        rungs=rungs,
        total_completion_tokens=total_completion_tokens,
        total_model_seconds=total_model_seconds,
        mean_tokens_per_second=mean,
    )
