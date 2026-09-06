"""The style pass — a second number for produced code, beside whether its oracle passed.

An oracle test answers one question: does the model's file work. It cannot answer whether the file
that passed is worth keeping, so a run where every case is green and every function is
undocumented scores identically to one where neither is true. This pass supplies the missing
number by linting the saved code tree, and it is deliberately kept OUT of the scorer: the code is
already on disk, so style is a post-hoc pass over artifacts that leaves the scorecard a pure
correctness record and reruns against any earlier run.

The linter is invoked isolated from this repository's own configuration, in both directions and
for two different reasons. The repository excludes the produced tree because a weak model's style
is not this repository's findings; this pass ignores the repository's rule selection because that
selection was chosen for source we write, not for a measurement of code we did not.

What it measures is bounded and stated: missing docstrings, missing annotations, the unused
imports, variables and arguments a linter can see, and errors caught blindly or re-raised without
their cause. Each selected rule names a rule the rules card already states, so the pass measures
whether stating it worked rather than some general notion of quality. It makes no claim beyond
them — notably, a guard against a state the code itself just made impossible has no rule in any
linter, so that defect class is visible to a reader and invisible here.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

# Ruff rule codes, each named for the produced-code defect it detects. Specific codes rather than
# family wildcards: the D family carries internally incompatible members (D203 against D211, D212
# against D213) that ruff resolves by warning on every invocation, and a measurement should not
# emit noise it did not choose. Verified present via `ruff rule <code>`.
_SELECTED_RULES = (
    "D100",  # undocumented-public-module
    "D101",  # undocumented-public-class
    "D102",  # undocumented-public-method
    "D103",  # undocumented-public-function
    "ANN001",  # missing-type-function-argument
    "ANN201",  # missing-return-type-undocumented-public-function
    "ANN202",  # missing-return-type-private-function
    "F401",  # unused-import
    "F841",  # unused-variable
    "ARG",  # unused function, method, class-method and lambda arguments
    "BLE001",  # blind-except
    "B904",  # raise-without-from-inside-except
)

# Ruff exits 1 to report findings and 0 when clean; both carry a parseable document. Any other code
# is the linter failing rather than judging, and is raised rather than read as "no findings".
_LINTER_VERDICT_EXIT_CODES = frozenset({0, 1})


@dataclass(frozen=True, slots=True)
class StyleFinding:
    """One linter finding against one produced file, attributed to the case that produced it.

    ``case_id`` is the first path segment beneath the run directory, which is how the saved tree
    stores a case's answer. Attribution by file path alone would merge two cases that both write
    ``app/main.py`` — and comparing cases is the entire purpose of the measurement.
    """

    case_id: str
    rule: str
    message: str
    line: int


def collect_style_findings(code_directory: Path) -> tuple[StyleFinding, ...]:
    """Lint every produced file under a saved run directory; return its findings, case-attributed.

    One linter invocation covers the whole tree rather than one per file: the codes and the
    isolation flag are identical for every file, so per-file invocations would pay the process cost
    once per case to reach the same verdict.

    Args:
        code_directory: A saved run directory laid out as ``<case_id>/<impl_path>``.

    Returns:
        Every finding, sorted by case, line, then rule, so two runs over one tree agree.

    Raises:
        FileNotFoundError: the linter is not installed beside the running interpreter or on PATH.
        RuntimeError: the linter exited with neither a clean nor a findings verdict.
    """
    root = code_directory.resolve()
    document = _run_linter(root)
    return tuple(sorted(_findings(document, root), key=lambda f: (f.case_id, f.line, f.rule)))


def _run_linter(root: Path) -> list[dict[str, object]]:
    """Invoke the linter over the whole tree and return its parsed findings document."""
    completed = subprocess.run(  # noqa: S603 — fixed argv, no shell, path from our own resolve()
        [
            str(_linter_executable()),
            "check",
            "--no-cache",
            "--isolated",
            f"--select={','.join(_SELECTED_RULES)}",
            "--output-format=json",
            str(root),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode not in _LINTER_VERDICT_EXIT_CODES:
        raise RuntimeError(
            f"style linter exited {completed.returncode} instead of judging: {completed.stderr}"
        )
    return json.loads(completed.stdout)


def _linter_executable() -> Path | str:
    """The linter beside the running interpreter, else its bare name for a PATH lookup.

    Resolving against ``sys.executable`` rather than trusting PATH is what makes this work the same
    way under a bare shell as under the project runner: the two put different directories first, so
    a PATH-only lookup finds the linter in one and nothing in the other.
    """
    adjacent = Path(sys.executable).parent / "ruff"
    return adjacent if adjacent.exists() else "ruff"


def _findings(document: list[dict[str, object]], root: Path) -> Iterator[StyleFinding]:
    """Convert each linter record into a case-attributed finding."""
    for record in document:
        filename = Path(str(record["filename"]))
        location = record["location"]
        yield StyleFinding(
            case_id=filename.relative_to(root).parts[0],
            rule=str(record["code"]),
            message=str(record["message"]),
            line=int(location["row"]) if isinstance(location, dict) else 0,
        )
