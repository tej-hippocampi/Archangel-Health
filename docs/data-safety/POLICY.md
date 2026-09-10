# Data preservation policy

Every data-related change must preserve accepted originals, complete submitted
forms, immutable contract versions and signature evidence, source clinical
charts, physician annotations, financial records, and their relationships.
Derived cases may select content for a task; they must retain provenance to the
complete source. Ambiguous patient identity or partial parsing requires review.
No successful receipt may precede durable storage and committed metadata.

This applies to PRDs, migrations, integrations, jobs, UI edits, configuration,
retention, cleanup, refactoring and infrastructure changes, in every realm.
A test suite is not proof of a live backup or an absolute zero-loss guarantee.

## Required change record

Create a dated record alongside the PRD with:
1. Data/stores/realms touched, including every database, blob root, object bucket,
   encryption key dependency, upload session, document and external integration.
2. Source-of-truth and immutable fields; intentional transformed columns and why;
   identity, foreign-key and checksum invariants. No silent overwrite or truncate.
3. Before inventory and after diff from the SAME frozen dataset. Use
   `backend/scripts/data_inventory.py --db PATH --snapshot --output BEFORE.json`
   and `--diff BEFORE.json --db PATH`; add `--blob-root NAME=PATH` for every local
   file tree. An explicit reviewed `--allow-change table.column` permits only
   that column's values to change; it never permits missing IDs or columns.
   Run separately for Asclepius, team, community, sandbox and any other SQLite
   databases in scope. Use an equivalent snapshot/export for PostgreSQL and
   object-version/checksum manifests for S3. The SQLite script does not cover
   remote stores. Do not claim an empty fixture proves production preservation.
4. Backup location/time, retained object versions, separately recoverable keys,
   tested restore procedure and rollback steps. Pause writes/workers for a
   consistent restore drill. SQLite copies must include committed WAL using its
   backup API or a consistent platform snapshot, not copying a live .db alone.
5. Failure tests: disk full, database failure, process death at each commit
   boundary, duplicate/parallel requests, retries, partial or unsupported files,
   foreign patient identifiers, and loss/unavailability of external services.
6. Independent reviewer, exact results, operational checks and release decision.

## Release gates

- `python backend/scripts/data_change_guard.py --base origin/main`
- Meaningful regression tests plus applicable suite/shard, route and PRD checks.
- Before/after inventories show no missing identity, unexpected field changes,
  missing files, changed hashes or broken lineage. Failed or unavailable checks
  block release; they are never a clean audit.
- Verify live mounted stores, available capacity, database backups, object
  versioning/PITR where used, key recovery, alerting and a successful restore.
- Verify actual email-provider acceptance and failures/bounces. A queued email,
  a provider-accepted email and a delivered email are distinct states.

The static SQL gate is a defense, not a complete data-flow analyzer. It cannot
prove dynamic SQL, ORM cascades, shell deletion, external API changes or backups.
Repository branch protection must require its check and the test jobs.

## Retention and recovery

Age alone does not authorize deleting originals or acknowledged upload parts.
Capacity pressure must reject new uploads visibly before receipt and alert the
operator. Preserve existing data and make interrupted uploads resumable.
Disposable decrypted scratch may be removed after its active-work window; it is
not the original. Never remove the last recoverable source during cleanup.

Legally required, contractually authorized deletion must follow a separate,
scoped disposition identifying the data, authority, legal holds, dependencies,
backup treatment, approver and audit evidence. This policy does not authorize
indefinite PHI retention or override a required lawful disposition.

## Production verification still required

A persistent volume is one copy, not a backup. Before describing the deployed
product as protected, record an off-volume backup, restore all stores and blobs
into an isolated environment, recover keys, compare inventories/hashes and run
upload→review→case plus agreement/email checks on that restored environment.
Do not run recovery over production or send real test mail without authorization.
