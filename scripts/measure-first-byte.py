#!/usr/bin/env -S uv run --quiet python
"""Report how long a freshly served model takes to send its first byte.

This is the measurement behind `HTTP_READ_TIMEOUT_S` (`claude_local.backend`). That bound is
a time-to-first-byte cap, so the number it must clear is the slowest first byte in the catalog —
and the previous value was chosen by reasoning about prefill instead of measuring arrival, which
is how it came to be wrong.

The gap the reasoning missed is that a model server answers its readiness probe long before it
can answer a generation. `ModelServer` polls `/v1/models`, which returns 200 as soon as the HTTP
server binds its port; mlx_vlm loads the weights lazily, on the first inference request. So a
24 GB model reports ready in seconds and then spends minutes faulting weights in — with no bytes
on the SSE socket for the whole load, which is exactly the silence the read timeout judges. The
server's own `timings` trailer cannot see it either: prefill is clocked once the weights are
resident, so a run that spent minutes loading still reports a healthy prefill rate.

Hence this probe measures arrival from the client side and reports the split:

    scripts/measure-first-byte.py Gemma4-31B

`first_byte_s` is the wall clock from sending the request to the first chunk coming back — the
quantity the bound has to clear. `prompt_ms` is the server's prefill within that, so the
remainder is the lazy weight load. Prefill scales with the prompt, so the probe sends one the size
of a real task's system prefix — the bundled rules card repeated until it reaches the largest
prefix the benchmark's own cases build (`_TARGET_PREFIX_BYTES`) — and asks for almost no output.

A warm page cache under-measures the load, so run this on a model that was not just served, and
read the result as a floor rather than a ceiling. The model is resident only inside the
`running()` block, so teardown is structural. The store is read from CLAUDE_LOCAL_MODELS when
set — the override that makes this runnable from a worktree.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import httpx

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from claude_local.backend import HttpxBackend  # noqa: E402
from claude_local.model_registry import ModelRegistry  # noqa: E402
from claude_local.model_server import (  # noqa: E402
    DEFAULT_STARTUP_TIMEOUT_S,
    ModelServer,
)
from claude_local.sandbox import DEFAULT_ORACLE_TIMEOUT_S  # noqa: E402
from claude_local.types import Budget  # noqa: E402

_RULES_CARD = _REPO_ROOT / "src" / "claude_local" / "rules_card.md"

_TARGET_PREFIX_BYTES = 32_021
"""The largest system prefix a real case builds — the one this probe has to reproduce.

Measured through ``PromptBuilder.stable_prefix`` over the seven committed schedule-manager cases
with the bundled card: 06_schedule_service at 32,021 bytes, against a 10,133-byte smallest and a
22,433-byte mean. The largest is the target because the bound this probe justifies has to clear
the SLOWEST first byte, and prefill scales with the prompt.

Stated as a size rather than as a count of card copies, because the copies are the mechanism and
the size is the intent. The previous form fixed the mechanism at six copies — a ratio to the 2.5 KB
card of the day — so when the card briefly grew to 34 KB the same six copies sent 205 KB, six times
any real prompt, while the constant's own docstring still called it benchmark-sized. Deriving the
count from the card's current size is what keeps that from happening again silently; the target
itself is pinned against the real cases by ``tests/test_measure_first_byte.py``.
"""


_REPLY_TOKENS = 32768
"""The benchmark's own token budget, because the KV cache a server reserves is sized from it.

Not "ask for almost nothing": the reply length is irrelevant here — the probe stops at the first
byte — but the cap is not, since a server allocates cache for the budget it was handed before it
emits anything. Measuring at 16 would measure a condition the loop never creates. Override with
``--max-tokens`` to isolate the allocation from the weight load.
"""

_GENERATION_TIMEOUT_S = 1800.0
"""Deliberately far past any plausible load — a bound here would censor the measurement."""

_REPORTED_TIMINGS = ("prompt_n", "prompt_ms", "prompt_per_second", "predicted_per_second")


def benchmark_sized_prefix() -> str:
    """The rules card repeated until it is at least as long as the largest real case prefix."""
    card = _RULES_CARD.read_text(encoding="utf-8") + "\n"
    return card * math.ceil(_TARGET_PREFIX_BYTES / len(card.encode("utf-8")))


def _served_model_id(base_url: str) -> str:
    """Ask the running server which model it is serving, rather than assuming its id.

    Cheap even on a cold server: this is the same endpoint the readiness probe polls, and it
    answers off the bound port without touching the weights.
    """
    payload = httpx.get(f"{base_url}/v1/models", timeout=30.0).json()
    return str(payload["data"][0]["id"])


def _timings(raw: str) -> dict[str, object]:
    """The first frame's prefill timings, or empty when the server reported none."""
    for frame in raw.split("\n\n"):
        if not frame.startswith("data: ") or "[DONE]" in frame:
            continue
        timings = json.loads(frame[len("data: ") :]).get("timings") or {}
        if timings.get("prompt_ms"):
            return {key: timings.get(key) for key in _REPORTED_TIMINGS}
    return {}


def _time_to_first_byte(backend: HttpxBackend, prefix: str, budget: Budget) -> tuple[float, str]:
    """Send one generation and return seconds until the first chunk, plus the whole stream.

    The clock starts before the stream is consumed and stops on the first chunk, because that is
    precisely the window a read timeout bounds: `httpx` applies its read bound per socket read,
    and the first one spans the lazy weight load, the prefill, and the first decoded token.
    """
    start = time.monotonic()
    stream = backend.generate(prefix, "Reply with exactly: OK", budget)
    first = next(stream)
    elapsed = time.monotonic() - start
    return elapsed, (first + b"".join(stream)).decode()


def main(argv: list[str] | None = None) -> int:
    """Serve one model cold, send a benchmark-sized prompt, and print its arrival timings."""
    parser = argparse.ArgumentParser(
        prog="measure-first-byte.py",
        description="Report a freshly served model's time to first byte.",
    )
    parser.add_argument("model", help="A name from the model registry (e.g. Gemma4-31B).")
    parser.add_argument("--startup-timeout", type=float, default=DEFAULT_STARTUP_TIMEOUT_S)
    parser.add_argument("--max-tokens", type=int, default=_REPLY_TOKENS)
    arguments = parser.parse_args(argv)

    prefix = benchmark_sized_prefix()
    print(
        f"[first-byte] prompt characters: {len(prefix)} "
        f"({len(prefix.encode('utf-8'))} bytes, target {_TARGET_PREFIX_BYTES})",
        file=sys.stderr,
    )

    resolved = ModelRegistry.default().resolve(arguments.model)
    server = ModelServer.for_model(resolved)
    ready = time.monotonic()
    with server.running(timeout_s=arguments.startup_timeout) as handle:
        ready_s = time.monotonic() - ready
        served = _served_model_id(handle.base_url)
        print(f"[first-byte] ready in {ready_s:.1f}s: {served}", file=sys.stderr)
        with httpx.Client(timeout=_GENERATION_TIMEOUT_S) as client:
            backend = HttpxBackend(base_url=handle.base_url, client=client, model=served)
            budget = Budget(
                max_attempts=1,
                max_tokens=arguments.max_tokens,
                generation_timeout_s=_GENERATION_TIMEOUT_S,
                oracle_timeout_s=DEFAULT_ORACLE_TIMEOUT_S,
            )
            first_byte_s, raw = _time_to_first_byte(backend, prefix, budget)

    print(
        json.dumps(
            {
                "model": arguments.model,
                "max_tokens": arguments.max_tokens,
                "ready_s": round(ready_s, 1),
                "first_byte_s": round(first_byte_s, 1),
                **_timings(raw),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
