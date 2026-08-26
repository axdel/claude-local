"""Oracle tests for the shared value objects (Status, Budget, TaskSpec).

These pin the leaf vocabulary of the loop: the terminal Status set, and the
frozen, self-validating Budget and TaskSpec. Every expected value derives from
the spec (the four terminal states; the positive-bounds and non-empty-path
invariants) — never from running the implementation under test.
"""

from __future__ import annotations

import dataclasses

import pytest

from claude_local.types import Budget, Status, TaskSpec


def build_budget(**overrides: object) -> Budget:
    """Canonical valid Budget; a test overrides only the field it exercises."""
    fields: dict[str, object] = {"max_attempts": 3, "max_tokens": 2048, "timeout_s": 30.0}
    fields.update(overrides)
    return Budget(**fields)  # type: ignore[arg-type]


def build_task_spec(**overrides: object) -> TaskSpec:
    """Canonical valid TaskSpec; a test overrides only the field it exercises."""
    fields: dict[str, object] = {
        "impl_path": "src/claude_local/widget.py",
        "spec_text": "Implement widget.",
        "test_text": "def test_widget():\n    assert True\n",
        "expected_tests": 1,
        "budget": build_budget(),
    }
    fields.update(overrides)
    return TaskSpec(**fields)  # type: ignore[arg-type]


# --- Status ---------------------------------------------------------------


def test_status_has_exactly_the_four_terminal_states() -> None:
    # Oracle: the spec's terminal set — nothing more, nothing less.
    assert {s.name for s in Status} == {"DONE", "DERAILED", "EXHAUSTED", "BLOCKED"}


@pytest.mark.parametrize(
    ("member", "value"),
    [
        (Status.DONE, "done"),
        (Status.DERAILED, "derailed"),
        (Status.EXHAUSTED, "exhausted"),
        (Status.BLOCKED, "blocked"),
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


# --- TaskSpec -------------------------------------------------------------


def test_task_spec_exposes_its_fields() -> None:
    budget = build_budget()
    spec = build_task_spec(impl_path="a/b.py", expected_tests=4, budget=budget)
    assert spec.impl_path == "a/b.py"
    assert spec.expected_tests == 4
    assert spec.budget is budget


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
