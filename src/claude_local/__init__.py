"""Claude Local — free local models as supervised, test-first code implementers.

A deterministic red→green loop that drives a local model to make a frontier-authored
failing test pass. The model receives a distilled rules card, the task spec, and the
failing test; it emits raw implementation text that the loop applies to disk and runs.
The oracle test is the judge — green means done.

``implement`` is the one public front door: hand it a ``TaskSpec`` (built from ``Budget`` and,
when needed, read-only ``ContextFile`` neighbors), the base URL of an already-running
OpenAI-compatible server, and a model name; it returns an ``Outcome``.

Two shared owners are also re-exported here so downstream consumers derive them through the
top-level API rather than reaching into a submodule (D-BENCH-002): ``slug_model_id`` (the single
owner of model-id → filename slugging) and ``TARGET_FILE_LABEL`` (the prompt's target-file wire
label, which a replay transport parses).
"""

from claude_local.entrypoint import Outcome, implement
from claude_local.prompt import TARGET_FILE_LABEL
from claude_local.telemetry import slug_model_id
from claude_local.types import Budget, ContextFile, Status, TaskSpec

__version__ = "0.1.0"

__all__ = [
    "TARGET_FILE_LABEL",
    "Budget",
    "ContextFile",
    "Outcome",
    "Status",
    "TaskSpec",
    "implement",
    "slug_model_id",
]
