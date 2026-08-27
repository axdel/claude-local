"""The loop orchestrator — the red→green spine that drives a local model to a passing oracle.

This is the top of the engine: it owns the control flow the README calls the loop. Given a task
spec and a worktree, it builds the KV-cacheable prefix ONCE, then loops under the budget —
generate, apply the whole-file reply to the one permitted impl path, score the immutable oracle
test, snapshot the attempt — feeding each failure back as distilled feedback until the oracle is
green or the budget is spent. On exit it restores the best-scoring snapshot and classifies a
terminal ``Status`` by strict precedence, then aggregates the local economy record for the run.

The spine composes the single-responsibility modules beneath it (client, prompt, edits, runner,
snapshot, telemetry) and adds no new I/O of its own beyond writing the oracle test and reading
back the collaborators' results — orchestration only, per the Boundary Map (``loop`` is the root).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from claude_local.edits import apply_files, extract_files
from claude_local.paths import KeepOnlyViolation
from claude_local.telemetry import LocalEconomyRecord
from claude_local.types import Status

if TYPE_CHECKING:
    from pathlib import Path

    from claude_local.client import GenerationResult, ModelClient
    from claude_local.prompt import PromptBuilder
    from claude_local.runner import TestRunner, TestScore
    from claude_local.snapshot import SnapshotStore
    from claude_local.types import TaskSpec

# The immutable oracle test is written to the worktree ROOT — outside the SnapshotStore's src
# subtree (so restore_best never clobbers it) and distinct from any impl path (so apply_files
# never overwrites it). A run-stable name, carrying no timestamp, keeps the worktree predictable.
_ORACLE_TEST_FILENAME = "test_loop_oracle.py"


@dataclass(frozen=True, slots=True)
class LoopResult:
    """One task's loop outcome: the terminal status, the best score seen, and the economy record.

    Frozen — a run reports its result once. ``best_score`` is ``None`` when no attempt was ever
    scored (the run derailed or was structurally blocked before any test ran). ``fault`` carries
    the upstream error message when the run terminated ``FAULTED`` (a server-side SSE error frame),
    else ``None``.
    """

    status: Status
    best_score: TestScore | None
    record: LocalEconomyRecord
    fault: str | None = None


def _classify_terminal(
    best_score: TestScore | None, derailed: bool, blocked: bool, faulted: bool
) -> Status:
    """Classify the loop's terminal status by strict precedence.

    A restored-best green snapshot wins outright — an earlier green is success even if a later
    attempt derailed. Otherwise the termination CAUSE ranks, strongest first: a server FAULT (an
    upstream SSE error frame — the host failed, not the model) outranks a derail (the
    bounded-decode kill), which outranks a structural block (no usable edit for the permitted
    path), which outranks plain budget exhaustion (attempts spent with a partial best).
    """
    if best_score is not None and best_score.is_green:
        return Status.DONE
    if faulted:
        return Status.FAULTED
    if derailed:
        return Status.DERAILED
    if blocked:
        return Status.BLOCKED
    return Status.EXHAUSTED


class Loop:
    """The orchestrator spine — composes the engine's modules into one bounded red→green run.

    Constructed once per task with its collaborators and the resident model id (a run-scoped
    constant: one warm client, one resident model). ``run`` executes the whole cycle and returns
    a ``LoopResult``; the loop builds the economy record but does not persist it — writing to the
    project's economy directory is the external adapter's job (D-TELEMETRY-001).
    """

    def __init__(
        self,
        client: ModelClient,
        prompt_builder: PromptBuilder,
        runner: TestRunner,
        snapshots: SnapshotStore,
        model: str,
    ) -> None:
        self._client = client
        self._prompt = prompt_builder
        self._runner = runner
        self._snapshots = snapshots
        self._model = model

    def run(self, spec: TaskSpec, worktree: Path) -> LoopResult:
        """Drive the red→green loop for one task; return status, best score, and economy record.

        Builds the KV-cacheable prefix once and writes the immutable oracle test once, then loops
        under the budget: generate → apply the whole-file edit to the permitted path → score →
        snapshot, threading each failure back as distilled feedback and stopping on green. A derail
        or a non-usable edit (no block, or an edit for a forbidden path) stops the loop early. On
        exit the best snapshot is restored and the terminal status classified by strict precedence.

        A transport failure (``BackendUnavailable`` from the client — an unreachable server) and a
        broken oracle (``OracleError`` from the runner) are never caught — they propagate, so a
        harness fault fails loud rather than masquerading as a failing implementation.
        """
        stable = self._prompt.stable_prefix(spec)  # built ONCE — the prefill-cache invariant
        oracle_path = worktree / _ORACLE_TEST_FILENAME
        oracle_path.write_text(spec.test_text, encoding="utf-8")

        results: list[GenerationResult] = []
        last_score: TestScore | None = None
        last_raw = ""
        derailed = False
        blocked = False
        faulted = False
        fault_message: str | None = None
        attempts = 0

        for index in range(spec.budget.max_attempts):
            attempts += 1
            tail = (
                "" if last_score is None else self._prompt.distill_feedback(last_score, last_raw)
            )
            gen = self._client.generate(stable, tail, spec.budget)
            results.append(gen)
            last_raw = gen.text
            # an upstream server fault (an SSE error frame) — the host failed, not the model
            if gen.fault is not None:
                faulted = True
                fault_message = gen.fault
                break
            if gen.derail_reason is not None:
                derailed = True
                break
            blocks = extract_files(gen.text)
            if not blocks:  # prose with no usable whole-file block — structurally blocked
                blocked = True
                break
            try:
                apply_files(blocks, worktree, spec.impl_path)
            except KeepOnlyViolation:  # an edit aimed outside the one permitted path
                blocked = True
                break
            score = self._runner.run(oracle_path, worktree, spec.expected_tests)
            last_score = score
            self._snapshots.record(index, score)
            if score.is_green:
                break

        self._snapshots.restore_best()
        best = self._snapshots.best()
        best_score = best.score if best is not None else None
        status = _classify_terminal(best_score, derailed, blocked, faulted)
        record = LocalEconomyRecord.from_run(
            model=self._model,
            results=results,
            total_calls=self._client.total_calls,
            attempts=attempts,
            status=status,
        )
        return LoopResult(status=status, best_score=best_score, record=record, fault=fault_message)
