# Specialty-matched onboarding — 2026-09-17

## Scope and intended changes

The Asclepius store in each realm gains an additive `onboarding_case_bank` table
and an index on independent-commit evaluator IDs. The realm-scoped store supplied
by each authenticated request owns every case-bank read and write. No team,
community, source-blob, upload, encryption-key, earning, record or export format
changes are made.

Confirmed onboarding specialty is retained as clinical identity independently of
paid-generation registry availability. The original credentials/CV stay intact.
For unfinished examinations assigned outside that specialty, the next draw saves
the previous stamp in `tutorial_json.previous_exam_draws` before replacing it.
Browser drafts remain keyed by case ID. Submitted examinations and their evidence
snapshots remain immutable. Case-bank entries marked ready are not overwritten.
Practice concerns are saved in onboarding state and append-only events.

New bank cases never enter `tasks`. Their examination responses remain in
`credentialing_exams`, with a snapshot of the case and key. A physician's prior
exam task is excluded from paid queues and submission writes, including after
approval; existing historical rows are retained. Legacy authored gold exams
remain available for the three existing specialties.

## Evidence and clinical release gate

Only a specialty string is sent to PubMed; no CV, doctor identity or patient data
is sent to PubMed or the case author. Newly authored content must match the exact
specialty, use fictional clinical data, include a complete key, and cite at least
two actually retrieved recent guideline/review abstracts. Retractions, unsupported
source IDs, unavailable image assets and incomplete charts are rejected. Two
provider families independently solve a blinded case, then review the clinical
key and each evidence claim. Every check must pass. This is automated evidence
review, not physician ratification or a claim of clinical certification.

Cases are prepared once per specialty/purpose/attempt under a persisted lease.
Concurrent applicants share that job. Failed work cannot be served, retries wait
60 seconds, and an expired worker cannot overwrite a newer lease. A missing or
failed case produces a visible pending/retry state rather than an unrelated case.

## Preservation and restore verification

`test_onboarding_specialty_cases.py` snapshots the same isolated database before
and after both specialty journeys and compares all existing row identities and
field hashes. Only the deliberate onboarding state and verification-state changes
are allowed. It creates a consistent SQLite backup through the backup API and
compares the restored inventory. The previous advisor-migration preservation test
continues to preserve legacy exam/submission coexistence; no historical deletion
is introduced by the new write guard.

The browser preservation fixture also backs up and compares the isolated
Asclepius, team and community stores. These are regression/restore tests, not
claims about live production backups. Public PubMed retrieval was verified for
dermatology and neurology. Real-model generation is exercised only through the
manual LLM smoke workflow with isolated CI storage and both provider keys.

## Rollback and operational limits

Reverting the application change leaves all new tables, keys, entries, exam
snapshots and old drafts intact. Do not drop the bank or delete generated rows.
Keep a database backup including committed WAL before release, retain the running
application version, and verify live mounted paths/capacity and restore coverage
under the repository data-preservation policy. No live store migration, backup
restore or test email was performed from the development workspace. Production
backup/restore evidence and real-model smoke results must be recorded before
calling the release verified.

## Read-only operational inspection

Railway inspection on 2026-09-17 confirmed the service volume mounted at `/data`,
5 GB provisioned and approximately 0.263 GB used. The current deployment was
healthy. The connector could not expose backup schedules, completed snapshot
metadata or a restore drill record.

The existing local aggregate records at
`output/production-recovery/RECOVERY_RESULT.md` and `VERIFIED_RECORDS.json` in
the parent project document an earlier successful recovery drill on 2026-09-14
for `archangel-production-20260914-recovery-01`. An encrypted off-Railway capture
of all six live/sandbox stores and 1,943 files was restored in isolation; key
recovery and both realms' application workflows passed with zero missing or
changed originals. The independent reviewer cleared the backup-specific hold
for that captured scope. This is prior production recovery evidence, not a
restore of this change or of records created after that capture. The record
explicitly leaves ongoing backup/alert coverage unverified; the current
connector inspection does not resolve that limitation.

GitHub repository-secret metadata showed neither required provider key configured;
real-model validation is blocked until the Actions secrets are supplied.
