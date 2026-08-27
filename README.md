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

**Claude Local** is the harness that makes local models useful anyway — and, just as
importantly, **measures whether they actually paid off**.

## The Idea in One Paragraph

Claude Code writes a failing test and a tight spec. A **deterministic loop** — not an agent —
hands both to a local model, applies the raw code it returns, runs the test, feeds the failure
back, and repeats under a hard budget. The test is the oracle: green means done. The
orchestrator never spends tokens *writing* the implementation — the free local model does. Every
task is metered, so the orchestrator can tell, per task class, whether the offload saved more
frontier tokens than it cost — and **switches itself off where it doesn't**.

## Quickstart

Claude Local *infers* — it never downloads or serves a model. A running OpenAI-compatible server
is a prerequisite you provide.

1. **Put a local model under `models/`.** Weights are git-ignored; downloads are explicit and
   user-initiated (see [`models/README.md`](models/README.md)).
2. **Serve it over an OpenAI-compatible HTTP API** — e.g. mlx-lm, llama.cpp's server, LM Studio,
   or vLLM. Note its base URL (the example defaults to `http://localhost:8080/v1`) and the model
   name it serves.
3. **Run the bundled example** — it drives one bounded red→green loop over a quicksort task with
   an immutable multi-case oracle:

   ```bash
   uv run python examples/quicksort/run.py --base-url http://localhost:8080/v1 --model <model-name>
   ```

   The produced implementation prints to stdout; the outcome and a local-economy line (calls,
   tokens, decode seconds, tokens/sec) print to stderr. Capture just the code with a redirect:

   ```bash
   uv run python examples/quicksort/run.py --model <model-name> > quicksort.py
   ```

Driving your own task is the same three inputs — an impl path, a spec, and an immutable oracle
test — handed to the one entry point:

```python
from claude_local import Budget, Status, TaskSpec, implement

spec = TaskSpec(
    impl_path="src/thing.py",  # the one file the model may write (must be nested)
    spec_text="<the ticket>",
    test_text="<a failing oracle test the model never sees as writable>",
    expected_tests=5,  # collected-node count the oracle must expose
    budget=Budget(max_attempts=5, max_tokens=4096, timeout_s=120.0),
)
outcome = implement(spec, base_url="http://localhost:8080/v1", model="<model-name>")
assert outcome.status is Status.DONE
print(outcome.code)  # the produced implementation
print(outcome.record)  # the local half of the economy record
```

See [`examples/README.md`](examples/README.md) and
[`skills/claude-local/SKILL.md`](skills/claude-local/SKILL.md) for the full plan →
author-oracle → implement-local → verify recipe.

## How It Works

```
distilled rules card + tight spec + FAILING TEST  (frontier-authored, immutable)
        |
        v
   local model  -->  a complete implementation file  (raw text, no tool calls)
        |
        v
   loop writes ONLY the permitted impl path
        |
        v
   run the frontier's test  --red-->  feedback to model   (repeat under token cap + derail guard)
        |
      green
        |
        v
   return an Outcome — status + produced code + local-economy record — to the orchestrator
```

Three deliberate choices make weak models usable:

- **Whole-file edits, not diffs.** The model returns the entire file; the loop writes it. Weak
  models reliably fail search/replace diff matching — so we never ask them to. With no edit-call
  protocol, the single failure mode that kills weak models inside agents simply cannot occur.
- **The orchestrator owns every test.** The local model never sees a writable test file — it
  *cannot* weaken the oracle, because it never touches it. Test immutability is enforced by the
  loop (it writes only the impl path), not requested politely.
- **Status comes from the oracle, not the model.** A weak model cannot be trusted to report
  "done." The loop decides: the frontier's test passes, or it does not.

## Does It Actually Pay? (The Measurement)

This is the whole question, and Claude Local answers it with data instead of hope. Every task
emits the **local half** of an economy record:

- **local tokens** produced + **wall-clock decode** + iterations (the *free* side and its cost)
- **decode rate** (tokens/sec) and whether any count was estimated
- **outcome** (done / exhausted / derailed / blocked), model, attempts

The driving orchestrator combines that with its own frontier-token accounting — the *paid* side:
spec + test + feedback + review — to derive, per task class, the **net frontier tokens saved**
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
- **Stable-prefix prompting** — card + spec first, only the test/feedback tail changes, so the
  prefill is KV-cache-reused across iterations.
- **One model resident at a time** — local inference is memory-bandwidth-bound.
- **Derail guard** — repetition penalty + hard token cap + repetition-loop detector + graceful
  timeout, streaming so a runaway is aborted mid-generation.

## Where It Fits

Claude Local is a Python library with a single public entry point — `implement()` — meant to be
driven by a frontier orchestrator that can author a failing test. **Claude Code is the intended
driver:** a bundled skill (`skills/claude-local/`) teaches it the plan → author-oracle →
implement-local → verify recipe. Any orchestrator that can write a failing test and a tight spec
can drive the same entry point.

It does exactly one thing — infer against an already-running OpenAI-compatible server. It does
not download models and it does not serve them; that stays the orchestrator's (or your) job, so
the loop is serving-agnostic. The runtime dependency is `httpx`, nothing more.

## License

MIT (c) 2026 axdel
