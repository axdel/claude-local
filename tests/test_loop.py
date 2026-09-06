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

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from backend_doubles import RecordingReplayBackend
from factories import (
    build_budget,
    build_local_economy_record,
    build_task_spec,
    build_test_score,
    build_whole_file_reply,
)
from sse_wire import sse_frame_json

from claude_local.backend import BackendUnavailable, ReplayBackend
from claude_local.client import ModelClient
from claude_local.loop import (
    ORACLE_TEST_FILENAME,
    AttemptProgress,
    Loop,
    LoopResult,
    _classify_terminal,
)
from claude_local.prompt import PromptBuilder
from claude_local.runner import OracleError, TestRunner
from claude_local.snapshot import SnapshotStore
from claude_local.types import Status

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from claude_local.runner import TestScore
    from claude_local.types import Budget

RULES_CARD = Path(__file__).parent.parent / "src" / "claude_local" / "rules_card.md"
JUNIT = Path(__file__).parent / "fixtures" / "junit"

# A distinctive oracle test body, so the oracle-write assertion pins content, not just existence.
_ORACLE_TEXT = "def test_widget():\n    from src import widget\n    assert widget.VALUE == 1\n"

# Whole-file impl bodies each script carries — distinct so a restore assertion pins exact bytes.
_V0 = "# widget v0\nVALUE = 0\n"
_V1 = "# widget v1\nVALUE = 1\n"
_V2 = "# widget v2\nVALUE = 2\n"


# --- Fixture / double builders ----------------------------------------------------


def _junit(name: str) -> str:
    """The captured JUnit report ``name`` — a real pytest xunit2 report, not hand-authored XML."""
    return (JUNIT / name).read_text(encoding="utf-8")


def _sse_script(text: str) -> bytes:
    """One OpenAI-style SSE stream whose single content delta is ``text``."""
    return _sse_script_parts(text)


def _sse_script_parts(*parts: str) -> bytes:
    """OpenAI-style SSE content deltas that the real decoder must concatenate in order."""
    return b"".join(sse_frame_json({"choices": [{"delta": {"content": part}}]}) for part in parts)


def _finished_sse_script(text: str, reason: str) -> bytes:
    """An SSE content delta followed by the server's terminal ``finish_reason`` frame."""
    return _sse_script(text) + sse_frame_json(
        {"choices": [{"delta": {}, "finish_reason": reason}]}
    )


def _edit_script(body: str, target: str = "src/widget.py") -> bytes:
    """An SSE stream carrying one canonical whole-file frame for ``target``."""
    return _sse_script(build_whole_file_reply(target, body))


def _error_script(message: str) -> bytes:
    """An SSE stream carrying one upstream ``{"error": ...}`` frame — a fault, not a text delta."""
    return sse_frame_json({"error": {"message": message}})


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

    def __init__(
        self,
        *reports: str,
        outputs: Sequence[tuple[bytes, bytes]] = (),
    ) -> None:
        self._reports = list(reports)
        self._outputs = list(outputs)
        self._i = 0

    def __call__(self, cmd: Sequence[str], cwd: Path, write_box: Path) -> tuple[bytes, bytes]:
        del cwd, write_box  # the fake ignores the worktree/box; it writes only where argv points
        body = self._reports[self._i]
        output = self._outputs[self._i] if self._i < len(self._outputs) else (b"", b"")
        self._i += 1
        _report_path(cmd).write_text(body, encoding="utf-8")
        return output


def _silent_spawn(cmd: Sequence[str], cwd: Path, write_box: Path) -> tuple[bytes, bytes]:
    """A spawn that produces NO report — TestRunner.run must raise OracleError (broken oracle)."""
    del cmd, cwd, write_box  # intentionally produce no JUnit report, to exercise broken-oracle
    return b"", b""


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
    on_attempt: Callable[[AttemptProgress], None] | None = None,
) -> tuple[Loop, ModelClient]:
    """Assemble a Loop over real collaborators, the two seams doubled; return it and the client."""
    client = ModelClient(backend)  # type: ignore[arg-type]
    prompt = prompt_builder if prompt_builder is not None else PromptBuilder(RULES_CARD)
    runner = TestRunner(spawn=spawn)  # type: ignore[arg-type]
    snapshots = SnapshotStore(worktree, "src")
    return Loop(client, prompt, runner, snapshots, model, on_attempt=on_attempt), client


def _widget(worktree: Path) -> str:
    """The current on-disk impl file text — what the loop left after restore_best."""
    return (worktree / "src" / "widget.py").read_text(encoding="utf-8")


# --- LoopResult contract: production derives from a scored snapshot ----------------------


@pytest.mark.parametrize(
    ("best_score", "expected"),
    [
        (None, False),
        (build_test_score(passed=2, failed=1, collected=3, expected=3), True),
        (build_test_score(passed=3, collected=3, expected=3), True),
    ],
)
def test_loop_result_has_scored_edit_derives_from_best_score(
    best_score: TestScore | None, expected: bool
) -> None:
    result = LoopResult(
        status=Status.DONE if expected else Status.BLOCKED,
        best_score=best_score,
        record=build_local_economy_record(),
    )

    assert result.has_scored_edit is expected


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


def test_sse_deltas_extract_and_write_byte_identical_payload(tmp_path: Path) -> None:
    worktree = _setup_worktree(tmp_path)
    payload = 'DOC = """\n```python\nFILE: inner.py\n```\n"""\nLABEL = "世界"\n\n'
    frame = build_whole_file_reply("src/widget.py", payload)
    script = _sse_script_parts(frame[:9], frame[9:41], frame[41:-2], frame[-2:])
    loop, _ = _make_loop(
        worktree,
        ReplayBackend([script]),
        ScriptedSpawn(_junit("all_pass.xml")),
    )
    spec = build_task_spec(
        impl_path="src/widget.py", expected_tests=3, test_text=_ORACLE_TEXT, budget=build_budget()
    )

    result = loop.run(spec, worktree)

    assert result.status is Status.DONE
    assert (worktree / "src" / "widget.py").read_bytes() == payload.encode("utf-8")


@pytest.mark.parametrize(
    "finish_reason",
    [
        pytest.param(None, id="no-finish"),
        pytest.param("stop", id="clean-stop"),
        pytest.param("length", id="length-cap"),
        pytest.param("tool_calls", id="other-terminal-reason"),
    ],
)
def test_a_cut_off_reply_is_written_and_scored_whatever_the_finish_reason(
    tmp_path: Path, finish_reason: str | None
) -> None:
    """A short payload is a file the oracle judges — extraction never reads the finish reason.

    Oracle: the frame grammar declares no length, so nothing in the reply text distinguishes a
    complete short file from a cut-off long one, and the four terminal reasons must therefore be
    indistinguishable here. The declared byte count this replaced *did* branch on them, and bought
    nothing for it: measured against gpt-oss-20b it refused payloads whose own oracle passed 7/7
    (D-EDITS-002). Truncation now reaches the oracle, which reports the syntax error with its line
    — repairable feedback, where the parser could only say BLOCKED.
    """
    worktree = _setup_worktree(tmp_path)
    partial = "# widget v0\nVALUE ="
    short_frame = build_whole_file_reply("src/widget.py", partial)
    script = (
        _sse_script(short_frame)
        if finish_reason is None
        else _finished_sse_script(short_frame, finish_reason)
    )
    spawn = ScriptedSpawn(_junit("one_failure.xml"))
    loop, _ = _make_loop(worktree, ReplayBackend([script]), spawn)
    spec = build_task_spec(
        impl_path="src/widget.py",
        expected_tests=3,
        test_text=_ORACLE_TEXT,
        budget=build_budget(max_attempts=1),
    )

    result = loop.run(spec, worktree)

    assert result.status is Status.EXHAUSTED
    assert _widget(worktree) == partial
    assert result.best_score is not None
    assert not result.best_score.is_green


def test_a_cut_off_reply_is_scored_then_repaired(tmp_path: Path) -> None:
    """The red short file feeds back as pytest diagnostics and the retry lands green."""
    worktree = _setup_worktree(tmp_path)
    partial = "# widget v0\nVALUE ="
    backend = ReplayBackend(
        [
            _sse_script(build_whole_file_reply("src/widget.py", partial)),
            _finished_sse_script(build_whole_file_reply("src/widget.py", _V1), "stop"),
        ]
    )
    spawn = ScriptedSpawn(_junit("one_failure.xml"), _junit("all_pass.xml"))
    loop, client = _make_loop(worktree, backend, spawn)
    spec = build_task_spec(
        impl_path="src/widget.py", expected_tests=3, test_text=_ORACLE_TEXT, budget=build_budget()
    )

    result = loop.run(spec, worktree)

    assert result.status is Status.DONE
    assert client.total_calls == 2
    assert result.record.attempts == 2
    assert _widget(worktree) == _V1


def test_a_second_concatenated_frame_never_reaches_its_named_path(tmp_path: Path) -> None:
    """A second frame is payload text of the first, so the path it names is never written.

    Oracle: the keep-only boundary (D-KEEP-001) — only the permitted impl path may be written.
    Retiring the byte count means a second frame is no longer refused at the parser, which moves
    this guarantee to the layer that actually owns it: whatever a reply concatenates,
    ``src/other.py`` must not exist. The stray ``FILE:`` line lands inside ``src/widget.py``,
    where the oracle reports it.
    """
    worktree = _setup_worktree(tmp_path)
    reply = build_whole_file_reply("src/widget.py", _V1) + build_whole_file_reply(
        "src/other.py", _V2
    )
    backend = ReplayBackend([_sse_script(reply)])
    loop, _ = _make_loop(worktree, backend, ScriptedSpawn(_junit("one_failure.xml")))
    spec = build_task_spec(
        impl_path="src/widget.py",
        expected_tests=3,
        test_text=_ORACLE_TEXT,
        budget=build_budget(max_attempts=1),
    )

    result = loop.run(spec, worktree)

    assert result.status is Status.EXHAUSTED
    assert not (worktree / "src" / "other.py").exists()
    assert _widget(worktree) == f"{_V1}FILE: src/other.py\n\n{_V2}"


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


def test_a_repeated_generation_is_nudged_rather_than_ending_the_run(tmp_path: Path) -> None:
    """A replay means the question must change, not that the run is over.

    Oracle: a byte-identical regeneration proves the prompt is an absorbing state — the same file
    scores the same, distils to the same brief, and so regenerates forever (INV-004). Stopping was
    the right response while the prompt was the only thing we could not change. It is the wrong one
    now that the tail can carry a different question, and the cost of the old rule is measured: on
    the standing benchmark with the repair brief, four of seven cases ended on a repeat with budget
    unspent, one of them holding six of seven oracle tests already passing, and two of them cases
    that had passed on a later attempt before.

    The fixture is the general shape of that loss: the third reply repeats the second, and the
    FOURTH is a different implementation the old rule threw away unread. Spending it is the point.
    """
    worktree = _setup_worktree(tmp_path)
    backend = ReplayBackend(
        [_edit_script(_V0), _edit_script(_V1), _edit_script(_V1), _edit_script(_V2)]
    )
    spawn = ScriptedSpawn(*([_junit("one_failure.xml")] * 4))
    loop, client = _make_loop(worktree, backend, spawn)
    spec = build_task_spec(
        impl_path="src/widget.py",
        expected_tests=3,
        test_text=_ORACLE_TEXT,
        budget=build_budget(max_attempts=4),
    )

    result = loop.run(spec, worktree)

    assert backend.served == 4  # the fourth generation — a DIFFERENT file — is now reached
    assert client.total_calls == 4
    assert result.record.attempts == 4
    # The repeat is still a real attempt: scored, counted, and left holding the partial best.
    assert result.status is Status.EXHAUSTED
    assert result.best_score is not None
    assert result.best_score.passed == 2 and not result.best_score.is_green


def test_a_repeat_and_the_nudge_that_answers_it_are_both_reported_live(tmp_path: Path) -> None:
    """The live view shows both halves: which attempt replayed, and which was asked differently.

    Oracle: the two facts have different consumers. ``repeats_previous`` explains why the model
    produced nothing new; ``nudged`` explains why the loop kept paying anyway. A watcher given only
    the first sees a loop stubbornly re-buying a known answer; given only the second, an escalation
    with no trigger. Each attempt is also still a real scored attempt — reporting ``blocked`` for a
    perfectly usable file would name the wrong cause entirely.

    The ladder is walked to its end here: three consecutive replays, so the run stops when the
    escalation runs out rather than when the budget does.
    """
    worktree = _setup_worktree(tmp_path)
    seen: list[AttemptProgress] = []
    backend = ReplayBackend([_edit_script(_V1)] * 4)
    spawn = ScriptedSpawn(*([_junit("one_failure.xml")] * 4))
    loop, _ = _make_loop(worktree, backend, spawn, on_attempt=seen.append)
    spec = build_task_spec(
        impl_path="src/widget.py",
        expected_tests=3,
        test_text=_ORACLE_TEXT,
        budget=build_budget(max_attempts=4),
    )

    loop.run(spec, worktree)

    assert [progress.attempt for progress in seen] == [1, 2, 3, 4]
    # Attempt 1 has nothing to repeat; every later one replays it.
    assert [progress.repeats_previous for progress in seen] == [False, True, True, True]
    # A nudge is a response to a repeat, so it can only appear on the attempt AFTER one.
    assert [progress.nudged for progress in seen] == [False, False, True, True]
    final = seen[-1]
    assert final.blocked is False
    assert final.score is not None
    assert final.score.passed == 2


def test_the_run_ends_when_the_nudge_ladder_is_spent_not_when_the_budget_is(
    tmp_path: Path,
) -> None:
    """A model that replays through every rung has been asked everything the card can ask.

    Oracle: the ladder is finite by design, so the run has to end somewhere other than the budget —
    otherwise a persistently deterministic model would spend every remaining attempt re-buying one
    answer, which is the exact waste the original stop-on-repeat rule existed to prevent. This
    keeps that saving and narrows it to the case where it is actually true: after the escalation is
    exhausted, not on the first sign of a replay. The budget here is six and the ladder ends it at
    four, so nothing but the ladder can be what stopped it.
    """
    worktree = _setup_worktree(tmp_path)
    backend = ReplayBackend([_edit_script(_V1)] * 6)
    spawn = ScriptedSpawn(*([_junit("one_failure.xml")] * 6))
    loop, client = _make_loop(worktree, backend, spawn)
    spec = build_task_spec(
        impl_path="src/widget.py",
        expected_tests=3,
        test_text=_ORACLE_TEXT,
        budget=build_budget(max_attempts=6),
    )

    result = loop.run(spec, worktree)

    assert client.total_calls == 4  # two unspent attempts the ladder had no new question for
    assert result.record.attempts == 4
    assert result.status is Status.EXHAUSTED


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
    # Stopped on the derail, did not exhaust the attempt budget — and was not re-asked either: an
    # unscorable reply earns one correction, but a guard kill is not a reply the model chose, so
    # there is nothing to quote back and re-asking would buy the same derail under the same bounds.
    assert client.total_calls == 1
    assert result.record.status is Status.DERAILED
    # The aborted call still cost decode time — its tokens are counted, never dropped.
    assert result.record.tokens_estimated is True
    assert result.record.total_completion_tokens > 0


def test_reply_without_a_frame_reaches_blocked(tmp_path: Path) -> None:
    """Prose reaches BLOCKED — after one correction, and after exactly one.

    The two scripted replies are also the bound: the budget allows four attempts, so an unbounded
    correction would ask for a third stream and the replay would raise rather than pass. The run
    ends on the second unscorable answer because a model told plainly what to return and answering
    the same way again has given its answer.
    """
    worktree = _setup_worktree(tmp_path)
    backend = ReplayBackend(
        [
            _sse_script("Here is an explanation, but no valid file frame."),
            _sse_script("Still explaining, still no frame."),
        ]
    )
    loop, client = _make_loop(worktree, backend, ScriptedSpawn())
    spec = build_task_spec(
        impl_path="src/widget.py", expected_tests=3, test_text=_ORACLE_TEXT, budget=build_budget()
    )

    result = loop.run(spec, worktree)

    # Prose with no valid frame is structurally BLOCKED, with nothing written or scored.
    assert result.status is Status.BLOCKED
    assert result.best_score is None
    # Two calls, not one: the first reply earned a correction, and the replay had nothing further
    # to give it, so the run ended on a second unscorable answer rather than on the first.
    assert client.total_calls == 2
    assert result.record.status is Status.BLOCKED


_TOOL_CALL_REPLY = (
    "Let me read the existing files.\n"
    "<tool_call><function=Read><parameter=file_path>app/schemas.py</parameter></function>"
    "</tool_call>"
)
"""A real blocked reply, shortened: the model asked to read files instead of writing one.

Captured from Qwen3.8-27B driven through the standing benchmark's ``04_auth_service`` case, not
composed here — an agentic coding model mistaking the loop for a tool-using harness is the reply
shape this correction exists for, and a hand-invented one would only test the parser's own idea of
prose.
"""


def test_an_unscorable_reply_earns_one_corrective_re_ask(tmp_path: Path) -> None:
    """A reply that wrote no file is re-asked once, so the case is not lost to a protocol slip.

    Oracle: the budget declares how many attempts a task may spend, and a reply that produced no
    file consumed a generation without consuming an oracle verdict — nothing has been learned that
    rules out the next attempt succeeding. Terminating there spends 1 of 4 and discards a case over
    a correctable answer, which is what the measured ``04_auth_service`` block actually was.
    """
    worktree = _setup_worktree(tmp_path)
    backend = ReplayBackend([_sse_script(_TOOL_CALL_REPLY), _edit_script(_V1)])
    loop, client = _make_loop(worktree, backend, ScriptedSpawn(_junit("all_pass.xml")))
    spec = build_task_spec(
        impl_path="src/widget.py", expected_tests=3, test_text=_ORACLE_TEXT, budget=build_budget()
    )

    result = loop.run(spec, worktree)

    assert client.total_calls == 2  # the block was corrected, not accepted as the verdict
    assert result.status is Status.DONE


def test_the_correction_carries_the_reply_that_earned_it(tmp_path: Path) -> None:
    """The re-ask shows the model its own reply, then states what to do instead.

    Oracle: the nudge contract is counterevidence followed by the imperative it leads — the same
    shape the repeat ladder uses (``_repeat_escalation``). Without the evidence the model is told
    it did something wrong and cannot see what, so the correction reads as a repetition of the
    instructions it has already failed to follow once.
    """
    worktree = _setup_worktree(tmp_path)
    backend = RecordingReplayBackend([_sse_script(_TOOL_CALL_REPLY), _edit_script(_V1)])
    loop, _ = _make_loop(worktree, backend, ScriptedSpawn(_junit("all_pass.xml")))
    spec = build_task_spec(
        impl_path="src/widget.py", expected_tests=3, test_text=_ORACLE_TEXT, budget=build_budget()
    )

    loop.run(spec, worktree)

    first_tail, second_tail = (tail for _prefix, tail in backend.calls)
    assert first_tail == ""  # the opening attempt carries no tail; only the prefix is sent
    assert "<tool_call>" in second_tail  # its own words, quoted back to it


def test_the_prefix_is_unchanged_by_a_correction(tmp_path: Path) -> None:
    """The correction rides in the tail, so the cached prefill survives it.

    Oracle: the prefix is byte-identical across a task's iterations by design (D-PROMPT-001) — a
    server reuses its prefill cache only while that holds. A correction written into the prefix
    would discard the cache on the attempt that most needs to be cheap.
    """
    worktree = _setup_worktree(tmp_path)
    backend = RecordingReplayBackend([_sse_script(_TOOL_CALL_REPLY), _edit_script(_V1)])
    loop, _ = _make_loop(worktree, backend, ScriptedSpawn(_junit("all_pass.xml")))
    spec = build_task_spec(
        impl_path="src/widget.py", expected_tests=3, test_text=_ORACLE_TEXT, budget=build_budget()
    )

    loop.run(spec, worktree)

    first_prefix, second_prefix = (prefix for prefix, _tail in backend.calls)
    assert first_prefix == second_prefix


def test_forbidden_target_reaches_blocked(tmp_path: Path) -> None:
    worktree = _setup_worktree(tmp_path)
    # The model names a path other than the permitted impl -> apply_file refuses the edit. Twice,
    # because a refused edit wrote no file and so earns the same one correction prose does — the
    # path IS part of the frame, and a reply aimed outside it is misframed rather than unframed.
    backend = ReplayBackend([_edit_script(_V1, "src/other.py")] * 2)
    loop, _ = _make_loop(worktree, backend, ScriptedSpawn())
    spec = build_task_spec(
        impl_path="src/widget.py", expected_tests=3, test_text=_ORACLE_TEXT, budget=build_budget()
    )

    result = loop.run(spec, worktree)

    assert result.status is Status.BLOCKED
    assert result.best_score is None
    assert not (worktree / "src" / "other.py").exists()  # containment held — nothing written


@pytest.mark.parametrize(
    "fixture_name",
    ["invalid_finish_reason_array.bytes", "invalid_finish_reason_object.bytes"],
)
def test_invalid_finish_reason_faults_before_parsing_or_writing(
    tmp_path: Path, fixture_name: str
) -> None:
    worktree = _setup_worktree(tmp_path)
    invalid_stream = (Path(__file__).parent / "fixtures" / "sse" / fixture_name).read_bytes()
    loop, client = _make_loop(worktree, ReplayBackend([invalid_stream]), ScriptedSpawn())
    spec = build_task_spec(
        impl_path="src/widget.py", expected_tests=3, test_text=_ORACLE_TEXT, budget=build_budget()
    )

    result = loop.run(spec, worktree)

    assert result.status is Status.FAULTED
    assert result.fault is not None and "finish_reason" in result.fault
    assert result.best_score is None
    assert client.total_calls == 1
    assert not (worktree / "src" / "widget.py").exists()


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
    oracle = worktree / ORACLE_TEST_FILENAME
    assert oracle.read_text(encoding="utf-8") == _ORACLE_TEXT
    assert not oracle.is_relative_to(worktree / "src")  # never inside the snapshot subtree


def test_prefix_is_stable_and_the_retry_tail_carries_the_file_that_failed(
    tmp_path: Path,
) -> None:
    """The prefix never moves; the tail carries both the failure AND the source that caused it.

    Oracle: ``rules_card.md`` tells the model to "return the corrected complete file. Change what
    the failure points to; keep what already passed." Both clauses name the previous file, so a
    tail without it asks for a repair of an artifact the model was never given — it must re-derive
    the whole implementation from the spec each attempt and guess which part of its own unseen code
    produced the failure.

    This overturns an earlier assertion that the prior source stays OUT of the tail. That assertion
    carried no rationale and no decision record; the shipped card is the stronger authority,
    because it is the instruction actually sent to the model. The prefix half of the guarantee is
    unchanged and still asserted here: the source rides in the tail, so KV-cache reuse is untouched
    (D-PROMPT-001).
    """
    worktree = _setup_worktree(tmp_path)
    recording = RecordingReplayBackend([_edit_script(_V0), _edit_script(_V1)])
    assertion_failure = (
        b"FAILED test_loop_oracle.py::test_widget - AssertionError: assert 0 == 1\n"
    )
    spawn = ScriptedSpawn(
        _junit("one_failure.xml"),
        _junit("all_pass.xml"),
        outputs=((assertion_failure, b""), (b"3 passed\n", b"")),
    )
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
    assert "AssertionError: assert 0 == 1" in tails[1]
    assert _V0 in tails[1]  # the file attempt 0 wrote — the artifact the card asks it to correct


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


# --- Live attempt progress: each attempt is reported as it resolves ----------------


def test_each_attempt_is_reported_while_the_run_is_still_in_flight(tmp_path: Path) -> None:
    """A progress report must arrive DURING the run, not be replayed after it.

    Oracle: the impl file's on-disk text is loop-external state that changes between attempts —
    attempt 1 writes _V1 and attempt 2 writes _V2. Reading it inside each report therefore
    distinguishes live reporting (_V1 then _V2) from a run that collected events and announced
    them at the end, which would read the restored best (_V2) both times. Attempt numbering is
    1-based because that is what a reader counts; the JUnit fixtures pin 2-of-3 then 3-of-3.
    """
    worktree = _setup_worktree(tmp_path)
    backend = ReplayBackend([_edit_script(_V1), _edit_script(_V2)])
    spawn = ScriptedSpawn(_junit("one_failure.xml"), _junit("all_pass.xml"))
    seen: list[tuple[int, int, str]] = []

    def observe(progress: AttemptProgress) -> None:
        assert progress.score is not None  # both attempts reached the oracle
        seen.append((progress.attempt, progress.score.passed, _widget(worktree)))

    loop, _ = _make_loop(worktree, backend, spawn, on_attempt=observe)
    spec = build_task_spec(
        impl_path="src/widget.py", expected_tests=3, test_text=_ORACLE_TEXT, budget=build_budget()
    )

    result = loop.run(spec, worktree)

    assert result.status is Status.DONE
    assert seen == [(1, 2, _V1), (2, 3, _V2)]


def test_a_reported_attempt_carries_the_generation_that_produced_it(tmp_path: Path) -> None:
    """The report composes the existing owners rather than restating their fields.

    Oracle: the generation's own text is the framed whole-file reply the script carried, and the
    score's counts are the captured all_pass report's 3 of 3. Both are read through the value
    objects that already own them, so neither can drift from a copy.
    """
    worktree = _setup_worktree(tmp_path)
    seen: list[AttemptProgress] = []
    loop, _ = _make_loop(
        worktree,
        ReplayBackend([_edit_script(_V1)]),
        ScriptedSpawn(_junit("all_pass.xml")),
        on_attempt=seen.append,
    )
    spec = build_task_spec(
        impl_path="src/widget.py", expected_tests=3, test_text=_ORACLE_TEXT, budget=build_budget()
    )

    loop.run(spec, worktree)

    assert len(seen) == 1
    assert seen[0].generation.text == build_whole_file_reply("src/widget.py", _V1)
    assert seen[0].score == build_test_score(passed=3, collected=3, expected=3)
    assert seen[0].blocked is False


def test_an_attempt_that_never_reached_the_oracle_is_reported_as_blocked(tmp_path: Path) -> None:
    """Prose with no usable frame still gets a report — that silence is the thing to watch.

    Oracle: BLOCKED means the attempt produced no verdict for a STRUCTURAL reason, which is
    exactly "no score, and neither the guard nor the server stopped it". An unreported blocked
    attempt would leave a watcher staring at a run that had already given up.
    """
    worktree = _setup_worktree(tmp_path)
    seen: list[AttemptProgress] = []
    loop, _ = _make_loop(
        worktree,
        ReplayBackend([_sse_script("Here is an explanation, but no valid file frame.")] * 2),
        ScriptedSpawn(),  # spawn is never called: nothing was applied to score
        on_attempt=seen.append,
    )
    spec = build_task_spec(
        impl_path="src/widget.py", expected_tests=3, test_text=_ORACLE_TEXT, budget=build_budget()
    )

    result = loop.run(spec, worktree)

    assert result.status is Status.BLOCKED
    # Both attempts are reported, not just the one that ended the run. The correction spends a
    # real generation, so a watcher shown only the last reads a two-call run as a one-call one.
    assert len(seen) == 2
    assert all(attempt.score is None for attempt in seen)
    assert all(attempt.blocked for attempt in seen)


def test_a_derailed_attempt_is_reported_as_derailed_rather_than_blocked(tmp_path: Path) -> None:
    """A derail and a structural block both score nothing; a watcher must still tell them apart.

    Oracle: the derail's cause has a single owner — ``GenerationResult.derail_reason`` — so
    ``blocked`` is the residue after the guard and the server have both been ruled out. Collapsing
    the two would report the bounded-decode kill as the model failing to answer.
    """
    worktree = _setup_worktree(tmp_path)
    seen: list[AttemptProgress] = []
    loop, _ = _make_loop(
        worktree,
        ReplayBackend([_sse_script("x" * 200)]),  # 200 chars under an 8-char cap
        ScriptedSpawn(),
        on_attempt=seen.append,
    )
    spec = build_task_spec(
        impl_path="src/widget.py",
        expected_tests=3,
        test_text=_ORACLE_TEXT,
        budget=build_budget(max_tokens=2),
    )

    result = loop.run(spec, worktree)

    assert result.status is Status.DERAILED
    assert len(seen) == 1
    assert seen[0].generation.derail_reason is not None
    assert seen[0].blocked is False


def test_a_server_fault_is_reported_as_faulted_rather_than_blocked(tmp_path: Path) -> None:
    """An upstream error frame is the host failing, and the report must say so.

    Oracle: the fault message has a single owner — ``GenerationResult.fault`` — and D-FAULT-001
    keeps a reachable server's error frame distinct from the model producing nothing usable.
    """
    worktree = _setup_worktree(tmp_path)
    seen: list[AttemptProgress] = []
    loop, _ = _make_loop(
        worktree,
        ReplayBackend([_error_script("overloaded")]),
        ScriptedSpawn(),
        on_attempt=seen.append,
    )
    spec = build_task_spec(
        impl_path="src/widget.py", expected_tests=3, test_text=_ORACLE_TEXT, budget=build_budget()
    )

    result = loop.run(spec, worktree)

    assert result.status is Status.FAULTED
    assert len(seen) == 1
    assert seen[0].generation.fault == "overloaded"
    assert seen[0].blocked is False


def test_an_unobserved_run_reaches_the_same_terminal_result(tmp_path: Path) -> None:
    """Observation is optional and must not alter the loop's outcome.

    Oracle: the two-attempt REPAIR path already pinned above — one_failure then all_pass reaches
    DONE with _V1 restored — asserted here with no observer attached.
    """
    worktree = _setup_worktree(tmp_path)
    backend = ReplayBackend([_edit_script(_V0), _edit_script(_V1)])
    spawn = ScriptedSpawn(_junit("one_failure.xml"), _junit("all_pass.xml"))
    loop, client = _make_loop(worktree, backend, spawn)
    spec = build_task_spec(
        impl_path="src/widget.py", expected_tests=3, test_text=_ORACLE_TEXT, budget=build_budget()
    )

    result = loop.run(spec, worktree)

    assert result.status is Status.DONE
    assert client.total_calls == 2
    assert _widget(worktree) == _V1
