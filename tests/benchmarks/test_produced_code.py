"""Tests for the produced-code writer (``benchmarks.harness.produced_code``).

A scorecard says how many oracle tests passed; only the code says whether what passed them is
worth keeping. These tests pin that the code reaches disk in a shape a person can browse, that a
FAILED case is saved rather than dropped, and that a case which produced nothing writes nothing.
Every expected value is the literal source handed in, so no assertion can be satisfied by whatever
the writer happened to emit. The filesystem is local-substitutable, so a real temp dir is used —
no mocks.
"""

from pathlib import Path

from factories import build_local_economy_record

from benchmarks.harness import CaseResult, write_produced_code
from claude_local import Outcome, Status

_MODEL = "local/candidate-7b"


def _case_result(case_id: str, status: Status, code: str | None, impl_path: str) -> CaseResult:
    """A ``CaseResult`` carrying an exact source string — the writer's sole input."""
    return CaseResult(
        case_id=case_id,
        outcome=Outcome(
            status=status,
            code=code,
            impl_path=impl_path,
            files_changed=(impl_path,) if code is not None else (),
            record=build_local_economy_record(model=_MODEL, status=status),
            fault=None,
        ),
    )


def test_a_cases_code_lands_at_its_own_impl_path_under_its_case_id(tmp_path: Path) -> None:
    """The saved tree mirrors the paths the model was asked to write.

    Oracle: the expected location is ``<case_id>/<impl_path>`` because that is the shape the reader
    compares against — the golden app is laid out by impl_path, so an answer stored anywhere else
    has to be mentally re-mapped before it can be read beside the file it was meant to replace. The
    expected content is the exact string handed in, never a re-read of what the writer produced.
    """
    source = "def health() -> dict[str, str]:\n    return {'status': 'ok'}\n"
    results = [_case_result("01_scaffold", Status.DONE, source, "app/main.py")]

    written = write_produced_code(results, _MODEL, tmp_path)

    assert (written / "01_scaffold" / "app" / "main.py").read_text(encoding="utf-8") == source


def test_a_failed_cases_code_is_saved_too(tmp_path: Path) -> None:
    """A case that did not pass is the one most worth reading.

    Oracle: the artifact exists so a human can judge quality, and the interesting judgement is on a
    near miss — whether a case that exhausted its budget was one detail short or had misunderstood
    the task. Saving only green cases would discard exactly the evidence that answers it, and would
    make the directory a redundant second encoding of the scorecard's pass column.
    """
    attempted = "class ScheduleRepository:\n    pass\n"
    results = [_case_result("03_repositories", Status.EXHAUSTED, attempted, "app/repositories.py")]

    written = write_produced_code(results, _MODEL, tmp_path)

    saved = written / "03_repositories" / "app" / "repositories.py"
    assert saved.read_text(encoding="utf-8") == attempted


def test_a_case_that_produced_nothing_writes_no_file(tmp_path: Path) -> None:
    """``code is None`` means the loop scored no model edit, so there is no answer to save.

    Oracle: ``Outcome.code`` is ``None`` exactly when no model edit was scored, regardless of any
    file already on disk. Writing an empty file under the case's name would claim the model
    produced an empty implementation, which is a different and false finding — the same reason the
    repair brief renders no source section for an attempt that produced nothing.
    """
    results = [_case_result("07_routers", Status.BLOCKED, None, "app/routers.py")]

    written = write_produced_code(results, _MODEL, tmp_path)

    assert written.is_dir()  # the run directory still exists, so "nothing scored" is legible
    assert list(written.iterdir()) == []


def test_the_code_directory_is_named_to_pair_with_the_scorecard(tmp_path: Path) -> None:
    """The directory carries the same model slug the scorecard's filename does.

    Oracle: ``Scorecard.write`` names its file ``scorecard-<slug>-<ms>.json`` using the same
    ``slug_model_id``. Sharing the slug is what lets a reader pair a verdict with the code behind
    it by name; without it the only correlation left is comparing modification times, which two
    runs of the same model minutes apart destroy.
    """
    results = [_case_result("01_scaffold", Status.DONE, "x = 1\n", "app/main.py")]

    written = write_produced_code(results, _MODEL, tmp_path)

    assert written.name.startswith("code-local-candidate-7b-")
