# Tech Debt

The Tech-Debt Ledger — the single destination for a deferred finding that met the
ACTION-FIRST escalation gate. `CLAUDE.md` holds one pointer to this file, never rows.

**Every row is a prior-session claim, not a verified fact.** Re-derive it from current
source before acting on it; a locator is not proof the claim still holds.

Cite a file and the symbol inside it, or an immutable commit hash — never `file.py:NNN`.
An edit above a line falsifies the citation while leaving it perfectly readable.

`fix_by` is an ISO-8601 `YYYY-MM-DD` date — a prose deadline cannot be compared, so it
reads as never-overdue and the row is never triaged.

Full contract — what belongs here, how rows are appended and retired, and what a past
`fix_by` obliges: `~/.claude/rules/development-discipline.md` → Tech-Debt Ledger.

| Item | fix_by | Notes |
|-|-|-|
| Dependency-metrics gate cannot import any analyzed project | 2026-11-30 | The fix is in the `claude-protocol` repo, not this one — `build_dependency_report` there calls `grimp.build_graph(root_package)` in-process, under claude-protocol's own interpreter. A console script's `sys.path[0]` is the script's `bin/` directory and the working directory is never added, so that call can import only packages installed in the tool's own venv. Every analyzed project fails identically — flat-layout and src-layout alike; claude-protocol appears to work only because it is analyzing itself. Measured here: `claude-protocol quality dep-metrics` reports "Could not find package 'claude_local' in your Python path", while `PYTHONPATH=src claude-protocol quality dep-metrics` returns the full report. Workaround documented in this project's `CLAUDE.md` → Commands. Real fix upstream: resolve the package from the project root before building the graph (prepend the project's source root to `sys.path`, or run grimp in a subprocess whose path includes it). |
