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
