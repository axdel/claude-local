"""Value object for one benchmark case and its immutable input fixtures."""

from __future__ import annotations

import ast
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath

from claude_local import Budget, ContextFile, TaskSpec


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

    def __post_init__(self) -> None:
        _validate_fixture_path(self.task.impl_path)
        golden_by_path = _golden_files_by_path(self.golden_tree)

        if self.task.impl_path not in golden_by_path:
            raise ValueError(
                f"implementation path must appear exactly once in golden_tree: "
                f"{self.task.impl_path!r}"
            )

        context_paths: set[str] = set()
        for context_file in self.task.context_files:
            _validate_fixture_path(context_file.path)
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
        _validate_fixture_path(golden_file.path)
        if golden_file.path in golden_by_path:
            raise ValueError(f"duplicate golden-tree path: {golden_file.path!r}")
        canonical_path = unicodedata.normalize("NFC", golden_file.path).casefold()
        if canonical_path in canonical_paths:
            raise ValueError(f"case-insensitive golden-tree path collision: {golden_file.path!r}")
        canonical_paths.add(canonical_path)
        golden_by_path[golden_file.path] = golden_file
    return golden_by_path


def _validate_fixture_path(relative_path: str) -> None:
    """Require a normalized POSIX path naming a regular file below the case worktree."""
    path = PurePosixPath(relative_path)
    if (
        path.is_absolute()
        or len(path.parts) < 2
        or relative_path != path.as_posix()
        or relative_path.endswith("/")
        or path.name in {"", ".", ".."}
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"fixture path must name a regular relative file, got {relative_path!r}")


def _oracle_test_count(oracle_text: str) -> int:
    """Count module-level pytest test functions declared in an oracle source file."""
    tree = ast.parse(oracle_text)
    return sum(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
        for node in tree.body
    )
