"""Fail-to-pass conformance for every benchmark case in the ladder.

Each case must bite: the committed golden file drives the loop to a DONE oracle,
and the deliberately incomplete blank stub does not. This is the data-driven
generalization of the scaffold mechanics proven in test_skeleton.py — one
parametrized proof over every ``case.toml`` directory, so no rung can ship
without its own fail-to-pass guarantee. Cases run through the real sandboxed
oracle with only the model transport replayed.
"""

from dataclasses import replace
from pathlib import Path

import pytest

from benchmarks.harness import BenchmarkCase, BenchmarkDriver, load_case, replay_http_client
from claude_local import Outcome, Status

_ROOT = Path(__file__).parents[2]
_BENCHMARK = _ROOT / "benchmarks" / "schedule_manager"
_GOLDEN_APP = _BENCHMARK / "golden" / "app"
_CASES_ROOT = _BENCHMARK / "cases"

_CASE_DIRS = sorted(path.parent for path in _CASES_ROOT.glob("*/case.toml"))


@pytest.fixture(params=_CASE_DIRS, ids=lambda case_dir: case_dir.name)
def case(request: pytest.FixtureRequest) -> BenchmarkCase:
    """Load each committed case directory in turn from its manifest and fixtures."""
    return load_case(request.param, golden_app_root=_GOLDEN_APP)


def _golden_impl(case: BenchmarkCase) -> str:
    """Return the golden content of the one file this case blanks."""
    return next(file.content for file in case.golden_tree if file.path == case.task.impl_path)


def _replay_outcome(case: BenchmarkCase, reply: str, scratch_root: Path) -> Outcome:
    """Run the case through the real loop with the model transport replaying ``reply``.

    The reply is deterministic, so one attempt fully proves fail-to-pass; retries would
    only re-run the sandboxed oracle without changing the verdict (the repair loop itself
    is covered by test_skeleton.py).
    """
    single_attempt = replace(
        case, task=replace(case.task, budget=replace(case.task.budget, max_attempts=1))
    )
    driver = BenchmarkDriver(
        scratch_root=scratch_root,
        base_url="http://benchmark.local",
        model="replay/f2p",
    )
    with replay_http_client(reply, impl_path=single_attempt.task.impl_path) as http_client:
        return driver.run_case(single_attempt, http_client=http_client)


def test_golden_file_drives_the_case_to_done(case: BenchmarkCase, tmp_path: Path) -> None:
    """Replaying the committed golden file passes the case's immutable oracle."""
    outcome = _replay_outcome(case, _golden_impl(case), tmp_path / "golden")

    assert outcome.status is Status.DONE
    assert outcome.files_changed == (case.task.impl_path,)


def test_blank_stub_does_not_pass_the_case(case: BenchmarkCase, tmp_path: Path) -> None:
    """Replaying the deliberately incomplete blank stub fails the case's oracle."""
    outcome = _replay_outcome(case, case.blank_stub, tmp_path / "blank")

    assert outcome.status is not Status.DONE
