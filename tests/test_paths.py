"""Oracle tests for realpath containment — the loop's security boundary.

Every expected verdict derives from the containment SPEC (path algebra): an
absolute path ignores the root; a `..` climb leaves it; a name-prefixed sibling
is not inside it; a symlinked final component is refused fail-closed. None of
these verdicts comes from running ``resolve_within`` — they come from where the
paths actually point. The escape cases are written to fail against a naive
``str.startswith`` containment check and pass only against true path algebra.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from claude_local.paths import KeepOnlyViolation, resolve_within


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A permitted subtree that genuinely exists on disk."""
    permitted = tmp_path / "allowed"
    permitted.mkdir()
    return permitted


# --- Contained (returns the resolved target) ------------------------------


def test_returns_resolved_path_for_existing_file_under_root(root: Path) -> None:
    impl = root / "impl.py"
    impl.touch()
    # Oracle: an existing regular file directly under root is contained.
    assert resolve_within(root, "impl.py") == impl.resolve()


def test_returns_resolved_path_for_new_file_with_existing_parent(root: Path) -> None:
    # Oracle: a not-yet-created file whose parent (root) exists resolves to root/name.
    assert resolve_within(root, "widget.py") == (root / "widget.py").resolve()


def test_returns_resolved_path_for_nested_new_file(root: Path) -> None:
    pkg = root / "pkg"
    pkg.mkdir()
    # Oracle: a new file under an existing nested parent is contained.
    assert resolve_within(root, "pkg/widget.py") == (pkg / "widget.py").resolve()


def test_root_itself_is_within(root: Path) -> None:
    # Oracle: the containment predicate admits the root path itself (the `==`
    # branch), not only its strict descendants.
    assert resolve_within(root, ".") == root.resolve()


# --- Refused: absolute ----------------------------------------------------


@pytest.mark.parametrize("candidate", ["/etc/passwd", "/opt/evil.py"])
def test_rejects_absolute_candidate(root: Path, candidate: str) -> None:
    # Oracle: an absolute path ignores the root entirely — never permitted.
    with pytest.raises(KeepOnlyViolation):
        resolve_within(root, candidate)


def test_rejects_absolute_candidate_even_inside_root(root: Path) -> None:
    inside = root / "impl.py"
    inside.touch()
    # Oracle: absoluteness is refused unconditionally — even pointing inside root
    # — so the rule never depends on where an absolute path happens to land.
    with pytest.raises(KeepOnlyViolation):
        resolve_within(root, str(inside))


# --- Refused: traversal ---------------------------------------------------


def test_rejects_parent_traversal_escape(root: Path) -> None:
    # Oracle: ../secret.py resolves to root's sibling — above the permitted subtree.
    with pytest.raises(KeepOnlyViolation):
        resolve_within(root, "../secret.py")


def test_rejects_deep_traversal_escape(root: Path) -> None:
    # Oracle: repeated .. climbs above root; refused (fail-closed on the missing
    # intermediate before it could even reach an outside target).
    with pytest.raises(KeepOnlyViolation):
        resolve_within(root, "sub/../../../../etc/passwd")


def test_rejects_prefix_sibling_escape(root: Path) -> None:
    sibling = root.parent / f"{root.name}-evil"
    sibling.mkdir()
    (sibling / "x.py").touch()
    # Oracle: a sibling dir sharing root's name-prefix is NOT inside root. A naive
    # str.startswith check would wrongly admit it; path algebra rejects it.
    with pytest.raises(KeepOnlyViolation):
        resolve_within(root, f"../{root.name}-evil/x.py")


# --- Refused: symlinks ----------------------------------------------------


def test_rejects_symlinked_final_pointing_outside(root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, root / "escape")
    # Oracle: a symlinked final component whose target escapes root is refused.
    with pytest.raises(KeepOnlyViolation):
        resolve_within(root, "escape")


def test_rejects_symlinked_final_pointing_inside(root: Path) -> None:
    real = root / "real.py"
    real.touch()
    os.symlink(real, root / "alias")
    # Oracle: a symlinked final component is refused fail-closed even when its
    # target lies inside root — the write must name a real path, not an alias.
    with pytest.raises(KeepOnlyViolation):
        resolve_within(root, "alias")


def test_rejects_intermediate_symlink_escaping_root(root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, root / "linkdir")
    # Oracle: an intermediate symlink that escapes root is followed by resolve();
    # the real parent lies outside, so the target is refused.
    with pytest.raises(KeepOnlyViolation):
        resolve_within(root, "linkdir/x.py")


# --- Refused: case-insensitive filesystems & fail-closed edges ------------


def test_rejects_case_variant_escape(root: Path, tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "x.py").touch()
    # Oracle: on case-insensitive APFS, ../SECRET resolves to the outside 'secret'
    # dir (refused as outside root); on a case-sensitive FS, 'SECRET' does not
    # exist (refused fail-closed). Either way the escape is refused.
    with pytest.raises(KeepOnlyViolation):
        resolve_within(root, "../SECRET/x.py")


@pytest.mark.parametrize("candidate", ["", "   "])
def test_rejects_empty_or_whitespace_candidate(root: Path, candidate: str) -> None:
    # Oracle: an empty or whitespace-only path names no writable target.
    with pytest.raises(KeepOnlyViolation):
        resolve_within(root, candidate)


def test_rejects_nonexistent_parent(root: Path) -> None:
    # Oracle: fail-closed — a write into a not-yet-existing directory is refused
    # rather than silently resolved lexically.
    with pytest.raises(KeepOnlyViolation):
        resolve_within(root, "nodir/x.py")


def test_violation_names_the_candidate(root: Path) -> None:
    with pytest.raises(KeepOnlyViolation) as excinfo:
        resolve_within(root, "../escape.py")
    # Oracle: the exception carries the offending candidate for the caller to log.
    assert excinfo.value.candidate == "../escape.py"
