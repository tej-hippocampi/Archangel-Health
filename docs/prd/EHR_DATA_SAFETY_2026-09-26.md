# ENV-EHR data change record — 26 September 2026

Implementation is on `feat/nephrology-ehr-sandbox`, based on main `6b72423`.
Only local synthetic stores have been exercised. Production rollout is not performed.

## Stores and source of truth

New `ehr_*` tables share the realm-scoped Asclepius database. Canonical input is
the complete retained `ingest_cases` record and original encrypted upload.
Original upload bytes, existing portal tasks/submissions, earnings and signatures
are not rewritten. New parsers produce relative-time derived fields; chart builds
keep ingest-case/upload provenance. Revisions retain previous chart rows.
Sealed keys require `DATA_ENCRYPTION_KEY`; plaintext fallback is rejected.
No new external storage integration is used. OCR scratch is disposable and does
not replace the original. Real source records are never test fixtures.

## Preservation evidence

Before the schema change, a populated synthetic database was created using the
base schema (one retained task with source-case text), then backed up using
SQLite's backup API. Workspace evidence is under `../output/ehr-sandbox/`:
`preservation.db`, `preservation-backup.db`, `inventory-before.json`,
`inventory-after-final.json`, `preservation-restored-final.db`,
`inventory-restored-final.json`, and `preservation-result.json`.

The final schema has 94 tables, including all 82 original tables and 12 new EHR
tables. The before/after comparison found zero missing original IDs and zero
changed original field hashes. A fresh database restored with SQLite's backup
API has the original 82 tables and zero differences from the baseline inventory.
The existing inventory tool discovers all tables dynamically. Independent review
confirmed the backup matches the before inventory. This is local synthetic
preservation evidence, not proof of live-store preservation.

## Failure and release checks

Tests cover unreadable PDF/OCR, malformed/DTD XML, multi-patient identity
ambiguity, unconverted dates, empty adapters, schema reinitialization and
immutable-key writes. Further milestone checks and audit outcomes are recorded
in `EHR_IMPLEMENTATION_STATUS.md`. No existing environment code or table is
repurposed. Rollback before deployment is a code rollback; schema additions can
remain. Any deployed rollback must preserve accepted EHR rows and encryption
keys and use a separately tested consistent store/blob restore.

Live mounted-store inventories, off-volume backups, key recovery and restore
verification remain release checks; no production-preservation claim is made.
