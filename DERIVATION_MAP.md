# Derivation Map

## Derivations

| Artifact | Source of Truth | Derives From | Regeneration | Status | Superseded By |
|-|-|-|-|-|-|
| BuildStatus | loop terminal Status | Status enum | contract.to_build_status(status) | active |  |
| LocalEconomyRecord | run measurements: client token usage and timing | per-attempt GenerationResult aggregates | Telemetry aggregation at loop exit | active |  |
| TestScore | pytest JUnit-XML report | the attempt's test run | TestRunner.run() parses the XML | active |  |
| stable-prefix | src/claude_local/rules_card.md | rules card + task spec + immutable test | PromptBuilder.stable_prefix(spec), assembled in-process | active |  |
