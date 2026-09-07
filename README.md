# Claude Local

**Put your free local models to work — as measured, test-first code implementers.**

A deterministic red→green loop that drives a local model to make a frontier-authored failing
test pass. One public entry point — `implement()` — and a runnable example you can drive today.

## Why This Exists

Frontier models write excellent code, but every output token costs money and burns context.
Local models (Qwen, gpt-oss, gemma, …) are free and run on hardware you already own — but on
their own they are unreliable. Point a full coding agent at one and it stalls for minutes,
mangles its own tool calls, and derails. Ask it to write its *own* tests and it writes bad
ones, then greenlights its own bugs.

The load-bearing conclusion: a local model **can** implement a well-specified change correctly
— but only as a *junior implementer* that needs a precise ticket, a test it must satisfy, and a
senior reviewer. It can't drive an agent, and it can't author its own oracle. That single
constraint shapes everything here.

It is a measured conclusion, not a hunch: [`docs/prior-model-study.md`](docs/prior-model-study.md)
archives the controlled sweep it came from — several local models implementing one non-trivial
feature through one deterministic driver against one hidden oracle. Read it as background and
re-measure before quoting it; it ran against a different harness, at n=1 per cell.

**Claude Local** is the harness that makes local models useful anyway — and, just as
importantly, **measures whether they actually paid off**.

## The Idea in One Paragraph

Claude Code writes a failing test and a tight spec, optionally attaching ordered, read-only
neighbor files the implementation must integrate with. A **deterministic loop** — not an agent —
hands that task context to a local model, applies the raw code it returns, runs the test, feeds
the failure back, and repeats under a hard budget. The test is the oracle: green means done. The
orchestrator never spends tokens *writing* the implementation — the free local model does. Every
task is metered, so the orchestrator can tell, per task class, whether the offload saved more
frontier tokens than it cost — and **switches itself off where it doesn't**.

## Quickstart

Claude Local never downloads a model, and the **loop** never serves one: `implement()` takes a
`base_url` and infers against whatever is already listening. Serving is a separate, optional
capability that claude-local *does* own — `model_server` spawns a registered model and guarantees
teardown (see [Where It Fits](#where-it-fits)) — so step 2 is a choice, not a prerequisite you
must satisfy elsewhere.

1. **Put a local model under `models/`.** Weights are git-ignored; downloads are explicit and
   user-initiated (see [`models/README.md`](models/README.md)).
2. **Serve it over an OpenAI-compatible HTTP API.** Either bring your own — mlx-lm, llama.cpp's
   server, LM Studio, vLLM — or let claude-local run one for a registered model
   (`uv sync --group serve`, then `scripts/benchmark_model.py <name>`, which spawns the server,
   runs the work, and tears it down on success and failure alike). Note the base URL and the
   model name it serves — neither is defaulted, because a guessed port reaches whatever
   happens to be listening.
3. **Run the bundled example** — it drives one bounded red→green loop over a quicksort task with
   an immutable multi-case oracle:

   ```bash
   uv run python examples/quicksort/run.py --base-url http://localhost:8081 --model <model-name>
   ```

   The produced implementation prints to stdout; the outcome and a local-economy line (calls,
   tokens, decode seconds, tokens/sec) print to stderr. Capture just the code with a redirect:

   ```bash
   uv run python examples/quicksort/run.py --model <model-name> > quicksort.py
   ```

   **Pass the model's `PARAMS` if its registry row declares any**, as `--generation-params`
   (a JSON object). They are not decoration: several models answer with a file only once their
   thinking channel is switched off in the request, and a server flag cannot do it — the flag's
   absence leaves the template variable undefined, which the template reads as *on*. Serving a
   model with its `FLAGS` and none of its `PARAMS` is a half-configured run that looks fully
   configured, and it fails as a `BLOCKED` with nothing to repair from.

   ```bash
   uv run python examples/quicksort/run.py --model <model-name> \
     --generation-params '{"enable_thinking": false, "thinking_budget": 256}'
   ```

   Steps 2 and 3 together, for a registered model, are one command that reads the row for you and
   tears the server down either way:

   ```bash
   scripts/e2e_local_model.py <model-name>
   ```

### Talking to a model interactively

To try prompts against a registered model — tuning a rules card, checking how one answers before
spending a benchmark on it — `model_session` collapses steps 1 and 2 into a `with` block. It
resolves the name in the store, spawns the server, waits for it to answer, and guarantees the
model is gone when the block ends, including when it ends by exception:

```python
from claude_local import model_session

with model_session("gpt-oss-20b") as chat:
    print(chat("write a haiku about static types"))
    print(chat.last.tokens_per_second)  # the turn's metering, not just its text
```

One keep-alive client serves the whole session, and holding the `system=` prefix identical across
turns keeps the server's prefill cache warm. This needs `uv sync --group serve`; the loop itself
does not.

### Driving your own task

Driving your own task hands one contract to the entry point: an impl path, a spec, an immutable
oracle test, its expected test count, and a budget. When the implementation must integrate with
existing code, add any neighbor files as optional, ordered, read-only context:

```python
from claude_local import Budget, ContextFile, Status, TaskSpec, implement

spec = TaskSpec(
    impl_path="src/thing.py",  # the one file the model may write (must be nested)
    spec_text="<the ticket>",
    test_text="<a failing oracle test the model never sees as writable>",
    expected_tests=5,  # collected-node count the oracle must expose
    budget=Budget(
        max_attempts=5,
        max_tokens=4096,
        generation_timeout_s=1200.0,  # generous: a slow model producing steadily is healthy
        oracle_timeout_s=120.0,  # tight: nothing legitimate makes a test suite slow
    ),
    context_files=(ContextFile(path="src/protocol.py", content="<existing neighbor source>"),),
)
outcome = implement(spec, base_url="http://localhost:8081", model="<model-name>")
assert outcome.status is Status.DONE
print(outcome.code)  # the produced implementation
print(outcome.record)  # the local half of the economy record
```

See [`examples/README.md`](examples/README.md) and
[`skills/claude-local/SKILL.md`](skills/claude-local/SKILL.md) for the full plan →
author-oracle → implement-local → verify recipe.

## How It Works

```
distilled rules card + tight spec + optional ordered read-only context + FAILING TEST
        |                                                         (frontier-authored, immutable)
        v
   local model  -->  a complete implementation file  (raw text, no tool calls)
        |
        v
   loop writes ONLY the permitted impl path (never the context files)
        |
        v
   run the frontier's test  --red-->  its file + the failure   (repeat under cap + derail guard)
        |
      green
        |
        v
   return an Outcome — status + produced code + local-economy record — to the orchestrator
```

Three deliberate choices make weak models usable:

- **Whole-file edits, not diffs.** The model returns the entire file; the loop writes it. Weak
  models reliably fail search/replace diff matching — so we never ask them to. With no edit-call
  protocol, the single failure mode that kills weak models inside agents simply cannot occur. The
  one thing the loop does *not* take literally is a markdown fence wrapping the payload end to
  end: models emit one whatever the prompt asks, and written through it makes the file uncompilable,
  so a scorecard would grade the wrapper instead of the model. Both ends are required, so a fence
  belonging to the source survives untouched.
- **The orchestrator owns every test.** The local model never sees a writable test file — it
  *cannot* weaken the oracle, because it never touches it. Test immutability is enforced by the
  loop (it writes only the impl path), not requested politely.
- **Status comes from the oracle, not the model.** A weak model cannot be trusted to report
  "done." The loop decides: the frontier's test passes, or it does not.
- **That guarantee is scoped, and the scope is worth knowing.** The oracle defeats a model that is
  *wrong*; it does not defeat one that is *hostile*. The verdict is computed inside the same
  process that imports and executes the model's file, so top-level code in that file could forge a
  green — and the registry carries safety-ablated rows, the model class for which "not hostile" is
  the weakest assumption. Making the verdict adversary-proof means computing it somewhere the impl
  cannot reach, which is a different design; this one takes the trade knowingly and says so, on
  stderr at every green and in `D-ORACLE-004` / `D-ORACLE-006`. Treat a local green as *supervised
  evidence a test passed*, never as a substitute for reading the diff.
- **The same scope bounds what the sandbox protects.** Kernel confinement denies the network, the
  ambient host filesystem, and unscoped Mach IPC — but the worktree itself is readable, because the
  implementation has to import its neighbours, and the run's diagnostics tail is fed back into the
  next prompt, because that is how the loop tells the model what failed. Together those two
  necessary halves are a read-and-return path: `(deny network*)` stops the child sending anything
  out, but the *parent* carries what it read into the next prompt and thence into the file it
  admits. Under dispatch that worktree is a working copy of your repository. Point claude-local at
  a checkout you would be willing to show the model — not one holding live credentials. Pinned by
  tests either side of the boundary and stated in `D-SANDBOX-010`.

## Does It Actually Pay? (The Measurement)

This is the whole question, and Claude Local answers it with data instead of hope. Every task
emits the **local half** of an economy record:

- **local tokens** produced + **wall-clock decode** + attempts (the *free* side and its cost)
- **decode rate** (tokens/sec) and whether any count was estimated
- **outcome** (done / exhausted / derailed / blocked), model, attempts

The driving orchestrator combines that with its own frontier-token accounting — the *paid* side:
spec + optional context + test + feedback + review — to derive, per task class, the **net frontier tokens saved**
and the **time multiplier**, and to route a class back to the frontier when the offload stops
paying. The sweet spot is narrow and counterintuitive: *high-volume but low-reasoning,
tightly-specifiable* work (mappers, serializers, CRUD, config, repetitive transforms), where the
implementation is large enough to be worth offloading yet simple enough for a weak model to get
right. The measurement exists to find that band empirically rather than guess it.

### Built for speed

A weak model is only worth using if it is fast enough to be cheaper than your own attention.

- **Tuned for MoE / fast local models** — fastest decode, least derail.
- **Non-thinking generation by default, hard thinking cap** — the derail guard bounds decode by
  construction.
- **Stable-prefix prompting** — card + spec + optional ordered context files + test stay fixed;
  only the tail changes, so the prefill is KV-cache-reused across attempts.
- **The tail is a repair brief, not a bug report** — it carries the complete file the last attempt
  wrote alongside the failure that file produced, because the card asks the model to correct its
  file and keep what already passed, and neither is possible against code it cannot see.
- **One model resident at a time** — local inference is memory-bandwidth-bound.
- **Derail guard** — repetition penalty + hard token cap + repetition-loop detector + graceful
  timeout, streaming so a runaway is aborted mid-generation.
- **Stall detection across attempts, answered by a nudge ladder** — an attempt that buys nothing
  proves the prompt is an absorbing state, and it has two shapes. The loud one is a *repeat*:
  byte-identical text, so under greedy decoding re-asking is arithmetic, not patience. The quiet
  one — and the common one — is a *plateau*: genuinely different code, twice running, that still
  never clears the best score. A loop watching only for identical text spends its whole budget
  re-deriving one wrong answer in fresh words. Both take the next rung of a fixed ladder, appended
  last in the tail and led by the first failure's own executed counterevidence (the statement
  pytest marked and the result beneath it); the run ends when the ladder is spent. Because one
  ladder answers both, no rung claims the file came back identical — that is false of a plateau,
  and a model handed a false premise about its own output argues with it instead of fixing the
  code. This is the only lever the runtime has: the server ignores temperature, top-p and seed,
  and emptying the tail just replays the first attempt.

## Where It Fits

Claude Local is a Python library with a single public entry point — `implement()` — meant to be
driven by a frontier orchestrator that can author a failing test. **Claude Code is the intended
driver:** a bundled skill (`skills/claude-local/`) teaches it the plan → author-oracle →
implement-local → verify recipe. Any orchestrator that can write a failing test and a tight spec
can drive the same entry point.

The loop does exactly one thing — infer against an already-listening OpenAI-compatible server.
It is serving-agnostic by construction: `implement()` takes a `base_url` and never starts
anything, so its only runtime dependency is `httpx`.

Serving is a separate, optional capability. `uv sync --group serve` installs the MLX stack, and
`model_server` then spawns a server for a registered model and guarantees it is torn down
afterwards — a model is resident only while something is using it. That group is deliberately
not a runtime dependency: MLX is Apple-silicon only, and the loop must install anywhere.

**Nothing here ever downloads a model.** Models are named by their path in the local store, so a
name that is registered but not pulled is refused rather than fetched; pulling weights stays an
explicit, user-initiated act.

### Dispatching to it from claude-protocol

claude-protocol can route a task here as its `local` implementer. That takes two things, and it
refuses to dispatch until both hold.

Put the CLI on `PATH`:

```bash
uv tool install --from . claude-local
claude-local --contract-version   # → claude-local/1
```

Then declare it in the consuming project's `.claude-protocol.toml`:

```toml
[implementers]
enabled = ["claude", "local"]
```

Availability and authorization are deliberately separate: resolving on `PATH` only makes the
backend *visible*, and a task is dispatched here only when the config above also names it. On top
of that, claude-protocol probes `claude-local --contract-version` and compares the output to its
own `LOCAL_CONTRACT_VERSION` — a mismatch fails closed, so a version skew drops claude-local from
the ready set instead of sending it an envelope it would reject. Sensitive work never arrives at
all: the ceremony classifier withholds `untrusted_implementer_allowed` for auth, payments, PII,
migrations, and for any change it cannot confidently classify.

## License

MIT (c) 2026 axdel
