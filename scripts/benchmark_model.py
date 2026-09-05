#!/usr/bin/env python3
"""Serve one catalogued model and run the standing benchmark against it, end to end.

`benchmarks/run.py` scores a model over the whole case ladder, but takes an already-running server
as a prerequisite — claude-local never downloads or serves a model on the benchmark's behalf. This
supplies exactly that prerequisite and nothing else: resolve a name through the model registry,
spawn the MLX server for it, run the documented benchmark command against the server that just came
up, and tear the server down on the way out. Teardown is structural rather than remembered, on
success and on failure alike, so a 20 GB resident model cannot outlive the run that needed it.

It is a script rather than a hand-typed sequence because it is needed more than once: the
multi-model sweep runs this same chain per model, and a procedure re-derived from memory each time
drifts silently from the code it exercises.

    scripts/benchmark_model.py gpt-oss-20b

The model store is read from CLAUDE_LOCAL_MODELS when set. That override is what makes the script
usable from a git worktree, whose own `models/` holds the catalog but no weights.

Exit codes are `benchmarks/run.py`'s, passed through unchanged: 0 when every case passed, 1 when a
case failed, 2 for a usage error, and 3 for a harness fault. A model that never becomes ready
raises out of `running()` instead — failing to serve is not a benchmark result, and reporting it as
one would put a host problem on the model's scorecard.
"""

from __future__ import annotations

import argparse
import subprocess  # nosec B404 (argv is built here from catalog data, never shell-interpreted)
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from claude_local.model_registry import ModelRegistry  # noqa: E402
from claude_local.model_server import ModelServer  # noqa: E402

_BENCHMARK_MODULE = "benchmarks.run"
_DEFAULT_SCORECARD_DIR = _REPO_ROOT / "benchmarks" / "scorecards"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", help="A name from the model registry (e.g. gpt-oss-20b).")
    parser.add_argument(
        "--out",
        type=Path,
        default=_DEFAULT_SCORECARD_DIR,
        help="Directory the scorecard JSON is written into (created if absent).",
    )
    parser.add_argument(
        "--startup-timeout",
        type=float,
        default=600.0,
        help="Seconds to wait for the model to load; a cold first read streams 11+ GB off disk.",
    )
    parser.add_argument(
        "--stream",
        action="store_true",
        help="Also print the model's raw text as it decodes; the ladder is live either way.",
    )
    args = parser.parse_args(argv)

    resolved = ModelRegistry.default().resolve(args.model)
    server = ModelServer.for_model(resolved)
    print(f"[bench] weights  : {resolved.path}", file=sys.stderr)
    print(f"[bench] command  : {' '.join(server.command)}", file=sys.stderr)

    started = time.monotonic()
    with server.running(timeout_s=args.startup_timeout) as handle:
        ready_after = time.monotonic() - started
        served = handle.served_model_id()
        print(f"[bench] ready in : {ready_after:.1f}s (pid {handle.pid})", file=sys.stderr)
        print(f"[bench] serving  : {served}", file=sys.stderr)

        # The documented consumer path, invoked as the benchmark README prints it — not an
        # in-process call to run_cases(), which would skip the surface a user actually drives.
        # Output is INHERITED, never captured: the benchmark reports each case and attempt live,
        # and a captured pipe would hold every one of those lines until the run was already over.
        command = [
            sys.executable,
            "-m",
            _BENCHMARK_MODULE,
            "--base-url",
            handle.base_url,
            "--model",
            served,
            "--out",
            str(args.out),
        ]
        if args.stream:
            command.append("--stream")
        done = subprocess.run(command, cwd=_REPO_ROOT, check=False)  # noqa: S603

    print(f"[bench] server torn down after {time.monotonic() - started:.1f}s", file=sys.stderr)
    return done.returncode


if __name__ == "__main__":
    raise SystemExit(main())
