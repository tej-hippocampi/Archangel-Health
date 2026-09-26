# Longitudinal case draft version repair

## Problem and scope

Opening an assigned chart-walk point with a saved V4 picker preference created a
V4 browser draft even when the task endpoint returned V5. Resuming that draft
retained the wrong version. Both answer reveal and clinical-invalid flagging
were then correctly rejected by the server's provenance boundary.

New drafts use the served version. Existing drafts are repaired when either
version crosses the V4/V5 real-case boundary; pinned synthetic V1–V3 experiences
remain intact. V5 now uses the seamless evaluation controls, optional prediction
capture, and a longitudinal badge. No server validation is weakened.

## Design invariants

- Modified data: only the browser draft's `portal_version` metadata when it
  disagrees with the served real/static/longitudinal case type. Existing draft
  identifiers, clinical reasoning, citations, critique, rubric, elapsed time and
  stage survive. Normal autosave persists the repaired draft.
- Subsequent physician actions use the existing reveal/submission endpoints and
  store the correct server-validated V5 version. Neither this change nor its
  operational verification submits answers or flags on a physician's behalf.
- No schema migration, database backfill, deletion, routing change, source-chart
  edit, regeneration, or change to assignments, existing commits, submissions,
  records, earnings, uploads or exports. Live and sandbox follow the same client.
- No blob, object-storage, encryption-key, notification or external-service
  changes. Current source evidence was inspected with read-only SQLite and
  network/file-write guards; only the reported account and chart were queried.

## Tests and recovery

214 focused tests pass across portal UX, longitudinal version boundaries,
longitudinal UI, and evaluation UI. The tests execute the shipped JavaScript
draft/action functions and send the resulting fresh/resumed reveal and both
flagging payloads to the real FastAPI endpoints. They also check all draft
stages preserve physician fields and V1/V2 remain outside the seamless flow.
Existing backend rejection tests still refuse incorrectly claimed versions.

The frozen local preservation fixture was inventoried before and after; all 80
tables and 28 rows are preserved without changes. A SQLite backup API copy was
restored separately and compared to the same inventory with no differences.
This is local fixture evidence, not a claim of a new production backup or full
production disaster-recovery audit. Since this release contains no persisted
data migration, rollback is to redeploy the preceding frontend bundle; retain
all browser drafts and database rows. Do not clear local storage to recover.

Independent reviewer `audit_v4_draft_fix` found that V5 also needed the seamless
predicate and badge mapping. Both were corrected and independently re-reviewed.
Final result: no actionable findings; 214 tests, JavaScript syntax and diff
checks passed.

## Do not touch

Keep the original source chart, current assignments, existing physician commits,
and all generated decision points unchanged. Never inject a later or undated
glucose observation into the earlier clinical question.

## Reported laboratory question

The source chart and generated first decision point were compared read-only.
All parsed laboratory measurements dated through that point are represented in
the displayed case after matching equivalent analytes and numeric values.
Source duplicate panels have been consolidated. No glucose result or dated
glucose note exists through that decision cutoff. The earliest dated glucose
reference is 33 days later; several other references are undated. These are
not grounds to invent a glucose value or introduce future evidence into the
earlier decision. No source content was changed.
