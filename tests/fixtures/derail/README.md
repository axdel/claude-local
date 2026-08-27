# derail fixtures — the two-fixture oracle

These are **behavioral scenarios**, not external-protocol captures, so their oracle is the
detection contract itself (D-DERAIL-001: *consecutive exact repetition of a large-n line is a
stuck decode; global token-diversity is not*), derived independently of the code under test —
not a wire shape that would demand a captured or schema-validated fixture.

| File | Represents | Required verdict |
|------|------------|------------------|
| `derail_loop.txt` | A weak model that begins a valid implementation, then falls into a stuck decode — one substantial line (`            last[1] = max(last[1], end)`, 39 chars) emitted 10 times in a row, well past the warmup. | **REPETITION fires** |
| `valid_repetitive.txt` | Legitimately repetitive-but-valid output: a long markdown table whose rows are structurally similar and individually large-n, yet every row is content-distinct. The false-positive trap the guard must survive. | **must NOT fire** |

The pair is the review's explicit ask: a real derail must be caught, and structurally-repetitive
valid output must not be. A detector that fires on the table would abort healthy generations — the
HIGH risk this task carries.
