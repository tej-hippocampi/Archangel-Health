"""Money boundary tests: explicit approvals, crashes, funding, duplicates and tax ambiguity."""
import json
import sqlite3
import sys
import types
from concurrent.futures import ThreadPoolExecutor

import pytest
import realm
from asclepius import payment_ops as ops, stripe_rail as rail, store as stores
from tests import _asclepius as A, _stripe_fake, test_stripe_transfers as T, test_stripe_webhooks as W


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    A.fresh_store()
    monkeypatch.setenv('ASCLEPIUS_PAYMENT_OPS_ENABLED', '1')
    monkeypatch.setenv('ASCLEPIUS_PAYMENT_OPS_LIVE_ENABLED', '0')
    monkeypatch.setenv('ASCLEPIUS_US_TAX_COLLECTION_ENABLED', '0')
    monkeypatch.setenv('STRIPE_CONNECT_WEBHOOK_SECRET', 'whsec_test_connect')


@pytest.fixture
def fake(monkeypatch):
    f = _stripe_fake.install(monkeypatch)
    f.available = 1000000
    monkeypatch.setattr(sys.modules['stripe'], 'Balance', types.SimpleNamespace(
        retrieve=lambda: {'available': [{'currency': 'usd', 'amount': f.available}, {'currency': 'eur', 'amount': 999999}]}), raising=False)
    return f


def draft(fake, *, cents=7500, ref='one'):
    doctor = T._doctor(account='acct_' + ref)
    fake.set_account_state(doctor['stripe_account_id'], payouts_enabled=True,
        capabilities={'transfers': 'active'}, country='US')
    earning = T._earning(doctor, ref=ref, cents=cents)
    batch = ops.create_batch(T._store(), [earning['earning_id']], 'admin')
    return doctor, earning, batch


def approve(batch):
    return ops.approve_batch(T._store(), batch['batch_id'], batch['fingerprint'], 'admin')


def item(batch):
    return ops.get_batch(T._store(), batch['batch_id'])['items'][0]


def test_draft_never_moves_money_and_approval_is_finite(fake):
    doctor, earning, batch = draft(fake)
    assert ops.run_once(T._store()) == []
    assert T._store().get_earning_by_id(earning['earning_id'])['status'] == 'approved'
    approve(batch)
    T._earning(doctor, ref='later')
    ops.run_once(T._store())
    ops.run_once(T._store())
    assert fake.settled_transfer_count == 1
    assert T._store().get_earning_by_id('e-later')['status'] == 'approved'
    assert item(batch)['status'] == 'transferred'


@pytest.mark.parametrize('field,value', [('amount_cents', 9000), ('user_id', 'wrong')])
def test_changed_draft_cannot_be_approved(fake, field, value):
    _, earning, batch = draft(fake)
    with T._store()._conn() as conn:
        conn.execute(f'UPDATE earnings SET {field}=? WHERE earning_id=?', (value, earning['earning_id']))
    with pytest.raises(ops.OpsError):
        approve(batch)
    assert fake.settled_transfer_count == 0


def test_post_approval_changes_fail_inside_settlement_transaction(fake):
    _, earning, batch = draft(fake)
    approve(batch)
    with T._store()._conn() as conn:
        conn.execute('UPDATE earnings SET amount_cents=9000 WHERE earning_id=?', (earning['earning_id'],))
    ops.run_once(T._store())
    assert fake.settled_transfer_count == 0
    assert T._store().get_earning_by_id(earning['earning_id'])['status'] == 'approved'
    assert item(batch)['status'] == 'needs_attention'


def test_destination_changes_do_not_redirect_approved_money(fake):
    doctor, _, batch = draft(fake)
    approve(batch)
    with T._store()._conn() as conn:
        conn.execute('UPDATE users SET stripe_account_id=? WHERE id=?', ('acct_attacker', doctor['id']))
    ops.run_once(T._store())
    assert fake.settled_transfer_count == 0
    assert item(batch)['status'] == 'needs_attention'


def test_balance_shortage_preserves_earning_and_retries_after_backoff(fake):
    _, earning, batch = draft(fake)
    approve(batch)
    fake.available = 0
    assert ops.run_once(T._store(), clock=100)[0]['status'] == 'retrying'
    assert T._store().get_stripe_transfer_intent(earning['earning_id']) is None
    assert T._store().get_earning_by_id(earning['earning_id'])['status'] == 'approved'
    fake.available = 100000
    assert ops.run_once(T._store(), clock=159) == []
    assert ops.run_once(T._store(), clock=160)[0]['status'] == 'transferred'
    assert fake.settled_transfer_count == 1


def test_parallel_workers_pay_once(fake):
    _, _, batch = draft(fake)
    approve(batch)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: ops.run_once(T._store()), range(2)))
    assert fake.settled_transfer_count == 1
    assert sum(len(r) for r in results) == 1


def test_response_lost_recovers_without_second_transfer(fake, monkeypatch):
    from tests.test_stripe_transfer_safety import lost_response
    _, _, batch = draft(fake)
    approve(batch)
    remote, _ = lost_response(monkeypatch)
    ops.run_once(T._store(), clock=100)
    assert item(batch)['status'] == 'retrying'
    fake.available = 0  # The remote success already spent the available balance.
    ops.run_once(T._store(), clock=160)
    assert item(batch)['status'] == 'transferred'
    assert len(remote) == 1


def test_bank_dashboard_separates_test_and_live_modes(fake, monkeypatch):
    _, _, _ = draft(fake)
    with T._store()._conn() as conn:
        for mode in ('test', 'live'):
            conn.execute('INSERT INTO stripe_bank_payouts VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                ('acct_one', 'po_' + mode, mode, 'user', 7500, 'usd', 'paid', None, None, 100, ops.now_iso()))
    assert [p['payout_id'] for p in ops.dashboard(T._store())['bank_payouts']] == ['po_test']
    monkeypatch.setenv('STRIPE_SECRET_KEY', 'sk_live_fixture')
    assert [p['payout_id'] for p in ops.dashboard(T._store())['bank_payouts']] == ['po_live']


def test_ambiguous_payment_never_retries_beyond_stripe_window(fake, monkeypatch):
    from tests.test_stripe_transfer_safety import lost_response
    _, earning, batch = draft(fake)
    approve(batch)
    remote, cache = lost_response(monkeypatch)
    ops.run_once(T._store(), clock=100)
    first = T._store().get_stripe_transfer_intent(earning['earning_id'])['first_attempt_at']
    cache.clear()
    monkeypatch.setattr(stores, '_stripe_now', lambda: first + 24 * 3600)
    ops.run_once(T._store(), clock=160)
    assert len(remote) == 1
    assert item(batch)['status'] == 'needs_attention'


def test_database_failure_before_intent_commit_moves_no_money(fake, monkeypatch):
    _, earning, batch = draft(fake)
    approve(batch)
    with T._store()._conn() as conn:
        conn.execute("CREATE TRIGGER test_no_intent BEFORE INSERT ON stripe_transfer_intents BEGIN SELECT RAISE(ABORT, 'disk full'); END")
    ops.run_once(T._store())
    assert fake.settled_transfer_count == 0
    assert T._store().get_earning_by_id(earning['earning_id'])['status'] == 'approved'


def test_crash_after_ledger_commit_resumes_original_intent(fake, monkeypatch):
    _, earning, batch = draft(fake)
    approve(batch)
    original = ops._reserve_decision
    def die(store, i, mode):
        original(store, i, mode)
        raise RuntimeError('process interrupted after commit')
    monkeypatch.setattr(ops, '_reserve_decision', die)
    ops.run_once(T._store(), clock=100)
    intent = T._store().get_stripe_transfer_intent(earning['earning_id'])
    assert intent and fake.settled_transfer_count == 0
    monkeypatch.setattr(ops, '_reserve_decision', original)
    ops.run_once(T._store(), clock=160)
    assert fake.settled_transfer_count == 1
    assert T._store().get_stripe_transfer_intent(earning['earning_id'])['intent_id'] == intent['intent_id']


def test_cancel_and_overlapping_drafts(fake):
    _, earning, batch = draft(fake)
    with pytest.raises(ops.OpsError, match='reserved'):
        ops.create_batch(T._store(), [earning['earning_id']], 'other-admin')
    ops.cancel_batch(T._store(), batch['batch_id'])
    with pytest.raises(ops.OpsError):
        approve(batch)
    assert ops.create_batch(T._store(), [earning['earning_id']], 'admin')['status'] == 'draft'


def test_live_disabled_and_mode_switch_blocks_approval(fake, monkeypatch):
    _, _, batch = draft(fake)
    monkeypatch.setenv('STRIPE_SECRET_KEY', 'sk_live_fixture')
    with pytest.raises(ops.OpsError, match='not been activated'):
        approve(batch)
    monkeypatch.setenv('ASCLEPIUS_PAYMENT_OPS_LIVE_ENABLED', '1')
    with pytest.raises(ops.OpsError, match='mode changed'):
        approve(batch)
    assert fake.settled_transfer_count == 0


def test_kill_switch_and_sandbox_block_worker(fake, monkeypatch):
    _, _, batch = draft(fake)
    approve(batch)
    monkeypatch.setenv('ASCLEPIUS_PAYMENT_OPS_ENABLED', '0')
    with pytest.raises(ops.OpsError):
        ops.run_once(T._store())
    monkeypatch.setenv('ASCLEPIUS_PAYMENT_OPS_ENABLED', '1')
    with realm.scoped('sandbox'), pytest.raises(ops.OpsError):
        ops.run_once(T._store())
    assert fake.settled_transfer_count == 0


def test_admin_api_denies_non_admin_and_requires_matching_fingerprint(fake):
    _, _, batch = draft(fake)
    assert T.client.get('/api/asclepius/admin/payment-ops').status_code == 401
    user = A.make_user(T._store())
    assert T.client.get('/api/asclepius/admin/payment-ops', headers=A.headers_for(user)).status_code == 403
    headers = T._admin()
    response = T.client.post('/api/asclepius/admin/payment-ops/batches/' + batch['batch_id'] + '/approve',
        headers=headers, json={'fingerprint': '0' * 64})
    assert response.status_code == 409
    assert fake.settled_transfer_count == 0


def test_payout_events_are_account_scoped_allowlisted_and_cannot_regress(fake, monkeypatch):
    doctor, _, _ = draft(fake)
    current = {'id': 'po_one', 'amount': 7500, 'currency': 'usd', 'status': 'paid',
               'arrival_date': 1800000000, 'failure_message': 'PRIVATE', 'destination': {'account_number': 'PRIVATE'}}
    calls = []
    def retrieve(pid, **kw):
        calls.append((pid, kw))
        return current
    monkeypatch.setattr(sys.modules['stripe'], 'Payout', types.SimpleNamespace(retrieve=retrieve), raising=False)
    def post(eid, status, created):
        body = json.loads(_stripe_fake.event_body(eid, 'payout.' + status, current))
        body.update(account=doctor['stripe_account_id'], created=created)
        return W._post(json.dumps(body).encode())
    assert post('evt_paid', 'paid', 200).status_code == 200
    assert calls[0] == ('po_one', {'stripe_account': doctor['stripe_account_id']})
    current['status'] = 'pending'
    assert post('evt_old', 'created', 100).status_code == 200
    assert ops.dashboard(T._store())['bank_payouts'][0]['status'] == 'paid'
    current.update(status='failed', failure_code='account_closed')
    assert post('evt_fail', 'failed', 50).status_code == 200  # old event, current authoritative failure
    assert ops.dashboard(T._store())['bank_payouts'][0]['status'] == 'failed'
    saved = T._store().get_stripe_webhook_event('evt_paid')
    assert 'PRIVATE' not in saved['payload_json']
    assert 'destination' not in saved['payload_json']
    assert len(T._store().list_earnings(status='paid')) == 0
    assert post('evt_fail', 'failed', 50).json()['duplicate'] is True


def test_unknown_account_payout_does_not_call_stripe(fake, monkeypatch):
    monkeypatch.setattr(sys.modules['stripe'], 'Payout', None, raising=False)
    assert ops.record_payout_event(T._store(), {'account': 'acct_foreign', 'object': {'id': 'po_other'}}).startswith('ignored')


@pytest.mark.parametrize('csv', [
    'stripe_account_id,amount_usd,tin\nacct_one,50,PRIVATE',
    'stripe_account_id,amount_usd\nacct_one,NaN',
    'stripe_account_id,amount_usd\nacct_one,-1',
    'stripe_account_id,amount_usd\nacct_one,1.001',
    'stripe_account_id,amount_usd\nacct_one,1\nacct_one,2',
])
def test_reconciliation_rejects_unsafe_or_ambiguous_csv(csv):
    with pytest.raises(ops.OpsError):
        ops.parse_totals_csv(csv)


def test_reconciliation_separates_unconfirmed_and_never_claims_filing_readiness(fake):
    doctor, earning, batch = draft(fake)
    approve(batch)
    ops.run_once(T._store())
    with T._store()._conn() as conn:
        conn.execute("UPDATE earnings SET resolved_at='2026-12-31T23:59:59+00:00' WHERE earning_id=?", (earning['earning_id'],))
    other = T._earning(doctor, ref='external', cents=1000, status='paid')
    with T._store()._conn() as conn:
        conn.execute("UPDATE earnings SET resolved_at='2026-01-01T00:00:00' WHERE earning_id=?", (other['earning_id'],))
    totals = ops.parse_totals_csv('stripe_account_id,amount_usd\nacct_one,75.00')
    report = ops.reconcile(T._store(), 2026, totals, 'admin')
    assert report['rows'][0]['difference_cents'] == 0
    assert report['rows'][0]['unconfirmed_or_external_cents'] == 1000
    assert report['rows'][0]['status'] == 'needs_review'
    assert report['filing_ready'] is False
    next_year = ops.reconcile(T._store(), 2027, totals, 'admin')
    assert next_year['rows'][0]['confirmed_transfer_cents'] == 0


def test_tax_collection_is_us_only_and_does_not_file(fake, monkeypatch):
    fake.set_account_state('acct_us', country='US')
    fake.set_account_state('acct_ca', country='CA')
    calls = []
    monkeypatch.setattr(sys.modules['stripe'].Account, 'modify', lambda a, **kw: calls.append((a, kw)), raising=False)
    rail.enable_us_tax_collection('acct_us')
    assert calls == [('acct_us', {'capabilities': {'tax_reporting_us_1099_misc': {'requested': True}}})]
    with pytest.raises(rail.RailUnavailable):
        rail.enable_us_tax_collection('acct_ca')


def test_onboarding_does_not_force_physician_to_individual_tax_classification(fake):
    rail.create_express_account(email='doctor@example.test')
    assert 'business_type' not in fake.account_create_calls[0]


def test_readiness_failure_is_unknown_not_zero(fake, monkeypatch):
    monkeypatch.setattr(sys.modules['stripe'].Account, 'retrieve', lambda: (_ for _ in ()).throw(TimeoutError()))
    result = ops.readiness(T._store(), 2026)
    assert result['stripe_check'] == 'unavailable'
    assert result['available_usd_cents'] is None


def test_tax_attestations_do_not_cross_stripe_modes(fake, monkeypatch):
    ops.save_tax_review(T._store(), 2026, 'admin', {'calculation_method': 'payments_including_fees'})
    assert ops.readiness(T._store(), 2026)['tax_review']['stripe_mode'] == 'test'
    monkeypatch.setenv('STRIPE_SECRET_KEY', 'sk_live_fixture')
    assert ops.readiness(T._store(), 2026)['tax_review'] is None


def test_both_event_destinations_are_verified(fake):
    body = _stripe_fake.event_body('evt_connect', 'payout.created', {'id': 'po_unknown'})
    assert W._post(body, secret='whsec_test_connect').status_code == 200
    assert W._post(body, secret='whsec_wrong').status_code == 400


def test_missing_connect_webhook_blocks_approval(fake, monkeypatch):
    _, _, batch = draft(fake)
    monkeypatch.delenv('STRIPE_CONNECT_WEBHOOK_SECRET')
    with pytest.raises(ops.OpsError, match='Connected-account webhook'):
        approve(batch)


def test_unconfigured_tax_review_returns_actionable_conflict(fake, monkeypatch):
    headers = T._admin()
    monkeypatch.delenv('STRIPE_SECRET_KEY')
    response = T.client.post('/api/asclepius/admin/payment-ops/tax-review', headers=headers, json={
        'tax_year': 2026, 'calculation_method': 'payments_including_fees',
        'platform_and_funding_checked': False, 'tax_identity_and_w9_checked': False,
        'delivery_and_consent_checked': False, 'state_filing_checked': False})
    assert response.status_code == 409
