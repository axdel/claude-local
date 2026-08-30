"""Run the standing model-evaluation benchmark suite against a local model.

The benchmark is a fixed ladder of implementation tasks against one correctly-architected
reference app (a schedule manager): each rung blanks exactly one file and hands the model the
spec, its neighbors, and a hidden correctness oracle. Driving a candidate model through the whole
ladder and scoring it against the oracles yields one comparable scorecard — the same instrument
for every model, so "add a model, bench it" is a single command.

A running server is a PREREQUISITE: claude-local neither downloads nor serves a model. Sync the
reference-app dependencies once (``uv sync --group bench``), point ``--base-url`` (or
``CLAUDE_LOCAL_BASE_URL``) at an already-running OpenAI-compatible server, name the resident model
with ``--model`` (or ``CLAUDE_LOCAL_MODEL``), and run from the repository root::

    uv run python -m benchmarks.run --model <name>

The per-rung table and suite totals print to stderr; ``--out DIR`` also writes the scorecard as
JSON. The process exits 0 only when every rung passed, 1 when any rung failed, and 2 for a usage
error (no model named).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from benchmarks.harness import load_suite, run_suite, score_suite

if TYPE_CHECKING:
    import httpx

    from benchmarks.harness import Scorecard

_HERE = Path(__file__).parent
_BENCHMARK = _HERE / "schedule_manager"
_CASES = _BENCHMARK / "cases"
_GOLDEN_APP = _BENCHMARK / "golden" / "app"
_DEFAULT_BASE_URL = "http://localhost:8080"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the model-evaluation benchmark suite against a local model."
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("CLAUDE_LOCAL_BASE_URL", _DEFAULT_BASE_URL),
        help="OpenAI-compatible server base URL (env: CLAUDE_LOCAL_BASE_URL).",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("CLAUDE_LOCAL_MODEL"),
        help="Model name the server should serve (env: CLAUDE_LOCAL_MODEL).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Directory to also write the scorecard JSON into (created if absent).",
    )
    return parser.parse_args(argv)


def _print_scorecard(scorecard: Scorecard) -> None:
    """Print the per-rung table and suite totals to stderr — the human-readable verdict."""
    print(f"model: {scorecard.model}", file=sys.stderr)
    for rung in scorecard.rungs:
        print(
            f"  {rung.case_id:<20} {rung.status.value:<10} {rung.attempts} attempt(s)",
            file=sys.stderr,
        )
    mean = scorecard.mean_tokens_per_second
    rate = f"{mean:.1f}" if mean is not None else "n/a"
    print(
        f"[suite] {scorecard.rungs_passed}/{scorecard.rungs_total} rungs passed, "
        f"{scorecard.total_completion_tokens} completion tokens, "
        f"{scorecard.total_model_seconds:.1f}s decode, {rate} tok/s",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None, *, http_client: httpx.Client | None = None) -> int:
    """Run the whole suite against the named model; print the scorecard and return an exit code.

    Returns the process exit code: 0 when every rung passed, 1 when any rung failed, 2 for a usage
    error (no model named). An injected ``http_client`` is shared across the suite and left open
    for its caller (the tests replay the transport through it); when omitted, each rung owns a
    per-case client against the real server.
    """
    args = _parse_args(argv)
    if not args.model:
        print("error: no model given (pass --model or set CLAUDE_LOCAL_MODEL)", file=sys.stderr)
        return 2

    cases = load_suite(_CASES, golden_app_root=_GOLDEN_APP)
    results = run_suite(cases, base_url=args.base_url, model=args.model, http_client=http_client)
    scorecard = score_suite(results)
    _print_scorecard(scorecard)
    if args.out is not None:
        written = scorecard.write(args.out)
        print(f"scorecard written to {written}", file=sys.stderr)
    return 0 if scorecard.rungs_passed == scorecard.rungs_total else 1


if __name__ == "__main__":
    raise SystemExit(main())
