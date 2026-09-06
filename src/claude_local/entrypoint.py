"""The public entry point — claude-local's owned composition root.

``implement()`` is the one front door an orchestrator drives: hand it a ``TaskSpec`` (impl path,
spec text, optional ordered read-only context files, an immutable oracle test the model may never
write, and a budget) plus a base URL and model name, and it wires the whole loop —
``HttpxBackend`` → ``ModelClient`` → ``PromptBuilder`` →
``TestRunner`` (under the kernel sandbox) → ``SnapshotStore`` → ``Loop`` — runs one bounded
red→green attempt cycle in a scratch worktree, reads the best implementation back off disk, and
returns an ``Outcome``.

The ``Outcome`` carries only what claude-local *produces and burns* — the terminal status, the
produced code (or ``None``), which files changed, and the local-half economy record. It never
computes net frontier-token savings: that verdict is the driving orchestrator's to compute from
its own frontier accounting plus this record. claude-local "just infers" — it neither downloads a
model nor serves one; ``base_url`` names an already-running OpenAI-compatible server.

This module is the single owner of the wiring: it constructs every collaborator and owns the
lifecycle of the resources it creates (the scratch worktree and, unless one is injected, the HTTP
client). Harness faults — an unreachable server, an unavailable sandbox, a broken oracle —
propagate rather than being mapped to a status, so a broken host fails loud instead of
masquerading as a failed task.
"""

from __future__ import annotations

import functools
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, assert_never

import httpx

from claude_local.backend import HttpxBackend
from claude_local.client import ModelClient
from claude_local.loop import AttemptProgress, Loop
from claude_local.prompt import PromptBuilder
from claude_local.runner import TestRunner
from claude_local.sandbox import sandboxed_spawn
from claude_local.snapshot import SnapshotStore
from claude_local.telemetry import LocalEconomyRecord
from claude_local.types import Status

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

    from claude_local.types import TaskSpec

_BUNDLED_RULES_CARD = Path(__file__).parent / "rules_card.md"
"""Default engineering-rules card — a static system prefix shipped beside the package."""

_HTTP_CONNECT_TIMEOUT_S = 10.0
"""Connect-phase cap for an owned client — reaching a local server is fast or it is down."""

_HTTP_READ_TIMEOUT_S = 600.0
"""Time-to-first-byte cap, and the DerailGuard's backstop for the one gap it cannot see.

Twelve times the 50.1s a cold 24 GB model took to answer at the benchmark's own token budget, of
which 9.1s was prefill and the rest a lazy weight load (``scripts/measure-first-byte.py``). The
margin is not padding: that figure is a floor, measured with the page cache evicted but nothing
else contending, and the same load exceeded 180s outright under the memory pressure of a sweep
that had already cycled three models through the host.
"""


@dataclass(frozen=True, slots=True)
class Outcome:
    """The result of one ``implement()`` task — only what claude-local produced and burned.

    ``code`` is the best implementation the loop reached, read off disk after the loop restored
    its best scored snapshot; it is ``None`` when the loop scored no model edit, regardless of any
    caller-seeded target already on disk. ``files_changed`` is ``(impl_path,)`` exactly when
    ``code`` is present. ``record`` is the local half of the economy story; the orchestrator owns
    the net-savings verdict, never this object. ``fault`` carries the upstream error message when
    status is ``FAULTED`` (a server-side SSE error frame stopped the run), else ``None``.
    """

    status: Status
    code: str | None
    impl_path: str
    files_changed: tuple[str, ...]
    record: LocalEconomyRecord
    fault: str | None = None

    @property
    def summary(self) -> str:
        """A past-tense, oracle-derived sentence naming the status and impl path (LLM-readable).

        Each arm names a word unique to its status so a caller (or an LLM reading the result) can
        tell the five terminal outcomes apart without inspecting ``status``.
        """
        match self.status:
            case Status.DONE:
                return f"Implemented {self.impl_path}; the oracle test passed."
            case Status.EXHAUSTED:
                return (
                    f"Exhausted the attempt budget on {self.impl_path}; "
                    f"kept the best partial (the oracle test did not pass)."
                )
            case Status.DERAILED:
                return (
                    f"Generation derailed on {self.impl_path} within the decode budget; "
                    f"produced no passing implementation."
                )
            case Status.BLOCKED:
                return (
                    f"Blocked on {self.impl_path}: the model returned no usable whole-file edit."
                )
            case Status.FAULTED:
                detail = f" ({self.fault})" if self.fault else ""
                return (
                    f"Faulted on {self.impl_path}: the model server returned an error{detail}; "
                    f"produced no passing implementation."
                )
        assert_never(self.status)


def implement(
    spec: TaskSpec,
    *,
    base_url: str,
    model: str,
    generation_params: Mapping[str, object] | None = None,
    rules_card_path: Path | None = None,
    worktree: Path | None = None,
    http_client: httpx.Client | None = None,
    on_delta: Callable[[str], None] | None = None,
    on_attempt: Callable[[AttemptProgress], None] | None = None,
) -> Outcome:
    """Drive one implementation task through the loop and return what it produced and burned.

    Wires and runs the full red→green loop against an already-running server at ``base_url``, in a
    scratch worktree (a managed tempdir unless ``worktree`` is supplied). The oracle budget's
    wall-clock (``spec.budget.timeout_s``) is bound into the kernel sandbox that runs the oracle,
    so a non-terminating implementation is killed at the task's budget rather than the sandbox's
    default backstop.

    Args:
        spec: The task — impl path (must be nested under a directory), spec text, optional
            ordered read-only context files, the immutable oracle test, expected test count,
            and the budget.
        base_url: The OpenAI-compatible server to infer against. claude-local does not serve.
        model: The model name to request from that server.
        generation_params: Optional extra generation parameters forwarded to the backend.
        rules_card_path: Override for the system-prefix rules card. Defaults to the bundled card.
        worktree: Override for the scratch worktree. Defaults to a managed temp directory that is
            created and removed around the run (the produced code is read back before removal).
        http_client: An HTTP client to reuse. When omitted, one is created for the call and closed
            on exit; an injected client is the caller's and is never closed here.
        on_delta: Optional live-decode observer, called with each raw content delta as it arrives.
            Raw means pre-normalisation: a reasoning model's channel markup streams verbatim
            (D-PROGRESS-002).
        on_attempt: Optional live-progress observer, called with one ``AttemptProgress`` per
            attempt the moment it resolves.

    Returns:
        An ``Outcome`` with the terminal status, produced code (or ``None``), changed files, and
        the local-half economy record.

    Raises:
        ValueError: ``spec.impl_path`` is not nested under a directory, so the writable subtree
            would collide with the worktree-root oracle test.
        BackendUnavailable: the model server at ``base_url`` was unreachable or returned an error
            status — a harness fault (the running server is a prerequisite), propagated, never
            mapped to a status.
        SandboxUnavailable: the host lacks the kernel sandbox — a harness fault, not a task
            outcome (propagated, never mapped to a status).
        OracleError: the oracle produced no verdict — a broken oracle, propagated as a fault.
    """
    subtree = _writable_subtree(spec.impl_path)
    card = rules_card_path if rules_card_path is not None else _BUNDLED_RULES_CARD
    owns_client = http_client is None
    client = _new_http_client() if owns_client else http_client
    try:
        with _scratch_worktree(worktree) as wt:
            (wt / Path(spec.impl_path).parent).mkdir(parents=True, exist_ok=True)
            loop = Loop(
                # Each observer goes to the module that owns its event — deltas to the decode,
                # attempts to the cycle — so neither module carries a callback it never calls.
                client=ModelClient(
                    HttpxBackend(base_url, client, model, generation_params), on_delta=on_delta
                ),
                prompt_builder=PromptBuilder(card),
                runner=TestRunner(
                    spawn=functools.partial(sandboxed_spawn, timeout_s=spec.budget.timeout_s)
                ),
                snapshots=SnapshotStore(wt, subtree),
                model=model,
                on_attempt=on_attempt,
            )
            result = loop.run(spec, wt)
            code = (
                (wt / spec.impl_path).read_text(encoding="utf-8")
                if result.has_scored_edit
                else None
            )
        files_changed = (spec.impl_path,) if code is not None else ()
        return Outcome(
            status=result.status,
            code=code,
            impl_path=spec.impl_path,
            files_changed=files_changed,
            record=result.record,
            fault=result.fault,
        )
    finally:
        if owns_client:
            client.close()


def _writable_subtree(impl_path: str) -> str:
    """Return the top path segment of ``impl_path`` — the one subtree the model may write.

    The oracle test is written to the worktree root, and the snapshot store captures the whole
    writable subtree; a flat ``impl_path`` would put the impl at the root beside the oracle,
    letting a snapshot swallow (or a write clobber) the immutable test. Requiring a nesting
    directory keeps the two disjoint by construction.

    Raises:
        ValueError: ``impl_path`` has no parent directory.
    """
    parts = Path(impl_path).parts
    if len(parts) < 2:
        raise ValueError(
            f"impl_path must be nested under a directory (e.g. 'src/foo.py'), got {impl_path!r}"
        )
    return parts[0]


def _new_http_client() -> httpx.Client:
    """Create the keep-alive HTTP client for an owned-lifecycle call.

    The read timeout outlasts the guard's stall bound rather than equalling it, because the two
    are not peers. The guard bounds a stream delivering bytes but no content and reports one task
    stalled; the socket bounds a stream delivering no bytes at all, and reaches that verdict from
    outside the guard's sight — ``_ticking`` runs the guard's clock on chunk arrival, so a stretch
    with no chunks in it is precisely the gap the guard cannot judge. Made equal, the socket won
    every race, including the ones the guard could see. Strictly greater, the guard decides
    whatever it can observe and the socket only what it alone can.

    Its magnitude clears the longest legitimate silence a healthy server can open, which is the
    lazy weight load, not the prefill: ``model_server`` reports ready off ``/v1/models``, which
    answers as soon as the port binds, while mlx_vlm faults the weights in on the first inference
    request. A 24 GB model therefore spends minutes mid-request with nothing on the socket, and
    the server's own prefill timings cannot see it — they start once the weights are resident.
    ``scripts/measure-first-byte.py`` is the measurement; re-run it when a larger model joins the
    registry, because this bound tracks the biggest weights in the catalog.

    It still deliberately does not scale with the task's ``timeout_s``. That budget bounds how long
    a PRODUCING generation may run, and pinning a silence bound to it made the transport wait
    longer the more generous the task was — the opposite of what a hang detector should do.
    """
    timeout = httpx.Timeout(_HTTP_READ_TIMEOUT_S, connect=_HTTP_CONNECT_TIMEOUT_S)
    return httpx.Client(timeout=timeout)


@contextmanager
def _scratch_worktree(worktree: Path | None) -> Iterator[Path]:
    """Yield the worktree to run in — a caller-supplied one as-is, else a managed tempdir.

    A managed tempdir is created on entry and removed on exit, so the produced code MUST be read
    back inside the ``with`` block, before the directory is gone.
    """
    if worktree is not None:
        yield worktree
    else:
        with tempfile.TemporaryDirectory(prefix="claude-local-") as tmp:
            yield Path(tmp)
