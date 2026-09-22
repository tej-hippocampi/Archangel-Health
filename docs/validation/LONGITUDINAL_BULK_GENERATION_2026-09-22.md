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

## Production inspection and additional fixes

After explicit user approval, a temporary Railway SSH key was registered for
read-only inspection and removed afterward. SQLite connections used `mode=ro`
and `query_only`; the deterministic replay additionally rejected network calls,
file writes and writable database connections. No live records were modified.

The bulk request continued until 05:23:46 UTC and persisted two points out of
seven, with three quality rejections and two failures. A concurrent single-row
request produced a separate one-point walk. This explains both the partial walk
and the independently numbered case in the screenshots. The source chart still
plans seven points, all passing content, temporal and leakage checks, with six
verifiable outcomes.

The two failed candidate calls each emitted exactly 2,000 tokens and were not
followed by judges. Their configured budget is 2,000. Truncation is strongly
supported but not proven because historic telemetry omitted stop reasons and
did not retain raw output. Individual scores for the three rejected points are
unavailable; aggregate rejection counts do not authorize bypassing quality gates.

The additional fix retries an incomplete or invalid candidate pair once with a
larger budget, validates answer text and source-ID mapping, and rejects even
parseable output when the provider reports truncation. OpenAI completion status
is preserved in both adapters, and future telemetry includes stop reasons.
Questions, fallback questions and the real-chart judge now use interval context
and the actual clinical question. All numeric quality floors remain unchanged.
Jobs retain structured failure scores; internal failure events retain the judge
explanation without adding raw clinical text to polling diagnostics.

The repeated frozen-fixture preservation bracket is `verified4-before.json` and
`verified4.db`, with backup `verified4-restore-copy.db`. Seven linked points and
six verifiable outcomes were generated; the inventory found no lost IDs or
changed baselined fields/files beyond the same three intended ingest-case fields.
These are deterministic local model tests, not proof of live model acceptance.

Current Railway backup metadata confirms a daily snapshot at 16:57 UTC on
22 September, daily/weekly schedules, and approximately 273 MB used of 5 GB.
The earlier isolated recovery drill remains separate operational evidence.

These tests do not certify live model output, production backup/restore readiness,
or completion of the user's live chart. Those require operational verification.
