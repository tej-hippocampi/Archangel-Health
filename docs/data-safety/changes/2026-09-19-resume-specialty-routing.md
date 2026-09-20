# Resume specialty routing correction

## Scope and source of truth

The change affects future CV parsing and onboarding form resume behavior in all
realms. Team-store credential JSON and CV attempt result JSON receive the same
parse payload through the existing attempt-scoped transactions. Parsed specialty
metadata is additive: status, source and canonical candidate fields. It contains
no resume excerpts, physician identifiers or new third-party disclosures.

The physician's reviewed primary specialty remains authoritative. A newly resolved
CV with explicit attribution can recover an unconfirmed legacy profile. Older
parses cannot override a saved profile; their display spelling cannot override
an explicit null or pediatric clinical identity. Case authoring never receives CV
or applicant information.

No schema migration, historical reparse, production account update, source upload
rewrite, clinical case regeneration, email or payment is performed. CV originals,
attempt identities, superseded attempts, submitted examinations, existing batches,
annotations, earnings and exports remain intact. Only untouched suggestions in
the review form can be cleared or replaced; manual physician edits are retained.

## Preservation and failure evidence

`docs/validation/RESUME_SPECIALTY_PRESERVATION_2026-09-19.json` records identical
before/after hashes for tracked onboarding material and synthetic PDF fixtures,
against the frozen main commit. All 86 released cases remain unchanged.

The worker regression snapshots the same populated synthetic team database before
and after success and failure callbacks. Only credential JSON and attempt progress
columns may change. It restores a SQLite backup made with the backup API and
compares inventories. Superseded attempts remain present, and no terminal poll
may expose the previous attempt's result under the new ID.

The case-flow regression snapshots the same populated Asclepius fixture before
and after replacing unfinished nephrology draws with dermatology. Only tutorial
state may change; original draw IDs remain in history and their case records
remain readable. Existing submitted-exam preservation and stale-submit rejection
regressions remain required. These are synthetic fixture checks, not evidence of
live production recovery coverage. Community stores, object buckets, encryption
keys and paid data paths are not modified by this change.

## Deployment, operational limits and rollback

At the initial 2026-09-19 inspection, the production version endpoint and Railway deployment metadata
both reported main commit `333896355790e89dd58b7e1cd6fe90ffb61c0ff5` (PR167).
That verifies the case library release is running; it does not verify an individual
applicant's stored specialty or previously submitted examination.

Subsequent owner-authorized Railway protection setup and API readback verified:

- Production `/data` volume instance `d8d049d8-f88f-4722-82e5-59da70988db3`,
  Ready, approximately 266.4 MB used of 5,000 MB.
- Retained native snapshot `3c92a291-db4f-4606-81ed-d2cc5fe27c21`, created
  2026-09-20 00:06:15 UTC, with 264 MB referenced. Its presence is provider
  evidence; this new snapshot has not itself been restored.
- Daily backups retained for 6 days and weekly backups retained for 27 days.
- Disk monitor `5b16c4d1-6909-46a8-ba9a-25655b40c2c8`, above 4 GB,
  attached to the single `DISK_USAGE_GB` widget for the correct production volume.
  This verifies configured native alerting; end-to-end alert delivery is untested.
- Refreshed production `/healthz`: healthy databases, durable paths under `/data`,
  no storage warnings, and no storage-gate override.

The September 14 off-Railway encrypted data/key capsules remain present with
matching recorded sizes and SHA-256 hashes. The earlier isolated recovery
verified six databases and 1,943 files. Current configuration metadata supports
key continuity since that recovery; live cryptographic equality was not tested.
Railway volume snapshots cover `/data`, not every historical recovery file or
environment secret. No production restore, data replacement or key change was
performed.

Independent auditor `audit_protection_release` cleared the affected-scope
backup/capacity-alert hold after inspecting this evidence. The policy does not
require particular build/crash email switches or Gmail access for this CV-only
change. Those preferences remain unverified. Application email-provider outcome
testing is outside this PR's unchanged email paths, not a passed check. The
broader platform must not be described as comprehensively recovery-verified.

Rollback reverts application code. Do not roll back databases, delete new CV
attempts, erase manual edits, or replace submitted assessments. The new parse
metadata is additive and can be ignored by the previous application version.

## Independent review and validation

A fresh auditor reproduced temporal entry leakage, unsupported declarations,
third-party section variants and failed-upload resume issues. The builder added
behavioral regressions and corrected each path; the final audit and exact test
results are recorded in the PR. CI must run the real OCR fixtures, 100 uploaded
PDF corpus, actual TypeScript mapper and all 43 specialties against the released
practice/examination library, in addition to normal repository checks.
