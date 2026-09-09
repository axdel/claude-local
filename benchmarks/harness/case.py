"""Value object for one benchmark case and its immutable input fixtures."""

from __future__ import annotations

import ast
import unicodedata
from dataclasses import dataclass, replace

from claude_local import Budget, ContextFile, TaskSpec
from claude_local.paths import require_nested_relative_file


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    """One validated model-facing hole in an otherwise complete golden tree."""

    task: TaskSpec
    golden_tree: tuple[ContextFile, ...]
    blank_stub: str

    @classmethod
    def from_fixtures(
        cls,
        *,
        impl_path: str,
        spec_text: str,
        oracle_text: str,
        golden_tree: tuple[ContextFile, ...],
        blank_stub: str,
        context_paths: tuple[str, ...],
        budget: Budget,
    ) -> BenchmarkCase:
        """Build a case whose context and expected-test pin derive from canonical fixtures."""
        expected_tests = _oracle_test_count(oracle_text)
        if expected_tests == 0:
            raise ValueError("oracle must declare at least one test_* function")
        golden_by_path = _golden_files_by_path(golden_tree)
        try:
            context_files = tuple(golden_by_path[path] for path in context_paths)
        except KeyError as exc:
            raise ValueError(f"context path must name a golden file: {exc.args[0]!r}") from exc
        return cls(
            task=TaskSpec(
                impl_path=impl_path,
                spec_text=spec_text,
                test_text=oracle_text,
                expected_tests=expected_tests,
                budget=budget,
                context_files=context_files,
            ),
            golden_tree=golden_tree,
            blank_stub=blank_stub,
        )

    def planning_first(self) -> BenchmarkCase:
        """The same case with the plan-first lever on — the fixtures are untouched.

        A variant rather than a load-time parameter: plan-first is a property of how a case is
        RUN, not of what the case is, so threading it down through the loader would make every
        fixture-building layer carry a flag none of them read. Sweeping the mode is then one map
        over already-loaded cases, and the pair being compared is provably the same fixtures.
        """
        return replace(self, task=replace(self.task, plan_first=True))

    def __post_init__(self) -> None:
        require_nested_relative_file(self.task.impl_path)
        golden_by_path = _golden_files_by_path(self.golden_tree)

        if self.task.impl_path not in golden_by_path:
            raise ValueError(
                f"implementation path must appear exactly once in golden_tree: "
                f"{self.task.impl_path!r}"
            )

        context_paths: set[str] = set()
        for context_file in self.task.context_files:
            require_nested_relative_file(context_file.path)
            if context_file.path == self.task.impl_path:
                raise ValueError("implementation target cannot be exposed as a context file")
            if context_file.path in context_paths:
                raise ValueError(f"duplicate context-file path: {context_file.path!r}")
            context_paths.add(context_file.path)
            golden_file = golden_by_path.get(context_file.path)
            if golden_file is None:
                raise ValueError(f"context file must name a golden file: {context_file.path!r}")
            if context_file.content != golden_file.content:
                raise ValueError(f"context file {context_file.path!r} must match its golden file")

        oracle_test_count = _oracle_test_count(self.task.test_text)
        if self.task.expected_tests != oracle_test_count:
            raise ValueError(
                "expected_tests must equal the oracle's declared test_* count: "
                f"expected {self.task.expected_tests}, found {oracle_test_count}"
            )


def _golden_files_by_path(golden_tree: tuple[ContextFile, ...]) -> dict[str, ContextFile]:
    """Index canonical golden files by their unique validated path."""
    golden_by_path: dict[str, ContextFile] = {}
    canonical_paths: set[str] = set()
    for golden_file in golden_tree:
        require_nested_relative_file(golden_file.path)
        if golden_file.path in golden_by_path:
            raise ValueError(f"duplicate golden-tree path: {golden_file.path!r}")
        canonical_path = unicodedata.normalize("NFC", golden_file.path).casefold()
        if canonical_path in canonical_paths:
            raise ValueError(f"case-insensitive golden-tree path collision: {golden_file.path!r}")
        canonical_paths.add(canonical_path)
        golden_by_path[golden_file.path] = golden_file
    return golden_by_path


def _oracle_test_count(oracle_text: str) -> int:
    """Count module-level pytest test functions declared in an oracle source file."""
    tree = ast.parse(oracle_text)
    return sum(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
        for node in tree.body
    )
