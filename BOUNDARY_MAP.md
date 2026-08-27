# Boundary Map

## Import Rules

| Module | Target | Rule | Notes | Status | Superseded By |
|-|-|-|-|-|-|
| backend | httpx | may-import | Only external transport dependency. | active |  |
| backend | types | may-import | Transport consumes Budget. | active |  |
| client | backend | may-import | Streams raw SSE bytes from the transport. | active |  |
| client | derail | may-import | Watches decode for repetition/cap/timeout. | active |  |
| client | sse | may-import | Decodes raw bytes via the shared decoder. | active |  |
| client | types | may-import | Consumes Budget and value objects. | active |  |
| contract | types | may-import | Maps loop Status to BuildStatus. | active |  |
| derail | types | may-import | Guard consumes Budget. | active |  |
| edits | paths | may-import | Writes only through realpath containment. | active |  |
| edits | types | may-import | Consumes value objects. | active |  |
| loop | client | may-import | Drives one generation per attempt. | active |  |
| loop | contract | may-import | Maps terminal Status to BuildStatus. | active |  |
| loop | edits | may-import | Applies whole-file blocks. | active |  |
| loop | prompt | may-import | Builds the stable prefix once per task. | active |  |
| loop | runner | may-import | Runs the oracle after each attempt. | active |  |
| loop | snapshot | may-import | Keeps the best-passing snapshot. | active |  |
| loop | telemetry | may-import | Writes the local economy record. | active |  |
| loop | types | may-import | Returns LoopResult. | active |  |
| prompt | types | may-import | Assembles the stable prefix from TaskSpec. | active |  |
| runner | sandbox | may-import | Runs the oracle under kernel confinement. | active |  |
| runner | types | may-import | Oracle returns a score value object. | active |  |
| snapshot | paths | may-import | Restore constrained by keep_only containment. | active |  |
| snapshot | runner | may-import | Ranks attempts by the oracle score. | active |  |
| snapshot | types | may-import | Consumes value objects. | active |  |
| sse | types | may-import | Decoder consumes value objects. | active |  |
| telemetry | types | may-import | Aggregates the local economy record. | active |  |

## Error Ownership

| Layer | Raises | Catches and Translates | Status | Superseded By |
|-|-|-|-|-|

## Layer Purity

| Layer | Owns | Must NOT Contain | Status | Superseded By |
|-|-|-|-|-|
