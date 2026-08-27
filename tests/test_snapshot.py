"""Tests for the best-passing snapshot store (``claude_local.snapshot``).

SnapshotStore is locked improvement (a): REPAIR can regress a passing attempt, so the store ranks
every attempt by D-SNAPSHOT-001's key — ``(passed desc, errors asc, failed asc, index asc)`` — and
restores the best on loop exit. Every rank expectation below is hand-derived from that key, never
from running ``best()``. ``record``/``restore`` run against a REAL temp subtree (the filesystem is
local-substitutable — no mocks), asserting persisted bytes and stale-file removal.
"""

from __future__ import annotations

from pathlib import Path

from claude_local.runner import TestScore
from claude_local.snapshot import SnapshotStore


def _subtree(root: Path, rel: str = "src/pkg") -> str:
    """Create the permitted subtree dir under ``root`` and return its relative path."""
    (root / rel).mkdir(parents=True)
    return rel


def _write(root: Path, rel_subtree: str, name: str, content: str) -> None:
    p = root / rel_subtree / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# Oracle score builders — each value hand-derived from D-ORACLE-001 count semantics.
def _green(expected: int = 3) -> TestScore:
    return TestScore(expected, 0, 0, expected, 0, expected)  # all pass, valid + green


def _errored(expected: int = 3) -> TestScore:
    return TestScore(0, 0, 1, 1, 0, expected)  # collection error: passed 0, errors 1, collected 1


def _partial(passed: int, failed: int, expected: int = 3) -> TestScore:
    return TestScore(passed, failed, 0, passed + failed, 0, expected)  # ran, some failed


# --- best(): the rank key (D-SNAPSHOT-001) --------------------------------------


def test_no_records_has_no_best(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path, _subtree(tmp_path))
    assert store.best() is None


def test_green_is_best_even_when_a_later_attempt_errors(tmp_path: Path) -> None:
    # Locked improvement (a): a green attempt must NEVER be clobbered by a worse later one.
    # green key (-3,0,0,0) < errored key (0,0,0,1) via the passed term — kills min->max and sign.
    rel = _subtree(tmp_path)
    store = SnapshotStore(tmp_path, rel)
    _write(tmp_path, rel, "impl.py", "GREEN\n")
    store.record(0, _green())
    _write(tmp_path, rel, "impl.py", "BROKEN\n")
    store.record(1, _errored())
    best = store.best()
    assert best is not None
    assert best.index == 0  # the green attempt, not the later errored one


def test_runnable_partial_ranks_above_a_collection_error(tmp_path: Path) -> None:
    # passed dominates: a partial (passed 2) beats a collection error (passed 0) even if later.
    rel = _subtree(tmp_path)
    store = SnapshotStore(tmp_path, rel)
    _write(tmp_path, rel, "impl.py", "ERR\n")
    store.record(0, _errored())
    _write(tmp_path, rel, "impl.py", "PARTIAL\n")
    store.record(1, _partial(passed=2, failed=1))
    best = store.best()
    assert best is not None
    assert best.index == 1


def test_error_ranks_below_failure_at_equal_passed(tmp_path: Path) -> None:
    # At equal passed, errors before failed: a crash is worse than a clean assertion failure.
    # A: passed2 errors0 failed1 (idx1); B: passed2 errors1 failed0 (idx0).
    # key A=(-2,0,1,1) < B=(-2,1,0,0): A wins despite the higher index (kills errors + index).
    rel = _subtree(tmp_path)
    store = SnapshotStore(tmp_path, rel)
    _write(tmp_path, rel, "impl.py", "B\n")
    store.record(0, TestScore(2, 0, 1, 3, 0, 3))  # passed 2, errors 1
    _write(tmp_path, rel, "impl.py", "A\n")
    store.record(1, _partial(passed=2, failed=1))  # passed 2, failed 1
    best = store.best()
    assert best is not None
    assert best.index == 1  # A: fewer errors wins over the lower index


def test_fewer_failures_wins_over_lower_index(tmp_path: Path) -> None:
    # key term `failed` must dominate index: B (failed1, idx1) beats A (failed2, idx0).
    # Without the failed term, the tie-break falls to index and A wrongly wins — the mutant.
    rel = _subtree(tmp_path)
    store = SnapshotStore(tmp_path, rel)
    _write(tmp_path, rel, "impl.py", "A\n")
    store.record(0, _partial(passed=2, failed=2))
    _write(tmp_path, rel, "impl.py", "B\n")
    store.record(1, _partial(passed=2, failed=1))
    best = store.best()
    assert best is not None
    assert best.index == 1  # fewer failures wins over lower index


def test_earliest_index_wins_even_when_recorded_out_of_order(tmp_path: Path) -> None:
    # The rank uses the PASSED index, not insertion order: record idx1 then idx0, equal scores.
    # With the index term the lower index (0) wins; drop it and min's stability keeps the
    # first-inserted idx1 — the out-of-order case is what makes the index term killable.
    rel = _subtree(tmp_path)
    store = SnapshotStore(tmp_path, rel)
    _write(tmp_path, rel, "impl.py", "LATER\n")
    store.record(1, _partial(passed=2, failed=1))
    _write(tmp_path, rel, "impl.py", "EARLIER\n")
    store.record(0, _partial(passed=2, failed=1))
    best = store.best()
    assert best is not None
    assert best.index == 0  # earliest index wins, regardless of insertion order


# --- record() + restore_best(): exact subtree sync -------------------------------


def test_restore_best_syncs_the_subtree_to_the_best_capture(tmp_path: Path) -> None:
    # Core: green captured at idx0; a worse errored attempt at idx1 rewrites impl and adds junk.
    # restore_best -> impl.py back to GREEN bytes; junk.py (stale, from the worse attempt) gone.
    rel = _subtree(tmp_path)
    store = SnapshotStore(tmp_path, rel)
    _write(tmp_path, rel, "impl.py", "GREEN\n")
    store.record(0, _green())
    _write(tmp_path, rel, "impl.py", "BROKEN\n")
    _write(tmp_path, rel, "junk.py", "STALE\n")
    store.record(1, _errored())
    store.restore_best()
    assert (tmp_path / rel / "impl.py").read_text(encoding="utf-8") == "GREEN\n"
    assert not (tmp_path / rel / "junk.py").exists()  # stale file removed


def test_record_captures_bytes_eagerly_not_a_live_reference(tmp_path: Path) -> None:
    # The capture is a value snapshot: a disk change AFTER record must not alter the entry.
    rel = _subtree(tmp_path)
    store = SnapshotStore(tmp_path, rel)
    _write(tmp_path, rel, "impl.py", "AT_RECORD\n")
    store.record(0, _green())
    _write(tmp_path, rel, "impl.py", "MUTATED_LATER\n")  # no new record
    store.restore_best()
    assert (tmp_path / rel / "impl.py").read_text(encoding="utf-8") == "AT_RECORD\n"


def test_restore_best_removes_a_stale_file_and_prunes_its_empty_dir(tmp_path: Path) -> None:
    # Exactness: best had no nested/ dir; a worse attempt added nested/extra.py. restore drops the
    # file AND prunes the emptied dir, so the subtree matches the best capture exactly.
    rel = _subtree(tmp_path)
    store = SnapshotStore(tmp_path, rel)
    _write(tmp_path, rel, "impl.py", "GREEN\n")
    store.record(0, _green())
    _write(tmp_path, rel, "nested/extra.py", "STALE\n")
    store.record(1, _errored())
    store.restore_best()
    assert not (tmp_path / rel / "nested" / "extra.py").exists()
    assert not (tmp_path / rel / "nested").exists()  # emptied dir pruned


def test_restore_best_is_a_noop_when_there_are_no_records(tmp_path: Path) -> None:
    rel = _subtree(tmp_path)
    _write(tmp_path, rel, "impl.py", "UNTOUCHED\n")
    store = SnapshotStore(tmp_path, rel)
    store.restore_best()  # nothing recorded -> must not raise, must not touch disk
    assert (tmp_path / rel / "impl.py").read_text(encoding="utf-8") == "UNTOUCHED\n"


def test_best_files_hold_the_captured_subtree_content(tmp_path: Path) -> None:
    # best().files is the captured {rel-posix: bytes} map the loop reads — assert it directly.
    rel = _subtree(tmp_path)
    store = SnapshotStore(tmp_path, rel)
    _write(tmp_path, rel, "impl.py", "VALUE\n")
    store.record(0, _green())
    best = store.best()
    assert best is not None
    assert best.files == {"impl.py": b"VALUE\n"}
