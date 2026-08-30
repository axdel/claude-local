# Changelog

## 2026-08-30

### feat: add the local-model evaluation benchmark — golden app, case ladder, harness, and CLI ([PR #2](https://github.com/axdel/claude-local/pull/2))
A reusable instrument that scores free local models as test-first implementers: a seven-case ladder blanks one file at a time of a half-finished FastAPI+SQLite golden app, and a candidate model driven through an OpenAI-compatible server is graded against orchestrator-owned immutable oracles into one comparable per-model scorecard.

- [`0c4053b`](https://github.com/axdel/claude-local/commit/0c4053b) Use the canonical "golden app" name in the last three prose sites
- [`4c5c19b`](https://github.com/axdel/claude-local/commit/4c5c19b) Conform benchmark names and durable records to the shipped code
- [`c2a2f1b`](https://github.com/axdel/claude-local/commit/c2a2f1b) Reconcile durable artifacts with the shipped standing benchmark
- [`2c349b8`](https://github.com/axdel/claude-local/commit/2c349b8) Withhold the /dev/tty write grant from the oracle sandbox
- [`9cbe5dd`](https://github.com/axdel/claude-local/commit/9cbe5dd) Share the recording replay backend across loop tests
- [`3c7fe3a`](https://github.com/axdel/claude-local/commit/3c7fe3a) Extract shared golden_impl helper for benchmark case tests
- [`34b120f`](https://github.com/axdel/claude-local/commit/34b120f) Index the schedules owner_id foreign key in the golden app
- [`614514f`](https://github.com/axdel/claude-local/commit/614514f) Tidy two idioms in the golden reference app
- [`1664db2`](https://github.com/axdel/claude-local/commit/1664db2) Document the scorecard's length_capped and fault rung fields
- [`0096266`](https://github.com/axdel/claude-local/commit/0096266) Give the RBAC benchmark case its two missing import neighbors
- [`6a8a59d`](https://github.com/axdel/claude-local/commit/6a8a59d) Surface harness faults and length-cap counts in the benchmark CLI
- [`12fbf56`](https://github.com/axdel/claude-local/commit/12fbf56) Name the byte-count comparison and record why it avoids int()
- [`d6f53e5`](https://github.com/axdel/claude-local/commit/d6f53e5) Expose slug_model_id and TARGET_FILE_LABEL on the public API
- [`a9d80d3`](https://github.com/axdel/claude-local/commit/a9d80d3) Bound SSE decoder memory on the data-block axis and scan bytes once
- [`8ab622b`](https://github.com/axdel/claude-local/commit/8ab622b) Benchmark and example default base URL must be the bare server root
- [`eae6f10`](https://github.com/axdel/claude-local/commit/eae6f10) Add benchmark run script and README for the model-eval suite
- [`db97258`](https://github.com/axdel/claude-local/commit/db97258) Add benchmark suite scorer with a comparable per-model scorecard
- [`f93e484`](https://github.com/axdel/claude-local/commit/f93e484) Add full benchmark suite runner over the whole case ladder
- [`35ce301`](https://github.com/axdel/claude-local/commit/35ce301) Add benchmark case ladder rungs 6-7 (schedule service, routers)
- [`92c2a92`](https://github.com/axdel/claude-local/commit/92c2a92) Add benchmark case ladder rungs 4-5 (auth service, RBAC)
- [`5088b98`](https://github.com/axdel/claude-local/commit/5088b98) Add benchmark case ladder rungs 1-3 with data-driven case loader
- [`af47bfb`](https://github.com/axdel/claude-local/commit/af47bfb) Wire golden-app HTTP routers and add the composed-app oracle
- [`c0faea0`](https://github.com/axdel/claude-local/commit/c0faea0) Add golden app services and security layer
- [`6ea9ea7`](https://github.com/axdel/claude-local/commit/6ea9ea7) Add golden schedule data layer
- [`fabbbb1`](https://github.com/axdel/claude-local/commit/fabbbb1) Report only scored model edits
- [`cc41f38`](https://github.com/axdel/claude-local/commit/cc41f38) Frame whole-file replies by UTF-8 byte length
- [`e514a6e`](https://github.com/axdel/claude-local/commit/e514a6e) Feed bounded oracle diagnostics into repairs
- [`6778638`](https://github.com/axdel/claude-local/commit/6778638) Add benchmark walking skeleton
- [`907d43c`](https://github.com/axdel/claude-local/commit/907d43c) Document read-only task context files
- [`9cf1747`](https://github.com/axdel/claude-local/commit/9cf1747) Add read-only context files to task prompts

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

