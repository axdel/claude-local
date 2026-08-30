# Resource Ownership

## Ownership

| Resource | Area | Owner | Consumers | Enforcement | Status | Superseded By |
|-|-|-|-|-|-|-|
| LocalEconomyRecord | telemetry | telemetry module | loop (reads for LoopResult), contract (maps status) | single writer; orchestrator half owned externally | active |  |
| benchmark-worktree-lifecycle | model-evaluation | BenchmarkDriver | implement (pre-populated worktree), benchmark oracles | one managed temp directory per case; populate before implement and remove on every exit | active |  |
| best-passing-snapshot | loop | snapshot store | loop (restore on exit) | restore constrained by keep_only | active |  |
| golden-authentication | model-evaluation | AuthService | security dependencies and benchmark oracles | password records and token signing centralized; accepted passwords reference validate_password; token carries user ID only; repository reload owns current role | active |  |
| golden-cron-semantics | model-evaluation | app.cron | ScheduleService and benchmark oracles | one parser and CronSchedule matcher; explicit UTC references; bounded calendar search; D-BENCH-008 | active |  |
| golden-database-schema | model-evaluation | app.db.initialize_database | repositories, application factory, benchmark oracles | DatabasePath is canonical; db owns DDL and cross-thread connection policy; app lifespan closes one connection; fresh path per case | active |  |
| golden-password-domain | model-evaluation | app.schemas.validate_password | UserCreate and AuthService | one validator rejects U+0000 before PBKDF2; D-BENCH-009 | active |  |
| golden-schedule-data-access | model-evaluation | ScheduleRepository | ScheduleService and benchmark oracles | parameterized schedule SQL; typed ScheduleRecord; field-local updates; no schedule SQL above repository | active |  |
| golden-schedule-policy | model-evaluation | ScheduleService | routers and benchmark oracles | one role-aware owner for schedule CRUD and disabled behavior; denied or missing operations never write; D-BENCH-010 | active |  |
| golden-user-data-access | model-evaluation | UserRepository | AuthService and benchmark oracles | parameterized user SQL; typed UserRecord; duplicate username translation; no user SQL above repository | active |  |
| impl-file | editing | edits.apply_file under keep_only | loop (reads), runner (reads) | paths.resolve_within realpath containment; oracle test excluded from writable set | active |  |
| keep_only | editing | paths.resolve_within | edits (apply), snapshot (restore) | single containment rule; rejects absolutes, resolves realpath, refuses symlinks, requires worktree containment | active |  |
| model-id-slug | telemetry | telemetry.slug_model_id | LocalEconomyRecord.write, Scorecard.write | one filename-safe slug for model ids; a shared literal has one owner (Deduplication Discipline) | active |  |
| model-reply-extraction | editing | edits.extract_file | loop (supplies GenerationResult.is_incomplete, then applies) | one WholeFileReply with validated UTF-8 bytes; exact after terminal stop; partial only for incomplete generation; keep_only validates target | active |  |
| oracle-sandbox | oracle | sandbox.sandboxed_spawn | runner (default spawn) | deny-default SBPL; task/runtime reads only; write-box-only writes; no network; secret-free env; file-backed diagnostic tails; CPU/FSIZE and process-group timeout caps; fail-closed without sandbox-exec | active |  |
| produced-edit-state | loop | LoopResult.has_scored_edit | entrypoint (maps Outcome code and files_changed) | derived from the scored best snapshot; seeded disk presence never counts | active |  |
| scorecard | model-evaluation | Scorer (score_cases, Scorecard.write) | benchmark run script, cross-model comparison | single writer; totals derived from CaseResult economy records, never re-counted; filename via telemetry.slug_model_id | active |  |
| stable-prefix | prompt | PromptBuilder | loop (builds once), client (sends) | PromptBuilder is the single writer; feedback remains isolated to the tail. | active |  |
