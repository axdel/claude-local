"""Suite-runner tests for the full benchmark driver over the whole golden-app case ladder.

These tests drive every rung through the public ``run_suite`` with only the external model
transport replayed. They prove the suite runs each case in its own disposable worktree with a
fresh in-memory database (all rungs green when fed their golden files), aggregates a mixed
pass/fail run in order, forwards the model and generation parameters to every case, and tears
every scratch worktree down — plus the per-case dispatch and failure paths of the suite-wide
replay transport.
"""

import json
from pathlib import Path

import httpx
import pytest

from benchmarks.harness import (
    BenchmarkCase,
    CaseResult,
    load_suite,
    replay_suite_http_client,
    run_suite,
)
from claude_local import Status

_ROOT = Path(__file__).parents[2]
_BENCHMARK = _ROOT / "benchmarks" / "schedule_manager"
_GOLDEN_APP = _BENCHMARK / "golden" / "app"
_CASES = _BENCHMARK / "cases"
_COMPLETIONS = "http://benchmark.local/v1/chat/completions"


def _load_all_cases() -> dict[str, BenchmarkCase]:
    """Every committed case, keyed by rung-directory name in ladder order."""
    return load_suite(_CASES, golden_app_root=_GOLDEN_APP)


def _golden_source(case: BenchmarkCase) -> str:
    """Return the golden implementation text for the file this case blanks."""
    return next(file.content for file in case.golden_tree if file.path == case.task.impl_path)


def _system_message(target_impl_path: str) -> dict[str, object]:
    """A minimal chat-completion body naming ``target_impl_path`` the way the prompt does."""
    return {"messages": [{"role": "system", "content": f"Target file: {target_impl_path}\n"}]}


def test_run_suite_runs_every_rung_green_with_golden_replies(tmp_path: Path) -> None:
    """Fed each hole's golden file, every rung reaches DONE in ladder order and cleans up.

    A golden reply reproduces the complete reference app, so its immutable oracle must pass — the
    strongest end-to-end proof the suite assembles, runs, and isolates each case correctly.
    """
    cases = _load_all_cases()
    sources = {case.task.impl_path: _golden_source(case) for case in cases.values()}
    scratch_root = tmp_path / "suite-worktrees"

    with replay_suite_http_client(sources) as http_client:
        results = run_suite(
            cases,
            base_url="http://benchmark.local",
            model="replay/golden",
            scratch_root=scratch_root,
            http_client=http_client,
        )

    assert [result.case_id for result in results] == list(cases)
    assert all(isinstance(result, CaseResult) for result in results)
    assert all(result.outcome.status is Status.DONE for result in results)
    assert all(result.outcome.code == sources[result.outcome.impl_path] for result in results)
    assert all(result.outcome.record.model == "replay/golden" for result in results)
    assert list(scratch_root.iterdir()) == []


def test_run_suite_aggregates_a_passing_and_a_failing_case_in_order(tmp_path: Path) -> None:
    """One green and one red case aggregate independently, in insertion order, both cleaned up."""
    cases = _load_all_cases()
    passing_case = cases["03_repositories"]
    failing_case = cases["01_scaffold"]
    passing_source = _golden_source(passing_case)
    failing_source = _golden_source(failing_case).replace(
        'HealthResponse(status="ok")', 'HealthResponse(status="down")'
    )
    assert failing_source != _golden_source(failing_case)
    suite = {"passing": passing_case, "failing": failing_case}
    sources = {
        passing_case.task.impl_path: passing_source,
        failing_case.task.impl_path: failing_source,
    }
    scratch_root = tmp_path / "suite-worktrees"

    with replay_suite_http_client(sources) as http_client:
        results = run_suite(
            suite,
            base_url="http://benchmark.local",
            model="replay/mixed",
            scratch_root=scratch_root,
            http_client=http_client,
        )

    by_id = {result.case_id: result for result in results}
    assert [result.case_id for result in results] == ["passing", "failing"]
    assert by_id["passing"].outcome.status is Status.DONE
    assert by_id["passing"].outcome.code == passing_source
    assert by_id["failing"].outcome.status is Status.EXHAUSTED
    assert by_id["failing"].outcome.code == failing_source
    assert by_id["failing"].outcome.record.attempts == failing_case.task.budget.max_attempts
    assert list(scratch_root.iterdir()) == []


def test_run_suite_forwards_model_generation_params_and_base_url_to_each_case(
    tmp_path: Path,
) -> None:
    """The suite's model, generation parameters, and base URL reach the per-case request."""
    cases = {"scaffold": _load_all_cases()["01_scaffold"]}
    case = cases["scaffold"]
    sources = {case.task.impl_path: _golden_source(case)}
    observed: list[httpx.Request] = []

    with replay_suite_http_client(sources, request_observer=observed.append) as http_client:
        results = run_suite(
            cases,
            base_url="http://benchmark.local",
            model="replay/params",
            generation_params={"temperature": 0.0, "repetition_penalty": 1.15},
            scratch_root=tmp_path / "suite-worktrees",
            http_client=http_client,
        )

    assert results[0].outcome.record.model == "replay/params"
    assert len(observed) == 1
    assert str(observed[0].url) == _COMPLETIONS
    body = json.loads(observed[0].content)
    assert body["model"] == "replay/params"
    assert body["temperature"] == 0.0
    assert body["repetition_penalty"] == 1.15


def test_run_suite_leaves_an_injected_client_open_for_its_caller(tmp_path: Path) -> None:
    """The suite runner never closes a client it did not create — the caller owns its lifecycle."""
    cases = {"scaffold": _load_all_cases()["01_scaffold"]}
    case = cases["scaffold"]
    sources = {case.task.impl_path: _golden_source(case)}

    with replay_suite_http_client(sources) as http_client:
        run_suite(
            cases,
            base_url="http://benchmark.local",
            model="replay/golden",
            scratch_root=tmp_path / "suite-worktrees",
            http_client=http_client,
        )
        assert not http_client.is_closed

    assert http_client.is_closed


def test_replay_suite_client_dispatches_each_target_to_its_own_source() -> None:
    """The transport routes a request to the source named by its ``Target file:`` marker."""
    sources = {"app/a.py": "A_MARKER = 1\n", "app/b.py": "B_MARKER = 2\n"}

    with (
        replay_suite_http_client(sources) as client,
        client.stream("POST", _COMPLETIONS, json=_system_message("app/b.py")) as response,
    ):
        body = "".join(response.iter_lines())

    assert "FILE: app/b.py" in body
    assert "B_MARKER = 2" in body
    assert "A_MARKER" not in body


def test_replay_suite_client_rejects_an_unmapped_target_file() -> None:
    """A request for a target with no replay source fails loud rather than mis-routing."""
    with (
        replay_suite_http_client({"app/main.py": "x = 1\n"}) as client,
        pytest.raises(ValueError, match="no replay source for target file"),
    ):
        client.post(_COMPLETIONS, json=_system_message("app/absent.py"))


def test_replay_suite_client_rejects_a_request_without_a_target_file() -> None:
    """A request whose system message declares no target file fails loud."""
    with (
        replay_suite_http_client({"app/main.py": "x = 1\n"}) as client,
        pytest.raises(ValueError, match="declared no target file"),
    ):
        client.post(_COMPLETIONS, json={"messages": [{"role": "system", "content": "none"}]})
