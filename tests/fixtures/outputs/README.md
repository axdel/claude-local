# `outputs/` — raw model-reply fixtures for `edits.extract_files`

Each `.txt` is a whole raw model reply, fed verbatim to `extract_files`. They are the
input-side oracle for the extractor: the expected `FileBlock` list in `test_edits.py` is
hand-derived from the **loop's own whole-file reply-format contract**, not from running the
parser.

## Provenance (Boundary Fixture Fidelity)

These are **hand-authored from a published schema — our own** — not captured from a specific
model run. The loop *defines* the reply format it instructs the model to emit (a `FILE:`
marker at column 0, optional ```` ```python ```` fences), so a fixture written to that format
is schema-validated, not authored-from-memory of an external protocol. No model was run to
produce them (this branch builds against a stub; a real-model run is separately gated).

The variant set is the **enumerated weak-model degradation modes** the extractor must survive
— the closed set, not a happy path:

| Fixture | Shape it pins |
|-|-|
| `fenced_with_marker.txt` | marker + fenced body; trailing prose after the fence is excluded |
| `unfenced_with_marker.txt` | marker + bare (unfenced) body |
| `marker_inside_fence.txt` | a `FILE:` line *inside* a fenced string is content, never a split |
| `truncated_final_block.txt` | a fence cut off mid-stream (no closing ```` ``` ````) is tolerated |
| `no_marker_single_fence.txt` | zero markers + exactly one fenced region -> the single permitted path |
| `no_marker_multiple_fences.txt` | zero markers + two regions -> ambiguous -> no blocks |
| `prose_no_blocks.txt` | pure prose, no markers, no fences -> no blocks -> BLOCKED |

When a real model server is available, capture genuine replies and add them here as
higher-fidelity fixtures — provenance noted per file. Until then, the schema-derived set above
is the enumerated contract.
