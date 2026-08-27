"""Tests for the prompt assembler (``claude_local.prompt``).

The stable prefix is the KV-cache reuse invariant (D-PROMPT-001): byte-identical across a task's
iterations so the server's prefill cache is reused. These tests pin ASSEMBLY DETERMINISM (same
spec -> identical bytes) and the absence of volatile tokens in the committed card + scaffolding —
never a server cache hit, which is not observable here. Feedback distillation is oracle-tested for
its declared byte cap and for preserving the failing node id after absolute-path stripping.
"""

from __future__ import annotations

import re
from pathlib import Path

from claude_local.prompt import FEEDBACK_BYTE_CAP, PromptBuilder
from claude_local.runner import TestScore
from claude_local.types import Budget, TaskSpec

# The committed static asset — the no-volatile-content test validates the real card, not a fixture.
REAL_CARD = Path(__file__).parents[1] / "src" / "claude_local" / "rules_card.md"


def _spec(
    spec_text: str = "Implement add(a, b) returning a + b.",
    test_text: str = "def test_add():\n    assert add(1, 2) == 3\n",
) -> TaskSpec:
    return TaskSpec(
        impl_path="src/pkg/impl.py",
        spec_text=spec_text,
        test_text=test_text,
        expected_tests=1,
        budget=Budget(max_attempts=4, max_tokens=2048, timeout_s=60.0),
    )


def _card(
    tmp_path: Path, content: str = "# Rules\nWrite the whole file in one fenced block.\n"
) -> Path:
    card = tmp_path / "card.md"
    card.write_text(content, encoding="utf-8")
    return card


def _score(passed: int, failed: int, errors: int, collected: int, expected: int) -> TestScore:
    return TestScore(passed, failed, errors, collected, 0, expected)


# --- stable_prefix: determinism + completeness ------------------------------------


def test_stable_prefix_is_byte_identical_across_calls(tmp_path: Path) -> None:
    # The KV-cache invariant: two calls with the same spec must produce identical bytes.
    builder = PromptBuilder(_card(tmp_path))
    spec = _spec()
    assert builder.stable_prefix(spec) == builder.stable_prefix(spec)


def test_stable_prefix_contains_card_spec_test_and_target(tmp_path: Path) -> None:
    # All three D-PROMPT-001 parts (card + spec + immutable test) plus the target file appear.
    builder = PromptBuilder(_card(tmp_path, "# CARD-SENTINEL\nrules.\n"))
    spec = _spec(spec_text="SPEC-SENTINEL body", test_text="TEST-SENTINEL body")
    prefix = builder.stable_prefix(spec)
    assert "CARD-SENTINEL" in prefix  # the static rules card
    assert "SPEC-SENTINEL" in prefix  # the task spec
    assert "TEST-SENTINEL" in prefix  # the immutable test
    assert spec.impl_path in prefix  # the target file name


def test_stable_prefix_changes_when_spec_changes(tmp_path: Path) -> None:
    # Determinism is per-spec, not a constant blob: a different spec yields a different prefix.
    builder = PromptBuilder(_card(tmp_path))
    assert builder.stable_prefix(_spec(spec_text="A")) != builder.stable_prefix(
        _spec(spec_text="B")
    )


_TIMESTAMP_PATTERNS = (
    re.compile(r"\d{4}-\d{2}-\d{2}"),  # ISO date
    re.compile(r"\d{2}:\d{2}:\d{2}"),  # clock time
    re.compile(r"\b\d{10}\b"),  # unix timestamp
)
_FORBIDDEN_SUBSTRINGS = ("/private/", "/Users/", "/var/folders/", "TASK-", "run_id", "run-id")


def test_real_card_prefix_has_no_volatile_tokens() -> None:
    # The committed card + the builder's static scaffolding must carry NO dynamic content: a
    # timestamp/run-id/absolute path in the prefix silently discards the server prefill cache. A
    # CLEAN fixture spec is used, so any hit comes from the card or scaffolding, not provided text.
    builder = PromptBuilder(REAL_CARD)
    prefix = builder.stable_prefix(_spec(spec_text="clean spec", test_text="clean test"))
    for pat in _TIMESTAMP_PATTERNS:
        assert pat.search(prefix) is None, f"volatile timestamp-like token: {pat.pattern}"
    for sub in _FORBIDDEN_SUBSTRINGS:
        assert sub not in prefix, f"forbidden volatile substring: {sub}"


# --- distill_feedback: node id, path-strip, byte cap, score -----------------------


def test_distill_feedback_includes_the_failing_node_id(tmp_path: Path) -> None:
    builder = PromptBuilder(_card(tmp_path))
    raw = (
        "=== short test summary info ===\n"
        "FAILED tests/test_add.py::test_add_specific_case - AssertionError: assert 2 == 3\n"
        "=== 1 failed in 0.01s ===\n"
    )
    out = builder.distill_feedback(_score(0, 1, 0, 1, 1), raw)
    assert "test_add_specific_case" in out


def test_distill_feedback_strips_absolute_paths(tmp_path: Path) -> None:
    # The volatile worktree/tmp prefix is stripped; the useful relative tail (file:line) is kept.
    builder = PromptBuilder(_card(tmp_path))
    raw = "/private/var/folders/ab/xy/worktree/test_oracle.py:12: AssertionError\n"
    out = builder.distill_feedback(_score(0, 1, 0, 1, 1), raw)
    assert "/private/var/folders" not in out
    assert "test_oracle.py:12" in out


def test_distill_feedback_caps_bytes_and_keeps_node_id(tmp_path: Path) -> None:
    # 60 fat lines (300 chars each) make the tail alone far exceed the 4 KiB cap, forcing real
    # truncation — yet the node id (placed ahead of the tail) survives the trim from the end.
    builder = PromptBuilder(_card(tmp_path))
    fat = "\n".join("x" * 300 for _ in range(60))
    raw = f"FAILED tests/test_add.py::test_pinned_node - AssertionError\n{fat}\n=== 1 failed ===\n"
    out = builder.distill_feedback(_score(0, 1, 0, 1, 1), raw)
    assert len(out.encode("utf-8")) <= FEEDBACK_BYTE_CAP  # hard cap honored (declared contract)
    assert "[...truncated]" in out  # truncation actually fired (the tail overflowed the cap)
    assert (
        "test_pinned_node" in out
    )  # node id survives capping — it sits ahead of the trimmed tail


def test_distill_feedback_reports_the_score_counts(tmp_path: Path) -> None:
    # Hand-derived: 2 of 3 passed, 1 failed -> both counts appear so the model knows the gap.
    builder = PromptBuilder(_card(tmp_path))
    out = builder.distill_feedback(_score(2, 1, 0, 3, 3), "some output\n")
    assert "2/3 passed" in out
    assert "1 failed" in out


def test_distill_feedback_short_output_is_not_truncated(tmp_path: Path) -> None:
    # Capping fires only when over the cap: a small blob keeps its content, no truncation marker.
    builder = PromptBuilder(_card(tmp_path))
    out = builder.distill_feedback(_score(0, 1, 0, 1, 1), "FAILED tests/t.py::test_x - boom\n")
    assert "[...truncated]" not in out
