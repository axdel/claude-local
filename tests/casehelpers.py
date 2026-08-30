"""Shared benchmark-case test helpers.

Extraction helpers over a loaded ``BenchmarkCase`` that several benchmark test modules need, so
no test re-implements pulling a case's golden files — the benchmark-case counterpart of
``factories`` for the loop's value objects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from benchmarks.harness import BenchmarkCase


def golden_impl(case: BenchmarkCase) -> str:
    """Return the golden content of the one file this case blanks — its reference answer."""
    return next(file.content for file in case.golden_tree if file.path == case.task.impl_path)
