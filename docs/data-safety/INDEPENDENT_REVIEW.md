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

## Production follow-up and health-check review

10 September 2026 Pacific / 11 September UTC. A fresh read-only reviewer compared
deployed `a578b001` with the original PR head, then checked the follow-up after
current main was merged. The reviewer independently reproduced a healthcheck
that returned 200 and created zero-byte databases when their parent directories
already existed. The builder changed probes to SQLite URI read-only mode and
checks for application tables.

The reviewer found two test-isolation defects: the healthy baseline depended on
a community database from previous runs, and the malformed-file test reused a
path with a valid WAL. The builder made temporary real schemas explicit, restored
realm store caches after each test, and separated the malformed fixture path.
The final cold independent run passed all 13 health tests in 1.37 seconds with
network connections blocked. The reviewer found no remaining blocker in this
follow-up, including encoded filenames and committed WAL reads. One documentation
function-name correction was also confirmed.

The builder's broader follow-up passed 220 focused storage/preservation,
onboarding, reminder and news tests; after the fixture correction, the combined
health/storage suite passed 44 tests. All PRD citation checks, the 671-route
baseline, import scan, data-preservation SQL gate, AGENTS check, merge-readiness
and diff formatting passed. A frozen synthetic inventory retained all content
in 70 tables, seven rows and two files. These runs are distinct from the earlier
full-suite run; a new full-suite result is not claimed here.

The reviewer confirmed the production report's distinctions: six successful
public GETs and reported `/data` storage establish limited availability, not
backup, key recovery, full inventories or end-to-end workflows. Backup/alert
settings remain unverified. Sandbox is a realm sharing the deployment, not
independent staging. Clinical worker ownership and aggregate capacity admission
remain open. No reviewer accessed production records or sent real mail.

## CI sandbox-isolation fixture correction

Both failed jobs on `229807a` (Backend and Keyless shard 1/4) stopped at the same
sandbox-isolation test. The passwordless health-system signup correctly refused
to queue an unencrypted claim link, but the test fixture supplied no encryption
key and expected signup to succeed. The failure reproduced when running that
test alone.

The builder added a synthetic encryption key scoped by pytest's monkeypatch
fixture, preserving the production refusal. The existing scenario now also
checks that the sandbox invitation is encrypted, decrypts to an invitation link,
and creates no recipient entry in the live queue. The focused sandbox,
invitation and preservation suite passed 52 tests.
The exact CI shard 1 file list then passed 1,754 tests with one skip in 152.17
seconds, starting without vendor API keys or inherited encryption keys. The
earlier full-suite pass had masked the missing fixture because another module
sets an encryption key at collection time. Preservation inventory, SQL gate,
PRD citation, merge-readiness and diff checks also passed.

The independent reviewer approved the change with no findings. A fresh isolated
run passed the sandbox leak test in 3.65 seconds with all encryption-key variables
initially absent and network connections blocked. The reviewer verified that
fixture teardown restored the encryption key to absent. No production calls or
application-code changes were made for this correction.

## Limits

These reviews support the repository changes. They do not establish live backup,
restore, deployment, key recovery, email delivery, cloud retention or legal
compliance. The production gates in the audit remain mandatory.
