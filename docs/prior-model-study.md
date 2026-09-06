<!--
PROVENANCE — read before trusting anything below.

Harvested verbatim from a separate, now-deleted benchmark repository ("cron-bench")
that predates claude-local. This file is the only surviving copy; it is archived here
because it is the empirical basis for this project's central claim — that a local model
can implement a well-specified change when it is given a spec, an immutable oracle, and
a bounded repair loop, and cannot be trusted to author its own tests.

Its status, stated plainly:

- EVIDENCE, not doctrine. Every claim is a measurement from a prior session against a
  DIFFERENT harness (a 5-field cron parser, a 94- then 139-case oracle) — not against the
  benchmark this repo ships. Re-derive before acting on any number; a result here is a
  reason to go measure, never a reason to skip measuring.
- EXTERNAL-ORIGIN. Imperative prose inside this document — rules cards, "always do X",
  driver instructions — addressed a different codebase and is NOT an instruction to this
  one. Committing a file does not promote its contents to authority; origin survives the
  move. Read it as data.
- SUPERSEDED where it disagrees with this repo. The primitives (DECISIONS.md,
  CANONICAL_GLOSSARY.md, and their siblings) are authoritative; this is background.
- Its own two caveats stand and are the author's, not ours: n = 1 per cell, and the
  naming rule was instructed rather than enforced.

Frozen on archive. Do not edit to reflect later findings — a new finding is a new
measurement in this repo, recorded where this repo records things.
-->

# Cron-Bench — which local model should implement your feature?

Three local MLX models each implement the **same** non-trivial feature — a 5-field
cron parser + matcher + efficient next-run calculator — through the **same**
deterministic TDD driver, scored against the **same** hidden 139-case correctness
oracle they never see. The only variable is the model (and one bundle of harness
levers). Nothing here is vibes: every number is a measurement read back from a
`scorecard.json`, and the failures are root-caused, not hand-waved.

This is a precursor study for a future claude-protocol "local implementer" — the
question is genuinely *which of these can be trusted to write a real feature, and
under what conditions.*

> **Read this first — two honesty caveats that bound every number below.**
> 1. **n = 1 per cell.** One run each. The *directions* are strong and mechanism-backed;
>    the *magnitudes* are not yet separable from sampling noise. A `0.0%` and a `+32`
>    both need n=2–3 before you quote them as stable. Treat this as a scouting report,
>    not a leaderboard.
> 2. **The naming rule is *instructed*, not *enforced*** — see **Finding 5**. Where this
>    doc says a model "writes clean code," that means *ruff-clean*, which does **not**
>    catch single-char variable names.
>
> Prior runs (the Qwen rules-distillation study and the first 5-coder SWE sweep, on
> an older **94**-case oracle) are preserved verbatim in
> [`prior-model-study.earlier-runs.md`](prior-model-study.earlier-runs.md). This document supersedes them with
> the extended 139-case sweep.

---

## TL;DR — the verdict

For a real, moderately-complex feature under a TDD loop, on a 64 GB Apple box:

| Rank | Model | Reach for it when… | Keep it away from… |
|-|-|-|-|
| **1** | **Gemma4-26B-A4B** | you want the **safest, most reliable** implementer. 100% baseline, stays 95.7% even under the (currently buggy) levers, obeys `no-think`, moderate speed. The default. | nothing structural — it's the all-rounder. Just don't feed it `--retry-mode resample` until the harness snapshot fix lands. |
| **2** | **gpt-oss-20b** | you want **speed on a well-specified prompt**. Fastest by 2–3× (18 min), 95% at baseline, smallest footprint (12 G). | **its own tests** (writes ~0 and ignores TDD) and **`resample` retries** (they zeroed it — 95% → 0%). Verify against an external oracle; drive retries with *repair*, not *resample*. |
| **3** | **Ornith-1.5-35B-A3B** | you can **invest in scaffolding** and afford the latency. Plan-first nearly doubled it (48% → 81%) — the biggest lift-from-structure in the sweep. | **latency-sensitive or instruction-critical work.** Slowest (172 min), and it's an RL reasoning model that **defies `no-think`** and self-harms on hard tasks. |

**The one-line answer for complex tasks:** default to **gemma** for reliability and
**gpt-oss** for speed on well-specified work; the single biggest correctness dial for
a *weak* model was the **plan-first lever, not the model choice** — ornith gained +32
points from structure alone. And the levers as a bundle are a **double-edged sword
today** (see Finding 1): fix the `resample` clobber before you trust any "levers-on"
number.

---

## The instrument (unchanged from prior phases)

- **Feature** (`spec/SPEC.md`): cron field grammar (`*`, `N`, `a-b`, `*/s`, `a-b/s`,
  unions), the dom/dow **OR** quirk, `dow 7→0`, `L`/`W`/`#`/`LW` extensions,
  leap-year next-run, "**efficient** — do not step minute-by-minute", and a
  `CronParseError` on every invalid-input class. A real algorithm with real edge
  cases, finishable in one session.
- **Driver** (`driver/driver.py`): a deterministic **red→green TDD loop** per task —
  the model writes a failing test, then makes the whole suite pass, re-prompted with
  the full spec + current files each step. Up to 3 green retries. Plain
  chat-completions, `temperature 0.3`.
- **Engineering rules** (`spec/ENGINEERING_RULES.md`): a naming / structure /
  errors-out-of-existence / TDD distillation, injected into the **system prompt on
  every call** (`driver.py:585`), identical across all cells by design.
- **Hidden oracle** (`hidden_tests/`): **139 correctness cases** (hand-derived from
  the spec + a calendar, cross-checked against a differential brute-force reference)
  + **3 efficiency probes** (assert `next_run` returns fast even for an impossible
  schedule — i.e. it *skips*, doesn't *step*). **Never shown to any model.**
  Structure-agnostic: imports only `CronParseError, CronSchedule`.
- **Hardware:** one Apple M-series, **64 GB unified memory → one model at a time.**
  The full sweep ran **serial, ~11 h wall-clock**, each model serving and tearing
  down its own `mlx_vlm` server so RAM is free between models.

### The 2×2 factorial — and why it collapsed to one axis

The design was **levers × thinking**: `B4` = `--plan-first --retry-mode resample
--mutation-gate` vs `B0` = baseline (no levers), crossed with reasoning **on** vs
**off**. It collapsed to **levers × no-think** because the thinking axis produced
**zero usable data** — every model's reasoning ran **unbounded** (it never converged
to code inside the 24 576-token probe budget), so all "think" cells are
recorded-not-run. See **Finding 2**.

Budgets: **no-think code budget 32 768 tokens**; think probe/ceiling 24 576.
Reasoning-family models use `--reasoning-effort low/high` (gpt-oss, harmony format);
Qwen-lineage models use `--think/--no-think` (gemma, ornith).

---

## The standings — six no-think cells

**The exact task every model implemented:** a Python module exposing
`CronSchedule(expr)` + a `CronParseError` — **parse** a 5-field cron string (`*`, `N`,
`a-b`, `*/s`, `a-b/s`, comma-unions, plus `L` / `W` / `#` / `LW` and `@daily`-style
aliases, with the dom/dow **OR**-quirk and `dow 7→0`), **match** whether a given
datetime fires it, and **compute the next matching datetime efficiently** (skip to the
next match, never step minute-by-minute) — raising `CronParseError` on every invalid
input. Built **test-first, as 9 ordered TDD tasks**, through the driver: tasks **1–5**
are the core (field parser → expression parser → matching → next-run → public API),
tasks **6–9** add the extensions (`@`-nicknames → `L` specials → `W`/`#` specials →
`next_n_runs`). The table below scores each model's *final* code against the 139-case
hidden oracle for that whole feature.

### Summary A — models × metrics (aggregate, per cell)

| model · cell | hidden oracle (139) | own suite (final) | tasks green ¹ | src lines | completion tokens | wall time | tok/s |
|-|-|-|-|-|-|-|-|
| **gemma** · b0 (baseline) | **139 / 139 · 100.0%** | 59 / 61 | 0 / 9 | 482 | 98,199 | 43 min | 38.4 |
| **gemma** · b4 (levers) | 133 / 139 · 95.7% | 58 / 63 | 0 / 9 | 520 | 142,377 | 61 min | 39.1 |
| **ornith** · b0 (baseline) | 67 / 139 · 48.2% | 105 / 135 | 1 / 9 | 623 | 128,427 | 40 min | 54.2 |
| **ornith** · b4 (levers) | **112 / 139 · 80.6%** | 98 / 120 | 1 / 9 | 526 | **504,784** | 172 min | 48.9 |
| **gpt-oss** · b0 (baseline) | 132 / 139 · 95.0% | 0 / 1 | 0 / 9 | 503 | 56,444 | 18 min | 52.7 |
| **gpt-oss** · b4 (levers) | **0 / 139 · 0.0%** | 7 / 84 | 3 / 9 | 281 | 60,431 | 19 min | 54.0 |

**The levers effect (b4 − b0, hidden oracle):** `+32.4` (ornith), `−4.3` (gemma),
`−95.0` (gpt-oss). Same three flags, three completely different outcomes — the story
of the sweep, unpacked in Finding 1. Note ornith b4's **504 K completion tokens — ~4×
its own baseline** — that's the reasoning leak (Finding 4) quantified: it *thought* its
way to 172 minutes.

### Summary B — tasks × models (per-task TDD progress)

Each cell is whether that model's **own test suite went green** at that task's green
step — a **TDD-process** signal, *not* hidden-oracle correctness — with green attempts
used in parentheses (max 3 = all retries exhausted). `✓` = own suite green, `✗` = never
green.

| # | task | gemma b0 | gemma b4 | ornith b0 | ornith b4 | gpt-oss b0 | gpt-oss b4 |
|-|-|-|-|-|-|-|-|
| 1 | Field parser | ✗(3) | ✗(3) | ✗(3) | ✓(1) | ✗(3) | ✓(3) |
| 2 | Expression parser | ✗(3) | ✗(3) | ✗(3) | ✗(3) | ✗(3) | ✓(2) |
| 3 | Matching | ✗(3) | ✗(3) | ✗(3) | ✗(3) | ✗(3) | ✓(1) ² |
| 4 | **Next run** (efficient) | ✗(3) | ✗(3) | ✗(3) | ✗(3) | ✗(3) | ✗(3) |
| 5 | Public API + e2e | ✗(3) | ✗(3) | ✓(2) | ✗(3) | ✗(3) | ✗(3) |
| 6 | Nicknames (`@daily`…) | ✗(3) | ✗(3) | ✗(3) | ✗(3) | ✗(3) | ✗(3) |
| 7 | `L` specials | ✗(3) | ✗(3) | ✗(3) | ✗(3) | ✗(3) | ✗(3) |
| 8 | `W` and `#` specials | ✗(3) | ✗(3) | ✗(3) | ✗(3) | ✗(3) | ✗(3) |
| 9 | `next_n_runs` | ✗(3) | ✗(3) | ✗(3) | ✗(3) | ✗(3) | ✗(3) |

Three things jump out, and they matter more than any single score:

- **The paradox that defines the sweep: gemma b0 is `0 / 9` green yet `100%`
  oracle-correct.** Its *code* is right; its *self-written tests* over-reached. Both of
  its 2 failing self-tests assert behavior the spec never required — one demands the
  error message read `'out of range'` when the code correctly raises `'Malformed range:
  -1'` (right exception, wrong prose); the other expects an exception the spec doesn't
  mandate. A task-1 self-test that stays red poisons every later task's green (each
  green step re-runs the whole suite). Summary B measures TDD discipline, and the
  sharpest result here is that **local models write correct code far better than they
  write self-consistent tests** — the own-suite and tasks-green columns are process
  signals, never a substitute for the oracle.
- **Task 4 "Next run" is the universal wall** — *no* cell greened it. The efficient
  skip-don't-step algorithm is the crux of the whole feature, and it's where every
  model's TDD loop broke.
- **gpt-oss b4 is the only cell to green several early tasks (1–3), then hit the
  task-4 wall** — and from there the resample clobber (Finding 1) finished it: the
  early wins were overwritten, ending at `0%`. Its `3 / 9` tasks-green sits *above* a
  `0%` oracle because the greens happened *before* the clobber.

*Metric notes:* ¹ "tasks green" counts green own-suite task steps (from `summary.json`);
because no cell reached a fully-green suite, the `--mutation-gate` lever never fired
(Finding 3). ² gpt-oss's task-3 green had **no real red step** first (its added test
didn't fail before passing) — a TDD-fidelity miss tracked as `red_kind` in
`summary.json`. ruff violations (dominated by docstring/whitespace nits; **naming `N`
is 0 everywhere** — a scorer blind spot, not clean naming, Finding 5) are omitted here
and detailed in `runs/<cell>/scorecard.json`.

---

## Per-model deep dive

### Gemma4-26B-A4B — the reliable all-rounder

- `lmstudio-community/gemma-4-26B-A4B-it-MLX-6bit` · MoE, ~4 B active/token · 21.8 G ·
  262 K context · the fastest decode in the roster (~61 tok/s decode, ~2070 prefill).
- **100% baseline correctness**, and it *stays high* under the levers (95.7%) rather
  than shattering. Its `next_run` is **analytic** — all three efficiency probes return
  in `0.00 s` (it computes the next match, never steps toward it).
- **Obeyed `no-think`** — zero leaked reasoning on every call, the clean control
  against which ornith's defiance (Finding 4) stands out.
- The levers slightly *hurt* it (−4.3) because it's already saturated on this task
  and the current `resample` implementation is a net risk, not a help (Finding 1).
- **Verdict:** the default pick. On a task this model can't yet saturate, pair it with
  plan-first *once the resample clobber is fixed*; today, run it at baseline.

### gpt-oss-20b — the fast specialist with two sharp edges

- `mlx-community/gpt-oss-20b-MXFP4-Q8` · MoE 20.9 B total / ~3.6 B active (4 of 32
  experts) · 12.1 G (smallest) · 128 K context · native MXFP4 · OpenAI open-weight,
  harmony reasoning format.
- **Fastest by far (18 min) and 95% correct at baseline** — excellent code under
  direct prompting. But two edges you must design around:
  - **Edge 1 — it ignores TDD.** Its own suite is **0 / 1**: it barely writes tests
    and doesn't let them drive the code. It codes well *anyway*, but **never trust its
    self-tests as a signal** — you need an external oracle.
  - **Edge 2 — `resample` is catastrophic for it.** b4 = **0.0%**. Not a scorer bug:
    a late resample overwrote its working `next_run` with a `NotImplementedError`
    stub and the loop kept the broken final state (Finding 1). Its baseline `next_run`
    also does *real work* on the efficiency probes (0.35 s / 0.59 s vs gemma's 0.00 s)
    — bounded and passing, but closer to stepping than skipping.
- **Verdict:** the speed play for **well-specified** prompts. Drive retries with
  *repair* (keep-and-fix), never *resample* (regenerate-from-scratch), and verify
  against something other than its own tests.

### Ornith-1.5-35B-A3B — high ceiling, needs scaffolding, pays in time

- `ornith-ai/Ornith-1.5-35B-A3B-MLX-6bit` · Qwen3.5-MoE, 35 B total / ~3 B active
  (8 of 256 experts) · 28.2 G · 262 K context · MIT · released 2026-08-19 by
  DeepReinforce via an **RL self-improvement loop** — a *reasoning* model.
- **Weakest baseline (48.2%)** but **plan-first nearly doubled it to 80.6%** — the
  single largest structural lift in the sweep, and the strongest evidence that a
  weak model's correctness is dominated by *scaffolding*, not raw capability.
- **Defies `no-think`** (Finding 4): on its two hardest green attempts it emitted
  40 K–86 K characters of `<think>` reasoning *with the flag off*, blowing the code
  budget and truncating its own output — it hurts itself by thinking when told not to.
- **Slowest by 3–4×** (172 min for the levers cell), partly *because* of that leaked
  reasoning.
- **Verdict:** only when you can invest in structure and eat the latency. Highest
  reward from plan-first, worst instruction-follower, and the clock is brutal.

---

## Findings

### 1. The levers bundle is high-variance — and `--retry-mode resample` can zero a capable model

`b4 − b0` on the hidden oracle was **+32.4 (ornith), −4.3 (gemma), −95.0 (gpt-oss)** —
the same three flags producing lift, noise, and catastrophe. The catastrophe is
mechanical and confirmed in the driver, not a scoring artifact:

- In `resample` mode, `green_step` writes a **fresh full `src/` on every attempt**
  (`driver.py:877-897`) and, on retry-exhaustion, keeps the **last** attempt's files
  (`:916`) with **no rollback to the best-passing state**.
- gpt-oss wrote a **working `next_run` in 12 replies** but stubbed it with
  `NotImplementedError` in **4** — including `task9_green_attempt3`, the *last* attempt
  of the *last* task. That final stub overwrote the previously-working core; retries
  exhausted; the stub stayed → oracle **0 / 139**, own suite **7 / 84**. The code
  imports and runs — it's a skeleton, not a crash.

The upside is just as real: plan-first's *structure* is what carried ornith's +32.
The lesson is that the **bundle** conflates a helpful lever (plan-first) with a
harmful one (`resample`, as currently implemented).

**Fix on the table (not yet taken — it changes what the bench measures):** keep a
best-passing snapshot per file and overwrite only on a passing attempt, mirroring the
existing `run_gremlins` defensive-restore (`driver.py:736`). Trade-off: the model's
*final* state (faithful to a real TDD session that ends broken) vs its *best coherent*
state (kinder, less brittle). Either choice needs a re-run.

### 2. The thinking axis produced zero data — every model's reasoning ran unbounded

Before running the two "think" cells, the harness sends one real spec+rules prompt
with reasoning **on** at the 24 576-token ceiling and checks whether it converges to
code. **All three verdicts were UNBOUNDED** — the model reasoned past the budget and
never emitted code (gpt-oss's probe: 386 s, truncated at 24 576, no code; gemma probed
unbounded at 8k/16k/24k in a prior pass; ornith likewise). So **all think cells are
recorded-not-run**, and the 2×2 collapsed to levers × no-think.

This is itself a result: **for a hard implementation task, unconstrained reasoning on
these local models is a budget trap** — it burns the token budget without producing a
deliverable. A reasoning model here needs a *hard* thinking cap, not an open one.

### 3. The `--mutation-gate` lever never engaged

Mutation testing (gremlins) only runs on a **fully-green own suite**. **No local model
produced one** — every b4 self-suite went non-green → `suite_not_green` → gremlins
never fired. The mutation lever is untested on local models here not because it failed,
but because **none of them cleared the bar to reach it.** A signal in its own right:
these models don't reliably make their *own* tests pass.

### 4. Ornith defies `enable_thinking:false` — `no-think` is a request to the model, not a hard gate

The no-think pole is **plumbed correctly end-to-end** (driver sends the flag at
`driver.py:364`; `mlx_vlm` applies it verbatim at `request_normalization.py:157` into
the chat template), yet ornith still emitted large `<think>` blocks on its hardest
tasks: **86 469 / 40 008 / 62 900 characters** of reasoning on three green attempts;
**0** on every plan/red call and all of tasks 3–5. The pattern is **difficulty-triggered**
— it obeys on easy calls and overrides on the hard ones.

- **Root cause:** it's an RL-reasoning-trained model; `enable_thinking:false` is a
  *soft, template-level* request its policy overrides when it "wants" to reason.
  **gemma honored the same flag perfectly** — so this is model policy, not a harness
  fault.
- **Corrects an earlier live read:** ornith's task-1 truncation was **not** "verbose
  code" — it was ~86 K chars of *leaked reasoning* eating the code budget.
- **Available hard gate (unused):** `mlx_vlm` exposes a per-request `thinking_budget`
  (`schemas.py:376`). `thinking_budget:0` would enforce a clean no-think pole even for
  a defiant model — at the risk of cutting it off mid-think with no code. Worth a
  controlled re-run before trusting.

### 5. The naming rule is instructed every call but largely unscored

`ENGINEERING_RULES.md §1` ("intent-revealing names", rejects `data`/`tmp`/`val`/`d`
for `day`) rides in the system prompt on **every** call — but the objective proxy is
`ruff select = ["E","F","I","N","D"]`, and ruff's **`N`** rules are pep8 *conventions*
(snake_case, CapWords), **not descriptiveness**. Single-char locals (`d`, `i`, `n`,
`x`) are all `N`-clean. So a model that writes `d` for `day` **breaks the rule we gave
it and pays zero measured penalty** — which is exactly why every cell shows `N = 0`.
**Descriptiveness is uninstrumented**; this doc says the naming rule is *instructed*,
never *enforced*. (A tune-later LLM-judge naming pass or a length/descriptiveness
linter would close it.)

### Bonus — gpt-oss scored 95% while writing ~1 of its own tests

gpt-oss b0: **oracle 95.0%, own suite 0 / 1.** It essentially skips TDD and codes well
regardless. Good news for raw capability, bad news for any workflow that *trusts the
model's self-tests* as a proxy for correctness — for gpt-oss, that proxy is empty.

---

## What a v2 sweep should fix

1. **`n = 2–3` per cell** — separate the real effects (plan-first's +32, resample's
   collapse) from sampling noise. This is the top priority; every headline number here
   is a single draw.
2. **Split the `b4` bundle** — measure `--plan-first`, `--retry-mode`, and
   `--mutation-gate` independently. Finding 1 shows they don't move together.
3. **Fix the resample clobber** — best-passing snapshot (Finding 1), then re-measure
   `b4` for all three.
4. **Add `--retry-mode repair`** and compare against `resample` — repair should be far
   safer for a model like gpt-oss.
5. **Hard thinking cap** — re-run the think axis with `thinking_budget` set, so the
   axis produces data instead of unbounded reasoning (Findings 2, 4).
6. **Instrument descriptiveness** — an LLM-judge or length-based naming check, so the
   naming rule is scored, not just sent (Finding 5).

---

## Appendix — provenance

- **Scorecards:** `runs/<label>-ext-{b0,b4}-nothink/scorecard.json` — every number in
  the standings table reads back from these.
- **Lifecycle logs:** `runs/_lifecycle-{gemma,ornith,gptoss}-ext.log` — per-cell drive
  and score timestamps (the wall-time column).
- **Live findings log:** `runs/_findings.md` — the working notes these findings were
  distilled from, with full evidence and file:line citations.
- **Prior phases:** [`prior-model-study.earlier-runs.md`](prior-model-study.earlier-runs.md) — the Qwen
  rules-distillation study (Phase 1) and the first 5-coder SWE sweep (Phase 2), on the
  older 94-case oracle. Superseded by this document but preserved verbatim.
- **Scorer:** `driver/score.py` — pure measurement (own suite + hidden oracle + ruff);
  nothing consults a model to decide a score.
