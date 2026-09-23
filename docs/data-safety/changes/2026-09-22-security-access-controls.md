# Security access controls and source preservation — 22 September 2026

Companion to `2026-09-22-security-reliability.md`, which records export, ledger,
durable processing and patient-signal transaction work. This record covers the
remaining authorization, transport and input-handling changes. The confidential
audit report and machine evidence are held outside the public repository at
the workspace's `output/security-audit/` path. No production write or deployment
is authorized by this record.

## Design and invariants

Affected stores and realms: live/sandbox team SQLite stores (patient ownership,
clinical audit, patient signals, Gold visits), Asclepius SQLite stores (users,
password/sign-in/reset tokens, review claims/permissions), community stores and
socket connections, patient/session revocation SQLite stores, existing patient
JSON snapshots, eligibility memory registries and original files, Gold audio,
generated audio, attachment-derived media and upload scratch. Existing live
paths remain stable; new sandbox files and process maps resolve by realm.
No encryption key, object bucket, external account, agreement or signature
evidence is changed. Existing external LLM, email and audio integrations retain
their request/result contracts; no real external send was used for this audit.

Accepted clinical sources, annotations, upload bytes, original patient/document
fields, original exports and financial values remain authoritative. Unknown
ownership is retained without assigning it to the current caller. Authorization
uses persisted owners where applicable and exact tenant/creator/patient identity.
Bulk MBI matching requires the same explicit owner and an active patient. A new
processing identifier is reserved atomically before external side effects.

Eligibility cancellation adds `archived_at` while retaining IDs, fields and
files; active listings exclude archived entries. Gold decline events gain an
additive nullable creator field; old unknown ownership is not backfilled.
Credential updates deliberately change password version/timestamp and consume
outstanding reset/sign-in links atomically. Password routes return replacement
credentials so existing successful navigation remains intact. Patient entry
tokens are consumed once and revocation failures deny access.

Patient JSON escaping and generated-card HTML filtering operate on response
copies; original text is not rewritten by reading resources. Community PDFs are
derived screened artifacts; unsupported or incomplete screening fails visibly
instead of publishing the original as a fallback. Archive expansion and upload
limits stop work before unsafe acceptance. This does not assert that legacy
eligibility in-memory metadata is durable or that heuristic screening proves
de-identification.

Authorized legacy-audio responses copy only an exact patient-bound, regular
source file into an opaque generated-audio filename. They never move or delete
the source or rewrite stored patient metadata. Symlinks, special files, foreign
patient names, ambiguous sandbox provenance and oversized sources are refused.
Partial new derived files are removed on copy failure; originals remain intact.
External audio URLs are returned without fetching them. Concurrency, collision,
failure, ownership and existing-cookie playback tests cover this compatibility
path; root independently confirmed 49 audio/card tests.

## Inventory, backup and restore

All preservation checks used synthetic data, captured before the relevant edits
and compared against the SAME nonempty fixture. Evidence folders:

- `agent-data`: frozen team DB (44 tables / 9 original rows) and original blob;
  frozen review DB (80 tables / 10 original rows), original chart, inventory
  before/after, complete ID/field/file checksums and SQLite backup/restore.
- `agent-app`: frozen Gold data (2 tables / 4 original rows), additive migration
  inventory diff, original fields unchanged and separately restored backup.
- `agent-reliability`: separate intake and patient-signal frozen team fixtures
  (44 tables / 9 original rows each), denied-operation preservation checks and
  consistent restored copies. Denial audit appends are intentional new rows.
- Test fixtures for eligibility cancellation, cross-tenant batch matching and
  extraction denial compare every original patient/document field and source
  byte, including foreign, missing-owner, archived and both-realm cases.

Backups used SQLite's backup API, retaining committed WAL, with writers absent
during the local comparison. Synthetic fixture data is retained in the evidence
directory. No production original or key was used; these tests cannot establish
live recovery, remote object versioning, production encryption coverage or
off-volume retention. No S3/PostgreSQL mutation is part of this patch.

## Tests and independent review

Regressions exercise anonymous/foreign/missing/revoked identity, same-ID realm
collisions, legitimate same-owner controls, pagination before disclosure,
secondary-object ID mismatch, concurrent ownership/token claims, revocation DB
failure, password rollback and refreshed sessions, oversized/hostile archives,
malformed/hidden/mixed-page PDFs, stream reconnect/session revocation, source
immutability and actual frontend transport execution. Existing UI markup/styles
are unchanged; existing browser and frontend suites provide compatibility checks.

Fresh auditors reviewed builders' source and ran tests independently. Additional
issues found in that loop (survey realm propagation/logout, eligibility batch
merging and extraction-result scoping) were fixed and confirmed. Final counts,
exact command logs, retained failed first-run results and remaining limits are in
the confidential report. Root independently reviewed the final batch merge and
extraction fixes: 87 related tests passed in the combined confirmation run.

The final frozen backend/browser run collected 8,372 tests: 8,360 passed, nine
failed and three skipped. The nine failures were independently traced to missing
owner fields in four older fixture modules. After fixture-only corrections,
all 35 tests in those modules passed with every original assertion retained.
Application sources were unchanged between the runs. The combined evidence is
8,369 unique passed tests and three skips, not a wholly green full-suite run.
Two skips require local Tesseract and one is an opt-in nightly timing check.
Frontend tests passed all 72 cases. Full original logs and source hashes remain
in the confidential evidence directory.

Release gates include the data-change guard, full applicable test suite,
route-baseline diff, complete CI-shard discovery, PRD citation audit, dangling
imports and whitespace. Source freeze and final gate evidence precede release.

## PR review follow-up — 23 September 2026

Roster-triggered pre-op outreach now returns immediately in the sandbox before
creating a task or consuming the shared live throttle. Synthetic email/SMS
controls confirm that sandbox reads send nothing and preserve send history,
while live email delivery, SMS fallback and duplicate throttling still work.
No recipient records, survey results, original sources or presentation changed.

The optional link timing check used fixed A/B request order, which aligned
periodic garbage collection with one account on both baseline and patched code.
Independent balanced-order and same-account purpose-swap measurements isolated
that harness artifact. The test now warms both clients and shuffles exactly
500 A/B and 500 B/A pairs using a fixed seed. It retains 1,000 observations per
variant and the existing absolute t-statistic threshold of 4.5, and additionally
requires every response to succeed. Root's timing-enabled module passed all ten
tests. Clinical application code for that measurement is unchanged.

## Do not touch / operational release decision

Do not delete accepted originals, rewrite unknown ownership, remove historical
claims/annotations, migrate production data by inference, send real test emails,
restore over production or publish vulnerability details in a public PR. The
public repository's partner-chart publication requires an owner decision and
review of authorization; private visibility cannot recall existing copies.

Read-only production metadata confirmed a mounted volume and scheduled platform
backups. A live isolated restore, off-volume backup, separate key recovery,
object-version coverage where used, provider delivery/failure checks and alert
verification are still required by `docs/data-safety/POLICY.md`. Unavailable
checks block the release claim; local green tests are not a substitute.

Rollback retains additive schema and all accepted data. Stop/fence durable workers
before a code rollback; do not abandon accepted receipts or revert a live DB over
newer work. A backup restore needs a write pause and an approved reconciliation
plan. Reverting access-control code can reopen the confirmed vulnerabilities.
