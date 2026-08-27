"""The transport seam — raw SSE bytes in, before any decoding.

A ``Backend`` turns a stable ``prefix`` and a changing ``tail`` into a stream of raw
SSE byte chunks, under a ``Budget``. Two implementations sit behind the one Protocol:

- ``ReplayBackend`` replays pre-captured byte streams with no model present, so the
  whole loop is exercisable offline. It fails loud when asked for more generations
  than it was scripted for, and counts what it served — the completion ledger.
- ``HttpxBackend`` POSTs the OpenAI-compatible streaming chat-completions request to
  a local server over one warm, injected client, and yields the response bytes. An
  unreachable server or an error status is translated to ``BackendUnavailable`` — a
  harness fault, since the running server is a prerequisite — never a raw ``httpx`` leak.

The seam sits at raw bytes on purpose: decoding lives one layer up in the client, so
neither backend knows the SSE grammar and this module never imports ``sse``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import httpx

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from claude_local.types import Budget


class Backend(Protocol):
    """A source of raw SSE byte chunks for one generation.

    ``prefix`` is the byte-identical KV-cacheable head (rules card + spec + test);
    ``tail`` is the per-attempt feedback. Splitting them lets a caller hold the prefix
    constant across a task's iterations so the server can reuse its prefill cache.
    """

    def generate(self, prefix: str, tail: str, budget: Budget) -> Iterator[bytes]:
        """Yield the raw SSE response bytes for one generation under ``budget``."""
        ...


class ReplayExhausted(RuntimeError):
    """A ``ReplayBackend`` was asked for a generation past its scripted supply.

    Carries how many scripts were available and how many had been served, so an
    over-read fails loud with the exact ledger rather than a silent empty stream.
    """

    def __init__(self, served: int, available: int) -> None:
        self.served = served
        self.available = available
        super().__init__(
            f"replay exhausted: {served} script(s) served, only {available} available"
        )


class ReplayBackend:
    """Replays pre-captured SSE byte streams, one per ``generate`` call.

    Each script is the raw bytes of one captured stream; successive generations serve
    them in order. Requesting more than were scripted raises ``ReplayExhausted`` rather
    than returning an empty stream, and ``served`` counts what has been handed out — the
    completion ledger a caller reconciles against the generations it expected.
    """

    def __init__(self, scripts: Sequence[bytes]) -> None:
        self._scripts: tuple[bytes, ...] = tuple(scripts)
        self._served = 0

    def generate(self, prefix: str, tail: str, budget: Budget) -> Iterator[bytes]:
        """Serve the next scripted stream as a single chunk; fail loud past the supply."""
        if self._served >= len(self._scripts):
            raise ReplayExhausted(self._served, len(self._scripts))
        script = self._scripts[self._served]
        self._served += 1
        return iter((script,))

    @property
    def served(self) -> int:
        """How many generations this backend has served — the completion ledger."""
        return self._served


class BackendUnavailable(RuntimeError):
    """The model server at ``base_url`` could not be reached, or answered with an error status.

    A precondition failure, not a task outcome: claude-local requires an already-running
    OpenAI-compatible server at ``base_url`` (it never serves one), so a server that is down,
    unreachable, or returning a non-2xx status means the prerequisite is unmet. Carries the URL
    and model for diagnosis and chains the originating ``httpx`` error as ``__cause__``. Like
    ``SandboxUnavailable`` (D-SANDBOX-001), it propagates as a harness fault — never mapped to a
    ``Status`` — so a missing server fails loud instead of masquerading as a failed task. Distinct
    from ``FAULTED`` (D-FAULT-001), which is a *reachable* server's mid-stream error frame.
    """

    def __init__(self, url: str, model: str, reason: str) -> None:
        self.url = url
        self.model = model
        super().__init__(f"model server unavailable at {url} (model {model!r}): {reason}")


class HttpxBackend:
    """POSTs the OpenAI-compatible streaming request to a local server, yielding bytes.

    The client is injected and kept warm across generations (one resident connection,
    not one per iteration). The request always streams with usage accounting on and the
    budget's token cap applied; ``generation_params`` supplies server-specific sampling
    knobs (non-thinking, repetition penalty) but can never countermand those core
    invariants. Real-run only — construction and request shape are unit-tested against a
    mock transport, never a live server.
    """

    _ENDPOINT = "/v1/chat/completions"

    def __init__(
        self,
        base_url: str,
        client: httpx.Client,
        model: str,
        generation_params: Mapping[str, object] | None = None,
    ) -> None:
        self._url = base_url.rstrip("/") + self._ENDPOINT
        self._client = client
        self._model = model
        self._generation_params = dict(generation_params or {})

    def generate(self, prefix: str, tail: str, budget: Budget) -> Iterator[bytes]:
        """Stream the chat-completions response bytes for one generation under ``budget``.

        Raises:
            BackendUnavailable: the server was unreachable (a transport failure) or answered with a
                non-2xx status. The underlying ``httpx`` error is translated at this transport
                boundary so a missing prerequisite server surfaces as a domain fault the caller can
                act on, not a raw ``httpx`` exception (clients translate infra errors to domain
                errors); the original error is preserved as ``__cause__``.
        """
        body: dict[str, object] = {
            **self._generation_params,
            "model": self._model,
            "messages": [
                {"role": "system", "content": prefix},
                {"role": "user", "content": tail},
            ],
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": budget.max_tokens,
        }
        try:
            with self._client.stream("POST", self._url, json=body) as response:
                response.raise_for_status()
                yield from response.iter_bytes()
        except httpx.HTTPStatusError as exc:
            reason = f"HTTP {exc.response.status_code}"
            raise BackendUnavailable(self._url, self._model, reason) from exc
        except httpx.RequestError as exc:
            reason = f"{type(exc).__name__}: {exc}"
            raise BackendUnavailable(self._url, self._model, reason) from exc
