# Physician payment operations

Archangel pays physicians for case review and AI work. An administrator prepares
and approves each finite payment batch in **Admin → Money → Payment operations**.
The worker runs every minute and dispatches only those approvals. It does not
automatically approve earnings or add later earnings to an approved batch.

## Deployment and Stripe setup

Ship with `ASCLEPIUS_PAYMENT_OPS_ENABLED=0` and
`ASCLEPIUS_PAYMENT_OPS_LIVE_ENABLED=0`. Database changes are additive. The existing
manual payment routes retain their behavior; these flags govern the new worker.

1. Verify the exact Stripe platform and environment, its Connect approval,
   business verification and bank funding eligibility. Keep enough available USD
   balance for the intended batch. This application does not initiate bank top-ups.
2. Configure a runtime Stripe key with account-read/update, balance-read,
   transfer-create/read and connected payout-read permissions. Store credentials
   in deployment secrets, never in source, test fixtures, chat, or reports.
3. Register two Stripe event destinations at
   `POST /api/asclepius/stripe/webhook`:
   - **Your account:** `transfer.created`, `transfer.updated`, `transfer.reversed`.
     Store that destination's secret as `STRIPE_WEBHOOK_SECRET`.
   - **Connected accounts:** `account.updated`, `payout.created`, `payout.updated`,
     `payout.paid`, `payout.failed`, `payout.canceled`. Store its distinct secret as
     `STRIPE_CONNECT_WEBHOOK_SECRET`.
   Use separate test/live deployments and secrets. The endpoint rejects events
   whose live/test mode does not match its key. Prove successful delivery of both
   scopes; a configured secret alone is not proof of delivery.
4. Configure physician bank payout schedules in Stripe. A platform-to-connected
   **transfer** and a connected-to-bank **payout** are separate operations.
   The new worker creates transfers; Stripe runs the bank payout schedule.
5. For US accounts, `ASCLEPIUS_US_TAX_COLLECTION_ENABLED=1` requests
   `tax_reporting_us_1099_misc` during hosted onboarding (the capability covers
   NEC/MISC identity information). Existing linked US accounts can also be
   updated individually using **Request US tax details**. Physicians return to
   their existing Set up payments flow to provide information. Foreign accounts
   are not assigned US requirements by this feature.
6. Separately configure certified W-9 collection where required, Connect 1099
   reporting, payer identity, default form, calculation method, state filings,
   outreach/e-delivery consent and postal fallback in Stripe. Have an accountant
   confirm payee entity classifications and year-specific reporting obligations.
   Tax identity capability activation alone is not W-9 certification or filing.
7. Record the actual checks in Annual setup review. These are administrator
   attestations, scoped to the tax year and Stripe mode, not API verification.

The readiness screen verifies API connectivity, platform flags and available USD
balance when access is available. It explicitly leaves dashboard-only eligibility,
tax and delivery checks to an administrator. Unknown balance is not shown as $0.

## Test and activate

Use a Stripe test deployment first: approve a small synthetic batch, verify exactly
one transfer per earning, confirm both webhook event scopes, and exercise a failed
bank payout. Test records cannot be presented as live deposits or tax reviews.

After production preservation/restore gates, Stripe setup, webhook delivery and
operator approval are complete, enable the rail and batch worker. Enabling live
execution is a separate deployment setting. No pending draft can dispatch until an
administrator approves its exact preview. Switching test/live mode invalidates a
draft's approval and does not migrate test instructions into live execution.

## Failure and recovery

- Insufficient available balance before first dispatch leaves the earning
  approved and unpaid. Fund Stripe separately; the approved item retries with
  exponential delays from 60 seconds to one hour, at most 12 attempts.
- Every approved batch freezes earning IDs, amounts and recipients. Changed or
  ineligible earnings require review. The ledger decision and immutable transfer
  intent commit together before a remote call.
- A lost response replays the original idempotency key, even if the first attempt
  spent the platform balance. The existing 23-hour retry cutoff is retained.
- After the cutoff or retry limit, inspect the original transfer in Stripe.
  **Retry unresolved approved payments** keeps the same intent and cutoff; it
  cannot bypass an expired window or resend a known/reversed transfer. Do not
  manufacture another earning or edit an intent to force a retry.
- A matching transfer webhook can recover a remote success after a response loss.
  Refresh the view; bank failure is distinct from a transfer failure.
- Failed bank payouts require correction in Stripe. Never make a second transfer
  to compensate for a failed bank deposit. Bank payout events never alter earnings.
- The bank table reflects received events (latest 200), not a complete historical
  import. It can combine many earnings in a deposit; no per-earning bank arrival is
  inferred. Stripe remains the source for complete payout history.
- Disable the worker flag to pause further dispatch. A request already in flight
  may complete. Preserve its intent and reconcile before reactivation. Monitor
  application errors and the visible needs-attention queue.

## Annual reconciliation and filing

1. Review draft 1099 totals in Stripe. Prepare only this minimal CSV; do not upload
   full tax exports containing tax IDs or addresses:

   ```csv
   stripe_account_id,amount_usd
   acct_example,1500.00
   ```

2. Choose the tax year and compare. The server records an immutable report with
   the administrator, mode, time, normalized-source checksum and account-level
   differences. Download its JSON for the accounting workpaper.
3. The local comparison uses **the ledger payment-decision year**, not tax
   recognition dates. It separates confirmed transfers, reversals and unconfirmed
   or external payments. Known opposite-mode transfers are excluded. An account
   missing from either side remains visible. No report claims filing readiness.
4. Reconcile timing across December/January using Stripe transaction logs and its
   selected calculation method. Check external payments, bonuses, fees, reversals,
   entity exemptions and state requirements. This cross-check does not calculate
   the final tax liability or determine whether each payee requires a form.
5. Correct the Stripe drafts as needed, then explicitly review and file in Stripe.
   Verify federal/state acceptance and recipient delivery; handle corrections or
   rejected filings there. The application does not submit IRS forms.

## Sources

- [Connect webhook scopes](https://docs.stripe.com/connect/webhooks)
- [Funding the platform balance](https://docs.stripe.com/connect/top-ups)
- [Bank payout schedules and events](https://docs.stripe.com/connect/payouts-connected-accounts)
- [1099 configuration](https://docs.stripe.com/connect/get-started-tax-reporting)
- [Calculation methods and recognition dates](https://docs.stripe.com/connect/calculation-methods)
- [Explicit tax filing and delivery](https://docs.stripe.com/connect/file-tax-forms)
