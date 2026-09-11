# Data ingestion preservation audit and implementation

Date: 10 September 2026. Base: `7a59f708c7e2eac54ea34d3f1e121ce244399b8a`.
Scope: health-system clinical uploads, bulk media receipts, onboarding forms,
agreements, email work, admin review, source-to-case processing and future change controls.

Status: repository audit completed and corrections implemented for review. **Production
release is not certified.** No production data was changed, no real mail was sent,
and no production deployment was performed. Initial Railway metadata requests
returned HTTP 403. A later account probe reached GraphQL and returned `Not
Authorized` for the saved login. The newly connected Railway integration now
works. The [production follow-up](../data-safety/PRODUCTION_INGESTION_AUDIT_2026-09-10.md)
identifies the main project, successful deployed revision, working routes and
reported durable paths. Backup/restore, key recovery and authenticated workflow
evidence remain unverified. The browser dashboard remains blocked because its
admin-enforced security policy could not be verified.

| Requested area | Repository result | Remaining production evidence |
| --- | --- | --- |
| Uploads and preservation | Originals/acknowledged parts retained; durable writes and atomic receipts; visible integrity/partial-input holds | Mounted storage, capacity alerts, off-volume backups and key recovery |
| Forms, contracts and email | Complete accepted answers; privacy gates; immutable signature evidence; encrypted, retryable invitation jobs; owner confirmed de-identified-only intake | Contract review and actual provider delivery/bounce checks |
| Admin review | Submitted answers/history, decision state, signed documents and email/large-file receipts visible; decisions atomic | Staging and live admin walkthrough; older application history beyond the latest 50 requires a database query |
| Case creation | Patient mappings held when uncertain; source observations retained; failed/partial extraction cannot claim complete ingestion | Real-format partner acceptance fixtures and durable worker operations |
| Future changes | Mandatory policy/rule, complete-content inventory and destructive-SQL CI check | Main branch currently unprotected; require checks and verify infrastructure controls |

## Design and invariants

A successful upload receipt means that original bytes and committed metadata exist.
Parsing success is separate from receipt. Unsupported files, failed case writes,
uncertain patient mappings and incomplete extraction require review; originals stay
recoverable. Human approval and the required signed agreement precede self-service
upload access. Derived cases retain the complete source chart and provenance.

The current approved path is de-identification **at the health system before
upload**, explicitly confirmed by the product owner on 10 September 2026.
An application reporting uncertain authority, uncertain de-identification,
or a need for a BAA cannot be approved through the ordinary DLA flow. This is a
technical gate, not a legal opinion or certification of a partner's data. A separate
PHI/BAA product workflow has not been authorized or implemented in this audit.
HHS describes Safe Harbor and Expert Determination as distinct methods; signing a
contract alone establishes neither method. [HHS de-identification guidance](https://www.hhs.gov/hipaa/for-professionals/special-topics/de-identification/index.html).

### Upload preservation

- Removed age-only deletion of encrypted originals, recoverable orphan blobs,
  acknowledged clinical upload parts and idle bulk-media uploads. Explicit user
  cancellation and disposable decrypted scratch cleanup remain separate actions.
- Originals, part receipts and local assets use atomic replacement, file fsync and
  directory fsync. Existing content-addressed media is rehashed before reuse;
  mismatches fail visibly. SQLite writable connections use synchronous FULL.
  FULL strengthens commit durability, assuming the operating system and storage
  honor synchronization. It does not replace backups. [SQLite documentation](https://www.sqlite.org/pragma.html#pragma_synchronous).
- New chunked sessions refuse with 507 when storage capacity cannot be checked,
  as well as when the declared working set exceeds available capacity.
- One-time link consumption and the upload row commit together. Chunk completion
  commits the receipt, organization/request/purpose attribution and verified
  session in one transaction. Failed commits preserve parts and allow retry;
  post-commit cleanup, event or convenience-default failures cannot undo success
  or suppress ingestion dispatch.
- Evidence: `insert_ingest_upload` at backend/asclepius/store.py:5710;
  `finalize` at backend/asclepius/uploads.py:437;
  `atomic_write` at backend/durable_files.py:23;
  `purge_expired_raw` at backend/asclepius/ingestion.py:538.

### Forms, agreements and email

- Overlong form answers and unsupported selections reject the complete submission
  visibly instead of silently truncating or discarding answers. Submission history
  remains append-only and ties are ordered by the database insertion order.
- Approval commits account decision, organization state, purpose and DLA email
  work and decision evidence together. A failed queue insert leaves the application
  pending in admin. Approval requires an eligible active account.
- Decline commits all account closures, session revocation, organization state,
  reason/event evidence and cancellation of pending access notices together.
  A failure at any write leaves the prior decision intact. Both decisions reject
  a changed state/revision observed after the request began. Declines deliberately
  send no automated rejection email; the existing policy is personal follow-up.
- Signing requires the exact displayed document hash. Immutable signed PDF bytes
  are stored first; signature evidence, activation and receipt/upload-open email
  jobs commit together. The PDF and signature row share the signing timestamp.
- Signup welcome/access emails, teammate invitations, application/intake alerts,
  DLA requests, signed copies and upload-open letters use durable queued jobs.
  Claim-link bodies are encrypted at rest; missing keys stop account creation
  rather than saving a plaintext token. Failed transport retries use the saved job.
  Revoked, superseded, used or expired invitations are suppressed.
- Admin detail shows saved intake answers, the latest 50 application versions,
  and email status, attempt count and last error. Older application versions stay
  in the database; the current application API limits its returned history to 50.
  Sent means provider acceptance, not inbox delivery.
  Large-file collections and their receipt/hash histories paginate for both admin
  and health-system users; they remain separate from clinical case ingestion.
- Evidence: `approve_hs_organization` at backend/asclepius/store.py:12996;
  `decline_hs_organization` at backend/asclepius/store.py:13025;
  `record_signed_agreement` at backend/asclepius/store.py:13152;
  `complete_hs_signup` at backend/asclepius/store.py:12464;
  `message` at backend/asclepius/hs_mail.py:15;
  `_drain_admin_notifications` at backend/main.py:782.

### Source-to-case correctness

- Source charts keep short clinical notes and complete text. Task-oriented curation
  operates on a separate derived copy. Note identity uses complete normalized
  text; lab identity includes panel, units and LOINC to avoid collapsing distinct
  specimens. An existing task ID cannot replace the original task/evidence.
- A file containing multiple patient identities is held, even if a manifest tries
  to override it. Cross-format IDs and unkeyed files require an explicit patient
  mapping. FHIR fullUrl/URN references resolve to their Patient identity.
- Standalone FHIR Patient JSON now parses instead of silently disappearing.
  DICOM requires explicit patient mapping and rejects mixed original patient IDs.
- Every FHIR vital reading survives as a source observation. The flat latest-vital
  set uses values from its own full timestamp, including across multiple files.
- Unsupported/partially parsed bundles and failed per-patient writes can no longer
  report successful complete ingestion. Bulk video remains stored/reviewed media;
  it does not automatically create clinical tasks or go to model APIs.
- Evidence: `unify_patient_keys` at backend/asclepius/ingestion.py:1688;
  `_merge_fragments` at backend/asclepius/ingestion.py:1160;
  `process_upload` at backend/asclepius/ingestion.py:1759;
  `insert_task` at backend/asclepius/store.py:6813.

### Future PRD enforcement

The mandatory [data preservation policy](../data-safety/POLICY.md), repository
AGENTS map and always-applied rule require a data inventory, failure tests,
independent review and restore evidence for every data-related change.
The new CI SQL gate rejects added destructive SQL targeting protected product
records. It is deliberately narrow: dynamic SQL, filesystem deletion, external
APIs and infrastructure still require review and operational evidence.

The inventory checker now fails for missing/empty databases, legacy or incomplete
baselines, missing rows/columns, changed existing field hashes and missing/changed
files. It inventories every SQLite table instead of seven selected ID lists.
Equivalent manifests remain required for PostgreSQL/object stores.
Evidence: `snapshot` at backend/scripts/data_inventory.py:43;
`compare` at backend/scripts/data_inventory.py:93;
`violations` at backend/scripts/data_change_guard.py:16.

## Tests and evidence

- New failure-oriented regressions exercise partial input, cross-patient mapping,
  original preservation, duplicate task IDs, receipt/session transaction failures,
  retries, post-commit faults, form/approval/signature/email rollback, encrypted
  invites, invalid document/input gates, timestamp ordering and failed inventories.
- Full combined regression run: **6,836 passed, 4 skipped**, 759.78 seconds.
  This run preceded the final atomic-decline/stale-decision, unavailable-capacity
  and receipt-error-display amendments. After those amendments, the focused
  preservation/onboarding/upload suite passed **145 tests**. The repeated
  health-system browser gate passed **22 tests**, covering desktop and mobile.
- Earlier focused onboarding/mail run: 123 passed. Obsolete fixture expectations
  were corrected for standalone FHIR Patient support, preserving distinct lab
  observations and the required authority/de-identification approval answers.
- PRD citation checks, the 671-route baseline, JavaScript syntax,
  data-preservation SQL gate and AGENTS map passed on the amended implementation.
  The dangling-import check found no missing imports.
- [Synthetic migration and restore evidence](../data-safety/SYNTHETIC_PRESERVATION_EVIDENCE.json):
  70 tables inspected, seven populated source records, two file fixtures; no old
  fields, IDs or file hashes disappeared across schema initialization or SQLite
  backup/isolated restore. Empty tables establish schema coverage, not production
  data coverage. Encrypted original/PDF behavior has separate execution tests.
- Independent upload, onboarding and case auditors reviewed the changes read-only,
  reproduced failure cases, and confirmed fixes. Their scopes included transaction
  rollback, token secrecy, conditional claims, latest-vital correctness and receipt
  pagination. No agent contacted recipients or modified production.
  See the [independent review record](../data-safety/INDEPENDENT_REVIEW.md).

## Do not touch

Do not delete/rewrite accepted uploads, contract evidence, physician submissions,
records, earnings or exports. Do not use this audit as authorization to merge,
deploy, remove legacy access, migrate live data, purchase storage or send real mail.
Do not edit synced source files or reinterpret brokering media as clinical tasks.
Do not loosen privacy, identity or quality holds to make a test green.

## Production release gates and remaining limits

1. Railway access now works: production is `easygoing-victory`, running
   `a578b001` with the six reported storage paths under `/data`. Verify actual live
   and sandbox inventories, capacity reservations and configured alerts; the
   green public healthcheck and persistent mount are not a preservation proof.
2. Take off-volume database and blob backups with separately recoverable keys;
   verify a consistent restore and full inventories on an isolated restored system.
   Confirm object versioning/PITR and lifecycle rules where object storage is used.
3. Run synthetic signup → form → admin approval/decline → agreement → upload →
   admin review → case generation in staging, plus email provider acceptance,
   failure/retry and bounce/webhook verification. No live end-to-end result is
   claimed in this report.
4. The product owner confirmed de-identified-only intake; review actual contract
   templates with the responsible privacy/legal owner. Legacy operator-provisioned accounts and
   existing partner-link doors keep their existing contractual compatibility;
   this audit does not invent missing historic agreement evidence.
5. Expired invitation jobs are safely voided after their existing 14-day lifetime;
   an operator must reissue access. Provider acceptance is observable; inbox
   delivery/bounces still require provider integration/configuration evidence.
6. Clinical ingestion retains startup recovery. A dedicated durable worker with
   fenced leases is still needed before claiming safe overlapping ingestion
   workers/rolling-worker recovery or automatic retries of every live-process
   scheduling failure. Raw originals and superseded case history remain retained.
7. GitHub read-only metadata confirms that main is currently unprotected. Require
   the new data-preservation and existing test jobs in repository branch protection.
   Policy files alone cannot enforce administrator overrides or cloud retention
   settings. All lawful retention dispositions require separate scope
   and authorization; indefinite PHI retention is not implied.

An absolute claim that no data can ever be lost is not supportable from code tests.
The release requirement is verified preservation and recoverability, with failures
reported visibly and no success claim when evidence is missing.
