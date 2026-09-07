"""Lifecycle tests for the model server.

The model server is an OS process holding ~20 GB of unified memory and a port, so the property
under test is not "it starts" but "it is always gone afterwards" — on normal exit, on an
exception inside the block, and on a readiness timeout alike. An orphan survives the test run
and starves the next one, so teardown is asserted, never assumed.

Command construction is tested separately from lifetime, and both against real objects: the
lifetime tests spawn a genuine subprocess serving genuine HTTP on a real port, because a mocked
process cannot demonstrate that a real one was reaped. The substitute answers the same readiness
endpoint the MLX server does, which is all the lifecycle depends on — no weights are loaded, so
the suite stays fast while still exercising spawn, probe, and kill for real.
"""

from __future__ import annotations

import socket
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from claude_local.model_registry import ResolvedModel, UnservableCombination
from claude_local.model_server import (
    ModelServer,
    PortUnavailable,
    ServerExited,
    ServerNotReady,
)

# A real HTTP server answering the readiness endpoint, and nothing else. Spawned as a genuine
# subprocess so teardown assertions are about a real process and a real bound port. The body it
# serves is an argument, so a test can choose what the catalogue endpoint advertises.
_SUBSTITUTE_SERVER = """
import http.server, sys

body = sys.argv[2].encode()

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        code = 200 if self.path == "/v1/models" else 404
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
"""

# OpenAI's /v1/models response shape, which mlx_vlm implements: a list envelope whose entries
# carry the id a chat-completions request must echo back.
_ONE_MODEL_BODY = (
    '{"object": "list", "data": [{"id": "substitute/store-path", "object": "model"}]}'
)
_NO_MODELS_BODY = '{"object": "list", "data": []}'


def _free_port() -> int:
    """Claim and release a port the OS says is free, then hand back its number."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _substitute(port: int, *, body: str = _ONE_MODEL_BODY) -> ModelServer:
    """A ModelServer whose command is a real HTTP server rather than a 12 GB model."""
    return ModelServer(
        command=(sys.executable, "-c", _SUBSTITUTE_SERVER, str(port), body),
        host="127.0.0.1",
        port=port,
    )


def _resolved(
    tmp_path: Path, *, draft_present: bool = False, flags: tuple[str, ...] = ()
) -> ResolvedModel:
    """A resolved model over a real directory, since the command names paths that must exist."""
    store = tmp_path / "gpt-oss-20b"
    store.mkdir(exist_ok=True)
    draft_path = None
    if draft_present:
        draft_path = tmp_path / "gpt-oss-20b-MTP"
        draft_path.mkdir(exist_ok=True)
    return ResolvedModel(
        name="gpt-oss-20b",
        repo="mlx-community/gpt-oss-20b-Q8",
        # A repo id is recorded even when the weights are absent; draft_path is what says
        # whether it can actually be served.
        draft_repo="lukaskremla/Qwen3.8-MTP" if draft_present else None,
        port=8088,
        flags=flags,
        # Empty because the server command is built from FLAGS alone: generation parameters ride
        # in each request body, so a row declaring them must not change one argv element here.
        generation_params={},
        path=store,
        draft_path=draft_path,
    )


def _is_listening(port: int) -> bool:
    """True when something accepts a loopback connection on ``port``."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def test_the_command_launches_the_mlx_server_module(tmp_path: Path) -> None:
    """Oracle: `python -m mlx_vlm.server` is the documented entry point of the installed stack."""
    command = ModelServer.for_model(_resolved(tmp_path)).command

    assert command[0] == sys.executable
    assert command[1:3] == ("-m", "mlx_vlm.server")


def test_the_model_is_passed_by_store_path_never_by_repo_id(tmp_path: Path) -> None:
    """Passing the repo id would let the server download the weights it cannot find locally.

    Oracle: mlx_vlm's get_model_path takes ``Path(arg)`` and, when it does not exist, falls
    through to snapshot_download. So a repo id for an unpulled model silently starts a
    multi-gigabyte fetch — the exact behaviour the never-pull invariant forbids. The store path
    is what keeps that branch unreachable.
    """
    resolved = _resolved(tmp_path)
    command = ModelServer.for_model(resolved).command

    assert "--model" in command
    assert command[command.index("--model") + 1] == str(resolved.path)
    assert resolved.repo not in command


def test_the_command_binds_loopback_rather_than_every_interface(tmp_path: Path) -> None:
    """Oracle: mlx_vlm's own --host default is 0.0.0.0, which publishes the model to the LAN.

    A local model server is for this machine, so the bind address must be passed explicitly.
    Drop the explicit host and the default takes over — this is the assertion that catches it.

    Counting the occurrences is load-bearing, not belt-and-braces. argparse resolves a repeated
    option to its LAST value, so a second ``--host`` appended anywhere later widens the bind while
    ``index()`` — which returns the FIRST match — still reads 127.0.0.1 and reports success.
    """
    command = ModelServer.for_model(_resolved(tmp_path)).command

    assert command.count("--host") == 1
    assert command[command.index("--host") + 1] == "127.0.0.1"


# S104 flags the all-interfaces literal, which here is the payload proved unreachable rather than
# an address anything binds. Kept verbatim because it is mlx_vlm's own --host default and so the
# exact string this refusal exists to stop; a stand-in address would test the same code path while
# no longer documenting the attack.
@pytest.mark.parametrize(
    "smuggled",
    [
        pytest.param(("--host", "0.0.0.0"), id="separate-tokens"),  # noqa: S104
        pytest.param(("--host=0.0.0.0",), id="equals-form"),
        pytest.param(("--hos", "0.0.0.0"), id="argparse-abbreviation"),  # noqa: S104
    ],
)
def test_a_flags_cell_may_not_redeclare_an_option_the_builder_supplies(
    tmp_path: Path, smuggled: tuple[str, ...]
) -> None:
    """Oracle: argparse's own resolution rules, which decide what the server actually binds.

    FLAGS is unvalidated catalog text, split on whitespace and appended after the options this
    builder supplies — so ordering alone is what keeps the bind on loopback, and argparse breaks
    ties the other way. All three spellings below reach ``--host`` in argparse and were confirmed
    against it directly: a repeated option takes the last value, ``--host=`` is the same option in
    one token, and a long option matches on any unambiguous prefix. A blocklist of exact tokens
    would catch only the first. The rule is therefore argparse's rule — a supplied name that is a
    prefix of an option the builder owns — which is why ``--port-range`` stays servable: argparse
    does not match it to ``--port`` either.

    Refusing beats ordering around it. Ordering is a property of the argv this module happens to
    build today, while the refusal is a property of the row, so it cannot be re-broken downstream.
    """
    with pytest.raises(UnservableCombination, match="host"):
        ModelServer.for_model(_resolved(tmp_path, flags=smuggled))


def test_serving_flags_the_builder_does_not_own_stay_servable(tmp_path: Path) -> None:
    """The refusal is scoped to conflicts: every real catalog flag must still pass through.

    Oracle: the FLAGS cells the shipped catalog actually carries. A refusal that also rejected
    these would be a fail-closed check that closed the feature.
    """
    flags = ("--enable-thinking", "--kv-bits", "8", "--quantized-kv-start", "0")

    command = ModelServer.for_model(_resolved(tmp_path, flags=flags)).command

    assert command[-len(flags) :] == flags


def test_serving_flags_from_the_registry_row_reach_the_command_as_separate_arguments(
    tmp_path: Path,
) -> None:
    """Oracle: argv is a sequence, so a joined string arrives as one unparseable argument."""
    resolved = _resolved(tmp_path, flags=("--enable-thinking", "--kv-bits", "8"))

    command = ModelServer.for_model(resolved).command

    assert "--enable-thinking" in command
    assert command[command.index("--kv-bits") + 1] == "8"


def test_a_present_draft_model_adds_the_speculative_decoding_arguments(tmp_path: Path) -> None:
    """Oracle: mlx_vlm's CLI pairs --draft-model with --draft-kind, whose MTP value is 'mtp'."""
    resolved = _resolved(tmp_path, draft_present=True)

    command = ModelServer.for_model(resolved).command

    assert command[command.index("--draft-model") + 1] == str(resolved.draft_path)
    assert command[command.index("--draft-kind") + 1] == "mtp"


def test_a_declared_but_unpulled_draft_is_left_out_rather_than_named_by_repo_id(
    tmp_path: Path,
) -> None:
    """The catalog names a draft for a model whose weights were never pulled.

    Oracle: the same download trap as the main model — a repo id the server cannot find
    locally is fetched, not refused. Serving without speculative decoding is slower; silently
    downloading a draft is a multi-gigabyte surprise, so absence must mean omission.
    """
    declared_but_unpulled = replace(
        _resolved(tmp_path), draft_repo="lukaskremla/Qwen3.8-MTP", draft_path=None
    )

    command = ModelServer.for_model(declared_but_unpulled).command

    assert "--draft-model" not in command
    assert "lukaskremla/Qwen3.8-MTP" not in command


def test_no_draft_model_means_no_speculative_arguments(tmp_path: Path) -> None:
    command = ModelServer.for_model(_resolved(tmp_path)).command

    assert "--draft-model" not in command
    assert "--draft-kind" not in command


def test_the_base_url_is_the_origin_the_backend_extends(tmp_path: Path) -> None:
    """Oracle: HttpxBackend appends '/v1/chat/completions' to base_url (backend.py:115,124).

    So base_url must be the bare origin. Appending '/v1' here — as the shell script this
    replaces printed — would produce '/v1/v1/chat/completions' and 404 every generation.
    """
    server = ModelServer.for_model(_resolved(tmp_path))

    assert server.base_url == "http://127.0.0.1:8088"
    assert not server.base_url.endswith("/v1")


def test_the_handle_reports_the_model_id_the_server_actually_serves() -> None:
    """Oracle: OpenAI's `/v1/models` schema — the served id is `data[0].id`.

    The server names the model however it chose to: mlx_vlm reports the store path it was
    launched with, which is neither the catalogue name nor the repo id, and a chat-completions
    request must echo that id back. Asking beats assuming, so the handle owns the question.
    """
    port = _free_port()

    with _substitute(port).running(timeout_s=30.0) as handle:
        assert handle.served_model_id() == "substitute/store-path"


def test_a_server_advertising_no_models_is_named_rather_than_indexed_into() -> None:
    """A server that is up but serving nothing fails with the endpoint named, not an IndexError.

    Oracle: the same schema — `data` is a list, and an empty one is well-formed. Reaching for
    `data[0]` would raise a bare `IndexError` whose message names no server, which is the
    failure a caller then has to guess at.
    """
    port = _free_port()

    with (
        _substitute(port, body=_NO_MODELS_BODY).running(timeout_s=30.0) as handle,
        pytest.raises(LookupError, match=r"/v1/models"),
    ):
        handle.served_model_id()


def test_the_process_is_reaped_and_the_port_freed_after_a_normal_exit() -> None:
    port = _free_port()

    with _substitute(port).running(timeout_s=30.0) as handle:
        assert _is_listening(port), "the substitute never came up, so teardown proves nothing"
        pid = handle.pid

    # Oracle: a served model holds ~20 GB and its port; the requirement is that neither outlives
    # the block. Both are observed from outside the module, not reported by it.
    assert not _is_listening(port)
    assert not _process_alive(pid)


def test_the_process_is_reaped_when_the_block_raises() -> None:
    """Teardown is structural, so an exception inside the block must not leak the server."""
    port = _free_port()
    escaped: int | None = None

    with (
        pytest.raises(RuntimeError, match="failure inside the block"),
        _substitute(port).running(timeout_s=30.0) as handle,
    ):
        escaped = handle.pid
        raise RuntimeError("failure inside the block")

    assert escaped is not None
    assert not _is_listening(port)
    assert not _process_alive(escaped)


def test_starting_on_an_occupied_port_is_refused_rather_than_raced() -> None:
    """Adopting a stranger's listener would make every later probe and teardown lie.

    Oracle: the port is the model server's identity here (one per registry row), so a bound
    port means someone else owns it. Refusing is the only outcome that keeps ownership true.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as squatter:
        squatter.bind(("127.0.0.1", 0))
        squatter.listen(1)
        occupied = int(squatter.getsockname()[1])

        with (
            pytest.raises(PortUnavailable) as refusal,
            _substitute(occupied).running(timeout_s=5.0),
        ):
            pass

    assert str(occupied) in str(refusal.value)


def test_a_server_that_dies_during_startup_reports_its_output_rather_than_timing_out() -> None:
    """A crash must surface its own diagnostic, not be masked as a slow load.

    Oracle: liveness and readiness are different failures. Waiting for the full timeout on a
    process that already exited wastes the whole budget and reports the wrong cause, which is
    why the prior shell implementation checked liveness before every HTTP probe.
    """
    port = _free_port()
    doomed = ModelServer(
        command=(
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('weights corrupt'); sys.exit(1)",
        ),
        host="127.0.0.1",
        port=port,
    )

    with pytest.raises(ServerExited) as death, doomed.running(timeout_s=30.0):
        pass

    assert "weights corrupt" in str(death.value)


def test_a_server_that_never_answers_is_timed_out_and_still_torn_down() -> None:
    """The timeout path is the one most likely to leak: the process is alive and unresponsive."""
    port = _free_port()
    silent = ModelServer(
        command=(sys.executable, "-c", "import time; time.sleep(120)"),
        host="127.0.0.1",
        port=port,
    )

    with pytest.raises(ServerNotReady), silent.running(timeout_s=2.0):
        pass

    # Oracle: a readiness failure is still an exit path, so the same teardown guarantee binds.
    assert not _is_listening(port)


def _process_alive(pid: int) -> bool:
    """True when ``pid`` still exists (signal 0 probes without delivering)."""
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
