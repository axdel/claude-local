# Boundary Map

## Import Rules

| Module | Target | Rule | Notes | Status | Superseded By |
|-|-|-|-|-|-|
| __init__ | entrypoint | may-import | Public front door re-exports implement and Outcome. | active |  |
| __init__ | types | may-import | Public front door re-exports Budget, Status, TaskSpec. | active |  |
| backend | httpx | may-import | Only external transport dependency. | active |  |
| backend | types | may-import | Transport consumes Budget. | active |  |
| client | backend | may-import | Streams raw SSE bytes from the transport. | active |  |
| client | derail | may-import | Watches decode for repetition/cap/timeout. | active |  |
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
| prompt | runner | may-import | Distills feedback over the oracle TestScore. | active |  |
| prompt | types | may-import | Assembles the stable prefix from TaskSpec. | active |  |
| runner | sandbox | may-import | Runs the oracle under kernel confinement. | active |  |
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
