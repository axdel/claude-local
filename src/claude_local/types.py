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
    """Hard per-task bounds: generation attempts, decode tokens, wall-clock seconds.

    All three are strictly positive; the token cap is the real decode bound.
    """

    max_attempts: int
    max_tokens: int
    timeout_s: float

    def __post_init__(self) -> None:
        if self.max_attempts <= 0:
            raise ValueError(f"max_attempts must be positive, got {self.max_attempts}")
        if self.max_tokens <= 0:
            raise ValueError(f"max_tokens must be positive, got {self.max_tokens}")
        if self.timeout_s <= 0:
            raise ValueError(f"timeout_s must be positive, got {self.timeout_s}")


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
    """

    impl_path: str
    spec_text: str
    test_text: str
    expected_tests: int
    budget: Budget
    context_files: tuple[ContextFile, ...] = ()

    def __post_init__(self) -> None:
        if not self.impl_path.strip():
            raise ValueError("impl_path must name a file, got empty or whitespace")
        if self.expected_tests <= 0:
            raise ValueError(f"expected_tests must be >= 1, got {self.expected_tests}")
