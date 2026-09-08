"""Benchmark scorecard — the comparable verdict for one model's run over the whole case ladder.

A ``Scorecard`` reduces a benchmark's ``CaseResult`` list to a per-case pass/fail table plus the
economy totals a reader compares across models: cases passed, completion tokens burned, decode
seconds, and mean decode rate. It reads each case's terminal status and local economy record off
the ``CaseResult`` the driver already produced — the scorer never re-runs a case or re-counts a
token, and it is the single writer of the scorecard artifact.

Cold path by construction: scoring and the JSON write run once after the benchmark completes, off
the inference hot path, so this module favours plain sums and ``json.dumps`` over anything built
for speed (E6).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from claude_local import Status, mean_tokens_per_second, slug_model_id

DEFAULT_SCORECARD_DIR = Path(__file__).resolve().parents[1] / "scorecards"
"""Where scorecards are written and read from — one owner, so the ladder has one location.

Anchored to ``benchmarks/`` rather than to a repo root, so a caller that moved the checkout
still resolves it. Every dev script defaults here instead of re-encoding the path.
"""

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .driver import CaseResult


@dataclass(frozen=True, slots=True)
class CaseScore:
    """One case's line on the scorecard: the case, its terminal status, attempts, and diagnostics.

    ``length_capped`` counts how many of this case's attempts the server ended at its own token
    cap (a budget signal, not a failure), and ``fault`` carries the upstream error message when
    the case ended ``FAULTED`` — both read straight off the driver's ``Outcome``, never
    recomputed. They default to the clean-run values (no fault, nothing capped), so a case that
    hit neither needs no ceremony to construct.
    """

    case_id: str
    status: Status
    attempts: int
    fault: str | None = None
    length_capped: int = 0


@dataclass(frozen=True, slots=True)
class Scorecard:
    """One model's comparable result over the benchmark — a per-case table plus economy totals.

    ``cases`` preserves run order. The economy totals are summed from each case's local economy
    record: ``total_completion_tokens`` and ``total_model_seconds`` across every case, and
    ``mean_tokens_per_second`` as their guarded quotient — ``None`` when no model-seconds elapsed.
    The quotient itself comes from the shared owner of that name, which the per-generation and
    per-task means also call. ``cases_passed`` and ``cases_total`` are derived from ``cases``,
    never stored, so the pass count can never drift from the table.

    ``style_findings`` counts the style problems in the code this run produced. It is supplied by
    the caller rather than derived here, because counting it means running a linter over files on
    disk and this module reduces results that are already in memory. ``None`` means not measured —
    a different answer from ``0``, which means measured and clean.
    """

    model: str
    rules_card_digest: str
    plan_first: bool
    cases: tuple[CaseScore, ...]
    total_completion_tokens: int
    total_model_seconds: float
    mean_tokens_per_second: float | None
    style_findings: int | None = None

    @property
    def cases_passed(self) -> int:
        """How many cases reached ``DONE`` — the headline pass count."""
        return sum(case.status is Status.DONE for case in self.cases)

    @property
    def cases_total(self) -> int:
        """How many cases the benchmark ran."""
        return len(self.cases)

    def write(self, directory: Path, stamp_ms: int) -> Path:
        """Serialize the scorecard as JSON into ``directory`` (created if absent); return the path.

        The filename is ``scorecard-<slugged model>-<ms timestamp>.json``: the ``scorecard-``
        prefix keeps it distinct from an economy record sharing the directory, and the timestamp
        keeps concurrent runs from clobbering one another. Returns where it wrote.

        Args:
            directory: Where to write; created if absent.
            stamp_ms: The run's stamp, supplied rather than read here so this and the run's
                produced code carry the same one — which is the whole of what makes them pair by
                name. Read independently at each writer they differ by however far the clock moved
                between the two calls, and the pairing silently becomes an mtime correlation.
        """
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"scorecard-{slug_model_id(self.model)}-{stamp_ms}.json"
        path.write_text(json.dumps(self._as_dict(), indent=2), encoding="utf-8")
        return path

    def _as_dict(self) -> dict[str, object]:
        """JSON-ready mapping; each case's ``status`` becomes its lowercase enum value."""
        return {
            "model": self.model,
            "rules_card_digest": self.rules_card_digest,
            "plan_first": self.plan_first,
            "cases_passed": self.cases_passed,
            "cases_total": self.cases_total,
            "total_completion_tokens": self.total_completion_tokens,
            "total_model_seconds": self.total_model_seconds,
            "mean_tokens_per_second": self.mean_tokens_per_second,
            "style_findings": self.style_findings,
            "cases": [self._case_as_dict(case) for case in self.cases],
        }

    @staticmethod
    def _case_as_dict(case: CaseScore) -> dict[str, object]:
        """One case's JSON mapping: always ``length_capped``, ``fault`` only when it faulted.

        ``length_capped`` is a stable numeric field (0 or more) so every case line has the same
        shape for cross-model comparison; ``fault`` is exceptional, so an absent key — not a
        ``null`` on every clean case — is what says the case ended without an upstream error frame.
        """
        case_dict: dict[str, object] = {
            "case_id": case.case_id,
            "status": case.status.value,
            "attempts": case.attempts,
            "length_capped": case.length_capped,
        }
        if case.fault is not None:
            case_dict["fault"] = case.fault
        return case_dict


def _held_constant[ConfigurationValue](
    observed: set[ConfigurationValue], subject: str
) -> ConfigurationValue:
    """The single value in ``observed``, or a ``ValueError`` naming what varied instead.

    A scorecard is a comparison, and a comparison is sound only where everything but the axis
    under test was held fixed. Three such conditions exist — one model, one rules card, one
    planning lever — and all fail the same way: a mixed run silently produces a card labelled
    with whichever value the reducer happened to pick. Naming the offending set is what turns
    that into a caller error.

    Generic over the value because the conditions are not all strings — the lever is a bool — and
    the check is about a set's CARDINALITY, which no value type changes. The offending values are
    rendered as text before sorting, so the helper asks nothing of the type at all: ordering is a
    property the MESSAGE needs, not the check, and requiring it of the value would narrow a
    genuinely general helper to buy stable output it can have either way.
    """
    if len(observed) > 1:
        raise ValueError(
            f"a scorecard describes one {subject}, but the benchmark ran "
            f"{sorted(str(value) for value in observed)}"
        )
    (value,) = observed
    return value


def score_cases(results: Sequence[CaseResult]) -> Scorecard:
    """Reduce the benchmark's per-case results to one comparable scorecard.

    Every total is derived from the results' local economy records — the benchmark is never re-run
    and no token re-counted. The model name is read from the records and must be identical across
    the benchmark (one scorecard describes one model); a mixed-model result list is a caller error,
    not a silently mislabelled card. The rules card and the planning lever are read the same way
    and held to the same rule: all three name the configuration a token total belongs to, so a
    result list that disagrees on any of them describes no single configuration at all.

    Args:
        results: One ``CaseResult`` per case, in benchmark order, as ``run_cases`` returns them.

    Returns:
        A ``Scorecard`` whose case table preserves the input order and whose economy totals sum the
        per-case records.

    Raises:
        ValueError: ``results`` is empty, or its records name more than one model.
    """
    if not results:
        raise ValueError("cannot score an empty benchmark")
    records = [result.outcome.record for result in results]
    model = _held_constant({record.model for record in records}, "model")
    rules_card_digest = _held_constant(
        {record.rules_card_digest for record in records}, "rules card"
    )
    plan_first = _held_constant({record.plan_first for record in records}, "planning lever")
    total_completion_tokens = sum(record.total_completion_tokens for record in records)
    total_model_seconds = sum((record.total_model_seconds for record in records), 0.0)
    mean = mean_tokens_per_second(total_completion_tokens, total_model_seconds)
    cases = tuple(
        CaseScore(
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
        rules_card_digest=rules_card_digest,
        plan_first=plan_first,
        cases=cases,
        total_completion_tokens=total_completion_tokens,
        total_model_seconds=total_model_seconds,
        mean_tokens_per_second=mean,
    )
