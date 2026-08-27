"""Prompt assembly — the byte-stable KV-cacheable prefix and the bounded volatile tail.

D-PROMPT-001: the loop reuses the server's prefill KV cache by sending a prefix that is
byte-identical across a task's iterations — the static rules card, the task spec, and the
IMMUTABLE test, in a fixed layout with no timestamps, run ids, or absolute paths. Pinning the
test in the prefix also blocks the model from rewriting or importing it away. Only the tail
varies: distilled feedback, byte-capped and path-stripped so a failure never primes a derail.

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

# Static prefix scaffolding — part of the byte-stable prefix, so these are frozen constants.
_SPEC_HEADER = "## Implementation task"
_TARGET_LABEL = "Target file:"
_TEST_HEADER = "## The test — immutable; do not modify or import it away. Make it pass."

# An absolute POSIX directory prefix, anchored at a token boundary (line start, whitespace, or an
# opening delimiter) so relative node ids like ``tests/test_x.py::test_y`` are never touched.
_ABS_PATH_PREFIX = re.compile(r"(?:^|(?<=\s)|(?<=[(=]))/(?:[^/\s:()]+/)+", re.MULTILINE)


class PromptBuilder:
    """Assembles the byte-stable prefix and the bounded feedback tail from a static rules card.

    The card is read once at construction; ``stable_prefix`` is a pure function of the card and
    the spec, so identical inputs yield identical bytes — the property the prefill cache reuses.
    """

    def __init__(self, card_path: Path) -> None:
        self._card = card_path.read_text(encoding="utf-8").rstrip("\n")

    def stable_prefix(self, spec: TaskSpec) -> str:
        """The KV-cacheable prefix: rules card + task spec + the immutable test.

        Byte-identical for a given spec (D-PROMPT-001). Carries no timestamps, run ids, or
        absolute paths, so no per-call mutation discards the server's prefill cache.
        """
        return (
            f"{self._card}\n\n"
            f"{_SPEC_HEADER}\n"
            f"{_TARGET_LABEL} {spec.impl_path}\n\n"
            f"{spec.spec_text}\n\n"
            f"{_TEST_HEADER}\n\n"
            f"{spec.test_text}\n"
        )

    def distill_feedback(self, score: TestScore, raw_output: str) -> str:
        """Distill a failing run into a compact, path-stripped, byte-capped tail.

        Layout: a hand-readable score line, then the failing node ids, then the run's last lines
        (with lines already shown as node ids removed, so a small output is never printed twice).
        Node ids sit ahead of the tail, so the byte cap only ever trims the low-signal tail.
        """
        stripped = _ABS_PATH_PREFIX.sub("", raw_output)
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
