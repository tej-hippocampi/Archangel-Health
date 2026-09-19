# Specialty onboarding revision and completion

## Scope and immutable material

PR #167 completes the synthetic onboarding library with 45 additions, for 86
cases across 43 specialties. The 41 original bundled files remain byte-for-byte
unchanged. No database migration, deployed-store write, email, payment or change
to existing batches is part of this release. Runtime lookup preserves existing
ready database cases, draws and submitted examinations.

Authoring inputs contain synthetic cases, public references and retained review
diagnostics. They contain no applicant/CV data or health-system partner records.
Pathology reference images are public-domain and de-identified, with provenance
and single-field limits. No object buckets, encryption keys, upload sessions or
external customer records are modified.

## Before/after preservation and rollback

The frozen before inventory is
`output/specialty-onboarding/revision-before-bundles.json`: SHA-256 values for all
41 original case files. The release inventory verifies the same original file
identities and hashes, adds only reviewed bundles, and validates all 86 release
records. All accepted originals and prior trial/rejection artifacts are retained.
No rejected verdict is overwritten; each revised entry has a new checksum and
fresh review evidence.

The prior same-dataset bundle and SQLite backup/restore regressions remain
applicable. This change opens no production stores and attempts no production
restore. Ongoing production backup and alert coverage have not been newly
verified; these local fixture/file checks do not claim live operational coverage.

Rollback reverts application changes while preserving stored draws, submissions,
ready database cases, paid batches and original artifact history. Do not overwrite
submitted assessment identities or remove accepted source records.

## Validation and independent review

The CI-only prepared path checks complete immutable input before paid probes,
skips authoring only, and requires four new blinded/clinical calls. Source hashes,
literal citations, clinical safety, >=0.90 confidence, public-answer boundaries,
image review and provider provenance remain enforced. Provider failures stop paid
work; failures retain safe diagnostics without publishing or writing store data.

Separate fresh-context artifact audits cleared all 45 additions. Source audits
verified exact evidence identities and licensed excerpts. Independent code review
confirmed fixes for stale/relabelled audit authority, third-party retention,
canonical schema coercion and pre-probe validation. The release registry binds
whole-document hashes to immutable reports and exact task/entry identities.

Final focused tests: **272 passed**, including the unchanged complete-library
assertion. All 86 release records validate; the frozen inventory preserves all
41 original file identities and hashes, with 45 additions. The committed result
is `onboarding_material/source_audits/release-file-inventory.json`. The PR records
final CI; merge requires all required checks to pass.
`data_change_guard.py --base origin/main` reports no newly added destructive SQL.
Detailed run links, audit paths and clinical-review limits are recorded in
`docs/validation/ONBOARDING_REVISIONS_2026-09-18.md`.
