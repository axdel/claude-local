# Model-evaluation benchmark

A standing, reusable instrument for judging how a local model performs as an implementer under
claude-local's `implement()` loop. It is a fixed ladder of implementation tasks against one
correctly-architected reference application; drive any candidate model through the whole ladder and
score its output against per-task correctness oracles to get a single, comparable scorecard. The
same instrument runs against every model, so results are directly comparable and "add a model,
bench it" is one command.

## What it measures

Real engineering skill, not toy parsing. The reference app is a small but properly layered
schedule manager — HTTP routers over role-aware services over a typed persistence boundary, with
strict schema validation, token-based authentication, and role-based access control. Each rung
asks the model to implement one layer of that app correctly, in place, against the surrounding
code. A model that can only pattern-match snippets fails the rungs that require honoring an
existing contract, delegating to a neighbor, or enforcing an authorization rule.

## The design: fill one hole

Every rung is one **hole** in an otherwise complete, working golden tree:

- The golden app is assembled in a disposable worktree with exactly one file replaced by a blank
  stub — the file the model must write.
- The model is handed the task **spec**, the **neighbor files** it must integrate with (its
  context), and a frontier-authored **oracle test** it must make pass. It may read the oracle but
  never edit it — the loop applies the model's whole-file output only to the one blanked path, so
  the oracle is never in the model's writable set.
- The rung **passes** when that immutable oracle goes green. The oracle is both the target and the
  answer key: because the model cannot modify it, passing it cannot be gamed.

Each rung is fail-on-blank, pass-on-golden by construction: the blank stub fails the oracle, and
dropping the golden file back in passes it. A rung that could only pass by leaking the answer into
its spec or context is not a valid rung.

Every rung runs in its own throwaway worktree with a fresh in-memory database, torn down before
the next rung starts, so no rung can observe another's files or rows.

## The case ladder

Seven rungs, in dependency order — each builds on the layers below it:

| Rung | Hole | What it exercises |
|-|-|-|
| `01_scaffold` | `app/main.py` | Application factory, lifespan, health endpoint |
| `02_schemas` | `app/schemas.py` | Strict Pydantic v2 request/response models shared across the app |
| `03_repositories` | `app/repositories/schedule_repository.py` | Typed persistence boundary owning all schedule SQL over one connection |
| `04_auth_service` | `app/services/auth_service.py` | Registration, password verification, signed identity tokens |
| `05_rbac` | `app/security.py` | Resolving a token to the current user and enforcing the admin role |
| `06_schedule_service` | `app/services/schedule_service.py` | Role-aware schedule CRUD, ownership authorization, derived cron behavior |
| `07_routers` | `app/routers/schedules.py` | HTTP router: pure delegation, ownership/role scoping, schema projection |

## Setup

The reference app's dependencies (FastAPI, Pydantic) are an opt-in dependency group, never a
runtime dependency of claude-local. Sync them once:

```bash
uv sync --group bench
```

## Running a model

A running server is a **prerequisite**: claude-local never downloads or serves a model. Start an
OpenAI-compatible server with your candidate model resident, then point the benchmark at it. Run
from the repository root:

```bash
uv run python -m benchmarks.run --model <model-name> --base-url http://localhost:8080/v1
```

| Flag | Env fallback | Meaning |
|-|-|-|
| `--model` | `CLAUDE_LOCAL_MODEL` | Model name the server should serve (required) |
| `--base-url` | `CLAUDE_LOCAL_BASE_URL` | OpenAI-compatible server base URL (default `http://localhost:8080/v1`) |
| `--out DIR` | — | Also write the scorecard as JSON into `DIR` (created if absent) |

The per-rung table and suite totals print to stderr. The process exits `0` only when every rung
passed, `1` when any rung failed, and `2` for a usage error (no model named).

## The scorecard

One scorecard describes one model's run over the whole ladder. `--out` writes it as
`scorecard-<model>-<timestamp>.json`:

```json
{
  "model": "candidate-7b",
  "rungs_passed": 5,
  "rungs_total": 7,
  "total_completion_tokens": 18432,
  "total_model_seconds": 240.5,
  "mean_tokens_per_second": 76.6,
  "rungs": [
    {"case_id": "01_scaffold", "status": "done", "attempts": 1},
    {"case_id": "02_schemas", "status": "done", "attempts": 2}
  ]
}
```

- `rungs_passed` / `rungs_total` — the headline: how many rungs reached `done`.
- `total_completion_tokens` / `total_model_seconds` / `mean_tokens_per_second` — the economy of the
  run, summed across rungs. The mean is `null` when no model-seconds elapsed.
- `rungs[]` — one line per rung in ladder order: its `case_id`, terminal `status` (`done` when the
  oracle passed, otherwise the loop's failure status), and how many loop `attempts` it took.

The token and decode totals are the **local** half of the economy story — what the model produced
and burned. Whether offloading a given rung to a local model actually saved net orchestrator
tokens is a separate comparison the driving orchestrator owns; the benchmark reports the local
half so that comparison can be made.

## Adding a model

1. Bring up an OpenAI-compatible server with the model resident (claude-local does not do this for
   you — model provisioning is explicit and user-initiated).
2. `uv run python -m benchmarks.run --model <name> --base-url <url> --out scorecards/`.
3. Compare the resulting scorecard against other models' scorecards in the same directory.

Nothing about the suite changes between models — that is the point.

## How the harness works

The harness lives in `benchmarks/harness/`:

- **`loader`** — reads a case directory (`case.toml` manifest + `spec.md` + `oracle.py` +
  `blank/<impl_path>`) into a validated `BenchmarkCase`; `load_suite` loads every rung under a
  cases root into a name-keyed suite in ladder order.
- **`driver`** — assembles one case's golden tree in a disposable worktree, blanks its one hole,
  runs it through `claude_local.implement`, and tears the worktree down; `run_suite` maps the whole
  suite in order, returning one `CaseResult` per rung.
- **`scorer`** — reduces a suite's `CaseResult` list to one comparable `Scorecard` and writes it
  as JSON.

`benchmarks/run.py` is the thin command-line entry point wiring those three together.
