# Derivation Map

## Derivations

| Artifact | Source of Truth | Derives From | Regeneration | Status | Superseded By |
|-|-|-|-|-|-|
| LocalEconomyRecord | run measurements: client token usage and timing | per-attempt GenerationResult aggregates | Telemetry aggregation at loop exit | active |  |
| PackageVersion | src/claude_local/__init__.py __version__ | — (root) | hatchling [tool.hatch.version] path reads __version__ at build | active |  |
| Scorecard | per-case run outcomes: each case's terminal status and local economy record | the CaseResult list from run_cases | score_cases() reduces the results; Scorecard.write() serializes JSON | active |  |
| TestScore | pytest JUnit-XML report | the attempt's test run | TestRunner.run() parses the XML | active |  |
| benchmark-expected-tests | benchmark-case oracle source | module-level test_* declarations | BenchmarkCase.from_fixtures parses the oracle AST | active |  |
| stable-prefix | src/claude_local/rules_card.md + TaskSpec | rules card + TaskSpec | PromptBuilder.stable_prefix(spec), assembled in-process | active |  |
| uv.lock | pyproject.toml | project and dependency-group requirements | uv lock | active |  |
