# claude-local — project notes

Free local models as supervised, test-first code implementers, driven by a deterministic
red→green loop. See [README.md](README.md) for the full design — whole-file edits, the
orchestrator-owned immutable oracle test, the derail guard, and the measurement/economy story.

## What this is

A deterministic loop — not an agent — hands a local model a distilled rules card, a tight
spec, and a frontier-authored **failing test the model may never write**. The model returns a
complete implementation file as raw text; the loop applies it to the one permitted impl path,
runs the test, and feeds the failure back under a hard token budget and a derail guard. The
test is the oracle: green means done. Every task is metered, so the system can tell — per task
class — whether offloading to a free local model saved net frontier tokens.

## Stack

- **Python 3.12+**, src-layout: package `claude_local` under `src/`, tests in `tests/`.
- **Runtime dependency: `httpx`** — the loop talks to a local, OpenAI-compatible model server
  over HTTP (one model resident at a time; local inference is memory-bandwidth-bound).
- **Local MLX models** resolve from `models/` — the single model store; weights are git-ignored
  and downloads are explicit and user-initiated (the loop never pulls a model on its own).
- **Tooling:** `uv` (runner), `pytest`, `ruff`, `basedpyright`.

## Commands

- Test: `uv run pytest`
- Lint / format: `uv run ruff check` · `uv run ruff format`
- Types: `uv run basedpyright`
- Commit quality gate: `claude-protocol quality gate`

## Architecture overview

The loop engine decomposes into single-responsibility modules, dependencies flowing one way
(orchestration → adapters → stdlib/external):

- **model client** — the httpx call to the local server; captures token usage and wall-clock timing.
- **edit applier** — extracts the whole-file blocks from raw model text and writes ONLY the
  permitted impl path; the oracle test is never in the model's writable set.
- **loop** — RED (run the immutable test) → REPAIR feedback on failure → best-passing snapshot →
  GREEN, bounded by a hard token/attempt budget.
- **derail guard** — repetition penalty + hard token cap + repetition-loop detector + graceful
  timeout; non-thinking by default with a hard thinking cap.
- **rules card** — a static, token-budgeted engineering-rules card injected as a stable system
  prefix (byte-identical across calls, so the prefill is KV-cache-reused).
- **telemetry** — a run-scoped writer for the **local half** of the per-task economy record.

Two seams are **external, owned by claude-protocol**, and are consumed here as stubs: the
**builder-adapter contract** (`build(task_spec, worktree, context_tier) -> {status, files_changed,
notes, telemetry}`) and the **orchestrator half** of the economy record. claude-local implements
the loop behind the contract and writes only the local half.

## Performance & inference efficiency — a first-class requirement

Speed is a correctness-tier concern here, not finishing polish: a local model pays off only if
the loop wrings maximum useful work from every token and every second of decode. Engineer the
**inference hot path** — prefix construction, the generation call, derail detection, the
per-iteration loop — for peak throughput, and back every optimization with the loop's own
telemetry (measure, never guess; cold paths like init and record-writing stay simple).

Standing hot-path principles:

- **KV-cache prefix reuse.** The system prefix (rules card + spec) is byte-identical across a
  task's iterations — only the test/feedback tail changes. Stability is a hard invariant: any
  per-call mutation silently discards the server's prefill cache.
- **One warm client, one resident model.** Reuse a single keep-alive httpx client; never
  reconnect per iteration. Local inference is memory-bandwidth-bound — keep one model resident.
- **Stream and abort early.** Consume tokens as they decode, so the derail guard kills a
  repetition loop or budget overrun mid-generation — not after a full wasted completion.
- **Bounded, right-typed hot-path structures.** Repetition detection over a fixed ring buffer
  (`deque(maxlen=)`), membership via `set`, no accidental O(n²) in the loop body.
- **Non-thinking default with hard caps** — bounded decode by construction (the derail guard).

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

## Roadmap — to spike later

### Standing model-evaluation benchmark

A reusable instrument for judging how any new or candidate local model performs as an
implementer under the claude-local loop: a deliberately half-finished project shipped with a
detailed plan and a hidden correctness oracle. Drop a candidate model in, drive it through the
plan's tasks, and score its output against the oracle — the same instrument across every model,
so results are directly comparable.

An earlier internal study — a real parser feature with a hidden multi-case oracle, run through a
deterministic driver across several local models — seeded this idea and confirmed the loop works.
The work is to generalize it from a one-off study into a standing, repeatable benchmark that
ships with claude-local.

Status: deferred — the current focus is robust code around the loop engine itself. **To spike.**
