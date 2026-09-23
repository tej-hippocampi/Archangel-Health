# Agreement enforcement and attestation evidence

Status: implementation and verification; production activation is a separate rollout.
Date: 2026-09-18. Initial audit: origin/main 321994b; implementation rebased onto d964fdb.
Source: “PRD - Agreement Enforcement and Attestation Evidence.md”, supplied by Tej.

## Design and invariant

When ASCLEPIUS_AGREEMENT_GATE is armed, all covered physician draw, review, QA,
annotation, and trajectory-score routes require the existing current-agreement
check. Eligibility, practice, first-run, assignment, and reviewer/QA authorization
remain enforced. Admin and mock agreement exemptions remain unchanged.

The three specifically requested continuation exceptions remain: POST
/api/asclepius/submissions, GET /api/asclepius/tasks/{task_id}, and POST
/api/asclepius/tasks/{task_id}/reveal. A physician whose signature is superseded
can still read, reveal, and submit the case already in hand.

This release preserves partial signup evidence and requires all seven actual
boolean consents at clinical signup completion. An explicit false value remains
false; a string, integer, or non-empty initials field is not consent.

### Corrections to the supplied specification

- Current main still had exactly three Python references: the gate definition,
  tasks/next, and tasks/available. The original six missing surfaces were confirmed.
- Eight additional routes are covered: direct environment run access, both review
  submissions, QA queue, QA bulk approval, QA decision, submission list, and
  submission detail. These also expose or accept physician work.
- The shared dependency chain executes eligibility/practice/first-run checks
  before consulting the flag. A disarmed agreement gate does **not** make these
  new dependency chains inert for previously ineligible callers.
- Wrapping the update in COALESCE alone was insufficient: missing attestations
  were serialized as the non-null string "{}". Missing input is now SQL NULL;
  explicit partial objects merge over existing keys within the write transaction.
- Both read-modify-write paths serialize their read and write with BEGIN IMMEDIATE.
  Corrupt existing evidence fails visibly instead of being silently replaced.
- The original claim that these changes establish universal buyer-record
  provenance is outside what the proposed wiring can prove. Existing exports
  do not require an agreement row/hash for every historical label. Later signing
  is not treated as evidence that existed when an earlier record was produced.
  A separate export-safeguard scope decision was requested from the owner.
- The three retained route exceptions do not themselves prove a prior draw.
  Known task IDs and assignments may still reach them, especially in open-pool
  deployments. Stronger proof of in-flight work requires a separate admission
  rule; this implementation does not silently revoke the specified exceptions.

## Implementation map

| Symbol | Verified current location |
|---|---|
| require_current_agreement | `require_current_agreement` at backend/routers/asclepius.py:3654 |
| next_double_label | `next_double_label` at backend/routers/asclepius_review.py:804 |
| next_review | `next_review` at backend/routers/asclepius_review.py:390 |
| next_review_pair | `next_review_pair` at backend/routers/asclepius_review.py:246 |
| submit_review | `submit_review` at backend/routers/asclepius_review.py:494 |
| submit_pair_review | `submit_pair_review` at backend/routers/asclepius_review.py:635 |
| annotation_queue | `annotation_queue` at backend/routers/asclepius_env.py:161 |
| get_run_for_annotation | `get_run_for_annotation` at backend/routers/asclepius_env.py:187 |
| annotate_environment | `annotate_environment` at backend/routers/asclepius_env.py:202 |
| trajectory_self_score | `trajectory_self_score` at backend/routers/asclepius.py:4779 |
| qa_queue | `qa_queue` at backend/routers/asclepius.py:5913 |
| qa_decision | `qa_decision` at backend/routers/asclepius.py:5942 |
| qa_approve_all | `qa_approve_all` at backend/routers/asclepius.py:5924 |
| list_submissions | `list_submissions` at backend/routers/asclepius.py:5857 |
| get_submission | `get_submission` at backend/routers/asclepius.py:5869 |
| save_asclepius_attestations | `save_asclepius_attestations` at backend/team_store.py:2223 |
| provision_user | `provision_user` at backend/asclepius/store.py:4066 |
| _attestations_complete | `_attestations_complete` at backend/routers/onboarding.py:70 |
| isRequired | `isRequired` at frontend/asclepius/agreement_gate.js:5 |
| `async renderAgreementView` | frontend/asclepius/asclepius.js:2739 |
| qaError | `qaError` at frontend/asclepius/admin_shell.js:2089 |

There are 16 covered route/method pairs, including the two original gated routes.
No endpoints are added or removed. Existing reviewer and QA guards remain alongside
the agreement dependency. No schema migration is introduced.

The portal, environment page, and QA console share one refusal classifier.
Review draws open the existing signing screen and resume review
after signing. Review submissions, environment annotations, trajectory scores, and QA notes offer
that same screen in a separate tab, preserving unsaved inputs in the original tab.
The portal entry is /asclepius#agreement (with /sandbox in that realm) and works before the flag is armed.
The onboarding bulk checkbox label now correctly says seven.

## Tests and acceptance

- Real HTTP requests exercise every covered route as unsigned, signed,
  superseded, disarmed, admin, and mock; all refusals preserve the existing 403,
  X-Asclepius-Agreement-Gate header, and sign_agreement action.
- Open case pools cannot bypass annotation enforcement.
- Route dependency-tree inspection discovers conventional /next,
  /annotation-queue, and /annotate paths, plus an explicit registry for the other
  work surfaces. Signed labelers still cannot take reviewer privileges.
- Supersession leaves the three specified continuation routes usable.
- Director and member finish reject each missing, false, string, or numeric
  attestation. Rejection creates no portal account or completion email.
- Partial posts, omitted/empty/partial re-onboarding, concurrent saves, explicit
  withdrawal, corrupt evidence, failed writes, and idempotent retries are covered.
- Browser tests use shipped UI and real local API routes to verify review
  signing/resumption, environment signing links, and unsaved annotation recovery.
- Updated old signup test fixtures supply the seven booleans the actual form has.
- Run focused tests, affected tests, full backend suite, JavaScript syntax,
  route baseline, data-change guard, dangling-import scan, and this PRD audit.
  Final results are recorded in the dated change record.

## Do not touch

The v1 legal text, CURRENT_VERSION, gate default, signed-agreement append-only
triggers, explicit render_pdf version argument, supersession ordering, admin/mock
exemptions, three continuation exceptions, and seven attestation wire names remain
unchanged. ci_shard.py continues discovering tests without manual registration.
No live stores, contributor emails, or Railway variables are changed by the branch.

## Rollout

1. Review this implementation and the dated data-safety record. Verify scoped live
   and sandbox backups, inventories, and isolated restore evidence before release.
2. Deploy code with the agreement flag unset/off. Verify ordinary signed and
   unsigned eligible accounts and the previously unguarded eligibility paths.
3. Verify /api/asclepius/me/agreement for a real unsigned account; use actual
   signature counts instead of assuming the entire roster remains unsigned.
4. Notify the intended contributor roster of the signing requirement and link to
   /asclepius#agreement. This branch includes a draft below; sending requires an
   explicit owner instruction. Allow the intended signing window.
5. Set ASCLEPIUS_AGREEMENT_GATE=1 in the intended Railway environment only after
   those rollout prerequisites are met. Verify unsigned refusals, signing, reviewer
   permissions, and a superseded in-flight continuation with controlled accounts.
6. Monitor agreement-gate denials and successful signatures separately for 48
   hours. A denial is not evidence that a physician subsequently signed.

Rollback: unset ASCLEPIUS_AGREEMENT_GATE to remove signature enforcement. It does
not remove the new eligibility dependency chains or the seven-checkbox finish
validation; revert this code release if either must be rolled back. Retain all
signature rows and all attestation evidence. No data restore is required merely
to disable the flag.

### Contributor notice draft — not sent

Subject: Please sign your Archangel Health contributor agreement

Before your next case, please read and sign the contributor agreement in your
Archangel Health workspace. It takes about a minute. Open your workspace, go to
Profile → Contributor agreement, or use the direct agreement link in this notice.

We will activate the requirement after the signing window communicated with this
notice. Your previous submissions remain saved. If you have trouble opening or
signing the agreement, reply to the sender for help.

Deployment operator: insert the verified portal base URL and activation date
before sending. Do not send placeholders or invent a production schedule.
