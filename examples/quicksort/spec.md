# Task: quicksort

Implement an ascending integer sort as a pure function.

## Signature

```python
def quicksort(values: list[int]) -> list[int]: ...
```

## Behavior

- Return a **new** list holding the same integers as `values`, ordered ascending
  (smallest first).
- The result is a reordering of the input: it has exactly the same values with the same
  multiplicities, so `len(result) == len(values)` and duplicates are all kept.
- Cover the ordinary shapes — empty list, single element, already-sorted, reverse-sorted,
  repeated values, and negative numbers.

## Constraints

- **Pure function.** Do not mutate `values`; the caller's list must be unchanged after the
  call. Build and return a new list.
- Use only the Python standard library — no third-party imports.
- Implement the sort yourself (quicksort by name); correctness is judged only by the ordering
  and the no-mutation rule, not by the technique.
