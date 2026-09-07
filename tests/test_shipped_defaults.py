"""What ships to the user works on their first real run — entry points and their argv alike.

Two shipped surfaces, one failure mode: something correct in every in-process test that breaks
the moment a person runs the documented command. Every replay transport in the loop and benchmark
tests routes by request BODY, not URL path, so nothing else here can notice which server an entry
point actually dialled — a guessed default reaches whatever is listening and no in-process test
objects. An executable script is the same shape: its shebang picks the interpreter, and nothing
that imports the module in-process ever exercises that choice.

This is the one place that runs the exact commands users run with.
"""

import os
import subprocess  # nosec B404 (argv is a discovered script path, never shell-interpreted)
from collections.abc import Callable
from pathlib import Path

import pytest

from benchmarks.run import main as benchmark_main
from examples.quicksort.run import main as example_main

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO_ROOT / "scripts"


def _executable_scripts() -> list[Path]:
    """Every script in ``scripts/`` carrying the executable bit, sorted.

    Scope is read off the filesystem rather than listed here, because the executable bit is the
    exact fact that makes a shebang load-bearing: set it and the documented ``scripts/x.py``
    invocation becomes a promise. A script added later is covered without editing this test.
    """
    return sorted(path for path in _SCRIPTS.glob("*.py") if os.access(path, os.X_OK))


def _plain_shell_environment() -> dict[str, str]:
    """The environment a user has, not the one pytest runs in — the venv scrubbed back out.

    This is the whole difference between a test that bites and one that cannot. pytest itself runs
    under ``uv run``, which puts ``.venv/bin`` first on PATH and sets VIRTUAL_ENV, so a subprocess
    inheriting it resolves ``env python3`` to the project's own interpreter — the one that already
    has every dependency. A script whose shebang names a bare ``python3`` therefore succeeds under
    the test and dies in the user's terminal, where ``python3`` is the system one.

    Removing both makes the child resolve interpreters the way the user's shell does. ``uv`` is
    left reachable because it is installed outside the venv, which is what lets a shebang that
    names ``uv run`` recover the environment on its own — the property under test.
    """
    environment = {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}
    venv_bin = str(_REPO_ROOT / ".venv" / "bin")
    path_entries = environment.get("PATH", "").split(os.pathsep)
    environment["PATH"] = os.pathsep.join(entry for entry in path_entries if entry != venv_bin)
    return environment


_USAGE_ERROR = 2
"""Both entry points document exit 2 for a usage error — read off the docstrings, not the code."""


@pytest.mark.parametrize(
    ("label", "entry_point"),
    [("benchmark", benchmark_main), ("example", example_main)],
)
def test_a_missing_base_url_is_refused_rather_than_defaulted(
    label: str,
    entry_point: Callable[[list[str]], int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no server named, each entry point refuses instead of dialling a guessed port.

    Oracle: D-CLI-002, already ratified for the machine CLI — "a missing value is refused rather
    than defaulted... Rejected a localhost default: it would silently send the task to whatever
    happened to be listening." These two shipped surfaces carry the identical hazard, so the
    identical rule binds them; exit 2 is the usage code both docstrings declare.

    Not hypothetical: every catalogued model serves on 8081-8093, and the registry documents 8080
    as deliberately unassignable because Docker Desktop binds it. The default that used to sit here
    could therefore only ever reach a foreign process or nothing — never a model this project
    serves.
    """
    monkeypatch.delenv("CLAUDE_LOCAL_BASE_URL", raising=False)

    assert entry_point(["--model", "any-resident-model"]) == _USAGE_ERROR, (
        f"the {label} entry point reached a server nobody named"
    )


@pytest.mark.parametrize("script", _executable_scripts(), ids=lambda path: path.name)
def test_an_executable_script_runs_under_its_own_shebang(script: Path) -> None:
    """Each executable script answers ``--help`` when invoked the way its docstring says.

    Oracle: ``--help`` exits 0 and prints a usage line naming the program — argparse's documented
    behavior, derived from the library contract rather than from running these scripts. So exit 0
    plus the script's own name in the usage line proves the shebang reached an interpreter that
    could import the project and build the parser.

    The script is executed by PATH, never as ``sys.executable <script>``, because the shebang IS
    the subject: handing it a known-good interpreter would test something the user never does.
    Every one of these imports ``claude_local`` or ``httpx``, so a shebang naming an interpreter
    without the project environment dies on the first import — which is exactly the failure this
    test exists to catch, and which no in-process import of the same module can ever surface.

    It runs under ``_plain_shell_environment`` for the same reason: inheriting pytest's own
    environment hands the child the project interpreter and makes every shebang look correct.
    """
    completed = subprocess.run(  # noqa: S603
        [str(script), "--help"],
        cwd=_REPO_ROOT,
        env=_plain_shell_environment(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, f"{script.name} --help failed:\n{completed.stderr}"
    assert f"usage: {script.name}" in completed.stdout
