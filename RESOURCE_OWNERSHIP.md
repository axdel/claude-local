# Resource Ownership

## Ownership

| Resource | Area | Owner | Consumers | Enforcement | Status | Superseded By |
|-|-|-|-|-|-|-|
| LocalEconomyRecord | telemetry | telemetry module | loop (reads for LoopResult), contract (maps status) | single writer; orchestrator half owned externally | active |  |
| benchmark-worktree-lifecycle | model-evaluation | BenchmarkDriver | implement (pre-populated worktree), benchmark oracles | one managed temp directory per case; populate before implement and remove on every exit | active |  |
| best-passing-snapshot | loop | snapshot store | loop (restore on exit) | restore constrained by keep_only | active |  |
| golden-data-access | model-evaluation | UserRepository and ScheduleRepository | golden services and benchmark oracles | parameterized SQL; typed records; no SQL above repositories | active |  |
| golden-database-schema | model-evaluation | app.db.initialize_database | repositories, application factory, benchmark oracles | DatabasePath is canonical; db owns DDL and cross-thread connection policy; app lifespan closes one connection; fresh path per case | active |  |
| impl-file | editing | edits.apply_file under keep_only | loop (reads), runner (reads) | paths.resolve_within realpath containment; oracle test excluded from writable set | active |  |
| keep_only | editing | paths.resolve_within | edits (apply), snapshot (restore) | single containment rule; rejects absolutes, resolves realpath, refuses symlinks, requires worktree containment | active |  |
| model-reply-extraction | editing | edits.extract_file | loop (supplies GenerationResult.is_incomplete, then applies) | one WholeFileReply with validated UTF-8 bytes; exact after terminal stop; partial only for incomplete generation; keep_only validates target | active |  |
| oracle-sandbox | oracle | sandbox.sandboxed_spawn | runner (default spawn) | deny-default SBPL; task/runtime reads only; write-box-only writes; no network; secret-free env; file-backed diagnostic tails; CPU/FSIZE and process-group timeout caps; fail-closed without sandbox-exec | active |  |
| produced-edit-state | loop | LoopResult.has_scored_edit | entrypoint (maps Outcome code and files_changed) | derived from the scored best snapshot; seeded disk presence never counts | active |  |
| stable-prefix | prompt | PromptBuilder | loop (builds once), client (sends) | PromptBuilder is the single writer; feedback remains isolated to the tail. | active |  |
