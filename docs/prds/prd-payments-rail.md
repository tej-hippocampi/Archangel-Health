# PRD: Payments rail, Stripe Connect Express behind a flag (group G)

Ships in PR-3 alongside the task-pipeline work (group D, its own PRD).

## Problem (from the meeting)

Physicians are paid for labeled cases and the meeting treats payouts plus 1099
generation as table stakes ("claimed partially working"). What actually exists
is the ledger half: accrual, quality holds, an admin mark-paid that "records
settled; does not move money" (`backend/routers/asclepius_payments.py:18`).
There is no rail. No money moves, no bank details exist anywhere, and the
physician-facing surface is a disabled "Link your bank account / coming soon"
card (`frontend/asclepius/first_run.js:436-443`) backed by a
`bank_link_status='coming_soon'` interest register
(`backend/routers/asclepius.py:1047-1063`).

The codebase has already committed to the shape of the fix, in the payments
router's header (`asclepius_payments.py:18-22`): the rail will be Stripe
Connect Express, physicians onboard themselves, Stripe holds bank details and
tax ids and files the 1099-NECs, and nothing in this codebase may ever store a
bank account number or a tax id. This PRD builds exactly that commitment.

## Decisions

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
  (`backend/asclepius/payments.py:1658`) stays the source of truth: its
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
  group), and Stripe's idempotency keys make every retry of a partially failed
  batch safe: rows that transferred are no-ops, rows that failed are retried.
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
  before any processing, then are processed and stamped. Replay, out-of-order
  delivery, and crash-mid-handler all resolve to "process each event id at
  most once, from the stored payload". Same reasoning as the notify outboxes:
  durable rows beat in-memory state.
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
- **A3.** `backend/requirements.txt` gains an exact pin in repo style
  (`stripe==<latest 12.x at implementation time>`; this repo pins with `==`,
  and the exact number is chosen when the line is added, not guessed in a PRD).

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
  (`routers/asclepius.py:1055`, "reads this column to find who has been
  waiting"); when the flag flips live, those users get the go-live nudge (see
  email touchpoints).
- **B4.** `frontend/asclepius/first_run.js:436-463`: flag on (surfaced via the
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
  admin-gated, idempotent via the same key, 409 unless the row is settled with
  a failed or missing transfer.

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
  (G7).
- **D4.** Unknown event types are stored and stamped processed with no action:
  a webhook that 500s on novelty gets disabled by Stripe's retry policy.

### E. Schema (additive only, no migration framework exists)

- **E1.** `users.stripe_account_id TEXT` (nullable). `bank_link_status` column
  already exists.
- **E2.** `stripe_webhook_events(event_id TEXT PRIMARY KEY, type TEXT,
  payload_json TEXT, received_at TEXT, processed_at TEXT, outcome TEXT)`.
- **E3.** `stripe_transfers(earning_id TEXT, transfer_id TEXT, status TEXT,
  failure_reason TEXT, payout_batch_id TEXT, created_at TEXT, updated_at
  TEXT)` with a unique index on `earning_id`.
- **E4.** Grep-enforced invariant, tested: no column, code path, or log line
  ever carries a bank account number, routing number, SSN, EIN, or TIN. The
  header comment becomes an assertion.

## What exists today (verified in the working tree)

- `backend/asclepius/payments.py:1-70` module owns money; `:1658` `mark_paid`
  with batch-id idempotency and guarded compare-and-set.
- `backend/routers/asclepius_payments.py:18-22` the DISBURSEMENT SEAM header:
  Stripe Connect Express intent, never store bank details or tax ids; `:579`
  mark-paid; `:908-953` `POST /admin/earnings/pay` (records settled, does not
  move money, equity-only guard via `compensation.accrues_payment`); `:756`
  held-earnings queue pattern; `:830` void 409s on paid.
- `backend/routers/asclepius.py:1047-1063` `POST /me/bank-link/interest`
  writes `bank_link_status='coming_soon'` and notes "The Stripe work lands on
  the payments track and reads this column to find who has been waiting."
- `frontend/asclepius/first_run.js:436-463` the disabled bank card, "coming
  soon" chip, best-effort interest POST.
- `backend/requirements.txt` has no stripe line; `grep -rn ASCLEPIUS_STRIPE
  backend/` returns nothing. This is a green field behind the flag.

## Gaps / changes per file

| File | Change |
|---|---|
| `backend/requirements.txt` | A3 exact `stripe==` pin |
| `backend/asclepius/constants.py` | A1 `stripe_enabled()` |
| `backend/asclepius/stripe_rail.py` (new) | account create + account links, transfer creation, webhook processing, status mapping; the ONLY module that imports stripe (G6) |
| `backend/asclepius/store.py` | E1-E3 additive schema + accessors |
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
  (`first_run.js:443`), so this is honoring recorded copy, not new marketing.
  Idempotent via a stamp, same claim pattern as `onboarding_nudge.py`.
- **Restricted account**: on `account.updated` moving a physician to
  `restricted`, one email telling them Stripe needs more information, linking
  a fresh account link. No repeat sends without a state change.
- **Transfer failure**: admin-facing only (the G4 queue). The physician is not
  emailed about our infrastructure problem.
- 1099 delivery is Stripe's: no email of ours touches tax forms, ever.

## Test plan (plain pytest, WHY docstrings, stripe fully mocked, no network)

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
    not double-pay, which the idempotency key guarantees and the test proves.
  - `test_pay_refused_without_active_bank_link`: WHY: C2; settled-but-unpayable
    must be impossible to create, not merely detectable.
- `test_stripe_webhooks.py`
  - `test_bad_signature_rejected`: WHY: an unsigned webhook is an unauthorized
    ledger-adjacent write path.
  - `test_duplicate_event_id_processed_once`: WHY: G5; Stripe redelivers, and
    at-most-once processing must come from the table, not from hope.
  - `test_no_bank_or_tax_data_ever_stored`: WHY: E4; the invariant that
    justifies delegating 1099s is that we hold nothing worth breaching.

## Out of scope

- Flipping the flag on (user completes Stripe Connect setup and KYC first).
- Refunds, reversals as ledger mutations, clawbacks (G7).
- Health-system payouts (`hs_payouts` stays admin-entry; different rail
  decision for a different counterparty size, likely invoiced ACH).
- Instant payouts, payout scheduling, currency other than USD.
- Any change to accrual, quality holds, or the mark-paid semantics.
- Equity-only contributors (existing guard already refuses pay; unchanged).
