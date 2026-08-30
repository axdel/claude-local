"""Wire-faithful OpenAI-compatible replay transport for offline benchmark runs.

The helper doubles only the external model server. It returns a real ``httpx.Client`` whose mock
transport emits schema-derived streaming chat-completion frames, so ``claude_local.implement``
still exercises its public HTTP, SSE, edit, sandbox, and oracle path.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping

import httpx

from claude_local import TARGET_FILE_LABEL

_REPLAY_MODEL = "benchmark-replay"
_CHUNK_BASE: dict[str, object] = {
    "id": "chatcmpl-benchmark-replay",
    "object": "chat.completion.chunk",
    "created": 0,
    "model": _REPLAY_MODEL,
}
_REPLAY_COMPLETION_TOKENS = 1
_REPLAY_PROMPT_TOKENS = 0
_TARGET_FILE_MARKER = f"{TARGET_FILE_LABEL} "
"""Prefix of the prompt's target-file line — the per-case dispatch key for a benchmark replay.

Derived from the public ``TARGET_FILE_LABEL`` ``PromptBuilder`` emits, so the parser can never
drift from the producer: every request for a case names the same target, so dispatching on it
routes each request — and each retry — to that case's reply.
"""


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


def replay_cases_http_client(
    sources_by_impl_path: Mapping[str, str],
    *,
    request_observer: Callable[[httpx.Request], None] | None = None,
) -> httpx.Client:
    """Return one client that replays the benchmark, each request routed to its case's source.

    A single injected client serves every case in a benchmark. Each request names its target file
    in the prompt (``Target file: <impl_path>``); this transport reads that marker and streams the
    matching source from ``sources_by_impl_path`` as one complete Python file. Dispatch is on the
    target, not on call order, so a case retried within its attempt budget still routes to the same
    reply.

    Args:
        sources_by_impl_path: Implementation-file text to replay, keyed by each case's impl path.
        request_observer: Optional observer called with the real outgoing request before replay.

    Returns:
        An injected-lifecycle ``httpx.Client`` backed by a per-case dispatching mock transport.

    Raises:
        ValueError: a request declared no target file, or named one absent from the source map.
    """
    sources = dict(sources_by_impl_path)

    def replay_dispatched(request: httpx.Request) -> httpx.Response:
        if request_observer is not None:
            request_observer(request)
        impl_path = _requested_impl_path(request)
        try:
            source = sources[impl_path]
        except KeyError:
            raise ValueError(f"no replay source for target file {impl_path!r}") from None
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_completion_stream(source, impl_path),
        )

    return httpx.Client(transport=httpx.MockTransport(replay_dispatched))


def _requested_impl_path(request: httpx.Request) -> str:
    """Return the target impl path a chat-completion request names in its system message.

    Reads the ``Target file: <impl_path>`` line the prompt places in the system message — the
    retry-invariant key a benchmark replay routes on.

    Raises:
        ValueError: the request carried no target-file line.
    """
    system_message = json.loads(request.content)["messages"][0]["content"]
    for line in system_message.splitlines():
        target = line.removeprefix(_TARGET_FILE_MARKER)
        if target != line:
            return target.strip()
    raise ValueError("replayed request declared no target file")


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
