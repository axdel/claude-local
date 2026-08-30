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

The three harness-fault exceptions ``implement`` documents under ``Raises`` are exported too, so a
caller catches a broken *host* distinctly from a task the model simply failed (D-BENCH-014):
``BackendUnavailable`` (the prerequisite server is unreachable), ``SandboxUnavailable`` (the host
lacks the kernel sandbox), and ``OracleError`` (the oracle produced no verdict).
"""

from claude_local.backend import BackendUnavailable
from claude_local.entrypoint import Outcome, implement
from claude_local.prompt import TARGET_FILE_LABEL
from claude_local.runner import OracleError
from claude_local.sandbox import SandboxUnavailable
from claude_local.telemetry import slug_model_id
from claude_local.types import Budget, ContextFile, Status, TaskSpec

__version__ = "0.1.0"

__all__ = [
    "TARGET_FILE_LABEL",
    "BackendUnavailable",
    "Budget",
    "ContextFile",
    "OracleError",
    "Outcome",
    "SandboxUnavailable",
    "Status",
    "TaskSpec",
    "implement",
    "slug_model_id",
]
