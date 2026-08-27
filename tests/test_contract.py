"""Tests for the builder-adapter contract stub (``claude_local.contract``).

The contract is the cross-repo seam claude-protocol owns; claude-local ships it as a stub
(D-CONTRACT-001). ``build`` must REFUSE — the loop implements it, not this module — and
``to_build_status`` must map every loop ``Status`` to the external ``BuildStatus``: a TOTAL
mapping (the review's exhaustiveness requirement), with ``DONE_WITH_CONCERNS`` never produced
locally. Every expected mapping below is hand-derived from D-CONTRACT-001, never read from the
implementation under test.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from factories import build_task_spec

from claude_local.contract import BuildStatus, build, to_build_status
from claude_local.types import Status

if TYPE_CHECKING:
    from pathlib import Path

# The mapping is fixed by D-CONTRACT-001; each expected value is read from the decision, never
# from to_build_status. DONE_WITH_CONCERNS is deliberately absent — no local Status reaches it.
_EXPECTED_MAPPING = {
    Status.DONE: BuildStatus.DONE,
    Status.EXHAUSTED: BuildStatus.NEEDS_CONTEXT,
    Status.DERAILED: BuildStatus.BLOCKED,
    Status.BLOCKED: BuildStatus.BLOCKED,
}


# --- to_build_status: the total Status -> BuildStatus mapping ----------------------


@pytest.mark.parametrize(("status", "expected"), list(_EXPECTED_MAPPING.items()))
def test_to_build_status_maps_each_loop_status(status: Status, expected: BuildStatus) -> None:
    # One arm per case, each pinned to its D-CONTRACT-001 target so a single mis-mapped arm fails.
    assert to_build_status(status) == expected


def test_to_build_status_is_total_over_every_status() -> None:
    # Exhaustiveness (review major 10): every Status member maps without raising. A missing arm
    # is a basedpyright error at type-check AND an assert_never AssertionError at runtime here.
    for status in Status:
        assert isinstance(to_build_status(status), BuildStatus)


def test_done_with_concerns_is_never_produced_by_the_local_half() -> None:
    # D-CONTRACT-001: the loop's oracle is binary, so nothing local distinguishes "passed with
    # concerns" from "passed" — DONE_WITH_CONCERNS is a downstream-human verdict, never a target.
    produced = {to_build_status(status) for status in Status}
    assert BuildStatus.DONE_WITH_CONCERNS not in produced


# --- build: the stub refuses -------------------------------------------------------


def test_build_raises_not_implemented_owned_by_claude_protocol(tmp_path: Path) -> None:
    # build() is claude-protocol's entry point; the loop implements it, not this module. The
    # stub must refuse, naming its owner (D-CONTRACT-001) — the match pins that contractual note.
    with pytest.raises(NotImplementedError, match="owned by claude-protocol"):
        build(build_task_spec(), tmp_path, "full")
