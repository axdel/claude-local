# Boundary Map

## Import Rules

| Module | Target | Rule | Notes | Status | Superseded By |
|-|-|-|-|-|-|
| __init__ | backend | may-import | Public front door re-exports BackendUnavailable, the transport failure a caller must distinguish from a model that merely failed the task. | active |  |
| __init__ | entrypoint | may-import | Public front door re-exports implement and Outcome. | active |  |
| __init__ | loop | may-import | Public front door re-exports AttemptProgress, the live event implement's on_attempt observer receives. | active |  |
| __init__ | model_registry | may-import | Public front door re-exports generation_params_from_json, the declaration reader every out-of-package front door needs: the CLI, the bundled example, and the benchmark each take --generation-params, and a parser at each would be one format with three readers. | active |  |
| __init__ | prompt | may-import | Public front door re-exports TARGET_FILE_LABEL, the header a caller prints to describe the frame it expects back. | active |  |
| __init__ | runner | may-import | Public front door re-exports OracleError, raised when the immutable test itself is broken rather than merely red. | active |  |
| __init__ | sandbox | may-import | Public front door re-exports SandboxUnavailable, the host fault that is not a model failure. | active |  |
| __init__ | session | may-import | Public front door re-exports model_session, the interactive surface. | active |  |
| __init__ | telemetry | may-import | Public front door re-exports slug_model_id, the one filename-safe model slug every artifact writer shares. | active |  |
| __init__ | types | may-import | Public front door re-exports task value objects. | active |  |
| __main__ | cli | may-import | The python -m shim resolves the same front door as the installed console script. | active |  |
| backend | httpx | may-import | Only external transport dependency. | active |  |
| backend | types | may-import | Transport consumes Budget. | active |  |
| benchmarks | claude_local | may-import | Downstream benchmark consumes the top-level public package API, plus claude_local.paths for the shared path-shape rule (its own row). | active |  |
| benchmarks | claude_local.paths | may-import | The one submodule reached past the public package API: the harness writes fixture trees, so it must apply the same path-shape rule the loop applies, not a second copy of it. | active |  |
| claude_local | benchmarks | must-not-import | Reusable loop never depends on benchmark subjects or harness code. | active |  |
| claude_local | scripts | must-not-import | Dev tooling is a leaf consumer above the package; the shipped library never depends on it. | active |  |
| cli | backend | may-import | Catches BackendUnavailable so a broken host exits apart from a failed task. | active |  |
| cli | entrypoint | may-import | Adapts one invocation to a TaskSpec; implement stays the composition root. | active |  |
| cli | model_registry | may-import | Reads generation_params_from_json only, to parse its --generation-params flag. This does NOT soften the never-serve rule above it: the registry resolves names and declarations, it starts nothing, and the CLI still takes its server from --base-url alone. | active |  |
| cli | model_server | must-not-import | The CLI never serves. A dispatched child runs under a profile granting outbound loopback but not network-bind, so it cannot listen; --base-url must name an already-running server. The layers contract would permit this import, so only this rule bars it. | active |  |
| cli | paths | may-import | The machine front door translates KeepOnlyViolation into its own rejected-task exit code, so it names the containment error. | active |  |
| cli | runner | may-import | Catches OracleError; a broken oracle produced no verdict, so no task outcome exists. | active |  |
| cli | sandbox | may-import | Catches SandboxUnavailable; a host without the kernel sandbox is a fault, not a status. | active |  |
| cli | types | may-import | Builds TaskSpec, Budget, and ContextFile from the stdin task envelope. | active |  |
| client | backend | may-import | Streams raw SSE bytes from the transport. | active |  |
| client | derail | may-import | Watches decode for repetition/cap/timeout. | active |  |
| client | harmony | may-import | Normalizes a leaked channel-transcript at the one place reply text is assembled. | active |  |
| client | sse | may-import | Decodes raw bytes via the shared decoder. | active |  |
| client | types | may-import | Consumes Budget and value objects. | active |  |
| derail | types | may-import | Guard consumes Budget. | active |  |
| edits | paths | may-import | Writes only through realpath containment. | active |  |
| entrypoint | backend | may-import | Constructs HttpxBackend, the transport to the model server. | active |  |
| entrypoint | client | may-import | Wraps the backend in ModelClient. | active |  |
| entrypoint | derail | may-import | Type-only under TYPE_CHECKING: DerailReason, mapped into the Outcome a caller reads. | active |  |
| entrypoint | httpx | may-import | Constructs the keep-alive httpx.Client for an owned-lifecycle call. | active |  |
| entrypoint | loop | may-import | Constructs and runs the Loop, the red->green driver. | active |  |
| entrypoint | paths | may-import | Validates the caller's impl_path shape before any resource is acquired; a path that climbs out of its declared subtree stays contained under the worktree root, so realpath containment alone cannot refuse it. | active |  |
| entrypoint | prompt | may-import | Builds PromptBuilder from the rules card. | active |  |
| entrypoint | runner | may-import | Constructs TestRunner over the budget-bound sandbox spawn. | active |  |
| entrypoint | sandbox | may-import | Binds the oracle budget timeout into sandboxed_spawn. | active |  |
| entrypoint | snapshot | may-import | Constructs SnapshotStore over the writable subtree. | active |  |
| entrypoint | telemetry | may-import | Surfaces LocalEconomyRecord on the Outcome. | active |  |
| entrypoint | types | may-import | Consumes TaskSpec and Status. | active |  |
| loop | client | may-import | Drives one generation per attempt. | active |  |
| loop | derail | may-import | Type-only under TYPE_CHECKING: DerailReason, carried onto the attempt record. | active |  |
| loop | edits | may-import | Applies whole-file blocks. | active |  |
| loop | paths | may-import | Raises KeepOnlyViolation from a mis-aimed whole-file edit. | active |  |
| loop | prompt | may-import | Builds the stable prefix once per task. | active |  |
| loop | runner | may-import | Runs the oracle after each attempt. | active |  |
| loop | snapshot | may-import | Keeps the best-passing snapshot. | active |  |
| loop | telemetry | may-import | Writes the local economy record. | active |  |
| loop | types | may-import | Returns LoopResult. | active |  |
| model_server | httpx | may-import | Readiness polling and the served-model probe speak HTTP, the one external transport this package allows. | active |  |
| model_server | model_registry | may-import | Turns a resolved registry row into a launch command; store paths keep the download branch unreachable. | active |  |
| model_server | sandbox | must-not-import | The oracle profile denies network, so a listening server cannot run under it; hosting one would widen the sandbox that contains untrusted model code. Separate spawn paths by design. | active |  |
| prompt | runner | may-import | Distills feedback over the oracle TestScore. | active |  |
| prompt | types | may-import | Assembles the stable prefix from TaskSpec. | active |  |
| runner | sandbox | may-import | Runs the oracle under kernel confinement. | active |  |
| scripts | benchmarks.harness.style | may-import | score_style re-lints a produced-code tree through the harness owner of the rule set, never a second ruff invocation. | active |  |
| scripts | claude_local.backend | may-import | probe_reply, measure_first_byte and capture_sse_fixture drive the transport directly to measure it. | active |  |
| scripts | claude_local.client | may-import | probe_reply reassembles the loop's wiring one layer at a time, so it holds the client itself. | active |  |
| scripts | claude_local.edits | may-import | probe_reply reports whether a raw reply carried an applicable frame, using the same parser the loop uses. | active |  |
| scripts | claude_local.model_registry | may-import | Dev-tooling probes above the package reach internal modules by design: their job is to exercise one seam in isolation, which the public API deliberately hides. Seven scripts resolve a model row before serving it. | active |  |
| scripts | claude_local.model_server | may-import | Six scripts serve a resolved model through the owning context manager rather than assuming a running server. | active |  |
| scripts | claude_local.prompt | may-import | probe_reply builds the stable prefix through its owner rather than restating it. | active |  |
| scripts | claude_local.sandbox | may-import | The same three run the oracle under the real confinement, never an unconfined stand-in. | active |  |
| scripts | claude_local.sse | may-import | probe_reply reads decoded stream events to show what the model emitted before any parsing. | active |  |
| scripts | claude_local.types | may-import | The same three compose a TaskSpec and Budget by hand to hold one variable still. | active |  |
| session | backend | may-import | Constructs HttpxBackend against the id the running server advertises. | active |  |
| session | client | may-import | Wraps the backend in ModelClient, kept warm for the session's whole lifetime. | active |  |
| session | httpx | may-import | Constructs the one keep-alive httpx.Client the session reuses across turns. | active |  |
| session | model_registry | may-import | Resolves a registered name to its store path before anything is spawned. | active |  |
| session | model_server | may-import | Composes ModelServer.for_model and .running; never spawns a process directly. | active |  |
| session | sandbox | may-import | Reads DEFAULT_ORACLE_TIMEOUT_S for the Budget field a chat turn never spends. | active |  |
| session | types | may-import | The interactive front door builds a Budget per turn, the same task value object implement takes. | active |  |
| snapshot | paths | may-import | Restore constrained by keep_only containment. | active |  |
| snapshot | runner | may-import | Ranks attempts by the oracle score. | active |  |
| telemetry | client | may-import | Aggregates GenerationResult token usage into the record. | active |  |
| telemetry | types | may-import | Aggregates the local economy record. | active |  |

## Error Ownership

| Layer | Raises | Catches and Translates | Status | Superseded By |
|-|-|-|-|-|
| backend | BackendUnavailable | httpx.HTTPStatusError, httpx.ReadTimeout and httpx.RequestError. No httpx type escapes this layer: a caller distinguishes an unreachable server from a model that merely failed the task, and needs no transport vocabulary to do it. | active |  |
| cli | TaskRejected | The outermost boundary, where every error above becomes a process exit code. TaskRejected is a malformed envelope (EXIT_REJECTED_TASK); BackendUnavailable, SandboxUnavailable and OracleError are a broken host (EXIT_HARNESS_FAULT); a loop that ran maps its Status. A broken host is never reported as a model that failed. | active |  |
| edits | KeepOnlyViolation | Nothing. A reply naming a path outside the permitted impl path is refused at the point of application, so the containment rule has one enforcement site and no recovery path above it. | active |  |
| entrypoint | Nothing | Nothing. A loop that fails returns an Outcome carrying a Status, so the library's own front door reports failure as a value a caller reads rather than an exception it must handle. | active |  |
| runner | OracleError | SandboxKilled and a missing or unparseable JUnit report become OracleError. It marks the immutable test as broken, which is a distinct answer from the test being red. | active |  |
| sandbox | SandboxUnavailable, SandboxKilled | subprocess.TimeoutExpired becomes SandboxKilled, naming the oracle deadline rather than the spawn mechanism. A host that cannot confine at all raises SandboxUnavailable, which is a broken host and never a model failure. | active |  |

## Layer Purity

| Layer | Owns | Must NOT Contain | Status | Superseded By |
|-|-|-|-|-|
| benchmark-harness | case loading, scratch-worktree assembly, implement invocation, result aggregation | golden app business logic or claude_local internals | active |  |
