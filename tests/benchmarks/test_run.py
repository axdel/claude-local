"""End-to-end tests for the benchmark run script (``benchmarks.run``).

These drive the script's ``main`` exactly as the command line does — parse argv, load the real
committed cases, run every case, score it, and return a process exit code — with only the external
model transport replayed. Feeding every hole its golden file proves the documented flow end to end
(exit 0, ``--out`` writes the scorecard); a single corrupted reply proves a failing case flips the
exit code to 1; and the no-model guard is asserted from the process's own exit code.
"""

import itertools
import json
import time
from pathlib import Path

import httpx
import pytest
from casehelpers import golden_impl
from factories import build_attempt_progress, build_generation_result, build_test_score

from benchmarks.harness import CaseScore, Scorecard, load_cases, replay_cases_http_client
from benchmarks.run import ConsoleProgress, _print_scorecard, main
from claude_local import AttemptProgress, Status
from claude_local.derail import DerailReason

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


def test_main_runs_only_the_named_cases_in_ladder_order(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--only`` narrows the ladder to the named cases, keeping the committed ladder order.

    Diagnosing why one case failed needs that case's replies, and paying the whole seven-case
    ladder for one of them makes the question too expensive to ask. The two ids are passed in
    REVERSE ladder order to pin that the run follows the committed order rather than argv order —
    the ladder is a progression, so a case must never see a later case's position.
    """
    sources = _golden_sources()

    with replay_cases_http_client(sources) as http_client:
        exit_code = main(
            [
                "--base-url",
                "http://benchmark.local",
                "--model",
                "replay/golden",
                "--only",
                "05_rbac",
                "--only",
                "01_scaffold",
                "--out",
                str(tmp_path),
            ],
            http_client=http_client,
        )

    assert exit_code == 0
    (scorecard_path,) = tmp_path.glob("scorecard-*.json")
    card = json.loads(scorecard_path.read_text(encoding="utf-8"))
    assert [case["case_id"] for case in card["cases"]] == ["01_scaffold", "05_rbac"]
    assert card["cases_total"] == 2  # the other five never ran
    assert "02_schemas" not in capsys.readouterr().err


def test_a_run_stamps_its_scorecard_and_its_code_directory_identically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run's two artifacts pair by name, which takes one clock read for both — not one each.

    Oracle: ``write_produced_code`` documents that the code directory's name matches the
    scorecard's, so a verdict and the code behind it are found by name instead of by correlating
    mtimes. Nothing else in the run reads the wall clock — every duration uses ``time.monotonic``
    — so advancing ``time.time`` a full second per call isolates exactly the stamping.

    That advancing clock is what makes the assertion bite. Left real, both reads land in the same
    millisecond on a replay run and two independent reads pass by luck, so the test would be green
    against the very bug it exists to catch.

    The two artifacts are written to SEPARATE directories, which is how the runner is meant to be
    driven: produced code is model output that a whole-repo analyzer must never walk, so it does
    not live beside the committed scorecards. The stamp is what still pairs them.
    """
    ticks = itertools.count(1_700_000_000.0)
    monkeypatch.setattr(time, "time", lambda: next(ticks))
    code_out = tmp_path / "produced"
    sources = _golden_sources()

    with replay_cases_http_client(sources) as http_client:
        exit_code = main(
            [
                "--base-url",
                "http://benchmark.local",
                "--model",
                "replay/golden",
                "--only",
                "01_scaffold",
                "--out",
                str(tmp_path),
                "--code-out",
                str(code_out),
            ],
            http_client=http_client,
        )

    assert exit_code == 0
    (scorecard_path,) = tmp_path.glob("scorecard-*.json")
    (code_directory,) = (path for path in code_out.glob("code-*") if path.is_dir())
    assert scorecard_path.stem.rsplit("-", 1)[1] == code_directory.name.rsplit("-", 1)[1]


def test_main_exits_2_when_only_names_an_unknown_case(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unknown ``--only`` id is a usage error naming the valid ids, never an empty green run.

    Scoring zero cases would satisfy ``cases_passed == cases_total`` and exit 0, so a typo would
    report success for a benchmark that never ran — the silent-skip failure this guard exists to
    make impossible. No transport is needed: the guard fires before any case is driven.
    """
    exit_code = main(
        ["--base-url", "http://benchmark.local", "--model", "replay/golden", "--only", "05_rback"]
    )

    assert exit_code == 2
    err = capsys.readouterr().err
    assert "05_rback" in err  # the id that was not found
    assert "05_rbac" in err  # and the real ids, so the typo is correctable from the message


def test_generation_params_reach_every_request_body() -> None:
    """``--generation-params`` rides in each chat-completion body, on every case and every retry.

    Oracle: the flag's declared contract is a JSON object of request-body fields, so the value the
    server receives is the one passed in — ``False``, the JSON boolean, not the string "false". The
    assertion is on the wire because that is the only place the guarantee holds: a body field is
    the ONLY lever that reaches a chat template defaulting a behaviour on, so a parameter that
    reaches the driver but not the request buys nothing. Every request is checked rather than the
    first, because a per-call body rebuilt without the field would still pass a first-request check
    while leaving every retry unconfigured.
    """
    bodies: list[dict[str, object]] = []

    with replay_cases_http_client(
        _golden_sources(),
        request_observer=lambda request: bodies.append(json.loads(request.content)),
    ) as http_client:
        exit_code = main(
            [
                "--base-url",
                "http://benchmark.local",
                "--model",
                "replay/golden",
                "--generation-params",
                '{"enable_thinking": false, "top_k": 20}',
            ],
            http_client=http_client,
        )

    assert exit_code == 0
    assert bodies, "no request was observed, so the assertion below would be vacuous"
    assert all(body["enable_thinking"] is False for body in bodies)
    assert all(body["top_k"] == 20 for body in bodies)


def test_a_run_declaring_no_generation_params_sends_none() -> None:
    """Without the flag, no extra field is invented — the default request body is left alone.

    Oracle: the flag is optional, so its absence must mean "declare nothing", not "declare a
    default". A field silently present with a guessed value would configure every model in the
    catalog from one row's needs.
    """
    bodies: list[dict[str, object]] = []

    with replay_cases_http_client(
        _golden_sources(),
        request_observer=lambda request: bodies.append(json.loads(request.content)),
    ) as http_client:
        exit_code = main(
            ["--base-url", "http://benchmark.local", "--model", "replay/golden"],
            http_client=http_client,
        )

    assert exit_code == 0
    assert bodies
    assert all("enable_thinking" not in body for body in bodies)


@pytest.mark.parametrize(
    "declared",
    [
        pytest.param("{not json}", id="not-json"),
        pytest.param('["enable_thinking"]', id="json-but-not-an-object"),
    ],
)
def test_generation_params_that_are_not_a_json_object_are_a_usage_error(
    declared: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """The flag takes a JSON object of body fields; anything else is refused before any case runs.

    Oracle: a request body is a JSON object, so a list or a scalar names no fields and cannot be
    merged into one. Failing at parse time is what makes the typo correctable — a server ignores an
    unrecognised body field silently, so a malformed declaration accepted here would read as a
    working configuration through an entire benchmark run.
    """
    with pytest.raises(SystemExit) as refusal:
        main(
            [
                "--base-url",
                "http://benchmark.local",
                "--model",
                "replay/golden",
                "--generation-params",
                declared,
            ]
        )

    assert refusal.value.code == 2  # argparse's usage-error code, as for any bad argument
    err = capsys.readouterr().err
    assert "--generation-params" in err
    # The requirement itself, not just the flag name: an unrecognised-argument error also names
    # the flag, so asserting only that would pass against a build where the flag does not exist.
    assert "JSON object" in err


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


def test_a_harness_fault_mid_sweep_still_writes_the_cases_that_finished(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Oracle: a sweep is hours of GPU time, and every completed case is a measurement already paid
    for — losing them to the next case's fault costs the whole run to report one failure.

    The server answers the first case and then stops answering, which is the ordinary way a long
    sweep ends: the host is killed, or swaps, or the model is unloaded. The fault must still be
    loud (exit 3, a named diagnostic) AND the finished cases must still reach a scorecard, because
    the two are not in tension — one says the run did not complete, the other says what it
    measured before it stopped. D-BACKEND-004 narrowed how often this fires; it is what happens
    when it fires anyway.
    """
    sources = _golden_sources()

    def die_after_the_first_case(request: httpx.Request) -> None:
        if b"app/main.py" not in request.content:
            raise httpx.ConnectError("connection refused")

    with replay_cases_http_client(
        sources, request_observer=die_after_the_first_case
    ) as http_client:
        exit_code = main(
            [
                "--base-url",
                "http://benchmark.local",
                "--model",
                "replay/dies-midway",
                "--out",
                str(tmp_path),
            ],
            http_client=http_client,
        )

    assert exit_code == 3
    err = capsys.readouterr().err
    assert "harness fault" in err

    (scorecard_path,) = tmp_path.glob("scorecard-*.json")
    card = json.loads(scorecard_path.read_text(encoding="utf-8"))
    # Exactly the one case that finished before the server went away — not zero, and not the
    # whole ladder either, since the remaining cases were never measured.
    assert [case["case_id"] for case in card["cases"]] == ["01_scaffold"]
    assert card["cases_passed"] == card["cases_total"] == 1


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
        rules_card_digest="0123456789ab",
        plan_first=False,
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


# --- The live scoreboard: what a watcher sees while the ladder runs -----------------


def test_console_progress_opens_a_case_with_its_target_and_its_budget(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Oracle: the committed case manifest — 01_scaffold targets app/main.py under its own budget.

    The attempt budget belongs on the case line rather than repeated on every attempt line, which
    is what lets the renderer hold no memory of the case it is inside: a reader gets the
    denominator once, immediately above the attempts it applies to.
    """
    case = load_cases(_CASES, golden_app_root=_GOLDEN_APP)["01_scaffold"]

    ConsoleProgress().case_started("01_scaffold", case, 1, 7)

    err = capsys.readouterr().err
    assert "[1/7]" in err
    assert "01_scaffold" in err
    assert case.task.impl_path in err
    assert str(case.task.budget.max_attempts) in err


@pytest.mark.parametrize(
    ("progress", "expected"),
    [
        (
            build_attempt_progress(
                score=build_test_score(passed=3, failed=6, collected=9, expected=9)
            ),
            "3/9 oracle tests passed",
        ),
        (
            build_attempt_progress(
                score=None,
                generation=build_generation_result(derail_reason=DerailReason.REPETITION),
            ),
            "derailed (repetition)",
        ),
        (
            build_attempt_progress(
                score=None, generation=build_generation_result(fault="upstream 503")
            ),
            "server fault: upstream 503",
        ),
        # A block carries its own reason, and the two blocks are different problems: a reply that
        # framed nothing is a formatting failure, one framed at the wrong path a targeting failure.
        (
            build_attempt_progress(
                score=None,
                blocked_reason="the reply carried no whole-file frame to write",
            ),
            "no whole-file frame",
        ),
        (
            build_attempt_progress(
                score=None,
                blocked_reason=(
                    "refused 'src/other.py': only the permitted impl path may be written"
                ),
            ),
            "src/other.py",
        ),
        # No reason recorded at all: the renderer must still name the shape, not print an empty
        # phrase — a watcher reading a blank verdict learns less than one reading a vague one.
        (build_attempt_progress(score=None), "no usable file frame"),
    ],
)
def test_console_progress_names_what_each_attempt_produced(
    progress: AttemptProgress, expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every attempt ends one of four ways, and a watcher must be able to tell which.

    Oracle: the four are the loop's own terminal shapes — an oracle verdict, a guard-cut derail,
    an upstream fault, and the structural block that is left when neither of those stopped it.
    Collapsing any pair would show the same phrase for two different failures, which is precisely
    the ambiguity a live view exists to remove. The block splits further into two causes with
    nothing in common to fix, so the renderer quotes the reason the loop recorded rather than
    restating a fixed phrase. ``DerailReason`` is named here only to build the fixture; the
    renderer reads its stable value rather than matching on the enum.
    """
    ConsoleProgress().attempt(progress)

    err = capsys.readouterr().err
    assert expected in err
    assert "attempt 1" in err


def test_console_progress_marks_a_repeated_attempt_and_the_escalation_it_earned(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A verbatim repeat and the loop's answer to it are facts the score alone cannot show.

    Oracle: the two flags answer different questions — ``repeat`` is what the attempt WROTE
    (byte-identical to its predecessor), ``nudged`` is what it was ASKED (its prompt carried an
    escalation). A watcher seeing neither reads a run that walks its whole budget at an unchanged
    score as a hang. Both ride on the attempt line rather than the case line because each is a fact
    about that attempt, and both are additive: the oracle verdict is still reported alongside them.
    """
    ConsoleProgress().attempt(
        build_attempt_progress(
            score=build_test_score(passed=2, failed=6, collected=8, expected=8),
            repeats_previous=True,
            nudged=True,
        )
    )

    err = capsys.readouterr().err
    assert "2/8 oracle tests passed" in err  # the verdict survives the marks
    assert "repeat" in err
    assert "nudged" in err

    ConsoleProgress().attempt(build_attempt_progress())
    unmarked = capsys.readouterr().err
    assert "repeat" not in unmarked
    assert "nudged" not in unmarked


def test_console_progress_shows_the_decode_rate_it_was_given(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Oracle: 100 completion tokens in 2.0 seconds is 50.0 tok/s — the factory's own defaults.

    Speed is the number a local-model watcher is actually judging, so it is rendered from the
    generation's owned rate rather than recomputed here.
    """
    ConsoleProgress().attempt(build_attempt_progress())

    assert "50.0 tok/s" in capsys.readouterr().err


def test_console_progress_streams_raw_model_text_only_when_asked(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The firehose is opt-in: seven cases of multi-thousand-token decodes drown the scoreboard.

    Oracle: a delta is the only thing written by this call, so its presence or absence in the
    captured stream is decisive either way.
    """
    ConsoleProgress().delta("def add(a, b):")
    assert "def add" not in capsys.readouterr().err

    ConsoleProgress(stream_text=True).delta("def add(a, b):")
    assert "def add" in capsys.readouterr().err


def test_console_progress_closes_a_half_written_stream_before_its_next_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Streamed model text ends mid-line, so a structured line must first break out of it.

    Oracle: model output stops on whatever token it stopped on, with no trailing newline. Without
    the break the attempt line is glued onto that last token — the scoreboard becomes unreadable
    exactly when the model is most interesting to watch.
    """
    renderer = ConsoleProgress(stream_text=True)
    renderer.delta("return a + b")
    renderer.attempt(build_attempt_progress())

    assert capsys.readouterr().err.startswith("return a + b\n")


def test_main_reports_the_ladder_live_and_streams_text_on_request(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The script wires the live renderer in, so a real run is watchable, not merely awaited.

    Oracle: the scorecard printed at the end already contains every case id, so case ids prove
    nothing here. The live markers do: a bracketed ladder position and a per-attempt line exist
    only while cases are running, and ``--stream`` additionally puts the golden reply's own text
    on the stream — text that is never part of the final scorecard.
    """
    sources = _golden_sources()

    with replay_cases_http_client(sources) as http_client:
        exit_code = main(
            ["--base-url", "http://benchmark.local", "--model", "replay/golden", "--stream"],
            http_client=http_client,
        )

    assert exit_code == 0
    err = capsys.readouterr().err
    assert "[1/" in err
    assert "attempt 1" in err
    assert 'HealthResponse(status="ok")' in err
