"""Immutable oracle for the quicksort example — the failing test the model may never edit.

claude-local writes this file to the loop's worktree root and the model's implementation to
``src/quicksort.py`` beside it; the two paths are disjoint, so the model authors only the impl
and can never touch this test. The import below therefore resolves against ``src/`` in the
worktree (or in this example directory during local verification), never against anything in the
repo — so a green run is a real, unedited pass. Every expected value is hand-derived from the
definition of an ascending sort, not read from running an implementation, so the oracle bites a
wrong one.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from quicksort import quicksort


def test_quicksort_empty_returns_empty() -> None:
    assert quicksort([]) == []


def test_quicksort_single_element_is_unchanged() -> None:
    assert quicksort([42]) == [42]


def test_quicksort_already_sorted_stays_sorted() -> None:
    assert quicksort([1, 2, 3]) == [1, 2, 3]


def test_quicksort_reverse_input_comes_back_ascending() -> None:
    assert quicksort([3, 2, 1]) == [1, 2, 3]


def test_quicksort_keeps_every_duplicate() -> None:
    assert quicksort([3, 1, 2, 1, 3]) == [1, 1, 2, 3, 3]


def test_quicksort_orders_negatives_below_zero() -> None:
    assert quicksort([-5, 3, -1, 0, -5]) == [-5, -5, -1, 0, 3]


def test_quicksort_does_not_mutate_its_input() -> None:
    original = [3, 1, 2]
    quicksort(original)
    assert original == [3, 1, 2]
