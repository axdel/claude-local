# Examples

Runnable demonstrations of claude-local's `implement()` entry point.

## quicksort

A complete turnkey task: a tight spec, an immutable multi-case oracle test, and a `run.py`
that composes a `TaskSpec` and drives one bounded red→green loop.

- `quicksort/spec.md` — the ticket handed to the model.
- `quicksort/quicksort_oracle.py` — the immutable oracle (hand-derived cases; the model may never
  edit it). Green means done. Named `*_oracle.py`, not `test_*.py`, so the repo's own pytest does
  not try to collect it — the impl it imports lives only inside the loop's worktree.
- `quicksort/run.py` — reads the two files above, composes a `TaskSpec` + `Budget`, calls
  `implement()`, and prints the produced code (stdout) plus the outcome and local-economy
  summary (stderr).

### Prerequisite

An already-running OpenAI-compatible server for a model under `models/`. claude-local infers
against it over HTTP; it never downloads or serves a model itself.

### Run it

```bash
uv run python examples/quicksort/run.py --base-url http://localhost:8080 --model <model-name>
```

Capture just the produced code with a redirect:

```bash
uv run python examples/quicksort/run.py --model <model-name> > quicksort.py
```

See [`../skills/claude-local/SKILL.md`](../skills/claude-local/SKILL.md) for the full
plan → test → implement-local → verify recipe.
