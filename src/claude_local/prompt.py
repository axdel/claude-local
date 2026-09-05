"""Prompt assembly — the byte-stable KV-cacheable prefix and the bounded volatile tail.

D-PROMPT-001, amended by D-CONTEXT-001: the loop reuses the server's prefill KV cache by
sending a prefix that is byte-identical across a task's iterations — the static rules card,
the task spec, any ordered read-only context files, and the IMMUTABLE test, in a fixed layout
with no builder-generated timestamps, run ids, or absolute worktree paths. Pinning the test in
the prefix also blocks the model from rewriting or importing it away. Only the tail varies: the
repair brief — the file the last attempt wrote, then how it failed — each section byte-capped and
path-stripped so neither can starve the other or prime a derail.

Assembly is a pure function of (card, spec): ``stable_prefix`` returns identical bytes for the
same spec, which is what the prefill cache keys on. The card is read once at construction (a
committed static asset), never per call, so no filesystem read sits on the per-iteration path.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from claude_local.runner import TestScore
    from claude_local.types import TaskSpec

# The volatile tail is hard-bounded so a large pytest dump cannot prime the derail guard's
# repetition detector or crowd the decode budget. 4 KiB fits a score line, the failing node ids,
# and a meaningful traceback tail.
FEEDBACK_BYTE_CAP = 4096
_FEEDBACK_TAIL_LINES = 40
_TRUNCATION_MARKER = "\n[...truncated]"

# The previous attempt's own source, capped INDEPENDENTLY of the failure diagnostics above. The
# two sections answer different questions — what you wrote, and how it broke — and a repair needs
# both, so a shared cap would let the larger one decide how much of the smaller survived. 8 KiB
# holds a whole module at the size these tasks produce.
PREVIOUS_SOURCE_BYTE_CAP = 8192
_PREVIOUS_SOURCE_HEADER = "## Your previous attempt — the complete file you wrote, scored below."

# Static prefix scaffolding — part of the byte-stable prefix, so these are frozen constants.
_SPEC_HEADER = "## Implementation task"
TARGET_FILE_LABEL = "Target file:"
"""Wire label prefixing the prompt's target-file line (``Target file: <impl_path>``).

Public because the benchmark replay transport parses the wire by this label; it is the single
owner of the literal, so the prompt producer and the replay parser can never encode it two
different ways (D-BENCH-002 — the benchmark derives shared owners through the public API).
"""
_CONTEXT_HEADER = (
    "## Existing files — read-only, integrate with them, do NOT reimplement or output them."
)
_TEST_HEADER = "## The test — immutable; do not modify or import it away. Make it pass."

# An absolute POSIX directory prefix, anchored at a token boundary (line start, whitespace, or an
# opening delimiter) so relative node ids like ``tests/test_x.py::test_y`` are never touched.
_ABS_PATH_PREFIX = re.compile(r"(?:^|(?<=\s)|(?<=[(=]))/(?:[^/\s:()]+/)+", re.MULTILINE)

# pytest's footer wall-clock (``=== 1 failed in 0.13s ===``, optionally ``(0:01:05)``). Anchored
# to the ``=``-padded footer so a duration the TEST asserted, inside a traceback, is left alone —
# that number is the diagnosis, where this one is only how busy the host was.
_RUN_DURATION = re.compile(r"(?<= )in \d+(?:\.\d+)?s(?: \(\d+:\d{2}:\d{2}\))?(?= *=)")

# The address inside a CPython default repr — ``<pkg.Klass object at 0x104a2f9d0>``, and the same
# ``at 0x…>`` tail on a function or bound method. It is an ASLR draw that differs every process.
# Anchored between ``at `` and the closing ``>`` so a hex literal the TEST asserted is left alone.
_REPR_ADDRESS = re.compile(r"(?<= at )0x[0-9a-fA-F]+(?=>)")
_REPR_ADDRESS_PLACEHOLDER = "0x<addr>"


class PromptBuilder:
    """Assembles the byte-stable prefix and the bounded feedback tail from a static rules card.

    The card is read once at construction; ``stable_prefix`` is a pure function of the card and
    the spec, so identical inputs yield identical bytes — the property the prefill cache reuses.
    """

    def __init__(self, card_path: Path) -> None:
        self._card = card_path.read_text(encoding="utf-8").rstrip("\n")

    def stable_prefix(self, spec: TaskSpec) -> str:
        """Build the KV-cacheable rules, task, context-file, and immutable-test prefix.

        Byte-identical for a given spec (D-PROMPT-001). Introduces no timestamps, run ids, or
        absolute worktree paths that would discard the server's prefill cache.
        """
        parts = [
            self._card,
            "\n\n",
            _SPEC_HEADER,
            "\n",
            TARGET_FILE_LABEL,
            " ",
            spec.impl_path,
            "\n\n",
            spec.spec_text,
            "\n\n",
        ]
        if spec.context_files:
            parts.extend((_CONTEXT_HEADER, "\n\n"))
            for context_file in spec.context_files:
                parts.extend(("### ", context_file.path, "\n\n", context_file.content, "\n\n"))
        parts.extend((_TEST_HEADER, "\n\n", spec.test_text, "\n"))
        return "".join(parts)

    def distill_feedback(
        self, score: TestScore, raw_output: str, previous_attempt_source: str = ""
    ) -> str:
        """Build the repair brief: the file the model last wrote, then how that file failed.

        The card instructs the model to "return the corrected complete file" and to "keep what
        already passed" — two clauses that both name an artifact. Sending only the failure asks it
        to correct code it has never seen and to preserve passing lines it cannot read, so it must
        re-derive the whole implementation from the spec every attempt and guess which part of its
        own output broke. Carrying the source is also what makes the diagnostics legible: a
        traceback naming a line is actionable only beside the file that line is in.

        ``previous_attempt_source`` is empty for the first attempt, which has produced nothing —
        rendering an empty file under the header would state something false about the model's own
        work. Each section is capped separately (see ``PREVIOUS_SOURCE_BYTE_CAP``).

        Every fact about the run — as opposed to about the code — is stripped, so one unchanged
        failure distills to one unchanged brief (INV-004).
        """
        brief = _failure_brief(score, raw_output)
        if not previous_attempt_source:
            return brief
        shown = _cap_bytes(previous_attempt_source, PREVIOUS_SOURCE_BYTE_CAP)
        return f"{_PREVIOUS_SOURCE_HEADER}\n\n{shown}\n\n{brief}"


def _failure_brief(score: TestScore, raw_output: str) -> str:
    """Distill a failing run into a compact, path-stripped, byte-capped failure summary.

    Layout: a hand-readable score line, then the failing node ids, then the run's last lines (with
    lines already shown as node ids removed, so a small output is never printed twice). Node ids
    sit ahead of the tail, so the byte cap only ever trims the low-signal tail.
    """
    stripped = _strip_run_facts(raw_output)
    sections = [_score_header(score)]
    node_ids = _failing_node_ids(stripped)
    tail = _last_lines(stripped, _FEEDBACK_TAIL_LINES)
    if node_ids:
        sections.append(node_ids)
        already = set(node_ids.splitlines())
        tail = "\n".join(line for line in tail.splitlines() if line not in already)
    if tail.strip():
        sections.append(tail)
    return _cap_bytes("\n\n".join(sections), FEEDBACK_BYTE_CAP)


def _strip_run_facts(raw_output: str) -> str:
    """Remove what is true of the RUN, keeping what is true of the CODE (INV-004).

    Three volatile fields reach this layer because none of them can be fixed where the oracle runs:
    the absolute worktree prefix on a traceback location, pytest's own wall-clock, and the ASLR
    address inside a default object repr. Each describes where or when the oracle ran, never the
    implementation under repair — and the tail is the only part of the prompt that varies between
    attempts, so a volatile field left in it makes one unchanged failure ask a different question
    every time it is fed back (D-PROMPT-002).
    """
    without_paths = _ABS_PATH_PREFIX.sub("", raw_output)
    without_durations = _RUN_DURATION.sub("", without_paths)
    return _REPR_ADDRESS.sub(_REPR_ADDRESS_PLACEHOLDER, without_durations)


def _score_header(score: TestScore) -> str:
    """A one-line, human-readable score summary reminding the model of the immutability rule."""
    return (
        f"Result: {score.passed}/{score.expected} passed, {score.failed} failed, "
        f"{score.errors} errored; collected {score.collected} of {score.expected}. "
        "Make the failing tests pass without modifying the test file."
    )


def _failing_node_ids(text: str) -> str:
    """The pytest short-summary lines (``FAILED``/``ERROR`` …) — the highest-signal lines."""
    return "\n".join(line for line in text.splitlines() if line.startswith(("FAILED", "ERROR")))


def _last_lines(text: str, count: int) -> str:
    """The final ``count`` lines of ``text`` — where pytest's summary and last traceback sit."""
    return "\n".join(text.splitlines()[-count:])


def _cap_bytes(text: str, cap: int) -> str:
    """Bound ``text`` to ``cap`` UTF-8 bytes, appending a marker when it had to be trimmed.

    Trims on a byte boundary (``errors="ignore"`` drops a split multibyte char) and reserves room
    for the marker, so the returned string never exceeds ``cap`` bytes.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= cap:
        return text
    keep = cap - len(_TRUNCATION_MARKER.encode("utf-8"))
    return encoded[:keep].decode("utf-8", errors="ignore") + _TRUNCATION_MARKER
