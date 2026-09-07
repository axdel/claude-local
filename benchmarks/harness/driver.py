"""Per-case benchmark driver with an owned disposable worktree lifecycle.

The driver composes a golden tree, replaces exactly the implementation hole with its blank stub,
and invokes the public ``claude_local.implement`` front door. The case worktree is removed on
success or failure after ``implement`` has copied its result into the returned ``Outcome``.

A benchmark run takes tens of minutes, so ``run_cases`` accepts an optional ``BenchmarkProgress``
observer and reports each case and each attempt as they resolve. The engine below exposes two
bare callables — one per module that owns its event — while this layer takes one cohesive
observer, because only here do the case identity, its position in the ladder, and its result
exist. Adapting the four-method observer down to those two callables is this module's job.
"""

from __future__ import annotations

import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from claude_local import BackendUnavailable, OracleError, Outcome, SandboxUnavailable, implement

from .case import BenchmarkCase

if TYPE_CHECKING:
    from collections.abc import Callable

    import httpx

    from claude_local import AttemptProgress


class BenchmarkInterrupted(RuntimeError):
    """A harness fault stopped a benchmark-run partway; carries the cases that finished before it.

    A harness fault is the host failing, not a case failing, so it must stay loud — it is raised,
    never folded into a case status. But every case already finished is a measurement that cost
    real decode time, and a sweep is hours long: letting the fault propagate bare discarded all of
    them to report the one that broke. The two facts are not in tension, so both travel — the
    exception says the run did not complete, ``completed`` says what it measured before it stopped.

    The underlying fault is the ``__cause__``, chained rather than restated, so nothing here can
    disagree with the error the host actually raised. D-BACKEND-004 narrowed how often a healthy
    sweep reaches this path; this is what happens when it reaches it anyway.
    """

    def __init__(self, completed: list[CaseResult]) -> None:
        self.completed = completed
        super().__init__(f"benchmark stopped after {len(completed)} completed case(s)")


class BenchmarkProgress(Protocol):
    """What a benchmark run reports while it runs — the seam a live renderer implements.

    Four events, in the order a run produces them: a case opens, its raw model text decodes, each
    attempt resolves, and the case closes with its result. A renderer receives the owning value
    objects (``BenchmarkCase``, ``AttemptProgress``, ``CaseResult``) rather than flattened copies,
    so what it can display is bounded by what those objects actually know.
    """

    def case_started(self, case_id: str, case: BenchmarkCase, index: int, total: int) -> None:
        """A case is about to run; ``index`` is its 1-based position among ``total`` cases."""

    def delta(self, text: str) -> None:
        """One raw content delta, as the model decodes it (pre-normalisation — D-PROGRESS-002)."""

    def attempt(self, progress: AttemptProgress) -> None:
        """One attempt of the running case has resolved."""

    def case_finished(self, result: CaseResult) -> None:
        """The running case is over; ``result`` carries its terminal status and economy record."""


class BenchmarkDriver:
    """Assemble and run one benchmark case while owning all scratch worktree children.

    ``rules_card_path`` selects the rules card every case is run under, defaulting to the bundled
    one. It is a benchmark VARIABLE rather than a fixed asset: the card is the largest span of the
    prompt, so comparing two cards on one model is as legitimate an experiment as comparing two
    models on one card, and neither comparison is readable unless the card is named. Whichever
    card is chosen, its digest travels back on every economy record and onto the scorecard.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        generation_params: Mapping[str, object] | None = None,
        scratch_root: Path | None = None,
        rules_card_path: Path | None = None,
    ) -> None:
        self._base_url = base_url
        self._model = model
        self._generation_params = dict(generation_params or {})
        self._scratch_root = scratch_root
        self._rules_card_path = rules_card_path

    def run_case(
        self,
        case: BenchmarkCase,
        *,
        http_client: httpx.Client | None = None,
        on_delta: Callable[[str], None] | None = None,
        on_attempt: Callable[[AttemptProgress], None] | None = None,
    ) -> Outcome:
        """Run ``case`` through ``implement`` and remove its assembled worktree on exit.

        An injected ``http_client`` is reused as-is (the caller owns its lifecycle); when omitted,
        ``implement`` creates and closes a per-case client sized to the case budget. The two
        optional observers are forwarded to ``implement`` unchanged — this driver adds no case
        context to them, because a single case has none to add.
        """
        if self._scratch_root is not None:
            self._scratch_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="claude-local-benchmark-",
            dir=self._scratch_root,
        ) as temporary_directory:
            worktree = Path(temporary_directory)
            for golden_file in case.golden_tree:
                _write_case_file(worktree, golden_file.path, golden_file.content)
            _write_case_file(worktree, case.task.impl_path, case.blank_stub)
            return implement(
                case.task,
                base_url=self._base_url,
                model=self._model,
                generation_params=self._generation_params,
                worktree=worktree,
                rules_card_path=self._rules_card_path,
                http_client=http_client,
                on_delta=on_delta,
                on_attempt=on_attempt,
            )


@dataclass(frozen=True, slots=True)
class CaseResult:
    """One benchmark case's identity paired with what the loop produced and burned for it.

    ``case_id`` is the caller's key for the case (its case-directory name in a full benchmark run).
    ``outcome`` carries the terminal status, the produced code, and the local-half economy record,
    so a scorer reaches the record and status through the outcome rather than a duplicated copy.
    """

    case_id: str
    outcome: Outcome


def run_cases(
    cases: Mapping[str, BenchmarkCase],
    *,
    base_url: str,
    model: str,
    generation_params: Mapping[str, object] | None = None,
    scratch_root: Path | None = None,
    rules_card_path: Path | None = None,
    http_client: httpx.Client | None = None,
    progress: BenchmarkProgress | None = None,
) -> list[CaseResult]:
    """Run every case through the driver in iteration order and return one ``CaseResult`` each.

    Each case runs in its own disposable worktree with a fresh in-memory database (the golden app
    opens ``:memory:`` per ``create_app``), torn down before the next case starts, so no case can
    observe another's files or rows. An injected ``http_client`` is shared across the benchmark —
    one warm connection to one resident model, closed by the caller; when omitted, each case owns a
    per-case client via ``implement``. Each case's economy record is captured on its
    ``CaseResult.outcome``; the net-savings verdict stays the orchestrator's, never computed here.

    Args:
        cases: The cases to run, keyed by the id each ``CaseResult`` carries; iteration order is
            preserved in the returned list.
        base_url: The OpenAI-compatible server every case infers against.
        model: The model name requested for every case.
        generation_params: Optional generation parameters applied uniformly across the benchmark.
        scratch_root: Parent directory for each case's disposable worktree; a managed system temp
            directory when omitted.
        rules_card_path: The rules card every case runs under; the bundled card when omitted. One
            card per benchmark, because a scorecard mixing two is not a comparison —
            ``score_cases`` refuses such a run rather than labelling it with one of the two.
        http_client: An HTTP client shared across every case. When omitted, each case creates and
            closes its own; an injected client is the caller's and is never closed here.
        progress: Optional live observer. Each event fires as it happens, never batched at the
            end — a run that takes tens of minutes is otherwise unobservable until it is over.

    Returns:
        One ``CaseResult`` per case, in the order ``cases`` iterates.
    """
    driver = BenchmarkDriver(
        base_url=base_url,
        model=model,
        generation_params=generation_params,
        scratch_root=scratch_root,
        rules_card_path=rules_card_path,
    )
    total = len(cases)
    results: list[CaseResult] = []
    for index, (case_id, case) in enumerate(cases.items(), start=1):
        if progress is not None:
            progress.case_started(case_id, case, index, total)
        try:
            outcome = driver.run_case(
                case,
                http_client=http_client,
                on_delta=None if progress is None else progress.delta,
                on_attempt=None if progress is None else progress.attempt,
            )
        except (BackendUnavailable, SandboxUnavailable, OracleError) as fault:
            raise BenchmarkInterrupted(results) from fault
        result = CaseResult(case_id=case_id, outcome=outcome)
        results.append(result)
        if progress is not None:
            progress.case_finished(result)
    return results


def _write_case_file(worktree: Path, relative_path: str, content: str) -> None:
    """Write one pre-validated committed case fixture below its assembled worktree path."""
    destination = worktree / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
