"""Drive the bundled quicksort task through claude-local's ``implement()`` entry point.

This is an EXAMPLE that teaches the API surface — not a command-line tool (claude-local ships
no CLI). It reads the task's spec and its immutable oracle test from the files beside it,
composes a ``TaskSpec`` + ``Budget``, and runs one bounded red→green loop against an
already-running OpenAI-compatible server. The produced implementation is written to stdout; the
human-readable outcome and the local-economy summary go to stderr — so ``run.py > quicksort.py``
captures only the code.

A running server is a PREREQUISITE: claude-local neither downloads nor serves a model. Point
``--base-url`` (or ``CLAUDE_LOCAL_BASE_URL``) at the server and name the resident model with
``--model`` (or ``CLAUDE_LOCAL_MODEL``).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from claude_local import Budget, Status, TaskSpec, implement

_HERE = Path(__file__).parent
_IMPL_PATH = "src/quicksort.py"
_EXPECTED_TESTS = 7
_DEFAULT_BASE_URL = "http://localhost:8080/v1"
_BUDGET = Budget(max_attempts=5, max_tokens=4096, timeout_s=120.0)


def _build_spec() -> TaskSpec:
    """Compose the quicksort ``TaskSpec`` from the spec + oracle files beside this script."""
    return TaskSpec(
        impl_path=_IMPL_PATH,
        spec_text=(_HERE / "spec.md").read_text(encoding="utf-8"),
        test_text=(_HERE / "quicksort_oracle.py").read_text(encoding="utf-8"),
        expected_tests=_EXPECTED_TESTS,
        budget=_BUDGET,
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Implement quicksort locally against its immutable oracle test."
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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the quicksort task; print the produced code (stdout) + outcome summary (stderr).

    Returns the process exit code: 0 when the oracle went green (``Status.DONE``), 1 for any
    other terminal status, 2 for a usage error (no model named).
    """
    args = _parse_args(argv)
    if not args.model:
        print("error: no model given (pass --model or set CLAUDE_LOCAL_MODEL)", file=sys.stderr)
        return 2

    outcome = implement(_build_spec(), base_url=args.base_url, model=args.model)

    record = outcome.record
    estimated = "  (tokens estimated)" if record.tokens_estimated else ""
    # mean_tokens_per_second is None when no model-seconds elapsed (the div-by-zero guard); show
    # "n/a" rather than crash the summary line on a run that decoded instantly.
    mean = record.mean_tokens_per_second
    rate = f"{mean:.1f}" if mean is not None else "n/a"
    print(outcome.summary, file=sys.stderr)
    print(
        f"[economy] {record.total_calls} call(s), "
        f"{record.total_completion_tokens} completion tokens, "
        f"{record.total_model_seconds:.1f}s decode, "
        f"{rate} tok/s{estimated}",
        file=sys.stderr,
    )
    if outcome.code is not None:
        sys.stdout.write(outcome.code)
    return 0 if outcome.status is Status.DONE else 1


if __name__ == "__main__":
    raise SystemExit(main())
