"""The loop orchestrator — the red→green spine that drives a local model to a passing oracle.

This is the top of the engine: it owns the control flow the README calls the loop. Given a task
spec and a worktree, it builds the KV-cacheable prefix ONCE, then loops under the budget —
generate, apply the whole-file reply to the one permitted impl path, score the immutable oracle
test, snapshot the attempt — feeding the runner-owned pytest diagnostics (never prior model
source) back as distilled feedback until the oracle is green or the budget is spent. On exit it
restores the best-scoring snapshot and classifies a
terminal ``Status`` by strict precedence, then aggregates the local economy record for the run.

The spine composes the single-responsibility modules beneath it (client, prompt, edits, runner,
snapshot, telemetry) and adds no new I/O of its own beyond writing the oracle test and reading
back the collaborators' results — orchestration only, per the Boundary Map (``loop`` is the root).

An optional ``on_attempt`` observer receives one ``AttemptProgress`` per attempt as it resolves,
so a caller can render a run while it happens. The loop reports; it never prints — keeping the
rendering outside preserves the no-I/O rule above (D-PROGRESS-001).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from claude_local.edits import apply_file, extract_file
from claude_local.paths import KeepOnlyViolation
from claude_local.telemetry import LocalEconomyRecord
from claude_local.types import Status

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from claude_local.client import GenerationResult, ModelClient
    from claude_local.prompt import PromptBuilder
    from claude_local.runner import OracleRun, TestRunner, TestScore
    from claude_local.snapshot import SnapshotStore
    from claude_local.types import TaskSpec

# The immutable oracle test is written to the worktree ROOT — outside the SnapshotStore's src
# subtree (so restore_best never clobbers it) and distinct from any impl path (so apply_file never
# overwrites it). A run-stable name, carrying no timestamp, keeps the worktree predictable.
ORACLE_TEST_FILENAME = "test_loop_oracle.py"


@dataclass(frozen=True, slots=True)
class AttemptProgress:
    """One resolved attempt, reported the moment it resolves — the loop's live progress event.

    Frozen, and composed rather than copied: ``generation`` and ``score`` are the value objects
    that already own the model's cost and the oracle's verdict, so a consumer reads tokens,
    seconds, the derail reason, the fault, and the test counts through their single owners and
    nothing here can drift from them. ``attempt`` is 1-based, matching what a reader counts.

    ``score`` is ``None`` whenever the attempt never reached the oracle. Three different things
    cause that, and a watcher must tell them apart: the server faulted, the guard cut a derail, or
    the model returned nothing usable to write — the last being what ``blocked`` names.
    """

    attempt: int
    generation: GenerationResult
    score: TestScore | None

    @property
    def blocked(self) -> bool:
        """The attempt produced no verdict for a structural reason — no usable edit to score.

        Derived as the residue after the two named causes are ruled out, so the classification
        cannot disagree with ``generation.fault`` or ``generation.derail_reason``.
        """
        return (
            self.score is None
            and self.generation.fault is None
            and self.generation.derail_reason is None
        )


@dataclass(frozen=True, slots=True)
class LoopResult:
    """One task's loop outcome: the terminal status, the best score seen, and the economy record.

    Frozen — a run reports its result once. ``best_score`` is ``None`` when no model edit crossed
    oracle scoring, and ``has_scored_edit`` derives model production from that snapshot-owned
    fact. ``fault`` carries
    the upstream error message when the run terminated ``FAULTED`` (a server-side SSE error frame),
    else ``None``.
    """

    status: Status
    best_score: TestScore | None
    record: LocalEconomyRecord
    fault: str | None = None

    @property
    def has_scored_edit(self) -> bool:
        """Whether an applied model edit produced the restored scored snapshot."""
        return self.best_score is not None


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

    ``on_attempt`` is the optional live-progress observer, invoked once per attempt the moment it
    resolves. It is what makes a long run watchable rather than merely awaited; the loop reports
    facts and never renders them, so this module stays free of I/O (D-PROGRESS-001).
    """

    def __init__(
        self,
        client: ModelClient,
        prompt_builder: PromptBuilder,
        runner: TestRunner,
        snapshots: SnapshotStore,
        model: str,
        on_attempt: Callable[[AttemptProgress], None] | None = None,
    ) -> None:
        self._client = client
        self._prompt = prompt_builder
        self._runner = runner
        self._snapshots = snapshots
        self._model = model
        self._on_attempt = on_attempt

    def run(self, spec: TaskSpec, worktree: Path) -> LoopResult:
        """Drive the red→green loop for one task; return status, best score, and economy record.

        Builds the KV-cacheable prefix once and writes the immutable oracle test once, then loops
        under the budget: generate → apply the whole-file edit to the permitted path → score →
        snapshot, threading each failure back as distilled feedback. The loop stops on a green
        oracle, and stops early on any attempt that reached no oracle at all — a server fault, a
        derail, or a reply with no usable edit — because there is nothing to repair from. On exit
        the best snapshot is restored and the status follows precedence.

        A transport failure (``BackendUnavailable`` from the client — an unreachable server) and a
        broken oracle (``OracleError`` from the runner) are never caught — they propagate, so a
        harness fault fails loud rather than masquerading as a failing implementation.
        """
        stable = self._prompt.stable_prefix(spec)  # built ONCE — the prefill-cache invariant
        oracle_path = worktree / ORACLE_TEST_FILENAME
        oracle_path.write_text(spec.test_text, encoding="utf-8")

        results: list[GenerationResult] = []
        last_run: OracleRun | None = None

        for index in range(spec.budget.max_attempts):
            tail = (
                ""
                if last_run is None
                else self._prompt.distill_feedback(last_run.score, last_run.output)
            )
            generation = self._client.generate(stable, tail, spec.budget)
            results.append(generation)
            last_run = self._score_attempt(generation, index, spec, worktree, oracle_path)
            if self._on_attempt is not None:
                self._on_attempt(
                    AttemptProgress(
                        attempt=len(results),
                        generation=generation,
                        score=None if last_run is None else last_run.score,
                    )
                )
            if last_run is None or last_run.score.is_green:
                break

        self._snapshots.restore_best()
        best = self._snapshots.best()
        best_score = best.score if best is not None else None
        # The terminal cause is a property of the LAST attempt, so it is read off that attempt
        # rather than tracked in flags that a second writer could disagree with. A block is the
        # residue: no verdict, and neither the server nor the guard stopped it.
        final = results[-1] if results else None
        derailed = final is not None and final.derail_reason is not None
        faulted = final is not None and final.fault is not None
        blocked = final is not None and last_run is None and not derailed and not faulted
        status = _classify_terminal(best_score, derailed, blocked, faulted)
        record = LocalEconomyRecord.from_run(
            model=self._model,
            results=results,
            total_calls=self._client.total_calls,
            attempts=len(results),
            status=status,
        )
        return LoopResult(
            status=status,
            best_score=best_score,
            record=record,
            fault=final.fault if final is not None else None,
        )

    def _score_attempt(
        self,
        generation: GenerationResult,
        index: int,
        spec: TaskSpec,
        worktree: Path,
        oracle_path: Path,
    ) -> OracleRun | None:
        """Apply one generation and score it; ``None`` when it never reached the oracle.

        Every way an attempt can yield nothing to score collapses to ``None`` here: an upstream
        server fault (the host failed, not the model), a derail the guard cut, a reply carrying no
        usable whole-file frame, and an edit aimed outside the one permitted path. Folding them
        into one return is what lets the caller report and classify every attempt at a single
        site instead of at five scattered breaks.
        """
        if generation.fault is not None or generation.derail_reason is not None:
            return None
        reply = extract_file(generation.text)
        if reply is None:  # prose with no usable whole-file reply
            return None
        try:
            apply_file(reply, worktree, spec.impl_path)
        except KeepOnlyViolation:  # an edit aimed outside the one permitted path
            return None
        run = self._runner.run(oracle_path, worktree, spec.expected_tests)
        self._snapshots.record(index, run.score)
        return run
