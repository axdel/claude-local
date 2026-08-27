# Canonical Glossary

## Concepts

| Canonical Name | Domain | Concept | Rejected Aliases | Notes | Status | Superseded By |
|-|-|-|-|-|-|-|
| Budget | generation | The value object bounding one task: max_attempts, max_tokens, timeout_s. | limits, quota, caps | The hard token cap is the real decode bound (D-PERF-001). | active |  |
| DerailGuard | generation | The bounded-decode enforcer: repetition-loop detector plus hard token cap plus wall-clock timeout that aborts a runaway generation. | watchdog, limiter, safety-net | Non-thinking by default (D-LOOP-002). | active |  |
| LocalEconomyRecord | telemetry | The local half of the per-task economy record: model, calls, completion tokens, model-seconds, tokens-per-second, status, attempts. | metrics, stats, usage-blob | Orchestrator half owned by the driving orchestrator (D-TELEMETRY-001). | active |  |
| REPAIR | loop | The retry step: on a failing attempt, feed distilled failure back to the model without clobbering the best-passing snapshot. | retry, fix-mode, feedback-loop | Distinct from a naive clobber-latest retry. | active |  |
| ReplayBackend | transport | The stub Backend that replays captured SSE bytes verbatim, driving the loop deterministically with no model download. | stub, mock, canned-backend | Raises ReplayExhausted on over-read. | active |  |
| attempt | loop | One bounded model generation plus apply plus oracle run; the unit the loop repeats, capped by Budget.max_attempts. | iteration, try, round | Canonical over 'iteration'; the code identifier is max_attempts. | active |  |
| backend | transport | The Backend protocol yielding raw SSE byte chunks from a generation call; HttpxBackend talks to the live server, ReplayBackend replays captured bytes. | provider, transport, driver | Seam at raw SSE bytes (D-BACKEND-001). | active |  |
| best-passing-snapshot | loop | The highest-scoring attempt's src subtree, retained so REPAIR feedback never leaves the loop worse than a prior passing attempt. | checkpoint, savepoint, best-snapshot | Ranked passed desc, errors asc, failed asc, index asc. | active |  |
| distilled-feedback | prompt | The byte-capped, abs-path-stripped failure summary fed back to the model in REPAIR mode; the only part of the prompt that varies across attempts. | feedback, error-summary | Owned by prompt builder; isolated to the tail to preserve the stable prefix (D-PROMPT-001). | active |  |
| implement | entry-point | The owned public front door implement(spec, *, base_url, model, ...) -> Outcome that wires and drives the loop for one task; Outcome carries status, code, impl_path, files_changed, and the LocalEconomyRecord. | contract, builder-adapter, adapter, builder-api, plugin-interface, build | The single composition root (D-ENTRYPOINT-001). | active |  |
| keep_only | editing | The writable allowlist: only the permitted impl path may be written, enforced by realpath containment under the worktree root. | allowlist, whitelist, path-guard | The oracle test is never in the writable set. | active |  |
| non-thinking | generation | Generation with chain-of-thought disabled by default under a hard thinking cap, keeping decode bounded for whole-file edit tasks. | no-cot, direct-mode | See D-LOOP-002. | active |  |
| oracle-sandbox | oracle | The kernel-enforced confinement wrapping the pytest subprocess that imports and executes untrusted model code: macOS sandbox-exec under a deny-default SBPL profile, writable only within the disposable report box, no network, with setrlimit CPU/file-size caps and a wall-clock process-group kill. | jail, cage, isolation-box | Fail-closed - absent sandbox-exec the spawn raises rather than running unconfined (D-SANDBOX-001). | active |  |
| oracle-test | oracle | The frontier-authored failing test that defines DONE; the model may never write it, and green means the task is complete. | spec-test, acceptance-test, gold-test | Immutable; pinned in the stable prefix (D-LOOP-001). | active |  |
| repetition-loop | generation | A stuck decode state where the model emits the same large-n token slice consecutively; the derail guard's REPETITION trigger. | degeneration, stuck-state | Detected over a fixed deque window after warmup. | active |  |
| rules_card | prompt | The static, token-budgeted engineering-rules card injected as a stable system prefix, byte-identical across a task's calls for KV-cache reuse. | system-prompt, guidelines, instructions | Static committed asset: src/claude_local/rules_card.md. | active |  |
| stable-prefix | prompt | The byte-identical prompt head (rules card + spec + immutable test) assembled once per task so only the feedback tail varies, preserving the server prefill cache. | prefix, system-prefix | Owned by prompt builder (D-PROMPT-001). | active |  |

## Track Prefixes

| Track | Branch Prefix | Notes | Status | Superseded By |
|-|-|-|-|-|

## State Locations

| State | Path Pattern | Status | Superseded By |
|-|-|-|-|
