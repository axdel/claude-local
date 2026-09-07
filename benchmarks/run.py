"""Run the standing model-evaluation benchmark against a local model.

The benchmark is a fixed ladder of implementation tasks against one correctly-architected
golden app (a schedule manager): each case blanks exactly one file and hands the model the
spec, its neighbors, and a hidden correctness oracle. Driving a candidate model through the whole
ladder and scoring it against the oracles yields one comparable scorecard — the same instrument
for every model, so "add a model, bench it" is a single command.

A running server is a prerequisite OF THIS MODULE — it scores a model, it does not serve one.
That is a division of labour inside claude-local, not a capability claude-local lacks:
``scripts/benchmark_model.py <name>`` supplies exactly this prerequisite, spawning a registered
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
passed, 1 when any case failed, 2 for a usage error (no server or model named, or an unknown
``--only`` id),
and 3 when the benchmark harness itself faults — the prerequisite server is unreachable, the kernel
sandbox is unavailable, or an oracle is broken. Exit 3 is a broken *host*, distinct from exit 1's
model that simply failed the task. A fault partway through still scores and writes the cases that
finished first: the run did not complete, but what it measured before stopping is not thrown away.

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
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from benchmarks.harness import (
    BenchmarkInterrupted,
    load_cases,
    run_cases,
    score_cases,
    write_produced_code,
)
from benchmarks.harness.style import collect_style_findings
from claude_local import generation_params_from_json

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import TextIO

    import httpx

    from benchmarks.harness import BenchmarkCase, CaseResult, Scorecard
    from claude_local import AttemptProgress

_HERE = Path(__file__).parent
_BENCHMARK = _HERE / "schedule_manager"
_CASES = _BENCHMARK / "cases"
_GOLDEN_APP = _BENCHMARK / "golden" / "app"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the model-evaluation benchmark against a local model."
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("CLAUDE_LOCAL_BASE_URL"),
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
        "--code-out",
        type=Path,
        default=None,
        help=(
            "Directory to keep the code each case produced (created if absent). Off by default, "
            "and point it OUTSIDE this repository: the produced code is model output, often not "
            "valid Python, and any whole-repo analyzer walking the tree will parse it and report "
            "it as this project's own findings. The scorecard records the style count either way."
        ),
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
    parser.add_argument(
        "--plan-first",
        action="store_true",
        help=(
            "Spend one generation per case on an implementation plan, frozen into the prefix. "
            "Off by default, so an unflagged run is comparable with every benchmark-run "
            "taken so far."
        ),
    )
    parser.add_argument(
        "--generation-params",
        type=generation_params_from_json,
        default={},
        metavar="JSON",
        help=(
            "JSON object of request-body fields sent with every generation, e.g. "
            "'{\"enable_thinking\": false}'. The only lever that reaches a chat template whose "
            "own default a server flag cannot countermand."
        ),
    )
    parser.add_argument(
        "--rules-card",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "Rules card to run every case under, replacing the bundled one. The card is the "
            "largest span of the prompt, so running two cards against one model is a real "
            "experiment; the card's digest is stamped on the scorecard either way."
        ),
    )
    return parser.parse_args(argv)


class _UsageError(Exception):
    """An invocation the benchmark declines to run — reported as exit 2, never as a fault.

    The distinction this type carries is the one the exit codes already draw: a usage error is the
    CALLER holding it wrong, where a harness fault (exit 3) is the host being broken. Raising it
    from each preparation phase lets ``main`` translate every refusal at one place, the way the
    layered error contract asks — the phase states what is wrong, the entry point decides how a
    process reports it.
    """


def _resolve_server(args: argparse.Namespace) -> tuple[str, str]:
    """The base URL and model name, refusing either when neither argv nor the environment set it.

    Both fall back to an environment variable rather than a literal default, so argparse cannot
    mark them ``required`` and the check lands here. Refusing rather than guessing is D-CLI-002,
    already ratified for the machine CLI: a defaulted address silently sends the run at whatever
    happens to be listening on that port. The machine CLI enforces the same rule through its own
    failure channel — an exception it maps to an exit code — which is why the guarantee is shared
    across the front doors while the wording each prints is not.

    Raises:
        _UsageError: no server, or no model, was named.
    """
    if not args.base_url:
        raise _UsageError("no server given (pass --base-url or set CLAUDE_LOCAL_BASE_URL)")
    if not args.model:
        raise _UsageError("no model given (pass --model or set CLAUDE_LOCAL_MODEL)")
    return args.base_url, args.model


def _prepare_cases(args: argparse.Namespace) -> dict[str, BenchmarkCase]:
    """The cases this run will drive: the committed ladder, under the run mode, narrowed to --only.

    Raises:
        _UsageError: ``--only`` named a case id the ladder does not have.
    """
    cases = load_cases(_CASES, golden_app_root=_GOLDEN_APP)
    if args.plan_first:
        # Applied to loaded cases rather than passed down to the loader: the mode is how a case is
        # run, not what it is, so the compared pair is provably the same fixtures.
        cases = {case_id: case.planning_first() for case_id, case in cases.items()}
    if args.only:
        selected = set(args.only)  # membership is the whole access pattern here
        unknown = sorted(selected - set(cases))
        if unknown:
            # Never narrow to nothing and exit 0: an empty run satisfies passed == total, so a
            # mistyped id would report a green benchmark that never ran a single case.
            raise _UsageError(
                f"unknown case id(s): {', '.join(unknown)} — the ladder is {', '.join(cases)}"
            )
        # Rebuilt from the loaded mapping, never from argv, so the ladder order is the committed
        # one: a case must never be driven from a later case's position in the progression.
        cases = {case_id: case for case_id, case in cases.items() if case_id in selected}
    return cases


def _attempt_verdict(progress: AttemptProgress) -> str:
    """One phrase naming what an attempt produced — the oracle's count, or why there is none.

    The four arms are the loop's own terminal shapes. Each is a distinct thing to do about it:
    a partial score means keep going, a fault means fix the server, a derail means the decode
    bounds are wrong for this task, and a block means the model produced nothing to score.

    The block arm quotes the loop's recorded reason rather than restating a phrase, because two
    unrelated problems land there: a reply that framed nothing (the model is not answering in
    protocol) and one framed at a path outside the permitted impl (it is answering in protocol
    and aiming elsewhere). A fixed phrase named only the first, so the second read as the first.
    """
    if progress.score is not None:
        return f"{progress.score.passed}/{progress.score.expected} oracle tests passed"
    if progress.generation.fault is not None:
        return f"server fault: {progress.generation.fault}"
    if progress.generation.derail_reason is not None:
        return f"derailed ({progress.generation.derail_reason.value})"
    return progress.blocked_reason or "no usable file frame"


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
            f"{budget.generation_timeout_s:.0f}s decode, {budget.oracle_timeout_s:.0f}s oracle)"
        )

    def delta(self, text: str) -> None:
        if not self._stream_text:
            return
        self._out.write(text)
        self._out.flush()
        self._mid_line = not text.endswith("\n")

    def attempt(self, progress: AttemptProgress) -> None:
        rate = progress.generation.tokens_per_second
        # Two facts the score alone cannot show: that an attempt replayed its predecessor (the
        # repeat), and that its prompt carried an escalation (the loop's answer to one). Both are
        # additive to the verdict — without them a watcher sees a run end short of its budget with
        # no visible cause. "repeat" describes what this attempt WROTE; "nudged" what it was ASKED.
        marks = [
            label
            for label, fired in (
                ("repeat", progress.repeats_previous),
                ("nudged", progress.nudged),
            )
            if fired
        ]
        annotation = f"  [{', '.join(marks)}]" if marks else ""
        self._line(
            f"    attempt {progress.attempt}  "
            f"{progress.generation.completion_tokens:6d} tok  "
            f"{progress.generation.seconds:6.1f}s  "
            f"{'  n/a' if rate is None else f'{rate:5.1f}'} tok/s  ->  "
            f"{_attempt_verdict(progress)}{annotation}"
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
    print(f"rules card: {scorecard.rules_card_digest}", file=sys.stderr)
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

    Returns the process exit code: 0 when every case passed, 1 when any case failed, 2 for any
    ``_UsageError`` the preparation phases raise, and 3 when the benchmark harness itself faults
    (unreachable server,
    unavailable sandbox, or broken oracle) — writing a scorecard for the cases that finished before
    the fault, since exit 3 says the run stopped, not that it measured nothing. An injected
    ``http_client`` is shared across the cases
    and left open for its caller (the tests replay the transport through it); when omitted, each
    case owns a per-case client against the real server.
    """
    args = _parse_args(argv)
    try:
        base_url, model = _resolve_server(args)
        cases = _prepare_cases(args)
    except _UsageError as refused:
        print(f"error: {refused}", file=sys.stderr)
        return 2
    # A harness fault is the HOST failing, so it always ends the run at exit 3 — but the cases
    # that finished first are hours of decode already paid for, and scoring them costs nothing.
    # The fault is reported after the scorecard is written, so the last line an operator reads is
    # why the run stopped rather than a filename that makes it look like it completed.
    interruption: BaseException | None = None
    try:
        results = run_cases(
            cases,
            base_url=base_url,
            model=model,
            http_client=http_client,
            progress=ConsoleProgress(stream_text=args.stream),
            generation_params=args.generation_params,
            rules_card_path=args.rules_card,
        )
    except BenchmarkInterrupted as interrupted:
        results = interrupted.completed
        interruption = interrupted.__cause__ or interrupted
    if not results:
        print(f"error: benchmark harness fault: {interruption}", file=sys.stderr)
        return 3
    scorecard = score_cases(results)
    # The scorecard says how many oracle tests passed; only the code says whether what passed them
    # is worth keeping. Counting the style findings HERE, into the scorecard, is what makes the
    # code itself optional: the judgement survives as a number even when the files it came from
    # do not, so a scorecard is a whole record rather than half of a pair that must stay together.
    # One clock read serves both writers below, so their names carry the same stamp.
    stamp_ms = int(time.time() * 1000)
    scorecard = replace(
        scorecard, style_findings=_count_style_findings(results, scorecard, stamp_ms)
    )
    _print_scorecard(scorecard)
    if args.code_out is not None:
        code_directory = write_produced_code(results, scorecard.model, args.code_out, stamp_ms)
        print(f"produced code written to {code_directory}", file=sys.stderr)
    if args.out is not None:
        written = scorecard.write(args.out, stamp_ms)
        print(f"scorecard written to {written}", file=sys.stderr)
    if interruption is not None:
        # Last, so it is what an operator reads after the artifact lines — and 3 outranks the
        # pass/fail verdict below, because a partial ladder never earns a green.
        print(f"error: benchmark harness fault: {interruption}", file=sys.stderr)
        print(
            f"the scorecard covers the {len(results)} case(s) that finished before it",
            file=sys.stderr,
        )
        return 3
    return 0 if scorecard.cases_passed == scorecard.cases_total else 1


def _count_style_findings(
    results: Sequence[CaseResult], scorecard: Scorecard, stamp_ms: int
) -> int | None:
    """Lint the code this run produced; return the count, or ``None`` when it produced none.

    The files are written to a temporary directory purely to be linted — ruff reads a tree, not
    strings in memory — and that directory is discarded. Persisting them is a separate, opt-in
    choice (``--code-out``), so the measurement never depends on the caller having made it.
    """
    with tempfile.TemporaryDirectory() as scratch:
        directory = write_produced_code(results, scorecard.model, Path(scratch), stamp_ms)
        if not any(directory.iterdir()):
            return None
        return len(collect_style_findings(directory))


if __name__ == "__main__":
    raise SystemExit(main())
