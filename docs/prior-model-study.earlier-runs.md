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

# Cron-Bench: local models implement the same real feature under a TDD driver

A controlled benchmark. One non-trivial feature (a 5-field cron parser + matcher
+ efficient next-run calculator), one deterministic TDD driver, one hidden
correctness oracle. The **local models** each do the **same 5 tasks in
order, test-first**, through the identical driver, re-prompted with the full spec
+ current files each step; **Opus is the frontier reference**, written directly
(not through the driver) as the quality bar. Nothing here is vibes — every score
is a measurement, and the one judgment layer (Opus reviewing code quality) is kept
separate from the mechanical scores and its findings are re-verified by hand.

> **This document has two phases, one instrument.** **Phase 1** (immediately below)
> is the original rules-distillation study — three local models + Opus, asking
> whether a better architecture-rules distillation makes the generated code more
> concise and elegant. **Phase 2** (["The SWE sweep"](#phase-2--the-swe-sweep-which-local-model-is-the-best-implementer), at the bottom) runs **five more local
> coders** through the *identical* instrument to answer a prior question — *which
> local model is the best implementer of this feature at all* — before any rules
> tuning. Same SPEC, same 94-case oracle, same driver, same 44-line baseline rules;
> the **only variable is the model.** Phase 2's champion is the target the deferred
> rules-light phase will tune against.

## The instrument

- **Feature** (`spec/SPEC.md`, 155 lines): cron field grammar (`*`, `N`, `a-b`,
  `*/s`, `a-b/s`, unions), the dom/dow **OR** quirk, dow `7→0`, leap-year
  next-run, "efficient — do not step minute-by-minute", raise `CronParseError`
  on every invalid-input class. A real algorithm with real edge cases, small
  enough to finish in one session.
- **Driver** (`driver/driver.py`): deterministic red→green TDD loop per task.
  Red = the model writes a test that must fail first; green = it makes the whole
  suite pass (and may correct *its own* test only if that test contradicts the
  SPEC — the SPEC is the sole source of truth). Up to 3 green retries. Plain
  chat-completions, `enable_thinking:false`, temp 0.3.
- **Hidden oracle** (`hidden_tests/test_oracle.py`): 94 correctness cases
  (hand-derived from the spec + a calendar, cross-checked against a differential
  brute-force reference) + 2 efficiency probes (assert `next_run` returns in
  <8 s even for an impossible schedule — i.e. it skips, not steps). **Never shown
  to any model.** Structure-agnostic: imports only `CronParseError, CronSchedule`.
- **Judge**: Opus reviews each contestant's *code* on six axes (naming,
  docstrings, architecture, elegance, error handling, robustness). Judgments are
  re-run by hand before being banked.

## What each model received

This is the crux of the fairness question. The models did **not** get the same
architectural guidance:

| Model | Architecture guidance | How produced |
|-|-|-|
| **Opus 4.8** (reference) | The full `~/.claude/rules/` engineering doctrine (thousands of lines: SOLID, KISS/YAGNI, deep modules, naming, testing methodology) — internalized | Written **directly** as the frontier reference — **not** through the per-task TDD driver (no red→green loop, no token/time measurement). It is the *aspirational bar*, not a same-instrument contestant. |
| **Qwen3.8-27B** (v1) | `spec/ARCH_RULES.md` — a **44-line** distillation (naming, structure, errors-out-of-existence, docs, TDD). **No explicit conciseness clause.** | Through the driver: local, 6-bit MLX, non-thinking |
| **Gemma4-26B-A4B** | Same 44-line `ARCH_RULES.md` | Through the driver: local, 6-bit MLX MoE, non-thinking |
| **Qwen3.8-27B (v2)** | `spec/ARCH_RULES_v2.md` — the 44 lines **plus one explicit Simplicity/KISS/YAGNI section** (deep modules, no shallow helpers, reach for stdlib, "a clean solution is well under ~350 lines"). *Isolates the single variable.* | Through the driver: local, same server, non-thinking |

**Two things are being compared, and the distinction is load-bearing for
honesty:**

1. **The controlled experiment** — the three **local runs through the identical
   driver**. Only these are process-comparable (same TDD loop, same per-call
   re-prompt, measured tokens + time). Within them: *Qwen-v1 vs Gemma* isolates
   the model (same rules), and *Qwen-v1 vs Qwen-v2* isolates the rules (same
   model) — the direct test of your hypothesis: **was Qwen's verbosity a model
   trait, or an artifact of thin architecture rules?** (Qwen-v2 derailed on task 3
   and delivered only a *component-level* answer — its field parser — not a full
   implementation; findings 5–6 report exactly what it did and didn't establish.)
2. **The reference bar** — Opus-with-full-doctrine, written directly. Its *code
   quality* (LOC, immutability, oracle correctness, ruff) is a fair target to
   measure the locals against, because scoring is process-independent. Its *speed*
   is not comparable and is not claimed — it never ran the driver.

---

## Results

### Correctness + efficiency + style (mechanical, pre-registered)

| | Hidden oracle | Efficiency probes | Own suite | ruff | Source LOC |
|-|-|-|-|-|-|
| **Opus 4.8** (gold) | **94/94 = 100%** | both instant | 88/88 green | 0 | **311** |
| **Qwen3.8-27B v1** | **94/94 = 100%** | both instant | 75/75 green | 0 | 527 (+69%) |
| **Gemma4-26B-A4B** | **94/94 = 100%** | both instant | 35/36 (1 self-authored bad test) | 6 (3 D, 3 I) | 353 |
| **Qwen3.8-27B v2** | run did not complete¹ | — | — | clean (tasks 1–2) | fields.py **89 vs v1's 165** → finding 6 |

**The headline surprise: all three implementations are 100% correct on the
hidden oracle** — including Gemma, whose driver "green" score was 0/5. "Green"
(whole own-suite passes) and "correct" (independent oracle passes) are *different
axes*; a model can write a correct implementation and one bad test. The oracle is
the real correctness truth. On LOC, **Opus 311 < Gemma 353 < Qwen 527** — Gemma,
on the *same* 44-line rules as Qwen, landed far closer to the gold standard.

¹ Qwen-v2 derailed on task 3 in **both** runs (finding 5), so it has no full
implementation to score end-to-end. Its completed tasks 1–2 give a *clean
single-variable* comparison against v1 — the answer to the rules-distillation
question — presented in finding 6, not in this full-impl table.

### Robustness edges (where the mechanical oracle didn't reach)

| Edge | Opus | Qwen v1 | Gemma | Qwen v2 | Spec-correct answer |
|-|-|-|-|-|-|
| `parse_field("5/2")` — step on a single value | **raises** | returns `[5]` | returns `[5]` | **raises** ✓ | raise `CronParseError` |
| Value object actually immutable? | **yes** (`frozen=True`) | **no** (fakes it via `__slots__`) | **yes** | — (schedule.py truncated) | must be immutable |

Only the frontier model gets the `5/2` malformed-step right among the *v1-rules*
runs; Qwen v1 and Gemma both share the bug. **Qwen-v2 — same model, conciseness
rules added — fixes it** (all of `5/2`, `10/3`, `0/5` now raise). On immutability,
**Gemma matches Opus and Qwen v1 does not** — v1's docstrings *claim* immutable
while the object mutates freely; v2's value object never landed (task-3 derail), so
that cell is untested for v2.

### Code-quality judge scores (Opus reviewing each contestant, 0-10 per axis)

Every score empirically re-verified by running the code; findings cite file:line.

| Axis | Qwen v1 | Gemma |
|-|-|-|
| Naming | 9 | 9 |
| Docstrings | 7 | 9 |
| Architecture | 6 | 8 |
| Elegance / conciseness | 5 | 6 |
| Error handling | 7 | 8 |
| Correctness robustness | 6 | 7 |
| **Overall / 100** | **66** | **84** |

(Qwen-v2 has no full implementation to judge — task 3 derailed twice. Its
completed **field parser** is judged head-to-head against v1's in finding 6 via a
focused Opus A/B review, which is the fair way to score a single-component result.)

**On identical 44-line rules, Gemma's code out-scored Qwen by 18 points** — better
docstrings, cleaner architecture, genuine immutability, and 174 fewer lines. So a
large part of Qwen v1's verbosity+66 is a **model trait**, not merely thin rules.
Both carry the same concentrated weakness (`next_run` rollover triplicated) and the
same `5/2` bug. (Opus is the reference bar and the judge, so it carries no self-
score.) The open question the v2 column answers: **how much of Qwen's *own* gap
closes with a better distillation?**

### Speed (wall-clock, run **alone** on the same M-series box, 64 GB)

Wall-clock here is **model-generation time** (`tokens ÷ decode tok/s`), the
comparable figure across a cloud model and two local ones; it excludes pytest and
prompt-assembly overhead, which are equal across runs.

| | Model kind | Decode | Calls | Gen tokens | Gen time | Notes |
|-|-|-|-|-|-|-|
| **Opus 4.8** | cloud frontier (reference) | — | — | — | — | written directly, not through the driver — no token/time measurement, speed not claimed |
| **Qwen3.8-27B v1** | 27B dense, MTP draft | 19.8 tok/s | 11 | 25.6 K | **21.6 min** | 100% correct; t1 took 2 green attempts |
| **Gemma4-26B-A4B** | 26B MoE (~4B active) | **46.3 tok/s (2.3×)** | 20 | 47.0 K | **16.9 min** | 2× the calls/tokens (chasing retries on its bad test) yet still faster overall |
| **Qwen3.8-27B v2** | 27B dense, same server | ~20 tok/s | — | — | — | did not complete (task-3 derail ×2 — finding 5); no clean wall-clock |

Gemma's decode is 2.3× Qwen's, so even burning 20 calls / 47 K tokens on retries
it finished *faster* than Qwen's clean 11-call run. Had it gone green in 1–2
attempts (like Qwen), it would have landed near **8–10 min** — the real
fast-and-correct ceiling, if only its test self-review held.

---

## The findings

### 1. Qwen: 100% correct, but the extra 69% of code buys nothing (judge: 66/100)

Opus review of Qwen v1 — **66/100** (Naming **9**, Docstrings 7, Architecture 6,
Elegance **5**, Error handling 7, Robustness 6). Both high-value findings
**independently re-verified by hand under the project venv**:

- **A latent bug the oracle missed.** `parse_field("5/2", 0, 59)` on Qwen returns
  `[5]` — it accepts a step on a single value and silently discards the step —
  where the spec (and Opus) **raise `CronParseError`**. Confirmed:
  `opus → RAISED`, `qwen → RETURNED [5]`. So **100% on the oracle ≠ bug-free**;
  the qualitative judge earned its place by catching a case my 94 hand-written
  cases didn't. (Instrument note: I keep the oracle frozen at 94 and report this
  separately — retroactively adding Opus-authored cases to dock Qwen would be
  moving my own goalposts, since Opus authored both the oracle and the gold impl.)
- **False immutability.** Qwen's value object claims "immutable" in three
  docstrings but uses `__slots__` + a manual `__init__`, so `s.minutes =
  frozenset({99})` **succeeds**. Opus's `@dataclass(frozen=True)` raises
  `FrozenInstanceError`. Confirmed both ways.
- **The +69% LOC is mostly unjustified** — over-documented private trivia, the
  hand-written `__init__`, and triplicated day/month/year wrap-arithmetic in
  `next_run`. Not depth; ceremony.

### 2. Gemma's implementation is correct — it failed *test self-review*, not the feature

At first glance Gemma looked broken: **0/5 green**, 3 retries exhausted on every
task. But the hidden oracle says **94/94 = 100%**, both efficiency probes pass,
and its value object is genuinely immutable — a **correct, efficient, 353-line**
implementation, more concise than Qwen and more architecturally honest. The Opus
judge scored it **84/100 — 18 points above Qwen v1** (better docstrings, cleaner
architecture, real immutability). So on the "green" axis Gemma looked like the
worst model, and on every *quality* axis it was the best of the locals.

What actually happened: one of its own tests is self-contradicting. It wrote

```python
# Month out of range
with pytest.raises(CronParseError):
    CronSchedule.parse("* 0 * * *")
```

— but `0` is in the **hour** field (position 2, range 0–23), so `* 0 * * *` is a
**valid** expression. Gemma's parser correctly accepts it (verified: it *does*
raise on the genuinely-invalid `* * * 0 *`, `* * * 13 *`, `60 * * * *`). The test
mislabels field positions. Under the driver's green gate the whole own-suite must
pass, so that one poisoned test masked five correct tasks as failures.

**The sharp finding is the contrast with Qwen.** Both local models wrote a buggy
test. The green rule explicitly permits fixing a test that contradicts the SPEC
(valid input must never raise — exactly this case). **Qwen recognised its bad test
(`10-20/10 → {10}` vs the spec's `{10,20}`), corrected it citing the SPEC, and
went green. Gemma never recognised its own mistake — it burned all three retries
trying to reconcile a correct implementation with an impossible test.** That is
the real local-model difference here: not implementation skill (both are strong),
but **self-review** — noticing that *your own test* is the thing that's wrong.

(Gemma also, notably, self-*fixed* an earlier malformed regex — it had written
`pytest.raises(match='.**/.*')` in task 1 and repaired it to `re.escape(term)` by
a later task. So its self-review is not absent, just unreliable — it caught the
regex, missed the field-position mislabel.)

### 3. Thinking-mode runaway (a real operational trap, root-caused)

Qwen with `--enable-thinking` burned its **entire** 12 K-token budget on
`reasoning_content` and emitted **zero** `content` (`finish_reason=length`) — the
task never got an implementation. The `/no_think` *text* soft-switch did **not**
survive a large system prompt. The robust fix is the `enable_thinking:false`
**request parameter** (mlx_vlm's chat schema honours it at the template layer):
verified `reasoning→0 chars, finish=stop`, code intact, *with* the system prompt
present. Every benchmark run uses it. **Corollary for the speed question: Qwen's
100% is the non-thinking run — thinking here is strictly worse (slower *and* it
produced no code).**

---

### 4. The instrument measured its own blind spots (why the multi-lens design matters)

Two independent lenses each caught something the 94-case mechanical oracle did
not — which is the whole argument for not scoring on one number:

- **Green ≠ correct.** The driver's per-task "green" gate measures own-suite
  self-consistency; the hidden oracle measures implementation correctness. Gemma
  scored 0/5 on the first and 94/94 on the second. Reporting only "green" would
  have called a correct implementation a total failure.
- **100% ≠ bug-free.** The Opus judge found `5/2` on Qwen; re-verification showed
  **both** local models share it. My hand-written oracle simply never probed a
  step-on-a-single-value, so all three passed 94/94 while two carried a real bug.
- **The fix is honesty, not oracle-tuning.** I keep the oracle frozen at 94 and
  report the `5/2` gap separately, because Opus authored *both* the oracle and the
  gold implementation — retroactively adding cases it happens to pass would launder
  a conflict of interest into the score. A strengthened oracle *would* widen
  Opus's lead; the honest move is to say so, not to quietly do it.

### 5. Qwen-v2 hit a *reproducible* repetition-loop derail on task 3 (2/2 runs)

Qwen-v2 derailed on task 3 **both times it was run** — at the exact same step,
with byte-identical output. Writing the dom/dow matching test, the model must
hand-derive a Friday calendar date for the assertion, and instead of reasoning
briefly it falls into a degenerate repetition loop in a code comment:

```
# 2024-09-13 is Friday. Use 2024-09-13? Use 2024-09-13.
# 2024-09-13 is Friday. Use 2024-09-13? Use 2024-09-13.   (×hundreds → 8192 tokens, truncated)
```

Both runs: `task3` maxed the 8192-token budget (`finish_reason=length`) and
truncated mid-file, so `schedule.py` never received a `matches()` method — yet the
driver logged the task "green" (the truncated test file also lost its real
assertion, so the empty suite passed vacuously). Attempt 2 additionally bloated the
*red* phase (6663 tokens). Evidence for both is preserved in
`runs/_qwen-v2-attempt{1,2}-derailed/`. After two identical derails I stopped —
re-running an identical config that fails identically is not evidence-gathering.

The deeper point ties this to finding #3. This model, on task 3's
calendar-reasoning sub-problem, *wants to spend a lot of tokens reasoning*, and
**whichever channel it's given, it can blow the budget there**:

- **Thinking ON** → burns the whole budget on `reasoning_content`, emits zero code (finding #3).
- **Thinking OFF** → pushes the reasoning into verbose code comments and loops there (this finding).

v1 (also non-thinking) cleared task 3 on its single run — so this is **sampling
variance on a known-fragile step, not a rules effect**: n is far too small to
attribute the derail to v2's KISS rules, which say nothing about date reasoning.
Two honest instrument notes: (a) raising `max_tokens` is not a fix — the loop is
unbounded; (b) a truncated green step that yields neither impl nor test *should* be
a hard driver failure, not a vacuous pass. Both are future-instrument work, not
applied here — changing the driver or adding a repetition penalty mid-experiment
would diverge from v1's instrument and invalidate the one clean comparison the run
*did* deliver (below).

### 6. The rules-distillation hypothesis: confirmed on the component that completed

Task 3 derailed, so Qwen-v2 has no full implementation to score end-to-end. But
tasks 1–2 (the error type and the **field parser**) completed cleanly — and the
field parser is exactly where v1's verbosity concentrated (165 of its 527 lines).
That gives a **clean controlled A/B**: identical model, driver, temperature,
non-thinking mode, and SPEC task; the *only* changed variable is the architecture
guide (v1's 44 lines vs v2's 44 + one KISS/YAGNI section).

| `parse_field` (same model, only the rules differ) | v1 (44-line rules) | v2 (44 + KISS) |
|-|-|-|
| Source LOC | 165 | **89 (−46%)** |
| ruff | clean | clean |
| Correctness (38-case independent battery, both versions run) | 35/38 (accepts `5/2`, `0/1`, `1,5/2,10`) | **38/38** |
| `parse_field("5/2")` — step on a single value | `[5]` (silent bug) | **raises `CronParseError`** (correct) |
| Opus A/B judge, field parser only | ~69/100 | **~82/100** |

The added conciseness section did not merely shrink the code — the smaller version
is **also more correct**, fixing the exact `5/2` latent bug that v1 *and* Gemma
both shipped (finding #1). This is the strongest possible shape for the hypothesis:
better architecture distillation improved elegance and correctness *together*, on a
controlled single-variable comparison.

**The Opus A/B judge (read-only, both parsers run against a 38-case independently
hand-derived battery) confirmed it — and found the causal mechanism, the sharpest
result in this benchmark:**

- **Conciseness *removed* the bug; it didn't trade against it.** v1's bug lives in
  its *extra* decomposition — a shallow `_parse_term → _expand_base` pass-through
  (qwen/fields.py:68,92-94) whose single-value branch silently drops the `step`
  argument, so `5/2` returns `{5}`. v2, pushed by the "deep module, no shallow
  single-use helper" clause, fused those into one `_parse_term`
  (qwen-v2/fields.py:37-55) — the single-value branch still sees the slash context
  and raises. **The verbose split was the seam that held the bug; collapsing it is
  what fixed it.** (v1 accepts the whole `N/s` class — `0/1`, `1,5/2,10` too — not
  just the one case my frozen oracle happened to probe.) v2 also unified v1's four
  duplicated range-building branches into one `range(start, end+1, step)` exit.
- **The honest cost (the judge went looking for it):** v2 introduced a magic-number
  step ceiling `10**9` (qwen-v2/fields.py:64) the spec doesn't call for — it raises
  on absurd billion-steps that v1 accepts spec-correctly — plus a weaker `*/0`
  message and one dead branch. Real blemishes, but v2 **fails safe** (raises, never
  wrong output), and they are dwarfed by closing an entire malformed-input class.

Net: the KISS distillation improved concision and correctness *together*, at the
price of one small, safe magic-number regression. (The ~69/~82 here is a
**field-parser-only** score — not comparable to the *full-impl* judge table above,
where v1 scored 66 and Gemma 84; it isolates the one component v2 completed.)

The load-bearing caveat, stated plainly: this is **one component (n=1 file)**, not
the full 527→? line reduction the headline would want, because the derail blocked
the end-to-end number. The direction is clear and the comparison is clean; the
*magnitude* across a whole implementation is extrapolation, not measurement.

## Verdict

Two questions were on the table. Both are answered — one fully, one in direction
with a clean single-variable measurement but not a full-impl magnitude.

**Q1 — What is the fastest *correct* local configuration?**
**Gemma4-26B-A4B in non-thinking mode.** Its MoE architecture (~4B active of 26B)
decodes at **46.3 tok/s — 2.3× Qwen's dense 19.8** — so even though its unreliable
test self-review made it burn 20 calls / 47 K tokens chasing retries, it still
finished (**16.9 min**) *faster* than Qwen v1's clean 11-call run (21.6 min), at an
identical **100% on the hidden oracle**. Had its self-review held (1–2 green
attempts, like Qwen), it projects to **~8–10 min** — the real fast-and-correct
ceiling here. Two levers make a local model fast without sacrificing correctness:
**(a) MoE decode speed** (the dominant factor — 2.3× for free) and **(b)
non-thinking mode**, confirmed strictly better than thinking, which burned the
whole budget on reasoning and emitted zero code (finding 3). Fewer output tokens is
the third lever — which is where Q2 comes in.

**Q2 — Does a better architecture distillation make the code more concise/elegant?**
**Yes — confirmed on the one component that completed, on a clean controlled A/B.**
Adding a single KISS/YAGNI section to the 44-line rules (v1 → v2, *same model, same
everything else*) cut the field parser from **165 to 89 LOC (−46%)**, and the Opus
A/B judge scored it ~82 vs ~69 on an independent 38-case battery (v2 38/38, v1
35/38). The decisive mechanism is the point: **v1's `5/2` bug lived *inside* its
extra decomposition** — a shallow `_parse_term → _expand_base` pass-through that
dropped the step argument — and the conciseness clause ("deep module, no shallow
single-use helper") made v2 collapse exactly that seam, which is what removed the
bug. Concise *and* more correct, by the same edit (finding 6). The caveat is
honest: task 3 derailed twice (finding 5), so this is one file, not the full 527→?
reduction — the *direction* is measured, the whole-impl *magnitude* is
extrapolation; and v2's brevity did cost one small, fail-safe magic-number
regression (`10**9` step ceiling).

A second, independent cut at Q2 came free from Q1's data: **Gemma, on the exact
same 44-line rules as Qwen v1, was already 174 lines leaner (353 vs 527) and scored
18 judge-points higher (84 vs 66).** So Qwen v1's verbosity was *both* a model trait
(Gemma proves a better model is more concise on identical rules) *and* a
rules-asymmetry (v2 proves better rules make the same model more concise). The two
levers are complementary, not redundant.

**The convergence claim — validated in direction.** More elegant → fewer tokens →
faster is real: at a fixed decode speed, wall-clock is token-count-bound, and v2's
conciseness rules cut the field-parser token count ~46%. So better distillation
improves elegance *and* speed by the same mechanism — the two questions genuinely
converge on one lever. But in *this* benchmark the largest speed win came from the
**model** (MoE, 2.3× decode), not the rules; distillation is the multiplier you
stack *on top* of the right model, not a substitute for it.

**The operational asterisk (finding 5).** "Fastest possible" in production must
also mean "finishes at all." A 27B dense model at temp 0.3 in non-thinking mode
reproducibly (2/2) looped on task 3's calendar reasoning and blew its token budget
— the same underlying over-reasoning that finding 3 showed on the thinking channel,
just relocated to code comments. A genuine fast-and-reliable config needs a
truncation-retry guard or a light repetition penalty (deliberately *not* applied
here, to keep v1's instrument intact for the one clean comparison). The MoE model
never hit this; that reliability edge is a further point in Gemma's favor.

---

### Bottom line

| | Correct? | Concise? | Fast? | Reliable? |
|-|-|-|-|-|
| **Opus 4.8** (reference bar) | 100%, + only one to catch `5/2` | **311 LOC (gold)** | not measured (not run through driver) | n/a |
| **Gemma4-26B-A4B** | 100% | 353 LOC, judge **84** | **fastest — 46.3 tok/s, 16.9 min** | test self-review unreliable (but impl correct) |
| **Qwen3.8-27B v1** | 100% (carries `5/2` bug) | 527 LOC, judge 66 | 19.8 tok/s, 21.6 min | clean run |
| **Qwen3.8-27B v2** (KISS rules) | field parser 38/38, `5/2` **fixed** | fields **−46%** vs v1, judge ~82 | — (derailed) | derailed 2/2 on task 3 |

For a fast, correct, *elegant* local build of a feature like this: **run an MoE
model in non-thinking mode, and give it a conciseness-tuned architecture
distillation** — the model buys you the speed, the distillation buys you the
elegance (and, here, a fixed bug), and the two compound. Just budget for a
reliability guard on the reasoning-heavy steps.

---

# Phase 2 — the SWE sweep: which local model is the best *implementer*?

Phase 1 tuned the *rules* against one model. That put the cart before the horse:
before asking "do better rules make the code more concise," ask the prior question —
**which local coder, run through the identical driver on the identical feature, is
the best implementer at all?** Phase 2 answers it. Five more local models, the
**same frozen `spec/SPEC.md`**, the **same 94-case hidden oracle + 2 efficiency
probes**, the **same deterministic red→green driver**, and the **same 44-line
baseline `spec/ARCH_RULES.md`**. Nothing varies but the model. The winner becomes
the target the deferred rules-light phase tunes against — you distill rules *for*
your best implementer, not in the abstract.

## The contestants (new lineup, instrument unchanged)

Four SWE-focused coders plus one open-weight generalist, chosen to fit a 64 GB
M-series box and, where the quant exists, at **6-bit parity**:

| Model | Params | Quant | Why it's here |
|-|-|-|-|
| **gpt-oss-20b** | 20B (MoE) | MXFP4 | OpenAI's open-weight generalist — the wildcard |
| **GLM-4.7-Flash** | ~30B (MoE) | 6-bit | current strong open coder |
| **Qwen3-Coder-30B** | 30B (MoE) | 6-bit | Qwen's dedicated coder line |
| **Devstral-Small-24B** | 24B (dense) | 6-bit | Mistral's SWE-tuned dense model |
| **DeepSeek-Coder-V2-Lite** | 16B (MoE) | 6-bit | newest DeepSeek *coder* that fits (V4 flagships are 837 G / 92 G even at 2-bit — impossible) |

**Quant policy — honest labelling.** 6-bit across the board where it exists. The one
exception, gpt-oss, ships as **MXFP4** (4-bit) — so it is a *different-tier* entry,
and the caveat is stated in whichever direction it cuts: a **win despite lower
fidelity is strong** (you beat 6-bit while quantised harder); a loss would be
*confounded*. As it happens gpt-oss wins, so the caveat runs in its favour.

**One harness change, proven neutral** (finding 10, below): gpt-oss emits the OpenAI
*harmony* channel format, which the driver's file-extraction couldn't read raw. A
general normalizer lifts its `final` channel out as the answer; it is a **no-op for
every instruct model** and was proven not to move any prior score.

## Results — the standings

94-case hidden oracle + 2 efficiency probes, each model run **alone** on the same
64 GB M-series box. Output tokens and decode speed are the driver's own
`run_meta.json`; `src_LOC` is `scripts/token_ledger.py` (raw line count — one
consistent tool across all models):

| Model | Quant | Hidden oracle | Eff probes | Output tokens | Decode tok/s | Wall (alone) | src_LOC |
|-|-|-|-|-|-|-|-|
| **gpt-oss-20b** 🏆 | MXFP4 | **89/94 = 94.7%** | **2/2** | 44,564 | **63.8** | **11.6 min** | 427 |
| GLM-4.7-Flash | 6-bit | 88/94 = 93.6% | **2/2** | 47,646 | 36.7 | 21.7 min | 339 |
| Qwen3-Coder-30B | 6-bit | 67/94 = 71.3% | 0/2 | 68,670 | 46.2 | 24.8 min | 362 |
| Devstral-Small-24B | 6-bit | 67/94 = 71.3% | 1/2 | 44,857 | 12.1 | 61.6 min | 423 |
| DeepSeek-Coder-V2-Lite | 6-bit | **0% — broken deliverable** | — | 23,791 | 59.2 | 6.7 min | 82† |
| Qwen3-Coder-Next-4bit | 4-bit | *gated on `task vram-raise`* | — | — | — | — | — |

† DeepSeek's 82-line total is itself the tell — 2 of its 4 source files were never
emitted (finding 9). The last row is a 4-bit Qwen variant (44.9 G) that needs the
Apple-Silicon wired-memory ceiling raised (sudo); it cannot change the result — a
4-bit Qwen cannot unseat a champion that already beats the *6-bit* Qwen-Coder by 23
oracle points.

**gpt-oss-20b is the champion, and it wins every axis at once:** highest correctness
(94.7%), both efficiency probes, fastest decode (63.8 tok/s), fastest wall-clock
(11.6 min), and near-lowest output-token cost — *and* the cleanest failure profile
(finding 7). GLM-4.7-Flash is a genuine, close second at 6-bit (93.6%, both probes).

**Decode speed confirms the memory-bandwidth law cleanly.** Decode ≈ bandwidth ÷
active-param-bytes, so MoEs (few active params) fly and dense models crawl: the four
MoEs clock 64 / 59 / 46 / 37 tok/s while the **dense** Devstral-24B is slowest in the
whole sweep at **12.1** — 5× slower than gpt-oss, and the reason its wall-clock (61.6
min) is worse than models that did far more work. For local inference, architecture
(MoE) buys more speed than parameter count costs.

## The findings

### 7. Same oracle score, materially different code — the multi-lens thesis, sharpened

Phase 1's finding 4 was that the mechanical oracle has blind spots. Phase 2 makes it
undeniable: **two models tied at exactly 67/94 on the oracle are judged nine points
apart on code quality**, because the oracle counts passing cases and cannot see
*how* the failures fail. Hand-verified against each running impl:

| | gpt-oss (89/94) | GLM (88/94) | Qwen-Coder (67/94) | Devstral (67/94) |
|-|-|-|-|-|
| Comma-unions (`1,5,7` — a SPEC feature) | ✓ | **❌ all raise** | ✓ | ✓ |
| Malformed `1-2-3` → domain error | ✓ CronParseError | **❌ leaks raw `ValueError`** | ✓ | ✓ |
| `matches()` weekday logic | ✓ | ✓ | **❌ off-by-2, every day wrong** | ✓ |
| `next_run` — numeric fields | ✓ | ✓ | ❌ crashes | ❌ can't backtrack |
| `next_run` — **day-of-week** | **❌ 5 cases** | ✓ | ❌ | ❌ |
| Efficiency probes | ✓ **2/2** | ✓ **2/2** | ❌ 0/2 (fast crash) | ⚠ 1/2 (fast raise) |

Four models, four oracle scores, **four distinct real weaknesses** — near-perfect
complements, and only hand-verification tells you which. The sharpest cuts:

- **Devstral 63 vs Qwen-Coder 54 at an identical 67/94.** The oracle can't see that
  Qwen-Coder's `matches()` is *also* broken — an off-by-two weekday conversion
  (`(w+6)%7` where it should be `(w+1)%7`, at two sites) corrupts **every** day-of-week
  match, not just next_run — while Devstral's `matches()` is correct. Nor that
  Qwen-Coder *crashes* with an uncaught `ValueError` where Devstral cleanly *raises*.
  Same headline number, materially worse code.
- **GLM's 93.6% is grammar-gapped.** It has the strongest *engine* in the field
  (correct dom/dow-OR, weekday, leap years, both efficiency probes at 0.44 s) — but
  it **never implemented comma-unions at all** (`15,45 * * * *` → `CronParseError:
  expected 1 field, got 2`), a required SPEC grammar production that gpt-oss and both
  "worse" 71.3% models all support. And it **leaks a raw `ValueError`** across the API
  boundary from four unguarded `int()` calls. High score, real holes.
- **The champion has the cleanest profile, not just the highest score.** gpt-oss is
  the only model that handles the *full grammar* (commas + error boundary, where GLM
  fails) **and** mostly nails the *hard next-run algorithm* (where the 71.3% models
  fail). Its lone weakness is narrow and specific — and, it turns out, one deletion
  deep (finding 8).

### 8. The champion is one 11-line deletion from 100% (root cause proven)

The Opus judge didn't stop at "dow next_run fails" — it root-caused it. gpt-oss's
`next_run` contains a **premature hardcoded special-case branch**
(`schedule.py:175-185`) that intercepts every day-of-week-only schedule and returns
"next Sunday at 00:00" — hardcoding cron-weekday 0 and `hour=0, minute=0`, never
consulting the actual dow set or the month. It is only *accidentally* right for
`0 0 * * 0`. The model's **own general dow path** (`schedule.py:232-235`) is already
correct; the shortcut is strictly harmful.

Verified non-destructively — copied the impl to scratch (**the pristine artifact was
never mutated**), deleted exactly those 11 lines, re-ran the *same* oracle:

| Arm | Same instrument (94 oracle + 2 probes) | Failures |
|-|-|-|
| Pristine gpt-oss (as the model shipped it) | 91/96 | 5 (all dow next_run) |
| Same code, the 11-line branch deleted | **96/96** | **0** |

Deleting the branch fixes **all five** failures and regresses **nothing**. So the
94.7% champion is a **single 11-line deletion from a perfect score** — and this is
the exact, concrete rules-light target: not "teach it day-of-week next_run" (it
already knows), but "don't emit a premature special-case shortcut in front of code
that's already correct." The mechanical oracle and the human judge point at the same
edit.

### 9. DeepSeek's 0% is a broken *deliverable*, not broken logic

DeepSeek-Coder-V2-Lite scored 0% — but the honest framing matters. Its package
**doesn't import**: `schedule.py` does `from .fields import parse_field` and
`from .errors import CronParseError`, but `fields.py` and `errors.py` **were never
written**. `from cron import …` raises `ImportError`, pytest collection fails, and
the 94-case oracle can't run at all.

**Proof it's the model, not the driver:** scanning `runs/deepseek/timeline.jsonl`,
across all 20 calls DeepSeek wrote `__init__.py`, `schedule.py`, and 3 test files —
but never `errors.py`/`fields.py`. The **other four models wrote all four source
files from the identical prompts.** The driver writes what a model emits; DeepSeek
emitted imports for modules it never created. So it is reported as **"0%, broken
deliverable (2 of 4 modules never emitted); its cron logic quality is untestable
because the package doesn't import"** — not "0% correct logic." (One controlled run
per model — retrying only DeepSeek would break the single-variable comparison.)
DeepSeek is the oldest and smallest model in the lineup (16B, 2024); this is a
capability floor, honestly recorded.

### 10. The harmony normalizer (harness change, proven neutral)

gpt-oss uses OpenAI's **harmony** format: mlx_vlm 0.6.15 returns the assistant turn
as `<|channel|>analysis<|message|>…<|end|><|start|>assistant<|channel|>final<|message|>…`
**inline in `content`**, leaving `reasoning_content` empty (probed live). Raw, this
defeats the driver's `FILE:`+fence extraction — the same class of derail as the
Phase-1 thinking-runaway (finding 3). Fix: `driver/driver.py`'s `normalize_harmony`
lifts the `final` channel out as the answer and the `analysis` channel into
`reasoning`. It activates **only** on `"<|channel|>" in content`, so it is a strict
**no-op** for every instruct model. Neutrality **proven**: all 80 saved responses of
the four already-scored instruct models contain **zero** harmony framing, so the
normalizer returns their content verbatim — their scores are untouched. gpt-oss also
gets `reasoning_effort=low` (a new `--reasoning-effort` driver flag): it can't be
fully non-thinking, so `low` is its minimal-CoT analog to the instruct lineup, and —
critically — keeps the analysis channel from eating the shared `max_tokens` budget
and truncating the code.

## Code-quality judge scores (Opus reviewing each contestant, 0-10 per axis)

One Opus judge per importable impl, fresh context, **re-verifying every claim by
running the code** with file:line citations (DeepSeek excluded — non-importable).
`LOC` here is each judge's pure-code count (docstrings/comments/blanks excluded),
which differs from the standings' raw `src_LOC`. Full scorecards in
`research/judge-scorecards.md`:

| Model | Oracle | Judge /100 | Naming | Docstr | Arch | Eleg | ErrH | Robust | LOC |
|-|-|-|-|-|-|-|-|-|-|
| **gpt-oss-20b** 🏆 | 94.7% | **65** | 8 | 7 | 5 | 5 | 8 | 6 | 236 |
| Devstral-24B | 71.3% | 63 | 8 | 7 | 6 | 6 | 8 | 3 | 209 |
| GLM-4.7-Flash | 93.6% | 62 | 9 | 7 | 6 | 6 | 4 | 5 | 150 |
| Qwen-Coder-30B | 71.3% | 54 | 8 | 8 | 6 | 3 | 5 | 3 | 182 |

Read the judge column *against* the oracle column — that divergence **is** the
result (finding 7). Two more reads worth stating plainly: **23 oracle points buy 2
judge points** (gpt-oss 94.7% vs Devstral 71.3% → 65 vs 63) because both are
excellent *parsers* undone by *next_run*, and the judge rewards the craft they share.
And the champion's own 65 is *depressed by the very bug of finding 8* — its
Architecture (5) and Elegance (5) both cite the 137-line `next_run` monolith whose
11-line branch is the defect; delete it and the judged quality rises too.

## The actual thesis — frontier tokens saved

This is the point of the whole exercise. **The driver is deterministic Python — the
red→green loop is code, not a model.** The only model calls in the entire feature
build are the *local* model's; Opus (the frontier) spends **zero** tokens
implementing anything. So the marginal frontier cost of building this feature through
the driver is **0 output tokens** — and every output token the local model produced
is a frontier output token **not spent**:

| Model | Local output tokens | Frontier output tokens | Result |
|-|-|-|-|
| **gpt-oss-20b** 🏆 | **44,564** | **0** | 94.7% correct (100% minus an 11-line branch) |
| GLM-4.7-Flash | 47,646 | 0 | 93.6% correct |
| Devstral-24B | 44,857 | 0 | 71.3% |
| Qwen-Coder-30B | 68,670 | 0 | 71.3% |
| DeepSeek-V2-Lite | 23,791 | 0 | broken deliverable |

The champion turned **~44.6 K output tokens of local compute into a 94.7%-correct
implementation of a real, edge-case-heavy feature — at zero frontier output spend.**
Apply your own frontier output rate to 44.6 K tokens per feature of this size to get
the per-feature saving; the ratio is what generalises, and here the ratio is *all* of
it. The correctness gap to 100% is 5.3 oracle points — which finding 8 showed is one
11-line deletion wide.

**The honest caveat.** This measures the **single-shot driver floor**, not a full
agentic loop. A real agentic harness (the deferred rules-light phase) would spend more
per model *and* more on the frontier baseline it's compared against — so the
zero-frontier-cost property holds structurally (the driver stays deterministic), but
the *absolute* token figures will grow with loop depth. What Phase 2 establishes is
the **denominator**: the best local implementer to point that loop at is
**gpt-oss-20b**, and the single highest-value rule to give it is "no premature
special-case shortcuts."

### Phase 2 bottom line

| | Correct? | Clean profile? | Fast? | Token cost | Frontier cost |
|-|-|-|-|-|-|
| **gpt-oss-20b** 🏆 | **94.7%** (100% −11 lines) | only dow next_run fails — full grammar + hard next_run | **fastest: 63.8 tok/s, 11.6 min** | 44.6 K | **$0** |
| GLM-4.7-Flash | 93.6% | strong engine; **no comma-unions**, ValueError leak | 36.7 tok/s, 21.7 min | 47.6 K | $0 |
| Devstral-24B | 71.3% | best errors; next_run can't backtrack | slowest: 12.1 tok/s (dense) | 44.9 K | $0 |
| Qwen-Coder-30B | 71.3% | `matches` *also* broken; crashes | 46.2 tok/s, 24.8 min | 68.7 K | $0 |
| DeepSeek-V2-Lite | 0% | 2 of 4 files never emitted | 59.2 tok/s | 23.8 K | $0 |

For the best local *implementer* of a real, edge-case-heavy feature under a
deterministic TDD driver: **run gpt-oss-20b** (OpenAI open-weight, MXFP4). It wins
correctness, speed, token economy, *and* code-cleanliness at once, beats the 6-bit
field from a harder quant tier, and lands one 11-line deletion from a perfect oracle
score — the concrete target for the rules-light phase to come. And it does it for
**zero frontier output tokens**, which was the whole question.
