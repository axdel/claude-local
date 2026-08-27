"""Structured oracle verdict from a pytest run — JUnit-XML parsed, never regex-scraped.

The loop's DONE signal must be trustworthy: a weak model must not "win" by importing away or
skipping tests. ``score_junit`` parses a pytest JUnit-XML report into a ``TestScore`` of five
counts (summed across ``<testsuite>`` elements), and ``TestRunner.run`` produces that report by
running the frozen oracle test under ``uv``. The verdict is pinned against an expected count
(D-ORACLE-001): a score is *valid* only when exactly that many tests ran to a pass/fail/error
verdict, and *green* only when all of them passed. ``collected`` counts verdicts —
``tests - skipped`` — so a skipped or imported-away test drops it below the pin and reads as
invalid, never green.

Because that pytest run *imports and executes the untrusted impl*, it runs under kernel
confinement (``sandbox``): the subprocess may write only the disposable report dir and reach no
network, and a non-terminating impl is killed at a wall-clock budget rather than hanging the loop.
"""

from __future__ import annotations

import tempfile
import xml.etree.ElementTree as ET  # nosec B405 (D-ORACLE-002)
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from claude_local.sandbox import SandboxTimeout, sandboxed_spawn

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


class OracleError(RuntimeError):
    """The oracle could not produce a verdict — the pytest run wrote no JUnit report, or a
    malformed one.

    Raised instead of returning a not-green score so a broken oracle fails loud rather than
    masquerading as a failing implementation and burning the loop's budget on futile retries.
    ``TestRunner.run`` unifies both failures (missing report, unparseable report) into this one
    type so the loop catches a single exception rather than a bare ``ParseError``.
    """


@dataclass(frozen=True, slots=True)
class TestScore:
    """The parsed outcome of one oracle run, judged against a pinned expected count.

    ``collected`` is the number of tests that ran to a verdict (``tests - skipped``); a skipped
    test is deliberately not collected, so any skip makes the score invalid (D-ORACLE-001).
    """

    __test__: ClassVar[bool] = False  # a value object, not a pytest test class

    passed: int
    failed: int
    errors: int
    collected: int
    skipped: int
    expected: int

    @property
    def is_valid(self) -> bool:
        """Exactly ``expected`` tests ran to a verdict — none skipped or errored away."""
        return self.collected == self.expected

    @property
    def is_green(self) -> bool:
        """The DONE signal: valid, with no failing and no errored test (D-ORACLE-001)."""
        return self.is_valid and self.failed == 0 and self.errors == 0


def score_junit(xml_text: str, expected: int) -> TestScore:
    """Parse a pytest JUnit-XML report into a ``TestScore``, summed across testsuites.

    Counts come from the ``tests``/``failures``/``errors``/``skipped`` attributes (never from
    scraping human output); ``passed`` and ``collected`` are derived. ``expected`` is the pinned
    count the verdict is judged against. The report is our own pytest subprocess's output into a
    temp file we own — pytest's junitxml emits no DTD or entity definitions — so parsing it with
    stdlib ``xml.etree`` carries no XXE/entity-expansion surface (S314/B314 documented-safe).

    Raises:
        xml.etree.ElementTree.ParseError: the report is not well-formed XML.
    """
    root = ET.fromstring(xml_text)  # noqa: S314 # nosec B314 (D-ORACLE-002)
    tests = failures = errors = skipped = 0
    for suite in root.iter("testsuite"):
        tests += int(suite.get("tests", "0"))
        failures += int(suite.get("failures", "0"))
        errors += int(suite.get("errors", "0"))
        skipped += int(suite.get("skipped", "0"))
    return TestScore(
        passed=tests - failures - errors - skipped,
        failed=failures,
        errors=errors,
        collected=tests - skipped,
        skipped=skipped,
        expected=expected,
    )


class TestRunner:
    """Runs the frozen oracle test under ``uv`` and scores its JUnit-XML report.

    The ``spawn`` seam is the pytest-subprocess boundary (a true-external tool): the default runs
    the process under kernel confinement (``sandbox.sandboxed_spawn`` — writable only within the
    disposable report dir, no network, no inherited secrets), and tests inject a fake that writes a
    captured report to the requested path. A spawn signals a non-terminating impl by raising
    ``SandboxTimeout``, which ``run`` maps to a zero-verdict score.
    """

    __test__ = False

    def __init__(
        self, spawn: Callable[[Sequence[str], Path, Path], None] = sandboxed_spawn
    ) -> None:
        self._spawn = spawn

    def run(self, test_path: Path, worktree: Path, expected: int) -> TestScore:
        """Run ``test_path`` under ``uv`` in ``worktree`` and score it against ``expected``.

        The command runs under kernel confinement; a non-terminating impl raises ``SandboxTimeout``
        from the spawn, which maps to a zero-verdict (non-green, invalid) score — the loop treats a
        hang as a failed attempt and retries, never a harness fault.

        Raises:
            OracleError: the run produced no JUnit report, or an unparseable one — a broken
                oracle, not a failing impl.
        """
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "oracle.xml"
            cmd = [
                "uv",
                "run",
                "pytest",
                str(test_path),
                f"--junit-xml={report}",
                "-o",
                "junit_family=xunit2",
                "-p",
                "no:cacheprovider",
            ]
            try:
                self._spawn(cmd, worktree, Path(tmp))
            except SandboxTimeout:
                return TestScore(
                    passed=0, failed=0, errors=0, collected=0, skipped=0, expected=expected
                )
            if not report.is_file():
                raise OracleError(f"pytest produced no JUnit report at {report}")
            try:
                return score_junit(report.read_text(encoding="utf-8"), expected)
            except ET.ParseError as exc:
                raise OracleError(f"pytest wrote a malformed JUnit report at {report}") from exc
