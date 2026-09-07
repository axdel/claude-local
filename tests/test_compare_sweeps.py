"""Tests for the cross-run comparison (``scripts/compare_sweeps.py``).

The script decides which (model, rules card) pair to actually use, so its two pieces of real logic
are the ones that can silently produce a wrong recommendation: picking ONE row per configuration
(a superseded run reported as current would recommend a fixed loop's old behaviour), and pairing a
scorecard with the produced code whose style it reports (a mismatched pair attributes one run's
findings to another). Both are exercised against real files in a temp directory — the filesystem
is local-substitutable, so nothing here is mocked.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from scriptloader import load_script

_SCRIPT = Path(__file__).parents[1] / "scripts" / "compare_sweeps.py"


def _script() -> ModuleType:
    """The comparison script, loaded by path the way its shebang runs it."""
    return load_script(_SCRIPT)


def _result(module: ModuleType, **overrides: object) -> Any:
    """A canonical ``LoadedScorecard``; a test overrides only the field it exercises.

    Returns ``Any`` because the script is loaded by path: its classes exist at runtime but have no
    statically-nameable type, so annotating anything narrower would be a fiction the checker then
    enforces against the real attributes.
    """
    fields: dict[str, object] = {
        "model": "local/candidate",
        "rules_card_digest": "aaaaaaaaaaaa",
        "plan_first": False,
        "stamp_ms": 1_000,
        "cases_passed": 7,
        "cases_total": 7,
        "completion_tokens": 5_000,
        "model_seconds": 300.0,
        "attempts": 7,
        "style_findings": 0,
    }
    fields.update(overrides)
    return module.LoadedScorecard(**fields)  # type: ignore[arg-type]


def test_a_rerun_supersedes_the_earlier_run_of_the_same_configuration() -> None:
    """Oracle: two runs of one (model, card) are one configuration, so exactly one row survives.

    The later stamp wins because a re-run measures the current loop; reporting the earlier one
    would recommend behaviour the code no longer has.
    """
    module = _script()
    older = _result(module, stamp_ms=1_000, completion_tokens=9_000)
    newer = _result(module, stamp_ms=2_000, completion_tokens=4_000)

    rows = module._latest_per_configuration([older, newer])

    assert len(rows) == 1
    assert rows[0].completion_tokens == 4_000


def test_the_same_model_under_two_cards_stays_two_rows() -> None:
    """Oracle: the card is part of the configuration, so one model yields two comparable rows.

    Collapsing on model alone is the failure this guards: it would silently discard one arm of
    every card A/B and report the survivor as that model's scorecard.
    """
    module = _script()
    compact = _result(module, rules_card_digest="aaaaaaaaaaaa", stamp_ms=1_000)
    doctrine = _result(module, rules_card_digest="bbbbbbbbbbbb", stamp_ms=2_000)

    rows = module._latest_per_configuration([compact, doctrine])

    assert {row.rules_card_digest for row in rows} == {"aaaaaaaaaaaa", "bbbbbbbbbbbb"}


def test_the_same_model_and_card_under_two_planning_modes_stays_two_rows() -> None:
    """Oracle: the planning lever is part of the configuration too, exactly as the card is.

    The failure above, one axis over, and it is not hypothetical — it is how this was found. A
    baseline sweep of one model measured 7/7 at 4436 completion tokens; the plan-first sweep of
    the SAME model under the SAME card measured 7/7 at 9858, ran later, and silently took the
    earlier row's place. The table then named the 9858 configuration the one worth using, which
    is the opposite of what the two runs together say.
    """
    module = _script()
    unplanned = _result(module, plan_first=False, stamp_ms=1_000, completion_tokens=4_436)
    planned = _result(module, plan_first=True, stamp_ms=2_000, completion_tokens=9_858)

    rows = module._latest_per_configuration([unplanned, planned])

    assert {row.completion_tokens for row in rows} == {4_436, 9_858}


def test_the_table_names_the_mode_so_two_rows_of_one_configuration_are_tellable_apart(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Oracle: keying on the lever puts two rows where there was one, and a reader must see why.

    Without the column the same model under the same card appears twice with different token
    totals and nothing on the line accounting for the difference, which reads as a duplicate or a
    bug rather than as the comparison it is. The lever's canonical name is what the on-row says;
    the off-row says ``off`` rather than naming a second concept the glossary does not have.
    """
    module = _script()

    module._print_table(
        [
            _result(module, plan_first=False, completion_tokens=4_436),
            _result(module, plan_first=True, completion_tokens=9_858),
        ]
    )

    printed = capsys.readouterr().out
    assert "mode" in printed
    planned, unplanned = (
        next(line for line in printed.splitlines() if str(tokens) in line)
        for tokens in (9_858, 4_436)
    )
    assert "plan-first" in planned
    assert "plan-first" not in unplanned
    assert "off" in unplanned


def test_a_planned_run_cannot_absorb_an_unstamped_run_it_merely_ties_on_totals() -> None:
    """Oracle: an unstamped run predates the card axis, so it also predates the lever — baseline.

    The drop rule reads matching totals as one measurement listed twice. That inference holds
    only within a configuration: a planned run and an unplanned one that happen to tie are two
    measurements, and dropping either loses a real arm of the comparison.
    """
    module = _script()
    unstamped = _result(module, rules_card_digest="unstamped", plan_first=False)
    planned = _result(module, plan_first=True)

    rows = module._drop_duplicated_unstamped([unstamped, planned])

    assert len(rows) == 2


def test_rows_are_ordered_by_cases_passed_then_by_fewest_tokens() -> None:
    """Oracle: more cases passed always outranks cheaper, so a 4/7 never precedes a 7/7.

    Correctness dominates economy — a cheap run that fails three cases is not the better system,
    and an ordering that put tokens first would recommend exactly that.
    """
    module = _script()
    cheap_but_failing = _result(
        module, model="a/cheap", cases_passed=4, completion_tokens=100, stamp_ms=1
    )
    dear_but_passing = _result(
        module, model="b/dear", cases_passed=7, completion_tokens=90_000, stamp_ms=2
    )
    frugal_and_passing = _result(
        module, model="c/frugal", cases_passed=7, completion_tokens=500, stamp_ms=3
    )

    rows = module._latest_per_configuration(
        [cheap_but_failing, dear_but_passing, frugal_and_passing]
    )

    assert [row.model for row in rows] == ["c/frugal", "b/dear", "a/cheap"]


def test_an_unstamped_run_a_stamped_one_already_reports_is_dropped() -> None:
    """One measurement must appear once, not twice under two labels.

    Oracle: the loop is output-deterministic — two runs 100 minutes apart produced the same 7,619
    tokens and the same per-case attempts — so identical totals for one model are the same
    configuration, measured once before the card was recorded and once after. Showing both invites
    reading a single scorecard as two independent data points.
    """
    module = _script()
    before_the_card_was_recorded = _result(
        module, rules_card_digest=module._UNSTAMPED, stamp_ms=1_000
    )
    stamped = _result(module, rules_card_digest="aaaaaaaaaaaa", stamp_ms=2_000)

    rows = module._latest_per_configuration([before_the_card_was_recorded, stamped])
    rows = module._drop_duplicated_unstamped(rows)

    assert [row.rules_card_digest for row in rows] == ["aaaaaaaaaaaa"]


def test_an_unstamped_run_with_no_stamped_twin_survives() -> None:
    """A model whose weights are gone can never be re-measured, so its one row must stay.

    Oracle: five models in the corpus were deleted after being benchmarked, and their scorecards
    are the only record that survives. Dropping every unstamped row would discard them, and no card
    can be inferred for one — which is why the rule is "already reported", never "must have been
    card X".
    """
    module = _script()
    only_record = _result(module, rules_card_digest=module._UNSTAMPED, completion_tokens=32_813)
    unrelated = _result(module, model="other/model", rules_card_digest="aaaaaaaaaaaa")

    rows = module._drop_duplicated_unstamped([only_record, unrelated])

    assert only_record in rows


def test_an_unstamped_run_matching_a_different_models_totals_survives() -> None:
    """The fingerprint is per model: two models hitting the same totals are not one measurement.

    Oracle: the totals are a fingerprint only because one model's decode is reproducible. Across
    models they are just numbers, and two models can land on the same token count — so a rule that
    ignored the model would delete a real scorecard belonging to another one.
    """
    module = _script()
    unstamped = _result(module, model="a/first", rules_card_digest=module._UNSTAMPED)
    same_totals_other_model = _result(module, model="b/second", rules_card_digest="aaaaaaaaaaaa")

    rows = module._drop_duplicated_unstamped([unstamped, same_totals_other_model])

    assert unstamped in rows


def test_two_stamped_cards_with_identical_totals_both_survive() -> None:
    """Only an unstamped row can be a duplicate; two named cards are two configurations.

    Oracle: a card that changed nothing about a model's output is itself the finding — the two rows
    are what shows the card made no difference. Collapsing them would erase that scorecard, and the
    heretic model measured 6/7 under both cards for exactly this reason.
    """
    module = _script()
    compact = _result(module, rules_card_digest="aaaaaaaaaaaa")
    doctrine = _result(module, rules_card_digest="bbbbbbbbbbbb")

    rows = module._drop_duplicated_unstamped([compact, doctrine])

    assert len(rows) == 2


def test_short_model_strips_the_weights_path_a_local_model_id_carries() -> None:
    """Oracle: a local model's id is its absolute weights path; a table needs the last segment."""
    module = _script()
    row = _result(module, model="/Users/someone/models/Qwen3.8-27B-abliterated")

    assert row.short_model == "Qwen3.8-27B-abliterated"


def test_the_style_count_is_read_from_the_scorecard_itself(tmp_path: Path) -> None:
    """The style column comes out of the scorecard, not out of the code a run produced.

    Oracle: the value asserted is the one written into the document, so nothing here depends on
    any file the run may or may not have kept. This replaced a nearest-timestamp join against a
    produced-code directory, which made a run's style column depend on whether 2 MB of model
    output — frequently not valid Python — was still sitting in the repository.
    """
    module = _script()
    _write_scorecard(tmp_path, 1_000, cases=7, rules_card_digest="aaaaaaaaaaaa", style_findings=3)

    (scorecard,) = module._load_scorecards(tmp_path)

    assert scorecard.style_findings == 3


def test_a_scorecard_predating_the_style_field_reports_it_as_unknown(tmp_path: Path) -> None:
    """``None`` and ``0`` are different answers: not measured, versus measured and clean.

    Oracle: seven scorecards in the corpus were written when the produced code had already been
    cleaned up, so no style count could be derived for them. Reporting those as ``0`` would claim
    they were linted and clean, which is a stronger and false statement.
    """
    module = _script()
    _write_scorecard(tmp_path, 1_000, cases=7, rules_card_digest="aaaaaaaaaaaa")

    (scorecard,) = module._load_scorecards(tmp_path)

    assert scorecard.style_findings is None


def _write_scorecard(directory: Path, stamp: int, cases: int, **extra: object) -> None:
    """Write a scorecard whose shape matches what ``Scorecard.write`` serializes."""
    document: dict[str, object] = {
        "model": "local/candidate",
        "cases_passed": cases,
        "cases_total": cases,
        "total_completion_tokens": 100,
        "total_model_seconds": 10.0,
        "cases": [
            {"case_id": f"{index:02d}", "status": "done", "attempts": 1} for index in range(cases)
        ],
    }
    document.update(extra)
    (directory / f"scorecard-local-candidate-{stamp}.json").write_text(
        json.dumps(document), encoding="utf-8"
    )


def test_a_partial_run_is_excluded_from_the_comparison(tmp_path: Path) -> None:
    """Oracle: a run that scored 1 case is not 1/7 at what a 7-case run measures.

    A single-case smoke test writes a real scorecard, so without this the table would rank a
    1/1 run above every full run that missed a case.
    """
    module = _script()
    _write_scorecard(tmp_path, 1_000, cases=7, rules_card_digest="aaaaaaaaaaaa")
    _write_scorecard(tmp_path, 2_000, cases=1, rules_card_digest="aaaaaaaaaaaa")

    scorecards = module._load_scorecards(tmp_path)

    assert [scorecard.cases_total for scorecard in scorecards] == [7]


def test_a_scorecard_written_before_the_card_axis_existed_is_kept_and_labelled(
    tmp_path: Path,
) -> None:
    """Oracle: 40+ scorecards predate the digest field; dropping them discards real measurements.

    They are labelled rather than guessed at — the verdict then excludes them, because a
    difference cannot be attributed to a card that cannot be named.
    """
    module = _script()
    _write_scorecard(tmp_path, 1_000, cases=7)

    (scorecard,) = module._load_scorecards(tmp_path)

    assert scorecard.rules_card_digest == module._UNSTAMPED


def test_the_card_verdict_compares_cards_within_one_mode_never_across_them(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Oracle: a verdict attributes a difference to the axis it names, so only that axis may vary.

    One model measured under ONE card in two modes is not a card comparison — it has a single
    card. Grouping on the model alone would pair those two rows and announce a winner between a
    card and itself, attributing the lever's whole cost to a card that never changed.
    """
    module = _script()

    module._print_card_verdicts(
        [
            _result(module, rules_card_digest="aaaaaaaaaaaa", plan_first=False),
            _result(module, rules_card_digest="aaaaaaaaaaaa", plan_first=True),
        ]
    )

    assert "No model has been measured under two NAMED cards yet." in capsys.readouterr().out


def test_the_mode_verdict_names_the_cheaper_mode_for_a_configuration_measured_in_both(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Oracle: the question the sweep was run to answer — does the planning generation pay off?

    Measured on one model under one card: 4436 completion tokens without the plan against 9858
    with it, both 7/7. The verdict is the one the artifacts support, so it survives the log the
    run was read from.
    """
    module = _script()

    module._print_mode_verdicts(
        [
            _result(module, plan_first=False, completion_tokens=4_436),
            _result(module, plan_first=True, completion_tokens=9_858),
        ]
    )

    printed = capsys.readouterr().out
    assert "off" in printed
    assert "4436" in printed
    assert "9858" in printed


def test_no_mode_verdict_is_offered_for_a_configuration_measured_in_one_mode_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Oracle: a comparison needs two arms. One arm is a measurement, not a winner.

    Every sweep before the lever was benchmarked ran unplanned, so this is the ordinary case —
    announcing ``off`` the winner there would report an A/B that was never run.
    """
    module = _script()

    module._print_mode_verdicts([_result(module, plan_first=False)])

    assert "measured in both modes" in capsys.readouterr().out


def test_a_scorecard_predating_the_lever_field_reports_it_as_unrecorded(tmp_path: Path) -> None:
    """``None`` and ``False`` are different answers: never recorded, versus recorded as off.

    Oracle: the same rule the style count already follows one field down. Reading an absent lever
    as baseline is an inference, and it stopped being a safe one the day --plan-first reached the
    two benchmark runners while the scorecard field was still a day away — a run in that window
    could plan and record nothing.
    """
    module = _script()
    _write_scorecard(tmp_path, 1_000, cases=7, rules_card_digest="aaaaaaaaaaaa")

    (scorecard,) = module._load_scorecards(tmp_path)

    assert scorecard.plan_first is None
    assert module._mode(scorecard) == "unrecorded"


def test_a_run_predating_the_lever_field_is_not_the_same_configuration_as_a_recorded_baseline(
    tmp_path: Path,
) -> None:
    """Oracle: an unknown mode and a known-off mode are two configurations, so two rows survive.

    Coercing the absent field to ``False`` put both on one drop key, and the rule then chose
    between them by stamp — a coin toss wearing a rule's clothes. The corpus holds exactly this
    pair: a 7/7 @4436 run whose lever predates the field, and the re-run under the fixed writer
    that recorded it. Today they agree only because the re-run happens to carry the later stamp.
    """
    module = _script()
    _write_scorecard(tmp_path, 1_000, cases=7, rules_card_digest="aaaaaaaaaaaa")
    _write_scorecard(tmp_path, 2_000, cases=7, rules_card_digest="aaaaaaaaaaaa", plan_first=False)

    rows = module._latest_per_configuration(module._load_scorecards(tmp_path))

    assert [row.plan_first for row in sorted(rows, key=lambda r: r.stamp_ms)] == [None, False]


def test_the_mode_verdict_ignores_a_run_whose_lever_was_never_recorded(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Oracle: here the lever is the verdict's SUBJECT, and no verdict can name an unknown.

    Counted as an arm, an unrecorded run satisfies the both-modes test against a genuine one and
    publishes "planning does not pay" out of a comparison whose other side may itself have
    planned. That is the failure the field exists to prevent, reintroduced by the reader.
    """
    module = _script()

    module._print_mode_verdicts(
        [
            _result(module, plan_first=None, completion_tokens=4_436),
            _result(module, plan_first=True, completion_tokens=9_858),
        ]
    )

    assert "measured in both modes" in capsys.readouterr().out


def test_the_card_verdict_keeps_unrecorded_runs_and_names_the_axis_it_could_not_hold(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Oracle: here the lever is the CONTROL and the card is the subject, so the verdict stands.

    The mirror of the rule above, and the reason the two differ. A named card can still be
    declared the winner when the control is unknown — what is owed is disclosure, not silence, so
    the mode column prints ``unrecorded`` instead of the ``off`` it used to assert. Eight models
    in the corpus are compared across two cards on exactly these terms; dropping them would
    discard the two-card finding to avoid admitting one caveat.
    """
    module = _script()

    module._print_card_verdicts(
        [
            _result(
                module, rules_card_digest="aaaaaaaaaaaa", plan_first=None, completion_tokens=4_436
            ),
            _result(
                module, rules_card_digest="bbbbbbbbbbbb", plan_first=None, completion_tokens=5_732
            ),
        ]
    )

    printed = capsys.readouterr().out
    assert "unrecorded" in printed
    assert "best: aaaaaaaaaaaa" in printed
