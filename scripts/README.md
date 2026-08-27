# scripts/

Committed, re-runnable dev tooling — the reproducible home for anything done more than
once, or once but required to track code: env rebuilds, seed / fixture / template
regeneration, repeatable diagnostic probes.

**Why this exists.** A hand-made artifact with no regeneration script is an undeclared
derivation that silently lags the code it depends on. If you catch yourself re-typing a
multi-step procedure from memory, or dumping a snapshot by hand, script it here instead.

**Graduation from the scratchpad.** One-shot exploration stays in the harness scratchpad.
The second need — or the first if it must stay in sync with code — moves it here, committed.

**Housekeeping.** A script is authored source, not a build artifact: commit the generator,
never its large output. Delete dead scripts like any dead code — VCS keeps the history.

Contract: `~/.claude/rules/development-discipline.md` → Rule 21.
