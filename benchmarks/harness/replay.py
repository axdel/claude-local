"""Wire-faithful OpenAI-compatible replay transport for offline benchmark runs.

The helper doubles only the external model server. It returns a real ``httpx.Client`` whose mock
transport emits schema-derived streaming chat-completion frames, so ``claude_local.implement``
still exercises its public HTTP, SSE, edit, sandbox, and oracle path.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx

_REPLAY_MODEL = "benchmark-replay"
_CHUNK_BASE: dict[str, object] = {
    "id": "chatcmpl-benchmark-replay",
    "object": "chat.completion.chunk",
    "created": 0,
    "model": _REPLAY_MODEL,
}
_REPLAY_COMPLETION_TOKENS = 1
_REPLAY_PROMPT_TOKENS = 0


def replay_http_client(
    implementation_source: str,
    *,
    impl_path: str,
    request_observer: Callable[[httpx.Request], None] | None = None,
) -> httpx.Client:
    """Return a client that streams ``implementation_source`` as one complete Python file.

    Args:
        implementation_source: Complete implementation-file text returned by the replayed model.
        impl_path: Relative implementation path declared by the reply frame.
        request_observer: Optional observer called with the real outgoing request before replay.

    Returns:
        An injected-lifecycle ``httpx.Client`` backed by a deterministic mock transport.
    """
    completion = _completion_stream(implementation_source, impl_path)

    def replay_completion(request: httpx.Request) -> httpx.Response:
        if request_observer is not None:
            request_observer(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=completion,
        )

    return httpx.Client(transport=httpx.MockTransport(replay_completion))


def _completion_stream(implementation_source: str, impl_path: str) -> bytes:
    """Encode one byte-counted completion with role, content, finish, usage, and terminator."""
    file_reply = (
        f"FILE: {impl_path}\n"
        f"UTF8-BYTES: {len(implementation_source.encode('utf-8'))}\n\n"
        f"{implementation_source}"
    )
    frames = (
        {
            **_CHUNK_BASE,
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": ""},
                    "finish_reason": None,
                }
            ],
        },
        {
            **_CHUNK_BASE,
            "choices": [{"index": 0, "delta": {"content": file_reply}, "finish_reason": None}],
        },
        {
            **_CHUNK_BASE,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
        {
            **_CHUNK_BASE,
            "choices": [],
            "usage": {
                "prompt_tokens": _REPLAY_PROMPT_TOKENS,
                "completion_tokens": _REPLAY_COMPLETION_TOKENS,
                "total_tokens": _REPLAY_PROMPT_TOKENS + _REPLAY_COMPLETION_TOKENS,
            },
        },
    )
    return b"".join(_sse_frame(frame) for frame in frames) + b"data: [DONE]\n\n"


def _sse_frame(payload: dict[str, object]) -> bytes:
    """Encode one OpenAI-compatible JSON chunk as an SSE data frame."""
    return f"data: {json.dumps(payload)}\n\n".encode()
