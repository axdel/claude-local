# Model store

The single, controlled location where claude-local keeps local MLX models — never a scattered
cache elsewhere. `CLAUDE_LOCAL_MODELS` overrides the path, which is how a worktree reaches the
weights in the main checkout without a second copy.

Two things live here, and they answer different questions:

- **The store** is what is on disk — one directory of weights per model, discovered at runtime.
  It answers "is this model *present*?"
- **The registry** is [`models.tsv`](models.tsv) — a curated, tracked catalogue naming each
  servable model and how to serve it (port, server flags, optional draft model). It answers
  "is this model *known*, and what does serving it take?"

Neither derives from the other, which is the point: a catalogued model with no weights is
refused (`ModelNotPresent`) rather than fetched, and weights present under a name nobody
catalogued are simply not servable. **Downloads are explicit and user-initiated — claude-local
never pulls a model on its own.**

Weights are git-ignored (large binaries, never committed). Only this file and `models.tsv` are
tracked, which also keeps the directory in the repo.
