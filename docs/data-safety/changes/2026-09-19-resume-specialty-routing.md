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

On 2026-09-19, the production version endpoint and Railway deployment metadata
both reported main commit `333896355790e89dd58b7e1cd6fe90ffb61c0ff5` (PR167).
That verifies the case library release is running; it does not verify an individual
applicant's stored specialty or previously submitted examination.

A read-only Railway inspection confirmed the `/data` volume and available
capacity, but the available tools did not expose backup schedules, successful
backup timestamps or retention. No project webhooks were returned. Notification
history does not establish configured alert coverage. Build-container snapshots
are not database or mounted-volume backup evidence. Consequently current backup
and alert coverage remain unverified; no claim of recovery readiness is made.
The repository's operational release gate remains open until that evidence is
available. No restoration or operational configuration change was attempted.

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
