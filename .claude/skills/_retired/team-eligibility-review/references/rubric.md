# TEAM Eligibility Rubric — the 6 checks

Source of truth for verdict semantics: `backend/eligibility/evaluate.py`
(`CHECK_CRITERIA`, `CHECK_RECOMMENDED_ACTIONS`, `evaluate()`). This file
documents the same rubric for review workflows; keep them in sync.

Overall verdict: ELIGIBLE if all six checks PASS; INELIGIBLE if any check
FAILs; BLOCKED_UNKNOWN otherwise (PRD §7.3).

## 1. `partA_active` — Part A active

- **Criterion:** Medicare Part A (Hospital Insurance) is active on the surgery
  date — effective on or before that date, and not terminated before it.
- **PASS:** status ACTIVE, effective ≤ surgery date, no termination before the
  surgery date. Termination date == surgery date counts as active THROUGH that
  day (PRD §11.8).
- **FAIL:** status INACTIVE, effective date after surgery date, or terminated
  before surgery date.
- **UNKNOWN:** no Part A information in the documents.
- **Recommended action on FAIL/UNKNOWN:** request a current X12 271 or payer
  portal printout showing Part A entitlement dates; confirm with the payer
  before overriding.

## 2. `partB_active` — Part B active

Same rubric as Part A, for Part B (Medical Insurance; X12 EB03 == "MB").

## 3. `not_ma` — Original Medicare (not Medicare Advantage)

- **Criterion:** patient is on Original (FFS) Medicare, not enrolled in a
  Part C (MA/MAPD) plan on the surgery date.
- **PASS:** extraction reports `enrolled: NO`.
- **FAIL:** `enrolled: YES` — MA contract ID (H/R/E prefix) or named MA plan
  covering the surgery date.
- **UNKNOWN:** enrollment not addressed.
- **Recommended action:** confirm via payer portal or a 271 with plan-level
  detail; Part C contract IDs starting with H, R, or E indicate an MA plan.

## 4. `medicare_primary` — Medicare primary payer

- **Criterion:** Medicare is the primary payer on the surgery date (no MSP
  arrangement placing another payer first).
- **PASS:** `isPrimary: YES`. **FAIL:** `isPrimary: NO` (working aged,
  workers' comp, etc. — see `secondaryReason`). **UNKNOWN:** MSP not addressed.
- **Recommended action:** verify MSP status with the payer before overriding.

## 5. `not_esrd_basis` — Not ESRD-basis

- **Criterion:** the Medicare entitlement basis is age or disability — not
  End-Stage Renal Disease. A comorbid kidney-disease diagnosis does NOT make
  the basis ESRD; basis is a legal entitlement category.
- **PASS:** `isESRDBasis: NO`. **FAIL:** `isESRDBasis: YES`.
  **UNKNOWN:** basis not stated.
- **Recommended action:** confirm the entitlement basis on the patient's
  Medicare record.

## 6. `not_umwa` — Not UMWA

- **Criterion:** patient is not covered by the United Mine Workers of America
  Health Plan.
- **PASS:** `isUMWA: NO` (preferred over UNKNOWN when no UMWA mention exists —
  UMWA coverage is rare and explicitly listed).
- **FAIL:** `isUMWA: YES`. **UNKNOWN:** only when a payer entry partially
  suggests mine-industry coverage.
- **Recommended action:** confirm with the patient or payer.

## Rationale entry shape

`build_rationale()` returns one entry per check:

```json
{
  "key": "partA_active",
  "label": "Part A active",
  "verdict": "PASS",
  "criterion": "...",
  "reasoning": "Part A is ACTIVE, effective 2024-01-01, ... covering the surgery date 2026-07-15.",
  "evidence": {"sourceExcerpt": "...", "values": {"status": "ACTIVE", "effectiveDate": "2024-01-01"}},
  "override": null,
  "recommendedAction": null
}
```

Overridden checks carry `override: {to, reason, actor, ts, originalVerdict}`
and render with an "overridden" badge in `frontend/doctor.html`.
