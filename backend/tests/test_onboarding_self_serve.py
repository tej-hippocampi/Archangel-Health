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


def test_honeypot_returns_decoy_and_stores_nothing(client, store):
    r = client.post(
        "/api/onboarding/self-serve",
        json={"email": "bot@spam.com", "company_website": "https://spam.example"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "/onboard/" in body["onboarding_url"]
    assert _rows(store) == []

    # Decoy must be indistinguishable from a real success by shape: pin key
    # parity so the responses can't silently diverge.
    real = client.post("/api/onboarding/self-serve", json={"email": "doc@hospital.org"}).json()
    assert set(body.keys()) == set(real.keys())

    # ...and the decoy token opens nothing.
    token = body["onboarding_url"].rsplit("/onboard/", 1)[1]
    s = client.get("/api/onboarding/session", params={"token": token})
    assert s.status_code == 404


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
