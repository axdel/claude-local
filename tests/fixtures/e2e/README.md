# e2e fixtures — provenance

Inputs for the end-to-end loop ceremony (`tests/test_e2e_loop.py`). The test drives the real
`Loop.run` with only the model doubled: these files supply the immutable oracle and the sequence
of whole-file implementations a model would return, replayed as schema-derived SSE.

## Files

- `oracle_test.txt` — the immutable oracle test. The loop writes it verbatim into the worktree
  root; the model may never modify it. It imports the model-written `calculator.add` from the
  worktree's `src/` and asserts three **hand-derived** sums (2+3=5, 7+0=7, −4+−6=−10) — pure
  specification oracles, so the test can detect a wrong implementation.
- `impl_correct.txt` — the correct implementation (`a + b`): all three cases pass → green.
- `impl_abs.txt` — `abs(a) + abs(b)`: passes the two non-negative cases, fails the negatives
  case → 2 of 3 (the least-bad partial).
- `impl_minus.txt` — `a - b`: passes only the zero case → 1 of 3.
- `impl_times.txt` — `a * b`: passes none → 0 of 3.

Each expected pass/fail count above is **derived from arithmetic**, not observed by running the
code — the same oracle discipline the assertions themselves follow.

## Why `.txt`, not `.py`

These are source *templates* read as text and either written into a temp worktree or wrapped in
an SSE content delta at test time. The extension keeps the outer pytest run from collecting the
oracle (which matches `*_test.py`) or importing a bare `calculator.add` from the impl templates.

## SSE shape

The test wraps each implementation in a wire-faithful streaming frame sequence (role chunk,
content delta, finish, `include_usage` trailer, `[DONE]`) matching `tests/fixtures/sse/
complete_stream.bytes` — schema-derived from the OpenAI-compatible streaming contract, not
authored from a mental model of the wire. The derail scenario replays a single oversized content
delta with no usage trailer, so the guard cuts decode before the stream finishes.
