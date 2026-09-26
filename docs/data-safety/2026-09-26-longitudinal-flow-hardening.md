# Longitudinal physician workflow: second-pass corrections

## Scope and invariants

The follow-up audit exercises the whole assigned longitudinal workflow, including
an approved nephrology reviewer assigned cardiology points, a saved V4 picker,
old browser drafts, and four assigned points in a seven-point chart walk.

Changes affect the shared physician JavaScript bundle, new Asclepius submissions,
validation, and read-only trajectory progress. The same code applies to live and
sandbox realms. Outcome recovery metadata and draft self-score marks are stored
locally under account- and realm-specific keys; they contain task identifiers and
physician marks, not copied source charts. Existing clinical browser drafts are
retained when they contain newer edits than an accepted in-flight request.

No schema migration, historical backfill, task regeneration, routing change, or
deletion is included. Source charts, generated points, assignments, accepted
submissions, records, earnings, uploads and exports retain their identities and
contents. Team/community stores, blob roots, object storage, encryption keys,
agreements, email and external integrations are not changed. Production checks
are read-only; no physician answers or flags are submitted operationally.

## Corrected behavior

- Real longitudinal V5 submissions satisfy the provenance validator; static real
  cases remain V4 and synthetic cases cannot claim either real-data version.
- Submission and normalized expected trajectory are inserted in one transaction.
  A crash or failed audit event after acceptance cannot strand the prediction
  outside the outcome-scoring column. Failed writes do not return success.
- A physician can commit once per longitudinal decision. Concurrent requests and
  new-ID retries cannot overwrite a prediction after future evidence is visible.
  A conflict identifies the retained original; the browser resumes its outcome
  while retaining the attempted draft. Other physicians can still label a point.
- Outcome reveal and walk continuation work without an optional prediction and
  after an invalid-case flag. Outcome-fetch failures have an explicit retry and
  dashboard resume; refreshing preserves pending marks and notes.
- Delayed task, reveal, submission, flag and outcome responses cannot replace a
  different active workspace. Pending score saves disable competing controls;
  late cleanup cannot erase newer drafts or outcome marks.
- Solo progress never advertises an unauthorized next point or skips an unanswered
  gap. After the last assigned point, the physician sees an assignment-wait state;
  the seven-point trajectory is not falsely marked complete. Relay ordering and
  retired-point behavior retain their existing rules.
- Static cases, terminal points and unreconstructible legacy outcomes cannot be
  recorded as verified self-scores. Legacy reveal and scoring share the same
  chronology validation; sealed evidence remains inaccessible before commitment.

## Preservation and restore evidence

Local evidence is retained outside the repository under
`output/longitudinal-flow-hardening/`. The existing frozen preservation fixture
has 80 tables and 28 rows; its before/after inventory reports no lost IDs or
changed content. The backend auditor's separately populated fixture has 82 tables
and 27 rows; its frozen inventory and SQLite backup-API restore match exactly.
Booting that second restore additionally exercises an existing normalization of
the synthetic fixture user's organization (NULL to fallback); the boot diff is
retained separately and is not described as unchanged. It does not affect the
raw restore result or introduce a migration in this change.

The reported account and seven points were inspected on production with read-only
SQLite, query-only mode, and write/network guards. All seven case JSON hashes and
the parsed source-chart hash match the previous investigation. The first four
points remain assigned and the final three remain unassigned. The account had no
submissions for these points at inspection time.

These are scoped fixture-restore and runtime-preservation checks, not a new
full-production backup or disaster-recovery certification. No storage, backup,
key, email or upload subsystem is modified. Rollback is a code redeploy retaining
all database rows and local browser drafts; do not clear storage or roll a live
database back over newly accepted physician work.

## Verification

New regressions exercise fresh and resumed V4 drafts in the real shipped browser
bundle against isolated FastAPI endpoints, desktop and mobile widths, complete
grading with reasoning/rubric capture, with and without predictions, reveal and
submit outages, outcome-fetch retry after refresh, score recovery and retry,
four successive invalid flags, and clean stopping before unassigned points.

Backend and executed-JavaScript tests cover V5 export readiness, source/version
boundaries, concurrent and changed-ID duplicates, original-payload preservation,
failure after durable insertion, missing/reversed legacy chronology, terminal
outcomes, assignment gaps, cross-specialty assignments, relay continuation,
navigation races and preserving newer work during late responses. Fake-model
transport is used for isolated generation/grading; no real physician work or
vendor requests are created by these checks.

Independent reviewer `review_longitudinal_hardening` reproduced and verified
corrections for legacy outcome validation and atomic prediction retention, then
reviewed client cleanup and score-recovery races. All four findings were fixed and
independently confirmed; final review found no remaining actionable issues.
The affected backend suite passes 315 tests; the final client/V5 suite passes 172,
including seven added retention-race regressions. The reviewer independently
reran those 172 tests and six backend interruption/concurrency/outcome tests.
Browser results and release checks are recorded in the PR.
