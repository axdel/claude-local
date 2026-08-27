# Changelog

## 2026-08-27

### feat: loop engine — deterministic red→green driver for local-model implementers ([PR #1](https://github.com/axdel/claude-local/pull/1))
Deterministic red→green loop driving a local OpenAI-compatible model as a supervised, test-first implementer: metered streaming client, whole-file edit applier, kernel-sandboxed oracle, derail guard, KV-cacheable prompt, and per-task economy telemetry. Single public entry point `implement()`; httpx-only runtime.

- [`06f80ef`](https://github.com/axdel/claude-local/commit/06f80ef) Cover the untrusted-input failure paths in the sandbox and SSE decoder
- [`bbc0401`](https://github.com/axdel/claude-local/commit/bbc0401) Extract _from_run helper to DRY the telemetry aggregation tests
- [`b8e9d51`](https://github.com/axdel/claude-local/commit/b8e9d51) Add a property test for SSE decoder chunking-invariance
- [`06989d1`](https://github.com/axdel/claude-local/commit/06989d1) Record decision on the forgeable oracle verdict channel
- [`7410f50`](https://github.com/axdel/claude-local/commit/7410f50) Extract shared test fixtures and SSE wire builders
- [`79779d8`](https://github.com/axdel/claude-local/commit/79779d8) Guard the quicksort example's decode-rate line against a None mean
- [`22613df`](https://github.com/axdel/claude-local/commit/22613df) Bound the SSE decode buffer in memory and time
- [`a5af5b7`](https://github.com/axdel/claude-local/commit/a5af5b7) Record the server's finish_reason and count length-capped generations
- [`08a2317`](https://github.com/axdel/claude-local/commit/08a2317) Lock in synchronous transport release on aborted generation
- [`74754bc`](https://github.com/axdel/claude-local/commit/74754bc) Enforce BOUNDARY_MAP one-way flow with an import-linter layers contract
- [`de7faa2`](https://github.com/axdel/claude-local/commit/de7faa2) Confine oracle sandbox via inline -p profile with race-free teardown
- [`56e6be9`](https://github.com/axdel/claude-local/commit/56e6be9) Translate an unreachable/error-status model server into BackendUnavailable
- [`612eb79`](https://github.com/axdel/claude-local/commit/612eb79) Surface upstream server faults as a FAULTED terminal outcome
- [`754a1c1`](https://github.com/axdel/claude-local/commit/754a1c1) Make claude-local a standalone, importable public tool
- [`78f6f2b`](https://github.com/axdel/claude-local/commit/78f6f2b) Add the quicksort example skill for driving implement() locally
- [`8517749`](https://github.com/axdel/claude-local/commit/8517749) Add implement() entry point, retire the contract stub
- [`81e230b`](https://github.com/axdel/claude-local/commit/81e230b) Confine the oracle subprocess with macOS sandbox-exec
- [`11c8ea1`](https://github.com/axdel/claude-local/commit/11c8ea1) Add loop-engine HTML explainer with hand-authored SVG diagrams
- [`0f9c3d1`](https://github.com/axdel/claude-local/commit/0f9c3d1) End-to-end loop ceremony, plus the stale-bytecode oracle fix it surfaced
- [`499f3e1`](https://github.com/axdel/claude-local/commit/499f3e1) Add the loop orchestrator spine driving local models red→green
- [`287b77a`](https://github.com/axdel/claude-local/commit/287b77a) Add builder-adapter contract stub with Status to BuildStatus mapping
- [`74e8a77`](https://github.com/axdel/claude-local/commit/74e8a77) Add local economy record — telemetry aggregation of a task run
- [`8454491`](https://github.com/axdel/claude-local/commit/8454491) Add prompt assembler — KV-cacheable prefix + bounded feedback tail
- [`b4d6944`](https://github.com/axdel/claude-local/commit/b4d6944) Add best-passing snapshot store for the repair loop
- [`59768a0`](https://github.com/axdel/claude-local/commit/59768a0) Add pytest JUnit-XML oracle so 'green means done' is trustworthy
- [`ac112d6`](https://github.com/axdel/claude-local/commit/ac112d6) Add edits — whole-file extraction + atomic containment writes
- [`dd74be9`](https://github.com/axdel/claude-local/commit/dd74be9) Add ModelClient — one metered generation over the streaming backend
- [`7da92e7`](https://github.com/axdel/claude-local/commit/7da92e7) Add DerailGuard — cut a stuck local decode mid-stream
- [`0ea98ac`](https://github.com/axdel/claude-local/commit/0ea98ac) Add the transport-seam backends (Replay + Httpx)
- [`536f63d`](https://github.com/axdel/claude-local/commit/536f63d) Add streaming SSE decoder for the model wire
- [`90b3f0d`](https://github.com/axdel/claude-local/commit/90b3f0d) Add realpath containment — the loop's write-safety boundary
- [`c518205`](https://github.com/axdel/claude-local/commit/c518205) Add shared value objects — Status, Budget, TaskSpec

