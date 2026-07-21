"""Landing lead-capture endpoint (POST /api/leads).

Self-contained: mounts just the leads router on a throwaway TeamStore so the
test needs none of the full app's heavy import chain. EMAIL_DEV_MODE makes
send_html_email succeed without a transport, so the handler exercises the real
store + email path.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("EMAIL_DEV_MODE", "1")  # send_html_email -> success, no network
os.environ.setdefault("RATE_LIMIT_ENABLED", "0")

from routers.leads import router as leads_router  # noqa: E402
from team_store import TeamStore  # noqa: E402


@pytest.fixture()
def store(tmp_path):
    return TeamStore(db_path=str(tmp_path / "leads.db"))


@pytest.fixture()
def client(store):
    app = FastAPI()
    app.state.team_store = store
    app.include_router(leads_router)
    with TestClient(app) as c:
        yield c


def _count(store) -> int:
    with store._conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM lead_submissions").fetchone()[0]


def test_request_data_lead_stored_and_ok(client, store):
    r = client.post(
        "/api/leads",
        json={
            "source": "request_data",
            "email": "buyer@lab.com",
            "message": "Improving our medical model reasoning on hard cases.",
        },
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert _count(store) == 1


def test_provide_data_lead_stored(client, store):
    r = client.post(
        "/api/leads",
        json={
            "source": "provide_data",
            "email": "ops@nephro.org",
            "message": "De-identified EMR + outcomes across ~5k patients.",
        },
    )
    assert r.status_code == 200
    assert _count(store) == 1


def test_invalid_email_rejected(client, store):
    r = client.post("/api/leads", json={"source": "request_data", "email": "not-an-email", "message": "hi"})
    assert r.status_code == 422
    assert _count(store) == 0


def test_empty_message_rejected(client, store):
    r = client.post("/api/leads", json={"source": "request_data", "email": "a@b.com", "message": "   "})
    assert r.status_code == 422
    assert _count(store) == 0


def test_unknown_source_rejected(client, store):
    r = client.post("/api/leads", json={"source": "phishing", "email": "a@b.com", "message": "x"})
    assert r.status_code == 422
    assert _count(store) == 0


def test_honeypot_silently_dropped(client, store):
    r = client.post(
        "/api/leads",
        json={
            "source": "request_data",
            "email": "bot@spam.example",
            "message": "spam",
            "company_website": "http://spam.example",
        },
    )
    # Bots get a normal-looking 200, but nothing is stored or emailed.
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert _count(store) == 0
