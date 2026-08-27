"""Unit + e2e tests for the public entry point — claude-local's owned composition root.

``implement()`` is the real front door: it wires HttpxBackend → ModelClient → PromptBuilder →
TestRunner (under the kernel sandbox) → SnapshotStore → Loop, runs one bounded task in a scratch
worktree, reads the best implementation back off disk, and returns an ``Outcome`` carrying
only what claude-local produces and burns. These tests drive the REAL HTTP decode path through an
``httpx.MockTransport`` — the model's response is the one true-external seam, everything else runs
live. Validation, the derail path, and client lifecycle are cross-platform (a derail trips during
decode, before any oracle runs); the DONE happy path and the timeout binding are gated on the macOS
sandbox that runs the real ``uv run pytest`` oracle.
"""

from __future__ import annotations

import json
import sys
import time

import httpx
import pytest
from factories import build_budget, build_local_economy_record, build_task_spec

from claude_local.entrypoint import Outcome, _writable_subtree, implement
from claude_local.sandbox import sandbox_available
from claude_local.types import Status

_MODEL = "test/model"

# A real two-case add() oracle: the expected values (5, -10) are hand-derived from integer
# addition, never read from running an implementation — so the oracle bites a wrong impl
# (Oracle Assertions).
_ADDER_ORACLE = (
    "import sys\n"
    "from pathlib import Path\n"
    "\n"
    "sys.path.insert(0, str(Path(__file__).parent / 'src'))\n"
    "\n"
    "from adder import add\n"
    "\n"
    "\n"
    "def test_add_two_positives() -> None:\n"
    "    assert add(2, 3) == 5\n"
    "\n"
    "\n"
    "def test_add_a_negative_pair() -> None:\n"
    "    assert add(-4, -6) == -10\n"
)
_ADDER_IMPL = "def add(a, b):\n    return a + b\n"
_HANGING_IMPL = (
    "while True:\n    pass\n"  # hangs at import — the oracle blocks until the sandbox kills it
)


# --- SSE assembly + mock model transport -------------------------------------------

_CHUNK: dict[str, object] = {"id": "chatcmpl-t", "object": "chat.completion.chunk"}


def _data(frame: dict[str, object]) -> str:
    """One ``data:``-framed SSE line for a chunk frame — the OpenAI streaming wire form."""
    return f"data: {json.dumps(frame)}\n\n"


def _sse(*frames: dict[str, object]) -> bytes:
    """Assemble ``data:``-framed SSE chunks terminated by the ``[DONE]`` sentinel."""
    return ("".join(_data(frame) for frame in frames) + "data: [DONE]\n\n").encode()


def _clean_impl_reply(code: str) -> bytes:
    """A wire-faithful clean stream whose one content delta is a fenced whole-file block.

    Role chunk, one fenced-code delta, a stop finish, then a usage trailer of 40 completion
    tokens — so the real decoder reports a server-counted (non-estimated) total, mirroring the
    streaming contract's usage frame.
    """
    content = f"```python\n{code}```"
    return _sse(
        {**_CHUNK, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]},
        {**_CHUNK, "choices": [{"index": 0, "delta": {"content": content}}]},
        {**_CHUNK, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {**_CHUNK, "choices": [], "usage": {"completion_tokens": 40, "total_tokens": 40}},
    )


def _runaway_reply(size: int = 256) -> bytes:
    """A single oversized content delta, no usage trailer — trips the derail guard mid-decode."""
    role = {**_CHUNK, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]}
    delta = {**_CHUNK, "choices": [{"index": 0, "delta": {"content": "z" * size}}]}
    return (_data(role) + _data(delta)).encode()


def _mock_client(reply: bytes) -> httpx.Client:
    """An httpx client whose transport returns ``reply`` for the streamed POST.

    The mock transport is the doubled model — the one true-external seam.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=reply)

    return httpx.Client(transport=httpx.MockTransport(_handler))


@pytest.fixture
def _project_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin VIRTUAL_ENV to the active venv so the sandboxed ``uv run pytest`` resolves from a
    temp cwd.

    The default spawn passes no env=, and the inner uv resolves its interpreter from the
    environment; pinning VIRTUAL_ENV to sys.prefix lets the child resolve pytest from the scratch
    worktree regardless of how this suite was launched. monkeypatch restores the prior value.
    """
    monkeypatch.setenv("VIRTUAL_ENV", sys.prefix)


# --- Unit: the writable-subtree derivation -----------------------------------------


@pytest.mark.parametrize(
    ("impl_path", "expected"),
    [("src/foo.py", "src"), ("a/b/c.py", "a"), ("pkg/mod/file.py", "pkg")],
)
def test_writable_subtree_is_the_top_path_segment(impl_path: str, expected: str) -> None:
    # Oracle: POSIX path semantics — the writable subtree is the first path component.
    assert _writable_subtree(impl_path) == expected


def test_writable_subtree_rejects_a_flat_path() -> None:
    # Oracle: a flat impl_path has no directory to host a subtree disjoint from the root
    # oracle test.
    with pytest.raises(ValueError, match="nested"):
        _writable_subtree("foo.py")


# --- Unit: fast-fail validation before any resource is acquired ---------------------


def test_implement_rejects_a_flat_impl_path_before_any_http_call() -> None:
    """A non-nested impl_path is refused up front — no HTTP call, injected client untouched.

    Proves validation precedes resource acquisition: the mock transport asserts if hit, and the
    caller-owned client is not closed by the rejected call.
    """
    spec = build_task_spec(impl_path="flat.py")

    def _boom(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call may happen when the impl_path is invalid")

    client = httpx.Client(transport=httpx.MockTransport(_boom))
    with pytest.raises(ValueError, match="nested"):
        implement(spec, base_url="http://local", model=_MODEL, http_client=client)
    assert (
        client.is_closed is False
    )  # an injected client is the caller's — never closed by implement


# --- Unit: the Outcome summary names its status and path ----------------------------


@pytest.mark.parametrize(
    ("status", "needle"),
    [
        (Status.DONE, "passed"),
        (Status.EXHAUSTED, "exhausted"),
        (Status.DERAILED, "derailed"),
        (Status.BLOCKED, "blocked"),
        (Status.FAULTED, "faulted"),
    ],
)
def test_outcome_summary_names_the_status_and_path(status: Status, needle: str) -> None:
    """Each summary is a past-tense sentence naming the impl path and a word UNIQUE to its status.

    The needle is unique per arm, so a mutant that swaps two summary arms changes the needle a case
    asserts on and is killed (LLM-readable return value — service-layer discipline).
    """
    outcome = Outcome(
        status=status,
        code=None,
        impl_path="src/thing.py",
        files_changed=(),
        record=build_local_economy_record(status=status),
    )
    summary = outcome.summary
    assert "src/thing.py" in summary
    assert needle in summary.lower()


def test_faulted_summary_surfaces_the_upstream_error_message() -> None:
    """A FAULTED outcome carries the upstream server message on ``.fault`` and in the summary.

    Naming the specific fault (context length vs overload vs auth) is the actionable diagnostic —
    a bare "the server errored" would hide which fault occurred. The message is the oracle: the
    exact text passed in must reach the reader.
    """
    outcome = Outcome(
        status=Status.FAULTED,
        code=None,
        impl_path="src/thing.py",
        files_changed=(),
        record=build_local_economy_record(status=Status.FAULTED),
        fault="context length exceeded",
    )
    assert outcome.fault == "context length exceeded"
    assert "context length exceeded" in outcome.summary
    assert "src/thing.py" in outcome.summary


# --- Unit: the DERAILED path (cross-platform — derail precedes any oracle run) ------


def test_implement_derailed_reply_returns_no_code_and_an_honest_record() -> None:
    """A runaway reply trips the derail guard mid-decode — before any oracle runs — so implement()
    returns DERAILED with no code and a one-call, estimated-tokens record.

    Exercises the real httpx → client → loop wiring cross-platform (no sandbox needed: the derail
    precedes the sandboxed oracle) AND pins the code=None / files_changed=() derivation.
    """
    spec = build_task_spec(
        impl_path="src/adder.py",
        test_text=_ADDER_ORACLE,
        expected_tests=2,
        budget=build_budget(max_attempts=3, max_tokens=2),  # tiny cap: 256 chars derail at once
    )
    client = _mock_client(_runaway_reply(size=256))

    outcome = implement(spec, base_url="http://local", model=_MODEL, http_client=client)

    assert outcome.status is Status.DERAILED
    assert outcome.code is None  # derail precedes any write to the permitted path
    assert outcome.files_changed == ()
    assert outcome.impl_path == "src/adder.py"
    # local-half record: one call, one attempt, tokens estimated from chars (aborted stream)
    assert outcome.record.status is Status.DERAILED
    assert outcome.record.total_calls == 1
    assert outcome.record.attempts == 1
    assert outcome.record.tokens_estimated is True
    assert client.is_closed is False  # injected client stays the caller's


# --- Unit: the entry point owns the lifecycle of a client it creates ----------------


def test_implement_closes_an_http_client_it_created(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no client injected, implement() creates the httpx client and closes it on exit.

    Monkeypatches the factory to a tracked client and drives the (cross-platform) derail path; the
    created client must be closed when implement returns — the owned-lifecycle contract.
    """
    spec = build_task_spec(
        impl_path="src/adder.py",
        test_text=_ADDER_ORACLE,
        expected_tests=2,
        budget=build_budget(max_attempts=1, max_tokens=2),
    )
    created = _mock_client(_runaway_reply(size=256))
    monkeypatch.setattr("claude_local.entrypoint._new_http_client", lambda timeout_s: created)

    outcome = implement(spec, base_url="http://local", model=_MODEL)  # no http_client → owned

    assert outcome.status is Status.DERAILED
    assert created.is_closed is True  # implement closed the client it created


# --- E2E: the DONE front door through the real kernel sandbox -----------------------


@pytest.mark.skipif(
    not sandbox_available(), reason="the oracle runs under the macOS kernel sandbox"
)
def test_implement_e2e_reaches_done_through_the_real_sandbox(_project_env: None) -> None:
    """Cold-start front door: default scratch worktree + bundled rules card + real httpx decode +
    a REAL sandboxed ``uv run pytest`` on a real oracle → DONE, with the produced code returned.

    Only the model's HTTP response is doubled; the worktree, rules card, sandbox, and oracle
    are all the production defaults — the highest-fidelity exercise of the entry point.
    """
    spec = build_task_spec(
        impl_path="src/adder.py",
        spec_text="Implement add(a, b) returning the integer sum of a and b.",
        test_text=_ADDER_ORACLE,
        expected_tests=2,
        budget=build_budget(max_attempts=3, max_tokens=4096, timeout_s=120.0),
    )
    client = _mock_client(_clean_impl_reply(_ADDER_IMPL))

    outcome = implement(spec, base_url="http://local", model=_MODEL, http_client=client)

    assert outcome.status is Status.DONE
    assert outcome.code is not None
    assert "return a + b" in outcome.code
    assert outcome.files_changed == ("src/adder.py",)
    assert outcome.impl_path == "src/adder.py"
    # local-half record: one clean, server-counted call reached green
    assert outcome.record.status is Status.DONE
    assert outcome.record.total_calls == 1
    assert outcome.record.tokens_estimated is False
    assert outcome.record.total_completion_tokens == 40


@pytest.mark.skipif(
    not sandbox_available(), reason="the oracle runs under the macOS kernel sandbox"
)
def test_implement_e2e_binds_budget_timeout_to_the_sandbox(_project_env: None) -> None:
    """A hanging impl with a 2s budget is killed at ~2s, not the sandbox's 120s default.

    Proves ``spec.budget.timeout_s`` is bound into the oracle sandbox (the composition-root timeout
    wiring): a mutant that dropped the binding and fell back to the 120s default would blow this
    wall-clock bound. The hang → SandboxTimeout → zero verdict → budget spent → EXHAUSTED.
    """
    spec = build_task_spec(
        impl_path="src/adder.py",
        test_text=_ADDER_ORACLE,
        expected_tests=2,
        budget=build_budget(max_attempts=1, max_tokens=4096, timeout_s=2.0),
    )
    client = _mock_client(_clean_impl_reply(_HANGING_IMPL))

    started = time.monotonic()
    outcome = implement(spec, base_url="http://local", model=_MODEL, http_client=client)
    elapsed = time.monotonic() - started

    assert outcome.status is Status.EXHAUSTED  # the hang scored zero; the single attempt is spent
    assert elapsed < 30.0  # the 2s budget bound the sandbox, far below the 120s default
