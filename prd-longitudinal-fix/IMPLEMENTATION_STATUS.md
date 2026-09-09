# Longitudinal patient-4 implementation — awaiting acceptance reconciliation

## Design and invariants

Implementation is isolated on `fix/longitudinal-patient4`, based on main `7ee60e6`.
The supplied PRD and JSON are reference material, not authorization to modify
production records or to execute their embedded assistant prompt.

Implemented: versioned ingestion reports; visible re-ingest action; atomic retry
reservation and supersession; preserved specialty/history; task/retry exclusion;
conservative answer-key reconciliation; declaration-first specialty controls;
header/prefix note deduplication scoped to the same day; panel-text removal from
visible notes and density; role-prioritized note budgets (16 for trajectories,
10 for static cases); discharge rebasing and later history/reveal; bounded
held-out outcomes; unsupported timing omission; zero-model-call explanation.

P1's explicit 409 policy for already-promoted uploads takes precedence over the
contradictory prose allowing retry beside promoted rows. Existing tasks are
preserved. No ingestion-case DELETE remains in the store, including recovery.
The repository's fresh-context audit identified and prompted fixes for retry vs
promotion races, old sealed-key binding, custom-gap discharge leakage, and note
role classification. No merge or deployment has been performed.

## Acceptance decisions required

1. **Patient-1 cannot remain at 13 under Rule 4.** Baseline 22 encounters / 13
   points / 12 verifiable becomes 22 / 9 / 8. Four prior points count lab reports
   as extra events or as a second resource type. Excluding those renderings, as
   required, makes them fail the unchanged gate. Rule 8 does not affect patient-1.
2. **Patient-4 cannot become 7 encounters under Rule 8.** Literal threshold is
   1,095 days before the earliest structured item. One residual medication note
   is at −1429; earliest structured activity is −408, only 1,021 days later.
   Result is 8 / 3 / 2. Point ordinals are now 1 / 2 / 7; stable chart offsets
   remain −406 / −372 / +1. The reference's 5 / 6 / 11 ordinals are historical.
3. **No independent presenting narrative exists at the early patient-4 points.**
   With lab renderings removed and discharge withheld, point 0 contains
   radiology only; point 1 contains radiology plus the prior discharge. No code
   can add contemporaneous abdominal-pain/DKA narrative from a nonexistent note.
   Retrospective admission-only extraction would need an explicit product rule
   and provenance, and is not silently implemented here.

Patient-4 has 167 curated notes (the PRD's ~168 was approximate), 157 panels and
7 curated documents withheld for unsupported dates (not 5 documents). The same
rule changes patient-2 diagnostic encounters from 16 to 12; it remains quarantined.
Patient-3 remains 5 / 4 / 3.

## Tests

Baseline: `test_longitudinal_front_door.py -k patient`: 11 passed.
P1 initial ingestion and retry tests: 40 passed.
Final combined regression and acceptance run: **372 passed, 4 failed**. All four
failures are the deliberately preserved front-door checks: patient-1/2/4 old
yield expectations and patient-1's minimum generated walk length. The safeguard
and related regression subset passes (355 tests); no assertion was weakened.
JavaScript syntax, whitespace validation, dangling-import scan (677 files), CI
shard enumeration and current PRD citation audit also passed.

The original patient-1 front-door assertions are intentionally not weakened.
They expose unresolved acceptance changes rather than treating a changed yield
as approved. New tests exercise real reference offsets, bounded reveals, no
current-admission discharge, custom gaps, header deduplication, role budgets,
unknown timing and both task/retry race orderings.

Local data inventory has no production database: baseline contains zero tables.
This proves no local production data was changed, not that production was audited.

## Out of scope / do not touch

No production re-ingestion, migration execution, deployment, fixture relocation,
external messages, live model spending or physician routing. Synced project
sources and unrelated onboarding work are untouched. The PRD's authored clinical
candidate answers are not hardcoded into runtime generation.

## Final independent audit

The auditor independently reproduced the fixture discrepancies, then verified
the fixes for retry/task races, stale sealed-key binding, custom-gap discharge
withholding, empty-chart handling, note roles and outcome narrative prioritization.
Final conclusion: all identified implementation defects addressed; the three
acceptance conflicts above still prevent production sign-off. The discharge now
appears in both bounded held-out narratives and ground-truth rationale.
