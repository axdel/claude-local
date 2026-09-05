"""The model server's lifetime — spawn it, wait for ready, and always tear it down.

A served model holds roughly 20 GB of unified memory and one port for as long as it runs, so
the expensive failure here is not a crash but a *survivor*: a server nobody is talking to,
still resident, still holding the port the next run needs. Expressing the lifetime as a context
manager makes teardown structural rather than remembered — the process is killed on the normal
exit, on an exception inside the block, and on a readiness timeout alike.

Deliberately NOT routed through ``sandbox.sandboxed_spawn``. That profile denies network
outright, so a listening server cannot run under it, and relaxing the denial to host one would
widen the confinement that exists to contain untrusted model-authored code. The two spawn paths
stay separate on purpose: one cages code we do not trust, this one runs a server we chose.

The server is addressed only over loopback. mlx_vlm's own ``--host`` default is ``0.0.0.0``,
which publishes the model to every interface, so the bind address is always passed explicitly.
"""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import subprocess  # nosec B404 (argv is built here from catalog data, never shell-interpreted)
import sys
import tempfile
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import BinaryIO

import httpx

from claude_local.model_registry import ResolvedModel

_SERVER_MODULE = "mlx_vlm.server"
"""Provided by the opt-in `serve` dependency group; absent, the spawn fails as ModuleNotFound."""

_LOOPBACK = "127.0.0.1"
"""Explicit because mlx_vlm defaults --host to 0.0.0.0 — every interface, not just this host."""

_READINESS_PATH = "/v1/models"
"""The endpoint that answers once weights are loaded; a bound port alone is not readiness."""

_DEFAULT_MAX_TOKENS = 32768

DEFAULT_STARTUP_TIMEOUT_S = 480.0
"""Eight minutes: a 27-31B model at 6-bit streams off disk on a cold first load."""

_READINESS_POLL_S = 0.25

_CATALOGUE_TIMEOUT_S = 30.0
"""One-shot budget for reading the served model id — generous, since it is asked once per run."""
"""Probe interval. A refused connection returns immediately, so polling costs almost nothing."""

_TERMINATE_GRACE_S = 10.0
"""How long a server gets to exit on SIGTERM before the group is SIGKILLed."""

_LOG_TAIL_BYTES = 8 * 1024
"""Diagnostic bytes kept from a failed startup; the cause is conventionally at the tail."""


class PortUnavailable(Exception):
    """The port is already bound by a process this module does not own."""


class ServerExited(Exception):
    """The server process exited before becoming ready; carries its output tail."""


class ServerNotReady(TimeoutError):
    """The server stayed alive but never answered its readiness endpoint in time."""


@dataclass(frozen=True, slots=True)
class ServerHandle:
    """A model server that is up and answering, for the duration of the ``running`` block."""

    base_url: str
    port: int
    pid: int

    def served_model_id(self) -> str:
        """Ask the running server which model it is serving.

        The server names the model however it chose to — mlx_vlm advertises the store path it was
        launched with, which is neither the catalogue name nor the repo id — and every
        chat-completions request must echo that id back. Asking beats assuming, so the handle that
        already knows the address owns the question.

        Returns:
            The id of the first model the server advertises at its catalogue endpoint.

        Raises:
            httpx.HTTPError: The server did not answer.
            LookupError: The server answered, but advertises no model to address.
        """
        catalogue_url = f"{self.base_url}{_READINESS_PATH}"
        advertised = httpx.get(catalogue_url, timeout=_CATALOGUE_TIMEOUT_S).json()["data"]
        if not advertised:
            raise LookupError(f"the model server at {catalogue_url} advertises no model")
        return str(advertised[0]["id"])


@dataclass(frozen=True, slots=True)
class ModelServer:
    """The launch specification for one model server, separate from any running process.

    Construction and lifetime are split so the spec can be built and asserted on without
    spawning anything, and so a caller (a test, a harness) can drive the lifetime against a
    substitute command without this module growing a parameter that exists only for tests.
    """

    command: tuple[str, ...]
    host: str
    port: int

    @classmethod
    def for_model(
        cls,
        resolved: ResolvedModel,
        *,
        host: str = _LOOPBACK,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> ModelServer:
        """Build the launch specification for a catalogued model.

        The model is named by its **store path**, never by its repo id. mlx_vlm resolves the
        argument with ``Path(arg)`` and falls through to ``snapshot_download`` when it does not
        exist, so a repo id for absent weights silently starts a multi-gigabyte fetch. Passing a
        path that ``ModelRegistry.resolve`` already proved present keeps that branch unreachable.

        The draft model follows the same rule and is included only when its weights are on disk:
        the catalog names one for a model whose draft was never pulled, and passing that id would
        download it.

        Args:
            resolved: A catalogued model whose weights the registry confirmed are present.
            host: Bind address; loopback unless a caller has a reason to widen it.
            max_tokens: Server-side generation ceiling.
        """
        draft_arguments: tuple[str, ...] = ()
        if resolved.draft_path is not None:
            draft_arguments = ("--draft-model", str(resolved.draft_path), "--draft-kind", "mtp")
        return cls(
            command=(
                sys.executable,
                "-m",
                _SERVER_MODULE,
                "--host",
                host,
                "--port",
                str(resolved.port),
                "--model",
                str(resolved.path),
                "--max-tokens",
                str(max_tokens),
                *draft_arguments,
                *resolved.flags,
            ),
            host=host,
            port=resolved.port,
        )

    @property
    def base_url(self) -> str:
        """The origin a Backend extends, carrying no path segment.

        ``HttpxBackend`` appends ``/v1/chat/completions`` itself, so including ``/v1`` here
        would address ``/v1/v1/chat/completions`` and 404 every generation.
        """
        return f"http://{self.host}:{self.port}"

    @contextmanager
    def running(self, *, timeout_s: float = DEFAULT_STARTUP_TIMEOUT_S) -> Generator[ServerHandle]:
        """Spawn the server, wait until it answers, and guarantee it is gone afterwards.

        Args:
            timeout_s: Budget for reaching readiness. Teardown is not charged against it.

        Yields:
            A handle carrying the base URL, port, and pid of the live server.

        Raises:
            PortUnavailable: something is already listening on the port.
            ServerExited: the process died during startup; the message carries its output.
            ServerNotReady: the process stayed alive but never answered in time.
        """
        if _is_port_bound(self.host, self.port):
            raise PortUnavailable(
                f"port {self.port} is already bound on {self.host} by a process this server "
                f"does not own — stop it first rather than racing it"
            )
        # Output goes to an unlinked temp file rather than a path under the repo: the only
        # writable root under an orchestrator's dispatch cage is its isolation worktree, and a
        # server log is not one of that worktree's declared artifacts.
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(  # noqa: S603 # nosec B603
                self.command,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # its own group, so teardown reaps every child
            )
            try:
                self._await_readiness(process, output, timeout_s=timeout_s)
                yield ServerHandle(base_url=self.base_url, port=self.port, pid=process.pid)
            finally:
                _terminate_session(process)

    def _await_readiness(
        self, process: subprocess.Popen[bytes], output: BinaryIO, *, timeout_s: float
    ) -> None:
        """Poll until the server answers, it dies, or the budget runs out.

        Liveness is checked before each HTTP probe. A server that died on startup would
        otherwise burn the entire budget and be reported as slow rather than as broken — and
        its own output, which names the actual cause, would never be surfaced.
        """
        deadline = time.monotonic() + timeout_s
        probe_url = f"{self.base_url}{_READINESS_PATH}"
        while True:
            if process.poll() is not None:
                raise ServerExited(
                    f"the model server exited with code {process.returncode} during startup; "
                    f"last output:\n{_read_tail(output)}"
                )
            with contextlib.suppress(httpx.HTTPError):
                if httpx.get(probe_url, timeout=_READINESS_POLL_S).status_code == 200:
                    return
            if time.monotonic() >= deadline:
                raise ServerNotReady(
                    f"the model server did not answer {probe_url} within {timeout_s:g}s; "
                    f"last output:\n{_read_tail(output)}"
                )
            time.sleep(_READINESS_POLL_S)


def _is_port_bound(host: str, port: int) -> bool:
    """True when something already accepts connections on ``host:port``."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((host, port)) == 0


def _read_tail(stream: BinaryIO) -> str:
    """Read the diagnostic tail of the captured output without moving the file offset.

    ``os.pread`` rather than seek-and-read: the child holds a dup of this descriptor, and a dup
    shares one open file description, so seeking here would relocate a live server's next write.
    A positional read leaves the offset untouched, which matters on the readiness-timeout path
    where the process is still running when its output is sampled.
    """
    end = os.fstat(stream.fileno()).st_size
    start = max(0, end - _LOG_TAIL_BYTES)
    return os.pread(stream.fileno(), end - start, start).decode("utf-8", errors="replace")


def _terminate_session(process: subprocess.Popen[bytes]) -> None:
    """SIGTERM the server's whole process group, escalating to SIGKILL if it lingers.

    The group, not the process: a server forks workers, and killing only the leader would leave
    them holding the port. ``ProcessLookupError`` is suppressed throughout for the benign race
    where it has already exited.
    """
    if process.poll() is not None:
        process.wait()
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    try:
        process.wait(timeout=_TERMINATE_GRACE_S)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        process.wait()
