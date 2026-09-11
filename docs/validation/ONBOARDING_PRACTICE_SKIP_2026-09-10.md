# Practice-only onboarding skip - change record, 2026-09-10

## Behavior and scope

An approved physician's button now reads **Skip practice case walkthrough**. It records only the practice opt-out, then continues to Community. Payouts and the manual remain interactive and unfinished sections remain available across refreshes and sign-ins. Completing a practice case within an existing welcome package also resumes onboarding; standalone practice does not enroll an account into a new welcome package. Help-menu replays still return to the dashboard.

The source of truth is the caller's Asclepius `users.first_run_json` in the current realm. The new `practice_skipped_at` timestamp distinguishes an explicit opt-out from a completed or passed case. Existing stop outcomes, timestamps, session bookkeeping and tutorial JSON survive the skip. Existing global dismissals are preserved; this change does not automatically reopen previously dismissed accounts because that timestamp also represents legitimate historic dismissals.

The dedicated `skip_practice` action requires an approved account and cannot target another stop. Duplicate skips retain the original timestamp. No schema migration, bulk update, credential, clinical record, submission, earning, uploaded original, contract, external service, encryption key or blob tree changes. Live and sandbox production databases were not accessed or modified. No production emails were sent.

## Preservation and restore evidence

`test_approved_doctor_can_skip_practice_without_dismissing_other_stops` takes a frozen before inventory, uses SQLite's backup API, calls the real route, repeats the request and authenticates again, then compares the after inventory. Every table and existing row/column hash is checked; only `users.first_run_json` is allowed to change. New audit event rows are additive. It restores the backup into a separate database with SQLite's backup API and compares against the original inventory.

- Inventory taken: 2026-09-11T05:10:11.084219+00:00 (before), 2026-09-11T05:10:11.107126+00:00 (after).
- Scope: 71 application tables, 1 existing rows in an isolated synthetic Asclepius database.
- Before manifest SHA-256: `afe535294ce75808be74a9ff85980f6a4f74476b2849a73ef5431b0e26441d1f`.
- After manifest SHA-256: `18ea7b99ea0cd6611164742a85267b1b8dda84aad4072d33b1fc000d081d5d0b`.
- Unexpected changes/missing identities, columns or field hashes: none.
- Isolated restore differences: none.
- Local backup: `/private/var/folders/yq/llwsy6gj12l482bt66x88htw0000gn/T/pytest-of-tejpatel/pytest-159/test_approved_doctor_can_skip_0/before.db`.
- Local restored database: `/private/var/folders/yq/llwsy6gj12l482bt66x88htw0000gn/T/pytest-of-tejpatel/pytest-159/test_approved_doctor_can_skip_0/restored.db`.

The nonempty fixture contains synthetic users and seeded application data; this evidence applies only to that fixture and is not evidence of live backup coverage. Manifests and restore results were retained in the local `onboarding-practice-validation` output directory. Reproduce them by setting `ONBOARDING_SKIP_EVIDENCE_DIR` when running the named test. No live restore or broad reset is part of this change. Rollback is a code revert; retain the original stored JSON and its timestamps rather than deleting data. Older code ignores the new timestamp and may ask for the practice walkthrough again.

## Failure and behavior checks

- 247 focused API/model/DOM tests passed across practice gates, tutorial, welcome package, first-run UI, applicant home and onboarding.
- Two Chromium cases passed against the shipped portal and real local API: skip from the welcome overlay and midway through a case, 503 retry, unchanged tutorial state, retained draft, Community handoff, payouts/manual interaction, refresh, and a fresh sign-in.
- A failed database write preserves the original checklist and remains retryable. A failed welcome skip retains its button; success removes all tutorial overlays before the next screen.
- Completed cases retain completion; a skip never fabricates a pass. All remaining stops can complete after skipping practice. Python and JavaScript cadence agree with and without the opt-out timestamp.
- Route baseline unchanged (671 routes); no dangling imports (706 files scanned); JavaScript syntax, SQL data-change guard, diff whitespace and merge readiness passed.
- File intake, external delivery, disk-capacity and encryption changes are outside this patch; no such operation is added.

## Independent review and release

The independent auditor found and verified fixes for standalone practice unexpectedly enrolling a contributor, and a failed welcome skip removing its own retry button. Final review found no remaining actionable issues, confirmed overlay cleanup is idempotent, and checked legacy dismissal, saved-state and queue preservation. The auditor reviewed the final 247-test and two-browser results plus inventory/restore evidence.

Ready for PR review. CI and deployment remain separate; no production state or deployment was changed by this task.
