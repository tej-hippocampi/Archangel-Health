---
name: prd-audit
description: Verify a PRD's file:line citations against the current tree before the PRD is declared done. Use whenever a PRD is written, reviewed, or claimed complete, or when a PRD's citations may have drifted. Fixes the PRD, never the code.
---

# /prd-audit

Every PRD in this repo has drifted at least once — `ingestion.py:1533→1534`,
`asclepius.js:7462→7997`, `store.py:291→503`. A drifted citation is worse than no
citation: it sends an agent to edit a confidently-named wrong line.

## Run it

```bash
python3 backend/scripts/prd_audit.py docs/prd/YOUR_PRD.md
```

Prints one row per citation — `citation · verdict · actual line` — and exits 1 if
any citation is stale or a required section is missing.

## The rule

**A failing citation is fixed in the PRD, never in the code.** The tree is the
fact; the PRD is the description. If the description disagrees, the description
is wrong. Re-run until clean.

The only exception is a citation that reveals a genuine bug — then fix the bug in
its own commit, and still update the PRD.

## What it checks

1. Every `path:line` resolves to a real file, and the line exists.
2. If a symbol is backticked in the same table cell or sentence, that symbol
   appears on the cited line.
3. The PRD has a design/invariant section, a tests section, and a
   do-not-touch section.

## Reading the verdicts

- `OK` — the cited line contains the cited symbol.
- `NO SYMBOL` — the line resolves but the PRD named no symbol to check it
  against. Not a failure; consider adding one so the citation can be verified.
- `DRIFTED` — the line exists but does not contain the symbol. Find the symbol's
  real line and update the PRD.
- `NO FILE` — unresolvable, or ambiguous (four `store.py` files live here; cite
  `asclepius/store.py`, not the bare basename).
- `OUT OF RANGE` — the file is shorter than the cited line.

`file.py:123→456` is skipped by design: the arrow form narrates a citation that
already drifted, so auditing it would re-report history as a fresh failure.

## Before declaring any PRD done

Run this, get exit 0, and say so in the commit or PR. An unaudited PRD is a PRD
whose citations you are asserting on faith.
