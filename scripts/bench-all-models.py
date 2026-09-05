#!/usr/bin/env -S uv run --quiet python
"""Sweep the whole catalog: benchmark every model in turn, one resident at a time.

`benchmark_model.py` already owns the per-model chain — resolve, serve, benchmark, tear down — and
says so in its own docstring. This adds exactly the one thing it does not do: iteration over the
catalog, with a per-model failure boundary. It re-implements none of that chain; it invokes it, so
the two can never drift into two different ideas of how a model is served.

One model is resident at a time. Weights run 12-45 GB against this machine's unified memory and
local inference is memory-bandwidth-bound, so two concurrent servers would slow both and risk the
swapper. Serving is therefore strictly sequential, and the previous model is gone before the next
is read.

**A failing model is a row, not an abort.** Each model runs in its own subprocess, so one that
cannot load, times out, or crashes the harness costs its own row in the summary and nothing else —
a sweep exists to produce a verdict per model, and losing eight results to the ninth's bad weights
would defeat it.

Weights live outside any worktree, so ``CLAUDE_LOCAL_MODELS`` must point at the store. No path is
hardcoded here: where the store lives is the registry's fact, not this script's.

Run from the repository root, like every other command here — the shebang resolves the project's
environment from the working directory, so this script imports ``claude_local`` and hands its own
interpreter to each per-model subprocess. Under a bare ``python3`` it dies on the first import::

    CLAUDE_LOCAL_MODELS=/path/to/models scripts/bench-all-models.py
    CLAUDE_LOCAL_MODELS=/path/to/models scripts/bench-all-models.py --skip Muse-Glimmer-30B-6bit

Exit code is 0 when every model was benchmarked, 1 when any model failed to produce a scorecard.
That is deliberately not the benchmark's own pass/fail: a model scoring 0/7 has been measured, and
measuring it is what this script is for.
"""

from __future__ import annotations

import argparse
import subprocess  # nosec B404 (argv is built from catalog data, never shell-interpreted)
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from claude_local.model_registry import ModelRegistry  # noqa: E402

_PER_MODEL_SCRIPT = Path(__file__).resolve().parent / "benchmark_model.py"
_SCORECARD_DIR = _REPO_ROOT / "benchmarks" / "scorecards"

# The largest catalogued row is 44.9 GB, and a cold first read streams all of it off disk before
# the server answers. The per-model script's own 600 s default is tuned for a small model.
_STARTUP_TIMEOUT_S = 900.0


def _catalogued_names(registry: ModelRegistry) -> list[str]:
    """Every model the catalog declares, in file order — which is the order the sweep reports in.

    Read off the registry's own file rather than a hardcoded list, so a model added to the catalog
    is swept without touching this script.
    """
    lines = [
        line
        for line in registry.registry_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    return [line.split("|", 1)[0] for line in lines[1:]]  # lines[0] is the NAME|REPO|... header


def _scorecards() -> set[Path]:
    """Every scorecard on disk now — the before/after sets whose difference is the verdict."""
    return set(_SCORECARD_DIR.glob("*.json"))


def _bench_one(name: str) -> tuple[bool, float]:
    """Run the per-model chain for ``name``. Returns (a scorecard appeared, wall-clock seconds).

    The verdict is "a new scorecard exists", never the exit code. Exit 1 is ambiguous by design:
    `benchmark_model.py` returns the benchmark's own 1 when a CASE failed — a real measurement —
    and also exits 1 when it raises before serving anything, which is no measurement at all.
    Reading the exit code alone reports an unknown model as "scored". The artifact cannot lie.

    Output is INHERITED, not captured: the benchmark reports each case and attempt as it happens,
    and a captured pipe would withhold every one of those lines until the model was already done.
    """
    before = _scorecards()
    started = time.monotonic()
    subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(_PER_MODEL_SCRIPT),
            name,
            "--startup-timeout",
            str(_STARTUP_TIMEOUT_S),
        ],
        cwd=_REPO_ROOT,
        check=False,
    )
    return bool(_scorecards() - before), time.monotonic() - started


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", action="append", metavar="NAME", help="Sweep just this model.")
    parser.add_argument("--skip", action="append", metavar="NAME", help="Exclude this model.")
    args = parser.parse_args(argv)

    names = args.only or _catalogued_names(ModelRegistry.default())
    names = [name for name in names if name not in set(args.skip or ())]

    print(f"[sweep] {len(names)} model(s): {', '.join(names)}", file=sys.stderr, flush=True)

    results: list[tuple[str, bool, float]] = []
    for index, name in enumerate(names, start=1):
        print(f"\n[sweep] === {index}/{len(names)}  {name} ===", file=sys.stderr, flush=True)
        scored, seconds = _bench_one(name)
        results.append((name, scored, seconds))

    print("\n[sweep] === complete ===", file=sys.stderr)
    for name, scored, seconds in results:
        verdict = "scored" if scored else "FAILED — no scorecard"
        print(f"[sweep]   {name:26} {verdict:22} {seconds / 60:5.1f} min", file=sys.stderr)
    return 0 if all(scored for _, scored, _ in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
