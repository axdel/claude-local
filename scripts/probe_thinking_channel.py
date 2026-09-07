#!/usr/bin/env -S uv run --quiet python
"""Serve one registered model and report, from the wire, whether it emits a reasoning channel.

The question this answers cannot be answered by reading the request. `enable_thinking` is a
*soft* request: mlx_vlm resolves it against the model's own chat template, and when no
preference is expressed it deliberately passes the template `None` rather than `False` --
"Preserve a model template's native default when the server default is off and the request did
not express a reasoning preference" (mlx_vlm/server/request_normalization.py). A template whose
native default is on therefore thinks anyway. So the only honest evidence is the response.

It sweeps two prompts across every control mlx_vlm exposes and reports what the wire actually
returned for each pairing. Both axes are deliberately complete rather than sampled: a model can
ignore one control and obey another, and it can stay quiet on an easy prompt and deliberate on a
hard one, so a subset of either axis proves nothing about what it leaves out.

For each request it reports:

- `reasoning_content` deltas -- the clean case. Reasoning arrives in its own field, so the
  content channel stays pure and the edit applier never sees it.
- reasoning leaked INTO `content` -- the derailing case. Prose the model believes is private
  thinking lands in the same channel as the file it was asked to write, and the applier has to
  parse a fenced block out of an essay.

    scripts/probe_thinking_channel.py gpt-oss-20b

The model store is read from CLAUDE_LOCAL_MODELS when set, which is what makes this usable
from a git worktree whose own `models/` holds the registry but no weights.
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx

from claude_local.model_registry import ModelRegistry
from claude_local.model_server import (
    DEFAULT_STARTUP_TIMEOUT_S,
    ModelServer,
)

_PROMPTS: tuple[tuple[str, str], ...] = (
    ("trivial", "Reply with exactly the word: ok"),
    (
        "reasoning-inviting",
        "A worker processes 3 items in the time another processes 5. Together they finish 240 "
        "items. How many did the faster one process? Answer with the number only.",
    ),
)
"""Two prompts, because thinking can be conditional on difficulty and one prompt cannot see that.

The trivial one catches a model that thinks *unconditionally* -- deliberation spent on "say ok"
is pure waste on every call. The second invites reasoning, and is the one that actually predicts
behaviour on a benchmark case: a model that stays quiet on "say ok" but deliberates the moment a
task has any structure looks clean under the trivial prompt alone and is not. Measured: Gemma4-31B
reports zero on the trivial prompt, which on its own would have been read as "never thinks".

Both ask for a bare answer, so any prose on the wire is deliberation rather than the reply.
"""

_THINKING_BUDGET_PROBE = 16
"""A budget small enough that any honoured cap is unmistakable against an uncapped baseline."""

_CONTROLS: tuple[dict[str, object], ...] = (
    {},
    {"enable_thinking": False},
    {"reasoning_effort": "none"},
    {"thinking_budget": _THINKING_BUDGET_PROBE},
)
"""Every thinking control mlx_vlm's request normalizer reads, plus the no-preference baseline.

Order mirrors the server's own precedence: an explicit ``enable_thinking`` wins outright, an
OpenAI reasoning field is consulted only in its absence, and the server default applies only
when neither is set. They are sent one at a time for exactly that reason -- send two and the
loser's effect is unobservable. ``reasoning_effort`` takes any of the normalizer's disabling
values (none/off/disabled/false/0); ``none`` stands for the set.

The last rung is the different kind. The first three ASK the model not to think and are soft:
they resolve against its chat template, which is free to think anyway. ``thinking_budget`` is a
stopping criterion that ENFORCES -- so it is sent deliberately WITHOUT a no-think preference,
because capping a model that was never going to think measures nothing. It is skipped, with a
reason, when a draft model is configured: the server raises rather than degrades there
("thinking_budget is not supported with speculative decoding in the server").
"""


def _probe(
    http: httpx.Client, base_url: str, model: str, params: dict[str, object], prompt: str
) -> None:
    """Post one streamed completion and print what each channel actually carried."""
    body: dict[str, object] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 2048,
        "stream": True,
        **params,
    }
    content, reasoning, other_keys = "", "", set()
    finish_reason: str | None = None
    with http.stream("POST", f"{base_url}/v1/chat/completions", json=body) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            choices = json.loads(line[6:]).get("choices") or [{}]
            delta = choices[0].get("delta") or {}
            other_keys.update(delta.keys())
            content += delta.get("content") or ""
            reasoning += delta.get("reasoning_content") or delta.get("reasoning") or ""
            finish_reason = choices[0].get("finish_reason") or finish_reason

    print(f"\n--- request params: {params or '(none)'}")
    print(f"    model           : {model}")
    # Without this the two char counts below are unreadable: zero content under a `length` finish
    # means the budget went entirely to reasoning, which is the opposite conclusion from zero
    # content under `stop` — that one says the model emitted no content channel at all.
    print(f"    finish_reason   : {finish_reason}")
    print(f"    delta keys seen : {sorted(other_keys)}")
    print(f"    reasoning chars : {len(reasoning)}")
    print(f"    content chars   : {len(content)}")
    print(f"    reasoning[:200] : {reasoning[:200]!r}")
    print(f"    content[:200]   : {content[:200]!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", help="A name from the model registry (e.g. gpt-oss-20b).")
    parser.add_argument(
        "--startup-timeout",
        type=float,
        default=DEFAULT_STARTUP_TIMEOUT_S,
        help="Seconds to wait for the model to load; a cold first read streams 11+ GB off disk.",
    )
    args = parser.parse_args(argv)

    resolved = ModelRegistry.default().resolve(args.model)
    server = ModelServer.for_model(resolved)
    print(f"[probe] weights        : {resolved.path}", file=sys.stderr)
    print(f"[probe] server flags   : {resolved.flags or '(none)'}", file=sys.stderr)
    print(f"[probe] registry params: {dict(resolved.generation_params) or '(none)'}")

    with server.running(startup_timeout_s=args.startup_timeout) as handle:
        served = handle.served_model_id()
        with httpx.Client(timeout=httpx.Timeout(300.0, connect=10.0)) as http:
            for label, prompt in _PROMPTS:
                print(f"\n==== prompt: {label}")
                for params in _CONTROLS:
                    if "thinking_budget" in params and resolved.draft_path is not None:
                        print("\n--- skipped thinking_budget: server raises with a draft model")
                        continue
                    _probe(http, handle.base_url, served, params, prompt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
