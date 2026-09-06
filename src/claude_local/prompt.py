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
from itertools import takewhile
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

# Escalating questions for a model that has stalled — an attempt that bought nothing. A stall
# proves the prompt is an absorbing state: same brief in, no further out. The only way out is to
# ask something else, and under a deterministic decoder that is the ONLY lever there is: sampling
# knobs are ignored by the server, and an emptied tail just reproduces the first attempt.
#
# A rung must be true of BOTH stall shapes, because the loop escalates on both and the model can
# check which one it sent. So no rung claims the file was identical: that is true of a verbatim
# repeat and false of a plateau, where the model wrote something genuinely new that got no further.
# What holds either way is the consequence — the score did not move — so that is what each rung
# asserts. Telling a model it repeated itself when it did not is a false premise about its own
# work, and a weak model given one argues with it instead of fixing the code.
#
# The rungs escalate in what they license the model to discard. The first keeps work that already
# passes, which matters: a case sitting at six of seven oracle tests must not be told to start
# over. The second gives up on the shape once narrowing has failed twice.
#
# Each rung POINTS AT the counterevidence hoisted above it (``_repeat_escalation``) rather than
# asking the model to go find it. That split is deliberate: a controlled study of repair prompting
# on frozen small code models found executed counterevidence carries the repair signal while
# generic retry instructions have minimal independent effect, so the rung's job is to say what to
# do with a concrete failure the loop has already extracted — never to request the extraction.
_NUDGE_LADDER = (
    "Your last attempt scored no better than the best you have already reached, so whatever you "
    "changed did not reach the failure. Writing it that way again cannot change that. Fix the "
    "failure shown directly above: make that path produce the value the test expects, and change "
    "nothing that already passes.",
    "You have now failed to improve twice, so narrowing has failed and what is wrong is the "
    "approach, not a detail inside it. Abandon the structure you have been writing. Read the "
    "test's failing assertions in order and build the implementation up from what they require — "
    "a different shape, not the same one restated.",
)

# The correction for an attempt that produced no file to score — a reply carrying no frame, or one
# framed at a path outside the single writable one. Measured, not imagined: driven through the
# benchmark's auth-service case, an agentic coding model answers "Let me read the existing files"
# and four tool_call blocks naming files it was already given, then stops. It finished normally and
# was not truncated; it simply answered a question nobody asked.
#
# So the rung leads with what is now true and unarguable — no file exists — before denying the
# premise the reply was built on. The denial is stated twice over, as a fact about the world (there
# is nothing to explore) and about the interface (there is no tool to call), because a model in
# this state has misread which of the two it is in.
#
# It POINTS AT the frame rules rather than restating them, for the same reason the repeat rungs
# point at their counterevidence: the rules card owns that shape, and a second copy here would be a
# second writer free to drift from it on the next edit.
_REFRAME = (
    "That reply produced no file. Nothing was written and nothing was run, so the task stands "
    "exactly where it did before you answered. There is nothing here to explore and no tool for "
    "you to call: every file you are permitted to read is already above, and that is all of them. "
    "Answer with the implementation file itself, framed exactly as the rules above require, and "
    "with nothing else around it."
)

# The unscorable reply, hoisted above the rung that answers it — the same counterevidence-then-
# imperative shape the repeat ladder uses. The cap matches that one: enough to show the model the
# opening it actually sent, never enough to push the imperative out of reach.
UNSCORABLE_REPLY_BYTE_CAP = 512
_UNSCORABLE_REPLY_HEADER = "## What you sent back — verbatim, and it is not a file."

# The first failure's executed counterevidence, hoisted to sit directly above the rung it leads. It
# is one statement and its result, so a small cap is generous; bounding it keeps a long assertion
# diff from displacing the imperative that follows it.
REPEAT_EVIDENCE_BYTE_CAP = 512
_REPEAT_EVIDENCE_HEADER = (
    "## What your file actually does — the first failure, verbatim from the run."
)

# pytest marks the failing statement with ``>`` and each line of the concrete result with ``E``.
# Captured from a real run rather than recalled: an assertion failure renders ``> assert add(2,2)
# == 4`` / ``E assert 0 == 4``, and a raised exception renders ``> return a - b`` / ``E TypeError:
# …`` — one shape, whether the failing line sits in the test or in the implementation. ``ERROR``
# short-summary lines do not collide, having no space in that position.
_FAILING_STATEMENT_MARKER = "> "
_FAILURE_DETAIL_MARKER = "E "

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

# The plan-first request, and the header its frozen answer sits under. The plan section is
# appended LAST in the prefix, after the test, for two reasons that happen to agree. Cache: the
# plan is generated from a prefix that does not yet contain it, so putting it last leaves that
# shorter prefix a strict byte-prefix of every later one and the plan call warms exactly the head
# the attempts reuse. Reading order: the model sees what it must satisfy, then the approach it
# chose for satisfying it.
#
# The request asks for an approach and forbids code, because a plan that contains the
# implementation is not a plan — it is a first attempt scored by nothing, frozen into the prefix
# where no oracle can ever contradict it.
_PLAN_HEADER = "## Your plan — you wrote this before implementing; follow it or better it."
_PLAN_REQUEST = (
    "Before writing any code, plan the implementation. Name the functions or classes the test "
    "requires, the data each one holds, and the order you will build them in. Work from the "
    "test's assertions — they define what must exist. Be brief and concrete, and write no code: "
    "this is the approach, and you will be asked for the file itself next."
)

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

    def plan_request(self) -> str:
        """The tail that asks for an implementation plan, before any code is written.

        A tail rather than a prefix section: it is the one thing said to the model that differs
        between the plan call and the attempts that follow, so keeping it out of the prefix is
        what lets the plan call warm the head those attempts reuse.
        """
        return _PLAN_REQUEST

    def stable_prefix(self, spec: TaskSpec, plan: str = "") -> str:
        """Build the KV-cacheable rules, task, context-file, immutable-test, and plan prefix.

        Byte-identical for a given spec and plan (D-PROMPT-001). Introduces no timestamps, run
        ids, or absolute worktree paths that would discard the server's prefill cache.

        ``plan`` is the frozen answer to ``plan_request``, computed once per task and passed
        unchanged on every attempt; empty means the lever is off and the prefix is exactly what it
        was before plan-first existed. It is appended last, so the plan call's own prefix — built
        with no plan — stays a strict byte-prefix of every later one.
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
        if plan:
            parts.extend(("\n", _PLAN_HEADER, "\n\n", plan, "\n"))
        return "".join(parts)

    def nudge_for(self, repeat_count: int) -> str | None:
        """The escalation for the ``repeat_count``-th replay, or ``None`` past the last rung.

        ``None`` is the loop's stop signal, and it is what keeps the original saving of stopping on
        a replay: after every rung has been tried, there is no further question this card knows how
        to ask, and continuing would spend the rest of the budget re-buying an answer already given
        three ways. ``repeat_count`` is 1-based — the first replay takes the first rung.
        """
        if repeat_count < 1 or repeat_count > len(_NUDGE_LADDER):
            return None
        return _NUDGE_LADDER[repeat_count - 1]

    def reframe_for(self, raw_output: str) -> str:
        """The correction for a reply that produced no file: its own words, then what to send.

        A sibling of ``nudge_for`` and the same kind of thing — counterevidence, then a single
        imperative about what to do differently. It differs only in what earned it: that one
        answers a model that repeated a scored file, this one a model that never produced one, so
        the evidence is the raw reply rather than an executed failure.

        Quoting the reply back is the load-bearing half. A model that sends tool calls into a loop
        with no tools believes it is somewhere else, and an instruction alone leaves that belief
        untouched — it has already read the rules once and answered this way regardless. Its own
        text beside the statement that no file exists is the part it cannot argue with.
        """
        shown = _cap_bytes(raw_output, UNSCORABLE_REPLY_BYTE_CAP)
        return f"{_UNSCORABLE_REPLY_HEADER}\n\n{shown}\n\n{_REFRAME}"

    def distill_feedback(
        self,
        score: TestScore,
        raw_output: str,
        previous_attempt_source: str = "",
        nudge: str = "",
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

        ``nudge`` is the escalation for a model that replayed its last answer (``nudge_for``). It
        goes last, closest to generation: everything above it is context, and it is the only
        imperative about what to do differently, and it is led by the first failure's own
        counterevidence (``_repeat_escalation``). It is short and bounded, so it cannot displace
        anything the caps kept.

        Every fact about the run — as opposed to about the code — is stripped, so one unchanged
        failure distills to one unchanged brief (INV-004). The nudge is a function of how many
        times the model has replayed, never of the run, so that property survives it.
        """
        sections = []
        if previous_attempt_source:
            shown = _cap_bytes(previous_attempt_source, PREVIOUS_SOURCE_BYTE_CAP)
            sections.append(f"{_PREVIOUS_SOURCE_HEADER}\n\n{shown}")
        sections.append(_failure_brief(score, raw_output))
        if nudge:
            sections.append(_repeat_escalation(nudge, raw_output))
        return "\n\n".join(sections)


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


def _repeat_escalation(nudge: str, raw_output: str) -> str:
    """Lead the ``nudge`` with the first failure's executed counterevidence.

    A replay means the model has already read the failure brief above and answered it with the same
    file, so restating that brief is not what breaks the tie — but an instruction on its own is the
    weakest available intervention. Hoisting the one concrete expected-vs-actual to sit directly
    above the imperative puts executed counterevidence at the point of maximum salience and gives
    the rung something specific to point at.

    Falls back to the bare rung when a run produced no marked failure — a collection error or an
    import failure has no failing statement to quote, and an empty header would assert one existed.
    """
    evidence = _first_failure_evidence(_strip_run_facts(raw_output))
    if not evidence:
        return nudge
    capped = _cap_bytes(evidence, REPEAT_EVIDENCE_BYTE_CAP)
    return f"{_REPEAT_EVIDENCE_HEADER}\n\n{capped}\n\n{nudge}"


def _first_failure_evidence(text: str) -> str:
    """The first failure's statement and result — pytest's ``>`` line and the ``E`` run below it.

    Scoped to the FIRST failure on purpose: a model that must fix everything at once fixes nothing,
    and the rung it leads asks for one path to change. The statement is the nearest ``>`` line
    above the result rather than the first in the output, so an earlier passing frame in the same
    traceback is never quoted as the cause.
    """
    lines = text.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.startswith(_FAILURE_DETAIL_MARKER)), None
    )
    if start is None:
        return ""
    detail = takewhile(lambda line: line.startswith(_FAILURE_DETAIL_MARKER), lines[start:])
    preceding = reversed(lines[:start])
    statement = next(
        (line for line in preceding if line.startswith(_FAILING_STATEMENT_MARKER)), ""
    )
    return "\n".join(filter(None, (statement, *detail)))


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
