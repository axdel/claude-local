# `outputs/` — raw model-reply fixtures for `edits.extract_file`

Each `.txt` is one whole raw model reply, read without normalization and passed directly to
`extract_file`. Expected `WholeFileReply` values in `test_edits.py` are hand-derived from the
canonical byte-counted frame declared by `src/claude_local/rules_card.md`.

## Provenance (Boundary Fixture Fidelity)

These fixtures are hand-authored from this project's published wire schema, not copied from the
extractor or recorded from its output. The schema is one frame beginning at byte zero with
`FILE: <relative-path>`, `UTF8-BYTES: <ASCII decimal>`, one blank line, and the raw payload.
The byte count covers only the payload. Exact length is required unless the caller has transport
evidence that generation is incomplete. No Markdown or marker compatibility grammar exists.

The set enumerates accepted payload properties and fail-closed degradation modes:

| Fixture | Property it pins |
|-|-|
| `payload_with_header_looking_lines.txt` | valid payload preserves fence-looking and `FILE:` lines |
| `no_terminal_newline.txt` | valid payload preserves the absence of a terminal newline |
| `unicode_with_terminal_newline.txt` | valid Unicode payload preserves one terminal newline |
| `truncated_payload.txt` | short payload blocks by default and survives only with explicit incomplete evidence |
| `second_frame.txt` | a second framed record is trailing bytes, so the whole reply is blocked |
| `no_marker_single_fence.txt` | legacy markerless Markdown fence is blocked |
| `no_marker_multiple_fences.txt` | multiple legacy Markdown fences are blocked |
| `prose_no_blocks.txt` | prose without a frame is blocked |

A future real-model capture may supplement this schema-derived corpus only when its provenance is
recorded; it must not replace these closed-set contract cases.
