"""Self-serve physician onboarding links (POST /api/onboarding/self-serve).

The public endpoint mints the same magic link the admin "Generate Health
System Link" button issues, with layered spam guards. Self-contained: mounts
just the onboarding router on a throwaway TeamStore (same pattern as
test_leads.py).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("EMAIL_DEV_MODE", "1")  # send_html_email -> success, no network
os.environ.setdefault("RATE_LIMIT_ENABLED", "0")

from routers.onboarding import router as onboarding_router  # noqa: E402
from team_store import TeamStore  # noqa: E402


@pytest.fixture()
def store(tmp_path):
    return TeamStore(db_path=str(tmp_path / "selfserve.db"))


@pytest.fixture()
def client(store):
    app = FastAPI()
    app.state.team_store = store
    app.include_router(onboarding_router)
    with TestClient(app) as c:
        yield c


def _rows(store):
    with store._conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM health_systems").fetchall()]


def test_self_serve_creates_pending_invite(client, store):
    r = client.post("/api/onboarding/self-serve", json={"email": "doc@hospital.org"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "/onboard/" in body["onboarding_url"]

    rows = _rows(store)
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "pending_onboarding"
    assert row["director_email"] == "doc@hospital.org"
    assert int(row["onboarding_step"] or 0) == 0  # wizard still runs identity + OTP

    # Self-serve links expire in ~7 days, not the admin default 30.
    exp = datetime.fromisoformat(row["onboarding_token_expires_at"])
    assert exp < datetime.utcnow() + timedelta(days=8)
    assert exp > datetime.utcnow() + timedelta(days=6)


def test_self_serve_link_opens_a_valid_wizard_session(client):
    r = client.post("/api/onboarding/self-serve", json={"email": "doc@hospital.org"})
    token = r.json()["onboarding_url"].rsplit("/onboard/", 1)[1]

    s = client.get("/api/onboarding/session", params={"token": token})
    assert s.status_code == 200
    session = s.json()
    assert session["status"] == "pending"
    assert session["step"] == 0
    assert session["director_email"] == "doc@hospital.org"


def test_join_extras_prefill_identity_and_flavor(client, store):
    """/join passes names + flavor; the wizard session hydrates them so the
    signer types nothing twice, and the flavor rides the row."""
    r = client.post("/api/onboarding/self-serve", json={
        "email": "adv@example.org", "first_name": "Robin",
        "last_name": "Ellis", "flavor": "general"})
    assert r.status_code == 200
    token = r.json()["onboarding_url"].rsplit("/onboard/", 1)[1]
    s = client.get("/api/onboarding/session", params={"token": token}).json()
    assert s["director_first_name"] == "Robin"
    assert s["director_last_name"] == "Ellis"
    assert s["signup_flavor"] == "general"


def test_a_plain_signup_carries_no_flavor(client):
    r = client.post("/api/onboarding/self-serve", json={"email": "doc@hospital.org"})
    token = r.json()["onboarding_url"].rsplit("/onboard/", 1)[1]
    s = client.get("/api/onboarding/session", params={"token": token}).json()
    assert s["signup_flavor"] is None


def test_an_unknown_flavor_is_stored_as_nothing(client, store):
    r = client.post("/api/onboarding/self-serve",
                    json={"email": "doc2@hospital.org", "flavor": "superuser"})
    assert r.status_code == 200
    row = [x for x in _rows(store) if x["director_email"] == "doc2@hospital.org"][0]
    assert row["signup_flavor"] is None


def test_a_referral_code_never_breaks_the_mint(client):
    """The asclepius store is not mounted on this throwaway app, so the
    attribution import path fails internally; the link must mint anyway."""
    r = client.post("/api/onboarding/self-serve",
                    json={"email": "doc3@hospital.org", "referral_code": "DRCHEN99"})
    assert r.status_code == 200
    assert "/onboard/" in r.json()["onboarding_url"]


@pytest.mark.parametrize("email", [
    "doctor@aiimsjodhpur.edu.in", "doctor@dpu.edu.in",
    "doctor@atriushealth.org", "doctor@gmail.com",
])
@pytest.mark.parametrize("hidden_value", ["", "https://hospital.example", "saved profile"])
def test_autofill_and_international_email_return_real_invites(client, store, email, hidden_value):
    """The reported production failure must open a persisted wizard session."""
    r = client.post("/api/onboarding/self-serve", json={
        "email": email, "company_website": hidden_value,
        "first_name": "Asha", "last_name": "Sharma",
    })
    assert r.status_code == 200
    token = r.json()["onboarding_url"].rsplit("/onboard/", 1)[1]
    session = client.get("/api/onboarding/session", params={"token": token})
    assert session.status_code == 200
    assert session.json()["director_email"] == email
    assert session.json()["director_first_name"] == "Asha"
    assert session.json()["status"] == "pending"
    assert session.json()["step"] == 1  # names prefill identity, never verify inbox
    assert len(_rows(store)) == 1
    password = client.post("/api/onboarding/asclepius/password", json={
        "token": token, "password": "correct-horse-battery-9876",
    })
    assert password.status_code == 403
    assert "Verify your email" in password.json()["detail"]


def test_autofilled_requests_still_obey_per_email_cap(client, store):
    payload = {"email": "doctor@aiimsjodhpur.edu.in", "company_website": "saved profile"}
    for _ in range(3):
        assert client.post("/api/onboarding/self-serve", json=payload).status_code == 200
    response = client.post("/api/onboarding/self-serve", json=payload)
    assert response.status_code == 429
    assert "onboarding_url" not in response.json()
    assert len(_rows(store)) == 3


def test_invite_storage_failure_never_returns_success(client, store, monkeypatch):
    def unavailable(**kwargs):
        raise RuntimeError("storage unavailable")
    monkeypatch.setattr(store, "create_health_system_invite", unavailable)
    with pytest.raises(RuntimeError, match="storage unavailable"):
        client.post("/api/onboarding/self-serve", json={
            "email": "doctor@aiimsjodhpur.edu.in", "company_website": "saved profile",
        })
    assert _rows(store) == []


@pytest.mark.parametrize("limit,rotate_ip", [(5, False), (60, True)])
def test_legacy_autofill_cannot_bypass_request_limits(client, store, monkeypatch, limit, rotate_ip):
    import ratelimit
    from routers import onboarding
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "1")
    monkeypatch.setattr(onboarding, "_email_configured", lambda: False)
    ratelimit.reset()
    try:
        for n in range(limit + 1):
            response = client.post("/api/onboarding/self-serve", json={
                "email": f"doctor{n}@aiimsjodhpur.edu.in", "company_website": "saved profile",
            }, headers={"X-Forwarded-For": f"192.0.2.{n + 1 if rotate_ip else 1}"})
            assert response.status_code == (200 if n < limit else 429)
        assert "onboarding_url" not in response.json()
        assert int(response.headers["Retry-After"]) > 0
        assert len(_rows(store)) == limit
    finally:
        ratelimit.reset()


def test_new_signup_preserves_existing_answers_and_backup_is_restorable(client, store, tmp_path):
    import sqlite3
    from scripts.data_inventory import compare, snapshot

    existing = store.create_health_system_invite(
        invite_base_url="https://landing.test", director_email="existing@hospital.org", product="asclepius")
    store.update_health_system_director_identity(
        existing["health_system_id"], first_name="Existing", last_name="Doctor", email="existing@hospital.org")
    before = snapshot(store.db_path)
    backup_path = tmp_path / "recoverable-team.db"
    with sqlite3.connect(store.db_path) as source, sqlite3.connect(backup_path) as target:
        source.backup(target)

    response = client.post("/api/onboarding/self-serve", json={
        "email": "doctor@aiimsjodhpur.edu.in", "company_website": "saved profile",
    })
    assert response.status_code == 200
    assert compare(before, snapshot(store.db_path)) == []
    assert len(_rows(store)) == 2

    restored = TeamStore(str(backup_path))
    assert compare(before, snapshot(restored.db_path)) == []
    token = existing["onboarding_url"].rsplit("/", 1)[1]
    assert restored.get_health_system_by_onboarding_token(token)["director_first_name"] == "Existing"


def test_per_email_cap(client, store):
    for _ in range(3):
        assert client.post("/api/onboarding/self-serve", json={"email": "doc@hospital.org"}).status_code == 200
    r = client.post("/api/onboarding/self-serve", json={"email": "doc@hospital.org"})
    assert r.status_code == 429
    assert len(_rows(store)) == 3

    # Case variants of the same inbox hit the same cap (normalization pinned).
    assert client.post("/api/onboarding/self-serve", json={"email": "DOC@Hospital.org"}).status_code == 429
    assert len(_rows(store)) == 3

    # A different email is unaffected.
    assert client.post("/api/onboarding/self-serve", json={"email": "other@clinic.org"}).status_code == 200


def test_invalid_email_rejected(client, store):
    r = client.post("/api/onboarding/self-serve", json={"email": "not-an-email"})
    assert r.status_code == 422
    assert _rows(store) == []


def test_lead_provenance_recorded(client, store):
    client.post("/api/onboarding/self-serve", json={"email": "doc@hospital.org"})
    with store._conn() as conn:
        rows = conn.execute("SELECT source, email FROM lead_submissions").fetchall()
    assert [(r[0], r[1]) for r in rows] == [("physician_onboard", "doc@hospital.org")]


# ─── Resending a stalled signup's link (admin › Physicians › Signups) ────────
def test_reissue_rotates_the_token_on_the_same_row(client, store):
    """A physician who stalled must resume on the row holding their answers.

    ``create_health_system_invite`` would mint a SECOND row, so the credentials
    they already submitted would sit orphaned on the first while the funnel
    counted them twice.
    """
    client.post("/api/onboarding/self-serve", json={"email": "doc@hospital.org"})
    row = _rows(store)[0]
    before = row["onboarding_token_hash"]

    out = store.reissue_onboarding_token(row["id"], invite_base_url="https://landing.test")
    assert out["onboarding_url"].startswith("https://landing.test/onboard/")

    rows = _rows(store)
    assert len(rows) == 1, "resending minted a duplicate signup row"
    after = rows[0]
    assert after["onboarding_token_hash"] != before
    assert store.onboarding_token_valid(after)
    assert after["last_generated_invite_url"] == out["onboarding_url"]
    # The fresh link actually resolves to the same row.
    token = out["onboarding_url"].rsplit("/", 1)[-1]
    assert store.get_health_system_by_onboarding_token(token)["id"] == row["id"]
    # ...and the old one is dead.
    assert store.get_health_system_by_onboarding_token("nonsense") is None


def test_reissue_refuses_completed_and_unknown_rows(client, store):
    client.post("/api/onboarding/self-serve", json={"email": "doc@hospital.org"})
    hs_id = _rows(store)[0]["id"]
    store.complete_asclepius_onboarding(hs_id)
    with pytest.raises(ValueError):
        store.reissue_onboarding_token(hs_id, invite_base_url="https://landing.test")
    with pytest.raises(ValueError):
        store.reissue_onboarding_token("no-such-row", invite_base_url="https://landing.test")


def test_db_path_parent_directory_is_created(tmp_path):
    """TEAM_DB_PATH must be settable straight to a volume path on first boot.

    Without this, following the storage warning's own advice ("point
    TEAM_DB_PATH at your persistent volume") crashed the app at import with
    sqlite3.OperationalError until someone mkdir'd the directory by hand.
    """
    target = tmp_path / "not" / "yet" / "there" / "team.db"
    assert TeamStore(db_path=str(target)).db_path == str(target)
    assert target.exists()
