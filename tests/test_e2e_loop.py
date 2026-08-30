"""End-to-end loop ceremony — the whole engine driven through Loop.run, only the model doubled.

This is the branch's FULL-tier live-test for a library surface (a pytest/REPL driver). It exercises
the real chain end to end: schema-derived SSE bytes -> the real ModelClient decode -> real
whole-file extraction -> a REAL immutable oracle run by a REAL `python -m pytest` subprocess in a
temp worktree -> real best-snapshot restore -> real telemetry aggregation and JSON write. The ONLY
seam doubled is the model: ReplayBackend replays pre-built streams, so no model is downloaded (the
branch's standing No-Go) yet the full path is proven — the loop's "prove it offline" design.

Three scenarios cover the terminal outcomes that do real work: (A) a partial then a green reply
reaches DONE with the green snapshot restored; (B) three partials exhaust the budget and the
least-bad valid partial is restored over a later regression; (C) a runaway generation derails
before any test runs. Each also asserts the completion ledger (scripts served == expected), that
the immutable oracle's bytes are untouched by the run, and that the economy record aggregates
consistently and round-trips to JSON on disk.

The inner `python -m pytest` uses the active interpreter explicitly, so every scenario exercises
the production child environment from an external `tmp_path` worktree without ambient-env setup.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from backend_doubles import RecordingReplayBackend
from factories import build_budget, build_task_spec, build_whole_file_reply
from sse_wire import sse_frame_json

from claude_local.backend import ReplayBackend
from claude_local.client import ModelClient
from claude_local.loop import Loop
from claude_local.prompt import PromptBuilder
from claude_local.runner import TestRunner
from claude_local.snapshot import SnapshotStore
from claude_local.types import Status

if TYPE_CHECKING:
    from claude_local.backend import Backend
    from claude_local.telemetry import LocalEconomyRecord
    from claude_local.types import TaskSpec

_ROOT = Path(__file__).parent.parent
_RULES_CARD = _ROOT / "src" / "claude_local" / "rules_card.md"
_FIXTURES = Path(__file__).parent / "fixtures" / "e2e"
_MODEL = "mlx-community/Qwen2.5-Coder-7B-Instruct-4bit"
_IMPL_PATH = "src/calculator.py"
_TOKENS_PER_REPLY = (
    40  # server-counted completion tokens each clean reply reports via its usage frame
)
_CHUNK: dict[str, object] = {"id": "chatcmpl-e2e", "object": "chat.completion.chunk"}


# --- fixtures & SSE assembly -------------------------------------------------------


def _read_fixture(name: str) -> str:
    """Read a captured e2e fixture (an oracle test or an implementation template) as text."""
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _clean_reply(content: str) -> bytes:
    """A wire-faithful clean stream: role chunk, one content delta, finish, usage trailer, [DONE].

    The frame shapes mirror tests/fixtures/sse/complete_stream.bytes (schema-derived from the
    OpenAI-compatible streaming contract), so the real decoder yields a Delta then a Usage and the
    client reports a server-counted token total (the non-estimated path).
    """
    return (
        sse_frame_json(
            {**_CHUNK, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]}
        )
        + sse_frame_json({**_CHUNK, "choices": [{"index": 0, "delta": {"content": content}}]})
        + sse_frame_json(
            {**_CHUNK, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        )
        + sse_frame_json(
            {
                **_CHUNK,
                "choices": [],
                "usage": {
                    "completion_tokens": _TOKENS_PER_REPLY,
                    "total_tokens": _TOKENS_PER_REPLY,
                },
            }
        )
        + b"data: [DONE]\n\n"
    )


def _impl_reply(fixture_name: str) -> bytes:
    """A clean reply whose one delta is the canonical byte-counted implementation frame."""
    payload = _read_fixture(fixture_name)
    return _clean_reply(build_whole_file_reply(_IMPL_PATH, payload))


def _runaway_reply(size: int = 256) -> bytes:
    """A single oversized content delta, no usage trailer — trips the derail guard mid-decode."""
    return sse_frame_json(
        {**_CHUNK, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]}
    ) + sse_frame_json({**_CHUNK, "choices": [{"index": 0, "delta": {"content": "z" * size}}]})


# --- worktree, loop, and record helpers --------------------------------------------


def _make_worktree(tmp_path: Path) -> Path:
    """A temp worktree with the permitted src/ subtree that apply_file and SnapshotStore need."""
    (tmp_path / "src").mkdir()
    return tmp_path


def _build_loop(worktree: Path, backend: Backend) -> tuple[Loop, ModelClient]:
    """The production loop with every real collaborator; only `backend` (the model) is doubled."""
    client = ModelClient(backend)
    loop = Loop(
        client=client,
        prompt_builder=PromptBuilder(_RULES_CARD),
        runner=TestRunner(),  # DEFAULT spawn — a real `python -m pytest` subprocess
        snapshots=SnapshotStore(worktree, "src"),
        model=_MODEL,
    )
    return loop, client


def _spec(oracle: str, *, max_attempts: int, max_tokens: int = 4096) -> TaskSpec:
    """A three-test task spec carrying the immutable oracle and the loop's per-task budget."""
    return build_task_spec(
        impl_path=_IMPL_PATH,
        spec_text="Implement add(a, b) returning the integer sum of its two arguments.",
        test_text=oracle,
        expected_tests=3,
        budget=build_budget(max_attempts=max_attempts, max_tokens=max_tokens),
    )


def _sole_oracle(worktree: Path) -> Path:
    """Locate the single immutable oracle test the loop wrote at the worktree root (black-box)."""
    written = list(worktree.glob("test_*.py"))
    assert len(written) == 1, f"expected exactly one oracle test at root, found {written}"
    return written[0]


def _assert_record_consistent(
    record: LocalEconomyRecord, *, status: Status, attempts: int, calls: int, estimated: bool
) -> None:
    """The economy record's internal invariants: counts match, tokens counted, mean is guarded."""
    assert record.status is status
    assert record.model == _MODEL
    assert record.attempts == attempts
    assert record.total_calls == calls
    assert record.tokens_estimated is estimated
    assert record.total_completion_tokens > 0
    assert record.total_model_seconds >= 0.0
    if record.total_model_seconds > 0:
        expected_mean = record.total_completion_tokens / record.total_model_seconds
        assert record.mean_tokens_per_second == pytest.approx(expected_mean)
    else:
        assert record.mean_tokens_per_second is None


def _assert_record_written(record: LocalEconomyRecord, directory: Path, *, status: str) -> None:
    """The record round-trips to JSON on disk with fields matching the run (telemetry write)."""
    path = record.write(directory)
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["status"] == status
    assert data["model"] == _MODEL
    assert data["total_calls"] == record.total_calls
    assert data["attempts"] == record.attempts
    assert data["total_completion_tokens"] == record.total_completion_tokens


# --- Scenario A: partial -> worse partial -> green reaches DONE ---------------------


def test_e2e_retry_carries_pytest_failure_not_prior_implementation(tmp_path: Path) -> None:
    """Attempt two receives real sandboxed pytest diagnostics and never echoes model source."""
    worktree = _make_worktree(tmp_path)
    oracle = _read_fixture("oracle_test.txt")
    prior_implementation = _read_fixture("impl_abs.txt").strip()
    backend = RecordingReplayBackend(
        [_impl_reply("impl_abs.txt"), _impl_reply("impl_correct.txt")]
    )
    loop, _ = _build_loop(worktree, backend)

    result = loop.run(_spec(oracle, max_attempts=2), worktree)

    assert result.status is Status.DONE
    assert len(backend.calls) == 2
    first_prefix, first_tail = backend.calls[0]
    second_prefix, second_tail = backend.calls[1]
    assert first_prefix == second_prefix
    assert first_tail == ""
    assert "assert 10 == -10" in second_tail
    assert prior_implementation not in second_tail


def test_e2e_partial_then_green_reaches_done(tmp_path: Path) -> None:
    """A partial, a worse partial, then a correct reply: reaches DONE on the green attempt.

    The whole chain runs for real — decoded SSE, whole-file extraction, a real oracle run by a real
    `python -m pytest` in a temp worktree, real snapshot restore — with only the model replayed.
    """
    worktree = _make_worktree(tmp_path)
    oracle = _read_fixture("oracle_test.txt")
    backend = ReplayBackend(
        [
            _impl_reply("impl_abs.txt"),  # attempt 0: 2/3 — wrong only on the negatives case
            _impl_reply("impl_minus.txt"),  # attempt 1: 1/3
            _impl_reply("impl_correct.txt"),  # attempt 2: 3/3 — green
        ]
    )
    loop, client = _build_loop(worktree, backend)

    result = loop.run(_spec(oracle, max_attempts=5), worktree)

    assert result.status is Status.DONE
    assert result.best_score is not None
    assert result.best_score.is_green and result.best_score.passed == 3
    # completion ledger: stopped ON the green reply, so exactly three of five allowed were served
    assert client.total_calls == 3
    assert backend.served == 3
    # best (green) restored to the real subtree
    assert "return a + b" in (worktree / "src" / "calculator.py").read_text(encoding="utf-8")
    # immutable oracle: exactly one test file at root, byte-equal to the fixture the loop was given
    assert _sole_oracle(worktree).read_text(encoding="utf-8") == oracle
    # economy record: consistent, server-counted (clean finishes), and round-trips to disk
    _assert_record_consistent(
        result.record, status=Status.DONE, attempts=3, calls=3, estimated=False
    )
    assert (
        result.record.total_completion_tokens == 3 * _TOKENS_PER_REPLY
    )  # aggregation over 3 replies
    _assert_record_written(result.record, tmp_path / "economy", status="done")


# --- Scenario B: three partials exhaust the budget; least-bad restored --------------


def test_e2e_all_partial_exhausts_and_restores_least_bad(tmp_path: Path) -> None:
    """Three partial replies to a three-attempt budget: EXHAUSTED, with the least-bad partial kept.

    Proves the best-passing snapshot survives a later regression end to end: the 2/3 attempt lands
    on disk though the final 0/3 attempt ran last — the D-SNAPSHOT-001 guarantee over a subtree.
    """
    worktree = _make_worktree(tmp_path)
    oracle = _read_fixture("oracle_test.txt")
    backend = ReplayBackend(
        [
            _impl_reply("impl_abs.txt"),  # 2/3 — the least-bad
            _impl_reply("impl_minus.txt"),  # 1/3
            _impl_reply("impl_times.txt"),  # 0/3 — ran last, must NOT be what remains on disk
        ]
    )
    loop, client = _build_loop(worktree, backend)

    result = loop.run(_spec(oracle, max_attempts=3), worktree)

    assert result.status is Status.EXHAUSTED
    assert result.best_score is not None
    assert result.best_score.passed == 2 and not result.best_score.is_green
    assert client.total_calls == 3
    assert backend.served == 3
    # least-bad (abs, 2/3) restored — NOT the last (times, 0/3) attempt
    impl = (worktree / "src" / "calculator.py").read_text(encoding="utf-8")
    assert "abs(a) + abs(b)" in impl
    assert "a * b" not in impl
    assert _sole_oracle(worktree).read_text(encoding="utf-8") == oracle
    _assert_record_consistent(
        result.record, status=Status.EXHAUSTED, attempts=3, calls=3, estimated=False
    )
    _assert_record_written(result.record, tmp_path / "economy", status="exhausted")


# --- Scenario C: a runaway generation derails before any test runs ------------------


def test_e2e_runaway_generation_derails(tmp_path: Path) -> None:
    """An oversized first reply trips the derail guard: DERAILED, no attempt scored, no write.

    The derail cuts decode before the usage trailer, so the aborted call's tokens are ESTIMATED
    from decoded chars, never dropped (D-TELEMETRY-001) — asserted on the real client's metering.
    """
    worktree = _make_worktree(tmp_path)
    oracle = _read_fixture("oracle_test.txt")
    backend = ReplayBackend([_runaway_reply(size=256)])
    loop, client = _build_loop(worktree, backend)

    # tiny token cap: 256 decoded chars blow past it, tripping the guard on the first content delta
    result = loop.run(_spec(oracle, max_attempts=3, max_tokens=2), worktree)

    assert result.status is Status.DERAILED
    assert result.best_score is None  # derailed before any attempt was scored
    assert client.total_calls == 1
    assert backend.served == 1
    # no implementation was ever applied (derail precedes any write to the permitted path)
    assert not (worktree / "src" / "calculator.py").exists()
    # the loop still wrote the immutable oracle at start, and it is untouched
    assert _sole_oracle(worktree).read_text(encoding="utf-8") == oracle
    # aborted-call tokens are estimated from chars, not dropped
    _assert_record_consistent(
        result.record, status=Status.DERAILED, attempts=1, calls=1, estimated=True
    )
    _assert_record_written(result.record, tmp_path / "economy", status="derailed")


# --- Runner-layer regression: fresh source scored, never stale bytecode -------------

_PROBE_ORACLE = (
    "import sys\n"
    "from pathlib import Path\n"
    "\n"
    "sys.path.insert(0, str(Path(__file__).parent / 'src'))\n"
    "\n"
    "from probe import VALUE\n"
    "\n"
    "\n"
    "def test_value_is_two() -> None:\n"
    "    assert VALUE == 2\n"
)


def test_runner_rescores_fresh_source_after_same_size_rewrite(tmp_path: Path) -> None:
    """The runner scores the CURRENT source after a same-size rewrite — never a stale .pyc.

    Regression for the stale-bytecode hazard the loop's REPAIR cycle triggers: CPython validates
    ``__pycache__`` by ``(size, second-granularity mtime)``, so an impl rewritten same-size within
    one clock second of the previous one would otherwise import the stale ``.pyc`` and score the
    WRONG source. Two same-byte-length sources — ``VALUE = 1`` then ``VALUE = 2`` — are written and
    scored back to back through the REAL default spawn; the second must read the new source.
    This drives ``TestRunner`` directly (not the whole loop), pinning the guarantee at its layer.
    """
    worktree = _make_worktree(tmp_path)
    oracle = worktree / "test_probe_oracle.py"
    oracle.write_text(_PROBE_ORACLE, encoding="utf-8")
    probe = worktree / "src" / "probe.py"
    runner = TestRunner()  # DEFAULT spawn — the real `python -m pytest` subprocess

    probe.write_text(
        "VALUE = 1\n", encoding="utf-8"
    )  # first attempt: oracle wants 2, so not green
    first = runner.run(oracle, worktree, expected=1)
    assert not first.score.is_green

    probe.write_text("VALUE = 2\n", encoding="utf-8")  # same byte length; the correcting attempt
    second = runner.run(oracle, worktree, expected=1)
    assert second.score.is_green  # must compile the NEW source, not reuse stale bytecode
