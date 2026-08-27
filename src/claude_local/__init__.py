"""Claude Local — free local models as supervised, test-first code implementers.

A deterministic red→green loop that drives a local model to make a frontier-authored
failing test pass. The model receives a distilled rules card, the task spec, and the
failing test; it emits raw implementation text that the loop applies to disk and runs.
The oracle test is the judge — green means done.

``implement`` is the one public front door: hand it a ``TaskSpec`` plus the base URL of an
already-running OpenAI-compatible server and a model name, and it returns an ``Outcome``.
"""

from claude_local.entrypoint import Outcome, implement
from claude_local.types import Budget, Status, TaskSpec

__version__ = "0.1.0"

__all__ = ["Budget", "Outcome", "Status", "TaskSpec", "implement"]
