"""Tests for the benchmark-command construction in ``scripts/benchmark_model.py``.

The script's job is to supply the standing benchmark's one prerequisite — a running server — and
then invoke the documented benchmark command against it. Everything that decides WHAT that command
says is pure, and is what these tests cover: which flags are forwarded, and how a catalog row's
generation parameters cross the process boundary into them. Serving a real model is not exercised
here; that is the live path, and a unit test that spawned 20 GB of weights would be neither.

Expected values are read off the benchmark CLI's own declared argument surface, never off the
builder — the two are separate modules precisely so one can check the other.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import MappingProxyType, ModuleType

import pytest

_SCRIPT = Path(__file__).parents[1] / "scripts" / "benchmark_model.py"


def _script() -> ModuleType:
    """Import the script by path, the way its shebang runs it.

    It lives in ``scripts/`` rather than the package, so no import statement can name it. Loading
    it through importlib is what lets its pure parts be tested at all — the alternative is testing
    only what a subprocess prints, which cannot see an argument that was silently dropped.
    """
    spec = importlib.util.spec_from_file_location("benchmark_model", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_generation_params_are_forwarded_as_one_json_object(tmp_path: Path) -> None:
    """A row's parameters reach the benchmark as JSON, the encoding its flag declares.

    Oracle: ``benchmarks/run.py`` declares ``--generation-params`` as a JSON object of request-body
    fields, so that is the contract this side must satisfy. Asserting on the decoded value rather
    than a literal string keeps the test off key order, which no contract fixes; asserting the
    boolean rather than "false" is the point of the encoding, since a string is truthy in a body
    and would leave the behaviour on.

    The input is a ``MappingProxyType`` because that is what the registry actually resolves a row
    to — a read-only mapping, deliberately not a dict. A plain-dict fixture here reads identically
    and tests nothing: ``json.dumps`` accepts the dict and rejects the mapping proxy, so the
    convenient fixture is green against code that raises on every real catalog row.
    """
    command = _script().benchmark_command(
        base_url="http://localhost:8080",
        served="lmstudio-community/Qwen3.8-27B-MLX-6bit",
        out=tmp_path,
        generation_params=MappingProxyType({"enable_thinking": False, "top_k": 20}),
        stream=False,
        only=(),
    )

    assert "--generation-params" in command
    declared = command[command.index("--generation-params") + 1]
    assert json.loads(declared) == {"enable_thinking": False, "top_k": 20}


def test_a_model_declaring_no_generation_params_omits_the_flag(tmp_path: Path) -> None:
    """An empty mapping sends no flag at all, rather than an empty JSON object.

    Oracle: the flag is optional and defaults to declaring nothing, so passing ``{}`` explicitly
    would say the same thing in more words. Most catalog rows declare no parameters, so this is the
    common shape of the command and the one a reader will check against the docstring.
    """
    command = _script().benchmark_command(
        base_url="http://localhost:8080",
        served="mlx-community/gpt-oss-20b-MXFP4-Q8",
        out=tmp_path,
        generation_params={},
        stream=False,
        only=(),
    )

    assert "--generation-params" not in command


def test_the_command_carries_the_server_the_run_is_scored_against(tmp_path: Path) -> None:
    """Base URL, served id, and output directory are the benchmark's required arguments.

    Oracle: ``benchmarks/run.py`` takes the server it scores against as ``--base-url``/``--model``
    and its scorecard destination as ``--out``. The served id is the one the server REPORTS, not
    the catalog name — a scorecard labelled with a name the server never served would attribute the
    result to the wrong weights.
    """
    command = _script().benchmark_command(
        base_url="http://127.0.0.1:8089",
        served="lmstudio-community/Qwen3.8-27B-MLX-6bit",
        out=tmp_path,
        generation_params={},
        stream=False,
        only=(),
    )

    assert command[command.index("--base-url") + 1] == "http://127.0.0.1:8089"
    assert command[command.index("--model") + 1] == "lmstudio-community/Qwen3.8-27B-MLX-6bit"
    assert command[command.index("--out") + 1] == str(tmp_path)


@pytest.mark.parametrize(
    ("stream", "expected"),
    [pytest.param(True, True, id="asked"), pytest.param(False, False, id="not-asked")],
)
def test_stream_is_forwarded_only_when_asked(stream: bool, expected: bool, tmp_path: Path) -> None:
    """``--stream`` is a store-true flag downstream, so it is present or absent, never valued."""
    command = _script().benchmark_command(
        base_url="http://localhost:8080",
        served="replay/golden",
        out=tmp_path,
        generation_params={},
        stream=stream,
        only=(),
    )

    assert ("--stream" in command) is expected


def test_a_chosen_rules_card_is_forwarded_as_a_path(tmp_path: Path) -> None:
    """``--rules-card`` reaches the benchmark as the path it was given.

    Oracle: ``benchmarks/run.py`` declares ``--rules-card`` as a ``Path``-typed valued flag. A
    dropped one is the silent failure this module exists to catch — the benchmark would run
    perfectly well on the bundled card and produce a scorecard labelled with the wrong digest.
    """
    card = tmp_path / "experimental_card.md"

    command = _script().benchmark_command(
        base_url="http://localhost:8080",
        served="replay/golden",
        out=tmp_path,
        generation_params={},
        stream=False,
        only=(),
        rules_card=card,
    )

    assert command[command.index("--rules-card") + 1] == str(card)


def test_no_chosen_rules_card_omits_the_flag_entirely(tmp_path: Path) -> None:
    """Omitting the flag is what leaves the bundled card in force.

    Passing ``--rules-card`` with an empty or ``"None"`` value would make the benchmark open a
    path that does not exist, turning a default into a crash.
    """
    command = _script().benchmark_command(
        base_url="http://localhost:8080",
        served="replay/golden",
        out=tmp_path,
        generation_params={},
        stream=False,
        only=(),
    )

    assert "--rules-card" not in command


def test_every_named_case_is_forwarded_as_its_own_only_flag(tmp_path: Path) -> None:
    """``--only`` is repeatable downstream, so N cases are N flags — never one joined value.

    Oracle: the benchmark declares ``--only`` with ``action="append"``, which reads one id per
    occurrence. A comma-joined value would arrive as a single unknown id and exit 2.
    """
    command = _script().benchmark_command(
        base_url="http://localhost:8080",
        served="replay/golden",
        out=tmp_path,
        generation_params={},
        stream=False,
        only=("01_scaffold", "05_rbac"),
    )

    assert command.count("--only") == 2
    assert command[command.index("--only") + 1] == "01_scaffold"
    assert "05_rbac" in command
