"""Run the standing model-evaluation benchmark against a local model.

The benchmark is a fixed ladder of implementation tasks against one correctly-architected
golden app (a schedule manager): each case blanks exactly one file and hands the model the
spec, its neighbors, and a hidden correctness oracle. Driving a candidate model through the whole
ladder and scoring it against the oracles yields one comparable scorecard — the same instrument
for every model, so "add a model, bench it" is a single command.

A running server is a prerequisite OF THIS MODULE — it scores a model, it does not serve one.
That is a division of labour inside claude-local, not a capability claude-local lacks:
``scripts/benchmark_model.py <name>`` supplies exactly this prerequisite, spawning a catalogued
model through ``model_server`` and tearing it down on the way out, then invoking the command
below. Reach for it unless a server is already up. (Downloading is the one thing nothing here
does: weights are user-initiated, always.)

Sync the reference-app dependencies once (``uv sync --group bench``), point ``--base-url`` (or
``CLAUDE_LOCAL_BASE_URL``) at an already-running OpenAI-compatible server, name the resident model
with ``--model`` (or ``CLAUDE_LOCAL_MODEL``), and run from the repository root::

    uv run python -m benchmarks.run --model <name>

The ladder is reported LIVE to stderr as it runs — a case line as each case opens, a line as each
attempt resolves, and the case's verdict as it closes — because a full run takes tens of minutes
and a scorecard printed only at the end leaves a watcher with nothing to watch. ``--stream`` adds
the model's own raw text as it decodes. The per-case table and benchmark totals still print at the
end; ``--out DIR`` also writes the scorecard as JSON. The process exits 0 only when every case
passed, 1 when any case failed, 2 for a usage error (no model named, or an unknown ``--only`` id),
and 3 when the benchmark harness itself faults — the prerequisite server is unreachable, the kernel
sandbox is unavailable, or an oracle is broken. Exit 3 is a broken *host*, distinct from exit 1's
model that simply failed the task.

``--only <case_id>`` narrows the run to the named cases and is repeatable. A scorecard is a claim
about a whole ladder, so the full run stays the default — but when the question is why ONE case
failed, paying the other six to ask it makes the question too expensive. Pair it with ``--stream``
to read what the model actually wrote for that case::

    uv run python -m benchmarks.run --model <name> --only 01_scaffold --stream
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from benchmarks.harness import load_cases, run_cases, score_cases
from claude_local import BackendUnavailable, OracleError, SandboxUnavailable

if TYPE_CHECKING:
    from typing import TextIO

    import httpx

    from benchmarks.harness import BenchmarkCase, CaseResult, Scorecard
    from claude_local import AttemptProgress

_HERE = Path(__file__).parent
_BENCHMARK = _HERE / "schedule_manager"
_CASES = _BENCHMARK / "cases"
_GOLDEN_APP = _BENCHMARK / "golden" / "app"
_DEFAULT_BASE_URL = "http://localhost:8080"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the model-evaluation benchmark against a local model."
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
    parser.add_argument(
        "--stream",
        action="store_true",
        help="Also print the model's raw text as it decodes; the ladder is live either way.",
    )
    parser.add_argument(
        "--only",
        action="append",
        metavar="CASE_ID",
        help="Run just this case; repeatable. Ladder order is kept whatever order they are given.",
    )
    return parser.parse_args(argv)


def _attempt_verdict(progress: AttemptProgress) -> str:
    """One phrase naming what an attempt produced — the oracle's count, or why there is none.

    The four arms are the loop's own terminal shapes. Each is a distinct thing to do about it:
    a partial score means keep going, a fault means fix the server, a derail means the decode
    bounds are wrong for this task, and a missing frame means the model is not answering in
    protocol at all.
    """
    if progress.score is not None:
        return f"{progress.score.passed}/{progress.score.expected} oracle tests passed"
    if progress.generation.fault is not None:
        return f"server fault: {progress.generation.fault}"
    if progress.generation.derail_reason is not None:
        return f"derailed ({progress.generation.derail_reason.value})"
    return "no usable file frame"


class ConsoleProgress:
    """Renders a running benchmark to a stream, one line per event, flushed as it happens.

    This is the whole reason the harness carries a progress seam: the loop and the driver report
    facts and never print, so the rendering lives out here at the outermost adapter. Every write
    is flushed, because a buffered live view is not a live view.

    ``stream_text`` opts into the model's raw decoded text. It is off by default — a full ladder
    is seven cases of multi-thousand-token decodes, which buries the scoreboard — and on when a
    watcher wants to see the model actually working.
    """

    def __init__(self, *, stream_text: bool = False, out: TextIO | None = None) -> None:
        self._stream_text = stream_text
        self._out = out if out is not None else sys.stderr
        self._mid_line = False

    def case_started(self, case_id: str, case: BenchmarkCase, index: int, total: int) -> None:
        budget = case.task.budget
        self._line(
            f"[{index}/{total}] {case_id}  ->  {case.task.impl_path}  "
            f"(up to {budget.max_attempts} attempts x {budget.max_tokens} tokens, "
            f"{budget.timeout_s:.0f}s oracle)"
        )

    def delta(self, text: str) -> None:
        if not self._stream_text:
            return
        self._out.write(text)
        self._out.flush()
        self._mid_line = not text.endswith("\n")

    def attempt(self, progress: AttemptProgress) -> None:
        rate = progress.generation.tokens_per_second
        # A repeat ends the run short of its budget, so the marker is additive to the verdict:
        # without it a watcher sees an unexplained halt, with it the cause and the score both.
        repeat = (
            " (verbatim repeat of the previous attempt — stopping)"
            if (progress.repeats_previous)
            else ""
        )
        self._line(
            f"    attempt {progress.attempt}  "
            f"{progress.generation.completion_tokens:6d} tok  "
            f"{progress.generation.seconds:6.1f}s  "
            f"{'  n/a' if rate is None else f'{rate:5.1f}'} tok/s  ->  "
            f"{_attempt_verdict(progress)}{repeat}"
        )

    def case_finished(self, result: CaseResult) -> None:
        record = result.outcome.record
        self._line(
            f"    = {result.outcome.status.value} after {record.attempts} attempt(s), "
            f"{record.total_completion_tokens} tokens in {record.total_model_seconds:.1f}s"
        )

    def _line(self, text: str) -> None:
        """Write one structured line, breaking out of a half-written stream of model text first."""
        if self._mid_line:
            self._out.write("\n")
            self._mid_line = False
        self._out.write(f"{text}\n")
        self._out.flush()


def _print_scorecard(scorecard: Scorecard) -> None:
    """Print the per-case table and benchmark totals to stderr — the human-readable verdict."""
    print(f"model: {scorecard.model}", file=sys.stderr)
    for case in scorecard.cases:
        line = f"  {case.case_id:<20} {case.status.value:<10} {case.attempts} attempt(s)"
        if case.length_capped:
            line += f", {case.length_capped} length-capped"
        if case.fault is not None:
            line += f" — fault: {case.fault}"
        print(line, file=sys.stderr)
    mean = scorecard.mean_tokens_per_second
    rate = f"{mean:.1f}" if mean is not None else "n/a"
    print(
        f"[benchmark] {scorecard.cases_passed}/{scorecard.cases_total} cases passed, "
        f"{scorecard.total_completion_tokens} completion tokens, "
        f"{scorecard.total_model_seconds:.1f}s decode, {rate} tok/s",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None, *, http_client: httpx.Client | None = None) -> int:
    """Run the benchmark against the named model; print the scorecard and return an exit code.

    The ladder renders live to stderr through ``ConsoleProgress`` as it runs, so a multi-case run
    is watchable rather than silent until the end.

    Returns the process exit code: 0 when every case passed, 1 when any case failed, 2 for a usage
    error (no model named), and 3 when the benchmark harness itself faults (unreachable server,
    unavailable sandbox, or broken oracle). An injected ``http_client`` is shared across the cases
    and left open for its caller (the tests replay the transport through it); when omitted, each
    case owns a per-case client against the real server.
    """
    args = _parse_args(argv)
    if not args.model:
        print("error: no model given (pass --model or set CLAUDE_LOCAL_MODEL)", file=sys.stderr)
        return 2

    cases = load_cases(_CASES, golden_app_root=_GOLDEN_APP)
    if args.only:
        selected = set(args.only)  # membership is the whole access pattern here
        unknown = sorted(selected - set(cases))
        if unknown:
            # Never narrow to nothing and exit 0: an empty run satisfies passed == total, so a
            # mistyped id would report a green benchmark that never ran a single case.
            print(
                f"error: unknown case id(s): {', '.join(unknown)} — "
                f"the ladder is {', '.join(cases)}",
                file=sys.stderr,
            )
            return 2
        # Rebuilt from the loaded mapping, never from argv, so the ladder order is the committed
        # one: a case must never be driven from a later case's position in the progression.
        cases = {case_id: case for case_id, case in cases.items() if case_id in selected}
    try:
        results = run_cases(
            cases,
            base_url=args.base_url,
            model=args.model,
            http_client=http_client,
            progress=ConsoleProgress(stream_text=args.stream),
        )
    except (BackendUnavailable, SandboxUnavailable, OracleError) as fault:
        print(f"error: benchmark harness fault: {fault}", file=sys.stderr)
        return 3
    scorecard = score_cases(results)
    _print_scorecard(scorecard)
    if args.out is not None:
        written = scorecard.write(args.out)
        print(f"scorecard written to {written}", file=sys.stderr)
    return 0 if scorecard.cases_passed == scorecard.cases_total else 1


if __name__ == "__main__":
    raise SystemExit(main())
