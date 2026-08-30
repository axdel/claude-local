"""Per-case benchmark driver with an owned disposable worktree lifecycle.

The driver composes a golden tree, replaces exactly the implementation hole with its blank stub,
and invokes the public ``claude_local.implement`` front door. The case worktree is removed on
success or failure after ``implement`` has copied its result into the returned ``Outcome``.
"""

from __future__ import annotations

import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from claude_local import Outcome, implement

from .case import BenchmarkCase

if TYPE_CHECKING:
    import httpx


class BenchmarkDriver:
    """Assemble and run one benchmark case while owning all scratch worktree children."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        generation_params: Mapping[str, object] | None = None,
        scratch_root: Path | None = None,
    ) -> None:
        self._base_url = base_url
        self._model = model
        self._generation_params = dict(generation_params or {})
        self._scratch_root = scratch_root

    def run_case(self, case: BenchmarkCase, *, http_client: httpx.Client | None = None) -> Outcome:
        """Run ``case`` through ``implement`` and remove its assembled worktree on exit.

        An injected ``http_client`` is reused as-is (the caller owns its lifecycle); when omitted,
        ``implement`` creates and closes a per-case client sized to the case budget.
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
                http_client=http_client,
            )


@dataclass(frozen=True, slots=True)
class CaseResult:
    """One benchmark case's identity paired with what the loop produced and burned for it.

    ``case_id`` is the caller's key for the case (its rung-directory name in a full suite run).
    ``outcome`` carries the terminal status, the produced code, and the local-half economy record,
    so a scorer reaches the record and status through the outcome rather than a duplicated copy.
    """

    case_id: str
    outcome: Outcome


def run_suite(
    cases: Mapping[str, BenchmarkCase],
    *,
    base_url: str,
    model: str,
    generation_params: Mapping[str, object] | None = None,
    scratch_root: Path | None = None,
    http_client: httpx.Client | None = None,
) -> list[CaseResult]:
    """Run every case through the driver in iteration order and return one ``CaseResult`` each.

    Each case runs in its own disposable worktree with a fresh in-memory database (the golden app
    opens ``:memory:`` per ``create_app``), torn down before the next case starts, so no case can
    observe another's files or rows. An injected ``http_client`` is shared across the whole suite —
    one warm connection to one resident model, closed by the caller; when omitted, each case owns a
    per-case client via ``implement``. Each case's economy record is captured on its
    ``CaseResult.outcome``; the net-savings verdict stays the orchestrator's, never computed here.

    Args:
        cases: The cases to run, keyed by the id each ``CaseResult`` carries; iteration order is
            preserved in the returned list.
        base_url: The OpenAI-compatible server every case infers against.
        model: The model name requested for every case.
        generation_params: Optional generation parameters applied uniformly across the suite.
        scratch_root: Parent directory for each case's disposable worktree; a managed system temp
            directory when omitted.
        http_client: An HTTP client shared across every case. When omitted, each case creates and
            closes its own; an injected client is the caller's and is never closed here.

    Returns:
        One ``CaseResult`` per case, in the order ``cases`` iterates.
    """
    driver = BenchmarkDriver(
        base_url=base_url,
        model=model,
        generation_params=generation_params,
        scratch_root=scratch_root,
    )
    return [
        CaseResult(case_id=case_id, outcome=driver.run_case(case, http_client=http_client))
        for case_id, case in cases.items()
    ]


def _write_case_file(worktree: Path, relative_path: str, content: str) -> None:
    """Write one pre-validated committed case fixture below its assembled worktree path."""
    destination = worktree / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
