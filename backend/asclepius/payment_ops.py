"""Manually approved payment batches; durable, bounded dispatch and tax cross-checks.

All money execution uses the existing transfer intent/lease/idempotency boundary.
Drafts never settle earnings. Approval freezes a finite list, amount and destination.
Only operational Stripe identifiers are stored here, never tax or bank identities.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import realm
from asclepius import compensation, stripe_rail as rail

log = logging.getLogger(__name__)
PAYOUT_EVENTS = ('payout.created', 'payout.updated', 'payout.paid', 'payout.failed', 'payout.canceled')


class OpsError(ValueError):
    pass


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def enabled():
    return os.getenv('ASCLEPIUS_PAYMENT_OPS_ENABLED', '0') == '1'


def execution_gate():
    if realm.is_sandbox():
        raise OpsError('Payments cannot run in the sandbox realm.')
    if not enabled() or not rail.enabled():
        raise OpsError('Payment processing is disabled. Drafts can still be reviewed.')
    if rail.mode() == 'live' and os.getenv('ASCLEPIUS_PAYMENT_OPS_LIVE_ENABLED', '0') != '1':
        raise OpsError('Live batch execution has not been activated.')
    rail.sdk(need_webhook_secret=True)
    if not rail.connect_webhook_secret():
        raise OpsError('Connected-account webhook signing secret is not configured.')


def init_schema(conn):
    # Additive only; called in the store's existing initialization transaction.
    for sql in (
        '''CREATE TABLE IF NOT EXISTS payment_batches (
            batch_id TEXT PRIMARY KEY, status TEXT NOT NULL, stripe_mode TEXT NOT NULL,
            total_cents INTEGER NOT NULL, fingerprint TEXT NOT NULL,
            created_by TEXT NOT NULL, created_at TEXT NOT NULL,
            approved_by TEXT, approved_at TEXT)''',
        '''CREATE TABLE IF NOT EXISTS payment_batch_items (
            batch_id TEXT NOT NULL, earning_id TEXT NOT NULL, user_id TEXT NOT NULL,
            destination TEXT NOT NULL, amount_cents INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
            lease_token TEXT, last_error TEXT,
            PRIMARY KEY (batch_id, earning_id))''',
        '''CREATE TABLE IF NOT EXISTS stripe_bank_payouts (
            account_id TEXT NOT NULL, payout_id TEXT NOT NULL, stripe_mode TEXT NOT NULL,
            user_id TEXT NOT NULL, amount_cents INTEGER NOT NULL, currency TEXT NOT NULL,
            status TEXT NOT NULL, arrival_date INTEGER, failure_code TEXT,
            event_created INTEGER NOT NULL, observed_at TEXT NOT NULL,
            PRIMARY KEY (account_id, payout_id, stripe_mode))''',
        '''CREATE TABLE IF NOT EXISTS payment_tax_reviews (
            review_id TEXT PRIMARY KEY, tax_year INTEGER NOT NULL, actor_id TEXT NOT NULL,
            reviewed_at TEXT NOT NULL, settings_json TEXT NOT NULL,
            stripe_mode TEXT NOT NULL DEFAULT 'unverified')''',
        '''CREATE TABLE IF NOT EXISTS payment_reconciliations (
            report_id TEXT PRIMARY KEY, tax_year INTEGER NOT NULL, actor_id TEXT NOT NULL,
            created_at TEXT NOT NULL, report_json TEXT NOT NULL)''',
    ):
        conn.execute(sql)
    if 'stripe_mode' not in {r[1] for r in conn.execute('PRAGMA table_info(payment_tax_reviews)')}:
        conn.execute("ALTER TABLE payment_tax_reviews ADD COLUMN stripe_mode TEXT NOT NULL DEFAULT 'unverified'")


def _eligible(conn, earning_id):
    row = conn.execute('SELECT * FROM earnings WHERE earning_id = ?', (earning_id,)).fetchone()
    if not row or row['status'] != 'approved' or row['payout_batch_id'] is not None:
        raise OpsError(f'{earning_id}: earning is no longer approved and unpaid.')
    row = dict(row)
    user = conn.execute('SELECT * FROM users WHERE id = ?', (row['user_id'],)).fetchone()
    user = dict(user) if user else {}
    if not user.get('active') or not compensation.accrues_payment(user):
        raise OpsError(f'{earning_id}: physician is inactive or equity-only.')
    if user.get('bank_link_status') != 'active' or not user.get('stripe_account_id'):
        raise OpsError(f'{earning_id}: physician must complete Stripe onboarding.')
    if row['amount_cents'] <= 0:
        raise OpsError(f'{earning_id}: amount must be positive.')
    return dict(earning_id=earning_id, user_id=row['user_id'],
                destination=user['stripe_account_id'], amount_cents=row['amount_cents'])


def create_batch(store, earning_ids, actor_id):
    if realm.is_sandbox():
        raise OpsError('Payment batches cannot be created in the sandbox realm.')
    if not earning_ids or len(earning_ids) > 200 or len(set(earning_ids)) != len(earning_ids):
        raise OpsError('Select 1–200 distinct earnings.')
    mode = rail.mode()  # Never guess test/live while freezing authorization.
    batch_id = 'pb_' + uuid.uuid4().hex
    with store._conn() as conn:
        store._immediate(conn)
        rows = [_eligible(conn, eid) for eid in sorted(earning_ids)]
        for row in rows:
            reserved = conn.execute('''SELECT 1 FROM payment_batch_items i
                JOIN payment_batches b USING(batch_id) WHERE i.earning_id = ?
                AND b.status IN ('draft', 'approved')''', (row['earning_id'],)).fetchone()
            if reserved:
                raise OpsError(f"{row['earning_id']}: already reserved by another batch.")
        fingerprint = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
        conn.execute('INSERT INTO payment_batches VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)',
                     (batch_id, 'draft', mode, sum(r['amount_cents'] for r in rows),
                      fingerprint, actor_id, now_iso()))
        conn.executemany('''INSERT INTO payment_batch_items
            (batch_id, earning_id, user_id, destination, amount_cents) VALUES (?, ?, ?, ?, ?)''',
            [(batch_id, r['earning_id'], r['user_id'], r['destination'], r['amount_cents']) for r in rows])
    return get_batch(store, batch_id)


def get_batch(store, batch_id):
    with store._conn() as conn:
        row = conn.execute('SELECT * FROM payment_batches WHERE batch_id = ?', (batch_id,)).fetchone()
        if not row:
            raise OpsError('No such payment batch.')
        items = [dict(r) for r in conn.execute('''SELECT i.*, u.email,
            t.transfer_id, t.status AS transfer_status FROM payment_batch_items i
            LEFT JOIN users u ON u.id = i.user_id
            LEFT JOIN stripe_transfers t ON t.earning_id = i.earning_id
            WHERE i.batch_id = ? ORDER BY i.earning_id''', (batch_id,))]
    return {**dict(row), 'items': items}


def approve_batch(store, batch_id, fingerprint, actor_id):
    execution_gate()
    with store._conn() as conn:
        store._immediate(conn)
        b = conn.execute('SELECT * FROM payment_batches WHERE batch_id = ?', (batch_id,)).fetchone()
        if not b or b['fingerprint'] != fingerprint:
            raise OpsError('The reviewed batch does not match. Reload its preview.')
        if b['stripe_mode'] != rail.mode():
            raise OpsError('Stripe mode changed since the preview. Create a new draft.')
        if b['status'] == 'approved':
            return get_batch(store, batch_id)
        if b['status'] != 'draft':
            raise OpsError('Only a draft can be approved.')
        for item in conn.execute('SELECT * FROM payment_batch_items WHERE batch_id = ?', (batch_id,)):
            fresh = _eligible(conn, item['earning_id'])
            if any(fresh[k] != item[k] for k in fresh):
                raise OpsError('An amount or recipient changed. Cancel this draft and create a new one.')
        conn.execute("UPDATE payment_batches SET status = 'approved', approved_by = ?, approved_at = ? WHERE batch_id = ?",
                     (actor_id, now_iso(), batch_id))
    return get_batch(store, batch_id)


def cancel_batch(store, batch_id):
    with store._conn() as conn:
        changed = conn.execute("UPDATE payment_batches SET status = 'canceled' WHERE batch_id = ? AND status = 'draft'", (batch_id,)).rowcount
        if not changed:
            raise OpsError('Only an unapproved draft can be canceled.')
    return get_batch(store, batch_id)


def retry_batch(store, batch_id, fingerprint):
    execution_gate()
    with store._conn() as conn:
        store._immediate(conn)
        b = conn.execute('SELECT * FROM payment_batches WHERE batch_id=?', (batch_id,)).fetchone()
        if not b or b['status'] != 'approved' or b['fingerprint'] != fingerprint or b['stripe_mode'] != rail.mode():
            raise OpsError('Only the reviewed, approved batch in this Stripe environment can be retried.')
        # NEVER resets the intent's first_attempt_at or changes its idempotency key.
        conn.execute("""UPDATE payment_batch_items SET status='queued', attempts=0, next_attempt=0,
            last_error=NULL WHERE batch_id=? AND status='needs_attention' AND lease_until <= ?""", (batch_id, time.time()))
    return get_batch(store, batch_id)


def _reserve_decision(store, item, mode):
    """Validate frozen approval and commit the ledger + intent in ONE transaction.

    Reuse mark_earnings_paid's transaction with an expected snapshot. Nothing
    can swap an amount or destination between validation and intent creation.
    """
    return store.mark_earnings_paid(payout_batch_id=item['batch_id'], paid_at=now_iso(),
        earning_ids=[item['earning_id']], stripe_mode=mode, expected_payment=item)


def run_once(store, *, clock=None):
    execution_gate()
    from routers.asclepius_payments import _transfer_one
    now = time.time() if clock is None else clock
    mode = rail.mode()
    with store._conn() as conn:
        work = [dict(r) for r in conn.execute('''SELECT i.* FROM payment_batch_items i
            JOIN payment_batches b USING(batch_id) WHERE b.status = 'approved'
            AND b.stripe_mode = ? AND i.status IN ('queued', 'retrying', 'processing')
            AND i.next_attempt <= ? AND i.lease_until <= ?
            ORDER BY b.approved_at, i.earning_id LIMIT 200''', (mode, now, now))]
    outcomes = []
    for item in work:
        execution_gate()  # Kill switch applies between individual transfers.
        token = uuid.uuid4().hex
        with store._conn() as conn:
            claimed = conn.execute('''UPDATE payment_batch_items SET lease_token = ?, lease_until = ?,
                status = 'processing', attempts = attempts + 1 WHERE batch_id = ? AND earning_id = ?
                AND status IN ('queued','retrying','processing') AND next_attempt <= ? AND lease_until <= ?''',
                (token, now + 600, item['batch_id'], item['earning_id'], now, now)).rowcount
        if not claimed:
            continue
        status, error = 'retrying', None
        try:
            existing = store.get_stripe_transfer(item['earning_id']) or {}
            if existing.get('transfer_id'):
                status = 'transferred' if existing['status'] == 'transferred' and existing['payout_batch_id'] == item['batch_id'] else 'needs_attention'
                if status == 'needs_attention':
                    error = 'This earning has an existing or reversed transfer. Reconcile it in Stripe.'
            else:
                intent = store.get_stripe_transfer_intent(item['earning_id'])
                replay = intent is not None and intent['first_attempt_at'] is not None
                # A previous attempt might already have spent the funds. Replaying
                # its original key recovers that outcome without requiring new funds.
                if not replay:
                    account = rail.retrieve_account(item['destination'])
                    if not rail._field(account, 'payouts_enabled') or rail._field(rail._field(account, 'capabilities'), 'transfers') != 'active':
                        raise OpsError('Stripe recipient is not ready for transfers and bank payouts.')
                if not replay and rail.available_balance_cents() < item['amount_cents']:
                    error = 'Insufficient available USD balance. Fund Stripe; this approved payment will retry.'
                else:
                    _reserve_decision(store, item, mode)
                    earning = store.get_earning_by_id(item['earning_id'])
                    outcome = _transfer_one(store, earning, actor={'id': 'approved-batch-worker'})
                    status = {'transferred': 'transferred', 'blocked': 'needs_attention',
                              'reversed': 'needs_attention'}.get(outcome['status'], 'retrying')
                    error = outcome.get('failure_reason')
        except (OpsError, ValueError) as exc:
            status, error = 'needs_attention', str(exc)
        except Exception:
            # Unknown transport/DB outcomes are retried only through the original durable intent.
            log.exception('Payment batch dispatch interrupted: %s', item['earning_id'])
            error = 'Dispatch interrupted. Retrying the original payment intent.'
        attempts = item['attempts'] + 1
        if attempts >= 12 and status == 'retrying':
            status, error = 'needs_attention', 'Automatic retry limit reached; reconcile this payment in Stripe.'
        delay = min(3600, 60 * 2 ** min(attempts - 1, 6))
        with store._conn() as conn:
            conn.execute('''UPDATE payment_batch_items SET status = ?, last_error = ?, next_attempt = ?,
                lease_until = 0, lease_token = NULL WHERE batch_id = ? AND earning_id = ? AND lease_token = ?''',
                (status, error, now + delay, item['batch_id'], item['earning_id'], token))
        outcomes.append({'earning_id': item['earning_id'], 'status': status})
    return outcomes


async def worker_loop():
    from asclepius.store import get_store
    while True:
        try:
            if enabled() and rail.enabled():
                with realm.scoped('live'):
                    await asyncio.to_thread(run_once, get_store())
        except Exception:
            log.exception('Approved payment worker needs attention')
        await asyncio.sleep(60)


def record_payout_event(store, event):
    account_id = event.get('account')
    user = store.get_user_by_stripe_account(account_id) if account_id else None
    obj = event.get('object') or {}
    payout_id = obj.get('id')
    if not user or not isinstance(payout_id, str) or not re.fullmatch(r'po_[A-Za-z0-9_]+', payout_id):
        return 'ignored: unknown connected account or payout'
    # Stripe events can arrive out of order; read the current object in account context.
    payout = rail.sdk().Payout.retrieve(payout_id, stripe_account=account_id)
    state = rail._field(payout, 'status')
    if state not in ('pending', 'in_transit', 'paid', 'failed', 'canceled'):
        raise OpsError('Unrecognized Stripe payout status.')
    code = rail._field(payout, 'failure_code')
    if not isinstance(code, str) or not re.fullmatch(r'[a-z_]{1,80}', code):
        code = None
    created = event.get('created') or 0
    with store._conn() as conn:
        store._immediate(conn)
        old = conn.execute('''SELECT * FROM stripe_bank_payouts
            WHERE account_id = ? AND payout_id = ? AND stripe_mode = ?''', (account_id, payout_id, rail.mode())).fetchone()
        if old and ((old['status'] in ('failed', 'canceled') and state not in ('failed', 'canceled')) or
                    (old['status'] == 'paid' and state in ('pending', 'in_transit')) or
                    (old['status'] == 'in_transit' and state == 'pending')):
            return 'ignored: older payout state'
        # The retrieved state is current even if an old event triggered this
        # read. In particular, a bank can fail a payout previously marked paid.
        created = max(created, old['event_created'] if old else 0)
        conn.execute('''INSERT INTO stripe_bank_payouts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id, payout_id, stripe_mode) DO UPDATE SET
            status=excluded.status, arrival_date=excluded.arrival_date, failure_code=excluded.failure_code,
            event_created=excluded.event_created, observed_at=excluded.observed_at''',
            (account_id, payout_id, rail.mode(), user['id'], rail._field(payout, 'amount'),
             rail._field(payout, 'currency'), state, rail._field(payout, 'arrival_date'), code, created, now_iso()))
    return 'bank payout recorded'


def readiness(store, year):
    result = {'rail_enabled': rail.enabled(), 'worker_enabled': enabled(),
              'us_tax_collection_enabled': os.getenv('ASCLEPIUS_US_TAX_COLLECTION_ENABLED', '0') == '1',
              'live_execution_enabled': os.getenv('ASCLEPIUS_PAYMENT_OPS_LIVE_ENABLED', '0') == '1',
              'webhook_configured': bool(rail.webhook_secret()), 'mode': 'unconfigured',
              'connect_webhook_configured': bool(rail.connect_webhook_secret()),
              'stripe_check': 'not_checked', 'available_usd_cents': None,
              'tax_review': None, 'tax_year': year}
    try:
        result['mode'] = rail.mode()
        if rail.enabled() and not realm.is_sandbox():
            acct = rail.sdk().Account.retrieve()
            result.update(stripe_check='verified', account_id=rail._field(acct, 'id'),
                          platform_details_submitted=bool(rail._field(acct, 'details_submitted')),
                          platform_charges_enabled=bool(rail._field(acct, 'charges_enabled')),
                          platform_payouts_enabled=bool(rail._field(acct, 'payouts_enabled')),
                          available_usd_cents=rail.available_balance_cents())
    except Exception:
        result['stripe_check'] = 'unavailable'
    with store._conn() as conn:
        review = conn.execute('SELECT * FROM payment_tax_reviews WHERE tax_year = ? AND stripe_mode = ? ORDER BY reviewed_at DESC LIMIT 1', (year, result['mode'])).fetchone()
        if review:
            result['tax_review'] = {**dict(review), 'settings': json.loads(review['settings_json'])}
            result['tax_review'].pop('settings_json')
    result['dashboard_checks'] = ['Connect platform approval and bank funding eligibility',
        '1099-NEC form and calculation method', 'Tax identity and W-9 collection',
        'Electronic delivery consent and postal fallback', 'Applicable state filing settings']
    return result


def save_tax_review(store, year, actor_id, settings):
    mode = rail.mode()
    with store._conn() as conn:
        conn.execute('INSERT INTO payment_tax_reviews VALUES (?, ?, ?, ?, ?, ?)',
                     ('tax_' + uuid.uuid4().hex, year, actor_id, now_iso(), json.dumps(settings), mode))


def dashboard(store):
    try:
        mode = rail.mode()
    except rail.RailUnavailable:
        mode = 'unconfigured'
    with store._conn() as conn:
        batches = [r[0] for r in conn.execute('SELECT batch_id FROM payment_batches ORDER BY created_at DESC LIMIT 50')]
        payouts = [dict(r) for r in conn.execute('SELECT * FROM stripe_bank_payouts WHERE stripe_mode=? ORDER BY observed_at DESC LIMIT 200', (mode,))]
        tax_accounts = [dict(r) for r in conn.execute("SELECT id, email, stripe_account_id FROM users WHERE stripe_account_id IS NOT NULL ORDER BY email LIMIT 200")]
        eligible = [dict(r) for r in conn.execute('''SELECT e.earning_id, e.user_id, e.amount_cents, e.kind, u.email
            FROM earnings e JOIN users u ON u.id=e.user_id WHERE e.status='approved' AND e.payout_batch_id IS NULL
            AND u.bank_link_status='active' AND u.stripe_account_id IS NOT NULL AND u.active=1
            AND COALESCE(u.compensation_model,'') != 'equity_only'
            AND NOT EXISTS (SELECT 1 FROM payment_batch_items i JOIN payment_batches b USING(batch_id)
                WHERE i.earning_id=e.earning_id AND b.status IN ('draft','approved'))
            ORDER BY e.accrued_at LIMIT 200''')]
    return {'batches': [get_batch(store, bid) for bid in batches], 'bank_payouts': payouts,
            'eligible': eligible, 'tax_accounts': tax_accounts, 'mode': mode}


def parse_totals_csv(content):
    """Deliberately accept a minimal export, so tax IDs never enter persistence."""
    reader = csv.DictReader(io.StringIO(content.lstrip('\ufeff')))
    if reader.fieldnames != ['stripe_account_id', 'amount_usd']:
        raise OpsError('Use exactly these CSV columns: stripe_account_id,amount_usd. Exclude names, addresses and tax IDs.')
    totals = {}
    for row in reader:
        account = row.get('stripe_account_id', '')
        if not re.fullmatch(r'acct_[A-Za-z0-9_]+', account) or account in totals or None in row:
            raise OpsError('Each Stripe account must occur once with a valid account ID.')
        try:
            amount = Decimal(row['amount_usd'])
            cents = amount * 100
            if not amount.is_finite() or cents != cents.to_integral_value() or not 0 <= cents <= 10**12:
                raise ValueError()
        except (InvalidOperation, ValueError, TypeError):
            raise OpsError('Amounts must be nonnegative USD with at most two decimal places.') from None
        totals[account] = int(cents)
    if not totals or len(totals) > 10000:
        raise OpsError('Provide 1–10,000 account totals.')
    return totals


def reconcile(store, year, totals, actor_id):
    """Cross-check only: ledger decision dates are not Stripe tax recognition dates.

    Deliberately reports unknown transfers, reversals and off-Stripe payments
    separately. A matching amount is never described as ready to file.
    """
    mode = rail.mode()
    with store._conn() as conn:
        rows = [dict(r) for r in conn.execute('''SELECT e.*, t.status AS transfer_status, t.transfer_id,
            COALESCE(i.destination,u.stripe_account_id,'unlinked') AS account_id,
            i.stripe_mode FROM earnings e LEFT JOIN stripe_transfers t ON t.earning_id=e.earning_id
            LEFT JOIN stripe_transfer_intents i ON i.earning_id=e.earning_id
            LEFT JOIN users u ON u.id=e.user_id WHERE e.status='paid'
            AND substr(e.resolved_at,1,4)=? AND (i.stripe_mode=? OR i.stripe_mode IS NULL)''', (str(year), mode))]
    accounts = {}
    for row in rows:
        acct = accounts.setdefault(row['account_id'], {'confirmed_transfer_cents': 0,
            'unconfirmed_or_external_cents': 0, 'reversed_cents': 0, 'earning_ids': []})
        acct['earning_ids'].append(row['earning_id'])
        if row['stripe_mode'] != mode:
            acct['unconfirmed_or_external_cents'] += row['amount_cents']
        elif row['transfer_status'] == 'transferred' and row['transfer_id']:
            acct['confirmed_transfer_cents'] += row['amount_cents']
        elif row['transfer_status'] == 'reversed':
            acct['reversed_cents'] += row['amount_cents']
        else:
            acct['unconfirmed_or_external_cents'] += row['amount_cents']
    report_rows = []
    for account in sorted(set(accounts) | set(totals)):
        local = accounts.get(account, {'confirmed_transfer_cents': 0, 'unconfirmed_or_external_cents': 0,
                                      'reversed_cents': 0, 'earning_ids': []})
        stripe_total = totals.get(account)
        delta = None if stripe_total is None else stripe_total - local['confirmed_transfer_cents']
        report_rows.append({'stripe_account_id': account, **local, 'stripe_form_cents': stripe_total,
            'difference_cents': delta, 'status': 'amounts_match' if delta == 0 and
            not local['unconfirmed_or_external_cents'] and not local['reversed_cents'] else 'needs_review'})
    report = {'report_id': 'rec_' + uuid.uuid4().hex, 'tax_year': year, 'stripe_mode': mode,
              'source_totals_sha256': hashlib.sha256(json.dumps(totals, sort_keys=True).encode()).hexdigest(),
              'created_at': now_iso(), 'rows': report_rows, 'filing_ready': False,
              'basis': 'Ledger payment-decision year; confirmed transfers cross-checked against supplied Stripe 1099 totals.',
              'review_required': ['Confirm tax recognition dates in Stripe, especially December/January transfers.',
                  'Reconcile external payments, reversals, adjustments and missing identities.',
                  'Confirm form type, payee tax classification, calculation method, state rules and delivery before filing in Stripe.']}
    with store._conn() as conn:
        conn.execute('INSERT INTO payment_reconciliations VALUES (?, ?, ?, ?, ?)',
                     (report['report_id'], year, actor_id, report['created_at'], json.dumps(report)))
    return report
