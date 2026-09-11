# Independent ingestion audit review

10 September 2026. Review scope: the local `fix/data-ingestion-durability`
branch. Reviewers inspected code and ran isolated synthetic probes; they did not
access production records, send real email, or change the implementation.

## Upload, media and preservation inventory

The upload reviewer confirmed durable original/part writes, retained interrupted
uploads, atomic link/session receipts, post-commit recovery, and scoped receipt
pagination. The review found that an incomplete inventory baseline could falsely
pass; the builder made legacy and incomplete baselines fail closed. Final review
confirmed complete per-row field hashes, valid digests and consistent identities.
Three focused inventory regressions passed. No remaining blocker in this scope.

## Onboarding and email

The onboarding reviewer verified transaction rollback for account creation and
invitation rotation, encrypted queued claim links, preservation of the pending
verification challenge when the encryption key is missing, and suppression of
expired/revoked invitations. The final mail review found no remaining blocker in
that scope. Invitation expiry still requires operator reissue, and provider
acceptance does not establish inbox delivery.

A subsequent decline review reproduced a partial-decision defect: failing a
second account update left another account able to upload; failing the final
organization update removed all accounts from pending review without recording
the decline. The builder moved the entire decision into one transaction, rejected
stale requests and approvals without an eligible account, and added failure and
concurrency regressions. Final independent review found no blocker. Probes passed
both signature/decline race orders: a signature rejects a stale decline, a decline
rejects a pending signature, and a fresh post-signature decline preserves the
signed agreement and receipt delivery while voiding queued access notices.
Decline continues to record the reason without an automated rejection email.

## Case creation

The case reviewer confirmed patient identity holds, FHIR reference resolution,
full timestamp ordering across files, source-note retention and duplicate task-ID
protection. No remaining blocker in this scope.

The reviewer also isolated five unrelated full-suite signup failures to an
existing test helper that replaced the realm store proxy. Both the branch and
clean main produced the same five failures in the same two-file sequence; both
passed the signup file alone. Restoring the proxy fixed the sequence. The builder
removed the leaking fixture assignment; the final independent review confirmed
that correction and the builder's rerun passed all 44 tests.

## Final validation

The builder's full combined run passed 6,836 tests with four skips. After the
last decision/capacity/receipt-error amendments, 145 focused preservation,
onboarding and upload tests passed, and the desktop/mobile health-system browser
gate passed all 22 tests. The upload reviewer independently confirmed the 507
response when capacity cannot be checked and visible receipt-loading errors.

## Limits

These reviews support the repository changes. They do not establish live backup,
restore, deployment, key recovery, email delivery, cloud retention or legal
compliance. The production gates in the audit remain mandatory.
