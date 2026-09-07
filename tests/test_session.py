"""Tests for the interactive session (``claude_local.session``).

The session's job is a sequence with five ways to leak a process — resolve, spawn, wait ready,
build a client, tear down — so these drive a REAL subprocess HTTP server rather than a mocked
transport: a mock cannot fail to be reaped, which is precisely the failure being tested. The
substitute serves the same two endpoints a model server does, replaying the captured SSE fixture
byte-for-byte so the wire shape is the recorded one and not one authored from memory.

Expected values come from the fixture and the OpenAI streaming contract, never from running the
session: the reply text is read out of ``complete_stream.bytes``, and teardown is asserted by
probing the port from the OS, not by trusting a return.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from ports import free_port, port_is_bound

from claude_local.model_registry import ModelNotPresent, UnknownModel
from claude_local.model_server import ModelServer, ServerNotReady
from claude_local.session import ChatSession, ModelSession
from claude_local.types import Budget

_FIXTURES = Path(__file__).parent / "fixtures" / "sse"

# A substitute for the model server: the same /v1/models list a real one advertises, plus a
# /v1/chat/completions that replays a recorded SSE stream. Small enough to pass as an argument,
# real enough that the process must actually be reaped.
_SUBSTITUTE_SERVER = """
import http.server, sys

stream = open(sys.argv[2], "rb").read()
body = b'{"object": "list", "data": [{"id": "substitute/store-path", "object": "model"}]}'

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        code = 200 if self.path == "/v1/models" else 404
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_response(404)
            self.end_headers()
            return
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(stream)

    def log_message(self, *args):
        pass

http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
"""

# The fixture's content deltas, concatenated — the oracle for what one turn must return. Read off
# the recorded stream, never off a run of the code under test.
_FIXTURE_REPLY = "Artificial intelligence"


def _session(port: int, *, stream: str = "complete_stream.bytes") -> ModelSession:
    """A session whose server is a real HTTP process rather than a multi-gigabyte model."""
    return ModelSession(
        server=ModelServer(
            command=(sys.executable, "-c", _SUBSTITUTE_SERVER, str(port), str(_FIXTURES / stream)),
            host="127.0.0.1",
            port=port,
        ),
        generation_params={},
        budget=Budget(
            max_attempts=1,
            max_tokens=4096,
            generation_timeout_s=60.0,
            oracle_timeout_s=60.0,
        ),
    )


# --- The yielded callable ---------------------------------------------------------


def test_a_session_yields_a_callable_that_returns_the_models_text() -> None:
    port = free_port()

    with _session(port).open(startup_timeout_s=30.0) as chat:
        reply = chat("write a haiku")

    # Oracle: the concatenated content deltas of the recorded stream.
    assert reply == _FIXTURE_REPLY


def test_a_turn_records_its_metered_result_on_last() -> None:
    port = free_port()

    with _session(port).open(startup_timeout_s=30.0) as chat:
        assert chat.last is None  # nothing generated yet
        chat("write a haiku")
        first = chat.last

    assert first is not None
    assert first.text == _FIXTURE_REPLY
    assert first.derail_reason is None  # the stream ended on its own


def test_a_session_reuses_one_client_across_turns() -> None:
    """Local inference is memory-bandwidth-bound: reconnecting per turn spends latency on nothing.

    Oracle: the client's own call counter. Two turns through one session is two calls on ONE
    client — a per-turn client would start each turn's count at zero.
    """
    port = free_port()

    with _session(port).open(startup_timeout_s=30.0) as chat:
        chat("first")
        chat("second")
        calls = chat.total_calls

    assert calls == 2


def test_a_session_reports_the_id_the_server_advertises() -> None:
    """The server names the model however it chose to, and every request must echo that back."""
    port = free_port()

    with _session(port).open(startup_timeout_s=30.0) as chat:
        advertised = chat.model_id

    assert advertised == "substitute/store-path"


# --- Teardown: the process is gone, asked of the OS --------------------------------


def test_the_server_is_gone_after_the_block() -> None:
    port = free_port()

    with _session(port).open(startup_timeout_s=30.0) as chat:
        assert port_is_bound(port)  # it really was up
        chat("write a haiku")

    assert not port_is_bound(port)


def test_the_server_is_gone_when_the_block_raises() -> None:
    """The failure mode this module exists to prevent: an exception leaking a resident model."""
    port = free_port()

    with (
        pytest.raises(RuntimeError, match="user code failed"),
        _session(port).open(startup_timeout_s=30.0),
    ):
        assert port_is_bound(port)
        raise RuntimeError("user code failed")

    assert not port_is_bound(port)


def test_a_server_that_never_answers_is_still_reaped() -> None:
    """A startup timeout must not leave the process it gave up on still running."""
    port = free_port()
    silent = ModelSession(
        server=ModelServer(
            command=(sys.executable, "-c", "import time; time.sleep(60)"),
            host="127.0.0.1",
            port=port,
        ),
        generation_params={},
        budget=Budget(
            max_attempts=1, max_tokens=4096, generation_timeout_s=60.0, oracle_timeout_s=60.0
        ),
    )

    with pytest.raises(ServerNotReady), silent.open(startup_timeout_s=1.0):
        pytest.fail("the session must not open against a server that never answered")

    assert not port_is_bound(port)


# --- The specification, built without spawning anything ----------------------------


def _store_with(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str) -> None:
    """Point the registry at a throwaway store holding just ``name``, so no real weights are read.

    The registry is repo-relative and present in every worktree; the store is not, which is exactly
    what ``CLAUDE_LOCAL_MODELS`` exists to override.
    """
    (tmp_path / name).mkdir()
    monkeypatch.setenv("CLAUDE_LOCAL_MODELS", str(tmp_path))


def test_for_model_resolves_without_spawning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Construction and lifetime are split: building a specification starts no process."""
    _store_with(monkeypatch, tmp_path, "gpt-oss-20b")
    spawned: list[object] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **_kwargs: spawned.append(args))

    spec = ModelSession.for_model("gpt-oss-20b")

    assert spawned == []
    assert spec.budget.max_attempts == 1  # a chat turn is one generation
    assert spec.server.host == "127.0.0.1"  # never mlx_vlm's 0.0.0.0 default


def test_an_unregistered_name_is_refused_before_anything_is_spawned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned: list[object] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **_kwargs: spawned.append(args))

    with pytest.raises(UnknownModel):
        ModelSession.for_model("no-such-model-in-any-registry")

    assert spawned == []


def test_a_registered_model_with_no_weights_is_refused_never_fetched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Nothing here ever downloads a model: an absent store entry is an error, not a fetch."""
    monkeypatch.setenv("CLAUDE_LOCAL_MODELS", str(tmp_path))  # empty store
    spawned: list[object] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **_kwargs: spawned.append(args))

    with pytest.raises(ModelNotPresent):
        ModelSession.for_model("gpt-oss-20b")

    assert spawned == []


# --- ChatSession in isolation ------------------------------------------------------


def test_chat_session_starts_with_no_recorded_turn() -> None:
    # Boundary: `last` is None before the first call, so a caller can tell "not yet" from a result.
    chat = ChatSession(None, None, "substitute/store-path")  # type: ignore[arg-type]

    assert chat.last is None
    assert chat.model_id == "substitute/store-path"
