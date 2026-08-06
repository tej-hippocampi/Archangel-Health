"""
CareGuide doctor sign-up email verification + task-assignment notification
emails.

Verification email transport auto-detects dev mode with no SendGrid
configured (email_utils), so no stub is required — registration doesn't hard-
fail if the send fails either. Covers: register -> verify (code + magic
link) -> login gate -> resend rate limit -> portal-handoff gate, and the
task-notification outbox (idempotency + auth on the manual trigger).
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("TEAM_DB_PATH", os.path.join(tempfile.gettempdir(), f"docverify_{uuid.uuid4().hex}.db"))
os.environ.setdefault("ADMIN_USERNAME", "testadmin")
os.environ.setdefault("ADMIN_PASSWORD", "testadminpass")
# Captured once at import time: other test files collected later in a full-suite
# run overwrite the process-global TEAM_DB_PATH env var for THEIR own module-level
# TeamStore construction, but `main.app`'s TeamStore singleton (imported below) was
# already built against whatever this value was at THIS import — re-reading the
# env var later would silently point at the wrong sqlite file.
_TEAM_DB_PATH = os.environ["TEAM_DB_PATH"]

import auth as auth_module  # noqa: E402
# Keep the landing user store out of the real backend/auth_users.json.
auth_module.USERS_FILE = Path(_TEAM_DB_PATH + ".users.json")

from main import app  # noqa: E402

PASSWORD = "pw12345678"


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _register(client, email=None):
    email = email or f"doc_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/api/auth/register", json={"email": email, "password": PASSWORD, "name": "Dr. Test"})
    assert r.status_code == 200, r.text
    return email, r.json()


# ─── Verification by code ──────────────────────────────────────────────────

def test_register_starts_unverified_but_still_returns_a_token(client):
    email, body = _register(client)
    assert body["user"]["email_verified"] is False
    assert body.get("access_token")


def test_login_before_verification_withholds_the_token(client):
    email, _ = _register(client)
    r = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200
    assert r.json() == {"verification_required": True, "email": email}


def test_wrong_code_is_rejected_and_does_not_consume_the_challenge(client, monkeypatch):
    monkeypatch.setattr(secrets, "choice", lambda seq: seq[0])  # code will be: 000000
    email, _ = _register(client)
    r = client.post("/api/auth/verify-email", json={"email": email, "code": "111111"})
    assert r.status_code == 400
    # The real code still works after a wrong attempt.
    r = client.post("/api/auth/verify-email", json={"email": email, "code": "000000"})
    assert r.status_code == 200 and r.json()["email_verified"] is True


def test_verified_login_returns_a_real_access_token(client, monkeypatch):
    monkeypatch.setattr(secrets, "choice", lambda seq: seq[0])
    email, _ = _register(client)
    client.post("/api/auth/verify-email", json={"email": email, "code": "000000"})
    r = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200
    body = r.json()
    assert body.get("access_token") and body["user"]["email_verified"] is True


def test_reregistering_an_unverified_account_does_not_duplicate_or_error(client):
    email, first = _register(client)
    r = client.post("/api/auth/register", json={"email": email, "password": PASSWORD, "name": "Dr. Test"})
    assert r.status_code == 200
    assert r.json()["user"]["email_verified"] is False


# ─── Verification by magic link ────────────────────────────────────────────

def test_magic_link_verifies_and_is_single_use(client, monkeypatch):
    captured = {}
    orig = secrets.token_urlsafe

    def _traced(n):
        tok = orig(n)
        captured["token"] = tok
        return tok

    monkeypatch.setattr(secrets, "token_urlsafe", _traced)
    email, _ = _register(client)
    raw_token = captured["token"]

    r = client.get("/api/auth/verify-email/by-token", params={"token": "not-a-real-token"})
    assert r.status_code == 400

    r = client.get("/api/auth/verify-email/by-token", params={"token": raw_token})
    assert r.status_code == 200 and r.json() == {"ok": True, "email_verified": True, "email": email}

    # Reusing the same link fails — it was consumed by the first visit.
    r = client.get("/api/auth/verify-email/by-token", params={"token": raw_token})
    assert r.status_code == 400


# ─── Resend ─────────────────────────────────────────────────────────────────

def test_resend_is_rate_limited_after_five_in_an_hour(client):
    email, _ = _register(client)  # registration itself sends #1
    codes = [client.post("/api/auth/verify-email/resend", json={"email": email}).status_code for _ in range(5)]
    assert 429 in codes, f"expected the resend cap to trip within 5 more sends, got {codes}"


def test_resend_does_not_reveal_account_existence(client):
    r = client.post("/api/auth/verify-email/resend", json={"email": "nobody-here@example.com"})
    assert r.status_code == 200  # same shape as a real (already-verified) account, no 404/enumeration


# ─── Portal handoff gate ────────────────────────────────────────────────────

def test_portal_handoff_blocked_until_verified_then_allowed(client, monkeypatch):
    monkeypatch.setattr(secrets, "choice", lambda seq: seq[0])
    email, body = _register(client)
    token = body["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    r = client.post("/api/auth/portal-handoff", headers=headers)
    assert r.status_code == 403

    client.post("/api/auth/verify-email", json={"email": email, "code": "000000"})

    r = client.post("/api/auth/portal-handoff", headers=headers)
    assert r.status_code == 200 and r.json().get("handoff_code")


# ─── Task-assignment notification outbox ───────────────────────────────────

def _seed_intake_notification(db_path: str, *, doctor_id: str = "doctor:default") -> str:
    nid = uuid.uuid4().hex
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO intake_form_notifications "
        "(id, doctor_id, intake_form_id, type, message, is_read, created_at) "
        "VALUES (?, ?, ?, ?, ?, 0, ?)",
        (nid, doctor_id, "form-123", "new_form", "Test notification", datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()
    return nid


def test_task_notification_manual_trigger_requires_auth(client, monkeypatch):
    monkeypatch.setenv("INTERNAL_TOOL_SECRET", "test-internal-secret")
    r = client.post("/internal/team/run-task-notifications", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_task_notification_sends_once_and_is_idempotent_on_rerun(client, monkeypatch):
    # The live db path, not a captured env var: in a full-suite run other test
    # files collected after this one can overwrite the process-global
    # TEAM_DB_PATH env var before any test actually executes, but the running
    # app's TeamStore singleton was already built against whatever path was
    # active at ITS OWN (first) import — this is the only way to reliably hit
    # the same file it's using (mirrors client.app.state.team_store elsewhere).
    db_path = client.app.state.team_store.db_path
    monkeypatch.setenv("INTERNAL_TOOL_SECRET", "test-internal-secret")
    nid = _seed_intake_notification(db_path)
    headers = {"Authorization": "Bearer test-internal-secret"}

    r = client.post("/internal/team/run-task-notifications", headers=headers)
    assert r.status_code == 200

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT status FROM task_notification_outbox WHERE task_id = ? AND task_type = 'intake_notification'",
        (nid,),
    ).fetchall()
    assert len(rows) == 1 and rows[0]["status"] == "sent"

    # Re-running the pass must not enqueue a second row for the same notification.
    r = client.post("/internal/team/run-task-notifications", headers=headers)
    assert r.status_code == 200
    rows_again = conn.execute(
        "SELECT id FROM task_notification_outbox WHERE task_id = ? AND task_type = 'intake_notification'",
        (nid,),
    ).fetchall()
    assert len(rows_again) == 1
