"""Oracle tests for the shared Status, Budget, ContextFile, and TaskSpec values.

These pin the loop's five terminal states and its frozen, self-validating value
objects. Every expected value derives from the spec (the terminal set, positive
bounds, and non-empty-path invariants), never from the implementation under test.
"""

from __future__ import annotations

import dataclasses

import pytest
from factories import build_budget, build_context_file, build_task_spec

import claude_local
import claude_local.types as shared_types
from claude_local.types import Status

# --- Status ---------------------------------------------------------------


def test_status_has_exactly_the_five_terminal_states() -> None:
    # Oracle: the spec's terminal set — nothing more, nothing less. FAULTED is the upstream
    # server-fault outcome, distinct from a model DERAILED/BLOCKED or budget EXHAUSTED.
    assert {s.name for s in Status} == {"DONE", "DERAILED", "EXHAUSTED", "BLOCKED", "FAULTED"}


@pytest.mark.parametrize(
    ("member", "value"),
    [
        (Status.DONE, "done"),
        (Status.DERAILED, "derailed"),
        (Status.EXHAUSTED, "exhausted"),
        (Status.BLOCKED, "blocked"),
        (Status.FAULTED, "faulted"),
    ],
)
def test_status_values_are_stable_lowercase(member: Status, value: str) -> None:
    # Oracle: stable serialized values — telemetry writes these into the record.
    assert member.value == value


# --- Budget ---------------------------------------------------------------


def test_budget_exposes_its_bounds() -> None:
    budget = build_budget(max_attempts=5, max_tokens=1000, timeout_s=12.5)
    assert (budget.max_attempts, budget.max_tokens, budget.timeout_s) == (5, 1000, 12.5)


def test_budget_is_frozen() -> None:
    budget = build_budget()
    with pytest.raises(dataclasses.FrozenInstanceError):
        budget.max_attempts = 9  # type: ignore[misc]


@pytest.mark.parametrize("field", ["max_attempts", "max_tokens", "timeout_s"])
@pytest.mark.parametrize("bad", [0, -1])
def test_budget_rejects_non_positive_bounds(field: str, bad: int) -> None:
    # Oracle: hard caps must be strictly positive; zero or negative is invalid.
    with pytest.raises(ValueError):
        build_budget(**{field: bad})


def test_budget_accepts_minimal_positive_bounds() -> None:
    # Boundary: 1 and a small positive float are valid (kills always-raise mutants).
    budget = build_budget(max_attempts=1, max_tokens=1, timeout_s=0.001)
    assert budget.max_attempts == 1


# --- ContextFile ----------------------------------------------------------


def test_context_file_exposes_path_and_content() -> None:
    context_file = build_context_file(path="src/pkg/neighbor.py", content="VALUE = 1\n")
    assert (context_file.path, context_file.content) == ("src/pkg/neighbor.py", "VALUE = 1\n")


@pytest.mark.parametrize("bad", ["", "   "])
def test_context_file_rejects_empty_path(bad: str) -> None:
    with pytest.raises(ValueError):
        build_context_file(path=bad)


def test_context_file_is_frozen() -> None:
    context_file = build_context_file()
    with pytest.raises(dataclasses.FrozenInstanceError):
        context_file.path = "src/pkg/other.py"  # type: ignore[misc]


def test_context_file_uses_slots() -> None:
    assert not hasattr(build_context_file(), "__dict__")


def test_context_file_is_available_from_the_public_package() -> None:
    assert getattr(claude_local, "ContextFile", None) is shared_types.ContextFile


# --- TaskSpec -------------------------------------------------------------


def test_task_spec_exposes_its_fields() -> None:
    budget = build_budget()
    spec = build_task_spec(impl_path="a/b.py", expected_tests=4, budget=budget)
    assert spec.impl_path == "a/b.py"
    assert spec.expected_tests == 4
    assert spec.budget is budget


def test_task_spec_defaults_to_no_context_files() -> None:
    assert build_task_spec().context_files == ()


def test_task_spec_exposes_supplied_context_files() -> None:
    context_files = (build_context_file(),)
    assert build_task_spec(context_files=context_files).context_files == context_files


def test_task_spec_is_frozen() -> None:
    spec = build_task_spec()
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.impl_path = "other.py"  # type: ignore[misc]


@pytest.mark.parametrize("bad", [0, -1, -100])
def test_task_spec_rejects_non_positive_expected_tests(bad: int) -> None:
    # Oracle: expected_tests pins the collected-node count; must be >= 1.
    with pytest.raises(ValueError):
        build_task_spec(expected_tests=bad)


def test_task_spec_accepts_single_expected_test() -> None:
    # Boundary: exactly 1 is valid (kills an off-by-one mutant on the guard).
    assert build_task_spec(expected_tests=1).expected_tests == 1


@pytest.mark.parametrize("bad", ["", "   "])
def test_task_spec_rejects_empty_impl_path(bad: str) -> None:
    # Oracle: an empty or whitespace-only impl path names no writable target.
    with pytest.raises(ValueError):
        build_task_spec(impl_path=bad)
