"""The builder-adapter contract — the cross-repo seam claude-protocol owns, stubbed here.

claude-protocol drives implementers through one adapter call,
``build(task_spec, worktree, context_tier) -> BuildResult``. claude-local implements the
red-green loop *behind* that call but does not own the contract surface, so ``build`` ships as
a stub that refuses; the real entry point is wired on the claude-protocol side (D-CONTRACT-001).

What this module DOES own is the one translation the engine needs to slot in without a later
redesign: ``to_build_status`` maps the loop's own ``Status`` to the adapter's ``BuildStatus``.
That mapping is total by construction — ``assert_never`` turns a forgotten case into a
type-check error, not a latent runtime gap. ``DONE_WITH_CONCERNS`` is unreachable from the
local half: the loop's oracle test is binary (green or not), so nothing local distinguishes
"passed with concerns" from "passed" — only a downstream human reviewer produces that verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, auto
from typing import TYPE_CHECKING, assert_never

from claude_local.types import Status

if TYPE_CHECKING:
    from pathlib import Path

    from claude_local.telemetry import LocalEconomyRecord
    from claude_local.types import TaskSpec


class BuildStatus(StrEnum):
    """The builder-adapter's task outcome — claude-protocol's vocabulary, not the loop's.

    Values are the lowercased member names (``StrEnum`` + ``auto``): the external owner defines
    the wire form, so this module fixes the outcome *categories* without hardcoding arbitrary
    strings. ``DONE_WITH_CONCERNS`` exists in the contract but is never produced locally (see
    the module docstring).
    """

    DONE = auto()
    DONE_WITH_CONCERNS = auto()
    BLOCKED = auto()
    NEEDS_CONTEXT = auto()


@dataclass(frozen=True, slots=True)
class BuildResult:
    """One task's result across the builder-adapter seam.

    The outcome, the files the implementer wrote, free-text notes, and the local economy record.
    Frozen — a result is reported once, never mutated after the fact.
    """

    status: BuildStatus
    files_changed: tuple[str, ...]
    notes: str
    telemetry: LocalEconomyRecord


def to_build_status(status: Status) -> BuildStatus:
    """Map a loop ``Status`` to the external ``BuildStatus`` (D-CONTRACT-001).

    ``DONE -> DONE``, ``EXHAUSTED -> NEEDS_CONTEXT``, ``DERAILED`` and ``BLOCKED -> BLOCKED``.
    Total by construction: ``assert_never`` makes a forgotten ``Status`` a type-check error. No
    arm yields ``DONE_WITH_CONCERNS`` — it is unreachable from the local half (module docstring).
    """
    match status:
        case Status.DONE:
            return BuildStatus.DONE
        case Status.EXHAUSTED:
            return BuildStatus.NEEDS_CONTEXT
        case Status.DERAILED | Status.BLOCKED:
            return BuildStatus.BLOCKED
    assert_never(status)


def build(task_spec: TaskSpec, worktree: Path, context_tier: str) -> BuildResult:
    """STUB — the builder-adapter entry point claude-protocol owns; the loop implements it here."""
    del task_spec, worktree, context_tier  # unused: stub refuses before touching them
    raise NotImplementedError(
        "build() is owned by claude-protocol; claude-local supplies the red-green loop "
        "behind this adapter call, not the contract surface itself."
    )
