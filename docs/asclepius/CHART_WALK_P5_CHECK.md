# Chart Walk — P5 sealed reveal checkpoint

Implements §6 P5 of the supplied Seven Points PRD on its own branch, following
the four commits recorded in [CHART_WALK_P4_CHECK.md](CHART_WALK_P4_CHECK.md).
The source PRD describes an older tree; the verified locations below refer to
this implementation.

## Design and invariants

- Each successful trajectory task stores a versioned private outcome envelope
  in generation JSON at creation. The planner captures the chart evidence before
  model gates run, with `index < offset <= until_offset`, rebased to the point.
  Admission/resolution timing matches case construction. The complete window
  retains evidence beyond a later task's note/lab budget.
- A missing, failed, retired or edited successor cannot replace the seal or
  extend its boundary. An original terminal point explicitly stores no outcome.
  Partial-generation counts follow the seals rather than generated length minus
  one; generating only a nonterminal point still yields one verifiable point.
- The reveal requires that physician's submission. The envelope is excluded
  from admin previews, blinded task responses and buyer generation provenance.
  Bounds, types, shape and calendar-date scans are checked before storage and
  again on read; corrupt present seals fail closed with a 409.
- Legacy tasks without a seal retain the existing next-stored-task fallback.
  They are not backfilled, and this checkpoint does not claim independent seals
  for those older rows.
- Navigation is separate from the sealed endpoint. Continue skips retired rows;
  relay physicians can continue only to a live assignment whose predecessors
  have been answered. Waiting for another physician is not completion. Terminal
  outcomes cannot be self-scored and retain a Continue button.
- Density remains 2 dates / 8 events / 2 resource types. Physician pay stays $75
  per submission for every point, with the declared specialty for the full walk.
  There is no buyer price field. Static planning does not create seals.
- No schema migration or deletion is needed. Existing generation JSON is the
  storage container; source charts and stored evidence are preserved.

## Current implementation locations

| Concern | Verified location |
| --- | --- |
| Chart window capture | `seal_outcome_window` — `backend/asclepius/real_cases.py:1558` |
| Envelope validation | `validate_sealed_outcome` — `backend/asclepius/sealed_outcomes.py:8` |
| Post-submission reveal | `trajectory_outcome` — `backend/routers/asclepius.py:4400` |
| Terminal score gate | `trajectory_self_score` — `backend/routers/asclepius.py:4516` |
| Live continuation | `evaluator_trajectory_progress` — `backend/asclepius/store.py:7125` |
| Buyer provenance filter | `_generation_provenance` — `backend/asclepius/packaging.py:251` |

## Tests

`test_chart_walk_sealed_outcomes.py` exercises all timed collections, a 30-note
window, strict corruption rejection, failed middle and final generation,
retirement plus later-case editing, partial walks, post-submit access, export
profile privacy, terminal score refusal and relay continuation. The executed
physician UI regression checks a terminal reveal with a stored prediction: it
offers Continue without a scoring card.

Fixture-door acceptance still asserts patient-4 has 7 encounters, 7 points
(3 decision, 4 interval), 7 generatable, 6 verifiable and 0 held. Model legs use
deterministic stubs; ingestion, planning, storage, routing and export are real.
The all-chart before/after table remains in the P4 checkpoint.

Independent auditor: no remaining blocking findings; 50 relevant tests passed.
The legacy fallback limitation is intentional and was included in the audit.

Final builder regression output (20 relevant test files, including CI sharding):

```text
389 passed, 5207 warnings in 70.04s (0:01:10)
```

All 34 points pass case, prompt, held-out-key and sealed-reveal date scans plus
temporal-split assertions. On identical baseline inputs, patient-1 and patient-3
static dictionaries remain exactly equal: 22/9 and 5/4 encounters/decisions.
Both JS bundles pass syntax checks. The 11 current P4/P5 implementation citations
pass the PRD audit. Inventory reports no persistent local database and no IDs
lost; database mutation tests use temporary stores, including retained retired
rows and unchanged source payloads.

## Out of scope / do not touch

No density changes, store deletions, legacy-seal backfill, price or specialty
policy change, deployment or merge. P5 is a separate commit and PR based on the
P1–P4 dependency branch so its review diff contains only this stage.
