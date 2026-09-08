"""Tests for the machine CLI (``claude_local.cli``) — the front door an orchestrator spawns.

The qualifier matters: this is the *machine* CLI, addressed by a dispatching orchestrator, not a
human-facing tool. Two of its guarantees are cross-repo contracts whose oracle lives in
claude-protocol rather than here, so both are pinned to their literal values below: a rename that
compiles and passes every other test would silently make claude-local undispatchable, with
nothing failing on this side.

The dispatching parent reads only the exit code and the isolation filesystem delta — it sends the
child's stdout and stderr to DEVNULL deliberately, because an untrusted child's stdout is
attacker-chosen text and its only upstream consumer is an LLM. So these tests assert exit codes,
never that a human-readable message reached the parent.
"""

from __future__ import annotations

import io
import json
import sys
from typing import TYPE_CHECKING

import pytest
from factories import build_local_economy_record

import claude_local
from claude_local import cli
from claude_local.backend import BackendUnavailable
from claude_local.cli import (
    CONTRACT_VERSION,
    EXIT_HARNESS_FAULT,
    EXIT_REJECTED_TASK,
    exit_code_for,
    main,
)
from claude_local.entrypoint import Outcome
from claude_local.loop import ORACLE_TEST_FILENAME
from claude_local.paths import KeepOnlyViolation
from claude_local.types import Status

if TYPE_CHECKING:
    from pathlib import Path

    from claude_local.types import TaskSpec


def build_envelope(**overrides: object) -> dict[str, object]:
    """A complete, runnable task envelope; a test overrides only the field it exercises."""
    envelope: dict[str, object] = {
        "impl_path": "src/quicksort.py",
        "spec_text": "Sort a list of integers ascending.",
        "test_text": "def test_sorts():\n    assert True\n",
        "expected_tests": 1,
        "budget": {
            "max_attempts": 3,
            "max_tokens": 2048,
            "generation_timeout_s": 30.0,
            "oracle_timeout_s": 30.0,
        },
    }
    envelope.update(overrides)
    return envelope


class RecordingImplement:
    """Stands in for ``implement`` at the CLI's one true-external boundary: a server and a sandbox.

    Records the specs it was handed, so a test can assert the budget was never burned on a task
    the CLI should have refused — an absence of work, which is behavior, not call shape.

    It records the run settings beside them for the same reason. Those are not incidental call
    shape either: ``implement`` is claude-local's one typed seam, and the CLI's whole job is
    adapting an invocation to it, so what the adapter hands across that seam IS its contract.
    """

    def __init__(self, status: Status = Status.DONE) -> None:
        self.status = status
        self.specs: list[TaskSpec] = []
        self.settings: list[dict[str, object]] = []

    def __call__(self, spec: TaskSpec, **settings: object) -> Outcome:
        self.specs.append(spec)
        self.settings.append(settings)
        return Outcome(
            status=self.status,
            code=None,
            impl_path=spec.impl_path,
            files_changed=(),
            record=build_local_economy_record(status=self.status),
        )


@pytest.fixture(autouse=True)
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put every test in the state a dispatched child starts in: coordinates in the environment.

    Autouse because that IS the module's subject — the parent strips the child's environment to an
    allowlist whose one open prefix is ``CLAUDE_LOCAL_``, so a configured environment is the normal
    condition here, not a per-test arrangement. The one test that needs the opposite unsets them
    explicitly, which reads as the deviation it is.
    """
    monkeypatch.setenv("CLAUDE_LOCAL_BASE_URL", "http://127.0.0.1:8080")
    monkeypatch.setenv("CLAUDE_LOCAL_MODEL", "gpt-oss-20b")


def run_envelope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    envelope: object,
    runner: RecordingImplement | None = None,
) -> int:
    """Write ``envelope`` to a task file and run the CLI over it against a stand-in implement."""
    task_file = tmp_path / "task.json"
    task_file.write_text(json.dumps(envelope), encoding="utf-8")
    monkeypatch.setattr(cli, "implement", runner if runner is not None else RecordingImplement())
    return main(["--task", str(task_file), "--worktree", str(tmp_path)])


def test_the_contract_version_is_the_exact_string_the_orchestrator_probes_for() -> None:
    """Oracle: claude-protocol compares against this literal to decide dispatchability.

    Its ready set is config AND PATH AND contract version; the expected value is a constant in
    that repo, so the string is a published contract, not an implementation detail. A test that
    read the value back out of `cli` would pin nothing — the literal is written out here on
    purpose.
    """
    assert CONTRACT_VERSION == "claude-local/1"


def test_the_contract_version_is_not_derived_from_the_package_version() -> None:
    """A release must never change dispatchability.

    Oracle: the two version lines answer different questions — `__version__` names this build
    (DERIVATION_MAP: PackageVersion, read by hatchling at build time), while the contract version
    names the dispatch protocol claude-protocol implements. Deriving one from the other would make
    a routine patch bump silently un-dispatch claude-local, since the orchestrator's expected
    string would no longer match. Fails against the tempting `f"claude-local/{__version__}"`.
    """
    assert claude_local.__version__ not in CONTRACT_VERSION


def test_the_version_probe_prints_the_contract_version_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The probe as the orchestrator actually runs it: exit 0, stdout stripping to the literal.

    Oracle: claude-protocol strips the captured stdout and compares it to its constant, so
    trailing whitespace is tolerated but any other byte on stdout is not. Asserting the stripped
    value mirrors that comparison exactly rather than pinning our own formatting.
    """
    exit_code = main(["--contract-version"])

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == CONTRACT_VERSION


def test_the_oracle_filename_is_the_exact_name_the_orchestrator_carves_out() -> None:
    """Oracle: claude-protocol excludes this exact name from the dispatch filesystem delta.

    The loop writes the immutable oracle to the worktree root, so without that carve-out every
    dispatch is discarded as an undeclared path. The name is therefore a cross-repo contract and
    is pinned to its literal here — as a private constant a rename would break their carve-out
    silently, with every dispatch discarded and nothing failing on our side.
    """
    assert ORACLE_TEST_FILENAME == "test_loop_oracle.py"


def test_only_done_exits_zero_and_every_status_gets_its_own_code() -> None:
    """Oracle: POSIX (0 is success) plus the parent's own rule, read off its dispatch path.

    The parent admits the isolation worktree's filesystem delta only on a zero exit and discards
    it whole otherwise, so mapping exactly one status to 0 is what keeps an implementation that
    never passed its oracle from being copied back. Distinctness is the second requirement: the
    parent collapses every non-zero into one status but interpolates the number into the summary
    an LLM reads, so the code is the only diagnostic that survives both streams going to DEVNULL.

    The specific integers are deliberately NOT asserted — nothing across the repo boundary binds
    to them today, and pinning them would manufacture a contract that does not exist.
    """
    codes = {status: exit_code_for(status) for status in Status}

    assert codes[Status.DONE] == 0
    assert all(code != 0 for status, code in codes.items() if status is not Status.DONE)
    assert len(set(codes.values())) == len(Status)


def test_an_empty_oracle_is_refused_before_any_budget_is_burned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The load-bearing refusal: the caller must supply the oracle the model may never author.

    Oracle: an empty test file collects zero tests, so the loop's validity check can never hold
    and Status.DONE is unreachable — the run would spend its entire budget to report EXHAUSTED.
    Asserting that ``implement`` was never reached is what proves the refusal happens first; a
    non-zero exit alone would also be produced by running the doomed task all the way to the end.
    """
    runner = RecordingImplement()

    exit_code = run_envelope(monkeypatch, tmp_path, build_envelope(test_text=""), runner)

    assert exit_code == EXIT_REJECTED_TASK
    assert runner.specs == []


def test_a_whitespace_only_oracle_is_refused_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Oracle: pytest collects zero tests from whitespace exactly as it does from an empty file.

    A truthiness check on the raw string would let this through, so the blank case is what
    distinguishes a real emptiness check from a cosmetic one.
    """
    runner = RecordingImplement()

    exit_code = run_envelope(monkeypatch, tmp_path, build_envelope(test_text="  \n\t\n"), runner)

    assert exit_code == EXIT_REJECTED_TASK
    assert runner.specs == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("impl_path", 7),
        ("spec_text", None),
        ("expected_tests", 0),
        ("expected_tests", True),
        (
            "budget",
            {
                "max_attempts": 0,
                "max_tokens": 2048,
                "generation_timeout_s": 30.0,
                "oracle_timeout_s": 30.0,
            },
        ),
        ("budget", "3 attempts"),
        ("context_files", "src/neighbor.py"),
        ("plan_first", "yes"),
    ],
    ids=[
        "impl-path-not-a-string",
        "spec-text-absent",
        "expected-tests-not-positive",
        "expected-tests-is-a-bool",
        "budget-attempts-not-positive",
        "budget-not-an-object",
        "context-files-not-an-array",
        "plan-first-not-a-boolean",
    ],
)
def test_an_unrunnable_envelope_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, field: str, value: object
) -> None:
    """Each case is a task the loop could not run, so the CLI refuses rather than starting it.

    Oracles, in order: JSON's type system (a number is not a string, an array is not a string);
    ``_writable_subtree``'s rule that a flat impl_path would collide with the worktree-root oracle;
    ``TaskSpec`` and ``Budget``'s own ``__post_init__`` bounds. ``True`` is the interesting one —
    Python makes ``bool`` a subclass of ``int``, so a JSON ``true`` satisfies a plain isinstance
    check for an integer and would silently become ``expected_tests=1``.
    """
    runner = RecordingImplement()

    exit_code = run_envelope(monkeypatch, tmp_path, build_envelope(**{field: value}), runner)

    assert exit_code == EXIT_REJECTED_TASK
    assert runner.specs == []


def test_a_flat_impl_path_is_refused_by_the_real_entry_point(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The one refusal deliberately driven through the REAL ``implement``, not a stand-in.

    ``implement`` owns this rule (a flat impl_path would put the implementation at the worktree
    root beside the immutable oracle, where a snapshot could swallow it), so re-checking it in the
    CLI would make a second writer of one rule. Nothing is mocked because nothing needs to be:
    ``_writable_subtree`` is the first statement of ``implement``, so it raises before any socket
    or sandbox is touched. What is under test is that the CLI turns that raise into a clean
    refusal rather than the traceback and exit 1 it produced before.
    """
    task_file = tmp_path / "task.json"
    task_file.write_text(json.dumps(build_envelope(impl_path="flat.py")), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert main(["--task", str(task_file)]) == EXIT_REJECTED_TASK


def test_a_traversing_impl_path_is_refused_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The dispatch door is the untrusted one, so the escape must die here, touching nothing.

    Oracle: ``src/../test_loop_oracle.py`` names the immutable oracle while declaring the
    subtree ``src``. Two independent assertions, because refusing late would still be a breach:
    the exit code says the task was rejected, and the working directory is byte-for-byte
    untouched — no scratch directory, and specifically no directory created for the path's
    parent, which is a write outside the worktree that used to happen before any check ran.
    """
    task_file = tmp_path / "task.json"
    task_file.write_text(
        json.dumps(build_envelope(impl_path="src/../test_loop_oracle.py")), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    before = sorted(path.name for path in tmp_path.iterdir())

    assert main(["--task", str(task_file)]) == EXIT_REJECTED_TASK
    assert sorted(path.name for path in tmp_path.iterdir()) == before


@pytest.mark.parametrize(
    "envelope", ["not json at all", "[1, 2, 3]", '"a string"'], ids=["invalid", "array", "scalar"]
)
def test_an_envelope_that_is_not_a_json_object_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, envelope: str
) -> None:
    """Oracle: the envelope's declared shape is a JSON object; nothing else carries the fields."""
    task_file = tmp_path / "task.json"
    task_file.write_text(envelope, encoding="utf-8")
    runner = RecordingImplement()
    monkeypatch.setattr(cli, "implement", runner)

    assert main(["--task", str(task_file)]) == EXIT_REJECTED_TASK
    assert runner.specs == []


# --- Envelope intake: the declared dispatch channel and the ways it can be refused -------------
#
# Every test above reaches the CLI through `--task <file>`, which the module docstring calls the
# convenience door. The dispatch channel is stdin, and until these tests it had no coverage at
# all: the one path an orchestrator actually takes was the one path nothing exercised.


class _TerminalStdin(io.StringIO):
    """A stdin that reports itself a terminal and fails loudly if anything reads it.

    Reading it IS the defect under test, and a real terminal expresses that as an unbounded block
    — which a test cannot reproduce without hanging the suite. Raising converts the hang into an
    immediate, legible failure. Without this the test would be vacuous: an empty stub read returns
    "", whose JSON parse fails, so a build with the terminal guard deleted would still exit
    rejected and still look green.
    """

    def isatty(self) -> bool:
        return True

    def read(self, size: int | None = -1) -> str:
        raise AssertionError("stdin was read despite reporting itself a terminal")


def test_the_envelope_is_read_from_stdin_which_is_the_dispatch_channel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Oracle: the module declares stdin the dispatch channel and argv never one, so this is the
    path a dispatching orchestrator takes; a green suite that only ever passed ``--task`` said
    nothing about it.

    The assertion is on the spec that crossed the seam, not on the exit code, because an exit code
    cannot tell "read stdin and parsed it" from "read nothing and defaulted". Recovering
    ``expected_tests`` proves the bytes on stdin became the task that ran.
    """
    runner = RecordingImplement()
    monkeypatch.setattr(cli, "implement", runner)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(build_envelope(expected_tests=7))))

    assert main(["--worktree", str(tmp_path)]) == 0
    assert [spec.expected_tests for spec in runner.specs] == [7]


def test_a_bare_invocation_at_a_terminal_is_refused_rather_than_hanging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Oracle: a terminal stdin yields no envelope and never EOFs, so reading it blocks forever.

    A hang is the worst refusal: it consumes the dispatch slot and reports nothing. The stand-in
    records that no work began, which is what separates "refused" from "ran and produced nothing".
    """
    runner = RecordingImplement()
    monkeypatch.setattr(cli, "implement", runner)
    monkeypatch.setattr(sys, "stdin", _TerminalStdin())

    assert main(["--worktree", str(tmp_path)]) == EXIT_REJECTED_TASK
    assert runner.specs == []


def test_a_task_path_that_cannot_be_read_is_refused_like_a_malformed_envelope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Oracle: a directory is a real, un-mocked OSError from ``read_text`` — IsADirectoryError.

    The refusal is the point rather than the errno: an unreadable envelope is a task the caller
    described wrongly, so it belongs in the same channel as one whose JSON will not parse, not in
    the harness-fault channel that tells an orchestrator its host is broken and to re-dispatch.
    """
    unreadable = tmp_path / "envelope_dir"
    unreadable.mkdir()
    runner = RecordingImplement()
    monkeypatch.setattr(cli, "implement", runner)

    assert main(["--task", str(unreadable), "--worktree", str(tmp_path)]) == EXIT_REJECTED_TASK
    assert runner.specs == []


@pytest.mark.parametrize(
    "envelope",
    [
        build_envelope(budget={**build_envelope()["budget"], "generation_timeout_s": "30"}),  # type: ignore[dict-item]
        build_envelope(context_files=["src/neighbor.py"]),
    ],
    ids=["timeout-is-a-string", "context-file-is-a-bare-string"],
)
def test_a_field_of_the_wrong_type_is_refused_before_any_work_begins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, envelope: dict[str, object]
) -> None:
    """Oracle: both fields have a declared shape the envelope contract names — a number, and an
    array of objects carrying ``path`` and ``content``.

    Both cases are the plausible near-miss rather than nonsense: a caller that JSON-encodes every
    value as a string, and one that sends a list of paths where a list of file objects is
    declared. A bare path would otherwise reach ``ContextFile`` as a positional string and be
    rejected far from the field that was wrong.
    """
    runner = RecordingImplement()
    monkeypatch.setattr(cli, "implement", runner)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(envelope)))

    assert main(["--worktree", str(tmp_path)]) == EXIT_REJECTED_TASK
    assert runner.specs == []


def test_a_refusal_leaves_stdout_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Oracle: the parent's contract handshake strips stdout and compares it to a literal.

    Every diagnostic therefore belongs on stderr. A refusal message printed to stdout would still
    exit non-zero and still look correct in every other test here, while quietly making the
    version probe's own stream the place errors go.
    """
    run_envelope(monkeypatch, tmp_path, build_envelope(test_text=""))

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "test_text" in captured.err


def test_a_containment_refusal_exits_as_a_rejected_task_not_an_uncaught_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Oracle: ``exit_code_for`` reserves 1 for an uncaught exception, so a refusal may not use it.

    ``resolve_within`` refuses an ``impl_path`` whose shape is legal but whose target escapes the
    worktree through a symlink — a case string validation cannot see, because only ``resolve()``
    follows links. That refusal is a statement about the caller's envelope, exactly like the flat
    path already translated here, so it owes the caller the same answer. Reaching the parent as a
    bare traceback instead makes a rejected task indistinguishable from a crashed claude-local,
    since the dispatching orchestrator reads only the exit code.

    Live reproduction against the real front door, before the fix: a worktree whose ``src`` is a
    symlink out of the tree exited 1 with a ``KeepOnlyViolation`` traceback.
    """

    def refuse_containment(_spec: TaskSpec, **_: object) -> Outcome:
        raise KeepOnlyViolation("src", "final component is a symlink")

    task_file = tmp_path / "task.json"
    task_file.write_text(json.dumps(build_envelope()), encoding="utf-8")
    monkeypatch.setattr(cli, "implement", refuse_containment)

    exit_code = main(["--task", str(task_file), "--worktree", str(tmp_path)])

    assert exit_code == EXIT_REJECTED_TASK
    assert "Traceback" not in capsys.readouterr().err


def test_missing_server_coordinates_are_refused_rather_than_guessed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Oracle: ``implement`` needs a base URL and a model; claude-local never serves one itself.

    Deliberately runs with the environment cleared — the confined child's environment is stripped
    to an allowlist, so an unset variable is the normal case, not an exotic one. A default here
    would send the task to whatever happened to be listening on this machine.
    """
    monkeypatch.delenv("CLAUDE_LOCAL_BASE_URL", raising=False)
    monkeypatch.delenv("CLAUDE_LOCAL_MODEL", raising=False)
    runner = RecordingImplement()

    exit_code = run_envelope(monkeypatch, tmp_path, build_envelope(), runner)

    assert exit_code == EXIT_REJECTED_TASK
    assert runner.specs == []


def test_generation_params_declared_on_the_command_line_reach_the_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A model's required request-body fields have a way in, or the model cannot be dispatched to.

    Oracle: the JSON specification — ``false`` is the boolean, never the string ``"false"`` — and
    the model registry, whose PARAMS column declares exactly these fields per model. The two must
    agree because the registry is where the value comes from: the dispatching parent reads a row
    and serialises it onto this flag.

    Measured, not supposed. Served with its thinking channel left on, Qwen3.8-27B-abliterated
    spends most of a generation deliberating and frames the result differently — 851 completion
    tokens for 515 characters of reply on the bundled example, which the loop then refused. The
    registry row carries ``enable_thinking=false`` for that reason, and before this flag existed
    the row had no way to reach a dispatched run: the CLI took a base URL and a model name and
    nothing else, so the one lever that fixes the flagship model was unreachable from the front
    door claude-protocol actually uses.
    """
    runner = RecordingImplement()
    task_file = tmp_path / "task.json"
    task_file.write_text(json.dumps(build_envelope()), encoding="utf-8")
    monkeypatch.setattr(cli, "implement", runner)

    exit_code = main(
        [
            "--task",
            str(task_file),
            "--worktree",
            str(tmp_path),
            "--generation-params",
            '{"enable_thinking": false, "thinking_budget": 256}',
        ]
    )

    assert exit_code == 0
    assert runner.settings[0]["generation_params"] == {
        "enable_thinking": False,
        "thinking_budget": 256,
    }


@pytest.mark.parametrize(
    "declared",
    [
        pytest.param("[1, 2]", id="json-array"),
        pytest.param('"enable_thinking=false"', id="json-string"),
        pytest.param("not json at all", id="not-json"),
    ],
)
def test_generation_params_that_name_no_fields_are_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, declared: str
) -> None:
    """A request body is an object, so anything else declares no fields and must not run.

    It has to fail here rather than later: a server drops an unrecognised body field silently, so a
    malformed declaration accepted at this layer would report a whole dispatched run as normally
    configured while the lever it named was never applied. Exit 2 is argparse's own usage code,
    which the CLI leaves to the runtime rather than claiming for a task status.
    """
    runner = RecordingImplement()
    task_file = tmp_path / "task.json"
    task_file.write_text(json.dumps(build_envelope()), encoding="utf-8")
    monkeypatch.setattr(cli, "implement", runner)

    with pytest.raises(SystemExit) as refused:
        main(["--task", str(task_file), "--generation-params", declared])

    assert refused.value.code == 2
    assert runner.specs == []  # the budget was never burned on a misconfigured run


def test_the_envelope_becomes_the_task_the_loop_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every field the caller declared reaches the ``TaskSpec``, in order, unaltered.

    Oracle: the envelope literal written below. This is the CLI's whole job — a field silently
    dropped or reordered here is a task the model is asked to solve without part of its input,
    which no downstream test would notice.
    """
    runner = RecordingImplement()
    envelope = build_envelope(
        impl_path="app/sorter.py",
        spec_text="Sort ascending.",
        test_text="def test_x():\n    assert True\n",
        expected_tests=4,
        budget={
            "max_attempts": 2,
            "max_tokens": 512,
            "generation_timeout_s": 90,
            "oracle_timeout_s": 45,
        },
        context_files=[
            {"path": "app/first.py", "content": "FIRST = 1\n"},
            {"path": "app/second.py", "content": "SECOND = 2\n"},
        ],
        plan_first=True,
    )

    assert run_envelope(monkeypatch, tmp_path, envelope, runner) == 0

    spec = runner.specs[0]
    assert spec.impl_path == "app/sorter.py"
    assert spec.spec_text == "Sort ascending."
    assert spec.test_text == "def test_x():\n    assert True\n"
    assert spec.expected_tests == 4
    assert spec.budget.max_attempts == 2
    assert spec.budget.max_tokens == 512
    # A JSON integer is a valid number: the wire type draws no float/int distinction.
    assert spec.budget.generation_timeout_s == 90.0
    assert spec.budget.oracle_timeout_s == 45.0
    assert [(f.path, f.content) for f in spec.context_files] == [
        ("app/first.py", "FIRST = 1\n"),
        ("app/second.py", "SECOND = 2\n"),
    ]
    assert spec.plan_first is True


def test_an_envelope_that_omits_plan_first_sends_the_prompt_it_always_sent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Oracle: the contract version is unchanged, so a parent written against it must still work.

    ``plan_first`` was added to the envelope without bumping ``claude-local/1``, which is only
    honest if an envelope that predates the field is run exactly as it was before — one decode
    per attempt, no plan frozen into the prefix. Defaulting it on would silently change what
    every existing caller's model is asked, and every measurement taken against them.
    """
    runner = RecordingImplement()

    assert run_envelope(monkeypatch, tmp_path, build_envelope(), runner) == 0

    assert runner.specs[0].plan_first is False


@pytest.mark.parametrize("status", list(Status))
def test_the_terminal_status_travels_out_as_the_process_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: Status
) -> None:
    """The wiring, end to end: whatever the loop reached is what the parent observes.

    Oracle: ``exit_code_for``, tested independently above. Parametrized over every member of
    ``Status`` so a status added later without a mapping fails here rather than in production.
    """
    runner = RecordingImplement(status)

    assert run_envelope(monkeypatch, tmp_path, build_envelope(), runner) == exit_code_for(status)


def test_a_green_verdict_discloses_that_it_was_judged_by_the_code_it_judges(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Oracle: D-ORACLE-004 — the verdict is computed inside the process that ran the impl.

    Zero is the one exit code that causes a parent to admit model-authored code into a real tree,
    and it rests on a verdict that model-authored top-level code can forge. Nothing in the machine
    channel can carry that caveat: the parent reads only the exit code and the filesystem delta,
    and sends both streams to DEVNULL by its own choice. So the disclosure is aimed at the operator
    reading a hand-run, who is the only party in a position to weigh it.
    """
    assert run_envelope(monkeypatch, tmp_path, build_envelope(), RecordingImplement()) == 0

    assert "same process" in capsys.readouterr().err


@pytest.mark.parametrize("status", [s for s in Status if s is not Status.DONE])
def test_only_a_green_verdict_carries_the_caveat_about_judging_itself(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    status: Status,
) -> None:
    """A non-green outcome admits nothing, so the caveat would be noise attached to a refusal.

    Parametrized over every non-DONE member so a status added later cannot quietly inherit a
    disclosure that only the delta-admitting exit code needs.
    """
    assert run_envelope(monkeypatch, tmp_path, build_envelope(), RecordingImplement(status)) != 0

    assert "same process" not in capsys.readouterr().err


def test_a_broken_host_is_reported_apart_from_a_failed_task(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Oracle: ``implement`` documents these as harness faults it propagates, never as a status.

    An unreachable server means nothing was learned about the task, so it must not be reported
    with a code that reads as a model outcome — an operator restarts a server, but retries a task.
    """

    def unreachable(_spec: TaskSpec, **_: object) -> Outcome:
        raise BackendUnavailable("http://127.0.0.1:8080", "gpt-oss-20b", "connection refused")

    task_file = tmp_path / "task.json"
    task_file.write_text(json.dumps(build_envelope()), encoding="utf-8")
    monkeypatch.setattr(cli, "implement", unreachable)

    exit_code = main(["--task", str(task_file), "--worktree", str(tmp_path)])

    assert exit_code == EXIT_HARNESS_FAULT
    assert exit_code not in {exit_code_for(status) for status in Status}


def test_no_economy_record_is_written_unless_a_directory_is_named(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Oracle: the parent discards the whole delta when the worktree holds an undeclared path.

    So writing the local economy record into the worktree by default would throw away the very
    implementation the run produced. Off unless asked is the only safe default.
    """
    run_envelope(monkeypatch, tmp_path, build_envelope())

    assert list(tmp_path.glob("*.json")) == [tmp_path / "task.json"]


def test_the_economy_record_is_written_where_the_caller_asks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The local half of the economy story reaches disk, since stdout cannot carry it.

    Oracle: ``LocalEconomyRecord`` owns the serialization (telemetry is its single writer), so the
    assertion is that a record for this run landed with its status — not what its JSON looks like.
    """
    records = tmp_path / "records"
    task_file = tmp_path / "task.json"
    task_file.write_text(json.dumps(build_envelope()), encoding="utf-8")
    monkeypatch.setattr(cli, "implement", RecordingImplement())

    main(["--task", str(task_file), "--worktree", str(tmp_path), "--record-dir", str(records)])

    written = list(records.glob("*.json"))
    assert len(written) == 1
    assert json.loads(written[0].read_text(encoding="utf-8"))["status"] == Status.DONE.value


def test_a_refused_record_directory_costs_the_record_but_not_the_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A host that will not take the record must not overwrite what the run achieved.

    Oracle: the exit code comes from this CLI's own published mapping — only DONE exits zero
    (D-CLI-003) — so the expected value is 0 regardless of what the write does. The refusal is
    constructed rather than observed: ``Path.mkdir(parents=True, exist_ok=True)`` raises
    ``FileExistsError`` when the last component is an existing non-directory file, which the
    stdlib documents as the one case ``exist_ok`` does not suppress.

    Both halves are asserted because neither implies the other. The write sits ahead of the status
    mapping, so an escaping ``OSError`` exited 1 — a code ``exit_code_for`` never returns — on a
    run that had already finished; and under dispatch stderr is ``DEVNULL``, so the traceback
    naming the unwritable directory was discarded along with the verdict it replaced. A build that
    swallows the refusal silently fails the second assertion, and one that still lets it escape
    fails the first (D-CLI-006).
    """
    records = tmp_path / "records"
    records.write_text("a file where the caller asked for a directory", encoding="utf-8")
    task_file = tmp_path / "task.json"
    task_file.write_text(json.dumps(build_envelope()), encoding="utf-8")
    monkeypatch.setattr(cli, "implement", RecordingImplement())

    exit_code = main(
        ["--task", str(task_file), "--worktree", str(tmp_path), "--record-dir", str(records)]
    )

    assert exit_code == 0  # the completed task keeps its verdict
    assert "record not written" in capsys.readouterr().err  # and the refusal is still reported
