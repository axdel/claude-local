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

import json
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
        "budget": {"max_attempts": 3, "max_tokens": 2048, "timeout_s": 30.0},
    }
    envelope.update(overrides)
    return envelope


class RecordingImplement:
    """Stands in for ``implement`` at the CLI's one true-external boundary: a server and a sandbox.

    Records the specs it was handed, so a test can assert the budget was never burned on a task
    the CLI should have refused — an absence of work, which is behavior, not call shape.
    """

    def __init__(self, status: Status = Status.DONE) -> None:
        self.status = status
        self.specs: list[TaskSpec] = []

    def __call__(self, spec: TaskSpec, **_: object) -> Outcome:
        self.specs.append(spec)
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
        ("budget", {"max_attempts": 0, "max_tokens": 2048, "timeout_s": 30.0}),
        ("budget", "3 attempts"),
        ("context_files", "src/neighbor.py"),
    ],
    ids=[
        "impl-path-not-a-string",
        "spec-text-absent",
        "expected-tests-not-positive",
        "expected-tests-is-a-bool",
        "budget-attempts-not-positive",
        "budget-not-an-object",
        "context-files-not-an-array",
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


def test_a_refusal_leaves_stdout_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Oracle: the parent's readiness probe strips stdout and compares it to a literal.

    Every diagnostic therefore belongs on stderr. A refusal message printed to stdout would still
    exit non-zero and still look correct in every other test here, while quietly making the
    version probe's own stream the place errors go.
    """
    run_envelope(monkeypatch, tmp_path, build_envelope(test_text=""))

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "test_text" in captured.err


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
        budget={"max_attempts": 2, "max_tokens": 512, "timeout_s": 90},
        context_files=[
            {"path": "app/first.py", "content": "FIRST = 1\n"},
            {"path": "app/second.py", "content": "SECOND = 2\n"},
        ],
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
    assert spec.budget.timeout_s == 90.0
    assert [(f.path, f.content) for f in spec.context_files] == [
        ("app/first.py", "FIRST = 1\n"),
        ("app/second.py", "SECOND = 2\n"),
    ]


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
