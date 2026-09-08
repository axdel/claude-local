"""The loop orchestrator — the red→green spine that drives a local model to a passing oracle.

This is the top of the engine: it owns the control flow the README calls the loop. Given a task
spec and a worktree, it builds the KV-cacheable prefix ONCE, then loops under the budget —
generate, apply the whole-file reply to the one permitted impl path, score the immutable oracle
test, snapshot the attempt — feeding the runner-owned pytest diagnostics back with the file that
produced them until the oracle is green or the budget is spent. On exit it
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

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from claude_local.edits import apply_file, extract_file
from claude_local.paths import KeepOnlyViolation
from claude_local.telemetry import LocalEconomyRecord
from claude_local.types import Status

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from claude_local.client import GenerationResult, ModelClient
    from claude_local.derail import DerailReason
    from claude_local.prompt import PromptBuilder
    from claude_local.runner import OracleRun, TestRunner, TestScore
    from claude_local.snapshot import SnapshotStore
    from claude_local.types import Budget, TaskSpec

# The immutable oracle test is written to the worktree ROOT — outside the SnapshotStore's src
# subtree (so restore_best never clobbers it) and distinct from any impl path (so apply_file never
# overwrites it). A run-stable name, carrying no timestamp, keeps the worktree predictable.
ORACLE_TEST_FILENAME = "test_loop_oracle.py"

_PLAN_MAX_TOKENS = 512
"""Decode ceiling for the plan step — roughly a screen of outline, generous for an approach.

Bounded separately from the implementation because the two decodes want opposite things. A file
needs room; a plan does not, and a long one is actively harmful: it is frozen into the prefix,
so it is re-read on every attempt and never scored by anything.
"""

_PLATEAU_ATTEMPTS = 2
"""Consecutive attempts that fail to clear the best score before the loop calls it a plateau.

Two, not one: a single miss between two clearing attempts is a model still searching, and a
detector that escalated on it would interrupt one that was about to succeed. Two consecutive is
the point where "different words, no further" stops being a sample and starts being the shape —
measured on a benchmark case whose four attempts wrote 558, 711, 649, and 589 tokens of entirely
different implementation and scored 3 of 13 every time.
"""


@dataclass(slots=True)
class _Plateau:
    """Watches a run for the quieter shape of standing still: new words that get no further.

    Mutable and loop-owned, holding the two facts the judgment needs — the best score any attempt
    has reached, and how many attempts since have failed to beat it. Both are hidden: a caller
    folds in each verdict and asks one question, so the watermark can never be read as a score in
    its own right or written by a second party.

    An attempt that reached no oracle carries no verdict about progress — the model may well have
    been improving — so it neither breaks the streak nor extends it.
    """

    _best_passed: int = -1
    _misses: int = 0

    def record(self, score: TestScore | None) -> None:
        """Fold one attempt's verdict in, extending the miss streak unless it cleared the best."""
        if score is None:
            return
        self._misses = 0 if score.passed > self._best_passed else self._misses + 1
        self._best_passed = max(self._best_passed, score.passed)

    @property
    def reached(self) -> bool:
        """Whether enough consecutive attempts have failed to clear the best score to call it."""
        return self._misses >= _PLATEAU_ATTEMPTS


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

    ``repeats_previous`` marks a generation that came back byte-identical to the one before it,
    ``plateaued`` marks one whose score failed to clear the best so far for a second consecutive
    attempt (D-LOOP-007), and ``nudged`` marks one generated under an escalation because an earlier
    attempt did (D-LOOP-005). All three are carried here rather than inferred, because only the
    loop holds the
    previous attempts, and each answers a question the others cannot: the first two are the two
    shapes of "the model made no progress" — identical words, and different words that get no
    further — while the third says why the loop kept paying anyway. A watcher given only some of
    them sees either a loop re-buying a known answer or an escalation with no trigger.

    ``blocked_reason`` names WHICH structural cause fired, and is set exactly when ``blocked`` is
    true — the two are computed from the same branch, so they cannot disagree. Two very different
    problems reach ``blocked``: a reply carrying no whole-file frame at all (a formatting failure),
    and one framed at a path outside the single writable one (a targeting failure). The second
    already knows the path it aimed at when the write is refused; discarding that left an operator
    watching two indistinguishable "the model returned nothing usable" lines for problems whose
    fixes have nothing in common.
    """

    attempt: int
    generation: GenerationResult
    score: TestScore | None
    repeats_previous: bool = False
    plateaued: bool = False
    nudged: bool = False
    blocked_reason: str | None = None

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
class _ScoredAttempt:
    """One attempt that reached the oracle: the file it wrote, and the verdict on that file.

    The pair travels together because the next prompt needs both — the failure says what broke and
    the source says what broke it, and a brief carrying one without the other asks the model to
    repair code it cannot see. ``None`` in place of this whole object is how the loop reads "that
    attempt never reached the oracle", which is what keeps a stale file from ever being reported
    to the model as its latest work.
    """

    source: str
    run: OracleRun


@dataclass(frozen=True, slots=True)
class _AttemptResult:
    """What one generation yielded: a scored attempt, or the reason it never reached the oracle.

    Both arms in one value because the caller needs them at the same moment — it reports the
    attempt and decides the next tail from the same fact, and splitting them would put a second
    writer on one classification. ``blocked_reason`` is set only for the causes that are the
    attempt's own shape; a server fault and a derail are already named on the generation.
    """

    scored: _ScoredAttempt | None
    blocked_reason: str | None = None


@dataclass(frozen=True, slots=True)
class LoopResult:
    """One task's loop outcome: the terminal status, the best score seen, and the economy record.

    Frozen — a run reports its result once. ``best_score`` is ``None`` when no model edit crossed
    oracle scoring, and ``has_scored_edit`` derives model production from that snapshot-owned
    fact. ``fault`` carries
    the upstream error message when the run terminated ``FAULTED`` (a server-side SSE error frame),
    else ``None``.

    ``derail_reason`` is the symmetric field for ``DERAILED``: WHICH bound cut the last generation,
    read off its single owner on the generation itself. The status alone says a bound fired, and
    the four bounds want four different responses — raise the token cap, raise the timeout, fix a
    prompt the model keeps repeating, chase a server that streams bytes and no content — so a
    result that named only the class of failure left every one of them to guesswork.
    """

    status: Status
    best_score: TestScore | None
    record: LocalEconomyRecord
    fault: str | None = None
    derail_reason: DerailReason | None = None

    @property
    def has_scored_edit(self) -> bool:
        """Whether an applied model edit produced the restored scored snapshot."""
        return self.best_score is not None


def _classify_terminal(
    best_score: TestScore | None, *, faulted: bool, derailed: bool, blocked: bool
) -> Status:
    """Classify the loop's terminal status by strict precedence.

    A restored-best green snapshot wins outright — an earlier green is success even if a later
    attempt derailed. Otherwise the termination CAUSE ranks, strongest first: a server FAULT (an
    upstream SSE error frame — the host failed, not the model) outranks a derail (the
    bounded-decode kill), which outranks a structural block (no usable edit for the permitted
    path), which outranks plain budget exhaustion (attempts spent with a partial best).

    The three flags are keyword-only, and declared in that same precedence order. Positionally
    they were three ``bool`` parameters in a DIFFERENT order from the one this docstring states,
    so a caller who typed them as written here silently swapped fault and derail — a call the
    type checker cannot fault, since the arguments differ only in meaning.
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


def _terminal_status(
    final: GenerationResult | None, scored: bool, best_score: TestScore | None
) -> Status:
    """Name the cause that ended the run, then rank it — the pair that decides a terminal status.

    This half asks WHAT happened; ``_classify_terminal`` asks WHICH cause wins. The cause is read
    off the LAST attempt rather than accumulated in loop flags, so no second writer can disagree
    with the generation that actually ended the run. ``scored`` says that attempt reached the
    oracle; a block is the residue — no verdict, and neither the server nor the guard stopped it.
    """
    derailed = final is not None and final.derail_reason is not None
    faulted = final is not None and final.fault is not None
    blocked = final is not None and not scored and not derailed and not faulted
    return _classify_terminal(best_score, faulted=faulted, derailed=derailed, blocked=blocked)


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
        oracle, and stops early wherever there is nothing left to repair from: on any attempt that
        reached no oracle at all (a server fault, a derail, or a reply with no usable edit), and
        once the nudge ladder is spent on a model that has stopped making progress (D-LOOP-004).

        Progress has two failure shapes and both feed that one ladder. A generation that comes
        back byte-identical is a *verbatim-repeat* of an answer already scored, not a repair. A
        *plateau* is
        the same failure in different words — consecutive attempts that never clear the best score
        — and it is the commoner one, so a loop watching only for identical text spends its whole
        budget re-deriving one wrong answer. On exit the best-passing snapshot is restored
        and the status follows precedence.

        A transport failure (``BackendUnavailable`` from the client — an unreachable server) and a
        broken oracle (``OracleError`` from the runner) are never caught — they propagate, so a
        harness fault fails loud rather than masquerading as a failing implementation.
        """
        planning, plan = self._plan(spec)
        stable = self._prompt.stable_prefix(spec, plan)  # built ONCE — the prefill-cache invariant
        oracle_path = worktree / ORACLE_TEST_FILENAME
        oracle_path.write_text(spec.test_text, encoding="utf-8")

        results: list[GenerationResult] = []
        last_attempt: _ScoredAttempt | None = None
        nudge = ""
        reframe = ""
        stalls = 0
        plateau = _Plateau()

        for index in range(spec.budget.max_attempts):
            tail, nudged = self._next_tail(last_attempt, nudge, reframe)
            generation = self._client.generate(stable, tail, spec.budget)
            repeats_previous = bool(results) and generation.text == results[-1].text
            results.append(generation)
            attempted = self._score_attempt(generation, index, spec, worktree, oracle_path)
            last_attempt = attempted.scored
            score = None if last_attempt is None else last_attempt.run.score
            plateau.record(score)
            if self._on_attempt is not None:
                self._on_attempt(
                    AttemptProgress(
                        attempt=len(results),
                        generation=generation,
                        score=score,
                        repeats_previous=repeats_previous,
                        plateaued=plateau.reached,
                        nudged=nudged,
                        blocked_reason=attempted.blocked_reason,
                    )
                )
            if last_attempt is None:
                correction = self._correction(generation, corrected=bool(reframe))
                if correction is None:
                    break
                reframe = correction
                continue
            if last_attempt.run.score.is_green:
                break
            stalled = repeats_previous or plateau.reached
            stalls += int(stalled)
            escalation = self._escalation(stalled, stalls)
            if escalation is None:
                break
            nudge = escalation

        return self._finish(
            planning, results, scored=last_attempt is not None, plan_first=spec.plan_first
        )

    def _next_tail(
        self, last_attempt: _ScoredAttempt | None, nudge: str, reframe: str
    ) -> tuple[str, bool]:
        """The next attempt's volatile tail, and whether that tail carried an escalation.

        The two travel together because the second is a fact ABOUT the first, and reading them
        apart is how they came to disagree: a correction replaces the tail outright with a reframe
        and does NOT clear the escalation (only the repair path reassigns it), so an attempt
        reframed after an earlier stall reported an escalation its prompt never carried.

        A scored last attempt earns a repair brief — the file it wrote and how it failed. One that
        reached no oracle has nothing to quote back, so it gets the reframe instead.
        """
        if last_attempt is None:
            return reframe, False
        return self._repair_brief(last_attempt, nudge), bool(nudge)

    def _finish(
        self,
        planning: list[GenerationResult],
        results: list[GenerationResult],
        *,
        scored: bool,
        plan_first: bool,
    ) -> LoopResult:
        """Restore the best attempt, classify how the run ended, and total what it burned.

        The exit phase, whole: every branch here reads the run's accumulated facts and none of them
        can advance it, so keeping them in the loop body only lent the attempt's variables a
        second, longer life. ``scored`` is whether the LAST attempt reached the oracle, which is
        what separates a structural block from plain exhaustion.
        """
        self._snapshots.restore_best()
        best = self._snapshots.best()
        best_score = best.score if best is not None else None
        final = results[-1] if results else None
        status = _terminal_status(final, scored=scored, best_score=best_score)
        record = LocalEconomyRecord.from_run(
            model=self._model,
            rules_card_digest=self._prompt.card_digest,
            # Carried from the spec, never read off `planning` — the record states how the run was
            # CONFIGURED, and a plan call that returned nothing must not make it read as a run
            # that never asked for one.
            plan_first=plan_first,
            results=[*planning, *results],  # the plan burned real decode; the loop pays for it
            total_calls=self._client.total_calls,
            attempts=len(results),  # but planning is not an attempt at the implementation
            status=status,
        )
        return LoopResult(
            status=status,
            best_score=best_score,
            record=record,
            fault=final.fault if final is not None else None,
            derail_reason=final.derail_reason if final is not None else None,
        )

    def _plan(self, spec: TaskSpec) -> tuple[list[GenerationResult], str]:
        """Spend one generation on a plan, or nothing at all when the lever is off.

        Returns the generation for the economy record and the plan text to freeze into the prefix,
        so the caller adds no branch of its own — the lever's whole cost is contained here.

        Computed once per TASK. Recomputing it per attempt would mutate the prefix and discard the
        server's prefill cache, reversing the guarantee the stable prefix exists to provide
        (D-PROMPT-001) — the same freeze-once shape context files already have.

        A plan the model failed to produce degrades to no plan rather than ending the run: the
        plan is an aid, never the oracle. It is frozen only when the generation decoded content
        resolving to non-empty assistant content, which a faulted or cap-cut one often has
        not: reasoning-channel output is metered but never appended, and an inline transcript
        cut before its final channel resolves to empty. So the budget can be spent for no plan
        at all. Either way the attempts that follow meet the same condition and terminate
        through the existing precedence, so a second termination path here would only duplicate
        it.
        """
        if not spec.plan_first:
            return [], ""
        planning = self._client.generate(
            self._prompt.stable_prefix(spec), self._prompt.plan_request(), self._plan_budget(spec)
        )
        return [planning], planning.text.strip()

    @staticmethod
    def _plan_budget(spec: TaskSpec) -> Budget:
        """The task's budget with decode capped to plan length.

        A plan given the implementation's full token cap invites an essay, and an essay is worse
        than useless here: it is frozen into the prefix, so every later attempt reads it and no
        oracle can ever contradict it. The cap bounds that blast radius by construction rather
        than by asking the model for brevity, which it is free to ignore. It only ever lowers the
        ceiling — a caller whose whole budget is already smaller keeps theirs.
        """
        return replace(spec.budget, max_tokens=min(spec.budget.max_tokens, _PLAN_MAX_TOKENS))

    def _repair_brief(self, attempt: _ScoredAttempt, nudge: str) -> str:
        """The next tail: the file the last attempt wrote, how it failed, and any escalation."""
        return self._prompt.distill_feedback(
            attempt.run.score, attempt.run.output, attempt.source, nudge
        )

    def _correction(self, generation: GenerationResult, *, corrected: bool) -> str | None:
        """The re-ask owed to an attempt that scored nothing, or ``None`` to end the run.

        Four different failures reach this point as one ``None`` score, and only two of them are
        the model's own shape: a reply carrying no frame, and one framed at a path outside the
        single writable one. The other two are the host and the guard — a server fault and a
        derail — where there is no answer the model chose, so there is nothing to quote back and a
        re-ask would buy the same failure under the same conditions. That is why this cannot key
        on "no score".

        ``corrected`` spends the correction at most once per run, which is what makes termination
        a property of the construction rather than of the model's cooperation — the same guarantee
        the nudge ladder gets from being walked once. A model told plainly what to send and
        answering the same way again has given its answer; the rest of the budget only re-buys it.
        """
        if generation.fault is not None or generation.derail_reason is not None:
            return None
        if corrected:
            return None
        return self._prompt.reframe_for(generation.text)

    def _escalation(self, stalled: bool, stalls: int) -> str | None:
        """The next attempt's nudge: empty while the model still moves, ``None`` to stop.

        A model that made progress needs no escalation — the failure speaks for itself, so an
        earlier nudge clears. One that stalled gets the next rung, and ``None`` past the last rung
        is where the run genuinely ends: every question this card knows how to ask has been asked,
        and the rest of the budget would only re-buy an answer already given.

        ``stalls`` counts every stalling attempt in the run rather than the current consecutive
        streak, so the ladder is walked at most once and termination is guaranteed by construction.
        A count that reset on progress could re-offer the first rung indefinitely.
        """
        if not stalled:
            return ""
        return self._prompt.nudge_for(stalls)

    def _score_attempt(
        self,
        generation: GenerationResult,
        index: int,
        spec: TaskSpec,
        worktree: Path,
        oracle_path: Path,
    ) -> _AttemptResult:
        """Apply one generation and score it, or say why it never reached the oracle.

        Every way an attempt can yield nothing to score arrives back through one value: an
        upstream server fault (the host failed, not the model), a derail the guard cut, a reply
        carrying no usable whole-file frame, and an edit aimed outside the one permitted path.
        Folding them into one return is what lets the caller report and classify every attempt at
        a single site instead of at five scattered breaks.

        The two STRUCTURAL causes also carry a reason out, because they are the two the caller
        cannot reconstruct: the fault and the derail are already named on the generation itself,
        while a refusal knows a path that exists nowhere else once this frame returns.
        """
        if generation.fault is not None or generation.derail_reason is not None:
            return _AttemptResult(None)
        reply = extract_file(generation.text)
        if reply is None:
            return _AttemptResult(None, "the reply carried no whole-file frame to write")
        try:
            apply_file(reply, worktree, spec.impl_path)
        except KeepOnlyViolation as refusal:
            return _AttemptResult(None, str(refusal))
        run = self._runner.run(oracle_path, worktree, spec.expected_tests)
        self._snapshots.record(index, run.score)
        # The payload validated as UTF-8 on the way in, so decoding returns the applied bytes
        # exactly — the file the oracle just scored, not what the reply meant to write.
        return _AttemptResult(_ScoredAttempt(source=reply.payload.decode("utf-8"), run=run))
