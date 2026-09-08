"""The machine CLI — the front door a dispatching orchestrator spawns.

*Machine*, not human: this door is addressed by an orchestrator that has already decided to
offload a task, so its whole shape is dictated by what survives a confined dispatch. Each
constraint below was read off claude-protocol's ``local_dispatch`` rather than assumed, because
none of them is negotiable from this side:

- **The task arrives on stdin as one JSON envelope.** The parent execs the binary with no
  arguments at all (``wrap_argv([binary], ...)``) and writes the task to the child's stdin, so
  argv is a convenience for a human at a terminal and never the dispatch channel.
- **The exit code is the entire report.** The child's stdout and stderr both go to ``DEVNULL``
  deliberately — an untrusted child's stdout is attacker-chosen text whose only upstream consumer
  is an LLM — so every terminal ``Status`` gets its own exit code. Nothing else reaches the parent
  except the isolation worktree's filesystem delta.
- **Server coordinates arrive through the environment.** The parent strips the child's environment
  to an allowlist whose one open prefix is ``CLAUDE_LOCAL_``. That prefix is the configuration
  channel, so the base URL and model name are read from it and not from the envelope: they are
  deployment facts about this machine, not data about the task.
- **The worktree is the current directory, never a temp directory.** The parent runs the child
  with ``cwd`` set to the isolation worktree and its sandbox profile allows writes only beneath
  that root, so ``implement()``'s default managed tempdir would be denied at the kernel. Handing
  it ``cwd`` is also what puts the produced implementation into the delta the parent copies back.
- **claude-local does not serve here.** That profile grants outbound loopback but never
  ``network-bind``, so a confined child cannot start a model server. ``--base-url`` must name one
  that is already listening.

``stdout`` carries exactly one thing — the contract version — because the parent's contract
handshake strips stdout and compares it to a literal. Every diagnostic goes to stderr, where a
human running this by hand reads it and the dispatching parent, by its own deliberate choice,
does not.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, assert_never

from claude_local.backend import BackendUnavailable
from claude_local.entrypoint import implement
from claude_local.model_registry import generation_params_from_json
from claude_local.paths import KeepOnlyViolation
from claude_local.runner import OracleError
from claude_local.sandbox import SandboxUnavailable
from claude_local.types import Budget, ContextFile, Status, TaskSpec

if TYPE_CHECKING:
    from collections.abc import Mapping

CONTRACT_VERSION = "claude-local/1"
"""The contract version the parent binds to, deliberately NOT the package version.

claude-protocol computes its ready set as config AND PATH AND contract version, probing
``claude-local --contract-version`` and comparing the stripped stdout to its own copy of this
literal. Deriving it from ``__version__`` would let a routine release silently un-dispatch
claude-local, so the two version lines stay independent: ``__version__`` names this build, this
names the protocol that build speaks.
"""

EXIT_REJECTED_TASK = 7
"""The caller's input was unusable: a malformed envelope, or missing server coordinates."""

EXIT_HARNESS_FAULT = 8
"""The host is broken, not the task: no reachable server, no kernel sandbox, or a broken oracle."""

SELF_JUDGED_VERDICT = (
    "claude-local: this verdict was computed in the same process that executed the model's code, "
    "which can forge it (D-ORACLE-004). Sound against a weak model, not against a hostile one."
)
"""Printed on the one exit code that admits model-authored code into a real working tree.

Aimed at an operator, deliberately — not at the parent. The parent reads only the exit code and
the filesystem delta and sends both streams to ``DEVNULL``, so no machine channel can carry this
caveat alongside a zero exit; a human running the command by hand is the only party positioned to
weigh it. That is also why the disclosure is prose here rather than a technical control: the
verdict cannot be made trustworthy without moving its computation out of the process that runs
the impl, which D-ORACLE-004 accepted the cost of not doing.
"""


class TaskRejected(ValueError):
    """The task envelope (or the configuration that must accompany it) cannot be run."""


def exit_code_for(status: Status) -> int:
    """Map a terminal ``Status`` to the process exit code that reports it.

    Zero is the load-bearing value: the parent admits the isolation worktree's filesystem delta
    only on a zero exit and discards it whole on any other, so mapping exactly ``DONE`` to 0 is
    what keeps an implementation that never passed its oracle from being copied back.

    The parent collapses every non-zero code into one status, but it interpolates the number into
    the summary an LLM reads, so a distinct code per status is the only diagnostic that survives
    both streams going to ``DEVNULL`` — the difference between a budget that ran out, a model that
    derailed, and a server that faulted. Codes start at 3 so they never collide with the two the
    runtime already owns: 1 is an uncaught exception and 2 is an argparse usage error.
    """
    match status:
        case Status.DONE:
            return 0
        case Status.EXHAUSTED:
            return 3
        case Status.DERAILED:
            return 4
        case Status.BLOCKED:
            return 5
        case Status.FAULTED:
            return 6
    assert_never(status)


def main(argv: list[str] | None = None) -> int:
    """Adapt one invocation to a ``TaskSpec``, run it through ``implement``, return an exit code.

    ``implement()`` stays the composition root; this function only reads the envelope, resolves
    the server coordinates, and translates the outcome into the one channel the parent reads.
    """
    args = _build_parser().parse_args(argv)
    if args.contract_version:
        print(CONTRACT_VERSION)
        return 0
    try:
        return _run(args)
    except TaskRejected as exc:
        print(f"claude-local: {exc}", file=sys.stderr)
        return EXIT_REJECTED_TASK
    except (BackendUnavailable, SandboxUnavailable, OracleError) as exc:
        print(f"claude-local: {exc}", file=sys.stderr)
        return EXIT_HARNESS_FAULT


def _run(args: argparse.Namespace) -> int:
    """Build the task, run it, and report it — leaving both failure channels to the caller.

    Raises:
        TaskRejected: the envelope or the configuration accompanying it cannot be run.
        BackendUnavailable, SandboxUnavailable, OracleError: the host is broken, not the task.
    """
    spec = _task_from_json(_read_envelope(args.task))
    base_url = _required_setting(args.base_url, BASE_URL_ENV, "--base-url")
    model = _required_setting(args.model, MODEL_ENV, "--model")
    try:
        outcome = implement(
            spec,
            base_url=base_url,
            model=model,
            worktree=args.worktree,
            generation_params=args.generation_params,
        )
    except (ValueError, KeepOnlyViolation) as exc:
        # Both are refusals, not faults, and both are statements about the caller's impl_path.
        # ``implement`` documents exactly one ValueError -- a flat path would put the
        # implementation at the worktree root beside the immutable oracle -- and it is the
        # package's only bare ValueError outside TaskSpec/Budget construction. KeepOnlyViolation
        # is the containment boundary refusing a path whose shape is legal but whose target
        # escapes the worktree through a symlink, which only resolution can see. The loop catches
        # its own KeepOnlyViolation where the model aims an edit outside the permitted path, so
        # the only one that reaches here is the store refusing the caller's own impl_path.
        raise TaskRejected(str(exc)) from exc
    print(outcome.summary, file=sys.stderr)
    if outcome.status is Status.DONE:
        print(SELF_JUDGED_VERDICT, file=sys.stderr)
    if args.record_dir is not None:
        outcome.record.write(args.record_dir)
    return exit_code_for(outcome.status)


BASE_URL_ENV = "CLAUDE_LOCAL_BASE_URL"
MODEL_ENV = "CLAUDE_LOCAL_MODEL"

_ENVELOPE_HELP = (
    "JSON task envelope: impl_path, spec_text, test_text, expected_tests, "
    "budget {max_attempts, max_tokens, generation_timeout_s, oracle_timeout_s}, "
    "optional context_files [{path, content}], optional plan_first (bool)."
)


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser. Every option is optional — a dispatch passes none of them."""
    parser = argparse.ArgumentParser(
        prog="claude-local",
        description="Drive a local model through one test-first implementation task.",
        epilog=_ENVELOPE_HELP,
    )
    parser.add_argument(
        "--contract-version",
        action="store_true",
        help="Print the contract version and exit; the orchestrator's contract handshake.",
    )
    parser.add_argument(
        "--task",
        type=Path,
        default=None,
        help="Read the task envelope from this file instead of stdin.",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help=f"An already-running OpenAI-compatible server. Defaults to ${BASE_URL_ENV}.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=f"The model name to request from that server. Defaults to ${MODEL_ENV}.",
    )
    parser.add_argument(
        "--generation-params",
        type=generation_params_from_json,
        default={},
        metavar="JSON",
        help=(
            "JSON object of request-body fields sent with every generation, e.g. "
            "'{\"enable_thinking\": false}'. Take it from the model registry's PARAMS column: "
            "it is the only lever that reaches a chat template whose own default no server flag "
            "can countermand, and the server this CLI talks to was started by someone else."
        ),
    )
    parser.add_argument(
        "--worktree",
        type=Path,
        default=Path.cwd(),
        help="Scratch worktree to implement in. Defaults to the current directory.",
    )
    parser.add_argument(
        "--record-dir",
        type=Path,
        default=None,
        help=(
            "Write the local economy record here. Off by default: under dispatch an undeclared "
            "path in the worktree makes the parent discard the whole delta."
        ),
    )
    return parser


def _read_envelope(task_file: Path | None) -> str:
    """Read the envelope from ``task_file``, else from stdin.

    Refuses an interactive terminal rather than blocking on a read that will never end: a human
    who typed the bare command wants the message, not a hang.
    """
    if task_file is not None:
        try:
            return task_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise TaskRejected(f"could not read task envelope {task_file}: {exc}") from exc
    if sys.stdin.isatty():
        raise TaskRejected(f"no task envelope on stdin. {_ENVELOPE_HELP}")
    return sys.stdin.read()


def _task_from_json(raw: str) -> TaskSpec:
    """Parse the envelope into a ``TaskSpec``, rejecting anything the loop could not run.

    Field validation stays with ``TaskSpec`` and ``Budget`` — their ``__post_init__`` rules are the
    single owner, so a ``ValueError`` from either is re-raised here rather than re-checked. The one
    rule this function adds is the non-blank oracle: ``TaskSpec`` permits empty ``test_text``, but
    an empty oracle collects zero tests, so the loop's validity check never holds, ``Status.DONE``
    becomes unreachable, and the run burns its whole budget before reporting ``EXHAUSTED``. The
    model may never author its own oracle (D-LOOP-001), so the caller must supply it.
    """
    envelope = _decoded_object(raw)
    test_text = _string(envelope, "test_text")
    if not test_text.strip():
        raise TaskRejected(
            "'test_text' is empty: the caller must supply the oracle test, which the model may "
            "never author. An empty oracle collects no tests, so the run can never reach done."
        )
    try:
        return TaskSpec(
            impl_path=_string(envelope, "impl_path"),
            spec_text=_string(envelope, "spec_text"),
            test_text=test_text,
            expected_tests=_integer(envelope, "expected_tests"),
            budget=_budget(envelope),
            context_files=_context_files(envelope),
            plan_first=_plan_first(envelope),
        )
    except ValueError as exc:
        raise TaskRejected(f"task envelope is not a runnable task: {exc}") from exc


def _decoded_object(raw: str) -> Mapping[str, object]:
    """Decode ``raw`` as a JSON object, or reject it."""
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TaskRejected(f"task envelope is not valid JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise TaskRejected(f"task envelope must be a JSON object, got {_kind(decoded)}")
    return decoded


def _string(envelope: Mapping[str, object], key: str) -> str:
    """Read a required string field."""
    value = envelope.get(key)
    if not isinstance(value, str):
        raise TaskRejected(f"task envelope field '{key}' must be a string, got {_kind(value)}")
    return value


def _integer(envelope: Mapping[str, object], key: str) -> int:
    """Read a required integer field. JSON has one number type, so a bool would slip through."""
    value = envelope.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise TaskRejected(f"task envelope field '{key}' must be an integer, got {_kind(value)}")
    return value


def _number(envelope: Mapping[str, object], key: str) -> float:
    """Read a required number field, accepting a JSON integer for a float."""
    value = envelope.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TaskRejected(f"task envelope field '{key}' must be a number, got {_kind(value)}")
    return float(value)


def _budget(envelope: Mapping[str, object]) -> Budget:
    """Read the required budget object. There is no default: an implicit budget is a silent one."""
    nested = envelope.get("budget")
    if not isinstance(nested, dict):
        raise TaskRejected(f"task envelope field 'budget' must be an object, got {_kind(nested)}")
    return Budget(
        max_attempts=_integer(nested, "max_attempts"),
        max_tokens=_integer(nested, "max_tokens"),
        generation_timeout_s=_number(nested, "generation_timeout_s"),
        oracle_timeout_s=_number(nested, "oracle_timeout_s"),
    )


def _context_files(envelope: Mapping[str, object]) -> tuple[ContextFile, ...]:
    """Read the optional ordered read-only neighbors; absent means none."""
    listed = envelope.get("context_files", [])
    if not isinstance(listed, list):
        raise TaskRejected(
            f"task envelope field 'context_files' must be an array, got {_kind(listed)}"
        )
    files: list[ContextFile] = []
    for index, entry in enumerate(listed):
        if not isinstance(entry, dict):
            raise TaskRejected(
                f"task envelope 'context_files[{index}]' must be an object, got {_kind(entry)}"
            )
        files.append(ContextFile(path=_string(entry, "path"), content=_string(entry, "content")))
    return tuple(files)


def _plan_first(envelope: Mapping[str, object]) -> bool:
    """Read the optional plan-first lever; absent means off.

    Absent-means-off is what keeps this an additive envelope field: a caller written against an
    earlier contract sends no such key and gets exactly the run it always got, so the version
    string does not move. A present non-boolean is still rejected — a silently-coerced ``"false"``
    would turn the lever on and quietly change what every later attempt reads.
    """
    supplied = envelope.get("plan_first", False)
    if not isinstance(supplied, bool):
        raise TaskRejected(
            f"task envelope field 'plan_first' must be a boolean, got {_kind(supplied)}"
        )
    return supplied


def _required_setting(supplied: str | None, env_var: str, flag: str) -> str:
    """Resolve a setting from the flag, else the environment, rejecting a blank or missing value.

    The environment is the dispatch channel: the parent's allowlist opens exactly the
    ``CLAUDE_LOCAL_`` prefix, so these settings can reach a confined child no other way.
    """
    value = supplied if supplied is not None else os.environ.get(env_var, "")
    if not value.strip():
        raise TaskRejected(f"no model server configured: pass {flag} or set ${env_var}")
    return value


def _kind(value: object) -> str:
    """Name a JSON value's type for a rejection message; ``None`` reads as the absent field."""
    return "nothing" if value is None else type(value).__name__
