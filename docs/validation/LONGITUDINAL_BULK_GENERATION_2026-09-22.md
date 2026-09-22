# Longitudinal bulk generation

The manual generation endpoint performed all model calls inside one HTTP request.
The per-encounter buttons created independent trajectory IDs, and promoting the
first point made the UI treat the entire chart as read-only. These paths could
leave an operator with an upstream timeout and disconnected one-point walks.

The admin controls now submit a durable background job and poll progress. Every
longitudinal generation control requests the complete selected chart walk. A
repeated request finds the same job, retries resume saved tasks, and a failed
point pauses the walk with its error visible. Allocation, relay, and reassignment
require the job to complete. Existing singleton walks are retained unchanged;
building a complete walk from a legacy chart creates a separate complete walk.

## Data and recovery scope

- Additive `real_case_generation_jobs` table in the realm's Asclepius database.
  Existing tables/columns/IDs are retained. No production data was modified by
  the local implementation or validation.
- Source charts, sealed outcomes, submissions, assignments, exports and earnings
  retain their existing contracts. Model and content gates still run for each
  generated point. Incomplete longitudinal tasks remain `assigned_only`.
- Request identity includes the source chart and normalized generation settings.
  Plan fingerprints additionally prevent resumed jobs from mixing planner
  versions. Task IDs derive from job ID and encounter index; insert transactions
  verify the current worker lease. Completed evidence is never overwritten.
- Jobs checkpoint after each task. Recovery handles both task-before-checkpoint
  crashes and notification-before-completion crashes. The notification batch ID
  is stable. Worker startup resumes queued/expired jobs in the correct realm.
- Intentional fixture changes: `ingest_cases.status`, `.task_id`, and `.updated_at`
  when a task is inserted; new tasks, jobs and audit events. Source clinical
  chart bytes and relationships are preserved.

## Preservation evidence

Validation artifacts are in the workspace's `output/longitudinal-bulk-generation/`.
The frozen patient-4 fixture contains seven encounters, three decision points,
four interval visits and six verifiable outcomes. It was generated with the
repository's fake model transport through the real pipeline, without replacing
the content or quality gates.

The first inventory found two pre-existing bootstrap backfills (`uploads`'s
health-system link and a user's organization). Replaying the unmodified base
store from `a6fe5b3` reproduced exactly those same changes. They were investigated,
not allowlisted. The verified bracket starts after that baseline migration:

- `verified3-before.json`: 79 tables, 11 rows, plus the encrypted original upload in an isolated blob root.
- `verified3-after.json`: 80 tables, 28 rows.
- Inventory diff: **no IDs lost; all baselined content and files preserved**,
  allowing only the three intentional ingest-case fields above.
- `verified3-restore-copy.db` is a consistent SQLite backup of that frozen
  pre-change fixture. The original encrypted upload was copied with its checksum verified and its fixture path remapped before the snapshot. It is local test recovery evidence, not a live backup.

For rollback, revert the application code while retaining the additive jobs
table and every generated task. Keep queued jobs for a subsequent fixed release;
do not delete tasks, restore over production, or silently recreate saved points.

## Verification

- Initial focused regression: 203 passed.
- Final job and JavaScript behavior tests: 27 passed, covering early acceptance,
  all seven linked points, six verifiable outcomes, duplicates, retries, source
  purpose gates, last-point crashes, stale leases and planner changes.
- No dangling imports; data-change guard and merge-readiness pass.
- Route snapshot adds the admin-only job status endpoint.
- Broader affected suite: **3,972 passed, 23 skipped** in 913.88 seconds.
- Hosted CI status is recorded in the PR.

## Independent audit

Fresh-context auditor reviewed the implementation against
`a6fe5b3393e5ad3ec5e7da048caaec7822598689`; reviewed-content manifest SHA-256:
`c9f045a153cd06e93343067b278b3cbc488199dbb70f3fc4bd831099be432319`.

No outstanding actionable findings. Independent temporary-database checks proved
that planner drift leaves saved points unchanged, all three routing paths reject
incomplete walks with zero assignments, and notification-boundary recovery
completes 7/7 with seven generation calls and one unchanged outbox row.

Read-only Railway diagnostics found the earlier preview request returning 200, but no recorded upstream error for the attempted bulk generation. SQL inspection is unavailable without a registered SSH key, so the two live task rows have not been examined.

These tests do not certify live model output, production backup/restore readiness,
or completion of the user's live chart. Those require operational verification.
