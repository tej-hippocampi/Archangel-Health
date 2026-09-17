# Stripe payout safety change record — September 17, 2026

## Scope and invariants

Three fixes: prevent duplicate transfers after delayed retries, minimize new
webhook storage, and prevent stale events from overwriting reversals.

Only the Asclepius SQLite store is changed, in both live and sandbox schemas.
The new `stripe_transfer_intents` table is additive. Existing users, tasks,
submissions, records, earnings, transfers, webhook history and blobs are retained.
Team, community, media stores and external object buckets are not changed.
No production credentials are read, no Stripe operations are run, and no new
encryption-key dependency is introduced by the schema.

The earnings ledger remains the source of truth for the decision to pay.
Stripe remains the source of truth for transfer execution. An immutable intent
(earning, physician, amount, USD currency, destination, batch, environment, random
intent ID) is inserted in the same transaction as a newly approved earning's
payment decision, only when the rail is enabled. A database failure rolls back
both. Historical paid rows are not backfilled with guesses about prior dispatch.

A first-attempt timestamp is committed before the external request. A five-minute
lease serializes concurrent dispatch. Retries keep the original request and key
and stop after 23 hours, before Stripe may prune a key at 24 hours. Expired,
legacy or environment-mismatched attempts require reconciliation, not a new
transfer. A signed webhook can recover a lost response only by matching every
immutable intent field and its random intent ID. Known transfers are never sent
again. Reversals survive stale events and late synchronous success/failure writes.

New webhook payloads retain only an allowlisted object ID and operational
booleans; unknown event types retain an empty object. Raw identity fields,
metadata, document references, requirements and free-form descriptions are not
persisted. Existing historical payloads are intentionally not rewritten by this
migration. If production history contains unnecessary identity details, its
disposition must separately cover retention authority, protected financial
evidence, backups and authorized cleanup under POLICY.md.

## Preservation and restore evidence

Before changes, a frozen synthetic baseline was created for each realm using
the previous schema. Each contains a physician, approved and paid earnings,
a failed legacy transfer, a historical webhook, and an immutable original file.
SQLite's backup API created consistent isolated backup copies. After migration,
`scripts.data_inventory.snapshot` / `compare` reported no missing IDs, changed
existing values, missing columns or changed file hashes. Each backup was restored
to a separate database and compared against the same baseline successfully.

Both realm fixtures retained all five existing rows. New table creation adds no
invented financial records. This is synthetic migration/restore evidence, not
proof of production preservation, backup coverage or key recovery.

Evidence is in the task's `output/stripe-payout-safety/preservation/` directory:
realm-specific before/after manifests, backup and restored databases, and
`after-results.json`. Production databases and uploads were not opened or changed.

## Failure and regression coverage

Tests exercise remote success with response loss, key pruning after 24 hours,
expiry boundaries, backward clock movement, concurrent requests, process loss
before dispatch and after remote success, intent-write failure with ledger
rollback, immutable retry parameters, legacy rows, mismatched webhook recovery
fields, malformed metadata, environment mismatch, stale reversal events, racing
API results, identity-data sentinels and actual SDK signature verification.

## Rollout and rollback

Keep `ASCLEPIUS_STRIPE_ENABLED=0` during deployment verification. Apply schema
initialization to a consistent staged copy first; record actual production store
inventories, backup location/time, capacity, alerting and isolated restore results
before release. Verify the configured webhook secret and Stripe environment.

For an expired/legacy unresolved attempt, review the original earning, batch,
physician and amount against Stripe's transfer records. Recover a verified
matching webhook where an intent exists. Do not delete the attempt/intent, reset
its first-attempt time, change its key, or use a new earning to evade the guard.
Any confirmed unpaid legacy case needs a separately reviewed reconciliation
operation before payment; this release does not guess that it is safe to send.

Rollback: disable the Stripe rail first, then deploy the previous application
version while leaving the additive table and all financial history intact. Do
not re-enable the old retry code; it does not enforce these safety invariants.
Restore only under the separately scoped recovery procedure, never over a live
database with active writers.

Independent audit and final validation results are recorded in the PR description.
Release remains subject to POLICY.md's production backup, restore and alert gates.
