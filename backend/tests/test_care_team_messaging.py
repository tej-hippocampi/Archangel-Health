from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("ADMIN_AUTH_TOKEN", "test-admin-token")
os.environ.setdefault("AUTH_SECRET", "test-auth-secret")

import main as main_module  # noqa: E402
from main import app  # noqa: E402
from patient_session import create_patient_session  # noqa: E402
from tenant_constants import DEMO_HEALTH_SYSTEM_ID  # noqa: E402
from tests._role_auth import auth_headers, tenant_token  # noqa: E402


def _seed_patient(*, hs_id: str = DEMO_HEALTH_SYSTEM_ID, email: str = "patient@example.com") -> str:
    pid = f"ctm_{uuid.uuid4().hex[:8]}"
    app.state.patient_store[pid] = {
        "id": pid,
        "name": "John Doe",
        "email": email,
        "phone": "+15555550100",
        "health_system_id": hs_id,
        "clinic_code": "DEMO",
        "resource_code": "ABC123",
        "phase": "post_op",
        "pipeline_type": "post_op",
    }
    app.state.team_store.ensure_episode(patient_id=pid)
    return pid


def test_clinician_send_persists_and_notifies(monkeypatch):
    pid = _seed_patient()
    sent = {}

    async def _fake_send(to_email, subject, html_body, *, importance_headers=False):
        sent["to"] = to_email
        sent["subject"] = subject
        sent["html"] = html_body
        sent["importance"] = importance_headers
        return True, None

    monkeypatch.setattr(main_module, "is_email_transport_configured", lambda: True)
    monkeypatch.setattr("email_utils.is_email_transport_configured", lambda: True)
    monkeypatch.setattr(main_module, "_send_html_email_with_reason_impl", _fake_send)

    headers = auth_headers("rn_coordinator", source="landing", email="rn@test.local")
    with TestClient(app, headers=headers) as client:
        r = client.post(
            f"/api/patients/{pid}/care-team-messages",
            json={"message": "Please review your symptoms today."},
        )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    assert data["emailed"] is True
    assert "Please review" not in sent["html"]
    assert "secure message" in sent["html"].lower() or "access codes" in sent["html"].lower()
    rows = app.state.team_store.list_care_team_messages(pid)
    assert len(rows) == 1
    assert rows[0]["sender_type"] == "CARE_TEAM"


def test_clinician_send_persists_when_email_off(monkeypatch):
    pid = _seed_patient()
    monkeypatch.setattr(main_module, "is_email_transport_configured", lambda: False)
    monkeypatch.setattr("email_utils.is_email_transport_configured", lambda: False)
    headers = auth_headers("surgeon", source="landing", email="surgeon@test.local")
    with TestClient(app, headers=headers) as client:
        r = client.post(
            f"/api/patients/{pid}/care-team-messages",
            json={"message": "Check in with us."},
        )
    assert r.status_code == 200
    assert r.json()["emailed"] is False
    assert len(app.state.team_store.list_care_team_messages(pid)) == 1


def test_patient_reply_no_email(monkeypatch):
    pid = _seed_patient()
    emails = []

    async def _fake_send(to_email, subject, html_body, *, importance_headers=False):
        emails.append(to_email)
        return True, None

    monkeypatch.setattr(main_module, "is_email_transport_configured", lambda: True)
    monkeypatch.setattr("email_utils.is_email_transport_configured", lambda: True)
    monkeypatch.setattr(main_module, "_send_html_email_with_reason_impl", _fake_send)

    headers = auth_headers("surgeon", source="landing", email="dr.thompson@test.local")
    with TestClient(app, headers=headers) as client:
        client.post(
            f"/api/patients/{pid}/care-team-messages",
            json={"message": "Hello from surgeon."},
        )
        before = len(emails)
        r = client.post(
            f"/api/patient/{pid}/care-team-messages/reply",
            json={
                "message": "Thanks, I will check in.",
                "recipient_email": "dr.thompson@test.local",
                "recipient_role": "surgeon",
            },
        )
    assert r.status_code == 200, r.text
    assert len(emails) == before
    rows = app.state.team_store.list_care_team_messages(pid)
    assert any(x["sender_type"] == "PATIENT" for x in rows)


def test_read_state_transitions():
    pid = _seed_patient()
    ts = app.state.team_store
    ts.create_care_team_message(
        patient_id=pid,
        sender_type="CARE_TEAM",
        body="Hi",
        sender_email="rn@test.local",
        sender_role="rn_coordinator",
        sender_name="Maria",
        health_system_id=DEMO_HEALTH_SYSTEM_ID,
    )
    assert ts.count_unread_for_patient(pid) == 1
    with TestClient(app) as client:
        client.cookies.set("pt_session", create_patient_session(pid, DEMO_HEALTH_SYSTEM_ID))
        client.get(f"/api/patient/{pid}/care-team-messages")
    assert ts.count_unread_for_patient(pid) == 0

    ts.create_care_team_message(
        patient_id=pid,
        sender_type="PATIENT",
        body="Reply",
        recipient_email="rn@test.local",
        health_system_id=DEMO_HEALTH_SYSTEM_ID,
    )
    assert ts.count_unread_for_care_team(pid) == 1
    headers = auth_headers("rn_coordinator", source="landing", email="rn@test.local")
    with TestClient(app, headers=headers) as client:
        client.get(f"/api/patients/{pid}/care-team-messages")
    assert ts.count_unread_for_care_team(pid) == 0


def test_tenant_isolation():
    other_hs = "00000000-0000-4000-8000-000000000099"
    app.state.team_store.ensure_demo_health_system(
        hs_id=other_hs,
        slug="other-tenant",
        name="Other HS",
        health_system_code="OTHER",
    )
    pid = _seed_patient(hs_id=other_hs)
    token = tenant_token("surgeon", health_system_id=DEMO_HEALTH_SYSTEM_ID, email="demo-surgeon@test.local")
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app, headers=headers) as client:
        r = client.get(f"/api/patients/{pid}/care-team-messages")
    assert r.status_code == 404
