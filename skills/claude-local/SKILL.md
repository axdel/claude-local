---
name: claude-local
description: Drive a free local model as a test-first implementer through claude-local's red→green loop. Hand it a spec and an immutable oracle test; get back a passing whole-file implementation. Use for the bundled quicksort example or any spec-plus-oracle coding task where a local model should do the writing under a frontier-authored test.
---

# claude-local — local models as test-first implementers

Hand a free local model a spec and a **failing test it may never edit**, and let a
deterministic loop drive it to a passing implementation. The test is the oracle: green means
done. This skill walks the bundled quicksort example end to end, then shows how to point the
same loop at your own task.

## What claude-local is (and is not)

- **A loop, not an agent.** It hands the model a rules card, the spec, and the immutable oracle
  test; applies the whole-file implementation the model returns to one permitted path; runs the
  test; and feeds any failure back — all under a hard token/attempt budget with a derail guard.
- **Test-first and supervised.** *You* (the orchestrator) author the oracle test; the model
  never writes or edits it. That is what makes green trustworthy — the model could not have
  changed the test to pass it.
- **Inference only.** claude-local never touches the network or the filesystem outside the one
  implementation path, and it never downloads or serves a model. It just infers against a
  server you point it at.

## Prerequisite: a running model server

claude-local talks to an already-running **OpenAI-compatible** server; standing one up is the
orchestrator's job, not claude-local's. Before running the example:

1. Put a model's weights under `models/` (downloads are explicit and user-initiated).
2. Start an OpenAI-compatible server for it — for MLX weights, for example, `mlx_lm.server`;
   any OpenAI-compatible server works. Note its base URL (e.g. `http://localhost:8080/v1`) and
   the model name it serves.

## Recipe — the quicksort example

1. **Read the task.** `examples/quicksort/spec.md` is the ticket;
   `examples/quicksort/quicksort_oracle.py` is the immutable oracle (hand-derived cases the model
   may never edit).
2. **Run the loop:**
   ```bash
   uv run python examples/quicksort/run.py --base-url http://localhost:8080/v1 --model <model-name>
   ```
   Or set `CLAUDE_LOCAL_BASE_URL` / `CLAUDE_LOCAL_MODEL` and drop the flags.
3. **Surface the result.** The produced implementation prints to stdout; the outcome and the
   local-economy summary (calls, completion tokens, decode seconds, tokens/second) print to
   stderr. Redirect stdout to capture just the code:
   ```bash
   uv run python examples/quicksort/run.py --model <model-name> > quicksort.py
   ```
4. **Confirm green.** A `Status.DONE` summary — "Implemented src/quicksort.py; the oracle test
   passed." — means the loop drove the local model to a passing implementation. Any other status
   (exhausted / derailed / blocked) means it did not converge within the budget; the summary
   says which.

## Adapt it to your own task

Copy the three files under `examples/quicksort/` and change:

- **`spec.md`** — your ticket: signature, behavior, constraints.
- **`quicksort_oracle.py`** → your oracle — a **failing** test whose expected values are
  hand-derived (never read from running an implementation), importing the impl from the nested
  `src/` subtree beside it. Name it `<task>_oracle.py`, **not** `test_*.py`: the impl it imports
  is materialized only inside the loop's worktree, so a `test_` name would make your own repo's
  pytest and CI try to collect it and fail on the missing import. The loop renames it internally
  regardless, so the repo filename is yours to choose.
- **`run.py`** — point `impl_path`, `expected_tests`, and the `Budget` at your task.

The one rule that makes the loop trustworthy: **the orchestrator writes the oracle test; the
local model never does.** Green is meaningful only because the model could not have edited the
test to reach it.
