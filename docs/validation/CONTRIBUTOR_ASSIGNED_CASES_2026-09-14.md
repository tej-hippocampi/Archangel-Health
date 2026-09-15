# Contributor case access — 14 September 2026

Implemented on `fix/contributor-assigned-cases` from main `b02a818`. This record
describes local validation, not a production deployment or authorization to merge.

## Product behavior

Completing approval and welcome onboarding grants access to the product. It does
not assign casework. An approved physician without assigned work sees:

> You're all set.
>
> No cases to label or review just yet. We'll notify you when a case is ready for you.

Practice, the guide and community remain available. Once an admin routes an
eligible case, the existing Start/Continue experience returns. Named assignments
take precedence over specialty and difficulty preferences. Real-data approval,
ingest holds, case capacity, independent labeling and longitudinal sequence gates
still apply. An API failure is shown as an error, not as an empty queue.

At the owner's request, the waiting card has one **Open the practice case**
button and no Refresh action. Its compact padding, small status icon and
left-aligned text use scoped styles shared by home and the empty labeling view.
Other error-retry controls retain their existing behavior.

The assignment requirement covers synthetic and real cases, both labeling draws,
direct case/reveal/answer/prelabel/submission routes, chart images, single and
paired review, legacy tiered QA, environment annotations and new paid review
sessions. Queue permissions are filtered before scan limits. Onboarding never
seeds or generates new work. Review counts describe this physician's eligible
work. A pending single review displays that it is being prepared in the existing
paired-review interface; this change does not add a new single-review UI.

The default is `ASCLEPIUS_OPEN_CASE_POOL_ENABLED=0` in both realms. Only the exact
value `1` opts into the former open-pool behavior. Keep this unset or `0` for
contributor launch. The admin's publish-to-all request is rejected under the new
default; routing to named people, a specialty, or the existing allocator continues.
Mock accounts and staff preview access remain available and unbillable.

Existing non-examination independent answers can be continued after rerouting.
Active review claims retain access until their configured lease expires, including
chart images at later trajectory points and images shared between cases. A future
review assignment alone does not bypass the sealed chart sequence. Examination
answers never grant paid work: new commits carry a server-owned purpose, and
tutorial restart/reset preserves the examination identity. Old untagged commits
without sufficient approval/exam provenance remain stored but conservatively
require explicit reassignment. Existing paid sessions can resume and close.

## Data preservation record

- Affected stores: realm-scoped Asclepius live and sandbox. Existing tasks,
  assignments, independent commits, submissions, reviews and work sessions are
  read to decide eligibility. Successful, authorized work continues through its
  existing write paths. No schema migration, deletion, retention change or bulk
  rewrite is introduced. Existing IDs, chart/source relationships, annotations,
  financial records and immutable submitted payloads remain intact.
- Intentional new-write differences: independent answer payloads receive a
  server-owned `purpose`; tutorial updates retain their existing exam stamp.
  Denied work does not create a submission, review claim or new paid session.
  Review claim acquisition still uses an atomic conditional update.
- Team, community, uploads, original documents and clinical blob trees are not
  migrated or rewritten. Image access changes authorization, not stored bytes.
  No new external integration, object bucket, encryption key or upload session
  is introduced. Existing assignment notifications are retained; no real email
  was sent in this task.
- Frozen synthetic fixtures: 72 tables, three populated rows (user, task,
  assignment), and one original file per realm. Original version-2 inventories
  and SQLite backup copies were preserved. The first startup diff detected the
  pre-existing normalization of a missing `users.organization`. Restarting the
  unchanged main implementation in a separate clone reproduced the exact same
  field change. Candidate startup matched main startup in every identity and
  field; every original file hash matched. No broad change allowance was used.
- SQLite backup included committed WAL. Each backup restored to an isolated
  database and file tree and matched its frozen original before boot. Candidate
  boot on the restored copy matched baseline-main boot. Zero missing originals
  and zero unexpected field changes. These small synthetic fixtures establish
  only their stated scope, not production-wide preservation or durability.
- Reproducible local evidence in the parent project:
  `output/contributor-assigned-cases/preservation_check.py` and
  `output/contributor-assigned-cases/preservation/` (original before inventories,
  backups, baseline-main startup, candidate startup, restore manifests and
  `results.json`). Regression tests separately exercise submission/review records,
  exam provenance, assignment expiration/revocation, active work and retries.
- Relevant failure paths: invalid/expired/revoked/wrong-role assignments,
  atomic claim failure, direct URL/POST attempts, repeated submission, exam
  reset/reapproval, failed queue requests, expired review leases, shared images
  and assignments outside the first scan window. Storage commit protocols are
  unchanged; this feature does not claim new disk-full/process-death coverage.
- Rollback: revert the application revision while retaining every data file and
  added payload field. No reverse migration or restore over production is needed.
  Reverting or setting the legacy flag restores open-pool access, so keep new
  contributor work paused if that contradicts the launch policy.

## Validation

- New default-mode API/access suite: **60 passed**.
- Independent auditor: **70 passed**, including both access modules and SQL
  placeholder checks; no outstanding actionable findings.
- Browser coverage: **12 passed**, including desktop/mobile empty states,
  welcome completion, practice/guide/community access, assigned Start/Continue,
  assigned review cards, pending single review and queue failures.
- Frontend/DOM follow-up after the waiting-card refinement: **252 passed**.
  All **12 browser scenarios** were rerun, including both practice buttons and
  confirmation that waiting cards contain no Refresh action. Desktop and mobile
  screenshots were regenerated and visually inspected.
- Review, routing and payment follow-up: **165 passed**.
- CI discovery: **21 passed**; both new test files are auto-discovered.
- Broader affected suite: **3,492 passed, 1 skipped**, across 161 test files
  in 440.02 seconds. Includes the 60 new default-mode tests above.
- Dangling-import scan: **716 files, clear**. Data-change guard: clear.
- Route table unchanged: **675 routes**. Merge-readiness: clear, zero commits
  behind freshly fetched main. `git diff --check`: clear.

The historical open-pool tests explicitly retain that deployment mode through
`tests/conftest.py`. The two new access modules clear the opt-in, and the new
browser scenarios use the shipped assignment-required default. Broad-suite
success alone must not be described as testing every old scenario in default mode.

Independent audit report, after the final image correction:

> No outstanding actionable findings in the reviewed diff. Verified assignment
> enforcement across labeling queues and direct routes, QA, single and paired
> review, environment annotations, and new paid sessions. Confirmed exam resets
> cannot convert exam answers into paid work; explicit assignments remain visible
> across specialty and difficulty preferences. The final image fix correctly
> preserves access through a live review claim, including shared assets and later
> chart points, and denies access after the configured claim lease expires.

The auditor separately confirmed that the frozen backup exactly matches each
original inventory, the baseline source is exactly `b02a818`, and candidate and
restored-candidate startup match unchanged-main startup without a field allowance.
The final waiting-card refinement was independently reviewed as well: no
actionable findings; syntax and diff checks passed. Its scoped markup/styles
preserve timer cleanup, practice replay and existing error retries.

## Release boundary

No production configuration, database, deployment or live contributor account
was changed by this task. The earlier recovery record
`archangel-production-20260914-recovery-01` verified the captured production scope
and the prior PR's restore; it is not a fresh restore test of this revision.
This task does not re-clear the operational alerting and actual email-provider
evidence requirements in `docs/data-safety/POLICY.md`. Keep the PR in draft pending
that existing release decision; do not equate local tests with deployment approval.
