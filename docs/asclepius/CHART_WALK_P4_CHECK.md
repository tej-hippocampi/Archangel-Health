# Chart Walk — P1–P4 acceptance checkpoint

Implements the supplied Chart Walk Seven Points PRD through P4. The source PRD
describes the pre-change tree; the verified implementation locations below refer
to this checkout. P5's independently sealed reveal is still pending.

## Design and invariants

- Two point classes: density-qualified encounters with presenting narrative are
  decisions; other eligible observation visits are intervals. Missing narrative
  causes a documented downgrade, with no predecessor hold cascade.
- Density thresholds remain 2 distinct dates, 8 events and 2 resource types.
- The declared specialty applies to the full walk. Physician pay remains $75
  per submission for both classes. No buyer price field is introduced.
- Class, narrative and downgrade metadata travel in each record's trajectory
  annex and at case level. Legacy missing classes remain null.
- Export counts represent distinct shipped points after profile filtering.
  Multiple record types or labels do not inflate the trajectory length.
- Chart-walk buyer files omit operational timestamps and monetary fields.
  Dated taxonomy/configuration identifiers become stable SHA-256 identifiers;
  original payloads and internal audit timestamps remain stored. A calendar-dated
  license expiry is rejected without changing its terms. The complete text bundle
  passes the date scan before any record is marked exported.
- No schema migration or store deletion is included. Inventory before/after:
  no local persistent database, no IDs lost. Tests use temporary stores.

## Measured before / after

Before is main at `22f8d7e`; after is the P4 fixture-door acceptance plan.
Ready counts precede model generation gates. Model legs in HTTP acceptance use
deterministic stubs; ingestion, planning, storage, routing and export are real.

| Chart | Encounters before → after | Decision points before → after | Interval after | Ready before → after | Verifiable walk points after | Held before → after |
| --- | --- | --- | --- | --- | --- | --- |
| patient-1 | 22 → 22 | 9 → 2 | 20 | 1 → 22 | 21 | 8 → 0 |
| patient-3 | 5 → 5 | 4 → 3 | 2 | 1 → 5 | 4 | 3 → 0 |
| patient-4 | 8 → 7 | 3 → 3 | 4 | 1 → 7 | 6 | 2 → 0 |

Patient-1 and patient-3 static plans compare exactly to the baseline on identical
ingested inputs: encounters/decisions remain 22/9 and 5/4. Fresh ingestion excludes
synopses, so only the static curation count of withheld untimed notes changes
(25→22 and 4→2); all other static fields match. Patient-3's actual baseline static
generatable count is 3, which is preserved; the source PRD's claim of 4 was stale.

## Current implementation locations

| Concern | Verified location |
| --- | --- |
| Planner | `plan_cases` — `backend/asclepius/real_cases.py:2225` |
| Conservative sentence boundary | `_leading_presentation` — `backend/asclepius/real_cases.py:192` |
| Record annex | `trajectory_block` — `backend/asclepius/packaging.py:1095` |
| Distinct shipped counts | `_walk_point_counts` — `backend/asclepius/export.py:791` |
| Export pipeline | `build_export` — `backend/asclepius/export.py:1799` |

## Tests

- `test_chart_walk_units.py`: real discharge A's two-sentence history; discharge
  B and its order-only continuation; misdated day −1429 drug page versus a genuine
  two-year-old consult; unitless INR; aggregate 37 notes / 28 dated; zero synopsis
  notes; trailing sparse interval exclusion; terminal downgraded interval.
- `test_chart_walk_export.py`: real packager output, class/downgrade metadata,
  distinct counts, legacy nulls, filtered buyer aliases, rubric export, complete
  date/monetary scans, content hashes, source-payload preservation, download
  fallback, and rejected exports preserving record/submission state.
- `test_chart_walk_acceptance.py`: fixture-door counts for all three charts;
  patient-4 generates seven assigned-only single-label tasks with dense indices;
  batch reads 1 trajectory / 7 points / 7 unrouted; solo unlocks sequentially;
  relay preview seed 7 commits rotation [2,0,1,2,0,1,2]; first admission prompt and
  34-day bounded reveal; seven-point export with 3 decision and 4 interval points.
- Existing front-door, patient-4, longitudinal generation/routing, export/case
  export, exclusivity, multimodal export and CI-sharding tests remain required.
- Independent auditor: 143 relevant tests passed, with no remaining blocker.
  The final static-profile alias regression also passed independently.

Final builder output (main regression suite, then final export/acceptance checks):

```text
260 passed, 4769 warnings in 51.65s
12 passed, 418 warnings in 11.38s
```

All 34 planned points also pass temporal-split and date scans of their cases,
rendered prompts, held-out keys and adjacent outcome deltas. The static comparison
uses the identical baseline input, and all five implementation citations pass
the repository PRD audit.

The exact discharge-A unit exposed a semicolon boundary that admitted a fragment
of a third sentence. P4 keeps that sentence intact, so its treatment language
withholds it. This correction is used only by trajectory discharge splitting.

## Out of scope / do not touch

P5 must persist the sealed outcome independently of the next task, on its own
branch/PR. P4 does not claim reveal invariance after generation failure or
successor retirement. Density constants, stored source data, pay policy and
declared specialty rules are unchanged. No push, deployment or P5 work is included.
