# claude-local — project notes

Free local models as supervised, test-first code implementers, driven by a deterministic
red→green loop. See [README.md](README.md) for the full design — whole-file edits, the
orchestrator-owned immutable oracle test, the derail guard, and the measurement/economy story.

## What this is

A deterministic loop — not an agent — hands a local model a distilled rules card, a tight
spec, any optional ordered, read-only neighbor files, and a frontier-authored **failing test the
model may never write**. The model returns a complete implementation file as raw text; the loop
applies it to the one permitted impl path — never a context file — runs the test, and feeds the
failure back under a hard token budget and a derail guard. The test is the oracle: green means
done — against a model that is wrong, not one that is hostile, because the verdict is computed in
the same process that runs the model's file (D-ORACLE-004, D-ORACLE-006). Every task is metered,
so the system can tell — per task
class — whether offloading to a free local model saved net frontier tokens.

## Stack

- **Python 3.12+**, src-layout: package `claude_local` under `src/`, tests in `tests/`.
- **Runtime dependency: `httpx`** — the loop talks to a local, OpenAI-compatible model server
  over HTTP (one model resident at a time; local inference is memory-bandwidth-bound).
- **Local MLX models** resolve from `models/` — the single model store; weights are git-ignored
  and downloads are explicit and user-initiated (the loop never pulls a model on its own).
- **Tooling:** `uv` (runner), `pytest`, `ruff`, `basedpyright`.

## Commands

- Test: `uv run --group bench pytest`
- Lint / format: `uv run ruff check` · `uv run ruff format`
- Types: `uv run basedpyright`
- Commit gate (pre-commit): `lefthook run pre-commit` — the registry's `pre-commit-fast` phase
  (ruff lint + format + gitleaks) plus this project's own basedpyright and bench-aware pytest
- Branch review / dependency metrics: prefix with `PYTHONPATH=src` —
  `PYTHONPATH=src claude-protocol quality run --phase branch-review`. The dependency-metrics
  gate imports `claude_local` **in claude-protocol's own interpreter**, whose `sys.path` never
  includes the working directory, so without the prefix it reports the package as absent.

## Architecture overview

The loop engine decomposes into single-responsibility modules, dependencies flowing one way
(orchestration → adapters → stdlib/external):

- **model client** — the httpx call to the local server; captures token usage and wall-clock timing.
- **edit applier** — extracts the whole-file reply from raw model text and writes ONLY the
  permitted impl path; the oracle test is never in the model's writable set.
- **loop** — RED (run the immutable test) → REPAIR feedback on failure → best-passing snapshot →
  GREEN, bounded by a hard token/attempt budget.
- **derail guard** — repetition penalty + hard token cap + repetition-loop detector + graceful
  timeout. Thinking is not defaulted here or anywhere: each registry row declares its own controls,
  because one field zeroes one model's reasoning and is inert on another (D-THINKING-001).
- **rules card** — a static, token-budgeted engineering-rules card injected as a stable system
  prefix (byte-identical across calls, so the prefill is KV-cache-reused).
- **oracle sandbox** — the kernel profile the immutable test runs under: deny-by-default, with a
  tight per-task deadline distinct from the generous one bounding decode.
- **telemetry** — a run-scoped writer for the **local half** of the per-task economy record.

Serving and the front doors sit above that engine, and the loop depends on none of them:

- **model registry** — resolves a name against its curated rows and the on-disk store. A
  registered model with no weights is refused, never fetched.
- **model server** — spawns a resolved model on 127.0.0.1, waits until it answers, and guarantees
  teardown on success and failure alike. Opt-in (`uv sync --group serve`); the loop never calls it.
- **model session** — the interactive surface: one `with` block yields a callable that serves a model,
  meters each turn, and reaps the process afterwards.
- **cli** — the machine front door claude-protocol dispatches to, taking one JSON envelope on
  stdin. Its contract version is the cross-repo handshake.

The **loop** has one entry point, `implement()` — it owns the whole red→green cycle behind that
single typed seam (see README). The CLI and the model session above are front doors too; neither runs
the loop, which is why one seam still holds (D-ENTRYPOINT-004). One thing stays **external to
claude-local**: the **orchestrator half** of the
economy record — the frontier-token accounting and the net-savings verdict that decide whether
offloading a task actually paid off. claude-local writes only the **local half** (what it produced
and burned); the driving orchestrator (Claude Code) owns the comparison.

## Performance & inference efficiency — a first-class requirement

Speed is a correctness-tier concern here, not finishing polish: a local model pays off only if
the loop wrings maximum useful work from every token and every second of decode. Engineer the
**inference hot path** — prefix construction, the generation call, derail detection, the
per-attempt loop — as the place where that is won or lost, and back every optimization with the
loop's own telemetry (measure, never guess; cold paths like init and record-writing stay simple).

Standing hot-path principles:

- **KV-cache prefix reuse.** The stable prefix (rules card + spec + optional ordered context
  files + immutable test) is byte-identical across a task's attempts — only the feedback tail
  changes. Stability is a hard invariant: any per-call mutation silently discards the server's
  prefill cache.
- **One warm client, one resident model.** Reuse a single keep-alive httpx client; never
  reconnect per attempt. Local inference is memory-bandwidth-bound — keep one model resident.
- **Stream and abort early.** Consume tokens as they decode, so the derail guard kills a
  repetition loop or budget overrun mid-generation — not after a full wasted completion.
- **Bounded, right-typed hot-path structures.** Repetition detection over a fixed ring buffer
  (`deque(maxlen=)`), membership via `set`, no accidental O(n²) in the loop body.
- **Bounded decode by construction** — hard token, attempt and wall-clock caps (the derail guard).
  Thinking is a per-model registry declaration, never a cross-model default (D-THINKING-001).

## Architecture Primitives

Build by CITE / REFERENCE / DERIVE from these — never restate a fact an owner already holds:

| Primitive | File | Owns |
|-|-|-|
| Canonical Glossary | [`CANONICAL_GLOSSARY.md`](CANONICAL_GLOSSARY.md) | One name per concept, across every layer |
| Boundary Map | [`BOUNDARY_MAP.md`](BOUNDARY_MAP.md) | Allowed import directions |
| Derivation Map | [`DERIVATION_MAP.md`](DERIVATION_MAP.md) | Source-of-truth chain for derived artifacts |
| Resource Ownership | [`RESOURCE_OWNERSHIP.md`](RESOURCE_OWNERSHIP.md) | Single writer per shared resource |
| Decisions | [`DECISIONS.md`](DECISIONS.md) | Rationale for non-obvious / irreversible choices |
| Cross-Cutting Invariants | [`INVARIANTS.md`](INVARIANTS.md) | Invariants indexed to their owner |
| Memory Governance | [`MEMORY_GOVERNANCE.md`](MEMORY_GOVERNANCE.md) | Authority + trust tier of each memory surface |

## Key Decisions

See [`DECISIONS.md`](DECISIONS.md) for the full decision registry.

## Known Tech Debt

See [`TECH_DEBT.md`](TECH_DEBT.md) for the tech-debt ledger.

## Resource Ownership

See [`RESOURCE_OWNERSHIP.md`](RESOURCE_OWNERSHIP.md) for the single-writer registry.
