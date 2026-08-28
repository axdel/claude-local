# Resource Ownership

## Ownership

| Resource | Area | Owner | Consumers | Enforcement | Status | Superseded By |
|-|-|-|-|-|-|-|
| LocalEconomyRecord | telemetry | telemetry module | loop (reads for LoopResult), contract (maps status) | single writer; orchestrator half owned externally | active |  |
| benchmark-worktree-lifecycle | model-evaluation | BenchmarkDriver | implement (pre-populated worktree), benchmark oracles | one managed temp directory per case; populate before implement and remove on every exit | active |  |
| best-passing-snapshot | loop | snapshot store | loop (restore on exit) | restore constrained by keep_only | active |  |
| impl-file | editing | edits.apply_files under keep_only | loop (reads), runner (reads) | paths.resolve_within realpath containment; oracle test excluded from the writable set | active |  |
| keep_only | editing | paths.resolve_within | edits (apply), snapshot (restore) | single containment rule; rejects absolutes, resolves realpath, refuses symlinks, requires worktree containment | active |  |
| oracle-sandbox | oracle | sandbox.sandboxed_spawn | runner (default spawn) | deny-default SBPL via sandbox-exec + post-fork setrlimit CPU/FSIZE + wall-clock SIGKILL of the process group; writable only within the caller-supplied box, no network, secrets dropped; fail-closed if sandbox-exec absent | active |  |
| stable-prefix | prompt | PromptBuilder | loop (builds once), client (sends) | PromptBuilder is the single writer; feedback remains isolated to the tail. | active |  |
