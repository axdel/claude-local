#!/usr/bin/env -S uv run --quiet python
"""Compare every benchmarked (model, rules card) pair and name the one worth using.

A scorecard measures one run. The decision this project exists to make — which local model to
actually hand work to — is a comparison ACROSS runs, and since the rules card became a benchmark
variable that comparison has two axes rather than one: the same model under two cards is two
different systems, and which card wins is not the same answer for every model.

    scripts/compare-sweeps.py

Reads only committed artifacts, so it needs no model, no server and no GPU, and it re-derives the
whole table from scorecards written long before it existed. Style findings come from
``collect_style_findings`` — the same function ``score-style.py`` calls, so the two reports can
never disagree about what a finding is.

Only the LATEST run of each (model, card) pair is reported. A re-run supersedes: an older
measurement of the same configuration is a strictly worse estimate of it, and averaging the two
would blend a fixed loop against itself. Partial runs — anything that did not attempt the full
case ladder — are excluded rather than compared, because a model that ran two cases is not
2/7 at the thing a 7-case run measures. A run predating the digest field is labelled ``unstamped``
and dropped only when a stamped run of the same model reports its exact totals — the loop is
deterministic, so that is one measurement listed twice, not two data points.

Run from the repository root, like every other command here — the shebang resolves the project's
environment from the working directory. Under a bare ``python3`` it dies on the first import.

Exit codes: 0 when a table was produced, 2 when no complete scorecard exists to compare.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
# The repo root itself, not `src`: this script imports the benchmark package, and Python seeds
# sys.path with the script's own directory rather than the working directory.
sys.path.insert(0, str(_REPO_ROOT))

from benchmarks.harness.style import collect_style_findings  # noqa: E402

_SCORECARDS = _REPO_ROOT / "benchmarks" / "scorecards"
_USAGE_ERROR = 2

# A run now stamps both its artifacts from one clock read, so their names match exactly. Every
# scorecard written before that fix landed came from two independent reads and pairs a millisecond
# or two off, and those runs are most of the comparison — so the join stays a nearest-match. One
# second is far wider than that historical gap and far narrower than the minutes between runs.
_PAIRING_TOLERANCE_MS = 1000

# What a scorecard written before the rules card became a benchmark variable reports as its card.
# Such runs are shown but never used in a verdict — see _print_card_verdicts.
_UNSTAMPED = "unstamped"


@dataclass(frozen=True, slots=True)
class SweepResult:
    """One model's measured result under one rules card — the unit the decision compares."""

    model: str
    rules_card_digest: str
    stamp_ms: int
    cases_passed: int
    cases_total: int
    completion_tokens: int
    model_seconds: float
    attempts: int
    style_findings: int | None

    @property
    def short_model(self) -> str:
        """The model's own name, with the store path its id carries stripped off.

        Scorecards label a run with the served model id, which for a local model is its absolute
        weights path flattened into one slug. That is correct provenance and unreadable in a table.
        """
        return self.model.rstrip("/").rsplit("/", 1)[-1]


def _load_scorecards(scorecards: Path = _SCORECARDS) -> list[SweepResult]:
    """Every complete scorecard in ``scorecards``, with the style count of its produced code.

    Completeness is defined by the scorecards themselves — the widest case ladder any of them
    scored — rather than by loading the ladder to count it. Two reasons, and the second is the
    real one: parsing every case's fixtures to learn a single integer is disproportionate, and the
    ladder's location is a private constant of the benchmark runner, so reading it here would give
    one fact two owners. Deriving it from the artifacts also handles a ladder that GREW: runs that
    scored the old, shorter ladder drop out rather than being compared as though 7 of 9 were their
    result.
    """
    code_directories = sorted(scorecards.glob("code-*"))
    documents = [
        (path, json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(scorecards.glob("scorecard-*.json"))
    ]
    if not documents:
        return []
    full_ladder = max(document["cases_total"] for _, document in documents)

    results: list[SweepResult] = []
    for path, loaded in documents:
        if loaded["cases_total"] < full_ladder:
            continue
        stamp_ms = int(path.stem.rsplit("-", 1)[1])
        results.append(
            SweepResult(
                model=loaded["model"],
                # Scorecards written before the card became a benchmark variable carry no digest.
                # They are kept and labelled rather than dropped: they were all produced under the
                # card that was bundled at the time, and excluding them would discard every
                # measurement taken before the axis existed.
                rules_card_digest=loaded.get("rules_card_digest", _UNSTAMPED),
                stamp_ms=stamp_ms,
                cases_passed=loaded["cases_passed"],
                cases_total=loaded["cases_total"],
                completion_tokens=loaded["total_completion_tokens"],
                model_seconds=loaded["total_model_seconds"],
                attempts=sum(case["attempts"] for case in loaded["cases"]),
                style_findings=_style_count(
                    _paired_code_directory(path, stamp_ms, code_directories)
                ),
            )
        )
    return results


def _paired_code_directory(scorecard: Path, stamp_ms: int, candidates: list[Path]) -> Path | None:
    """The produced-code directory written by the same run, or ``None`` if it is gone.

    Matched on the model slug plus the nearest timestamp within the tolerance, because the two
    artifacts do not in fact share a stamp. Returning ``None`` rather than raising is deliberate:
    an old scorecard whose code was cleaned up is still a valid correctness measurement, and
    losing its row would hide a result to protect a secondary column.
    """
    slug = scorecard.stem[len("scorecard-") : scorecard.stem.rindex("-")]
    prefix = f"code-{slug}-"
    nearest: Path | None = None
    nearest_gap = _PAIRING_TOLERANCE_MS
    for candidate in candidates:
        if not candidate.name.startswith(prefix):
            continue
        gap = abs(int(candidate.name.rsplit("-", 1)[1]) - stamp_ms)
        if gap <= nearest_gap:
            nearest, nearest_gap = candidate, gap
    return nearest


def _style_count(code_directory: Path | None) -> int | None:
    """How many style findings the run's produced code carries; ``None`` when it is unavailable.

    ``None`` and ``0`` are different answers and the table prints them differently — one means
    the code was not found, the other means it was found and was clean.
    """
    if code_directory is None:
        return None
    return len(collect_style_findings(code_directory))


def _output_fingerprint(result: SweepResult) -> tuple[str, int, int, int, int]:
    """What a deterministic loop reproduces exactly: the model, its score, and what it burned.

    Wall-clock is deliberately absent — it is the one number that moves between identical runs,
    measured at 42.4 and 43.5 minutes for byte-identical output. Style findings are absent too:
    they come from the produced-code directory, which an old run may no longer have, so including
    them would make a fingerprint depend on whether an artifact was cleaned up.
    """
    return (
        result.model,
        result.cases_passed,
        result.cases_total,
        result.completion_tokens,
        result.attempts,
    )


def _drop_duplicated_unstamped(results: list[SweepResult]) -> list[SweepResult]:
    """Drop an unstamped row whose measurement a stamped row of the same model already reports.

    The loop is output-deterministic, so a run's totals are its fingerprint: an unstamped row that
    matches a stamped one exactly is that same configuration, measured once before the card was
    recorded and once after. Listing both shows one result twice under two labels, which reads as
    two independent data points.

    An unstamped row with no such twin stays, because it may be the only surviving record of a
    model whose weights are gone. Note what this does and does not claim — that the measurement is
    *already reported*, never that it *must have used* some particular card. The card stays
    unknown, which is why these rows are still excluded from the verdicts below.
    """
    reported = {
        _output_fingerprint(result) for result in results if result.rules_card_digest != _UNSTAMPED
    }
    return [
        result
        for result in results
        if result.rules_card_digest != _UNSTAMPED or _output_fingerprint(result) not in reported
    ]


def _latest_per_configuration(results: list[SweepResult]) -> list[SweepResult]:
    """One row per (model, card): the most recent run of each configuration."""
    latest: dict[tuple[str, str], SweepResult] = {}
    for result in results:
        key = (result.model, result.rules_card_digest)
        if key not in latest or result.stamp_ms > latest[key].stamp_ms:
            latest[key] = result
    return sorted(
        latest.values(),
        key=lambda r: (-r.cases_passed, r.completion_tokens, r.short_model),
    )


def _print_table(rows: list[SweepResult]) -> None:
    """Print every configuration, best first — most cases passed, then fewest tokens."""
    header = (
        f"{'model':<30} {'card':<13} {'cases':>6} {'tokens':>8} "
        f"{'decode':>8} {'attempts':>9} {'style':>6}"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        style = "  n/a" if row.style_findings is None else f"{row.style_findings:>5}"
        print(
            f"{row.short_model:<30} {row.rules_card_digest:<13} "
            f"{row.cases_passed:>3}/{row.cases_total:<2} {row.completion_tokens:>8} "
            f"{row.model_seconds / 60:>7.1f}m {row.attempts:>9} {style}"
        )


def _print_card_verdicts(rows: list[SweepResult]) -> None:
    """For every model measured under more than one card, say which card won and by how much.

    The per-model verdict is the point: a card that helps a weak model can cost a strong one, so
    a single overall winner would average away the only finding that changes what to run.
    """
    by_model: dict[str, list[SweepResult]] = {}
    for row in rows:
        # Unstamped rows stay in the table above — they are real measurements — but they cannot
        # appear in a verdict. A difference can only be ATTRIBUTED to a card that can be named,
        # and an unstamped run against a stamped one reads as two cards while possibly being one:
        # a compact-card run reproduced an unstamped run's token count and per-case attempts
        # exactly, which is what a single card measured twice looks like.
        if row.rules_card_digest == _UNSTAMPED:
            continue
        by_model.setdefault(row.short_model, []).append(row)

    compared = {model: runs for model, runs in by_model.items() if len(runs) > 1}
    if not compared:
        print("\nNo model has been measured under two NAMED cards yet.")
        return

    print("\nPer-model card comparison (a model's own best card, not a global winner):")
    for model, runs in sorted(compared.items()):
        best = min(runs, key=lambda r: (-r.cases_passed, r.completion_tokens))
        others = ", ".join(
            f"{r.rules_card_digest} {r.cases_passed}/{r.cases_total} @{r.completion_tokens}tok"
            for r in sorted(runs, key=lambda r: r.rules_card_digest)
        )
        print(f"  {model:<30} best: {best.rules_card_digest}   [{others}]")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)

    results = _load_scorecards()
    if not results:
        print(f"error: no complete scorecard found under {_SCORECARDS}", file=sys.stderr)
        return _USAGE_ERROR

    rows = _drop_duplicated_unstamped(_latest_per_configuration(results))
    _print_table(rows)
    _print_card_verdicts(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
