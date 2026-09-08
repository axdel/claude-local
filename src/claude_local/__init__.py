"""Claude Local — free local models as supervised, test-first code implementers.

A deterministic red→green loop that drives a local model to make a frontier-authored
failing test pass. The model receives the byte-stable prefix ``prompt.PromptBuilder``
assembles — that module's docstring and ``stable_prefix`` are the one enumeration of its
composition (INV-017) — and emits raw implementation text that the loop applies to disk
and runs. The oracle test is the judge — green means done.

``implement`` is the one public front door: hand it a ``TaskSpec`` (built from ``Budget`` and,
when needed, read-only ``ContextFile`` neighbors), the base URL of an already-running
OpenAI-compatible server, and a model name; it returns an ``Outcome``.

Four shared owners are also re-exported here so downstream consumers derive them through the
top-level API rather than reaching into a submodule (D-BENCH-002): ``slug_model_id`` (the single
owner of model-id → filename slugging), ``TARGET_FILE_LABEL`` (the prompt's target-file wire
label, which a replay transport parses), ``WHOLE_FILE_REPLY_PREFIX`` (the header of the reply a
model writes, which that same transport encodes to stand in for one), and
``mean_tokens_per_second`` (the guarded decode-rate quotient every population shares — one
generation, one task, one benchmark run).

``AttemptProgress`` is exported for the same reason: it is the event ``implement``'s ``on_attempt``
observer receives, and a downstream consumer reaches the loop only through this public API.

The three harness-fault exceptions ``implement`` documents under ``Raises`` are exported too, so a
caller catches a broken *host* distinctly from a task the model simply failed (D-BENCH-014):
``BackendUnavailable`` (the prerequisite server is unreachable), ``SandboxUnavailable`` (the host
lacks the kernel sandbox), and ``OracleError`` (the oracle produced no verdict).
"""

from claude_local.backend import BackendUnavailable
from claude_local.edits import WHOLE_FILE_REPLY_PREFIX
from claude_local.entrypoint import Outcome, implement
from claude_local.loop import AttemptProgress
from claude_local.model_registry import generation_params_from_json
from claude_local.model_session import ModelSession, SessionHandle, model_session
from claude_local.prompt import TARGET_FILE_LABEL
from claude_local.runner import OracleError
from claude_local.sandbox import SandboxUnavailable
from claude_local.telemetry import slug_model_id
from claude_local.types import Budget, ContextFile, Status, TaskSpec, mean_tokens_per_second

__version__ = "0.1.0"

__all__ = [
    "TARGET_FILE_LABEL",
    "WHOLE_FILE_REPLY_PREFIX",
    "AttemptProgress",
    "BackendUnavailable",
    "Budget",
    "ContextFile",
    "ModelSession",
    "OracleError",
    "Outcome",
    "SandboxUnavailable",
    "SessionHandle",
    "Status",
    "TaskSpec",
    "generation_params_from_json",
    "implement",
    "mean_tokens_per_second",
    "model_session",
    "slug_model_id",
]
