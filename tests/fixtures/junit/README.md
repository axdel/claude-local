# `junit/` — captured pytest JUnit-XML fixtures for `runner.score_junit`

Each `.xml` is a **real pytest JUnit report** (`--junit-xml`, `junit_family=xunit2`), captured
verbatim by running pytest on a throwaway test file. This is the highest-fidelity provenance
(Boundary Fixture Fidelity): the xunit2 schema is pytest's, not ours, so these are **captured,
never authored from memory** of the format.

The expected `TestScore` in `test_runner.py` is hand-derived from the xunit2 `<testsuite>`
attribute semantics + D-ORACLE-001 (`collected = tests − skipped`), **not** from running
`score_junit` — the parser is under test, so it can never be its own oracle.

| Fixture | Real `<testsuite>` attrs | Shape it pins |
|-|-|-|
| `all_pass.xml` | tests=3 failures=0 errors=0 skipped=0 | valid + green (the only green case) |
| `one_failure.xml` | tests=3 failures=1 | valid but a failure → not green |
| `import_error.xml` | tests=1 errors=1 | a collection/import error → collected 1 ≠ expected → invalid |
| `mismatched_count.xml` | tests=2 | fewer ran than pinned → invalid |
| `skipped.xml` | tests=3 skipped=1 | a skip drops collected (3−1) below the pin → invalid, never green |

**Captured finding:** a collection/import error is *not* "zero-collected" — pytest emits
`tests="1" errors="1"`, one placeholder `<testcase classname="">` for the errored module. The
`collected == expected` pin rejects it anyway (1 ≠ 3), and `errors == 0` in `is_green` guards the
coincidental `expected == 1` case.

Captured 2026-08-27 against pytest under uv (Python 3.14). Regenerate with one throwaway test
file per shape (a passing set, a failing member, a bad import, a short set, a `@pytest.mark.skip`);
provenance stays "captured", never hand-edited.
