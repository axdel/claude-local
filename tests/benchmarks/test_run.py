"""End-to-end tests for the benchmark run script (``benchmarks.run``).

These drive the script's ``main`` exactly as the command line does — parse argv, load the real
committed cases, run every case, score it, and return a process exit code — with only the external
model transport replayed. Feeding every hole its golden file proves the documented flow end to end
(exit 0, ``--out`` writes the scorecard); a single corrupted reply proves a failing case flips the
exit code to 1; and the no-model guard is asserted from the process's own exit code.
"""

import json
from pathlib import Path

import httpx
import pytest
from casehelpers import golden_impl

from benchmarks.harness import CaseScore, Scorecard, load_cases, replay_cases_http_client
from benchmarks.run import _print_scorecard, main
from claude_local import Status

_ROOT = Path(__file__).parents[2]
_BENCHMARK = _ROOT / "benchmarks" / "schedule_manager"
_GOLDEN_APP = _BENCHMARK / "golden" / "app"
_CASES = _BENCHMARK / "cases"


def _golden_sources() -> dict[str, str]:
    """Map every case's ``impl_path`` to its golden text, keyed for the replay client."""
    cases = load_cases(_CASES, golden_app_root=_GOLDEN_APP)
    return {case.task.impl_path: golden_impl(case) for case in cases.values()}


def test_main_runs_the_full_benchmark_green_and_writes_the_scorecard(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fed every hole's golden file, the script drives all cases to DONE, exits 0, writes a card.

    Golden replies reproduce the complete golden app, so every immutable oracle passes — the
    end-to-end proof that ``main`` loads the real cases, runs it, scores it, writes the JSON, and
    prints the human-readable verdict to stderr.
    """
    sources = _golden_sources()

    with replay_cases_http_client(sources) as http_client:
        exit_code = main(
            [
                "--base-url",
                "http://benchmark.local",
                "--model",
                "replay/golden",
                "--out",
                str(tmp_path),
            ],
            http_client=http_client,
        )

    assert exit_code == 0
    (scorecard_path,) = tmp_path.glob("scorecard-*.json")
    card = json.loads(scorecard_path.read_text(encoding="utf-8"))
    assert card["model"] == "replay/golden"
    # Oracle: golden replies pass every case, and the benchmark ran every committed case.
    assert card["cases_passed"] == card["cases_total"] == len(sources)
    assert [case["status"] for case in card["cases"]] == ["done"] * len(sources)

    # The human-readable verdict reaches stderr: model, the N/N pass line, and each case id.
    err = capsys.readouterr().err
    assert "replay/golden" in err
    assert f"{len(sources)}/{len(sources)} cases passed" in err
    assert "01_scaffold" in err


def test_main_exits_1_when_a_case_fails() -> None:
    """A single corrupted reply exhausts its case, so not all cases pass and the script exits 1."""
    sources = _golden_sources()
    scaffold_impl = "app/main.py"
    # The scaffold oracle asserts health is "ok"; flipping it to "down" fails that case.
    corrupted = sources[scaffold_impl].replace(
        'HealthResponse(status="ok")', 'HealthResponse(status="down")'
    )
    assert corrupted != sources[scaffold_impl]  # the corruption actually applied
    sources[scaffold_impl] = corrupted

    with replay_cases_http_client(sources) as http_client:
        exit_code = main(
            ["--base-url", "http://benchmark.local", "--model", "replay/mixed"],
            http_client=http_client,
        )

    assert exit_code == 1


def test_main_exits_2_when_no_model_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no --model and no env fallback, the script reports a usage error and exits 2.

    The guard returns before any benchmark is loaded or run, so no transport is needed.
    """
    monkeypatch.delenv("CLAUDE_LOCAL_MODEL", raising=False)
    monkeypatch.delenv("CLAUDE_LOCAL_BASE_URL", raising=False)

    assert main([]) == 2


def test_main_exits_3_when_the_server_is_unreachable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unreachable prerequisite server is a harness fault: the script exits 3, not 1.

    The backend translates the transport failure to ``BackendUnavailable`` (a raised harness fault,
    not a case status), which ``main`` must catch and report as a broken host — exit 3, distinct
    from a model that merely failed a case (exit 1). The sandbox works in this environment (the
    golden-reply cases reach DONE), so a connection error surfaces as the backend fault, not the
    sandbox one.
    """

    def unreachable(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with httpx.Client(transport=httpx.MockTransport(unreachable)) as http_client:
        exit_code = main(
            ["--base-url", "http://benchmark.local", "--model", "replay/unreachable"],
            http_client=http_client,
        )

    assert exit_code == 3
    err = capsys.readouterr().err
    # A clean one-line diagnostic, never a leaked traceback.
    assert "harness fault" in err
    assert "Traceback" not in err


def test_print_scorecard_surfaces_a_faulted_case_and_a_capped_case(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The per-case stderr line shows a case's length-cap count and its ``FAULTED`` fault message.

    Driving a real capped or faulted completion end-to-end is disproportionate here; the
    surfacing is a pure rendering of two ``CaseScore`` fields, so a hand-built scorecard is the
    honest oracle.
    """
    scorecard = Scorecard(
        model="local/candidate",
        cases=(
            CaseScore(case_id="01_scaffold", status=Status.DONE, attempts=2, length_capped=1),
            CaseScore(
                case_id="02_schemas", status=Status.FAULTED, attempts=1, fault="upstream 503"
            ),
        ),
        total_completion_tokens=110,
        total_model_seconds=3.0,
        mean_tokens_per_second=36.6,
    )

    _print_scorecard(scorecard)

    err = capsys.readouterr().err
    assert "1 length-capped" in err
    assert "fault: upstream 503" in err
