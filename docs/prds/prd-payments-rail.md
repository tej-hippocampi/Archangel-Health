# PRD: Payments rail, Stripe Connect Express behind a flag (group G)

## September 17, 2026 safety amendment

The retry and webhook persistence guarantees below are superseded by the
[Stripe payout safety change record](../data-safety/2026-09-17-stripe-payout-safety.md).
Stripe idempotency keys are not permanent. New payments commit immutable transfer
intents with the ledger decision, persist first dispatch time, and refuse
unresolved retries after 23 hours or without an intent. Matching webhooks recover
lost responses. Reversed transfers cannot regress or be retried. New webhook
storage is allowlisted and excludes identity details; historical records are
preserved pending any separately authorized disposition.

The original plan below describes the flag-gated Connect rail. The September
17 amendment and updated retry/webhook requirements describe the current safety
contract; notification touchpoints remain product requirements, not evidence of
deployment or provider acceptance.

## September 19, 2026 operations amendment

The [payment operations runbook](../payments/OPERATIONS.md) and
[data-preservation record](../data-safety/2026-09-19-payment-automation.md)
supersede earlier assumptions that Express onboarding automatically files 1099s.
Tax collection, W-9 certification, annual form settings, reconciliation, delivery
and explicit filing are separate steps. Every new payment batch requires manual
approval; the worker only dispatches that frozen selection. Bank payouts are
tracked independently of transfers. Tax reviews and bank status are mode-scoped.
New processing and live execution remain disabled by default.

## Original problem (from the meeting)

Physicians are paid for labeled cases and the meeting treats payouts plus 1099
generation as table stakes ("claimed partially working"). At that point only
the ledger half existed: accrual, quality holds, an admin mark-paid that "records
settled; does not move money" (`backend/routers/asclepius_payments.py:18`).
With the rail disabled, no money moves and the physician-facing surface is a
disabled "Link your bank account / coming soon"
card (`frontend/asclepius/first_run.js:646-655`) backed by a
`bank_link_status='coming_soon'` interest register
(`backend/routers/asclepius.py:1740-1756`).

The codebase has already committed to the shape of the fix, in the payments
router's header (`backend/routers/asclepius_payments.py:18-22`): the rail uses Stripe
Connect Express, physicians onboard themselves, Stripe holds bank details and
tax ids and files the 1099-NECs, and nothing in this codebase may ever store a
bank account number or a tax id. This PRD builds exactly that commitment.

## Design and invariants

**Locked (founder meeting + planning session):**

- Build Stripe Connect Express now, behind `ASCLEPIUS_STRIPE_ENABLED=0`, with
  mocked tests. The user completes Stripe account setup (Connect enablement,
  KYC) separately and flips the flag live with real keys. Everything is dark
  until then: flag off means exact current behavior.
- 1099 is fully delegated to Stripe: 1099-NEC via Connect tax forms. We never
  generate a tax form, never collect a W-9, never see a TIN. Stripe collects
  tax identity during Express onboarding and files.
- No new dependency beyond the `stripe` python SDK.

**Made here, with rationale:**

- **G1. We store exactly two Stripe facts per physician: account id and
  status.** `users.stripe_account_id` and the existing `bank_link_status`
  column, which gains real states. Anything richer (requirements due, payout
  schedule) is read from Stripe when an admin asks, never cached, because a
  cached copy of compliance state is a stale copy the moment Stripe updates it.
  The header rule is the test: if a change wants to store more, it belongs
  behind Connect instead.
- **G2. Ledger first, transfer follows.** `mark_paid`
  (`backend/asclepius/payments.py:2003`) stays the source of truth: its
  batch-id idempotency and guarded compare-and-set already make a retried
  disbursement safe, and a second write path would be a second chance to pay
  twice. The Stripe transfer is created AFTER the compare-and-set succeeds,
  as a consequence of the ledger row changing state, never as a precondition.
  A transfer failure therefore cannot un-settle the ledger; it becomes a
  visible reconciliation item (G4), which is the honest ordering: the ledger
  records our decision to pay, Stripe records the execution.
- **G3. One transfer per ledger row, idempotency key from the row id.**
  Key `earning:{earning_id}`, `transfer_group` set to the `payout_batch_id`.
  Per-row transfers make Stripe's ledger reconcile 1:1 against ours (the
  existing `GET /admin/earnings?payout_batch_id=` view maps to a transfer
  group), and retries use the same frozen intent and key. Known transfers
  are no-ops;
  unresolved retries require an intent, an exclusive dispatch lease and a
  first-attempt age below 23 hours. Expired or legacy attempts require
  reconciliation because Stripe may prune keys after 24 hours.
  A single batch-sum transfer would be cheaper to create but turns partial
  failure into manual arithmetic.
- **G4. Failed transfers are a queue, not an exception.** A new
  `stripe_transfers` record per attempt (earning_id, transfer_id, status,
  failure reason, timestamps) plus an admin view of "settled but not
  transferred" rows with a retry action. This mirrors the held-earnings
  pattern already on this router: a failure nobody can see is an automated
  decision with extra steps.
- **G5. Webhooks are durable rows processed idempotently.** Signature-verified
  events land in a `stripe_webhook_events` table (event id is the primary key)
  before any processing, then are processed and stamped. Only allowlisted
  operational fields are stored. Handlers read the verified event in memory;
  an interrupted handler is retried using Stripe's signed redelivery. A
  processed event is deduplicated, and transfer reversal is an absorbing state.
  Concurrent delivery must be safe to apply more than once.
- **G6. The stripe SDK is imported lazily, inside the flag.** With
  `ASCLEPIUS_STRIPE_ENABLED=0` the module must import and every endpoint must
  behave exactly as today even if the `stripe` package were absent or broken.
  The dependency is pinned in requirements but never load-bearing while dark.
- **G7. No refunds, no reversals, no clawbacks in v1.** The void endpoint
  already 409s on paid rows ("money has left; refunds are handled outside the
  ledger"). Transfer reversals stay a Stripe-dashboard treasury operation; the
  webhook records `transfer.reversed` for visibility but triggers no ledger
  write, because a ledger that auto-mutates on reversal would contradict the
  attributed, human-decided shape of every other money action here.

## Requirements

### A. Flag and configuration

- **A1.** `constants.stripe_enabled()` reads `ASCLEPIUS_STRIPE_ENABLED`
  (default `0`), same env-flag idiom as the empirical-difficulty flags.
- **A2.** New env vars, documented in `.env.example` and
  `docs/DEPLOY_BACKEND_RAILWAY.md`: `ASCLEPIUS_STRIPE_ENABLED`,
  `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`. Flag on with either key
  missing fails loudly at the first Stripe call, never silently.
- **A3.** `backend/requirements.txt` pins `stripe==15.6.1`. The signed-webhook
  regression uses the installed SDK to verify its event conversion behavior.

### B. Physician onboarding (Connect Express account links)

- **B1.** `POST /api/asclepius/me/bank-link/start` (session-scoped, EARNINGS
  surface, matching this router's no-user-id-in-path rule): creates the
  Express account on first call (storing only its id), then returns a fresh
  account-link URL (`account_onboarding` type, with return and refresh URLs
  back into the portal). Idempotent: an existing account id gets a new link,
  never a second account.
- **B2.** `GET /api/asclepius/me/bank-link` returns `bank_link_status` and,
  when an account exists and the flag is on, live payouts-enabled state read
  from Stripe.
- **B3.** `bank_link_status` becomes a real state machine:
  `coming_soon -> onboarding -> active | restricted`. `coming_soon` rows are
  the waiting list the placeholder endpoint has been collecting
  (`backend/routers/asclepius.py:1748`, "reads this column to find who has been
  waiting"); when the flag flips live, those users get the go-live nudge (see
  email touchpoints).
- **B4.** `frontend/asclepius/first_run.js:646-715`: flag on (surfaced via the
  bootstrap payload) replaces the disabled card with a live "Link your bank
  account" button opening the account-link URL; flag off renders the card
  exactly as today, including the interest POST.
- **B5.** Flag off: both new endpoints return the current placeholder behavior
  (`{"ok": true, "bank_link_status": "coming_soon"}` shape), so no client can
  tell the rail exists.

### C. Transfers on admin pay

- **C1.** `POST /admin/earnings/pay` and `mark-paid` keep their exact current
  semantics. With the flag on, after `asc_payments.mark_paid` returns the
  changed rows, a transfer is created per row (G3) to the physician's
  connected account, amount `amount_cents`, currency usd, idempotency key
  `earning:{earning_id}`, `transfer_group` the batch id.
- **C2.** Paying a physician with no `active` bank link is refused with a 409
  naming the problem BEFORE `mark_paid` runs, so the ledger never says settled
  for someone we provably cannot pay. (Flag off skips this check entirely:
  exact current behavior.)
- **C3.** Every transfer attempt writes a `stripe_transfers` row (G4); the pay
  response reports per-row transfer outcomes so the console shows what
  actually happened rather than what was intended.
- **C4.** Retry endpoint: `POST /admin/earnings/{earning_id}/retry-transfer`,
  admin-gated, 409 unless the row is settled with a failed or missing transfer.
  Dispatch requires a durable intent, matching environment and an unexpired
  retry window. Blocked attempts report the reconciliation reason.

### D. Webhooks

- **D1.** `POST /api/asclepius/stripe/webhook`: verifies the signature with
  `STRIPE_WEBHOOK_SECRET` (constructed event, never trusted JSON), inserts
  into `stripe_webhook_events` keyed on the Stripe event id (duplicate insert
  is a no-op 200), then processes. Returns 404 when the flag is off, so the
  route does not exist observably while dark.
- **D2.** `account.updated`: recompute `bank_link_status` from
  `payouts_enabled` and `requirements.disabled_reason` (`active` vs
  `restricted`); log an event on every status change.
- **D3.** `transfer.created` / `transfer.updated` / `transfer.reversed`:
  stamp the matching `stripe_transfers` row; reversal writes visibility only
  (G7) and cannot be overwritten by a stale event or API response. A missing
  response is recovered only when the complete immutable intent matches.
- **D4.** Unknown event types are stored and stamped processed with no action:
  their stored object is empty, so unfamiliar payloads cannot retain identity
  details. Known types also exclude metadata and free-form fields.

### E. Schema (additive only, no migration framework exists)

- **E1.** `users.stripe_account_id TEXT` (nullable). `bank_link_status` column
  already exists.
- **E2.** `stripe_webhook_events(event_id TEXT PRIMARY KEY, type TEXT,
  payload_json TEXT, received_at TEXT, processed_at TEXT, outcome TEXT)`.
- **E3.** `stripe_transfers(earning_id TEXT, transfer_id TEXT, status TEXT,
  failure_reason TEXT, payout_batch_id TEXT, created_at TEXT, updated_at
  TEXT)` with a unique index on `earning_id`.
- **E4.** Store account IDs and operational status only. Newly persisted
  webhook objects exclude bank/tax details, identity fields and metadata. Tests
  use identity sentinels for known and unknown event types. Historical raw
  payloads need separately authorized cleanup; this migration preserves them.
- **E5.** `stripe_transfer_intents` holds immutable request parameters, a random
  intent ID, first-attempt time and lease ownership. Intent and payment decision
  commit atomically. No historical attempt times are inferred or backfilled.

## Current code map

- `mark_paid` in `backend/asclepius/payments.py:2003` owns the ledger decision.
- `mark_paid` in `backend/routers/asclepius_payments.py:976` and
  `admin_pay_earnings` in `backend/routers/asclepius_payments.py:1769` dispatch
  transfers only after the ledger commit, with the rail enabled.
- `register_bank_link_interest` in `backend/routers/asclepius.py:1741` records
  the waiting list while the rail is disabled.
- `comingSoonBankCard` in `frontend/asclepius/first_run.js:646` and `liveBankCard`
  in `frontend/asclepius/first_run.js:667` render the flag-dependent bank card.
- `claim_stripe_transfer` in `backend/asclepius/store.py:6740` enforces the
  durable retry window; `recover_stripe_transfer` in
  `backend/asclepius/store.py:6781` reconciles matching signed webhooks.
- `webhook_storage_object` in `backend/asclepius/stripe_rail.py:354` defines
  the persisted allowlist. The SDK is pinned in `backend/requirements.txt`.

## Gaps / changes per file

| File | Change |
|---|---|
| `backend/requirements.txt` | A3 exact `stripe==` pin |
| `backend/asclepius/constants.py` | A1 `stripe_enabled()` |
| `backend/asclepius/stripe_rail.py` (new) | account create + account links, transfer creation, webhook processing, status mapping; the ONLY module that imports stripe (G6) |
| `backend/asclepius/store.py` | E1-E5 additive schema + accessors |
| `backend/routers/asclepius_payments.py` | C1-C4 transfer-after-mark-paid, retry endpoint, pay-time bank-link check |
| `backend/routers/asclepius.py` | B1-B3 bank-link endpoints beside the existing interest route |
| `backend/main.py` | D1 webhook route registration |
| `frontend/asclepius/first_run.js` | B4 live card behind the flag |
| `frontend/asclepius/earnings.js` | bank-link status strip on the earnings surface (link, restricted warning) |
| `.env.example`, `docs/DEPLOY_BACKEND_RAILWAY.md` | A2 |

## Email / notification touchpoints

- **Go-live nudge** (one-time, when the flag flips on): every user whose
  `bank_link_status='coming_soon'` gets the promised "banking is live" DM from
  the Archangel bot plus an email via the existing outbox pattern; the
  first-run card literally promised "we'll DM you the moment it does"
  (`frontend/asclepius/first_run.js:654`), matching recorded copy.
  Idempotent via a stamp, same claim pattern as `onboarding_nudge.py`.
- **Restricted account**: on `account.updated` moving a physician to
  `restricted`, one email telling them Stripe needs more information, linking
  a fresh account link. No repeat sends without a state change.
- **Transfer failure**: admin-facing only (the G4 queue). The physician is not
  emailed about our infrastructure problem.
- 1099 delivery is Stripe's: no email of ours touches tax forms, ever.

## Test plan (plain pytest, WHY docstrings, no network)

- `test_stripe_rail_dark.py`
  - `test_flag_off_is_byte_identical_current_behavior`: WHY: the lock is
    "everything dark"; interest endpoint, pay endpoint, and first-run payload
    must match today's responses exactly with the flag off.
  - `test_module_imports_without_stripe_package`: WHY: G6; the dependency must
    not be load-bearing while dark.
  - `test_webhook_404_when_dark`: WHY: D1; a dark rail should not advertise a
    signature oracle.
- `test_stripe_onboarding.py`
  - `test_start_creates_account_once_and_stores_only_id_and_status`: WHY: G1
    is the file-header commitment; assert the users row diff is exactly two
    fields.
  - `test_account_updated_webhook_moves_status`: WHY: B3/D2 state machine,
    including restricted and recovery back to active.
- `test_stripe_transfers.py`
  - `test_transfer_follows_mark_paid_with_row_idempotency_key`: WHY: G2/G3;
    ledger writes first, key is `earning:{id}`, group is the batch.
  - `test_transfer_failure_leaves_ledger_settled_and_queues_row`: WHY: G4; the
    ledger is the decision record, the failure is a visible reconciliation
    item, not a rollback.
  - `test_retry_is_idempotent_and_gated`: WHY: C4; a double-clicked retry must
    not double-pay within the durable retry window; delayed retries must stop.
  - `test_pay_refused_without_active_bank_link`: WHY: C2; settled-but-unpayable
    must be impossible to create, not merely detectable.
- `test_stripe_webhooks.py`
  - `test_bad_signature_rejected`: WHY: an unsigned webhook is an unauthorized
    ledger-adjacent write path.
  - `test_duplicate_event_id_processed_once`: WHY: G5; Stripe redelivers, and
    processed events are deduplicated and interrupted handlers remain retryable.
  - `test_no_bank_or_tax_data_ever_stored`: WHY: E4; the invariant that
    new webhook records exclude bank/tax identity details.

- `test_stripe_transfer_safety.py`
  - Delayed retries after key pruning, concurrent dispatch, immutable request
    parameters, failure at commit boundaries, exact-intent webhook recovery,
    unknown-event privacy, irreversible reversal status, and real SDK signature
    verification. These are network-free regression tests.

## Out of scope

- Flipping the flag on (user completes Stripe Connect setup and KYC first).
- Refunds, reversals as ledger mutations, clawbacks (G7).
- Health-system payouts (`hs_payouts` stays admin-entry; different rail
  decision for a different counterparty size, likely invoiced ACH).
- Instant payouts, payout scheduling, currency other than USD.
- Any change to accrual, quality holds, or the mark-paid semantics.
- Equity-only contributors (existing guard already refuses pay; unchanged).
