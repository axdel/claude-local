"""Tests for the transport seam (``claude_local.backend``).

Two backends, one Protocol. ``ReplayBackend`` is verified by replay determinism
(what is captured comes back verbatim), an explicit completion ledger (it counts what
it served and fails loud past its supply), and the empty-supply boundary. ``HttpxBackend``
is verified by asserting the request it POSTs against the OpenAI streaming request spec
(schema-derived: model / messages / stream / stream_options.include_usage / max_tokens)
using an httpx MockTransport — never a live server (the real run is gated). Its failure
path (a non-2xx status raises) and warm-client reuse (the injected client is not closed)
are pinned too.
"""

from __future__ import annotations

import json

import httpx
import pytest
from factories import build_budget

from claude_local.backend import HttpxBackend, ReplayBackend, ReplayExhausted

# --- ReplayBackend: replay determinism -------------------------------------------


def test_replay_yields_the_captured_stream_verbatim() -> None:
    captured = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n'
    backend = ReplayBackend([captured])
    # Replay determinism: what was captured comes back byte-for-byte.
    assert b"".join(backend.generate("prefix", "tail", build_budget())) == captured


def test_replay_advances_through_scripts_in_order() -> None:
    first, second = b"data: A\n\n", b"data: B\n\n"
    backend = ReplayBackend([first, second])
    budget = build_budget()
    # Each generation serves the next script, in order — no repeats, no skips.
    assert b"".join(backend.generate("p", "t", budget)) == first
    assert b"".join(backend.generate("p", "t", budget)) == second


# --- ReplayBackend: the completion ledger ----------------------------------------


def test_replay_served_count_starts_at_zero() -> None:
    # A fresh backend has served nothing — the ledger baseline.
    assert ReplayBackend([b"x\n\n"]).served == 0


def test_replay_served_count_tracks_generations() -> None:
    backend = ReplayBackend([b"a\n\n", b"b\n\n", b"c\n\n"])
    budget = build_budget()
    backend.generate("p", "t", budget)
    backend.generate("p", "t", budget)
    # The ledger equals the number of generations served — assigned == processed.
    assert backend.served == 2


def test_replay_raises_when_over_read() -> None:
    backend = ReplayBackend([b"only\n\n"])
    budget = build_budget()
    backend.generate("p", "t", budget)  # exhausts the single script
    # Over-read fails LOUD, never returns a silent empty stream.
    with pytest.raises(ReplayExhausted):
        backend.generate("p", "t", budget)


def test_replay_empty_supply_is_immediately_exhausted() -> None:
    # The boundary: a backend scripted with nothing exhausts on the first request.
    with pytest.raises(ReplayExhausted):
        ReplayBackend([]).generate("p", "t", build_budget())


def test_replay_exhausted_carries_the_ledger_counts() -> None:
    backend = ReplayBackend([b"one\n\n"])
    budget = build_budget()
    backend.generate("p", "t", budget)
    with pytest.raises(ReplayExhausted) as excinfo:
        backend.generate("p", "t", budget)
    # The exception names the ledger so an over-read is diagnosable: 1 served, 1 available.
    assert excinfo.value.served == 1
    assert excinfo.value.available == 1


# --- HttpxBackend: request shape vs the OpenAI streaming spec ---------------------


def _capturing_client() -> tuple[httpx.Client, dict[str, object]]:
    """An httpx client whose mock transport records the request body it receives."""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=b"data: [DONE]\n\n")

    return httpx.Client(transport=httpx.MockTransport(handler)), captured


def test_httpx_request_shape_matches_openai_streaming_spec() -> None:
    client, captured = _capturing_client()
    backend = HttpxBackend("http://local:8080", client, model="local-model")
    list(backend.generate("STABLE-PREFIX", "CHANGING-TAIL", build_budget(max_tokens=512)))
    body = captured["body"]
    assert isinstance(body, dict)
    # Schema-derived from the OpenAI streaming request contract, not from memory.
    assert body["model"] == "local-model"
    assert body["messages"] == [
        {"role": "system", "content": "STABLE-PREFIX"},
        {"role": "user", "content": "CHANGING-TAIL"},
    ]
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["max_tokens"] == 512


def test_httpx_generation_params_merge_but_core_keys_win() -> None:
    client, captured = _capturing_client()
    # generation_params carries the server-specific sampling knobs (non-thinking,
    # repetition penalty) AND attempts to override a core key — which must not take.
    backend = HttpxBackend(
        "http://local:8080",
        client,
        model="local-model",
        generation_params={"temperature": 0.0, "repetition_penalty": 1.3, "stream": False},
    )
    list(backend.generate("p", "t", build_budget(max_tokens=99)))
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["temperature"] == 0.0
    assert body["repetition_penalty"] == 1.3
    # Core invariants are non-negotiable: streaming, usage accounting, and the token
    # cap always hold, even when generation_params tries to countermand them.
    assert body["stream"] is True
    assert body["max_tokens"] == 99


def test_httpx_url_appends_endpoint_and_normalizes_trailing_slash() -> None:
    for base in ("http://local:8080", "http://local:8080/"):
        client, captured = _capturing_client()
        backend = HttpxBackend(base, client, model="m")
        list(backend.generate("p", "t", build_budget()))
        # Exactly one endpoint path regardless of a trailing slash on the base URL.
        assert captured["url"] == "http://local:8080/v1/chat/completions"


# --- HttpxBackend: failure path and warm-client reuse ----------------------------


def test_httpx_raises_on_error_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"upstream boom")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    backend = HttpxBackend("http://local:8080", client, model="m")
    # A non-2xx status is surfaced as an error, never yielded as if it were content.
    with pytest.raises(httpx.HTTPStatusError):
        list(backend.generate("p", "t", build_budget()))


def test_httpx_yields_response_bytes_verbatim() -> None:
    wire = b'data: {"choices":[{"delta":{"content":"x"}}]}\n\ndata: [DONE]\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=wire)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    backend = HttpxBackend("http://local:8080", client, model="m")
    # The backend is a byte pipe: it yields the response bytes untouched (decoding
    # is the client's job), so the raw wire arrives intact.
    assert b"".join(backend.generate("p", "t", build_budget())) == wire


def test_httpx_reuses_injected_client_without_closing_it() -> None:
    client, _ = _capturing_client()
    backend = HttpxBackend("http://local:8080", client, model="m")
    budget = build_budget()
    list(backend.generate("p", "t", budget))
    # The client is injected and warm: the backend must not close it, so it stays
    # usable across generations (one resident connection, not one per iteration).
    assert client.is_closed is False
    list(backend.generate("p", "t", budget))  # a second generation still works
    assert client.is_closed is False
