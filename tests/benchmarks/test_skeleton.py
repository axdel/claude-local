"""Walking-skeleton tests for the benchmark harness and minimal health case.

These tests drive benchmark data through the public ``claude_local`` entry point, with only the
external model transport replayed. They prove the real sandboxed oracle, stable-prefix context,
and driver-owned scratch-worktree teardown before the benchmark grows more layers.
"""

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from benchmarks.harness import BenchmarkCase, BenchmarkDriver, replay_http_client
from claude_local import Budget, ContextFile, Status

_ROOT = Path(__file__).parents[2]
_BENCHMARK = _ROOT / "benchmarks" / "schedule_manager"
_GOLDEN_APP = _BENCHMARK / "golden" / "app"
_HEALTH_CASE = _BENCHMARK / "cases" / "01_health"


def _read(path: Path) -> str:
    """Read one benchmark fixture exactly as it will enter a case."""
    return path.read_text(encoding="utf-8")


def _build_health_case() -> BenchmarkCase:
    """Build the canonical tracer case from its committed golden and oracle fixtures."""
    golden_main = ContextFile(path="app/main.py", content=_read(_GOLDEN_APP / "main.py"))
    golden_db = ContextFile(path="app/db.py", content=_read(_GOLDEN_APP / "db.py"))
    return BenchmarkCase.from_fixtures(
        impl_path="app/main.py",
        spec_text=_read(_HEALTH_CASE / "spec.md"),
        oracle_text=_read(_HEALTH_CASE / "oracle.py"),
        golden_tree=(golden_main, golden_db),
        blank_stub=_read(_HEALTH_CASE / "blank" / "app" / "main.py"),
        context_paths=(golden_db.path,),
        budget=Budget(max_attempts=1, max_tokens=2048, timeout_s=30.0),
    )


def test_health_case_contract_carries_golden_tree_stub_and_neighbor() -> None:
    """The tracer case contains one implementation hole and its selected golden neighbor."""
    case = _build_health_case()

    assert case.task.impl_path == "app/main.py"
    assert tuple(file.path for file in case.golden_tree) == ("app/main.py", "app/db.py")
    assert tuple(file.path for file in case.task.context_files) == ("app/db.py",)
    assert "FastAPI" in case.task.spec_text
    assert "create_app" not in case.blank_stub
    assert case.task.expected_tests == 4


def test_case_counts_only_module_level_oracle_tests() -> None:
    """Nested helper functions named test_* do not inflate pytest's collection pin."""
    case = _build_health_case()
    oracle_text = """\
def test_collected_behavior() -> None:
    def test_nested_helper() -> None:
        raise AssertionError
"""

    rebuilt_case = BenchmarkCase.from_fixtures(
        impl_path=case.task.impl_path,
        spec_text=case.task.spec_text,
        oracle_text=oracle_text,
        golden_tree=case.golden_tree,
        blank_stub=case.blank_stub,
        context_paths=tuple(file.path for file in case.task.context_files),
        budget=case.task.budget,
    )

    assert rebuilt_case.task.expected_tests == 1


def test_case_rejects_the_target_file_as_model_context() -> None:
    """The implementation answer cannot also appear among its read-only neighbors."""
    case = _build_health_case()
    target = next(file for file in case.golden_tree if file.path == case.task.impl_path)

    with pytest.raises(ValueError, match="implementation target"):
        replace(case, task=replace(case.task, context_files=(target,)))


def test_case_rejects_context_that_does_not_match_the_golden_tree() -> None:
    """Model-visible neighbors must be byte-identical to their assembled golden files."""
    case = _build_health_case()
    drifted_context = ContextFile(path="app/db.py", content="DEFAULT_DATABASE_PATH = 'wrong'\n")

    with pytest.raises(ValueError, match="must match its golden file"):
        replace(case, task=replace(case.task, context_files=(drifted_context,)))


def test_case_rejects_duplicate_or_missing_golden_target() -> None:
    """Exactly one golden file must own the implementation path replaced by the blank stub."""
    case = _build_health_case()
    golden_db = next(file for file in case.golden_tree if file.path == "app/db.py")

    with pytest.raises(ValueError, match="exactly once"):
        replace(case, golden_tree=(golden_db,))
    with pytest.raises(ValueError, match="duplicate golden-tree path"):
        replace(case, golden_tree=(*case.golden_tree, golden_db))


def test_case_rejects_case_insensitive_golden_path_collisions() -> None:
    """Fixture identity cannot depend on the host filesystem's case-sensitivity."""
    case = _build_health_case()
    colliding_db = ContextFile(path="APP/DB.PY", content="DATABASE_PATH = 'collision'\n")

    with pytest.raises(ValueError, match="case-insensitive golden-tree path"):
        replace(case, golden_tree=(*case.golden_tree, colliding_db))


def test_replay_client_emits_schema_complete_streaming_chunks() -> None:
    """Replay frames match the published role, content, finish, usage, and terminator shapes."""
    with (
        replay_http_client('TEXT = "世界"\n', impl_path="app/main.py") as http_client,
        http_client.stream("POST", "http://benchmark.local/v1/chat/completions") as response,
    ):
        event_lines = [line for line in response.iter_lines() if line]

    assert event_lines[-1] == "data: [DONE]"
    chunks = [json.loads(line.removeprefix("data: ")) for line in event_lines[:-1]]
    chunk_base = {
        "id": "chatcmpl-benchmark-replay",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "benchmark-replay",
    }
    assert chunks == [
        {
            **chunk_base,
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": ""},
                    "finish_reason": None,
                }
            ],
        },
        {
            **chunk_base,
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": 'FILE: app/main.py\nUTF8-BYTES: 16\n\nTEXT = "世界"\n'},
                    "finish_reason": None,
                }
            ],
        },
        {
            **chunk_base,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
        {
            **chunk_base,
            "choices": [],
            "usage": {"prompt_tokens": 0, "completion_tokens": 1, "total_tokens": 1},
        },
    ]


def test_driver_runs_health_case_green_with_neighbor_in_real_request(tmp_path: Path) -> None:
    """A golden reply passes the real sandbox and carries its selected neighbor on the wire."""
    case = _build_health_case()
    observed_requests: list[httpx.Request] = []
    observed_worktrees: list[Path] = []
    observed_assembled_files: dict[str, str] = {}
    golden_main = next(
        file.content for file in case.golden_tree if file.path == case.task.impl_path
    )
    scratch_root = tmp_path / "driver-worktrees"
    driver = BenchmarkDriver(
        scratch_root=scratch_root,
        base_url="http://benchmark.local",
        model="replay/golden",
        generation_params={"temperature": 0.0, "repetition_penalty": 1.1},
    )

    def observe_request(request: httpx.Request) -> None:
        observed_requests.append(request)
        (worktree,) = scratch_root.iterdir()
        observed_worktrees.append(worktree)
        observed_assembled_files["main"] = _read(worktree / "app" / "main.py")
        observed_assembled_files["db"] = _read(worktree / "app" / "db.py")

    with replay_http_client(
        golden_main, impl_path=case.task.impl_path, request_observer=observe_request
    ) as http_client:
        outcome = driver.run_case(case, http_client=http_client)

    assert outcome.status is Status.DONE
    assert outcome.code == golden_main
    assert outcome.files_changed == (case.task.impl_path,)
    assert outcome.record.model == "replay/golden"
    assert observed_assembled_files == {
        "main": case.blank_stub,
        "db": next(file.content for file in case.task.context_files),
    }
    assert len(observed_requests) == 1
    request = observed_requests[0]
    assert request.method == "POST"
    assert request.url == "http://benchmark.local/v1/chat/completions"
    assert request.headers["content-type"] == "application/json"
    request_body = json.loads(request.content)
    assert request_body["model"] == "replay/golden"
    assert request_body["stream"] is True
    assert request_body["stream_options"] == {"include_usage": True}
    assert request_body["max_tokens"] == case.task.budget.max_tokens
    assert request_body["temperature"] == 0.0
    assert request_body["repetition_penalty"] == 1.1
    system_message = request_body["messages"][0]
    assert system_message["role"] == "system"
    assert case.task.spec_text in system_message["content"]
    assert case.task.test_text in system_message["content"]
    assert request_body["messages"][1] == {"role": "user", "content": ""}
    assert "### app/db.py" in system_message["content"]
    assert "### app/main.py" not in system_message["content"]
    assert golden_main not in system_message["content"]
    assert next(file.content for file in case.task.context_files) in system_message["content"]
    assert len(observed_worktrees) == 1
    assert not observed_worktrees[0].exists()
    assert list(scratch_root.iterdir()) == []


def test_driver_seeded_stub_without_a_scored_edit_reports_no_code_and_cleans_up(
    tmp_path: Path,
) -> None:
    """A pre-seeded blank target is not model output when generation derails before an edit."""
    case = _build_health_case()
    derailed_case = replace(
        case,
        task=replace(case.task, budget=replace(case.task.budget, max_tokens=2)),
    )
    scratch_root = tmp_path / "driver-worktrees"
    driver = BenchmarkDriver(
        scratch_root=scratch_root,
        base_url="http://benchmark.local",
        model="replay/derailed",
    )

    with replay_http_client(
        "z" * 256,
        impl_path=derailed_case.task.impl_path,
    ) as http_client:
        outcome = driver.run_case(derailed_case, http_client=http_client)

    assert outcome.status is Status.DERAILED
    assert outcome.code is None
    assert outcome.files_changed == ()
    assert list(scratch_root.iterdir()) == []


def test_driver_preserves_a_reply_without_a_terminal_newline(tmp_path: Path) -> None:
    """Replay framing keeps complete source bytes when the model omits the final newline."""
    case = _build_health_case()
    golden_main = next(
        file.content for file in case.golden_tree if file.path == case.task.impl_path
    )
    reply_without_final_newline = golden_main.rstrip("\n")
    driver = BenchmarkDriver(
        scratch_root=tmp_path / "driver-worktrees",
        base_url="http://benchmark.local",
        model="replay/no-final-newline",
    )

    with replay_http_client(
        reply_without_final_newline, impl_path=case.task.impl_path
    ) as http_client:
        outcome = driver.run_case(case, http_client=http_client)

    assert outcome.status is Status.DONE
    assert outcome.code == reply_without_final_newline


def test_driver_rejects_behaviorally_wrong_health_reply_and_removes_worktree(
    tmp_path: Path,
) -> None:
    """An importable wrong implementation stays red, and cleanup still completes."""
    case = _build_health_case()
    golden_main = next(
        file.content for file in case.golden_tree if file.path == case.task.impl_path
    )
    wrong_main = golden_main.replace(
        'HealthResponse(status="ok")', 'HealthResponse(status="down")'
    )
    assert wrong_main != golden_main
    scratch_root = tmp_path / "driver-worktrees"
    driver = BenchmarkDriver(
        scratch_root=scratch_root,
        base_url="http://benchmark.local",
        model="replay/wrong-health",
    )

    with replay_http_client(wrong_main, impl_path=case.task.impl_path) as http_client:
        outcome = driver.run_case(case, http_client=http_client)

    assert outcome.status is Status.EXHAUSTED
    assert outcome.code == wrong_main
    assert outcome.files_changed == (case.task.impl_path,)
    assert outcome.record.attempts == 1
    assert list(scratch_root.iterdir()) == []


@pytest.mark.parametrize(
    "invalid_path", ["../escape.py", "/outside/escape.py", "app/", "test_loop_oracle.py"]
)
@pytest.mark.parametrize("path_source", ["implementation", "golden"])
def test_case_rejects_fixture_paths_outside_regular_files(
    invalid_path: str, path_source: str
) -> None:
    """Case construction refuses paths that could escape or replace a directory."""
    case = _build_health_case()

    with pytest.raises(ValueError, match="regular relative file"):
        if path_source == "implementation":
            replace(case, task=replace(case.task, impl_path=invalid_path))
        else:
            invalid_golden = replace(case.golden_tree[0], path=invalid_path)
            replace(case, golden_tree=(invalid_golden, *case.golden_tree[1:]))


def test_driver_removes_worktree_when_model_seam_raises(tmp_path: Path) -> None:
    """An escaping model-seam error still removes the exact child worktree it observed."""
    case = _build_health_case()
    scratch_root = tmp_path / "driver-worktrees"
    observed_worktrees: list[Path] = []
    driver = BenchmarkDriver(
        scratch_root=scratch_root,
        base_url="http://benchmark.local",
        model="replay/fault",
    )

    def abort_request(request: httpx.Request) -> None:
        del request
        (worktree,) = scratch_root.iterdir()
        observed_worktrees.append(worktree)
        raise RuntimeError("replay observer aborted")

    with (
        replay_http_client(
            case.blank_stub,
            impl_path=case.task.impl_path,
            request_observer=abort_request,
        ) as http_client,
        pytest.raises(RuntimeError, match="replay observer aborted"),
    ):
        driver.run_case(case, http_client=http_client)

    assert len(observed_worktrees) == 1
    assert not observed_worktrees[0].exists()
    assert list(scratch_root.iterdir()) == []
