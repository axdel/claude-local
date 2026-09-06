"""Shared value objects — the leaf vocabulary of the loop.

Only genuinely cross-module types live here: ``Status`` (the terminal outcome),
``Budget`` (the hard per-task bounds), ``ContextFile`` (a read-only neighbor), and
``TaskSpec`` (one implementation task). Per-owner records — ``TestScore`` (runner),
``LocalEconomyRecord`` (telemetry), ``GenerationResult`` (client), and ``LoopResult``
(loop) — live with their owners, so this module imports nothing from the package
and stays a leaf every other module can depend inward on.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Status(StrEnum):
    """The five terminal outcomes of a loop run.

    Values are stable lowercase strings: telemetry serializes them into the
    economy record and the entry point surfaces them on its Outcome. ``FAULTED``
    is an upstream server fault (an SSE error frame) — distinct from a model
    ``DERAILED``/``BLOCKED`` or a budget ``EXHAUSTED``, so a reader can tell a
    server-side failure apart from the model failing the task.
    """

    DONE = "done"
    DERAILED = "derailed"
    EXHAUSTED = "exhausted"
    BLOCKED = "blocked"
    FAULTED = "faulted"


@dataclass(frozen=True, slots=True)
class Budget:
    """Hard per-task bounds: generation attempts, decode tokens, and two wall-clock deadlines.

    The two deadlines are separate because they answer opposite questions. The generation
    deadline bounds one producing decode, where a slow model streaming steadily is healthy and a
    hang is caught by silence instead (the derail guard's STALLED bound), so it is set generously.
    The oracle deadline bounds one sandboxed test run, where nothing legitimate takes long and a
    non-terminating implementation is the failure being caught, so it stays tight. All four
    values are strictly positive; the token cap is the real decode bound.
    """

    max_attempts: int
    max_tokens: int
    generation_timeout_s: float
    oracle_timeout_s: float

    def __post_init__(self) -> None:
        if self.max_attempts <= 0:
            raise ValueError(f"max_attempts must be positive, got {self.max_attempts}")
        if self.max_tokens <= 0:
            raise ValueError(f"max_tokens must be positive, got {self.max_tokens}")
        if self.generation_timeout_s <= 0:
            raise ValueError(
                f"generation_timeout_s must be positive, got {self.generation_timeout_s}"
            )
        if self.oracle_timeout_s <= 0:
            raise ValueError(f"oracle_timeout_s must be positive, got {self.oracle_timeout_s}")


@dataclass(frozen=True, slots=True)
class ContextFile:
    """An existing neighbor file shown to the model as read-only context."""

    path: str
    content: str

    def __post_init__(self) -> None:
        if not self.path.strip():
            raise ValueError("path must name a context file, got empty or whitespace")


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """One implementation task handed to the loop.

    ``expected_tests`` pins the collected-node count the oracle validates against,
    so a reply that imports tests away fails the count check instead of passing.
    ``context_files`` carries ordered, read-only neighbors the implementation must
    integrate with and defaults to none for existing callers.

    ``plan_first`` spends one generation on an implementation plan before the first
    attempt and freezes it into the prefix for the whole task. It is a property of the
    task rather than of the loop because it is measured per task class, and it defaults
    off so an unchanged caller sends the prompt every prior measurement was taken against.
    """

    impl_path: str
    spec_text: str
    test_text: str
    expected_tests: int
    budget: Budget
    context_files: tuple[ContextFile, ...] = ()
    plan_first: bool = False

    def __post_init__(self) -> None:
        if not self.impl_path.strip():
            raise ValueError("impl_path must name a file, got empty or whitespace")
        if self.expected_tests <= 0:
            raise ValueError(f"expected_tests must be >= 1, got {self.expected_tests}")
