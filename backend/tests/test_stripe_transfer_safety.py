"""Money safety at remote-success/local-failure, replay and privacy boundaries.

All amounts/identities are synthetic. No Stripe network calls are made.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import importlib
import json
import sqlite3
import sys
import threading
import time

import pytest

from tests import _asclepius as A, _stripe_fake
from tests import test_stripe_transfers as T, test_stripe_webhooks as W
from asclepius import store as store_module, stripe_rail
from routers import asclepius_payments as routes


@pytest.fixture(autouse=True)
def isolated():
    A.fresh_store()


@pytest.fixture
def fake(monkeypatch):
    return _stripe_fake.install(monkeypatch)


def lost_response(monkeypatch):
    """Stripe commits the transfer, but the app never receives its response."""
    remote, cache = [], {}

    def create(*, idempotency_key, **params):
        if idempotency_key in cache:
            return cache[idempotency_key]
        obj = {'id': f'tr_remote_{len(remote) + 1}', 'reversed': False, **params}
        remote.append(obj)
        cache[idempotency_key] = obj
        raise TimeoutError('Synthetic response lost after remote commit')

    monkeypatch.setattr(sys.modules['stripe'].Transfer, 'create', create)
    return remote, cache


def pay_one(ref='safety'):
    doctor, admin = T._doctor(), T._admin()
    earning = T._earning(doctor, ref=ref)
    result = T._pay(admin, doctor, 'batch-' + ref)
    assert result.status_code == 200, result.text
    return doctor, admin, earning, result


def retry(admin, earning):
    return T.client.post(f"/api/asclepius/admin/earnings/{earning['earning_id']}/retry-transfer", headers=admin)


def test_lost_response_cannot_duplicate_after_stripe_prunes_key(fake, monkeypatch):
    remote, cache = lost_response(monkeypatch)
    doctor, admin, earning, result = pay_one()
    assert result.json()['transfers'][0]['status'] == 'failed'
    first = T._store().get_stripe_transfer_intent(earning['earning_id'])['first_attempt_at']
    cache.clear()  # Stripe no longer promises deduplication after 24 hours.
    monkeypatch.setattr(store_module, '_stripe_now', lambda: first + 25 * 3600)
    response = retry(admin, earning)
    assert response.status_code == 200
    assert response.json()['ok'] is False
    assert response.json()['transfer']['status'] == 'blocked'
    assert 'window has closed' in response.json()['transfer']['failure_reason']
    assert T._pay(admin, doctor, 'batch-safety').status_code == 200
    assert len(remote) == 1
    assert T._store().get_earning_by_id(earning['earning_id'])['status'] == 'paid'


def test_webhook_recovers_response_loss_even_after_retry_window(fake, monkeypatch):
    remote, _ = lost_response(monkeypatch)
    doctor, admin, earning, _ = pay_one()
    first = T._store().get_stripe_transfer_intent(earning['earning_id'])['first_attempt_at']
    monkeypatch.setattr(store_module, '_stripe_now', lambda: first + 25 * 3600)
    assert W._post(_stripe_fake.event_body('evt_recover', 'transfer.created', remote[0])).status_code == 200
    saved = T._store().get_stripe_transfer(earning['earning_id'])
    assert saved['transfer_id'] == remote[0]['id'] and saved['status'] == 'transferred'
    assert retry(admin, earning).status_code == 409
    assert T._pay(admin, doctor, 'batch-safety').json()['transfers'] == []
    assert len(remote) == 1


@pytest.mark.parametrize('field,value', [
    ('amount', 1), ('currency', 'eur'), ('destination', 'acct_wrong'),
    ('transfer_group', 'wrong-batch'), ('transfer_intent_id', 'wrong-intent'),
    ('asclepius_user_id', 'wrong-user'), ('earning_id', 'wrong-earning'),
])
def test_recovery_requires_every_original_request_field(fake, monkeypatch, field, value):
    remote, _ = lost_response(monkeypatch)
    _, _, earning, _ = pay_one()
    obj = json.loads(json.dumps(remote[0]))
    target = obj['metadata'] if field in obj['metadata'] else obj
    target[field] = value
    assert W._post(_stripe_fake.event_body('evt_mismatch', 'transfer.created', obj)).status_code == 200
    saved = T._store().get_stripe_transfer(earning['earning_id'])
    assert saved['transfer_id'] is None and saved['status'] == 'failed'


@pytest.mark.parametrize('age', [23 * 3600, 48 * 3600, -1])
def test_retry_boundary_and_backward_clock_fail_closed(fake, monkeypatch, age):
    remote, _ = lost_response(monkeypatch)
    _, admin, earning, _ = pay_one()
    first = T._store().get_stripe_transfer_intent(earning['earning_id'])['first_attempt_at']
    monkeypatch.setattr(store_module, '_stripe_now', lambda: first + age)
    assert retry(admin, earning).json()['transfer']['status'] == 'blocked'
    assert len(remote) == 1


def test_retry_freezes_amount_destination_and_metadata(fake, monkeypatch):
    remote, _ = lost_response(monkeypatch)
    doctor, admin, earning, _ = pay_one()
    with T._store()._conn() as conn:
        conn.execute('UPDATE earnings SET amount_cents = 9900 WHERE earning_id = ?', (earning['earning_id'],))
        conn.execute('UPDATE users SET stripe_account_id = ? WHERE id = ?', ('acct_changed', doctor['id']))
    assert retry(admin, earning).json()['ok'] is True
    assert len(remote) == 1
    assert remote[0]['amount'] == 7500 and remote[0]['destination'] == 'acct_linked'


def test_parallel_attempts_dispatch_once(fake, monkeypatch):
    doctor, admin = T._doctor(), T._admin()
    earning = T._earning(doctor, ref='parallel')
    from asclepius import payments
    payments.mark_paid(T._store(), payout_batch_id='batch-parallel', user_id=doctor['id'])
    saved = T._store().get_earning_by_id(earning['earning_id'])
    entered, release = threading.Event(), threading.Event()
    original = sys.modules['stripe'].Transfer.create

    def create(**params):
        entered.set()
        assert release.wait(5)
        return original(**params)

    monkeypatch.setattr(sys.modules['stripe'].Transfer, 'create', create)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(routes._transfer_one, T._store(), saved, actor=None)
        assert entered.wait(5)
        try:
            second = pool.submit(routes._transfer_one, T._store(), saved, actor=None).result(5)
            assert second['status'] == 'pending'
        finally:
            release.set()
        assert first.result(5)['status'] == 'transferred'
    assert fake.settled_transfer_count == 1 and len(fake.transfer_calls) == 1


def test_crash_after_paid_commit_leaves_an_unsent_recoverable_intent(fake):
    doctor, admin = T._doctor(), T._admin()
    earning = T._earning(doctor, ref='commit')
    from asclepius import payments
    payments.mark_paid(T._store(), payout_batch_id='batch-commit', user_id=doctor['id'])
    assert T._store().get_stripe_transfer_intent(earning['earning_id'])['first_attempt_at'] is None
    assert retry(admin, earning).json()['ok'] is True
    assert fake.settled_transfer_count == 1


def test_intent_storage_failure_rolls_back_paid_decision(fake):
    doctor = T._doctor()
    earning = T._earning(doctor, ref='disk')
    with T._store()._conn() as conn:
        conn.execute("CREATE TRIGGER fail_intent BEFORE INSERT ON stripe_transfer_intents "
                     "BEGIN SELECT RAISE(ABORT, 'synthetic disk full'); END")
    from asclepius import payments
    with pytest.raises(sqlite3.IntegrityError, match='synthetic disk full'):
        payments.mark_paid(T._store(), payout_batch_id='batch-disk', user_id=doctor['id'])
    assert T._store().get_earning_by_id(earning['earning_id'])['status'] == 'approved'
    assert fake.transfer_calls == []


def test_crash_after_remote_commit_is_recovered_by_webhook(fake, monkeypatch):
    doctor = T._doctor()
    earning = T._earning(doctor, ref='record-failure')
    from asclepius import payments
    payments.mark_paid(T._store(), payout_batch_id='batch-record', user_id=doctor['id'])
    saved = T._store().get_earning_by_id(earning['earning_id'])
    with monkeypatch.context() as patch:
        def fail(**kwargs):
            raise sqlite3.OperationalError('synthetic database unavailable')
        patch.setattr(T._store(), 'record_stripe_transfer', fail)
        with pytest.raises(sqlite3.OperationalError):
            routes._transfer_one(T._store(), saved, actor=None)
    obj = next(iter(fake.transfers.values()))
    assert W._post(_stripe_fake.event_body('evt_db_recovery', 'transfer.created', obj)).status_code == 200
    assert T._store().get_stripe_transfer(earning['earning_id'])['transfer_id'] == obj['id']
    assert routes._transfer_one(T._store(), saved, actor=None)['status'] == 'transferred'
    assert len(fake.transfer_calls) == 1


@pytest.mark.parametrize('existing_attempt', [False, True])
def test_legacy_paid_rows_are_not_assumed_unsent(fake, existing_attempt):
    doctor, admin = T._doctor(), T._admin()
    earning = T._earning(doctor, ref='legacy', status='paid')
    if existing_attempt:
        T._store().record_stripe_transfer(earning_id=earning['earning_id'], status='failed')
    response = retry(admin, earning)
    assert response.json()['transfer']['status'] == 'blocked'
    assert 'Legacy payment' in response.json()['transfer']['failure_reason']
    assert fake.transfer_calls == []


@pytest.mark.parametrize('event_type', ['account.updated', 'transfer.created', 'billing.alert.triggered'])
def test_webhook_persists_only_allowlisted_fields(fake, event_type):
    obj = {'id': 'acct_fixture' if event_type == 'account.updated' else 'tr_fixture',
           'payouts_enabled': True, 'reversed': False,
           'individual': {'dob': {'year': 1980}, 'address': {'line1': 'PRIVATE_SENTINEL'}},
           'metadata': {'notes': 'PRIVATE_SENTINEL'}, 'description': 'PRIVATE_SENTINEL',
           'failure_message': 'PRIVATE_SENTINEL', 'requirements': {'disabled_reason': 'PRIVATE_SENTINEL'},
           'external_accounts': {'data': [{'routing_number': 'PRIVATE_SENTINEL'}]}}
    assert W._post(_stripe_fake.event_body('evt_private', event_type, obj)).status_code == 200
    row = T._store().get_stripe_webhook_event('evt_private')
    assert 'PRIVATE_SENTINEL' not in json.dumps(row)
    data = json.loads(row['payload_json'])
    assert set(data) <= {'id', 'payouts_enabled', 'restricted', 'reversed'}


def test_reversal_survives_old_events_and_late_request_results(fake):
    doctor, admin, earning, _ = pay_one('reverse')
    obj = next(iter(fake.transfers.values()))
    reversed_obj = {**obj, 'reversed': True}
    assert W._post(_stripe_fake.event_body('evt_reverse', 'transfer.reversed', reversed_obj)).status_code == 200
    assert W._post(_stripe_fake.event_body('evt_old', 'transfer.created', obj)).status_code == 200
    for status in ('transferred', 'failed', 'blocked'):
        T._store().record_stripe_transfer(earning_id=earning['earning_id'], status=status, transfer_id=obj['id'])
    assert T._store().get_stripe_transfer(earning['earning_id'])['status'] == 'reversed'
    assert retry(admin, earning).status_code == 409
    assert T._pay(admin, doctor, 'batch-reverse').json()['transfers'] == []
    assert fake.settled_transfer_count == 1


def test_reversed_webhook_before_response_is_not_overwritten(fake, monkeypatch):
    original = sys.modules['stripe'].Transfer.create
    def create(**params):
        obj = original(**params)
        assert W._post(_stripe_fake.event_body('evt_early_reverse', 'transfer.reversed',
                                              {**obj, 'reversed': True})).status_code == 200
        return obj
    monkeypatch.setattr(sys.modules['stripe'].Transfer, 'create', create)
    _, _, earning, result = pay_one('early')
    assert result.json()['transfers'][0]['status'] == 'reversed'
    assert T._store().get_stripe_transfer(earning['earning_id'])['status'] == 'reversed'


def test_wrong_environment_webhook_is_rejected_without_storage(fake):
    event = json.loads(_stripe_fake.event_body('evt_live', 'account.updated', {'id': 'acct_hooked'}))
    event['livemode'] = True
    assert W._post(json.dumps(event).encode()).status_code == 400
    assert T._store().get_stripe_webhook_event('evt_live') is None


@pytest.mark.parametrize('metadata', [[], ['invalid'], {'earning_id': []}, {'earning_id': {}}, {'earning_id': None}])
def test_malformed_recovery_metadata_is_ignored(fake, metadata):
    body = _stripe_fake.event_body('evt_malformed', 'transfer.created', {'id': 'tr_unknown', 'metadata': metadata})
    assert W._post(body).status_code == 200
    assert T._store().get_stripe_webhook_event('evt_malformed')['outcome'] == 'ignored: unknown transfer'


def test_reversed_api_response_can_advance_a_concurrent_created_webhook(fake):
    store = T._store()
    store.record_stripe_transfer(earning_id='e_race', transfer_id='tr_same', status='transferred')
    store.record_stripe_transfer(earning_id='e_race', transfer_id='tr_foreign', status='reversed')
    assert store.get_stripe_transfer('e_race')['status'] == 'transferred'
    store.record_stripe_transfer(earning_id='e_race', transfer_id='tr_same', status='reversed')
    assert store.get_stripe_transfer('e_race')['status'] == 'reversed'


def test_real_sdk_accepts_valid_signature_and_rejects_tampering(monkeypatch):
    """The simplified fake verifier cannot validate the real Stripe HMAC protocol."""
    # Register restoration BEFORE importing, so this test does not leak the
    # optional SDK into tests asserting the flag-off lazy-import boundary.
    monkeypatch.setitem(sys.modules, 'stripe', sys.modules.get('stripe'))
    sys.modules.pop('stripe', None)
    actual_sdk = importlib.import_module('stripe')
    monkeypatch.setenv('ASCLEPIUS_STRIPE_ENABLED', '1')
    monkeypatch.setenv('STRIPE_SECRET_KEY', 'sk_test_local_signature_fixture')
    secret = 'whsec_local_signature_fixture'
    monkeypatch.setenv('STRIPE_WEBHOOK_SECRET', secret)
    body = _stripe_fake.event_body('evt_real_signature', 'transfer.created', {'id': 'tr_local'})
    now = str(int(time.time()))
    digest = hmac.new(secret.encode(), now.encode() + b'.' + body, hashlib.sha256).hexdigest()
    header = f't={now},v1={digest}'
    assert actual_sdk.Webhook is not None
    assert W._post(body, signature=header).status_code == 200
    assert W._post(body + b' ', signature=header).status_code == 400
