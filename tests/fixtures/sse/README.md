# SSE fixtures — provenance

Raw Server-Sent-Events byte samples for the streaming chat-completions decoder
(`claude_local.sse.decode_sse`). Each `*.bytes` file is one wire stream, stored
verbatim as the decoder receives it.

## Source — two tiers, each labelled per file

`harmony_channel_stream.bytes` is a **real capture** (highest trust) — see its
entry under Files. Every other fixture is **schema-derived** from the published
OpenAI-compatible streaming Chat Completions SSE format, the contract every
target server (`mlx_lm.server`, llama.cpp, LM Studio, vLLM) implements. The
derived ones are NOT authored from a mental model of the wire; the shape of every
frame was verified on 2026-08-27 against:

- OpenAI API Reference — Chat Completions streaming events
  (https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events)
- OpenAI Cookbook — How to stream completions
  (https://developers.openai.com/cookbook/examples/how_to_stream_completions)
- mlx-lm HTTP server (ml-explore/mlx-lm) — OpenAI-compatible `/v1/chat/completions`

Until 2026-09-05 no model was downloaded here, so the published schema was the
only available anchor. That is no longer true, and the standing rule is unchanged:
**a real capture replaces a derived fixture whenever one becomes available.**
Record one with the committed script, never by hand — the request body then comes
from `HttpxBackend.generate` itself, so the fixture cannot drift from the request
the loop actually sends:

```
scripts/capture_sse_fixture.py <model> <destination.bytes> --user "<prompt>"
```

### The one normalization a capture makes

The script replaces the served model id with the registry name, and changes nothing
else. Models are named by absolute store path so a repo id can never fall through
to `snapshot_download`, so every chunk would otherwise echo the capturing machine's
home directory — one copy per token. `decode_sse` reads `choices`, `delta.content`,
`finish_reason`, and `usage`; it never reads `model`, so frame boundaries, delta
granularity, marker placement, and the usage trailer are preserved exactly.

## The closed variant set covered

| Frame | Wire shape (verified) | Decoder event |
|-|-|-|
| Role chunk (first) | `delta:{"role":"assistant","content":""}`, `finish_reason:null` | (none — empty content) |
| Content delta | `delta:{"content":"..."}`, `finish_reason:null` | `Delta(text)` |
| Reasoning delta | `delta:{"content":null,"reasoning_content":"...","reasoning":"..."}` | `Reasoning(text)` — the `reasoning` alias is ignored |
| Finish | `delta:{}`, `finish_reason:"stop"\|"length"\|"tool_calls"` | `Finish(reason)` |
| Invalid finish | non-null array/object `finish_reason` | `Error(message)` and no `Finish` |
| Invalid nested shape | non-array `choices`, non-object choice/delta/usage, non-string content, invalid token count | one `Error(message)`, no partial events |
| Usage (`include_usage`) | `choices:[]` (empty), `usage:{completion_tokens,...}` | `Usage(completion_tokens)` |
| Mid-stream error | `{"error":{"message","type","code"}}` | `Error(message)` |
| Sentinel | `data: [DONE]` (not JSON) | stops the stream |

## Files

- `complete_stream.bytes` — role, two content deltas, finish (`stop`), a separate
  `include_usage` chunk with empty `choices`, then `[DONE]`. The full happy path.
- `mid_stream_error.bytes` — role, one delta (`"Hello"`), then the OpenAI error envelope
  `{"error":{"message":"context length exceeded","type":"invalid_request_error","code":"context_length_exceeded"}}`,
  then `[DONE]`. The decoder yields `Delta("Hello")` then `Error("context length exceeded")`; the
  client surfaces that message verbatim as the run's `fault` and stops decoding at the frame. The
  message string is the oracle for the fault-surfacing tests — derived from this documented
  envelope, never from running the client.
- `aborted_midstream.bytes` — role, two complete deltas, then a final frame **cut off
  mid-JSON** with no terminating blank line — the network-truncation case. The decoder
  must yield the two deltas and NO phantom terminator.
- `harmony_channel_stream.bytes` — **a real capture**, recorded 2026-09-05 from
  `mlx_vlm.server` serving `gpt-oss-20b` (system `"You are a terse assistant."`,
  user `"Reply with exactly: OK"`). 52 content deltas, `finish_reason:"stop"`,
  `usage.completion_tokens=53`. The server returns the model's whole harmony
  transcript as `content` instead of the assistant's message, which is what
  `claude_local.harmony` exists to undo (ml-explore/mlx-lm#875). Three facts this
  capture establishes that no derived fixture could:
  - The **streaming** path leaks the transcript exactly as the non-streaming one
    does — the seam being fixed is on the path the loop actually consumes.
  - `<|channel|>`, `<|message|>`, `<|end|>`, `<|start|>` are single tokens in this
    model's vocabulary, so each arrives as its own complete delta and never
    straddles a boundary. Convenient, and the opposite of the safe assumption —
    the client joins before parsing regardless, so it does not depend on this.
  - The server calls it a clean `stop` while returning an unscorable reply. Nothing
    upstream of the client has any signal that the generation failed, which is why
    the loop reported BLOCKED with a correct answer in hand.
- `reasoning_channel_stream.bytes` — **a real capture**, recorded 2026-09-06 from
  `mlx_vlm.server` serving `Qwen3.8-27B` (same prompts as above). 25 reasoning deltas
  carrying 89 characters, then two content deltas (`"\n\n"`, `"OK"`) carrying 4,
  `finish_reason:"stop"`, `usage.completion_tokens=29`. Every reasoning frame has
  `content:null` and puts its text on `reasoning_content`, with a duplicate `reasoning`
  alias beside it. Three facts this capture establishes that no derived fixture could:
  - The thinking channel is a **separate wire field**, not markup inside `content` — the
    structured twin of the harmony leak above, and invisible to `claude_local.harmony`.
  - The server bills the thinking: 29 completion tokens for a 2-character answer, so a
    loop that meters only content under-counts a reasoning model's real cost by 25/29ths.
  - Thinking is 86% of the decode on a prompt this trivial. At the 12.2 tok/s this same
    capture reports, a real task's thinking alone outlasts any content-silence bound —
    which is how a healthy model came to be recorded as a dead socket.
- `invalid_finish_reason_array.bytes` / `invalid_finish_reason_object.bytes` — schema-invalid
  non-null, non-string finish reasons paired with valid-looking implementation content and a
  later valid choice. The decoder must emit `Error` and stop the frame before any `Finish`.

CRLF line endings and arbitrary byte-boundary splits are exercised by transforming
`complete_stream.bytes` in the tests (both are spec-faithful transforms), not by
storing redundant fixtures.
