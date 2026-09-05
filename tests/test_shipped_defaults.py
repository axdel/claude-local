"""What ships to the user works on their first real run — defaults and entry points alike.

Two shipped surfaces, one failure mode: something correct in every in-process test that breaks
the moment a person runs the documented command. ``HttpxBackend`` owns the OpenAI path suffix
(``/v1/chat/completions``), so a shipped default that itself carried ``/v1`` would build
``.../v1/v1/chat/completions`` and 404 — and every replay transport in the loop and benchmark
tests routes by request body, not URL path, so the doubling never surfaces there. An executable
script is the same shape: its shebang picks the interpreter, and nothing that imports the module
in-process ever exercises that choice.

This is the one place that runs the exact values and the exact commands users run with.
"""

import os
import subprocess  # nosec B404 (argv is a discovered script path, never shell-interpreted)
from pathlib import Path

import httpx
import pytest

from benchmarks.run import _DEFAULT_BASE_URL as _BENCHMARK_DEFAULT
from claude_local.backend import HttpxBackend
from examples.quicksort.run import _DEFAULT_BASE_URL as _EXAMPLE_DEFAULT

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


@pytest.mark.parametrize(
    ("label", "base_url"),
    [("benchmark", _BENCHMARK_DEFAULT), ("example", _EXAMPLE_DEFAULT)],
)
def test_shipped_default_base_url_yields_one_versioned_openai_endpoint(
    label: str, base_url: str
) -> None:
    """The backend appends exactly one OpenAI version+endpoint segment to each shipped default.

    Oracle: the OpenAI chat-completions endpoint is ``/v1/chat/completions`` exactly once
    (a specification fact the backend appends), so a correct default is the bare root and the
    constructed URL carries the version segment once — never the doubled ``/v1/v1`` a
    ``/v1``-suffixed default would produce.
    """
    with httpx.Client() as client:
        backend = HttpxBackend(base_url, client, "model-under-test")

    assert "/v1/v1" not in backend._url, f"{label} default doubles the version prefix"
    assert backend._url.count("/v1") == 1  # exactly one version segment (a /v1 default → 2)
    assert backend._url.endswith("/v1/chat/completions")


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
