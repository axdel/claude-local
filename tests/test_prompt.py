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

from factories import build_budget, build_context_file, build_task_spec
from hypothesis import given
from hypothesis import strategies as st

from claude_local.prompt import FEEDBACK_BYTE_CAP, PromptBuilder
from claude_local.runner import TestScore
from claude_local.types import ContextFile, TaskSpec

# The committed static asset — the no-volatile-content test validates the real card, not a fixture.
REAL_CARD = Path(__file__).parents[1] / "src" / "claude_local" / "rules_card.md"
_REAL_CARD_BYTES = REAL_CARD.read_bytes()
_REAL_BUILDER = PromptBuilder(REAL_CARD)


def _spec(
    spec_text: str = "Implement add(a, b) returning a + b.",
    test_text: str = "def test_add():\n    assert add(1, 2) == 3\n",
    context_files: tuple[ContextFile, ...] = (),
) -> TaskSpec:
    return build_task_spec(
        impl_path="src/pkg/impl.py",
        spec_text=spec_text,
        test_text=test_text,
        budget=build_budget(max_attempts=4, timeout_s=60.0),
        context_files=context_files,
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


def test_stable_prefix_with_empty_context_matches_legacy_layout(tmp_path: Path) -> None:
    builder = PromptBuilder(_card(tmp_path, "# Rules\nKeep the prefix stable.\n"))

    assert builder.stable_prefix(_spec(spec_text="SPEC", test_text="TEST")) == (
        "# Rules\n"
        "Keep the prefix stable.\n\n"
        "## Implementation task\n"
        "Target file: src/pkg/impl.py\n\n"
        "SPEC\n\n"
        "## The test — immutable; do not modify or import it away. Make it pass.\n\n"
        "TEST\n"
    )


def test_stable_prefix_contains_card_spec_test_and_target(tmp_path: Path) -> None:
    # All three D-PROMPT-001 parts (card + spec + immutable test) plus the target file appear.
    builder = PromptBuilder(_card(tmp_path, "# CARD-SENTINEL\nrules.\n"))
    spec = _spec(spec_text="SPEC-SENTINEL body", test_text="TEST-SENTINEL body")
    prefix = builder.stable_prefix(spec)
    assert "CARD-SENTINEL" in prefix  # the static rules card
    assert "SPEC-SENTINEL" in prefix  # the task spec
    assert "TEST-SENTINEL" in prefix  # the immutable test
    assert spec.impl_path in prefix  # the target file name


def test_stable_prefix_renders_ordered_read_only_context_before_test(tmp_path: Path) -> None:
    builder = PromptBuilder(_card(tmp_path, "# Rules\nUse the existing interfaces.\n"))
    spec = _spec(
        spec_text="Implement the target.",
        test_text="def test_target():\n    assert target() == 3",
        context_files=(
            build_context_file(path="src/pkg/first.py", content="FIRST = 1"),
            build_context_file(path="src/pkg/second.py", content="SECOND = 2"),
        ),
    )

    assert builder.stable_prefix(spec) == (
        "# Rules\n"
        "Use the existing interfaces.\n\n"
        "## Implementation task\n"
        "Target file: src/pkg/impl.py\n\n"
        "Implement the target.\n\n"
        "## Existing files — read-only, integrate with them, "
        "do NOT reimplement or output them.\n\n"
        "### src/pkg/first.py\n\n"
        "FIRST = 1\n\n"
        "### src/pkg/second.py\n\n"
        "SECOND = 2\n\n"
        "## The test — immutable; do not modify or import it away. Make it pass.\n\n"
        "def test_target():\n"
        "    assert target() == 3\n"
    )


_CONTEXT_FILES = st.lists(
    st.builds(
        ContextFile,
        path=st.text(
            alphabet=st.characters(exclude_categories=("Cs",)),
            min_size=1,
            max_size=32,
        ).filter(lambda path: bool(path.strip())),
        content=st.text(
            alphabet=st.characters(exclude_categories=("Cs",)),
            max_size=128,
        ),
    ),
    max_size=8,
).map(tuple)


@given(context_files=_CONTEXT_FILES)
def test_stable_prefix_is_deterministic_and_rules_card_first(
    context_files: tuple[ContextFile, ...],
) -> None:
    spec = _spec(context_files=context_files)

    first = _REAL_BUILDER.stable_prefix(spec).encode()
    second = _REAL_BUILDER.stable_prefix(spec).encode()

    assert first == second
    assert first.startswith(_REAL_CARD_BYTES)


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
