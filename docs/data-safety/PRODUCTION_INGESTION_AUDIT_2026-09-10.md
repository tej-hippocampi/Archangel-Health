# Production ingestion audit follow-up

10 September 2026, Pacific time; observations collected 11 September UTC.
Continuation of PR #151. The owner confirmed `easygoing-victory` as the main
Railway project and reaffirmed de-identification **before upload**.

**Release decision: hold.** Railway access now works, the production service is
reachable, and its reported storage paths are on the persistent volume. This
does not establish recoverability. Backup/restore, key recovery, authenticated
workflow/email validation and clinical worker recovery remain unverified.
The deployed revision also predates PR #151's preservation and privacy-gate fixes.

## Verified environment and routes

- Project: `easygoing-victory`, `932bbbfd-ab16-4246-be34-6c99835a2247`.
- Environment: `production`, `24a0d489-32b2-4b54-83de-9cc3c1b3eab1`.
- Service: `Archangel-Health`, `554c28e3-5cbe-404d-af72-6a7a7e62f14b`.
- Deployment: `d2147538-8ca2-41aa-be27-c679a9c75f8e`, successful, created
  `2026-09-11T00:28:14.464Z`; source `main` at
  `a578b00125a0bbdf55f78ad88695e59e8c2fc7ad` (PR #149).
- One configured replica in `europe-west4-drams3a`; one 5000 MB volume,
  `f549ed4a-d426-4b78-8114-6c5c76644856`, mounted at `/data`.
- Railway reports the RAILPACK builder. The deployment's config-as-code manifest
  sets `/healthz`, restart on failure and ten maximum retries; its healthcheck
  stage completed at `2026-09-11T00:28:56.709Z`. Runtime logs show Uvicorn listening
  on port 8080. The effective start-command field was null, so the Dockerfile CMD
  is not treated as proof of the full runtime command.
- [Provider portal](https://app.archangelhealth.ai/provider),
  [Asclepius admin](https://admin.archangelhealth.ai/asclepius/admin),
  [general admin](https://admin.archangelhealth.ai/admin) and
  [sandbox admin shell](https://app.archangelhealth.ai/sandbox/admin) all returned
  HTTP 200 at `2026-09-11T02:22:38.812908Z`. These are page-shell checks; no
  authenticated records or end-to-end workflow were examined.
- Only the production environment is listed in this project. The sandbox is an
  application realm in the same deployment and storage, not an isolated Railway
  staging or restore environment.

Both domains' `/healthz` responses reported `status=ok`, `storage_durable=true`,
no warnings and `storage_gate_overridden=false`. Reported resolved paths were:

- Asclepius: `/data/asclepius.db`.
- Team: `/data/team.db`.
- Community: `/data/community.db`.
- Originals: `/data/asclepius-ingest`.
- Assets: `/data/assets`.
- Exports: `/data/asclepius-exports` (reported, not boot-gated).

Railway's 24-hour service metric `DISK_USAGE_GB` was approximately 0.249 GB.
Its scope was not established as the mounted volume alone. It must not be
subtracted from 5 GB to claim free space, upload headroom or a capacity pass.
Sandbox database existence and the actual file inventory remain unverified.

## Findings and changes

1. **Age-based deletion remains in the deployed revision.** At deployed commit
   `a578b001`, `purge_expired_raw` in `backend/asclepius/ingestion.py` removes
   eligible originals after a default 30 days and `purge_orphan_raw` removes
   orphaned originals after a default 24-hour grace period.
   `reap_stale_sessions` in `backend/asclepius/uploads.py`
   removes interrupted parts after a default 24-hour idle period. PR #151 removes
   these age-only paths. Actual deletion counts, effective retention overrides
   and recoverable historic originals were not examined; this is a verified
   code risk, not evidence that particular production records have been lost.
2. **The new health-system application privacy gates are not deployed.** PR #151
   checks declared authority and ability to de-identify in the partner's own
   environment during approval/signing/upload access. These declaration gates
   cannot prove the contents of every original were de-identified before upload.
   Existing legacy/provider-link compatibility routes remain. Post-receipt
   residual-identifier scanning is a separate defense, not an approved PHI intake
   workflow or a substitute for pre-upload de-identification.
3. **Health checks could recreate a lost database and report success.** An
   independent synthetic reproduction of both deployed and original PR handlers
   started with existing parent directories and absent team/community databases.
   `/healthz` returned HTTP 200 and created two zero-byte files with no tables.
   The previous test used a missing parent directory, which masked the bug.
   This follow-up changes SQLite probes to URI `mode=ro` and requires an
   application table. Missing, empty or corrupt databases now fail; a committed
   WAL and filenames containing URI punctuation remain readable. The endpoint
   still checks only team/community databases and static assets, reports cached
   startup durability, and is not a full inventory or ingestion-readiness test.
4. **Clinical ingestion still runs in-process without fenced ownership.** Startup
   recovery and retained originals do not prove safe worker overlap or recovery
   from every live-process scheduling failure. One configured replica reduces
   concurrent replicas but does not prove that restart/deployment overlap is safe.
5. **Clinical upload capacity checks do not reserve aggregate capacity.** Parallel
   sessions can each pass against the same free space; the default upload estimate
   is 4 times declared bytes while supported archive expansion can be up to 10
   times compressed bytes. PR #151 closes the unknown-capacity acceptance path,
   but reservations and expansion-aware admission remain separate work. Removing
   age deletion must be paired with verified capacity alerts and admission limits.
6. **GitHub release enforcement is incomplete.** Main was unprotected, and Railway
   source configuration had `checkSuites=false`. At the initial PR-head query
   only Vercel checks were present; this was not proof of backend CI success.
   Current main was merged into the audit branch, resolving a single PRD citation
   conflict. PR #151 remains a draft and is not authorization to deploy.

## Recovery and email evidence gaps

The connector exposes variable names and masks all values. Presence of
`DATA_ENCRYPTION_KEY` and SendGrid variable names is verified; key validity,
independent recovery and provider delivery are not. Startup logs identify
SendGrid as the active mail transport. No test email was sent and no provider
acceptance, delivered event, failure/retry or bounce webhook was observed.

Backup schedules, snapshot timestamps, retention, independent backup destinations
and configured capacity/error alerts are **unverified**. The available connector
and Railway agent tools do not expose those settings. Missing fields or variable
names and a lack of triggered alert events are not proof that backups/alerts are
absent. Unsupported absence claims in the Railway agent's initial summary were
rejected, and its working memory was corrected to reflect these limits.

The browser dashboard could not be inspected because the browser security check
could not verify the admin-enforced policy. No bypass was attempted.

No separate bulk-media PostgreSQL/S3 configuration or worker is exposed in this
project's service inventory/variable names. Its operational readiness, bucket
versioning/lifecycle and KMS recovery have not been verified; the ordinary
clinical healthcheck does not cover this optional media stack.

## Required evidence to release

1. Record backup schedules, retention and the latest successful snapshot IDs from
   the volume's authoritative backup controls, plus an independently recoverable
   copy. Verify configured capacity/error alert thresholds and recipients.
2. Use an isolated destination with outbound email/model calls disabled. Take a
   consistent backup of all live/sandbox stores and blob trees; use SQLite backup
   APIs or a consistent platform snapshot including committed WAL. Keep keys
   separately recoverable. Restore there, compare all IDs/fields/file hashes and
   test decryption. Do not restore over production or put patient data in this repo.
3. Complete a synthetic partner signup, privacy declarations, form, admin decision,
   agreement, de-identified upload, review and case flow against the restored/staging
   system. Separately verify mail-provider acceptance, delivery/failure and retries
   with an approved test recipient. A 200 page shell is not this evidence.
4. Validate clinical worker recovery/overlap and capacity reservations, close any
   identified implementation defects, require backend/data-preservation checks on
   main and have Railway wait for passing CI. Merge/deploy only after the release
   evidence and concrete production action are approved.

## Tests and evidence

- Live GET evidence: six successful route responses; both health payloads retained
  in [public probe evidence](PRODUCTION_PUBLIC_PROBES_2026-09-10.json).
- [Railway/GitHub metadata](PRODUCTION_INGESTION_EVIDENCE_2026-09-10.json) includes
  IDs, commit, redacted variable names, service metrics and explicit limitations.
- The independent missing-database reproduction used isolated FastAPI handlers
  extracted unchanged from both commits. It read no production data.
- The independent cold health-check suite passed all 13 tests, including missing
  databases in existing directories, empty/corrupt files and committed WAL with
  URI punctuation. The builder's final combined health/storage suite passed 44
  tests; a separate broader run passed 220 focused tests. All PRD citation,
  route, import, data-preservation SQL, AGENTS, merge-readiness and diff checks
  passed. The [review record](INDEPENDENT_REVIEW.md) records the test-isolation
  findings and their confirmed fixes.
- A frozen synthetic inventory covers 70 tables, seven existing rows and two
  file fixtures. Its before/after comparison supports only this local fixture,
  not the live volume or a production backup/restore claim.

## Do not touch

This audit does not authorize identifiable uploads, production data migrations,
cleanup/deletion, key rotation, production restarts/deployment, sending test mail
to real recipients or creating billable staging infrastructure. No such actions
were performed. The audit artifacts contain operational metadata and synthetic
test results, not source patient/account records or secret values.
