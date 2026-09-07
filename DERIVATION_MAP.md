# Derivation Map

## Derivations

| Artifact | Source of Truth | Derives From | Regeneration | Status | Superseded By |
|-|-|-|-|-|-|
| LocalEconomyRecord | run measurements: client token usage and timing | per-attempt GenerationResult aggregates | Telemetry aggregation at loop exit | active |  |
| PackageVersion | src/claude_local/__init__.py __version__ | — (root) | hatchling [tool.hatch.version] path reads __version__ at build | active |  |
| ResolvedModel | models/models.psv | one catalog row, split positionally | ModelRegistry.resolve(name) — no build step; edit the row | active |  |
| Scorecard | per-case run outcomes: each case's terminal status and local economy record | the CaseResult list from run_cases | score_cases() reduces the results; Scorecard.write() serializes JSON | active |  |
| TestScore | pytest JUnit-XML report | the attempt's test run | TestRunner.run() parses the XML | active |  |
| benchmark-expected-tests | benchmark-case oracle source | module-level test_* declarations | BenchmarkCase.from_fixtures parses the oracle AST | active |  |
| harmony_channel_stream.bytes | the running mlx_vlm server's wire format | one live streaming chat-completions response, recorded verbatim | scripts/capture_sse_fixture.py gpt-oss-20b tests/fixtures/sse/harmony_channel_stream.bytes --user 'Reply with exactly: OK' — re-record when mlx_vlm changes its wire format; a fresh capture is equivalent, not byte-identical (sampling varies), and the script's one substitution is the model id | active |  |
| plan-first-lever | TaskSpec.plan_first, the caller's declaration of how the run was configured | the task spec handed to Loop.run | carried onto LocalEconomyRecord at loop exit, then onto Scorecard via _held_constant and onto SweepResult from the scorecard JSON; never re-derived from whether a planning generation is present in the timeline, which would report a failed plan call as a run that never asked to plan | active |  |
| rules-card-digest | the rules-card bytes PromptBuilder read at construction | sha256 of the card after the same trailing-newline strip stable_prefix applies | PromptBuilder.card_digest, computed once at construction; carried onto LocalEconomyRecord and Scorecard, never re-derived downstream | active |  |
| stable-prefix | src/claude_local/rules_card.md + TaskSpec | rules card + TaskSpec | PromptBuilder.stable_prefix(spec), assembled in-process | active |  |
| uv.lock | pyproject.toml | project and dependency-group requirements | uv lock | active |  |
