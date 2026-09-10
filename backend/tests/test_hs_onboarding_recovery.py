"""Public onboarding recovery and simultaneous one-time credential use."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import asyncio

import httpx

import pytest
from fastapi.testclient import TestClient

from tests import _asclepius as A
from routers import asclepius_provider as P
from asclepius import hs_provisioning

API = "/api/asclepius/hs"
PASSWORD = "harbor-thistle-meadow-41"
EMAIL = "dana@example.org"
NOTIFY_SIGNUP = P._notify_hs_signup


@pytest.fixture()
def setup(monkeypatch):
    store = A.fresh_store()
    monkeypatch.setenv("ASCLEPIUS_PORTAL_BUDGET_MS", "0")
    monkeypatch.setattr(P, "_notify_hs_signup", lambda *a, **kw: None)
    monkeypatch.setattr(P, "is_email_transport_configured", lambda: True)
    sent = []

    async def send(*args, **kwargs):
        sent.append(args)
        return True

    monkeypatch.setattr(P, "send_html_email", send)
    store.create_hs_signup(email=EMAIL, full_name="Dana Reyes", organization="Recovery Health",
                           password=PASSWORD, code="123456")
    return store, TestClient(A.app, base_url="https://testserver"), sent


def test_resend_renews_an_expired_challenge(setup):
    store, client, sent = setup
    with store._conn() as conn:
        conn.execute("UPDATE hs_signups SET expires_at = '2000-01-01T00:00:00+00:00'")
    response = client.post(API + "/signup/resend", json={"email": EMAIL})
    assert response.status_code == 200
    assert len(sent) == 1
    assert store.get_live_hs_signup(EMAIL)


@pytest.mark.parametrize("raises", [False, True])
def test_failed_resend_reports_failure_and_preserves_the_previous_code(setup, monkeypatch, raises):
    store, client, _ = setup

    async def fail(*args, **kwargs):
        if raises:
            raise RuntimeError("transport unavailable")
        return False

    monkeypatch.setattr(P, "send_html_email", fail)
    response = client.post(API + "/signup/resend", json={"email": EMAIL})
    assert response.status_code == 503
    response = client.post(API + "/signup/verify", json={"email": EMAIL, "code": "123456"})
    assert response.status_code == 200


def test_production_resend_without_transport_never_logs_a_code(setup, monkeypatch, caplog):
    _, client, _ = setup
    monkeypatch.setattr(P, "_is_production", lambda: True)
    monkeypatch.setattr(P, "is_email_transport_configured", lambda: False)
    assert client.post(API + "/signup/resend", json={"email": EMAIL}).status_code == 503
    assert "no transport, code" not in caplog.text


def test_initial_send_exception_is_recoverable(setup, monkeypatch):
    store, client, _ = setup

    async def fail(*args, **kwargs):
        raise RuntimeError("transport unavailable")

    monkeypatch.setattr(P, "send_html_email", fail)
    response = client.post(API + "/signup", json={
        "full_name": "Dana", "email": "new@example.org", "organization": "New Health"})
    assert response.status_code == 503
    assert not store.get_live_hs_signup("new@example.org")


def test_signup_send_outage_does_not_exhaust_retries_or_replace_working_code(setup, monkeypatch):
    store, client, _ = setup
    before = store.count_recent_hs_signups_for_email(EMAIL)

    async def fail(*args, **kwargs):
        return False

    monkeypatch.setattr(P, "send_html_email", fail)
    for _ in range(4):
        response = client.post(API + "/signup", json={
            "full_name": "Dana", "email": EMAIL, "organization": "Recovery Health"})
        assert response.status_code == 503
    assert store.count_recent_hs_signups_for_email(EMAIL) == before
    assert client.post(API + "/signup/verify", json={"email": EMAIL, "code": "123456"}).status_code == 200


def test_simultaneous_signup_sends_cannot_exceed_the_address_cap(setup, monkeypatch):
    store, _, _ = setup
    sent = []

    async def send(*args, **kwargs):
        sent.append(args)
        await asyncio.sleep(0.02)
        return True

    monkeypatch.setattr(P, "send_html_email", send)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=A.app), base_url="https://testserver") as client:
            return await asyncio.gather(*(client.post(API + "/signup", json={
                "full_name": "Dana", "email": "simultaneous@example.org", "organization": "Simultaneous Health"
            }) for _ in range(4)))

    responses = asyncio.run(run())
    assert all(r.status_code == 200 for r in responses)
    assert len(sent) == 3
    assert store.count_recent_hs_signups_for_email("simultaneous@example.org") == 3


def test_crashed_signup_delivery_reservation_expires_without_losing_working_code(setup):
    store, client, _ = setup
    assert store.reserve_hs_signup_delivery(EMAIL)
    assert store.reserve_hs_signup_delivery(EMAIL)
    assert store.reserve_hs_signup_delivery(EMAIL) is None
    with store._conn() as conn:
        conn.execute("UPDATE hs_signup_delivery_reservations SET expires_at = '2000-01-01'")
    assert store.reserve_hs_signup_delivery(EMAIL)
    assert client.post(API + "/signup/verify", json={"email": EMAIL, "code": "123456"}).status_code == 200


@pytest.mark.parametrize("email", ["a@", "a@@example.org", "a b@example.org", "a@example.org\nBcc:x@y.org"])
def test_signup_rejects_undeliverable_email_before_staging(setup, email):
    store, client, sent = setup
    response = client.post(API + "/signup", json={
        "full_name": "Dana", "email": email, "organization": "New Health"})
    assert response.status_code == 400
    assert not store.get_live_hs_signup(email)
    assert not sent


def test_simultaneous_verification_creates_only_one_portal(setup, monkeypatch):
    store, _, _ = setup
    original = store.get_live_hs_signup
    barrier = Barrier(2)

    def lookup(email):
        row = original(email)
        barrier.wait(timeout=10)
        return row

    monkeypatch.setattr(store, "get_live_hs_signup", lookup)

    def verify(_):
        with TestClient(A.app, base_url="https://testserver") as client:
            return client.post(API + "/signup/verify", json={"email": EMAIL, "code": "123456"}).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(verify, range(2)))
    assert sorted(statuses) == [200, 400]
    assert len(store.list_health_systems()) == 1
    assert len(store.list_hs_portal_users()) == 1


def test_failed_account_creation_rolls_back_and_leaves_code_retryable(setup):
    store, _, _ = setup
    with store._conn() as conn:
        conn.execute("CREATE TRIGGER fail_signup BEFORE INSERT ON hs_portal_users "
                     "BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END")
    with TestClient(A.app, base_url="https://testserver", raise_server_exceptions=False) as client:
        response = client.post(API + "/signup/verify", json={"email": EMAIL, "code": "123456"})
        assert response.status_code == 503
        assert store.list_health_systems() == []
        assert store.get_live_hs_signup(EMAIL)
        with store._conn() as conn:
            conn.execute("DROP TRIGGER fail_signup")
        assert client.post(API + "/signup/verify", json={"email": EMAIL, "code": "123456"}).status_code == 200


def test_late_wrong_code_does_not_burn_a_successfully_resent_challenge(setup, monkeypatch):
    store, client, _ = setup
    original = store.reject_hs_signup_code
    old = store.get_live_hs_signup(EMAIL)
    with store._conn() as conn:
        conn.execute("UPDATE hs_signups SET attempts = 4 WHERE signup_id = ?", (old["signup_id"],))

    def reject(signup_id, **kwargs):
        assert store.renew_hs_signup_code(signup_id, previous_code_hash=old["code_hash"], code="654321")
        original(signup_id, **kwargs)

    monkeypatch.setattr(store, "reject_hs_signup_code", reject)
    assert client.post(API + "/signup/verify", json={"email": EMAIL, "code": "000000"}).status_code == 400
    assert store.get_live_hs_signup(EMAIL)["attempts"] == 0
    assert client.post(API + "/signup/verify", json={"email": EMAIL, "code": "654321"}).status_code == 200


def test_founder_alert_failure_does_not_suppress_the_access_email(setup, monkeypatch):
    store, _, sent = setup

    def fail(*args, **kwargs):
        raise RuntimeError("notification unavailable")

    monkeypatch.setattr("notifications.notify_founders", fail)
    NOTIFY_SIGNUP(store, "Dana", EMAIL, "Recovery Health", "hs-test", "recovery", [], "claim-token")
    assert len(sent) == 1
    assert sent[0][0] == EMAIL
    assert "your portal access" in sent[0][1]


def test_invalid_teammate_email_is_rejected_before_any_account_is_created(setup):
    store, client, _ = setup
    assert client.post(API + "/signup/verify", json={"email": EMAIL, "code": "123456"}).status_code == 200
    response = client.post(API + "/members", json={"emails": ["colleague@example.org", "bad@"]})
    assert response.status_code == 400
    assert len(store.list_hs_portal_users()) == 1


def test_simultaneous_invite_claim_does_not_overwrite_the_winning_password(setup, monkeypatch):
    store, _, _ = setup
    hs = store.create_health_system_unclaimed("Invite Health", contact_email=EMAIL)
    minted = hs_provisioning.provision_account(store, hs_id=hs["hs_id"], org_name=hs["name"],
                                              email=EMAIL, mint_invite=True)
    original = P._hs_invite_row
    barrier = Barrier(2)

    def lookup(*args):
        row = original(*args)
        barrier.wait(timeout=10)
        return row

    monkeypatch.setattr(P, "_hs_invite_row", lookup)

    def claim(i):
        password = PASSWORD + str(i)
        with TestClient(A.app, base_url="https://testserver") as client:
            response = client.post(API + "/invite/" + minted["invite_token"] + "/claim",
                                   json={"full_name": "Dana", "password": password})
            return response.status_code, password

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, range(2)))
    assert sorted(status for status, _ in results) == [200, 400]
    for status, password in results:
        with TestClient(A.app, base_url="https://testserver") as client:
            response = client.post(API + "/login", json={"username": minted["username"], "password": password})
            assert response.status_code == (200 if status == 200 else 401)
