"""Tests for the loop orchestrator spine (``claude_local.loop``).

The loop drives the red→green cycle: build the KV-cacheable prefix once, generate, apply the
whole-file reply to the one permitted impl path, score the immutable oracle test, snapshot each
attempt, and on exit restore the best and classify a terminal ``Status``. Two seams are doubled at
their genuine external boundaries — the transport (``ReplayBackend`` feeds a REAL ``ModelClient``)
and the pytest subprocess (``ScriptedSpawn`` writes a CAPTURED JUnit fixture for a REAL
``TestRunner``); the ``SnapshotStore`` and ``PromptBuilder`` are real in-process collaborators.

Every expected value is derived independently of the loop: the terminal precedence is hand-derived
from the plan's rule (DONE-best > DERAILED > BLOCKED > EXHAUSTED), the JUnit fixtures are captured
pytest reports with known counts (``all_pass`` 3/3, ``one_failure`` 2/3, ``import_error`` 0-passed
crash), and the restored bytes are the exact whole-file text the winning script carried.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from factories import build_budget, build_task_spec, build_test_score

from claude_local.backend import BackendUnavailable, ReplayBackend
from claude_local.client import ModelClient
from claude_local.loop import _ORACLE_TEST_FILENAME, Loop, _classify_terminal
from claude_local.prompt import PromptBuilder
from claude_local.runner import OracleError, TestRunner
from claude_local.snapshot import SnapshotStore
from claude_local.types import Status

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from claude_local.types import Budget

RULES_CARD = Path(__file__).parent.parent / "src" / "claude_local" / "rules_card.md"
JUNIT = Path(__file__).parent / "fixtures" / "junit"

# A distinctive oracle test body, so the oracle-write assertion pins content, not just existence.
_ORACLE_TEXT = "def test_widget():\n    from src import widget\n    assert widget.VALUE == 1\n"

# Whole-file impl bodies each script carries — distinct so a restore assertion pins exact bytes.
_V0 = "# widget v0\nVALUE = 0"
_V1 = "# widget v1\nVALUE = 1"
_V2 = "# widget v2\nVALUE = 2"


# --- Fixture / double builders ----------------------------------------------------


def _junit(name: str) -> str:
    """The captured JUnit report ``name`` — a real pytest xunit2 report, not hand-authored XML."""
    return (JUNIT / name).read_text(encoding="utf-8")


def _sse_script(text: str) -> bytes:
    """One OpenAI-style SSE stream whose single content delta is ``text`` (json.dumps escapes it).

    Mirrors the wire shape captured in the sse fixtures (one ``data:`` frame, no usage trailer), so
    the real client decodes it to exactly ``text`` — a transport carrier, not a hand-read protocol.
    """
    payload = json.dumps({"choices": [{"delta": {"content": text}}]})
    return f"data: {payload}\n\n".encode()


def _edit_script(body: str) -> bytes:
    """An SSE stream with one fenced whole-file block — extract_files routes it to permitted."""
    return _sse_script(f"```\n{body}\n```")


def _forbidden_edit_script(target: str, body: str) -> bytes:
    """An SSE stream whose block names ``target`` — apply_files must refuse a forbidden path."""
    return _sse_script(f"FILE: {target}\n```\n{body}\n```")


def _error_script(message: str) -> bytes:
    """An SSE stream carrying one upstream ``{"error": ...}`` frame — a fault, not a text delta."""
    payload = json.dumps({"error": {"message": message}})
    return f"data: {payload}\n\n".encode()


def _report_path(cmd: Sequence[str]) -> Path:
    """The ``--junit-xml=<path>`` target TestRunner asked the spawn to produce."""
    for arg in cmd:
        if arg.startswith("--junit-xml="):
            return Path(arg[len("--junit-xml=") :])
    raise AssertionError(f"no --junit-xml target in {cmd!r}")  # a test-side invariant


class ScriptedSpawn:
    """A TestRunner spawn double: writes the next captured JUnit report to the path in argv.

    One call per scored attempt; over-reading past the script raises ``IndexError`` so a miscount
    of attempts surfaces loudly rather than silently repeating a verdict.
    """

    def __init__(self, *reports: str) -> None:
        self._reports = list(reports)
        self._i = 0

    def __call__(self, cmd: Sequence[str], cwd: Path, write_box: Path) -> None:
        del cwd, write_box  # the fake ignores the worktree/box; it writes only where argv points
        body = self._reports[self._i]
        self._i += 1
        _report_path(cmd).write_text(body, encoding="utf-8")


def _silent_spawn(cmd: Sequence[str], cwd: Path, write_box: Path) -> None:
    """A spawn that produces NO report — TestRunner.run must raise OracleError (broken oracle)."""
    del cmd, cwd, write_box  # intentionally produce no JUnit report, to exercise broken-oracle


class RecordingBackend:
    """Wraps a ReplayBackend, capturing each ``(prefix, tail)`` for a prefix-stability assert."""

    def __init__(self, scripts: Sequence[bytes]) -> None:
        self._inner = ReplayBackend(scripts)
        self.calls: list[tuple[str, str]] = []

    def generate(self, prefix: str, tail: str, budget: Budget) -> Iterator[bytes]:
        self.calls.append((prefix, tail))
        return self._inner.generate(prefix, tail, budget)


class UnavailableBackend:
    """A backend whose generation raises ``BackendUnavailable`` — the server-unreachable case.

    Stands in for ``HttpxBackend`` translating a transport failure to the domain fault; the loop
    must let it propagate, never fold it into a terminal ``Status``.
    """

    def generate(self, prefix: str, tail: str, budget: Budget) -> Iterator[bytes]:
        del prefix, tail, budget  # the server is down before any request shape matters
        raise BackendUnavailable("http://local:8080", "m", "connection refused")


class CountingPromptBuilder(PromptBuilder):
    """A PromptBuilder that counts ``stable_prefix`` calls — pins build-once-per-task."""

    def __init__(self, card_path: Path) -> None:
        super().__init__(card_path)
        self.prefix_calls = 0

    def stable_prefix(self, spec: object) -> str:  # type: ignore[override]
        self.prefix_calls += 1
        return super().stable_prefix(spec)  # type: ignore[arg-type]


def _setup_worktree(tmp_path: Path) -> Path:
    """A worktree with the writable ``src`` subtree present (write_text won't create parents)."""
    (tmp_path / "src").mkdir()
    return tmp_path


def _make_loop(
    worktree: Path,
    backend: object,
    spawn: object,
    *,
    prompt_builder: PromptBuilder | None = None,
    model: str = "mlx-community/test-coder",
) -> tuple[Loop, ModelClient]:
    """Assemble a Loop over real collaborators, the two seams doubled; return it and the client."""
    client = ModelClient(backend)  # type: ignore[arg-type]
    prompt = prompt_builder if prompt_builder is not None else PromptBuilder(RULES_CARD)
    runner = TestRunner(spawn=spawn)  # type: ignore[arg-type]
    snapshots = SnapshotStore(worktree, "src")
    return Loop(client, prompt, runner, snapshots, model), client


def _widget(worktree: Path) -> str:
    """The current on-disk impl file text — what the loop left after restore_best."""
    return (worktree / "src" / "widget.py").read_text(encoding="utf-8")


# --- Terminal precedence: the pure classifier, pinned exhaustively (the spine's core) ----


@pytest.mark.parametrize(
    ("best_score", "derailed", "blocked", "faulted", "expected"),
    [
        # Best is green -> DONE, regardless of any later flag (an earlier green wins outright).
        (build_test_score(passed=3, collected=3, expected=3), False, False, False, Status.DONE),
        (build_test_score(passed=3, collected=3, expected=3), True, False, False, Status.DONE),
        (build_test_score(passed=3, collected=3, expected=3), False, True, False, Status.DONE),
        (build_test_score(passed=3, collected=3, expected=3), False, False, True, Status.DONE),
        # Not green: an upstream server fault outranks every model-side cause beneath it.
        (
            build_test_score(passed=2, failed=1, collected=3, expected=3),
            False,
            False,
            True,
            Status.FAULTED,
        ),
        (None, True, False, True, Status.FAULTED),
        (None, False, True, True, Status.FAULTED),
        # Not green, no fault: a derail outranks a block and plain exhaustion.
        (
            build_test_score(passed=2, failed=1, collected=3, expected=3),
            True,
            False,
            False,
            Status.DERAILED,
        ),
        (None, True, False, False, Status.DERAILED),
        (
            build_test_score(passed=2, failed=1, collected=3, expected=3),
            True,
            True,
            False,
            Status.DERAILED,
        ),
        # Not green, no fault, no derail: a structural block outranks plain exhaustion.
        (
            build_test_score(passed=2, failed=1, collected=3, expected=3),
            False,
            True,
            False,
            Status.BLOCKED,
        ),
        (None, False, True, False, Status.BLOCKED),
        # Not green, nothing set: the loop simply ran out of attempts.
        (
            build_test_score(passed=2, failed=1, collected=3, expected=3),
            False,
            False,
            False,
            Status.EXHAUSTED,
        ),
    ],
)
def test_classify_terminal_precedence(
    best_score: object, derailed: bool, blocked: bool, faulted: bool, expected: Status
) -> None:
    # Expected is hand-derived from the rule "DONE(best green) > FAULTED > DERAILED > BLOCKED >
    # EXHAUSTED", never from running _classify_terminal — so a reordered branch is caught.
    assert (
        _classify_terminal(best_score, derailed, blocked, faulted) is expected  # type: ignore[arg-type]
    )


# --- Integration: each terminal path wired end-to-end through run() ----------------


def test_red_then_green_reaches_done_in_two_attempts(tmp_path: Path) -> None:
    worktree = _setup_worktree(tmp_path)
    backend = ReplayBackend([_edit_script(_V0), _edit_script(_V1)])
    spawn = ScriptedSpawn(_junit("one_failure.xml"), _junit("all_pass.xml"))
    loop, client = _make_loop(worktree, backend, spawn)
    spec = build_task_spec(
        impl_path="src/widget.py", expected_tests=3, test_text=_ORACLE_TEXT, budget=build_budget()
    )

    result = loop.run(spec, worktree)

    # Oracle: attempt 0 scores 2/3 (one_failure), attempt 1 scores 3/3 (all_pass) and stops.
    assert result.status is Status.DONE
    assert result.best_score is not None and result.best_score.is_green
    assert client.total_calls == 2  # exactly two logical generations, no wasted third
    assert result.record.status is Status.DONE
    assert result.record.attempts == 2
    assert _widget(worktree) == "# widget v1\nVALUE = 1\n"  # the green attempt's whole-file body


def test_all_partial_reaches_exhausted(tmp_path: Path) -> None:
    worktree = _setup_worktree(tmp_path)
    backend = ReplayBackend([_edit_script(_V0), _edit_script(_V1), _edit_script(_V2)])
    spawn = ScriptedSpawn(*([_junit("one_failure.xml")] * 3))
    loop, _ = _make_loop(worktree, backend, spawn)
    spec = build_task_spec(
        impl_path="src/widget.py",
        expected_tests=3,
        test_text=_ORACLE_TEXT,
        budget=build_budget(max_attempts=3),
    )

    result = loop.run(spec, worktree)

    # Oracle: three valid 2/3 attempts, never green, never derailed/blocked -> ran out of attempts.
    assert result.status is Status.EXHAUSTED
    assert result.record.attempts == 3
    assert result.best_score is not None
    assert result.best_score.passed == 2 and not result.best_score.is_green


def test_first_attempt_derail_reaches_derailed(tmp_path: Path) -> None:
    worktree = _setup_worktree(tmp_path)
    # A 200-char delta under a 2-token (8-char) cap trips the derail guard on the first attempt.
    backend = ReplayBackend([_sse_script("x" * 200)])
    loop, client = _make_loop(worktree, backend, ScriptedSpawn())  # spawn never called
    spec = build_task_spec(
        impl_path="src/widget.py",
        expected_tests=3,
        test_text=_ORACLE_TEXT,
        budget=build_budget(max_tokens=2),
    )

    result = loop.run(spec, worktree)

    assert result.status is Status.DERAILED
    assert result.best_score is None  # nothing was ever scored
    assert client.total_calls == 1  # stopped on the derail, did not exhaust the attempt budget
    assert result.record.status is Status.DERAILED
    # The aborted call still cost decode time — its tokens are counted, never dropped.
    assert result.record.tokens_estimated is True
    assert result.record.total_completion_tokens > 0


def test_zero_block_output_reaches_blocked(tmp_path: Path) -> None:
    worktree = _setup_worktree(tmp_path)
    backend = ReplayBackend([_sse_script("Here is my explanation, but no code fence at all.")])
    loop, client = _make_loop(worktree, backend, ScriptedSpawn())
    spec = build_task_spec(
        impl_path="src/widget.py", expected_tests=3, test_text=_ORACLE_TEXT, budget=build_budget()
    )

    result = loop.run(spec, worktree)

    # Prose with no fenced region yields zero blocks -> structural BLOCKED, nothing scored.
    assert result.status is Status.BLOCKED
    assert result.best_score is None
    assert client.total_calls == 1
    assert result.record.status is Status.BLOCKED


def test_forbidden_target_reaches_blocked(tmp_path: Path) -> None:
    worktree = _setup_worktree(tmp_path)
    # The model names a path other than the permitted impl -> apply_files refuses the edit.
    backend = ReplayBackend([_forbidden_edit_script("src/other.py", _V1)])
    loop, _ = _make_loop(worktree, backend, ScriptedSpawn())
    spec = build_task_spec(
        impl_path="src/widget.py", expected_tests=3, test_text=_ORACLE_TEXT, budget=build_budget()
    )

    result = loop.run(spec, worktree)

    assert result.status is Status.BLOCKED
    assert result.best_score is None
    assert not (worktree / "src" / "other.py").exists()  # containment held — nothing written


def test_server_fault_reaches_faulted(tmp_path: Path) -> None:
    worktree = _setup_worktree(tmp_path)
    # The server streams an upstream error frame instead of a completion — a fault, not a model
    # failure. The loop must stop and classify FAULTED, surfacing the wire message.
    backend = ReplayBackend([_error_script("context length exceeded")])
    loop, client = _make_loop(worktree, backend, ScriptedSpawn())  # spawn never called
    spec = build_task_spec(
        impl_path="src/widget.py", expected_tests=3, test_text=_ORACLE_TEXT, budget=build_budget()
    )

    result = loop.run(spec, worktree)

    # Oracle: an error frame before any edit -> FAULTED, carrying the wire message; nothing was
    # scored, and the loop stops on the fault rather than burning the whole attempt budget.
    assert result.status is Status.FAULTED
    assert result.fault == "context length exceeded"
    assert result.best_score is None
    assert client.total_calls == 1  # stopped on the fault, did not exhaust the budget
    assert result.record.status is Status.FAULTED


def test_regression_restores_the_best_snapshot(tmp_path: Path) -> None:
    worktree = _setup_worktree(tmp_path)
    # Attempt 0 scores 2/3; attempt 1 regresses to a collection crash. Best remains attempt 0.
    backend = ReplayBackend([_edit_script(_V0), _edit_script(_V1)])
    spawn = ScriptedSpawn(_junit("one_failure.xml"), _junit("import_error.xml"))
    loop, _ = _make_loop(worktree, backend, spawn)
    spec = build_task_spec(
        impl_path="src/widget.py",
        expected_tests=3,
        test_text=_ORACLE_TEXT,
        budget=build_budget(max_attempts=2),
    )

    result = loop.run(spec, worktree)

    assert result.status is Status.EXHAUSTED
    assert result.best_score is not None and result.best_score.passed == 2
    # D-SNAPSHOT-001: the loop must not end on the regression — the 2/3 body is restored, not v1.
    assert _widget(worktree) == "# widget v0\nVALUE = 0\n"


def test_writes_the_frozen_oracle_test_before_running(tmp_path: Path) -> None:
    worktree = _setup_worktree(tmp_path)
    backend = ReplayBackend([_edit_script(_V1)])
    loop, _ = _make_loop(worktree, backend, ScriptedSpawn(_junit("all_pass.xml")))
    spec = build_task_spec(
        impl_path="src/widget.py", expected_tests=3, test_text=_ORACLE_TEXT, budget=build_budget()
    )

    loop.run(spec, worktree)

    # The loop owns writing the immutable oracle, verbatim, to its loop-owned path outside src.
    oracle = worktree / _ORACLE_TEST_FILENAME
    assert oracle.read_text(encoding="utf-8") == _ORACLE_TEXT
    assert not oracle.is_relative_to(worktree / "src")  # never inside the snapshot subtree


def test_prefix_is_byte_identical_across_attempts_and_feedback_threads(tmp_path: Path) -> None:
    worktree = _setup_worktree(tmp_path)
    recording = RecordingBackend([_edit_script(_V0), _edit_script(_V1)])
    spawn = ScriptedSpawn(_junit("one_failure.xml"), _junit("all_pass.xml"))
    loop, _ = _make_loop(worktree, recording, spawn)
    spec = build_task_spec(
        impl_path="src/widget.py", expected_tests=3, test_text=_ORACLE_TEXT, budget=build_budget()
    )

    loop.run(spec, worktree)

    prefixes = [prefix for prefix, _ in recording.calls]
    tails = [tail for _, tail in recording.calls]
    assert len(recording.calls) == 2
    assert prefixes[0] == prefixes[1]  # the KV-cacheable prefix never mutates between attempts
    assert tails[0] == ""  # attempt 0 has no feedback to distil
    assert "2/3 passed" in tails[1]  # attempt 1 receives the distilled failure of attempt 0


def test_prefix_is_built_once_per_task(tmp_path: Path) -> None:
    worktree = _setup_worktree(tmp_path)
    counting = CountingPromptBuilder(RULES_CARD)
    backend = ReplayBackend([_edit_script(_V0), _edit_script(_V1)])
    spawn = ScriptedSpawn(_junit("one_failure.xml"), _junit("all_pass.xml"))
    loop, _ = _make_loop(worktree, backend, spawn, prompt_builder=counting)
    spec = build_task_spec(
        impl_path="src/widget.py", expected_tests=3, test_text=_ORACLE_TEXT, budget=build_budget()
    )

    loop.run(spec, worktree)

    # Two attempts, one prefix build: the prefix is assembled once and reused (D-PROMPT-001).
    assert counting.prefix_calls == 1


def test_broken_oracle_propagates_and_is_not_masked(tmp_path: Path) -> None:
    worktree = _setup_worktree(tmp_path)
    backend = ReplayBackend([_edit_script(_V1)])
    loop, _ = _make_loop(worktree, backend, _silent_spawn)  # produces no JUnit report
    spec = build_task_spec(
        impl_path="src/widget.py", expected_tests=3, test_text=_ORACLE_TEXT, budget=build_budget()
    )

    # A broken oracle must fail loud, never be swallowed into a BLOCKED/EXHAUSTED status.
    with pytest.raises(OracleError):
        loop.run(spec, worktree)


def test_backend_unavailable_propagates_and_is_not_masked(tmp_path: Path) -> None:
    worktree = _setup_worktree(tmp_path)
    loop, _ = _make_loop(worktree, UnavailableBackend(), ScriptedSpawn())  # spawn never called
    spec = build_task_spec(
        impl_path="src/widget.py", expected_tests=3, test_text=_ORACLE_TEXT, budget=build_budget()
    )

    # An unreachable server is a harness fault, not a task outcome: BackendUnavailable must
    # propagate, never be folded into a FAULTED/BLOCKED/EXHAUSTED status (D-BACKEND-003) — the same
    # fail-loud contract as a broken oracle (OracleError) above. FAULTED (D-FAULT-001) is for a
    # *reachable* server's error frame; a server that never answered is a missing prerequisite.
    with pytest.raises(BackendUnavailable):
        loop.run(spec, worktree)
