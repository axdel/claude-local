#!/usr/bin/env python3
"""Record one real streaming chat-completions response, verbatim, as a byte fixture.

`tests/fixtures/sse/README.md` ranks a captured session above a schema-derived one and says
outright that a capture should replace a derived fixture whenever one becomes available. That
makes capture a recurring procedure rather than a one-off, so it lives here instead of being
retyped: a hand-run capture drifts from the request body the loop actually sends, and a fixture
recorded against the wrong body tests the decoder against a wire no server produces.

The request body is built by `HttpxBackend.generate` itself, not restated here, so the captured
bytes are by construction the answer to the request the loop makes — system prefix, user tail,
streaming, usage accounting on. Only the transport is local to this script.

    scripts/capture_sse_fixture.py gpt-oss-20b tests/fixtures/sse/harmony_channel_stream.bytes \
        --user "Reply with exactly: OK"

The model is resident only inside the `running()` block, so teardown is structural. The store is
read from CLAUDE_LOCAL_MODELS when set — the override that makes this runnable from a worktree.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from claude_local.backend import HttpxBackend  # noqa: E402
from claude_local.model_registry import ModelRegistry  # noqa: E402
from claude_local.model_server import ModelServer  # noqa: E402
from claude_local.types import Budget  # noqa: E402


def _served_model_id(base_url: str) -> str:
    """Ask the running server which model it is serving, rather than assuming its id."""
    payload = httpx.get(f"{base_url}/v1/models", timeout=30.0).json()
    return str(payload["data"][0]["id"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", help="A name from the model registry (e.g. gpt-oss-20b).")
    parser.add_argument("destination", type=Path, help="Where to write the raw bytes.")
    parser.add_argument("--system", default="You are a terse assistant.", help="Stable prefix.")
    parser.add_argument("--user", required=True, help="The changing tail — the prompt to send.")
    parser.add_argument("--max-tokens", type=int, default=200)
    parser.add_argument("--timeout", type=float, default=300.0, help="Per-generation wall clock.")
    parser.add_argument("--startup-timeout", type=float, default=600.0)
    args = parser.parse_args(argv)

    resolved = ModelRegistry.default().resolve(args.model)
    server = ModelServer.for_model(resolved)

    with server.running(timeout_s=args.startup_timeout) as handle:
        served = _served_model_id(handle.base_url)
        print(f"[capture] serving {served} (pid {handle.pid})", file=sys.stderr)
        # A capture is one generation, so the attempt and wall-clock bounds are formalities;
        # only max_tokens shapes the recorded bytes.
        budget = Budget(max_attempts=1, max_tokens=args.max_tokens, timeout_s=args.timeout)
        with httpx.Client(timeout=args.timeout) as client:
            backend = HttpxBackend(base_url=handle.base_url, client=client, model=served)
            raw = b"".join(backend.generate(args.system, args.user, budget))

    args.destination.write_bytes(_with_model_id_replaced(raw, served, args.model))
    print(f"[capture] wrote {args.destination.stat().st_size} bytes", file=sys.stderr)
    return 0


def _with_model_id_replaced(raw: bytes, served: str, name: str) -> bytes:
    """Swap the served model id for the catalog name in every recorded frame.

    The one substitution a capture from this project makes, and it is structural rather than
    cosmetic: models are named by absolute store path so that a repo id can never fall through
    to `snapshot_download` (`ModelServer.for_model`), so every chunk echoes back the capturing
    machine's home directory. Committing that verbatim would put one developer's path in a public
    fixture and, at one copy per token, would dominate the file.

    Nothing under test is touched. `decode_sse` reads `choices`, `delta.content`, `finish_reason`,
    and `usage` — never `model` — so frame boundaries, delta granularity, marker placement, and
    the usage trailer are all preserved exactly as recorded.
    """
    return raw.replace(served.encode(), name.encode())


if __name__ == "__main__":
    raise SystemExit(main())
