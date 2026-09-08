"""The interactive surface — serve one registered model and hand back something you can call.

``implement()`` is the machine front door: it takes a task and returns an outcome. This is the
human one. ``model_session("gpt-oss-20b")`` resolves the name in the store, spawns a server, waits
for it to answer, builds a warm client against it, and yields a callable — then guarantees the
server is gone when the block ends, whether it ended normally or by exception.

The point is that a caller at an interactive prompt should not have to know that sequence.
Resolution, spawn, readiness polling, model-id negotiation, client construction, and teardown are
five steps with five ways to leak a process; they live here once instead of in every shell that
wants to try a prompt against a local model.

Serving is what separates this module from the rest of the package: the loop is deliberately
serving-agnostic (``implement()`` takes a ``base_url`` and starts nothing), so the MLX stack stays
an optional dependency group. Importing this module does not need it — the server is a
subprocess, named but never imported — so only actually opening a session requires
``uv sync --group serve``.

Reach it as ``from claude_local import model_session`` or ``from claude_local.model_session import
ModelSession``. ``import claude_local.model_session as x`` does NOT work: the package re-exports
the function of the same name, so that binds the function and ``x.ModelSession`` raises
AttributeError. Module and entry point share a name because they are one concept (D-SESSION-001).
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from claude_local.backend import HTTP_CONNECT_TIMEOUT_S, HTTP_READ_TIMEOUT_S, HttpxBackend
from claude_local.client import GenerationResult, ModelClient
from claude_local.model_registry import ModelRegistry
from claude_local.model_server import DEFAULT_STARTUP_TIMEOUT_S, ModelServer
from claude_local.sandbox import DEFAULT_ORACLE_TIMEOUT_S
from claude_local.types import Budget

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping
    from contextlib import AbstractContextManager

_TURN_MAX_TOKENS = 4096
"""Decode cap for one interactive turn — generous for one, far below a whole-file generation."""

_TURN_GENERATION_TIMEOUT_S = 600.0
"""Generation deadline for one interactive turn.

Generous on purpose, and safe for the same reason the benchmark's is: a slow model producing
steadily is healthy, and silence — not elapsed time — is what the derail guard cuts on.
"""


class SessionHandle:
    """A live model you can call: ``model("write a haiku")`` returns the text it produced.

    Named for the running half of a session the way ``ServerHandle`` is named for the running
    half of a ``ModelServer`` — this is what ``ModelSession.open()`` yields, so the pair reads
    the same way at both entry points.

    Callable rather than a plain function so one turn's metering survives it. ``__call__`` returns
    the text, because printing a dataclass is not what an interactive prompt wants; ``last`` keeps
    the full ``GenerationResult`` — tokens, seconds, the derail reason — which is the whole point
    of tuning a model interactively rather than just talking to it.
    """

    def __init__(self, client: ModelClient, budget: Budget, model_id: str) -> None:
        self._client = client
        self._budget = budget
        self.model_id = model_id
        """The id the server advertises, which every request must echo back."""
        self.last: GenerationResult | None = None
        """The previous turn's metered result, or ``None`` before the first call."""

    def __call__(self, user: str, *, system: str = "") -> str:
        """Generate one reply and return its text, recording the metered result on ``last``.

        Args:
            user: The prompt.
            system: An optional stable prefix. Passing the same one across a session's turns
                keeps the prefix byte-identical, so the server reuses its prefill cache instead
                of discarding it; varying it per turn silently throws that reuse away.

        Returns:
            The generated text, with any reasoning-channel markup already stripped.
        """
        self.last = self._client.generate(system, user, self._budget)
        return self.last.text

    @property
    def total_calls(self) -> int:
        """How many logical calls this session has issued — the client's own count."""
        return self._client.total_calls


@dataclass(frozen=True, slots=True)
class ModelSession:
    """The specification for an interactive session, separate from any running process.

    Split from its lifetime for the same reason ``ModelServer`` is: a caller can build and assert
    on the specification without spawning a multi-gigabyte model, and a test can drive ``open()``
    against a substitute server without this module growing a parameter that exists only for
    tests.
    """

    server: ModelServer
    generation_params: Mapping[str, object]
    budget: Budget

    @classmethod
    def for_model(
        cls,
        name: str,
        *,
        max_tokens: int = _TURN_MAX_TOKENS,
        generation_timeout_s: float = _TURN_GENERATION_TIMEOUT_S,
    ) -> ModelSession:
        """Resolve a registered name into a session specification, spawning nothing.

        Args:
            name: A model name from the registry's NAME column.
            max_tokens: Decode cap for one turn.
            generation_timeout_s: Wall-clock bound on one turn's decode.

        Returns:
            The specification, carrying the row's own generation parameters.

        Raises:
            ModelNotPresent: The name is registered but its weights are not in the store.
                Nothing here ever downloads a model.
            UnknownModel: The name is not in the registry at all.
        """
        resolved = ModelRegistry.default().resolve(name)
        return cls(
            server=ModelServer.for_model(resolved),
            generation_params=resolved.generation_params,
            budget=Budget(
                max_attempts=1,  # one turn is one generation; there is nothing to retry
                max_tokens=max_tokens,
                generation_timeout_s=generation_timeout_s,
                # Inert here: a turn runs no oracle. Budget requires it positive, so it takes the
                # sandbox's own default rather than a number invented for this call site.
                oracle_timeout_s=DEFAULT_ORACLE_TIMEOUT_S,
            ),
        )

    @contextmanager
    def open(
        self, *, startup_timeout_s: float = DEFAULT_STARTUP_TIMEOUT_S
    ) -> Generator[SessionHandle]:
        """Serve the model and yield a callable, tearing the server down on the way out.

        One keep-alive HTTP client serves the whole session: local inference is
        memory-bandwidth-bound, so reconnecting per turn spends latency on nothing.

        Args:
            startup_timeout_s: How long to wait for the server to answer before giving up.

        Yields:
            A ``SessionHandle`` bound to the running server.

        Raises:
            PortUnavailable: The port is already bound.
            ServerExited: The server process died during startup.
            ServerNotReady: The server did not answer within ``startup_timeout_s``.
        """
        with self.server.running(startup_timeout_s=startup_timeout_s) as handle:
            served = handle.served_model_id()
            timeout = httpx.Timeout(HTTP_READ_TIMEOUT_S, connect=HTTP_CONNECT_TIMEOUT_S)
            with httpx.Client(timeout=timeout) as http:
                backend = HttpxBackend(handle.base_url, http, served, self.generation_params)
                yield SessionHandle(ModelClient(backend), self.budget, served)


def model_session(
    name: str,
    *,
    max_tokens: int = _TURN_MAX_TOKENS,
    generation_timeout_s: float = _TURN_GENERATION_TIMEOUT_S,
    startup_timeout_s: float = DEFAULT_STARTUP_TIMEOUT_S,
) -> AbstractContextManager[SessionHandle]:
    """Serve a registered model for the duration of a ``with`` block.

    The one-liner an interactive prompt wants::

        with model_session("gpt-oss-20b") as model:
            print(model("write a haiku about static types"))
            print(model.last.tokens_per_second)

    The server is spawned on entry and gone on exit, including when the block raises.

    Args:
        name: A model name from the registry's NAME column.
        max_tokens: Decode cap for one turn.
        generation_timeout_s: Wall-clock bound on one turn's decode.
        startup_timeout_s: How long to wait for the server to answer before giving up.

    Returns:
        A context manager yielding a callable ``SessionHandle``.

    Raises:
        ModelNotPresent: The name is registered but its weights are not in the store.
        UnknownModel: The name is not in the registry at all.
    """
    spec = ModelSession.for_model(
        name, max_tokens=max_tokens, generation_timeout_s=generation_timeout_s
    )
    return spec.open(startup_timeout_s=startup_timeout_s)
