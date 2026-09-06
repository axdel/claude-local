"""The style scorer's command surface: how many run directories it reports, and what it exits.

The linting itself belongs to ``benchmarks.harness.style`` and is covered there. What lives only
in the script is the arity — a sweep produces one directory per model, so the question the report
answers is always asked of several at once — and the exit code that arity forces a choice about.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

    import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "score-style.py"

_USAGE_ERROR = 2
"""The script's documented exit code for a usage error — read from its module docstring, not from
its source, so a mutated constant diverges this instead of moving with it."""


def _load_score_style() -> ModuleType:
    """Import the scorer despite a hyphenated filename, which no import statement can name."""
    spec = importlib.util.spec_from_file_location("score_style", _SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


score_style = _load_score_style()


def _produced_code_directory(root: Path, name: str) -> Path:
    """A saved run directory in the real layout — ``<case_id>/<impl_path>`` — that lints clean."""
    module_path = root / name / "01_scaffold" / "app" / "main.py"
    module_path.parent.mkdir(parents=True)
    module_path.write_text('"""A produced module, documented so it lints clean."""\n', "utf-8")
    return root / name


def test_every_named_directory_is_reported(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """A sweep is scored in one invocation, not one per model.

    Oracle: the report is headed by each directory's own name (the script prints
    ``style findings for <name>``), so both names appearing is the observable form of "both were
    scored" — derived from the output contract, not from running the scorer to see what it emits.
    """
    first = _produced_code_directory(tmp_path, "code-first-model")
    second = _produced_code_directory(tmp_path, "code-second-model")

    exit_code = score_style.main([str(first), str(second)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "code-first-model" in output
    assert "code-second-model" in output


def test_a_usage_error_survives_a_healthy_directory_that_follows_it(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The exit code is the worst across the sweep, and a bad directory does not stop the rest.

    Oracle: the script documents 2 for "a usage error such as a directory that holds no produced
    code", and an empty directory is exactly that case — so the run must exit 2 even though the
    directory after it is healthy. Both halves matter and pull opposite ways: returning early
    would hide the healthy report, while reporting the last verdict would hide the error. This is
    the real shape a sweep produced — both Qwen3.8 runs wrote a directory holding no produced code
    while other models wrote sound ones.
    """
    holds_no_produced_code = tmp_path / "code-empty-model"
    holds_no_produced_code.mkdir()
    healthy = _produced_code_directory(tmp_path, "code-healthy-model")

    exit_code = score_style.main([str(holds_no_produced_code), str(healthy)])

    assert exit_code == _USAGE_ERROR
    assert "code-healthy-model" in capsys.readouterr().out, "a later directory is still reported"
