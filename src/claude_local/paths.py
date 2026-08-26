"""Realpath containment — the security boundary of the loop.

The model may write ONLY inside a permitted subtree; the oracle test never lies
within it. ``resolve_within`` is the single owner of that rule: it resolves an
untrusted, model-supplied relative path against the permitted root and returns it
ONLY when the real, symlink-resolved target stays inside the root. Every doubt
fails closed with ``KeepOnlyViolation`` — a refused write is always safer than an
escaped one.

Pure path algebra over the standard library: this most security-critical module
imports nothing from the package, so nothing it depends on can weaken the rule.
"""

from __future__ import annotations

from pathlib import Path

# Resolution touches the filesystem (lstat/realpath), so a malformed path, a
# symlink loop, or a missing component can raise any of these. All are treated
# as fail-closed refusals — the boundary never lets an unresolvable path through.
_RESOLUTION_ERRORS = (OSError, RuntimeError, ValueError)


class KeepOnlyViolation(Exception):
    """A candidate path was refused: it does not resolve to a file inside the root.

    Carries the offending ``candidate`` (and a human ``reason``) so the caller can
    log exactly what was rejected without re-deriving it.
    """

    def __init__(self, candidate: str, reason: str) -> None:
        self.candidate = candidate
        self.reason = reason
        super().__init__(f"refused {candidate!r}: {reason}")


def resolve_within(root: Path, candidate: str) -> Path:
    """Resolve ``candidate`` against ``root``, returning it only if truly contained.

    ``candidate`` is untrusted (model output). The returned path is the real,
    symlink-resolved absolute target, guaranteed equal to ``root`` or beneath it.

    Args:
        root: The permitted subtree. Must exist.
        candidate: A relative path from within ``root``.

    Returns:
        The resolved absolute target, contained within ``root``.

    Raises:
        KeepOnlyViolation: candidate is empty, absolute, has a symlinked final
            component, has a non-existent parent, is otherwise unresolvable, or
            resolves outside ``root`` — every case is a fail-closed refusal.
    """
    if not candidate.strip():
        raise KeepOnlyViolation(candidate, "empty or whitespace-only path")
    if Path(candidate).is_absolute():
        raise KeepOnlyViolation(candidate, "absolute paths are never permitted")

    raw = root / candidate
    if raw.is_symlink():
        raise KeepOnlyViolation(candidate, "final component is a symlink")

    real_root = _resolve_strict(root, candidate)
    if raw.exists():
        real_target = _resolve_strict(raw, candidate)
    else:
        # A not-yet-existing file cannot be resolved: resolve its parent (which
        # must exist, symlinks and all) and re-append the lexical filename.
        real_target = _resolve_strict(raw.parent, candidate) / raw.name

    if real_root == real_target or real_root in real_target.parents:
        return real_target
    raise KeepOnlyViolation(candidate, "resolves outside the permitted root")


def _resolve_strict(path: Path, candidate: str) -> Path:
    """Fully resolve ``path``, failing closed if it cannot be resolved.

    ``candidate`` rides along only so a refusal names the original untrusted input
    rather than the intermediate path being resolved.
    """
    try:
        return path.resolve(strict=True)
    except _RESOLUTION_ERRORS as exc:
        raise KeepOnlyViolation(candidate, f"unresolvable path: {exc}") from exc
