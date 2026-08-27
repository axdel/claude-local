"""Canonical test factories for the loop's value objects.

One ``build_<entity>`` per domain type, shared across every test module so no test
re-invents construction. Each returns a valid instance and accepts partial overrides;
a test specifies only the field it exercises and takes sensible defaults for the rest.
"""

from __future__ import annotations

from claude_local.client import GenerationResult
from claude_local.types import Budget, TaskSpec


def build_generation_result(**overrides: object) -> GenerationResult:
    """Canonical valid GenerationResult; a telemetry test overrides only the field it needs.

    Defaults describe one clean, server-counted generation (exact tokens, no derail); a test
    aggregating a timeline sets only ``completion_tokens`` / ``seconds`` / ``tokens_estimated``.
    """
    fields: dict[str, object] = {
        "text": "impl",
        "completion_tokens": 100,
        "tokens_estimated": False,
        "seconds": 2.0,
        "derail_reason": None,
    }
    fields.update(overrides)
    return GenerationResult(**fields)  # type: ignore[arg-type]


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
