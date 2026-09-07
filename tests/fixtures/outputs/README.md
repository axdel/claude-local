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

## Real captures

One fixture is a recorded reply rather than a schema-derived one, kept because the property it
pins is a thing a model does and not a thing the schema says:

| Fixture | Property it pins | Provenance |
|-|-|-|
| `leading_blank_lines_before_frame.txt` | a well-formed frame preceded by blank lines is accepted (D-EDITS-005) | `scripts/probe_reply.py Qwen3.8-27B-abliterated examples/quicksort`, served with the thinking channel left on; recorded verbatim, 515 bytes, `finish_reason: stop`, 851 completion tokens |

It is a capture and not an authored case on purpose: the two leading newlines are the residue of a
reasoning channel, and an authored fixture would be this project guessing what that residue looks
like — the input-side anti-oracle. The expected values in `test_edits.py` are still read off the
wire schema and off the captured bytes, never off a parse of them.
