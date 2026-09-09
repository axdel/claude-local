"""Tests for the style pass over produced code (``benchmarks.harness.style``).

An oracle answers whether a model's file works; nothing answers whether it is worth reading. This
pass supplies the second number, so these tests pin that it attributes each finding to the case
that produced it, that it judges by its own rule set rather than the repository's, and that clean
code scores clean. Every expected rule code comes from ruff's published rule table, never from
running the pass — a code this file asserts is one ``ruff rule <code>`` documents by that name.
The linter is a real subprocess against real files: it is local-substitutable, so mocking it would
leave the one thing under test — that the invocation actually reports these rules — unexercised.
"""

from pathlib import Path

from benchmarks.harness.style import StyleFinding, collect_style_findings

# Ruff's own rule codes (verified against the installed ruff via `ruff rule <code>`), each named
# for the produced-code defect it detects. These are a published specification, not observations.
_MISSING_MODULE_DOCSTRING = "D100"
_MISSING_FUNCTION_DOCSTRING = "D103"
_MISSING_RETURN_ANNOTATION = "ANN201"
_UNUSED_IMPORT = "F401"
_BLIND_EXCEPT = "BLE001"
_RAISE_WITHOUT_FROM = "B904"

# Written line by line rather than as one escaped string: these fixtures ARE the inputs under
# test, so a reader must be able to see the docstring and annotation each rule is looking for.
_CLEAN_SOURCE = (
    '"""A module that satisfies every selected rule."""\n'
    "\n"
    "\n"
    "def add(left: int, right: int) -> int:\n"
    '    """Return the sum of two numbers."""\n'
    "    return left + right\n"
)


def _produced(directory: Path, case_id: str, impl_path: str, source: str) -> None:
    """Lay one produced file into the saved-run shape ``<case_id>/<impl_path>``."""
    target = directory / case_id / impl_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")


def _rules(findings: tuple[StyleFinding, ...]) -> set[str]:
    """The distinct rule codes in a result — the axis every assertion here is about."""
    return {finding.rule for finding in findings}


def test_a_file_with_no_docstrings_reports_the_missing_docstring_rules(tmp_path: Path) -> None:
    """The defect measured on a passing 7/7 run — no docstring anywhere — is now a number.

    Oracle: ruff documents D100 as `undocumented-public-module` and D103 as
    `undocumented-public-function`. A module with neither must therefore report both, and that
    expectation is read off ruff's rule table rather than off this pass's output.
    """
    _produced(tmp_path, "01_scaffold", "app/main.py", "def health():\n    return 1\n")

    findings = collect_style_findings(tmp_path)

    assert _MISSING_MODULE_DOCSTRING in _rules(findings)
    assert _MISSING_FUNCTION_DOCSTRING in _rules(findings)


def test_an_unannotated_return_is_reported(tmp_path: Path) -> None:
    """The second measured defect: one function annotated, its neighbour not.

    Oracle: ruff documents ANN201 as `missing-return-type-undocumented-public-function`. The
    fixture mirrors the real finding — ``create_app`` carried a return type and ``health_endpoint``
    did not — so exactly one function is missing one, and the rule must name it.
    """
    source = (
        '"""Documented."""\n'
        "\n"
        "\n"
        "def health_endpoint():\n"
        '    """Return the health payload."""\n'
        '    return {"status": "ok"}\n'
    )
    _produced(tmp_path, "01_scaffold", "app/main.py", source)

    findings = collect_style_findings(tmp_path)

    assert _MISSING_RETURN_ANNOTATION in _rules(findings)


def test_each_finding_is_attributed_to_the_case_that_produced_it(tmp_path: Path) -> None:
    """Findings are per-case, because the comparison they serve is per-case.

    Oracle: the saved tree stores a case's answer under its own case id, so a finding's case is the
    first path segment beneath the run directory. Attributing to the file path alone would make two
    cases that both write ``app/main.py`` indistinguishable, which is the whole comparison.
    """
    _produced(tmp_path, "01_scaffold", "app/main.py", "import os\n")
    _produced(tmp_path, "02_schemas", "app/schemas.py", _CLEAN_SOURCE)

    findings = collect_style_findings(tmp_path)

    offending_cases = {finding.case_id for finding in findings}
    assert offending_cases == {"01_scaffold"}


def test_clean_code_reports_nothing(tmp_path: Path) -> None:
    """A file satisfying every selected rule scores zero, so the metric can reach zero.

    Oracle: the fixture carries a module docstring, a function docstring, annotated parameters and
    an annotated return — the exact conditions D100, D103, ANN001 and ANN201 each require. A metric
    that could never reach zero would report a floor rather than a defect count.
    """
    _produced(tmp_path, "02_schemas", "app/schemas.py", _CLEAN_SOURCE)

    assert collect_style_findings(tmp_path) == ()


def test_the_pass_judges_by_its_own_rules_not_the_repositorys(tmp_path: Path) -> None:
    """The repository's ruff config must not reach this measurement.

    Oracle: ``pyproject.toml`` selects E, F, W, I, S, UP, B, C4, SIM and RUF — no D and no ANN —
    and separately excludes the produced-code tree entirely. Under the repository's config this
    file would report nothing at all, so a D-family finding here is proof the pass ran isolated.
    That isolation is deliberate in both directions: a model's style is not this repository's
    findings, and this repository's rule selection is not the measurement.
    """
    _produced(tmp_path, "01_scaffold", "app/main.py", "def health():\n    return 1\n")

    findings = collect_style_findings(tmp_path)

    assert any(finding.rule.startswith("D") for finding in findings)


def test_an_unused_import_is_reported(tmp_path: Path) -> None:
    """Dead code the linter *can* see is counted.

    Oracle: ruff documents F401 as `unused-import`. This is the detectable half of the dead-code
    defect class — a guard against a state the code just made impossible has no rule in any linter,
    so this pass measures unused imports, variables and arguments and makes no claim beyond them.
    """
    source = '"""Documented."""\n\nimport os\n'
    _produced(tmp_path, "03_repositories", "app/repository.py", source)

    findings = collect_style_findings(tmp_path)

    assert _UNUSED_IMPORT in _rules(findings)


def test_swallowing_an_error_is_reported(tmp_path: Path) -> None:
    """The card tells the model not to catch broadly or drop a cause; the pass must see both.

    Oracle: ruff documents BLE001 as `blind-except` and B904 as
    `raise-without-from-inside-except`. The fixture is the shape a real model produced in an auth
    service — a bare `except Exception` that swallows the exception the same block raised, and a
    re-raise with no `from`. Both codes come from ruff's rule table, not from running this pass.
    """
    source = (
        '"""Documented."""\n'
        "\n"
        "\n"
        "def verify(token: str) -> int:\n"
        '    """Return the identifier a token carries."""\n'
        "    try:\n"
        "        return int(token)\n"
        "    except Exception:\n"
        "        raise ValueError(token)\n"
    )
    _produced(tmp_path, "04_auth_service", "app/services/auth_service.py", source)

    findings = collect_style_findings(tmp_path)

    assert _BLIND_EXCEPT in _rules(findings)
    assert _RAISE_WITHOUT_FROM in _rules(findings)


def test_a_finding_carries_the_line_it_was_found_on(tmp_path: Path) -> None:
    """A count says how many; a line says where, so the artifact stays readable beside the code.

    Oracle: the fixture places its only undocumented function on line 4 — after the module
    docstring on line 1 and two blank lines — so D103's line is 4 by construction of the input,
    derived by counting the fixture rather than by reading the pass's output.
    """
    source = '"""Documented."""\n\n\ndef health() -> int:\n    return 1\n'
    _produced(tmp_path, "01_scaffold", "app/main.py", source)

    findings = collect_style_findings(tmp_path)

    docstring_findings = [f for f in findings if f.rule == _MISSING_FUNCTION_DOCSTRING]
    assert [finding.line for finding in docstring_findings] == [4]
