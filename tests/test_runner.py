"""Tests for the pytest JUnit-XML oracle (``claude_local.runner``).

``score_junit`` is a pure text->structure function, so every expected ``TestScore`` is
hand-derived from the xunit2 attribute semantics + D-ORACLE-001 (``collected = tests - skipped``),
never from running the parser. The ``.xml`` fixtures are CAPTURED from real pytest runs (see
``fixtures/junit/README.md``) — the xunit2 schema is pytest's, so the fixtures are captured, not
authored from memory (Boundary Fixture Fidelity). ``TestRunner.run`` is exercised through an
injected ``spawn`` (the pytest-subprocess boundary — a true-external tool) that writes a captured
report to the path ``run`` requested, so the test asserts the scored verdict, never an argv shape.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from claude_local.runner import OracleError, TestRunner, TestScore, score_junit
from claude_local.sandbox import SandboxTimeout

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

JUNIT = Path(__file__).parent / "fixtures" / "junit"


def load_junit(name: str) -> str:
    return (JUNIT / name).read_text(encoding="utf-8")


# --- score_junit: captured report -> hand-derived TestScore ------------------------
# Each expected score is derived from the fixture's <testsuite> attributes:
#   passed = tests - failures - errors - skipped ;  collected = tests - skipped .


def test_all_pass_scores_three_passed_and_is_green() -> None:
    # tests=3 failures=0 errors=0 skipped=0 -> passed 3, collected 3; the ONLY green case.
    assert score_junit(load_junit("all_pass.xml"), 3) == TestScore(3, 0, 0, 3, 0, 3)


def test_one_failure_scores_a_failure_and_is_not_green() -> None:
    # tests=3 failures=1 -> passed 2, failed 1, collected 3 (valid but a real failure).
    assert score_junit(load_junit("one_failure.xml"), 3) == TestScore(2, 1, 0, 3, 0, 3)


def test_import_error_scores_an_error_and_is_invalid() -> None:
    # A collection/import failure: pytest emits tests=1 errors=1 (one error placeholder node),
    # NOT zero. collected 1 != expected 3 -> invalid; the model importing away tests cannot win.
    assert score_junit(load_junit("import_error.xml"), 3) == TestScore(0, 0, 1, 1, 0, 3)


def test_mismatched_count_scores_fewer_collected_and_is_invalid() -> None:
    # tests=2 all pass, but the pinned count is 3 -> collected 2 != 3 -> invalid, never green.
    assert score_junit(load_junit("mismatched_count.xml"), 3) == TestScore(2, 0, 0, 2, 0, 3)


def test_skipped_test_is_not_counted_as_collected() -> None:
    # tests=3 skipped=1 -> passed 2, skipped 1, collected 3-1=2. A skip drops collected below
    # the pin -> invalid (D-ORACLE-001: a skip reads as invalid, not green). This fixture is the
    # only one with skipped>0, so it is what distinguishes collected=tests-skipped from =tests.
    assert score_junit(load_junit("skipped.xml"), 3) == TestScore(2, 0, 0, 2, 1, 3)


# --- is_valid / is_green: the verdict truth table (D-ORACLE-001) --------------------
# Constructed directly so the predicate logic is isolated from the parser.


def test_is_valid_true_when_collected_equals_expected() -> None:
    assert TestScore(3, 0, 0, 3, 0, 3).is_valid is True


def test_is_valid_false_when_fewer_collected_than_expected() -> None:
    assert TestScore(2, 0, 0, 2, 0, 3).is_valid is False


def test_is_green_true_only_when_valid_with_no_failures_or_errors() -> None:
    assert TestScore(3, 0, 0, 3, 0, 3).is_green is True


def test_is_green_false_when_a_test_failed() -> None:
    # collected 3 == expected 3 (valid), but a failure -> not green (the failed term bites).
    assert TestScore(2, 1, 0, 3, 0, 3).is_green is False


def test_is_green_false_when_errored_even_if_count_matches() -> None:
    # collected 1 == expected 1 (valid) but an error occurred -> not green (the errors term bites).
    assert TestScore(0, 0, 1, 1, 0, 1).is_green is False


def test_is_green_false_when_count_mismatched_though_every_run_test_passed() -> None:
    # No failures, no errors, but fewer ran than pinned -> invalid -> never green (the valid term).
    assert TestScore(2, 0, 0, 2, 0, 3).is_green is False


# --- TestRunner.run: spawn -> report -> score ---------------------------------------


def _writing_spawn(report_xml: str) -> Callable[[Sequence[str], Path, Path], None]:
    """A fake pytest: write the given JUnit-XML to the --junit-xml=<path> the command requested."""

    def spawn(cmd: Sequence[str], cwd: Path, write_box: Path) -> None:
        for arg in cmd:
            if arg.startswith("--junit-xml="):
                report = Path(arg.split("=", 1)[1])
                # The report path MUST lie inside the box the runner declares writable — the
                # invariant the real sandbox relies on (the child may write only there).
                assert write_box in report.parents, "report path escapes the writable box"
                report.write_text(report_xml, encoding="utf-8")
                return
        raise AssertionError("run() did not request a --junit-xml=<path>")

    return spawn


def test_run_scores_the_report_its_spawn_produced(tmp_path: Path) -> None:
    runner = TestRunner(spawn=_writing_spawn(load_junit("all_pass.xml")))
    score = runner.run(tmp_path / "test_oracle.py", tmp_path, expected=3)
    assert score == TestScore(3, 0, 0, 3, 0, 3)
    assert score.is_green is True


def test_run_reports_a_collection_error_as_invalid(tmp_path: Path) -> None:
    runner = TestRunner(spawn=_writing_spawn(load_junit("import_error.xml")))
    score = runner.run(tmp_path / "test_oracle.py", tmp_path, expected=3)
    assert score == TestScore(0, 0, 1, 1, 0, 3)
    assert score.is_valid is False
    assert score.is_green is False


def test_run_raises_oracle_error_when_no_report_is_written(tmp_path: Path) -> None:
    def silent_spawn(cmd: Sequence[str], cwd: Path, write_box: Path) -> None:
        return  # pytest crashed before writing the report — a broken oracle, not a failing impl

    runner = TestRunner(spawn=silent_spawn)
    with pytest.raises(OracleError):
        runner.run(tmp_path / "test_oracle.py", tmp_path, expected=1)


def test_run_wraps_a_malformed_report_in_oracle_error(tmp_path: Path) -> None:
    # A present-but-malformed report (pytest killed mid-write, disk full) is a broken oracle, not a
    # failing impl. run() unifies it into OracleError so the loop catches a single failure type,
    # never a bare ParseError leaking from the parser.
    runner = TestRunner(spawn=_writing_spawn("this is not junit xml <<<"))
    with pytest.raises(OracleError):
        runner.run(tmp_path / "test_oracle.py", tmp_path, expected=1)


def test_run_maps_a_sandbox_timeout_to_a_zero_verdict_score(tmp_path: Path) -> None:
    # A non-terminating impl: the spawn kills it and raises SandboxTimeout. run() maps that to a
    # non-green, invalid score (zero verdicts reached) so the loop retries a hang, never crashes.
    def hanging_spawn(cmd: Sequence[str], cwd: Path, write_box: Path) -> None:
        raise SandboxTimeout("oracle exceeded its wall-clock budget")

    runner = TestRunner(spawn=hanging_spawn)
    score = runner.run(tmp_path / "test_oracle.py", tmp_path, expected=3)
    assert score == TestScore(0, 0, 0, 0, 0, 3)
    assert score.is_valid is False
    assert score.is_green is False
