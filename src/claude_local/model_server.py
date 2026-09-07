"""The model server's lifetime — spawn it, wait for ready, and always tear it down.

A served model holds roughly 20 GB of unified memory and one port for as long as it runs, so
the expensive failure here is not a crash but a *survivor*: a server nobody is talking to,
still resident, still holding the port the next run needs. Expressing the lifetime as a context
manager makes teardown structural rather than remembered — the process is killed on the normal
exit, on an exception inside the block, and on a readiness timeout alike.

Deliberately NOT routed through ``sandbox.sandboxed_spawn``. That profile denies network
outright, so a listening server cannot run under it, and relaxing the denial to host one would
widen the confinement that exists to contain untrusted model-authored code. The two spawn paths
stay separate on purpose: one sandboxes code we do not trust, this one runs a server we chose.

The server is addressed only over loopback. mlx_vlm's own ``--host`` default is ``0.0.0.0``,
which publishes the model to every interface, so the bind address is always passed explicitly.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess  # nosec B404 (argv is built here from registry data, never shell-interpreted)
import sys
import tempfile
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import httpx

from claude_local.model_registry import ResolvedModel, UnservableCombination

_SERVER_MODULE = "mlx_vlm.server"
"""Provided by the opt-in `serve` dependency group; absent, the spawn fails as ModuleNotFound."""

_LOOPBACK = "127.0.0.1"
"""Explicit because mlx_vlm defaults --host to 0.0.0.0 — every interface, not just this host."""

_BUILDER_OWNED_OPTIONS = frozenset(
    {"--host", "--port", "--model", "--max-tokens", "--draft-model", "--draft-kind"}
)
"""Options ``for_model`` supplies itself, which a registry FLAGS cell may therefore not declare.

The server parses argv with argparse, which resolves a repeated option to its LAST value, and
FLAGS is appended after these. Ordering is what would otherwise keep the bind on loopback, and
argparse breaks that tie the other way — so a row adding ``--host 0.0.0.0`` would publish an
unauthenticated model server to every interface from a data-only edit.
"""

_READINESS_PATH = "/v1/models"
"""The endpoint that answers once weights are loaded; a bound port alone is not readiness."""

_DEFAULT_MAX_TOKENS = 32768

DEFAULT_STARTUP_TIMEOUT_S = 900.0
"""Fifteen minutes: a 27-31B model at 6-bit streams off disk on a cold first load.

The value is the largest any caller needed, not a guess. It was eight minutes while every script
that actually serves the biggest registered models overrode it upward — which is the constant
being wrong rather than the callers being cautious, since an override that every caller makes is
the default in the wrong place. Callers now pass this through instead of re-typing a number.
"""

_READINESS_POLL_S = 0.25
"""How often to ask. A refused connection returns immediately, so polling costs almost nothing."""

_READINESS_PROBE_TIMEOUT_S = 5.0
"""How long one answer may take — deliberately NOT the poll interval.

Sharing one constant ties "how long may an answer take" to "how often do we ask", so a server
that is loaded but busy — answering correctly in more than a poll interval — aborts every probe
client-side and is reported as never having answered, at any budget.
"""

_SERVED_MODELS_TIMEOUT_S = 30.0
"""One-shot budget for reading the served model id — generous, since it is asked once per run."""

_TERMINATE_GRACE_S = 10.0
"""How long a server gets to exit on SIGTERM before the group is SIGKILLed."""

_LOG_TAIL_BYTES = 8 * 1024
"""Diagnostic bytes kept from a failed startup; the cause is conventionally at the tail."""

_ANSWER_EXCERPT_CHARS = 200
"""Head chars kept from an unrecognised served-models answer — where a proxy names itself."""


class PortUnavailable(Exception):
    """The port is already bound by a process this module does not own."""


class ServerExited(Exception):
    """The server process exited before becoming ready; carries its output tail."""


class ServerNotReady(TimeoutError):
    """The server stayed alive but never answered its readiness endpoint in time."""


class ServerModelUnknown(LookupError):
    """The server answered its readiness endpoint but named no model a request can address.

    A ``LookupError`` subclass for the same reason ``ServerNotReady`` subclasses ``TimeoutError``:
    the stdlib base is the semantically right family, so a caller reaching for it still catches
    this. Raising the bare base instead would make ``except LookupError`` around a call also
    swallow any stray ``KeyError`` or ``IndexError`` from the caller's own code.
    """


@dataclass(frozen=True, slots=True)
class ServerHandle:
    """A model server that is up and answering, for the duration of the ``running`` block."""

    base_url: str
    port: int
    pid: int

    def served_model_id(self) -> str:
        """Ask the running server which model it is serving.

        The server names the model however it chose to — mlx_vlm advertises the store path it was
        launched with, which is neither the registry name nor the repo id — and every
        chat-completions request must echo that id back. Asking beats assuming, so the handle that
        already knows the address owns the question.

        Returns:
            The id of the first model the server advertises at its served-models endpoint.

        Raises:
            httpx.HTTPError: The server did not answer.
            ServerModelUnknown: The server answered, but advertises no model this can address —
                either an empty served-models list, or a body that is not one at all.
        """
        served_models_url = f"{self.base_url}{_READINESS_PATH}"
        answer = httpx.get(served_models_url, timeout=_SERVED_MODELS_TIMEOUT_S).text
        advertised = _advertised_models(served_models_url, answer)
        if not advertised:
            raise ServerModelUnknown(
                f"the model server at {served_models_url} advertises no model"
            )
        return str(advertised[0]["id"])


def _advertised_models(served_models_url: str, answer: str) -> list[dict[str, object]]:
    """Read the advertised models out of a server's answer, or say what arrived instead.

    Anything can be listening on a port — a proxy, a dev server, the wrong process — and answer
    200 with a body that is not a served-models list. Indexing into it raises a bare ``KeyError:
    'data'`` naming neither the address probed nor what came back, so the operator learns that a
    dict lacked a key rather than that something other than a model server holds their port.
    """
    try:
        return list(json.loads(answer)["data"])
    except (json.JSONDecodeError, TypeError, KeyError) as exc:
        raise ServerModelUnknown(
            f"the model server at {served_models_url} did not answer with a served-models list. "
            f"It said: {answer[:_ANSWER_EXCERPT_CHARS]}"
        ) from exc


def _redeclared_builder_options(flags: tuple[str, ...]) -> tuple[str, ...]:
    """The builder-owned options ``flags`` would reach, sorted; empty when the row is servable.

    Matching is argparse's, not string equality, because argparse is what will read these. It
    splits ``--opt=value`` at the first ``=`` and accepts any unambiguous prefix of a long option,
    so ``--host``, ``--host=0.0.0.0`` and ``--hos`` all reach the same option while a blocklist of
    exact tokens catches only the first. Asking whether an owned option *starts with* the supplied
    name is that rule, and it stays scoped: ``--port-range`` is not a prefix of ``--port``, so it
    is left servable — argparse does not match it either.

    Args:
        flags: The registry FLAGS cell, already split on whitespace into argv tokens.

    Returns:
        The owned options the cell would reach, sorted for a stable message. Empty means none.
    """
    supplied = {flag.split("=", 1)[0] for flag in flags if flag.startswith("--")}
    return tuple(
        sorted(
            owned
            for owned in _BUILDER_OWNED_OPTIONS
            if any(owned.startswith(name) for name in supplied)
        )
    )


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
        max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> ModelServer:
        """Build the launch specification for a registered model, bound to loopback.

        The model is named by its **store path**, never by its repo id. mlx_vlm resolves the
        argument with ``Path(arg)`` and falls through to ``snapshot_download`` when it does not
        exist, so a repo id for absent weights silently starts a multi-gigabyte fetch. Passing a
        path that ``ModelRegistry.resolve`` already proved present keeps that branch unreachable.

        The draft model follows the same rule and is included only when its weights are on disk:
        the registry names one for a model whose draft was never pulled, and passing that id would
        download it.

        The bind address is not a parameter, mirroring ``sandboxed_spawn``: this exposes no knob
        that widens the exposure of an unauthenticated model server, so loopback holds by
        construction rather than by every caller remembering to leave a default alone.

        Args:
            resolved: A registered model whose weights the registry confirmed are present.
            max_tokens: Server-side generation ceiling.

        Raises:
            UnservableCombination: the row's FLAGS cell declares an option this builder supplies,
                which argparse would resolve in the cell's favour.
        """
        redeclared = _redeclared_builder_options(resolved.flags)
        if redeclared:
            raise UnservableCombination(
                f"model {resolved.name!r} declares {', '.join(redeclared)} in its FLAGS cell, "
                f"which this builder supplies itself. argparse takes the last occurrence of a "
                f"repeated option, so the row would silently override it."
            )
        draft_arguments: tuple[str, ...] = ()
        if resolved.draft_path is not None:
            draft_arguments = ("--draft-model", str(resolved.draft_path), "--draft-kind", "mtp")
        return cls(
            command=(
                sys.executable,
                "-m",
                _SERVER_MODULE,
                "--host",
                _LOOPBACK,
                "--port",
                str(resolved.port),
                "--model",
                str(resolved.path),
                "--max-tokens",
                str(max_tokens),
                *draft_arguments,
                *resolved.flags,
            ),
            host=_LOOPBACK,
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
    def running(
        self,
        *,
        startup_timeout_s: float = DEFAULT_STARTUP_TIMEOUT_S,
        log_path: Path | None = None,
    ) -> Generator[ServerHandle]:
        """Spawn the server, wait until it answers, and guarantee it is gone afterwards.

        Args:
            startup_timeout_s: Budget for reaching readiness. Teardown is not charged against it.
            log_path: Keep the server's output here instead of in an unlinked temp file. Off by
                default because the only writable root under an orchestrator's dispatch cage is
                its isolation worktree, and a server log is not one of that worktree's declared
                artifacts — but a caller running a long sweep outside a cage names a path and
                keeps what the default destroys.

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
        with _server_output(log_path) as output:
            process = subprocess.Popen(  # noqa: S603 # nosec B603
                self.command,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # its own group, so teardown reaps every child
            )
            try:
                self._await_readiness(process, output, startup_timeout_s=startup_timeout_s)
                yield ServerHandle(base_url=self.base_url, port=self.port, pid=process.pid)
            except BaseException as failure:
                # A server that dies mid-run surfaces to the caller as a transport error raised by
                # the backend, while the reason is only in the server's own output — which the
                # default capture is about to destroy unread. A note carries it out without
                # changing the exception type every caller already catches.
                if process.poll() is not None:
                    failure.add_note(_server_death_note(process.returncode, output))
                raise
            finally:
                _terminate_session(process)

    def _await_readiness(
        self, process: subprocess.Popen[bytes], output: BinaryIO, *, startup_timeout_s: float
    ) -> None:
        """Poll until the server answers, it dies, or the budget runs out.

        Liveness is checked before each HTTP probe. A server that died on startup would
        otherwise burn the entire budget and be reported as slow rather than as broken — and
        its own output, which names the actual cause, would never be surfaced.
        """
        deadline = time.monotonic() + startup_timeout_s
        probe_url = f"{self.base_url}{_READINESS_PATH}"
        while True:
            if process.poll() is not None:
                raise ServerExited(
                    f"the model server exited with code {process.returncode} during startup; "
                    f"last output:\n{_read_tail(output)}"
                )
            with contextlib.suppress(httpx.HTTPError):
                if httpx.get(probe_url, timeout=_READINESS_PROBE_TIMEOUT_S).status_code == 200:
                    return
            if time.monotonic() >= deadline:
                raise ServerNotReady(
                    f"the model server did not answer {probe_url} within {startup_timeout_s:g}s; "
                    f"last output:\n{_read_tail(output)}"
                )
            time.sleep(_READINESS_POLL_S)


@contextmanager
def _server_output(log_path: Path | None) -> Generator[BinaryIO]:
    """Yield the sink the server's stdout and stderr are captured into.

    Unlinked by default, which is what makes it safe under a dispatch cage — and also what makes
    it unrecoverable, so a caller who wants the log after the run names a path and gets a file.
    """
    if log_path is None:
        with tempfile.TemporaryFile() as unnamed:
            yield unnamed
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w+b") as named:
        yield named


def _server_death_note(returncode: int | None, output: BinaryIO) -> str:
    """State that the server was already dead, and quote what it said on the way out."""
    tail = _read_tail(output).strip()
    death = f"the model server had already exited (returncode {returncode})"
    if not tail:
        return f"{death}, having written nothing."
    return f"{death}. Its output was:\n{tail}"


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
