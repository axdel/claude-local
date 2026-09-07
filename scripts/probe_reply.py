#!/usr/bin/env -S uv run --quiet python
"""Serve a model, send it one task's real prompt, and print the reply verbatim.

When the loop reports BLOCKED there is exactly one question worth asking — what did the model
actually say? — and answering it by reading loop internals or re-deriving the prompt by hand
gets the prompt wrong, which makes the answer worthless. This sends the *same* stable prefix
`implement()` sends, through the *same* client, and prints what came back plus the frame the
edit parser made of it.

    scripts/probe_reply.py gpt-oss-20b examples/quicksort

The task directory is one holding `spec.md` and a single `*_oracle.py`, matching the layout the
bundled example uses. The model is resident only inside the `running()` block.

A BENCHMARK CASE is not that layout and must not be forced into it: a case names its own impl path
and its ordered context neighbors in `case.toml`, and both reach the prompt. Reconstructing them
here would send a prompt the benchmark does not send, which is exactly the failure this script
exists to avoid. Ask the benchmark instead, which drives the real case through the real driver::

    scripts/benchmark_model.py <model> --only 01_scaffold --stream

What is left here that the benchmark cannot do is `--raw`: the reply before `assistant_content`
normalizes it, which is where a reasoning model's channel markup is visible.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

from claude_local.backend import HttpxBackend
from claude_local.client import ModelClient
from claude_local.edits import extract_file
from claude_local.model_registry import ModelRegistry
from claude_local.model_server import (
    DEFAULT_STARTUP_TIMEOUT_S,
    ModelServer,
)
from claude_local.prompt import PromptBuilder
from claude_local.sandbox import DEFAULT_ORACLE_TIMEOUT_S
from claude_local.sse import Delta, decode_sse
from claude_local.types import Budget, TaskSpec

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RULES_CARD = _REPO_ROOT / "src/claude_local/rules_card.md"


def _spec_from(task_dir: Path, budget: Budget) -> TaskSpec:
    """Read a task directory's spec and its single oracle into a TaskSpec."""
    oracles = sorted(task_dir.glob("*_oracle.py"))
    if len(oracles) != 1:
        raise SystemExit(f"expected exactly one *_oracle.py in {task_dir}, found {len(oracles)}")
    return TaskSpec(
        impl_path=f"src/{task_dir.name}.py",
        spec_text=(task_dir / "spec.md").read_text(encoding="utf-8"),
        test_text=oracles[0].read_text(encoding="utf-8"),
        expected_tests=1,
        budget=budget,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", help="A name from the model registry (e.g. gpt-oss-20b).")
    parser.add_argument("task_dir", type=Path, help="A directory with spec.md and *_oracle.py.")
    parser.add_argument(
        "--tail",
        default="",
        help="Feedback tail appended after the stable prefix, as REPAIR would send it.",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Print the server's whole reply, reasoning channels included, before normalization.",
    )
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--generation-timeout", type=float, default=300.0)
    parser.add_argument("--startup-timeout", type=float, default=DEFAULT_STARTUP_TIMEOUT_S)
    args = parser.parse_args(argv)

    # This probe only decodes — it never runs the oracle — so the oracle deadline stays at the
    # sandbox default and only the generation deadline is worth a flag.
    budget = Budget(
        max_attempts=1,
        max_tokens=args.max_tokens,
        generation_timeout_s=args.generation_timeout,
        oracle_timeout_s=DEFAULT_ORACLE_TIMEOUT_S,
    )
    spec = _spec_from(args.task_dir, budget)
    resolved = ModelRegistry.default().resolve(args.model)
    server = ModelServer.for_model(resolved)

    with server.running(timeout_s=args.startup_timeout) as handle:
        served = handle.served_model_id()
        with httpx.Client(timeout=args.generation_timeout) as http:
            backend = HttpxBackend(base_url=handle.base_url, client=http, model=served)
            # The same prefix implement() sends — read from the builder and the bundled card,
            # never restated here, so a probe cannot answer a question about a prompt the loop
            # does not actually send.
            prefix = PromptBuilder(_RULES_CARD).stable_prefix(spec)
            if args.raw:
                # Straight off the wire: what the client would normalize away, which is where a
                # reasoning model shows its working. Deliberately not ModelClient — the whole
                # point is to see the channels before assistant_content removes them.
                events = decode_sse(backend.generate(prefix, args.tail, budget))
                transcript = "".join(e.text for e in events if isinstance(e, Delta))
                sys.stdout.write(transcript)
                return 0
            generation = ModelClient(backend).generate(prefix, args.tail, budget)

    print(f"--- finish_reason: {generation.finish_reason}", file=sys.stderr)
    print(f"--- derail_reason: {generation.derail_reason}", file=sys.stderr)
    print(f"--- completion_tokens: {generation.completion_tokens}", file=sys.stderr)
    print(f"--- reply chars: {len(generation.text)}", file=sys.stderr)
    reply = extract_file(generation.text)
    print(f"--- extract_file: {'None' if reply is None else reply.path}", file=sys.stderr)
    sys.stdout.write(generation.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
