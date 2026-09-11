"""The approved letter reaches accepted physicians on every entry path."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import tests._asclepius as A
import main
import notifications
import email_utils
from asclepius import store as store_module, verification_agent as agent
from onboarding_emails import build_application_welcome_email
from tests.test_auto_verification import _clean_dossier


def _rows(store, email):
    with store._conn() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM admin_notify_outbox WHERE kind='physician_approved' AND recipient_email=?", (email,))]


def _user(store, password_state='chosen', role='evaluator'):
    user = A.make_user(store, role=role, tier=None, practice_case=False,
                       specialty='nephrology',
                       board_cert='board_certified_nephrology', years_experience=15)
    with store._conn() as conn:
        conn.execute('UPDATE users SET full_name=? WHERE id=?', ('Sarah Patel',user['id']))
    store.set_verification_status(user['id'], 'pending')
    if password_state == 'unset':
        with store._conn() as conn:
            conn.execute('UPDATE users SET password_hash=? WHERE id=?', (store_module.NO_PASSWORD_HASH, user['id']))
    elif password_state == 'temporary':
        store.set_temp_password(user['id'], 'unknown-old-temporary-secret')
    return store.get_user_by_id(user['id'])


@pytest.fixture
def capture_delivery(monkeypatch):
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(email_utils, 'send_html_email', send)
    monkeypatch.setattr(email_utils, 'is_email_transport_configured', lambda: True)
    monkeypatch.setattr(main._realm, 'active_realms', lambda: ['live'])
    return send


@pytest.mark.parametrize('path', ['manual', 'automatic', 'restore'])
@pytest.mark.parametrize('tier', ['labeler', 'reviewer'])
@pytest.mark.parametrize('password_state', ['chosen', 'unset', 'temporary'])
def test_all_acceptance_paths_queue_the_same_letter(path, tier, password_state, monkeypatch, capture_delivery):
    store = A.fresh_store()
    admin = A.make_user(store, role='admin', practice_case=False)
    user = _user(store, password_state, role='admin' if path == 'restore' else 'evaluator')
    original_hash = user['password_hash']
    client = TestClient(A.app)
    if path == 'manual':
        result = client.post(f"/api/asclepius/verify/queue/{user['id']}/approve",
                             json={'tier':tier}, headers=A.headers_for(admin))
        assert result.status_code == 200, result.text
        assert result.json()['welcome_email_queued'] is True
        assert result.json()['welcome_email_sent'] is False
        assert result.json()['welcome_email_retryable'] is False
    elif path == 'restore':
        result = client.post(f"/api/asclepius/admin/physicians/restore?email={user['email']}",
                             json={'approve_verification':True,'tier':tier}, headers=A.headers_for(admin))
        assert result.status_code == 200, result.text
    else:
        # Exercise the real agent delivery path; tier policy is separately
        # covered by test_auto_verification and is not widened by this test.
        monkeypatch.setattr(agent, '_run_registry_check', lambda *args: None)
        monkeypatch.setattr(agent, 'build_dossier', lambda *args: _clean_dossier())
        monkeypatch.setattr(agent, 'decide', lambda d: {'decision':'auto_approve','tier':tier,'recommendation':'Eligible'})
        monkeypatch.setattr(agent, 'auto_approve_enabled', lambda: True)
        assert asyncio.run(agent.run_one(store, {'user_id':user['id']}))['outcome'] == 'auto_approved'
    rows = _rows(store, user['email'])
    assert len(rows) == 1
    assert rows[0]['status'] == 'pending'
    body = rows[0]['body_html']
    assert 'Welcome, Sarah.' in body and 'That’s you.' in body
    assert 'welcome-monogram-v1.png' in body and 'welcome-signature-v1.png' in body
    assert ('Forgot your password?' in body) == (password_state != 'chosen')
    assert 'unknown-old-temporary-secret' not in body
    assert store.get_user_by_id(user['id'])['password_hash'] == original_hash
    assert store.get_user_by_id(user['id'])['tier'] == tier
    asyncio.run(main._drain_admin_notifications())
    sent = [call for call in capture_delivery.await_args_list if call.args[0] == user['email']]
    assert len(sent) == 1 and 'Welcome, Sarah.' in sent[0].args[2]
    assert sent[0].kwargs['importance_headers'] is True
    asyncio.run(main._drain_admin_notifications())
    assert len([call for call in capture_delivery.await_args_list if call.args[0] == user['email']]) == 1


@pytest.mark.parametrize('invalid', ['pending','rejected','inactive','admin','buyer','health_system'])
def test_queued_welcome_is_rechecked_before_delivery(invalid, capture_delivery):
    store = A.fresh_store()
    user = _user(store)
    store.record_verification_decision(user['id'], status='approved', decided_by='admin', tier='reviewer')
    with store._conn() as conn:
        if invalid in ('pending','rejected'):
            conn.execute('UPDATE users SET verification_status=? WHERE id=?', (invalid,user['id']))
        elif invalid == 'inactive':
            conn.execute('UPDATE users SET active=0 WHERE id=?',(user['id'],))
        else:
            conn.execute('UPDATE users SET role=? WHERE id=?',(invalid,user['id']))
    asyncio.run(main._drain_admin_notifications())
    assert not [c for c in capture_delivery.await_args_list if c.args[0] == user['email']]
    assert _rows(store,user['email'])[0]['status'] == 'void'


def test_rejection_cancels_and_reapproval_revives_only_an_unsent_welcome(capture_delivery):
    store = A.fresh_store(); user = _user(store)
    for status in ('approved','rejected','approved'):
        store.record_verification_decision(user['id'],status=status,decided_by='admin',tier='labeler')
        assert _rows(store,user['email'])[0]['status'] == ('void' if status == 'rejected' else 'pending')
    assert len(_rows(store,user['email'])) == 1
    asyncio.run(main._drain_admin_notifications())
    for status in ('rejected','approved'):
        store.record_verification_decision(user['id'],status=status,decided_by='admin',tier='reviewer')
    asyncio.run(main._drain_admin_notifications())
    assert len([c for c in capture_delivery.await_args_list if c.args[0] == user['email']]) == 1


def test_repeated_manual_approvals_and_failed_transport_retry_one_row(monkeypatch, capture_delivery):
    store = A.fresh_store(); user = _user(store)
    admin = A.make_user(store,role='admin',practice_case=False)
    client = TestClient(A.app)
    for _ in range(3):
        assert client.post(f"/api/asclepius/verify/queue/{user['id']}/approve",json={'tier':'reviewer'},headers=A.headers_for(admin)).status_code == 200
    assert len(_rows(store,user['email'])) == 1
    capture_delivery.return_value = False
    asyncio.run(main._drain_admin_notifications())
    assert _rows(store,user['email'])[0]['status'] == 'pending'
    capture_delivery.return_value = True
    asyncio.run(main._drain_admin_notifications())
    row = _rows(store,user['email'])[0]
    assert row['status'] == 'sent' and row['send_attempts'] == 2


def test_reapproval_during_an_inflight_send_does_not_start_a_second_send(capture_delivery):
    store = A.fresh_store(); user = _user(store)
    store.record_verification_decision(user['id'],status='approved',decided_by='admin',tier='reviewer')

    async def scenario():
        started, finish = asyncio.Event(), asyncio.Event()

        async def provider(email, *args, **kwargs):
            if email == user['email']:
                started.set()
                await finish.wait()
            return True

        capture_delivery.side_effect = provider
        first = asyncio.create_task(main._drain_admin_notifications())
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            original_claim = _rows(store,user['email'])[0]['claimed_at']
            for status in ('rejected','approved'):
                store.record_verification_decision(user['id'],status=status,decided_by='admin',tier='reviewer')
            assert _rows(store,user['email'])[0]['claimed_at'] == original_claim
            await asyncio.wait_for(main._drain_admin_notifications(), timeout=5)
        finally:
            finish.set()
            await first
        await main._drain_admin_notifications()

    asyncio.run(scenario())
    assert len([c for c in capture_delivery.await_args_list if c.args[0] == user['email']]) == 1
    row = _rows(store,user['email'])[0]
    assert row['status'] == 'sent' and row['send_attempts'] == 1


def test_manual_retry_revives_welcome_after_reactivation(capture_delivery):
    store = A.fresh_store(); user = _user(store)
    admin = A.make_user(store,role='admin',practice_case=False)
    client = TestClient(A.app)
    route = f"/api/asclepius/verify/queue/{user['id']}/approve"
    assert client.post(route,json={'tier':'reviewer'},headers=A.headers_for(admin)).status_code == 200
    with store._conn() as conn:
        conn.execute('UPDATE users SET active=0 WHERE id=?',(user['id'],))
    asyncio.run(main._drain_admin_notifications())
    assert _rows(store,user['email'])[0]['status'] == 'void'
    assert _rows(store,user['email'])[0]['claimed_at'] is None
    with store._conn() as conn:
        conn.execute('UPDATE users SET active=1 WHERE id=?',(user['id'],))
    response = client.post(route,json={'tier':'reviewer'},headers=A.headers_for(admin))
    assert response.status_code == 200 and response.json()['welcome_email_queued'] is True
    asyncio.run(main._drain_admin_notifications())
    response = client.post(route,json={'tier':'reviewer'},headers=A.headers_for(admin))
    assert response.json()['welcome_email_sent'] is True
    asyncio.run(main._drain_admin_notifications())
    assert len([c for c in capture_delivery.await_args_list if c.args[0] == user['email']]) == 1


@pytest.mark.parametrize('ok', [True,False])
def test_old_sender_cannot_complete_or_release_a_replacement_claim(ok):
    store = A.fresh_store(); user = _user(store)
    store.record_verification_decision(user['id'],status='approved',decided_by='admin',tier='labeler')
    row = next(row for row in store.due_admin_notifications() if row['recipient_email'] == user['email'])
    replacement = '2099-01-01T00:00:00'
    with store._conn() as conn:
        conn.execute('UPDATE admin_notify_outbox SET claimed_at=? WHERE id=?',(replacement,row['id']))
    store.mark_admin_notification_sent(row['id'],ok=ok,claimed_at=row['claimed_at'])
    store.release_admin_notification_claim(row['id'],row['claimed_at'])
    current = _rows(store,user['email'])[0]
    assert current['status'] == 'pending' and current['send_attempts'] == 0
    assert current['claimed_at'] == replacement


def test_accepted_qa_reviewer_role_receives_the_welcome(capture_delivery):
    store = A.fresh_store(); user = _user(store,role='qa_reviewer')
    store.record_verification_decision(user['id'],status='approved',decided_by='admin',tier='reviewer')
    asyncio.run(main._drain_admin_notifications())
    assert len([c for c in capture_delivery.await_args_list if c.args[0] == user['email']]) == 1


def test_legacy_void_after_inline_send_is_not_revived(capture_delivery):
    store = A.fresh_store(); user = _user(store)
    admin = A.make_user(store,role='admin',practice_case=False)
    store.set_verification_status(user['id'],'approved')
    key = notifications._person_key('physician_approved',f"approved:{user['id']}",user['email'])
    store.enqueue_admin_notification(idempotency_key=key,kind='physician_approved',
        subject="You're approved for Archangel Health",body_html='<p>Legacy approval notice</p>',
        recipient_email=user['email'])
    store.void_pending_admin_notification(key)
    original = store.get_admin_notification(key)
    response = TestClient(A.app).post(f"/api/asclepius/verify/queue/{user['id']}/approve",
        json={'tier':'reviewer'},headers=A.headers_for(admin))
    assert response.status_code == 200
    assert not response.json()['welcome_email_queued'] and not response.json()['welcome_email_sent']
    assert response.json()['welcome_email_retryable'] is False
    assert 'previous welcome may already have been sent' in response.json()['warning']
    for status in ('rejected','approved'):
        store.record_verification_decision(user['id'],status=status,decided_by='admin',tier='reviewer')
    asyncio.run(main._drain_admin_notifications())
    assert store.get_admin_notification(key) == original
    assert not [c for c in capture_delivery.await_args_list if c.args[0] == user['email']]


def test_legacy_pending_notice_uses_new_design_and_current_setup_state(capture_delivery):
    store = A.fresh_store(); user = _user(store)
    store.set_verification_status(user['id'],'approved')
    key = notifications._person_key('physician_approved',f"approved:{user['id']}",user['email'])
    store.enqueue_admin_notification(idempotency_key=key,kind='physician_approved',
        subject="You're approved for Archangel Health",body_html='<p>Legacy approval notice</p>',
        recipient_email=user['email'])
    store.set_temp_password(user['id'],'undeliverable-temporary-secret')
    asyncio.run(main._drain_admin_notifications())
    sent = [c for c in capture_delivery.await_args_list if c.args[0] == user['email']]
    assert len(sent) == 1
    assert 'Welcome, Sarah.' in sent[0].args[2] and 'Forgot your password?' in sent[0].args[2]
    assert 'undeliverable-temporary-secret' not in sent[0].args[2]
    assert store.get_admin_notification(key)['status'] == 'sent'


@pytest.mark.parametrize('operation', ['get_admin_notification','revive_unsent_admin_notification'])
def test_outbox_failure_does_not_turn_committed_approval_into_http_error(operation, monkeypatch, capture_delivery):
    store = A.fresh_store(); user = _user(store)
    admin = A.make_user(store,role='admin',practice_case=False)
    original = getattr(store,operation)

    def unavailable(*args, **kwargs):
        raise RuntimeError('outbox unavailable')

    monkeypatch.setattr(store,operation,unavailable)
    client = TestClient(A.app)
    route = f"/api/asclepius/verify/queue/{user['id']}/approve"
    response = client.post(route,json={'tier':'reviewer'},headers=A.headers_for(admin))
    assert response.status_code == 200
    assert response.json()['verification_status'] == 'approved'
    assert 'queue could not be confirmed' in response.json()['warning']
    assert response.json()['welcome_email_retryable'] is True
    monkeypatch.setattr(store,operation,original)
    response = client.post(route,json={'tier':'reviewer'},headers=A.headers_for(admin))
    assert response.status_code == 200 and response.json()['welcome_email_queued'] is True
    asyncio.run(main._drain_admin_notifications())
    assert len([c for c in capture_delivery.await_args_list if c.args[0] == user['email']]) == 1


@pytest.mark.parametrize('current_state', ['approved','rejected','inactive','nonphysician'])
def test_mail_only_retry_preserves_the_current_approval_and_tier(current_state, capture_delivery):
    store = A.fresh_store(); user = _user(store)
    admin = A.make_user(store,role='admin',practice_case=False)
    store.record_verification_decision(user['id'],status='approved',decided_by='first-admin',tier='labeler')
    store.void_pending_admin_notification(_rows(store,user['email'])[0]['idempotency_key'])
    # Another admin may have changed the decision after the first tab opened.
    store.record_verification_decision(user['id'],status='rejected' if current_state == 'rejected' else 'approved',
        decided_by='second-admin',tier='reviewer')
    with store._conn() as conn:
        if current_state == 'inactive':
            conn.execute('UPDATE users SET active=0 WHERE id=?',(user['id'],))
        elif current_state == 'nonphysician':
            conn.execute("UPDATE users SET role='buyer' WHERE id=?",(user['id'],))
    before = store.get_user_by_id(user['id'])
    with store._conn() as conn:
        tables = ['users','events','tiering_decisions']
        snapshots = {t: [tuple(r) for r in conn.execute(f'SELECT * FROM {t}')] for t in tables}
    response = TestClient(A.app).post(f"/api/asclepius/verify/queue/{user['id']}/welcome/retry",
        headers=A.headers_for(admin))
    assert response.status_code == (200 if current_state == 'approved' else 409), response.text
    assert store.get_user_by_id(user['id']) == before
    with store._conn() as conn:
        assert snapshots == {t: [tuple(r) for r in conn.execute(f'SELECT * FROM {t}')] for t in tables}
    asyncio.run(main._drain_admin_notifications())
    assert len([c for c in capture_delivery.await_args_list if c.args[0] == user['email'] and 'Welcome, Sarah.' in c.args[2]]) == (1 if current_state == 'approved' else 0)


def test_mail_only_retry_requires_an_admin(capture_delivery):
    store = A.fresh_store(); user = _user(store)
    store.record_verification_decision(user['id'],status='approved',decided_by='admin',tier='reviewer')
    response = TestClient(A.app).post(f"/api/asclepius/verify/queue/{user['id']}/welcome/retry",
        headers=A.headers_for(user))
    assert response.status_code == 403


@pytest.mark.parametrize('role', ['admin','buyer','health_system'])
def test_nonphysicians_never_queue_a_welcome(role):
    store = A.fresh_store(); user = _user(store)
    with store._conn() as conn:
        conn.execute('UPDATE users SET role=? WHERE id=?',(role,user['id']))
    store.record_verification_decision(user['id'],status='approved',decided_by='admin',tier='reviewer')
    assert _rows(store,user['email']) == []


@pytest.mark.parametrize('name, greeting', [
    ('Sarah Patel','Welcome, Sarah.'), ('Dr. Sarah Patel','Welcome, Sarah.'),
    ('Patel, Sarah','Welcome, Sarah.'), ('Zoë O’Neill','Welcome, Zoë.'),
    ('','Welcome.'), ('<script>alert(1)</script> Patel','Welcome, &lt;script&gt;alert(1)&lt;/script&gt;.'),
])
def test_personalized_email_is_safe_and_contains_real_links(name, greeting, monkeypatch):
    monkeypatch.setenv('BASE_URL','https://api.example.invalid')
    body = build_application_welcome_email(full_name=name,email='doctor@example.invalid',
        sign_in_url='https://api.example.invalid/asclepius?a=1&b=2',calendly_url='https://calendly.com/founders/20')
    assert greeting in body
    assert 'href="https://api.example.invalid/asclepius?a=1&amp;b=2"' in body
    assert 'href="https://calendly.com/founders/20"' in body
    assert 'https://api.example.invalid/email-assets/welcome-monogram-v1.png' in body
    assert '<script' not in body and 'data:image' not in body and '@font-face' not in body
    assert len(body.encode()) < 30_000


def test_email_artwork_is_served_without_signing_in():
    client = TestClient(A.app)
    for name in ('welcome-monogram-v1.png','welcome-signature-v1.png'):
        response = client.get('/email-assets/'+name)
        assert response.status_code == 200
        assert response.headers['content-type'] == 'image/png'
        assert response.content.startswith(b'\x89PNG')


@pytest.mark.parametrize('password_state', ['unset','temporary'])
def test_welcome_setup_instructions_lead_to_a_working_sign_in(password_state, monkeypatch):
    from routers import asclepius as auth_router
    store = A.fresh_store(); user = _user(store,password_state)
    store.record_verification_decision(user['id'],status='approved',decided_by='admin',tier='reviewer')
    assert 'Forgot your password?' in _rows(store,user['email'])[0]['body_html']
    reset_mail = AsyncMock()
    monkeypatch.setattr(auth_router,'_mail_password_reset',reset_mail)
    monkeypatch.setattr(auth_router,'_mail_password_changed',AsyncMock())
    client = TestClient(A.app)
    response = client.post('/api/asclepius/auth/password/forgot',json={'email':user['email']})
    assert response.status_code == 200
    token = reset_mail.await_args.args[1]
    password = 'A-new-secret-password-839!'
    response = client.post('/api/asclepius/auth/password/reset',json={'token':token,'new_password':password})
    assert response.status_code == 200, response.text
    response = client.post('/api/asclepius/auth/login',json={'email':user['email'],'password':password})
    assert response.status_code == 200, response.text
    fresh = store.get_user_by_id(user['id'])
    assert fresh['verification_status'] == 'approved' and not fresh['must_change_password']
