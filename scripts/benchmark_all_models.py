#!/usr/bin/env -S uv run --quiet python
"""Sweep the whole registry: benchmark every model in turn, one resident at a time.

`benchmark_model.py` already owns the per-model chain — resolve, serve, benchmark, tear down — and
says so in its own docstring. This adds exactly the one thing it does not do: iteration over the
registry, with a per-model failure boundary. It re-implements none of that chain; it invokes it, so
the two can never drift into two different ideas of how a model is served.

One model is resident at a time. Weights run 12-45 GB against this machine's unified memory and
local inference is memory-bandwidth-bound, so two concurrent servers would slow both and risk the
swapper. Serving is therefore strictly sequential, and the previous model is gone before the next
is read.

**A failing model is a row, not an abort.** Each model runs in its own subprocess, so one that
cannot load, times out, or crashes the harness costs its own row in the summary and nothing else —
a sweep exists to produce a verdict per model, and losing eight results to the ninth's bad weights
would defeat it.

**A model with no weights is not even a row.** The registry claims what exists upstream; the store
proves what is on disk. A registered model that was never pulled — or was deleted to reclaim
space — is announced and skipped before it costs a subprocess, so the sweep's exit code keeps
meaning "every model that could be measured was". Naming a model with ``--only`` opts out of that
filter: an explicit request for absent weights fails loudly rather than vanishing from the run.

``--rules-card`` runs the whole sweep under a card other than the bundled one. The card is the
largest span of the prompt, so sweeping the registry twice under two cards is the experiment that
says which card a given model is actually better under; each scorecard carries its card's digest,
so the two sweeps stay distinguishable after the fact.

Weights live outside any worktree, so ``CLAUDE_LOCAL_MODELS`` must point at the store. No path is
hardcoded here: where the store lives is the registry's fact, not this script's.

Run from the repository root, like every other command here — the shebang resolves the project's
environment from the working directory, so this script imports ``claude_local`` and hands its own
interpreter to each per-model subprocess. Under a bare ``python3`` it dies on the first import::

    CLAUDE_LOCAL_MODELS=/path/to/models scripts/benchmark_all_models.py
    CLAUDE_LOCAL_MODELS=/path/to/models scripts/benchmark_all_models.py --skip GLM-4.7-Flash

Exit code is 0 when every model was benchmarked, 1 when any model failed to produce a scorecard.
That is deliberately not the benchmark's own pass/fail: a model scoring 0/7 has been measured, and
measuring it is what this script is for.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import signal
import subprocess  # nosec B404 (argv is built from registry data, never shell-interpreted)
import sys
import time
from pathlib import Path

from claude_local.model_registry import ModelRegistry

_PER_MODEL_SCRIPT = Path(__file__).resolve().parent / "benchmark_model.py"
_REPO_ROOT = Path(__file__).resolve().parent.parent
# Python seeds sys.path with the script's own directory rather than the working directory, so the
# repo root has to be added before the benchmark harness that owns the scorecard location resolves.
sys.path.insert(0, str(_REPO_ROOT))

from benchmarks.harness.scorer import DEFAULT_SCORECARD_DIR  # noqa: E402

_PER_MODEL_CEILING_S = 4 * 60 * 60.0
"""Wall-clock ceiling for one model's whole chain — the sweep's failure boundary against a hang.

Without it the docstring's promise above is only half true: a model that *fails* costs its own
row, but a model that *hangs* costs the sweep. The bounded worst case is already hours (seven
cases, five attempts, a generous decode budget each), so this is set well beyond a legitimate run
and exists purely so an unattended overnight sweep reaches model two.
"""


def _scorecards() -> set[Path]:
    """Every scorecard on disk now — the before/after sets whose difference is the verdict."""
    return set(DEFAULT_SCORECARD_DIR.glob("*.json"))


def _bench_one(name: str, rules_card: Path | None) -> tuple[bool, str, float]:
    """Run the per-model chain for ``name``. Returns (scored, how it ended, wall-clock seconds).

    The verdict is "a new scorecard exists", never the exit code, which is ambiguous by design.
    The exit code is still read, because it alone can NAME a failure the missing artifact merely
    proves: it decides nothing and describes everything (D-SWEEP-001).

    The startup budget is not passed: the per-model script defaults to the same
    ``DEFAULT_STARTUP_TIMEOUT_S`` the server module owns, so re-stating it here would be a second
    writer to one number.

    Output is INHERITED, not captured: the benchmark reports each case and attempt as it happens,
    and a captured pipe would withhold every one of those lines until the model was already done.
    """
    before = _scorecards()
    started = time.monotonic()
    command = [sys.executable, str(_PER_MODEL_SCRIPT), name]
    if rules_card is not None:
        command.extend(("--rules-card", str(rules_card)))
    ending = _run_bounded(command)
    return bool(_scorecards() - before), ending, time.monotonic() - started


def _run_bounded(command: list[str]) -> str:
    """Run one per-model chain under the sweep's ceiling and say how it ended.

    Spawned into its own session so the ceiling can reap the whole tree. ``subprocess.run``'s own
    timeout kills only the direct child, and the child that matters here is a GRANDCHILD: the
    per-model script's model server, holding tens of gigabytes of unified memory and the port the
    next model in the sweep is about to need. Killing the process group is what makes the ceiling
    a real boundary instead of a way to leak a server per hung model.
    """
    process = subprocess.Popen(command, cwd=_REPO_ROOT, start_new_session=True)  # noqa: S603
    try:
        return f"exit {process.wait(timeout=_PER_MODEL_CEILING_S)}"
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        process.wait()
        return f"timed out after {_PER_MODEL_CEILING_S / 3600:.0f}h"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", action="append", metavar="NAME", help="Sweep just this model.")
    parser.add_argument("--skip", action="append", metavar="NAME", help="Exclude this model.")
    parser.add_argument(
        "--rules-card",
        type=Path,
        default=None,
        metavar="PATH",
        help="Rules card every model runs under, replacing the bundled one.",
    )
    args = parser.parse_args(argv)

    # --only is the caller naming models outright, so it is NOT filtered against the store: a model
    # asked for by name and missing its weights must fail loudly in the per-model script, never be
    # silently dropped from a sweep the caller believes ran it.
    names = args.only or list(ModelRegistry.default().servable_names())
    names = [name for name in names if name not in set(args.skip or ())]

    print(f"[sweep] {len(names)} model(s): {', '.join(names)}", file=sys.stderr, flush=True)

    results: list[tuple[str, bool, str, float]] = []
    for index, name in enumerate(names, start=1):
        print(f"\n[sweep] === {index}/{len(names)}  {name} ===", file=sys.stderr, flush=True)
        scored, ending, seconds = _bench_one(name, args.rules_card)
        results.append((name, scored, ending, seconds))

    print("\n[sweep] === complete ===", file=sys.stderr)
    for name, scored, ending, seconds in results:
        verdict = "scored" if scored else f"FAILED — {ending}"
        print(f"[sweep]   {name:26} {verdict:26} {seconds / 60:5.1f} min", file=sys.stderr)
    return 0 if all(scored for _, scored, _, _ in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
