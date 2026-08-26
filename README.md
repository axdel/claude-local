# Claude Local

**Put your free local models to work — as measured, test-first code implementers.**

> **Status: early / design stage.** The architecture below is settled and recorded;
> the implementation has not landed yet. This repository reserves the name and the
> design. Not yet usable — watch this space.

## Why This Exists

Frontier models write excellent code, but every output token costs money and burns
context. Local models (Qwen, gpt-oss, gemma, …) are free and run on hardware you already
own — but on their own they are unreliable. Point a full coding agent at one and it stalls
for minutes, mangles its own tool calls, and derails. Ask it to write its *own* tests and
it writes bad ones, then greenlights its own bugs.

Benchmarking a range of 20–30B local models surfaced one load-bearing conclusion: a local
model **can** implement a well-specified change correctly — but only as a *junior
implementer* that needs a precise ticket, a test it must satisfy, and a senior reviewer.
It cannot drive an agent, and it cannot author its own oracle.

**Claude Local** is the harness that makes them useful anyway — and, just as importantly,
**measures whether they actually paid off**.

## The Idea in One Paragraph

A frontier orchestrator (claude-protocol) writes a failing test and a tight spec. A
**deterministic loop** — not an agent — hands both to a local model, applies the raw code
it returns, runs the test, feeds the failure back, and repeats under a hard budget. The
test is the oracle: green means done. The orchestrator never spends tokens *writing* the
implementation — the free local model does. Every task is metered, so the system knows,
per task class, whether the offload saved more frontier tokens than it cost — and
**switches itself off where it doesn't**.

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
   hand back to the orchestrator's quality gates  +  emit a telemetry record
```

Three deliberate choices make weak models usable:

- **Whole-file edits, not diffs.** The model returns the entire file; the loop writes it.
  Weak models reliably fail search/replace diff matching — so we never ask them to. With no
  edit-call protocol, the single failure mode that kills weak models inside agents simply
  cannot occur.
- **The orchestrator owns every test.** The local model never sees a writable test file —
  it *cannot* weaken the oracle, because it never touches it. Test immutability is enforced
  by the loop (it writes only the impl path), not requested politely.
- **Status comes from the oracle, not the model.** A weak model cannot be trusted to report
  "done." The loop decides: the frontier's test passes, or it does not.

## Does It Actually Pay? (The Measurement)

This is the whole question, and Claude Local answers it with data instead of hope. Every
offloaded task emits a record:

- **orchestrator tokens** spent — spec + test + feedback + review (the *paid* side)
- **local tokens** produced + **wall-clock** + iterations (the *free* side and its time cost)
- **outcome** (pass / fail / blocked), task class, model, edit format

From these it derives, per task class, the **net frontier tokens saved** and the **time
multiplier** — and a self-correcting gate **demotes any class back to the frontier when the
offload stops paying**. The sweet spot is narrow and counterintuitive: *high-volume but
low-reasoning, tightly-specifiable* work (mappers, serializers, CRUD, config, repetitive
transforms), where the implementation is large enough to be worth offloading yet simple
enough for a weak model to get right. Claude Local finds that band empirically rather than
guessing it.

### Built for speed

A weak model is only worth using if it is fast enough to be cheaper than your own attention.

- **MoE + non-thinking models by default** — fastest decode, least derail.
- **Stable-prefix prompting** — card + spec first, only the test/feedback tail changes, so
  the prefill is KV-cache-reused across iterations.
- **One model resident at a time** — local inference is memory-bandwidth-bound.
- **Derail guard** — repetition penalty + hard token cap + repetition-loop detector.

## Where It Fits

Part of the [claude-protocol](https://github.com/axdel/claude-protocol) ecosystem, which
routes each task to a backend and owns the quality gates. Claude Local implements the same
*builder contract* as the in-harness Claude builder — the same status vocabulary in, gated
code out — so everything downstream (mutation testing, commit, task FSM) treats a
local-model result identically to a frontier one.

| Repo | Runs | Through |
|-|-|-|
| **claude-protocol** | — | routes each task, owns the review gates (the senior reviewer) |
| **[claude-bridge](https://github.com/axdel/claude-bridge)** | strong external models (GPT-5, Grok) | the full Claude Code harness |
| **claude-local** (this repo) | free local models (Qwen, gpt-oss, …) | this deterministic loop |

Strong external models emit real diffs and drive a full harness well, so they go through
the bridge. Local models get the loop. The split is deliberate — each backend runs through
the transport it is actually good at.

## License

MIT (c) 2026 axdel
