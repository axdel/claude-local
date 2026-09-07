"""Tests for the first-byte probe (``scripts/measure-first-byte.py``).

The probe is what ``HTTP_READ_TIMEOUT_S`` cites as its justification, so the one property worth
pinning is that it still sends what it claims to send: a prompt the size of a real task's system
prefix. Nothing about a served model is exercised here — that needs weights and minutes — only the
pure prompt construction, which is the half that can go quietly wrong.
"""

from __future__ import annotations

from pathlib import Path

from scriptloader import load_script

from benchmarks.harness.loader import load_cases
from claude_local.prompt import PromptBuilder

_REPO_ROOT = Path(__file__).parents[1]
_CARD = _REPO_ROOT / "src" / "claude_local" / "rules_card.md"
_BENCHMARK = _REPO_ROOT / "benchmarks" / "schedule_manager"

probe = load_script(_REPO_ROOT / "scripts" / "measure-first-byte.py")


def _largest_real_case_prefix_bytes() -> int:
    """The biggest system prefix the benchmark actually builds, assembled the way the loop does.

    The oracle for the probe's prompt size, and deliberately computed rather than read from the
    probe: it goes through ``PromptBuilder.stable_prefix`` over the committed cases, so it is a
    second, independent answer to the question the probe's own constant answers. A test that took
    that constant as its expected value would agree with the probe at any size and pin nothing.
    """
    builder = PromptBuilder(_CARD)
    cases = load_cases(_BENCHMARK / "cases", golden_app_root=_BENCHMARK / "golden" / "app")
    return max(len(builder.stable_prefix(case.task).encode("utf-8")) for case in cases.values())


def test_the_probe_prompt_brackets_the_largest_real_case_prefix() -> None:
    """The probe sends at least the largest real prefix, and at most one card copy beyond it.

    Both bounds matter and they fail for opposite reasons. Under the floor, the probe measures a
    prefill smaller than any real task's and the timeout derived from it is unearned. Over the
    ceiling, it stops being the thing it claims to measure — which is what happened when a fixed
    six copies of a card that had grown to 34 KB sent 205 KB, six times the largest real prompt.
    """
    card_bytes = len((_CARD.read_text(encoding="utf-8") + "\n").encode("utf-8"))
    sent = len(probe.benchmark_sized_prefix().encode("utf-8"))
    largest = _largest_real_case_prefix_bytes()

    assert largest <= sent < largest + card_bytes, (
        f"probe sends {sent} bytes; the largest real case prefix is {largest} bytes"
    )
