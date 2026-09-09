#!/usr/bin/env -S uv run --quiet python
"""Serve one registered model and drive the bundled example against it, end to end.

This is the whole chain a downstream user composes, in one re-runnable command: resolve a name
through the model registry, spawn the MLX server for it, and run the documented example — the
exact `examples/quicksort/run.py` invocation the README prints — against the server that just
came up. The model is resident only inside the `running()` block, so teardown is structural
rather than remembered, on success and on failure alike.

It is a script rather than a hand-typed sequence because it is needed more than once: the
multi-model sweep runs this same chain per model, and a procedure re-derived from memory each
time drifts silently from the code it exercises.

    scripts/e2e_local_model.py gpt-oss-20b

The model store is read from CLAUDE_LOCAL_MODELS when set. That override is what makes the
script usable from a git worktree, whose own `models/` holds the registry but no weights.
"""

from __future__ import annotations

import argparse
import json
import subprocess  # nosec B404 (argv is built here from registry data, never shell-interpreted)
import sys
import time
from pathlib import Path

from claude_local.model_registry import ModelRegistry
from claude_local.model_server import (
    DEFAULT_STARTUP_TIMEOUT_S,
    ModelServer,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_EXAMPLE = _REPO_ROOT / "examples/quicksort/run.py"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", help="A name from the model registry (e.g. gpt-oss-20b).")
    parser.add_argument(
        "--startup-timeout",
        type=float,
        default=DEFAULT_STARTUP_TIMEOUT_S,
        help="Seconds to wait for the model to load; a cold first read streams 11+ GB off disk.",
    )
    args = parser.parse_args(argv)

    resolved = ModelRegistry.default().resolve(args.model)
    server = ModelServer.for_model(resolved)
    print(f"[e2e] weights   : {resolved.path}", file=sys.stderr)
    print(f"[e2e] command   : {' '.join(server.command)}", file=sys.stderr)

    started = time.monotonic()
    with server.running(startup_timeout_s=args.startup_timeout) as handle:
        ready_after = time.monotonic() - started
        served = handle.served_model_id()
        print(f"[e2e] ready in  : {ready_after:.1f}s (pid {handle.pid})", file=sys.stderr)
        print(f"[e2e] serving   : {served}", file=sys.stderr)

        # The documented consumer path, invoked exactly as the README prints it — not an
        # in-process call to implement(), which would skip the surface a user actually drives.
        # The row's PARAMS ride along as JSON: the registry declares them, so this chain has to
        # carry them rather than let the example guess. Serving a model with its FLAGS while
        # dropping its PARAMS is a half-configured run that looks fully configured.
        example = [sys.executable, str(_EXAMPLE), "--base-url", handle.base_url, "--model", served]
        if resolved.generation_params:
            example += ["--generation-params", json.dumps(dict(resolved.generation_params))]
        done = subprocess.run(  # noqa: S603
            example,
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        print(f"[e2e] example exit: {done.returncode}", file=sys.stderr)
        print(done.stderr, file=sys.stderr)
        # Not "produced_code": that names the benchmark's saved TREE, and this is one task's one
        # implementation file. Two concepts under one name is how a reader starts looking for a
        # directory that a single-example run never writes.
        produced = json.dumps({"produced_implementation": done.stdout}, indent=2)
        print(produced[:400], file=sys.stderr)

    print(f"[e2e] server torn down after {time.monotonic() - started:.1f}s", file=sys.stderr)
    return done.returncode


if __name__ == "__main__":
    raise SystemExit(main())
