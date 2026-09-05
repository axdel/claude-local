# `outputs/` — raw model-reply fixtures for `edits.extract_file`

Each `.txt` is one whole raw model reply, read without normalization and passed directly to
`extract_file`. Expected `WholeFileReply` values in `test_edits.py` are hand-derived from the
canonical frame declared by `src/claude_local/rules_card.md`.

## Provenance (Boundary Fixture Fidelity)

These fixtures are hand-authored from this project's published wire schema, not copied from the
extractor or recorded from its output. The schema is one frame beginning at byte zero with a
single `FILE: <relative-path>` line, one blank line, and the raw payload running to the end of
the reply. There is no byte count, no terminator, and no Markdown or marker compatibility
grammar: the header is one line, and everything past the blank line is the file.

The set enumerates accepted payload properties and fail-closed degradation modes:

| Fixture | Property it pins |
|-|-|
| `payload_with_header_looking_lines.txt` | valid payload preserves fence-looking and `FILE:` lines |
| `no_terminal_newline.txt` | valid payload preserves the absence of a terminal newline |
| `unicode_with_terminal_newline.txt` | valid Unicode payload preserves one terminal newline |
| `truncated_payload.txt` | a cut-off payload parses as a short file, so the oracle — not the parser — reports it |
| `second_frame.txt` | a second framed record is payload text, so the oracle names the offending line |
| `no_marker_single_fence.txt` | legacy markerless Markdown fence is blocked |
| `no_marker_multiple_fences.txt` | multiple legacy Markdown fences are blocked |
| `prose_no_blocks.txt` | prose without a frame is blocked |

The last two rows of the accepted set carry the one cost of dropping the byte count: trailing
bytes are no longer refused at the parser. That trade is deliberate and lands where the loop is
stronger — a written file fails the immutable oracle with a `SyntaxError` naming the line, which
is repairable feedback, where a parse rejection produced a silent `BLOCKED` with nothing to
repair from (D-EDITS-002).

A future real-model capture may supplement this schema-derived corpus only when its provenance is
recorded; it must not replace these closed-set contract cases.
