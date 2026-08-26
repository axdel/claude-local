"""Shared value objects — the leaf vocabulary of the loop.

Only genuinely cross-module types live here: ``Status`` (the terminal outcome),
``Budget`` (the hard per-task bounds), and ``TaskSpec`` (one implementation
task). Per-owner records — ``TestScore`` (runner), ``LocalEconomyRecord``
(telemetry), ``GenerationResult`` (client), ``LoopResult`` (loop) — live with
their owners, so this module imports nothing from the package and stays a leaf
every other module can depend inward on.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Status(StrEnum):
    """The four terminal outcomes of a loop run.

    Values are stable lowercase strings: telemetry serializes them into the
    economy record and the contract adapter maps them to a BuildStatus.
    """

    DONE = "done"
    DERAILED = "derailed"
    EXHAUSTED = "exhausted"
    BLOCKED = "blocked"


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
class TaskSpec:
    """One implementation task handed to the loop.

    ``expected_tests`` pins the collected-node count the oracle validates against,
    so a reply that imports tests away fails the count check instead of passing.
    """

    impl_path: str
    spec_text: str
    test_text: str
    expected_tests: int
    budget: Budget

    def __post_init__(self) -> None:
        if not self.impl_path.strip():
            raise ValueError("impl_path must name a file, got empty or whitespace")
        if self.expected_tests <= 0:
            raise ValueError(f"expected_tests must be >= 1, got {self.expected_tests}")
