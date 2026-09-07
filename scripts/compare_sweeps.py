#!/usr/bin/env -S uv run --quiet python
"""Compare every benchmarked (model, rules card, mode) configuration and name the one worth using.

A scorecard measures one run. The decision this project exists to make — which local model to
actually hand work to — is a comparison ACROSS runs, and every benchmark variable adds an axis to
it: the same model under two cards is two different systems, and so is the same model with and
without a planning generation. Which card wins is not the same answer for every model, and whether
planning pays for itself is not the same answer for every configuration.

The mode axis was added after a plan-first benchmark-run silently overwrote the baseline run
it followed — same model, same card, 9858 completion tokens taking the place of 4436 — and
this script named the survivor the one worth using. A configuration axis left out of the key
does not read as missing; it reads as a re-run.

    scripts/compare_sweeps.py

Reads only the committed scorecards, so it needs no model, no server and no GPU, and it
re-derives the whole table from runs recorded long before it existed. Each scorecard is a complete
record: the style count is written into it at scoring time, so the comparison never has to find the
code a run produced — that code is optional run output and lives outside this repository.

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
_SCORECARDS = _REPO_ROOT / "benchmarks" / "scorecards"
_USAGE_ERROR = 2

# What a scorecard written before the rules card became a benchmark variable reports as its card.
# Such runs are shown but never used in a verdict — see _print_card_verdicts.
_UNSTAMPED = "unstamped"


@dataclass(frozen=True, slots=True)
class LoadedScorecard:
    """One model's measured scorecard under one rules card — the unit the decision compares."""

    model: str
    rules_card_digest: str
    plan_first: bool | None
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


def _load_scorecards(scorecard_directory: Path = _SCORECARDS) -> list[LoadedScorecard]:
    """Every complete scorecard in ``scorecard_directory``, with its produced code's style count.

    Completeness is defined by the scorecards themselves — the widest case ladder any of them
    scored — rather than by loading the ladder to count it. Two reasons, and the second is the
    real one: parsing every case's fixtures to learn a single integer is disproportionate, and the
    ladder's location is a private constant of the benchmark runner, so reading it here would give
    one fact two owners. Deriving it from the artifacts also handles a ladder that GREW: runs that
    scored the old, shorter ladder drop out rather than being compared as though 7 of 9 were their
    scorecard.
    """
    documents = [
        (path, json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(scorecard_directory.glob("scorecard-*.json"))
    ]
    if not documents:
        return []
    full_ladder = max(document["cases_total"] for _, document in documents)

    scorecards: list[LoadedScorecard] = []
    for path, loaded in documents:
        if loaded["cases_total"] < full_ladder:
            continue
        stamp_ms = int(path.stem.rsplit("-", 1)[1])
        scorecards.append(
            LoadedScorecard(
                model=loaded["model"],
                # Scorecards written before the card became a benchmark variable carry no digest.
                # They are kept and labelled rather than dropped: they were all produced under the
                # card that was bundled at the time, and excluding them would discard every
                # measurement taken before the axis existed.
                rules_card_digest=loaded.get("rules_card_digest", _UNSTAMPED),
                # Absent means UNRECORDED, never False. Reading it as baseline was an inference
                # standing in for a fact, and it stopped being safe the day --plan-first reached
                # benchmarks/run.py and scripts/benchmark_model.py while the scorecard field was
                # still a day away: a run in that window could plan and say nothing. None keeps
                # such a row from colliding with a recorded baseline on the key below, which is
                # the whole failure this field exists to prevent (D-TELEMETRY-003).
                plan_first=bool(loaded["plan_first"]) if "plan_first" in loaded else None,
                stamp_ms=stamp_ms,
                cases_passed=loaded["cases_passed"],
                cases_total=loaded["cases_total"],
                completion_tokens=loaded["total_completion_tokens"],
                model_seconds=loaded["total_model_seconds"],
                attempts=sum(case["attempts"] for case in loaded["cases"]),
                # None and 0 are different answers and the table prints them differently: one
                # means the run predates the field, the other means its code was linted and clean.
                style_findings=loaded.get("style_findings"),
            )
        )
    return scorecards


def _output_fingerprint(
    scorecard: LoadedScorecard,
) -> tuple[str, bool | None, int, int, int, int]:
    """What a deterministic loop reproduces exactly: the model, its score, and what it burned.

    Wall-clock is deliberately absent — it is the one number that moves between identical runs,
    measured at 42.4 and 43.5 minutes for byte-identical output. Style findings are absent too:
    they come from the produced-code directory, which an old run may no longer have, so including
    them would make a fingerprint depend on whether an artifact was cleaned up.

    The planning lever IS present, though it is an input rather than an output, because the
    inference this fingerprint serves is "these two rows are one measurement listed twice" — and
    that can only hold within one configuration. Two runs that tie on totals across the lever are
    two measurements, and the tie is what makes them worth telling apart rather than what makes
    them the same. An unrecorded lever is a third value here, not a match for the baseline: a row
    that ties a known-baseline run on every total still cannot be called the same measurement,
    because what it did is unknown rather than known to agree.
    """
    return (
        scorecard.model,
        scorecard.plan_first,
        scorecard.cases_passed,
        scorecard.cases_total,
        scorecard.completion_tokens,
        scorecard.attempts,
    )


def _drop_duplicated_unstamped(scorecards: list[LoadedScorecard]) -> list[LoadedScorecard]:
    """Drop an unstamped row whose measurement a stamped row of the same model already reports.

    The loop is output-deterministic, so a run's totals are its fingerprint: an unstamped row that
    matches a stamped one exactly is that same configuration, measured once before the card was
    recorded and once after. Listing both shows one scorecard twice under two labels, reading as
    two independent data points.

    An unstamped row with no such twin stays, because it may be the only surviving record of a
    model whose weights are gone. Note what this does and does not claim — that the measurement is
    *already reported*, never that it *must have used* some particular card. The card stays
    unknown, which is why these rows are still excluded from the verdicts below.
    """
    reported = {
        _output_fingerprint(scorecard)
        for scorecard in scorecards
        if scorecard.rules_card_digest != _UNSTAMPED
    }
    return [
        scorecard
        for scorecard in scorecards
        if scorecard.rules_card_digest != _UNSTAMPED
        or _output_fingerprint(scorecard) not in reported
    ]


def _latest_per_configuration(scorecards: list[LoadedScorecard]) -> list[LoadedScorecard]:
    """One row per (model, card, planning lever): the most recent run of each configuration.

    All three name the configuration a token total belongs to, so all three are the key. Leaving
    any one out silently discards an arm of the A/B that varied it and reports the survivor as
    that model's scorecard — what a plan-first benchmark-run did to the baseline run it followed.
    """
    latest: dict[tuple[str, str, bool | None], LoadedScorecard] = {}
    for scorecard in scorecards:
        key = (scorecard.model, scorecard.rules_card_digest, scorecard.plan_first)
        if key not in latest or scorecard.stamp_ms > latest[key].stamp_ms:
            latest[key] = scorecard
    return sorted(
        latest.values(),
        key=lambda r: (-r.cases_passed, r.completion_tokens, r.short_model),
    )


def _mode_label(plan_first: bool | None) -> str:
    """How a run was configured, in one column-width word.

    The lever's canonical name when it is on; ``off`` when it is not. Naming the off state after
    the same lever rather than inventing a second term keeps this to the one concept the glossary
    declares — there is no such thing as a run in "direct mode", only a run that did not plan.

    ``unrecorded`` is a third answer and not a synonym for ``off``: the run predates the field, so
    what it did is unknown rather than known to be nothing.

    Takes the lever rather than a row because the card verdict labels a GROUP, which holds the
    lever and no particular row. Two spellings of this mapping is how the third case gets added
    to one of them and not the other.
    """
    if plan_first is None:
        return "unrecorded"
    return "plan-first" if plan_first else "off"


def _mode(scorecard: LoadedScorecard) -> str:
    """The mode label for one row — the same label its verdict group prints."""
    return _mode_label(scorecard.plan_first)


def _print_table(rows: list[LoadedScorecard]) -> None:
    """Print every configuration, best first — most cases passed, then fewest tokens."""
    header = (
        f"{'model':<30} {'card':<13} {'mode':<10} {'cases':>6} {'tokens':>8} "
        f"{'decode':>8} {'attempts':>9} {'style':>6}"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        style = "  n/a" if row.style_findings is None else f"{row.style_findings:>5}"
        print(
            f"{row.short_model:<30} {row.rules_card_digest:<13} {_mode(row):<10} "
            f"{row.cases_passed:>3}/{row.cases_total:<2} {row.completion_tokens:>8} "
            f"{row.model_seconds / 60:>7.1f}m {row.attempts:>9} {style}"
        )


def _print_card_verdicts(rows: list[LoadedScorecard]) -> None:
    """For every model measured under more than one card, say which card won and by how much.

    The per-model verdict is the point: a card that helps a weak model can cost a strong one, so
    a single overall winner would average away the only finding that changes what to run.
    """
    by_model_and_mode: dict[tuple[str, bool | None], list[LoadedScorecard]] = {}
    for row in rows:
        # Unstamped rows stay in the table above — they are real measurements — but they cannot
        # appear in a verdict. A difference can only be ATTRIBUTED to a card that can be named,
        # and an unstamped run against a stamped one reads as two cards while possibly being one:
        # a compact-card run reproduced an unstamped run's token count and per-case attempts
        # exactly, which is what a single card measured twice looks like.
        if row.rules_card_digest == _UNSTAMPED:
            continue
        # Keyed with the mode, not the model alone: a verdict may only vary the axis it names, so
        # two rows differing in the LEVER are not a card comparison. Grouped on the model alone
        # they would be paired anyway and a winner announced between a card and itself.
        #
        # An unrecorded lever is its own group rather than an exclusion, because here the lever is
        # the CONTROL and the card is the subject. The subject is named, so the verdict is still
        # statable; what is unknown is whether the control held, and printing "unrecorded" in the
        # mode column says exactly that. Contrast the unstamped digest above, where the subject
        # itself has no name and no verdict can be stated at all.
        by_model_and_mode.setdefault((row.short_model, row.plan_first), []).append(row)

    compared = {group: runs for group, runs in by_model_and_mode.items() if len(runs) > 1}
    if not compared:
        print("\nNo model has been measured under two NAMED cards yet.")
        return

    print("\nPer-model card comparison (a model's own best card, not a global winner):")
    for (model, plan_first), runs in sorted(compared.items()):
        best = min(runs, key=lambda r: (-r.cases_passed, r.completion_tokens))
        others = ", ".join(
            f"{r.rules_card_digest} {r.cases_passed}/{r.cases_total} @{r.completion_tokens}tok"
            for r in sorted(runs, key=lambda r: r.rules_card_digest)
        )
        print(
            f"  {model:<30} {_mode_label(plan_first):<10} "
            f"best: {best.rules_card_digest}   [{others}]"
        )


def _print_mode_verdicts(rows: list[LoadedScorecard]) -> None:
    """For every (model, card) measured in both modes, say whether the plan paid for itself.

    The mirror of the card verdict, on the axis the lever varies, and per configuration for the
    same reason: a plan is worth its generation only where it saves more than it costs, and that
    is a property of a particular model under a particular card — a weak model may need the
    scaffolding a strong one is only slowed by. A single global answer would average away the
    finding that decides what to run.
    """
    by_configuration: dict[tuple[str, str], list[LoadedScorecard]] = {}
    for row in rows:
        # Here the lever is the SUBJECT, not the control, so an unrecorded one is excluded on the
        # same grounds an unstamped card is: a verdict cannot name a mode the run never recorded.
        # Kept in, such a row would satisfy the both-modes test below against a genuine one and
        # publish "planning does not pay" out of a comparison with a run that may itself have
        # planned.
        if row.rules_card_digest == _UNSTAMPED or row.plan_first is None:
            continue
        by_configuration.setdefault((row.short_model, row.rules_card_digest), []).append(row)

    compared = {
        configuration: runs
        for configuration, runs in by_configuration.items()
        if len({run.plan_first for run in runs}) > 1
    }
    if not compared:
        print("\nNo configuration has been measured in both modes yet.")
        return

    print("\nPer-configuration mode comparison (does a planning generation pay for itself?):")
    for (model, card), runs in sorted(compared.items()):
        best = min(runs, key=lambda r: (-r.cases_passed, r.completion_tokens))
        others = ", ".join(
            f"{_mode(r)} {r.cases_passed}/{r.cases_total} @{r.completion_tokens}tok"
            # Sorted by the printed label, not the raw lever: every row here has a recorded one,
            # but the label is what the reader compares and it orders off before plan-first
            # anyway, so the sort key is the thing on the page rather than a parallel rule.
            for r in sorted(runs, key=_mode)
        )
        print(f"  {model:<30} {card:<13} best: {_mode(best):<10} [{others}]")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)

    scorecards = _load_scorecards()
    if not scorecards:
        print(f"error: no complete scorecard found under {_SCORECARDS}", file=sys.stderr)
        return _USAGE_ERROR

    rows = _drop_duplicated_unstamped(_latest_per_configuration(scorecards))
    _print_table(rows)
    _print_card_verdicts(rows)
    _print_mode_verdicts(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
