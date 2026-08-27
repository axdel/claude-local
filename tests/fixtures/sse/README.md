# SSE fixtures — provenance

Raw Server-Sent-Events byte samples for the streaming chat-completions decoder
(`claude_local.sse.decode_sse`). Each `*.bytes` file is one wire stream, stored
verbatim as the decoder receives it.

## Source (schema-derived, not hand-invented)

These fixtures are **schema-derived** from the published OpenAI-compatible
streaming Chat Completions SSE format — the contract every target server
(`mlx_lm.server`, llama.cpp, LM Studio, vLLM) implements. They are NOT authored
from a mental model of the wire; the shape of every frame was verified on
2026-08-27 against:

- OpenAI API Reference — Chat Completions streaming events
  (https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events)
- OpenAI Cookbook — How to stream completions
  (https://developers.openai.com/cookbook/examples/how_to_stream_completions)
- mlx-lm HTTP server (ml-explore/mlx-lm) — OpenAI-compatible `/v1/chat/completions`

A real captured session is unavailable here by design (no model is downloaded in
this environment), so the published provider schema is the trust anchor — the
"High trust" tier of boundary-fixture fidelity. When a real capture becomes
available it should replace these, verbatim.

## The closed variant set covered

| Frame | Wire shape (verified) | Decoder event |
|-|-|-|
| Role chunk (first) | `delta:{"role":"assistant","content":""}`, `finish_reason:null` | (none — empty content) |
| Content delta | `delta:{"content":"..."}`, `finish_reason:null` | `Delta(text)` |
| Finish | `delta:{}`, `finish_reason:"stop"\|"length"\|"tool_calls"` | `Finish(reason)` |
| Usage (`include_usage`) | `choices:[]` (empty), `usage:{completion_tokens,...}` | `Usage(completion_tokens)` |
| Mid-stream error | `{"error":{"message",...}}` | `Error(message)` |
| Sentinel | `data: [DONE]` (not JSON) | stops the stream |

## Files

- `complete_stream.bytes` — role, two content deltas, finish (`stop`), a separate
  `include_usage` chunk with empty `choices`, then `[DONE]`. The full happy path.
- `mid_stream_error.bytes` — role, one delta, then an `{"error":...}` frame, then `[DONE]`.
- `aborted_midstream.bytes` — role, two complete deltas, then a final frame **cut off
  mid-JSON** with no terminating blank line — the network-truncation case. The decoder
  must yield the two deltas and NO phantom terminator.

CRLF line endings and arbitrary byte-boundary splits are exercised by transforming
`complete_stream.bytes` in the tests (both are spec-faithful transforms), not by
storing redundant fixtures.
