#!/usr/bin/env -S uv run --quiet python
"""Serve one registered model and run the standing benchmark against it, end to end.

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

``--only <case_id>`` (repeatable) and ``--stream`` are forwarded to the benchmark unchanged, which
turns this into the diagnosis path too: one case, with the model's raw text, at a fraction of a
full ladder's cost.

    scripts/benchmark_model.py gpt-oss-20b --only 01_scaffold --stream

The model store is read from CLAUDE_LOCAL_MODELS when set. That override is what makes the script
usable from a git worktree, whose own `models/` holds the registry but no weights.

Run from the repository root, like every other command here — the shebang resolves the project's
environment from the working directory. Under a bare `python3` it dies on the first import.

Exit codes are `benchmarks/run.py`'s, passed through unchanged: 0 when every case passed, 1 when a
case failed, 2 for a usage error, and 3 for a harness fault. A model that never becomes ready
raises out of `running()` instead — failing to serve is not a benchmark result, and reporting it as
one would put a host problem on the model's scorecard.
"""

from __future__ import annotations

import argparse
import json
import subprocess  # nosec B404 (argv is built here from registry data, never shell-interpreted)
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from claude_local.model_registry import ModelRegistry
from claude_local.model_server import (
    DEFAULT_STARTUP_TIMEOUT_S,
    ModelServer,
)

_BENCHMARK_MODULE = "benchmarks.run"
_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_SCORECARD_DIR = _REPO_ROOT / "benchmarks" / "scorecards"


def benchmark_command(
    *,
    base_url: str,
    served: str,
    out: Path,
    generation_params: Mapping[str, object],
    stream: bool,
    only: Sequence[str],
    plan_first: bool = False,
    rules_card: Path | None = None,
) -> list[str]:
    """Build the documented benchmark invocation for a server that is already up.

    Separate from the serving block that runs it because deciding what the command SAYS is pure,
    while running it needs 20 GB of resident weights. Kept apart, the forwarding is checkable
    without them — and a dropped argument is exactly the failure a subprocess cannot report, since
    the benchmark would run perfectly well in whatever configuration it was left in.

    Args:
        base_url: Where the just-started server is listening.
        served: The model id the server REPORTS, not the registry name — the scorecard is labelled
            with it, and a name the server never served would attribute the run to other weights.
        out: Directory the scorecard and produced code are written into.
        generation_params: The row's request-body fields; an empty mapping forwards no flag.
        stream: Whether to also print the model's raw text as it decodes.
        plan_first: Whether each case spends one generation on a plan before implementing.
        only: Case ids to run, forwarded one flag each because the benchmark appends them.
        rules_card: Rules card to run under; ``None`` forwards no flag, leaving the bundled card.

    Returns:
        The argv, ready for ``subprocess.run``. Never shell-interpreted.
    """
    command = [
        sys.executable,
        "-m",
        _BENCHMARK_MODULE,
        "--base-url",
        base_url,
        "--model",
        served,
        "--out",
        str(out),
    ]
    if generation_params:
        # JSON because that is what the flag declares. The registry already owns the key=value
        # syntax these came from, so re-serializing to it here would give one format two writers.
        # dict() because a resolved row hands over a read-only mapping and json.dumps takes dicts.
        command.extend(("--generation-params", json.dumps(dict(generation_params))))
    if stream:
        command.append("--stream")
    if plan_first:
        command.append("--plan-first")
    if rules_card is not None:
        command.extend(("--rules-card", str(rules_card)))
    for case_id in only:
        # Forwarded, never checked here: the benchmark loads the ladder, so it is the only thing
        # that knows which ids exist, and a second validator would drift from it.
        command.extend(("--only", case_id))
    return command


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
        default=DEFAULT_STARTUP_TIMEOUT_S,
        help="Seconds to wait for the model to load; a cold first read streams 11+ GB off disk.",
    )
    parser.add_argument(
        "--stream",
        action="store_true",
        help="Also print the model's raw text as it decodes; the ladder is live either way.",
    )
    parser.add_argument(
        "--only",
        action="append",
        metavar="CASE_ID",
        help="Run just this case; repeatable. Forwarded verbatim, so the benchmark owns validity.",
    )
    parser.add_argument(
        "--plan-first",
        action="store_true",
        help="Spend one generation per case on a plan, frozen into the prefix. Off by default.",
    )
    parser.add_argument(
        "--rules-card",
        type=Path,
        default=None,
        metavar="PATH",
        help="Rules card to run under, replacing the bundled one. Its digest lands on the card.",
    )
    args = parser.parse_args(argv)

    resolved = ModelRegistry.default().resolve(args.model)
    server = ModelServer.for_model(resolved)
    print(f"[bench] weights  : {resolved.path}", file=sys.stderr)
    print(f"[bench] command  : {' '.join(server.command)}", file=sys.stderr)
    # Echoed because the failure this configuration prevents is a SILENT one: a model left in the
    # wrong mode still answers, still scores, and reports nothing unusual. Seeing the row's
    # parameters in the run's own header is what makes a misconfigured benchmark-run visible
    # while it runs.
    print(f"[bench] params   : {json.dumps(dict(resolved.generation_params))}", file=sys.stderr)

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
        command = benchmark_command(
            base_url=handle.base_url,
            served=served,
            out=args.out,
            generation_params=resolved.generation_params,
            stream=args.stream,
            only=args.only or (),
            plan_first=args.plan_first,
            rules_card=args.rules_card,
        )
        done = subprocess.run(command, cwd=_REPO_ROOT, check=False)  # noqa: S603

    print(f"[bench] server torn down after {time.monotonic() - started:.1f}s", file=sys.stderr)
    return done.returncode


if __name__ == "__main__":
    raise SystemExit(main())
