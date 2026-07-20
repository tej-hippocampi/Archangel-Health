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
    assert "/onboard/" in body["onboarding_url"]  # same shape as a real success
    assert _rows(store) == []

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
