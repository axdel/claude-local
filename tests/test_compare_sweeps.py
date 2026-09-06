"""Tests for the cross-run comparison (``scripts/compare-sweeps.py``).

The script decides which (model, rules card) pair to actually use, so its two pieces of real logic
are the ones that can silently produce a wrong recommendation: picking ONE row per configuration
(a superseded run reported as current would recommend a fixed loop's old behaviour), and pairing a
scorecard with the produced code whose style it reports (a mismatched pair attributes one run's
findings to another). Both are exercised against real files in a temp directory — the filesystem
is local-substitutable, so nothing here is mocked.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

_SCRIPT = Path(__file__).parents[1] / "scripts" / "compare-sweeps.py"


def _script() -> ModuleType:
    """Import the script by path, the way its shebang runs it.

    Its name is hyphenated and it lives outside the package, so no import statement can name it.
    """
    spec = importlib.util.spec_from_file_location("compare_sweeps", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _result(module: ModuleType, **overrides: object) -> Any:
    """A canonical ``SweepResult``; a test overrides only the field it exercises.

    Returns ``Any`` because the script is loaded by path: its classes exist at runtime but have no
    statically-nameable type, so annotating anything narrower would be a fiction the checker then
    enforces against the real attributes.
    """
    fields: dict[str, object] = {
        "model": "local/candidate",
        "rules_card_digest": "aaaaaaaaaaaa",
        "stamp_ms": 1_000,
        "cases_passed": 7,
        "cases_total": 7,
        "completion_tokens": 5_000,
        "model_seconds": 300.0,
        "attempts": 7,
        "style_findings": 0,
    }
    fields.update(overrides)
    return module.SweepResult(**fields)  # type: ignore[arg-type]


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
    every card A/B and report the survivor as that model's result.
    """
    module = _script()
    compact = _result(module, rules_card_digest="aaaaaaaaaaaa", stamp_ms=1_000)
    doctrine = _result(module, rules_card_digest="bbbbbbbbbbbb", stamp_ms=2_000)

    rows = module._latest_per_configuration([compact, doctrine])

    assert {row.rules_card_digest for row in rows} == {"aaaaaaaaaaaa", "bbbbbbbbbbbb"}


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


def test_short_model_strips_the_weights_path_a_local_model_id_carries() -> None:
    """Oracle: a local model's id is its absolute weights path; a table needs the last segment."""
    module = _script()
    row = _result(module, model="/Users/someone/models/Qwen3.8-27B-abliterated")

    assert row.short_model == "Qwen3.8-27B-abliterated"


def test_code_directory_pairs_with_the_scorecard_written_milliseconds_apart(
    tmp_path: Path,
) -> None:
    """Historical runs did not share a stamp between their two artifacts, so the join tolerates it.

    Oracle: every scorecard written before ``run.py`` was changed to read the clock once came from
    two separate reads, which is why the pair observed on disk is ``…723.json`` beside ``…724``.
    Those runs are most of the corpus, so an exact-match join would silently drop their style
    column. Runs written since do match exactly, and the same nearest-match join accepts them.
    """
    module = _script()
    scorecard = tmp_path / "scorecard-local-candidate-1788709222723.json"
    scorecard.write_text("{}", encoding="utf-8")
    code = tmp_path / "code-local-candidate-1788709222724"
    code.mkdir()

    paired = module._paired_code_directory(scorecard, 1788709222723, [code])

    assert paired == code


def test_code_directory_pairs_when_the_two_stamps_are_identical(tmp_path: Path) -> None:
    """The nearest-match join accepts a zero gap, which is what every run now produces.

    Oracle: ``run.py`` reads the clock once and hands the same stamp to both writers, so a current
    run's two names differ only by their prefix. A join written purely around the historical
    millisecond gap — one that required a nonzero difference — would pair every old run and no new
    one, which is the regression this pins.
    """
    module = _script()
    scorecard = tmp_path / "scorecard-local-candidate-1788709222723.json"
    scorecard.write_text("{}", encoding="utf-8")
    code = tmp_path / "code-local-candidate-1788709222723"
    code.mkdir()

    assert module._paired_code_directory(scorecard, 1788709222723, [code]) == code


def test_a_code_directory_from_a_different_run_is_not_paired(tmp_path: Path) -> None:
    """Oracle: runs are minutes apart, so a candidate outside the tolerance belongs to another run.

    Attributing one run's style findings to another is worse than reporting none: the number looks
    authoritative and describes different code.
    """
    module = _script()
    scorecard = tmp_path / "scorecard-local-candidate-1788709222723.json"
    scorecard.write_text("{}", encoding="utf-8")
    other_run = tmp_path / "code-local-candidate-1788709900000"
    other_run.mkdir()

    assert module._paired_code_directory(scorecard, 1788709222723, [other_run]) is None


def test_a_code_directory_for_a_different_model_is_not_paired(tmp_path: Path) -> None:
    """Oracle: the slug identifies the model, so another model's same-instant run is not this one.

    A sweep serves one model at a time, but nothing prevents two runs sharing a millisecond, and
    the timestamp alone cannot tell them apart.
    """
    module = _script()
    scorecard = tmp_path / "scorecard-local-candidate-1788709222723.json"
    scorecard.write_text("{}", encoding="utf-8")
    other_model = tmp_path / "code-other-model-1788709222724"
    other_model.mkdir()

    assert module._paired_code_directory(scorecard, 1788709222723, [other_model]) is None


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

    results = module._load_scorecards(tmp_path)

    assert [result.cases_total for result in results] == [7]


def test_a_scorecard_written_before_the_card_axis_existed_is_kept_and_labelled(
    tmp_path: Path,
) -> None:
    """Oracle: 40+ scorecards predate the digest field; dropping them discards real measurements.

    They are labelled rather than guessed at — the verdict then excludes them, because a
    difference cannot be attributed to a card that cannot be named.
    """
    module = _script()
    _write_scorecard(tmp_path, 1_000, cases=7)

    (result,) = module._load_scorecards(tmp_path)

    assert result.rules_card_digest == module._UNSTAMPED
