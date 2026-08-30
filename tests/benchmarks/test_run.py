"""End-to-end tests for the benchmark run script (``benchmarks.run``).

These drive the script's ``main`` exactly as the command line does — parse argv, load the real
committed suite, run every rung, score it, and return a process exit code — with only the external
model transport replayed. Feeding every hole its golden file proves the documented flow end to end
(exit 0, ``--out`` writes the scorecard); a single corrupted reply proves a failing rung flips the
exit code to 1; and the no-model guard is asserted from the process's own exit code.
"""

import json
from pathlib import Path

import pytest

from benchmarks.harness import load_suite, replay_suite_http_client
from benchmarks.run import main

_ROOT = Path(__file__).parents[2]
_BENCHMARK = _ROOT / "benchmarks" / "schedule_manager"
_GOLDEN_APP = _BENCHMARK / "golden" / "app"
_CASES = _BENCHMARK / "cases"


def _golden_sources() -> dict[str, str]:
    """Map every rung's ``impl_path`` to its golden text, keyed for the replay client.

    The per-case ``next(...)`` extraction mirrors ``test_driver._golden_source`` — the second
    occurrence of "pull a case's golden file text"; Rule of Three notes it, extract on a third.
    """
    cases = load_suite(_CASES, golden_app_root=_GOLDEN_APP)
    return {
        case.task.impl_path: next(
            file.content for file in case.golden_tree if file.path == case.task.impl_path
        )
        for case in cases.values()
    }


def test_main_runs_the_full_suite_green_and_writes_the_scorecard(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fed every hole's golden file, the script drives all rungs to DONE, exits 0, writes a card.

    Golden replies reproduce the complete reference app, so every immutable oracle passes — the
    end-to-end proof that ``main`` loads the real suite, runs it, scores it, writes the JSON, and
    prints the human-readable verdict to stderr.
    """
    sources = _golden_sources()

    with replay_suite_http_client(sources) as http_client:
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
    # Oracle: golden replies pass every rung, and the suite ran every committed case.
    assert card["rungs_passed"] == card["rungs_total"] == len(sources)
    assert [rung["status"] for rung in card["rungs"]] == ["done"] * len(sources)

    # The human-readable verdict reaches stderr: model, the N/N pass line, and each rung id.
    err = capsys.readouterr().err
    assert "replay/golden" in err
    assert f"{len(sources)}/{len(sources)} rungs passed" in err
    assert "01_scaffold" in err


def test_main_exits_1_when_a_rung_fails() -> None:
    """A single corrupted reply exhausts its rung, so not all rungs pass and the script exits 1."""
    sources = _golden_sources()
    scaffold_impl = "app/main.py"
    # The scaffold oracle asserts health is "ok"; flipping it to "down" fails that rung.
    corrupted = sources[scaffold_impl].replace(
        'HealthResponse(status="ok")', 'HealthResponse(status="down")'
    )
    assert corrupted != sources[scaffold_impl]  # the corruption actually applied
    sources[scaffold_impl] = corrupted

    with replay_suite_http_client(sources) as http_client:
        exit_code = main(
            ["--base-url", "http://benchmark.local", "--model", "replay/mixed"],
            http_client=http_client,
        )

    assert exit_code == 1


def test_main_exits_2_when_no_model_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no --model and no env fallback, the script reports a usage error and exits 2.

    The guard returns before any suite is loaded or run, so no transport is needed.
    """
    monkeypatch.delenv("CLAUDE_LOCAL_MODEL", raising=False)
    monkeypatch.delenv("CLAUDE_LOCAL_BASE_URL", raising=False)

    assert main([]) == 2
