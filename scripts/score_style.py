#!/usr/bin/env -S uv run --quiet python
"""Report the style of the code a benchmarked model produced, case by case.

A scorecard answers whether each case passed. It cannot answer whether the file that passed is
worth keeping, so a run where every case is green and every function is undocumented reads exactly
like one where neither is true. This reports the second number: how many missing docstrings,
missing annotations and unused imports, variables and arguments each case's produced file carries.

    scripts/score_style.py benchmarks/scorecards/code-gpt-oss-20b-1788655677482

A sweep writes one directory per model and the comparison is the point, so it takes as many as
you name and reports each in turn:

    scripts/score_style.py benchmarks/scorecards/code-*

With no argument it reports the most recently written code directory:

    scripts/score_style.py

It reads only artifacts, so it needs no model, no server and no GPU, and it reruns against any
earlier run's produced code — including runs made before this script existed. That is why it is a
separate pass rather than part of the scorer: the produced code is already on disk, so the number
stays re-derivable from the artifact instead of only from a live run.

A scorecard does carry the count — ``benchmarks/run.py --code-out`` fills its ``style_findings``
while the tree it linted is still there. This script is how that same number is recovered
afterwards, from a tree that was retained. It is not recoverable for a run whose tree was not:
``--code-out`` is off by default and points outside the repository, so an older scorecard's count
is a frozen measurement rather than a regenerable one, which is what ``DERIVATION_MAP.md`` records
for ``Scorecard.style_findings``.

What it counts is bounded and stated. A guard against a state the code itself just made
impossible — the defect that reads worst to a human — has no rule in any linter, so it is absent
from these numbers. Read the code for that one; this narrows how much code you must read.

Run from the repository root, like every other command here — the shebang resolves the project's
environment from the working directory. Under a bare `python3` it dies on the first import.

Exit codes: 0 when the report was produced (findings or not — a defect count is the output, not a
failure), and 2 for a usage error such as a directory that holds no produced code. Across several
directories the WORST code wins, and every directory is reported either way — a model that
produced nothing must not be masked by the healthy report that follows it.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
# The repo root itself, not `src`: this is the one script importing the benchmark package, and
# Python seeds sys.path with the script's own directory rather than the working directory.
sys.path.insert(0, str(_REPO_ROOT))

from benchmarks.harness.style import StyleFinding, collect_style_findings  # noqa: E402

_SCORECARDS = _REPO_ROOT / "benchmarks" / "scorecards"
_USAGE_ERROR = 2


def _latest_code_directory() -> Path | None:
    """The most recently written produced-code directory, or ``None`` when none exists.

    Directory names end in a millisecond stamp, so the newest sorts last by name — which beats
    comparing modification times, since reading a tree can leave those unequal to write order.
    """
    directories = sorted(path for path in _SCORECARDS.glob("code-*") if path.is_dir())
    return directories[-1] if directories else None


def _report(findings: tuple[StyleFinding, ...], cases: list[str]) -> None:
    """Print one line per case, then the run total, widest-offender last."""
    per_case = Counter(finding.case_id for finding in findings)
    width = max(len(case) for case in cases)
    for case in cases:
        count = per_case[case]
        rules = Counter(f.rule for f in findings if f.case_id == case)
        breakdown = "  ".join(f"{rule} x{n}" for rule, n in sorted(rules.items())) or "clean"
        print(f"  {case:<{width}}  {count:>3} findings   {breakdown}")
    print(f"\n  {'TOTAL':<{width}}  {len(findings):>3} findings across {len(cases)} case(s)")


def _report_directory(directory: Path) -> int:
    """Print one run directory's per-case report; return that directory's exit code."""
    if not directory.is_dir():
        print(f"not a directory: {directory}", file=sys.stderr)
        return _USAGE_ERROR

    cases = sorted(path.name for path in directory.iterdir() if path.is_dir())
    if not cases:
        print(f"{directory} holds no produced code", file=sys.stderr)
        return _USAGE_ERROR

    print(f"\nstyle findings for {directory.name}\n")
    _report(collect_style_findings(directory), cases)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, lint each produced-code tree, and print the per-case reports."""
    parser = argparse.ArgumentParser(
        prog="score_style.py",
        description="Report style findings in the code benchmarked models produced.",
    )
    parser.add_argument(
        "code_directories",
        nargs="*",
        type=Path,
        help="saved produced-code directories; defaults to the most recent one",
    )
    arguments = parser.parse_args(argv)

    directories: list[Path] = arguments.code_directories
    if not directories:
        latest = _latest_code_directory()
        if latest is None:
            print(f"no produced-code directory found under {_SCORECARDS}", file=sys.stderr)
            return _USAGE_ERROR
        directories = [latest]

    # max() over a generator consumes it, so every directory is reported before the worst code is
    # returned. Short-circuiting here would hide either the error or the reports that follow it.
    return max(_report_directory(directory) for directory in directories)


if __name__ == "__main__":
    raise SystemExit(main())
