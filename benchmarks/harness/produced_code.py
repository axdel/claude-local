"""The code a model actually wrote, saved beside its scorecard so a human can read it.

A scorecard answers *how many* oracle tests passed. It cannot answer whether the implementation
that passed them is any good — whether the names reveal intent, whether the structure is one a
reviewer would accept, whether a case that scored 6 of 7 missed by a detail or by a
misunderstanding. Those are judgements only a reader can make, and until this module existed there
was nothing to read: the driver tears each case's worktree down, so ``Outcome.code`` was the last
reference to the model's work and the scorer dropped it.

Written as ordinary source files rather than folded into the scorecard JSON. The point of the
artifact is that a person opens it, and a 5 KB module escaped into a JSON string field is neither
readable nor reviewable; it would also bloat the one artifact that is meant to stay small and
diffable across models.

**Failed cases are written too, and are the most interesting ones.** A case that exhausted its
budget at 6 of 7 tests is exactly the code worth reading, so the only case with nothing on disk is
one where the loop scored no model edit at all (``code`` is ``None``).

Cold path: this runs once, after the whole ladder, off the inference hot path (E6).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from claude_local import slug_model_id

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from .driver import CaseResult

_RUN_DIRECTORY_PREFIX = "code-"
"""The directory-name prefix ``write_produced_code`` composes and ``latest_run_directory`` globs.

Spelled once, on the side that writes it: a reader that spells the prefix itself is a second
writer to the name, and the two stay agreed only until one of them is edited.
"""


def write_produced_code(
    results: Sequence[CaseResult], model: str, directory: Path, stamp_ms: int
) -> Path:
    """Save each case's produced file under one run-stamped directory. Returns that directory.

    Each file lands at ``<case_id>/<impl_path>``, mirroring the path the model was told to write,
    so a reader browses the answers in the same shape as the golden app they are compared against.

    The directory name pairs with the scorecard's — same model slug, same stamp — so a verdict and
    the code behind it are matched by name rather than by having to correlate mtimes.

    Args:
        results: One ``CaseResult`` per case, in benchmark order, as ``run_cases`` returns them.
        model: The model id the run scored, used for the directory name.
        directory: Where the run-stamped directory is created.
        stamp_ms: The run's stamp, the same one its scorecard was written under. Supplied rather
            than read here because a second clock read is what breaks the pairing above.

    Returns:
        The run-stamped directory the files were written into, created even when every case
        produced nothing — an empty directory is itself the finding that no case scored an edit.
    """
    run_directory = directory / f"{_RUN_DIRECTORY_PREFIX}{slug_model_id(model)}-{stamp_ms}"
    run_directory.mkdir(parents=True, exist_ok=True)
    for result in results:
        if result.outcome.code is None:
            continue
        # impl_path is validated against traversal before the loop will write it, so by the time
        # it reaches an Outcome it is a known-relative path inside the case (see the driver's
        # rejection of '../escape.py', '/outside/escape.py', and a bare directory).
        target = run_directory / result.case_id / result.outcome.impl_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(result.outcome.code, encoding="utf-8")
    return run_directory


def latest_run_directory(directory: Path) -> Path | None:
    """The most recently written produced-code directory in ``directory``, or ``None`` if none is.

    The inverse of the naming above, and here for that reason. Names end in a millisecond stamp, so
    the newest sorts last by name — which beats comparing modification times, since reading a tree
    can leave those unequal to write order.
    """
    run_directories = sorted(
        path for path in directory.glob(f"{_RUN_DIRECTORY_PREFIX}*") if path.is_dir()
    )
    return run_directories[-1] if run_directories else None
