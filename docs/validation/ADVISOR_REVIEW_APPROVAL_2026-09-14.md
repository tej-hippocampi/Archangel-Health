# Advisor reviewer approval — 14 September 2026

Owner request: advisor-link applicants enter the human decision queue with
Reviewer proposed. The admin may approve as Reviewer or reject platform access.
Existing undecided advisor accounts must enter the same queue without approval.

## Preservation and intended changes

The Asclepius users table is the source of truth in each realm. Preserve user
IDs, passwords, advisor origin, credentials, attestations, decisions, exam
attempts, submissions, earnings, referrals and audit history. No blob, team or
community record is migrated. Existing approved/rejected decisions are retained.

The migration changes only undecided advisor verification_status to pending.
It clears an accidental labeler tier only when tier_assigned_by identifies the
automatic tier backfill; the migration source and timestamp are retained as
evidence. New advisor accounts must commit their pending state with creation.
Human approval writes reviewer and its attribution; rejection retains evidence
and denies access, without the physician exam-retake behavior. Advisor origin
remains recorded after approval. No automatic verification job may approve it.

Approval may queue the existing welcome and community introduction. Rejection
queues a notice that does not promise continuing access or an exam retake.
Pending signup notices must not promise access to an examination. Referrer
accounts and the regular physician workflow retain their existing behavior.

## Validation and release record

Local validation completed on 14 September 2026:

- 876 backend tests passed: 89 signup/access/decision/capability checks, 708
  onboarding/auth/community/model/admin regressions, and 4 tests executing the
  actual admin module's advisor proposal, approval, rejection and legacy repair,
  plus 75 advisor-access and engineering-harness checks.
- 47 landing component tests passed, including advisor confirmation without an
  examination prompt and the unchanged physician examination handoff.
- Production landing build and syntax checks for both portal JavaScript files
  passed. The build reports the existing large-chunk warning.
- Data-change SQL guard passed; 710 files have no dangling imports; all 672
  routes are unchanged. Merge readiness is clear against main fced9c556.

Full CI initially identified seven assertions for the superseded permanent
view-only advisor policy and stale PRD line references. Those tests now enforce
pending denial, explicit Reviewer approval and rejection, including actual
community reads/posts. The cited lines were updated in the PRDs. Product code
was unchanged during this CI correction; the complete 75-test access/harness
group passes locally, including the PRD audit.

The nonempty frozen preservation fixture contains advisor and physician user
rows, credentials/attestations, an examination response, a submitted annotation,
its source task and a source file. Inventory comparison allows only the intended
users.verification_status and users.tier changes. No IDs, other field values,
files or hashes disappeared or changed. Repeating startup produces no further
changes. Separate tests preserve prior approval/rejection decisions exactly.

At 2026-09-14 05:27 UTC, the SQLite backup API captured committed data; restoring
that backup to an isolated database reproduced the entire before inventory.
Local evidence is retained outside the worktree at
`../advisor-review-validation/preservation/test_legacy_advisor_migration_0/`:
`before.json`, `after.json`, `backup.db`, `restored.db`, `restored.json`, and the
source file. No encryption keys or external providers are required by this
synthetic drill. This evidence does not establish production backup coverage.

Failure/access tests cover rollback of account creation with pending state,
existing-token rejection, open WebSocket revocation, old retake stamps, alternate
approval/retier/restore entry points, automatic verification enabled, and clinical
nudge suppression. Advisor decisions create no physician-model training rows.

Independent auditor: `advisor_implementation_audit`, fresh-context read-only
review. Report: “Independent audit complete; no unresolved substantive findings
in the current diff.” The auditor independently reproduced and confirmed fixes
for physician reminders, QA-role community access, already-open WebSockets,
passwordless examination prompts, alternate tier changes and roster account kind.
They ran all 21 advisor API/preservation tests successfully and reconfirmed the
final diff after the DOM tests and obsolete-copy cleanup. No live access or
repository edits were performed by the auditor.

Production data is not modified during development. Live backup, off-volume
restore, provider acceptance/bounce checks and deployment remain unverified;
local preservation tests cannot establish those operational guarantees. Rollback
must preserve the recorded decisions and pending applications; restoring an old
database over newer writes is not an acceptable rollback.

Release is held pending the applicable live operational gates or an explicit
owner exception for this advisor workflow change. The earlier join-link hotfix
exception covered a separate already-released change. Production approval of any
individual advisor is a separate owner decision and is never part of deployment.

Rollback: stop this rollout and deploy a forward correction to the application
code while retaining user rows and decisions. Do not revert the pending backfill
to NULL, clear verified_by/verified_at, or restore an older database over newer
applications. Retain the advisor access gates during any rollback so rejected
accounts do not regain access. Inspect the waiting queue and an isolated advisor
account after release; do not approve a real applicant as a deployment test.
