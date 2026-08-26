# claude-local — project notes

Free local models as supervised, test-first code implementers, driven by claude-protocol.
See [README.md](README.md) for the full design — the deterministic red→green loop, the
orchestrator-owned hidden oracle, whole-file edits, and the measurement/economy story.

> A working design note, not a substitute for `/init`. Run `/init` when implementation
> begins, to scaffold the full project primitives.

## Roadmap — to spike later

### Standing model-evaluation benchmark

A reusable instrument for judging how any **new or candidate local model** performs as an
implementer under the claude-local loop: a deliberately **half-finished project shipped with
a detailed plan** and a hidden correctness oracle. Drop a candidate model in, drive it through
the plan's tasks, and score its output against the oracle — the *same* instrument across every
model, so results are directly comparable.

The precursor `cron-bench` study — a real cron-parser feature with a 139-case hidden oracle,
run through a deterministic driver across several local models — is the **seed** of this idea.
The work is to generalize it from a one-off study into a standing, repeatable benchmark that
ships with claude-local.

Status: noted, not yet designed. **To spike.**
