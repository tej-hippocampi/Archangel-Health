# Physician payment automation — September 19, 2026

## Scope and source of truth

Only the Asclepius SQLite store in each realm is in migration scope. Team,
community, clinical blobs, S3 objects, encryption keys, accepted originals,
agreements and clinical source records are unchanged. Stripe is the external
integration. This build made no live Stripe calls or money movements.

Added tables: payment_batches, payment_batch_items, stripe_bank_payouts,
payment_tax_reviews and payment_reconciliations. Existing earnings, transfer
intents and transfer records retain their identities and values on migration.
No existing table, row or column is deleted. A legacy review lacking a mode gets
an explicit unverified mode, never an inferred live attestation.

The earnings ledger records the approved payment decision; immutable Stripe
transfer intents retain exact requests; Stripe records transfer execution and
bank deposits. Annual reports cross-check decisions against administrator-supplied
Stripe draft totals and explicitly do not calculate final reportable income or
claim filing readiness. Account, transfer and payout IDs are operational keys;
bank account details, tax IDs, personal addresses and full webhook bodies are
not stored by this change.

## Design and invariants

- Every batch requires manual approval of a frozen finite selection. New work
  cannot enter an approved batch; a changed amount or recipient fails closed.
- Concurrent workers claim individual items. Ledger decision plus transfer
  intent commit atomically before money movement. Known or reversed transfers
  are never resent. Original idempotency keys and the existing 23-hour cutoff
  survive restarts and administrator-requested retries.
- Insufficient funds before dispatch leaves earnings unpaid. A lost-response
  replay does not require replenishing funds already consumed by remote success.
- Each connected-account bank payout is scoped by account, payout ID and mode.
  Current remote state is retrieved under that account. Stale observations cannot
  downgrade terminal states; a current failure can supersede paid status even
  when triggered by an old event. Payout events never change earnings.
- Platform and connected-account webhook destinations have independent secrets.
  Both are verified; malformed signatures and environment mismatches fail.
- New processing and live execution default off. Sandbox money movement is
  blocked. Test payouts and tax attestations cannot appear as live evidence.

## Frozen inventories and backup restore

Before modifying the schema, generated two nonempty synthetic frozen databases,
one for each realm, using the unmodified main code. Each contains a physician,
approved and paid earnings, a failed historical transfer and a historical webhook.
Each realm also has an immutable original-file fixture. This is a migration
fixture, not production evidence.

Evidence resides in the task workspace under
`output/physician-payment-automation/preservation/`:
live-before.json, live-after.json, sandbox-before.json, sandbox-after.json,
before-results.json and after-results.json. The local preserve.py script uses
the repository data_inventory.snapshot/compare functions and the SQLite backup
API; it never copies an active .db while ignoring WAL.

Results: **5 existing rows before and after per realm**, no missing IDs or
columns, no changed original field hashes, and no changed file hashes. Both
pre-migration SQLite backups were restored to isolated files and compared
successfully with their original inventories.

## Failure tests and independent review

The new backend tests exercise finite manual authorization, changed recipients
and amounts, overlapping drafts, parallel workers, insufficient balance,
lost responses after spending the entire balance, expired Stripe idempotency,
database abort before intent commit, interruption after commit, mode switches,
kill switches, sandbox rejection, authorization, signature verification,
out-of-order/duplicate bank events, tax review environment isolation, malformed
CSV data, and ambiguous annual payments. DOM tests execute the approval UI,
verify its explicit confirmation and request fingerprint, and check visible
unknown/error states.

Independent auditor: audit_payment_ops, fresh context. Four initial findings
(lost-response balance preflight, bank mode mixing, tax-review mode mixing, and
old event timestamps suppressing current bank failure) were corrected and
re-reviewed. Final and incremental reviews reported no unresolved findings.
The incremental reviewer independently ran 79 targeted payment/webhook/safety
tests successfully. Broader validation results are recorded in the PR.

## Do not touch

Do not alter existing payment intent amounts, destinations, first-attempt times
or idempotency keys to force retry. Do not infer bank settlement from a transfer.
Do not upload tax IDs in the reconciliation CSV. Do not change source clinical
records, original documents or contract evidence as part of this work.

## Release decision and rollback

Code is prepared for review with processing disabled by default. **Production
activation is pending.** Stripe dashboard access was blocked because the browser
could not verify the admin access policy. Platform approval, bank funding, tax
settings, W-9 configuration, electronic consent, postal fallback and actual
webhook delivery have not been verified live in this task.

Before merge/deploy, verify production mounted stores/capacity, off-volume
backups, retained object versions and key recovery, then restore all relevant
production stores and blobs into an isolated environment and compare inventories.
Verify both webhook scopes and alerting. Synthetic fixtures do not establish
production protection. Do not enable processing or conduct a real payment test
without operator authorization of the exact batch and environment.

To pause dispatch, turn off ASCLEPIUS_PAYMENT_OPS_ENABLED. An in-flight Stripe
request may finish; reconcile its original intent. Roll back application code
while retaining the additive tables and operational records. Restore a database
only through a separately approved incident plan, never automatically over live
data; preserve post-backup approvals and reconcile external Stripe operations.
