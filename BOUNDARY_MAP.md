# Boundary Map

## Import Rules

| Module | Target | Rule | Notes | Status | Superseded By |
|-|-|-|-|-|-|
| __init__ | entrypoint | may-import | Public front door re-exports implement and Outcome. | active |  |
| __init__ | loop | may-import | Public front door re-exports AttemptProgress, the live event implement's on_attempt observer receives. | active |  |
| __init__ | session | may-import | Public front door re-exports model_session, the interactive surface. | active |  |
| __init__ | types | may-import | Public front door re-exports task value objects. | active |  |
| __main__ | cli | may-import | The python -m shim resolves the same front door as the installed console script. | active |  |
| backend | httpx | may-import | Only external transport dependency. | active |  |
| backend | types | may-import | Transport consumes Budget. | active |  |
| benchmarks | claude_local | may-import | Downstream benchmark consumes only the top-level public package API. | active |  |
| claude_local | benchmarks | must-not-import | Reusable loop never depends on benchmark subjects or harness code. | active |  |
| cli | backend | may-import | Catches BackendUnavailable so a broken host exits apart from a failed task. | active |  |
| cli | entrypoint | may-import | Adapts one invocation to a TaskSpec; implement stays the composition root. | active |  |
| cli | model_server | must-not-import | The CLI never serves. A dispatched child runs under a profile granting outbound loopback but not network-bind, so it cannot listen; --base-url must name an already-running server. The layers contract would permit this import, so only this rule bars it. | active |  |
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
| entrypoint | httpx | may-import | Constructs the keep-alive httpx.Client for an owned-lifecycle call. | active |  |
| entrypoint | loop | may-import | Constructs and runs the Loop, the red->green driver. | active |  |
| entrypoint | prompt | may-import | Builds PromptBuilder from the rules card. | active |  |
| entrypoint | runner | may-import | Constructs TestRunner over the budget-bound sandbox spawn. | active |  |
| entrypoint | sandbox | may-import | Binds the oracle budget timeout into sandboxed_spawn. | active |  |
| entrypoint | snapshot | may-import | Constructs SnapshotStore over the writable subtree. | active |  |
| entrypoint | telemetry | may-import | Surfaces LocalEconomyRecord on the Outcome. | active |  |
| entrypoint | types | may-import | Consumes TaskSpec and Status. | active |  |
| loop | client | may-import | Drives one generation per attempt. | active |  |
| loop | edits | may-import | Applies whole-file blocks. | active |  |
| loop | paths | may-import | Raises KeepOnlyViolation from a mis-aimed whole-file edit. | active |  |
| loop | prompt | may-import | Builds the stable prefix once per task. | active |  |
| loop | runner | may-import | Runs the oracle after each attempt. | active |  |
| loop | snapshot | may-import | Keeps the best-passing snapshot. | active |  |
| loop | telemetry | may-import | Writes the local economy record. | active |  |
| loop | types | may-import | Returns LoopResult. | active |  |
| model_server | model_registry | may-import | Turns a resolved catalog row into a launch command; store paths keep the download branch unreachable. | active |  |
| model_server | sandbox | must-not-import | The oracle profile denies network, so a listening server cannot run under it; hosting one would widen the cage that contains untrusted model code. Separate spawn paths by design. | active |  |
| prompt | runner | may-import | Distills feedback over the oracle TestScore. | active |  |
| prompt | types | may-import | Assembles the stable prefix from TaskSpec. | active |  |
| runner | sandbox | may-import | Runs the oracle under kernel confinement. | active |  |
| session | backend | may-import | Constructs HttpxBackend against the id the running server advertises. | active |  |
| session | client | may-import | Wraps the backend in ModelClient, kept warm for the session's whole lifetime. | active |  |
| session | httpx | may-import | Constructs the one keep-alive httpx.Client the session reuses across turns. | active |  |
| session | model_registry | may-import | Resolves a catalogued name to its store path before anything is spawned. | active |  |
| session | model_server | may-import | Composes ModelServer.for_model and .running; never spawns a process directly. | active |  |
| session | sandbox | may-import | Reads DEFAULT_ORACLE_TIMEOUT_S for the Budget field a chat turn never spends. | active |  |
| snapshot | paths | may-import | Restore constrained by keep_only containment. | active |  |
| snapshot | runner | may-import | Ranks attempts by the oracle score. | active |  |
| telemetry | client | may-import | Aggregates GenerationResult token usage into the record. | active |  |
| telemetry | types | may-import | Aggregates the local economy record. | active |  |

## Error Ownership

| Layer | Raises | Catches and Translates | Status | Superseded By |
|-|-|-|-|-|

## Layer Purity

| Layer | Owns | Must NOT Contain | Status | Superseded By |
|-|-|-|-|-|
| benchmark-harness | case loading, scratch-worktree assembly, implement invocation, result aggregation | golden app business logic or claude_local internals | active |  |
