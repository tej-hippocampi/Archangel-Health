from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("ADMIN_AUTH_TOKEN", "test-admin-token")

import main as main_module  # noqa: E402
from main import app  # noqa: E402
from tests._role_auth import auth_headers  # noqa: E402


def _seed_escalation(*, email: str = "patient@example.com", tier: int = 3) -> tuple[str, int]:
    pid = f"intervention_case_{uuid.uuid4().hex[:8]}"
    app.state.patient_store[pid] = {
        "id": pid,
        "name": "John Doe",
        "email": email,
        "phase": "post_op",
        "pipeline_type": "post_op",
        "initial_tier": "TIER_1",
        "current_tier": f"TIER_{tier}",
        "structured_data": {"procedure_date": "2026-06-01"},
        "clinic_code": "DEMO",
        "resource_code": "RES001",
    }
    ts = app.state.team_store
    ts.ensure_episode(patient_id=pid)
    esc_id = ts.create_escalation(
        patient_id=pid,
        tier=tier,
        trigger_type="chat:semantic",
        message="urgent concern",
        conversation_snapshot=[],
    )
    return pid, esc_id


def test_intervention_send_success_logs_audit(monkeypatch):
    pid, escalation_id = _seed_escalation()
    sent: dict[str, str] = {}

    async def _fake_send(to_email, subject, html_body, *, importance_headers=False):
        sent["to_email"] = to_email
        sent["subject"] = subject
        sent["html_body"] = html_body
        sent["importance_headers"] = str(importance_headers)
        return True, None

    monkeypatch.setattr(main_module, "is_email_transport_configured", lambda: True)
    # persist_and_notify_care_team_message imports this fresh from email_utils,
    # so the email_utils symbol must be patched too (the main_module binding alone
    # is not consulted on the send path).
    monkeypatch.setattr("email_utils.is_email_transport_configured", lambda: True)
    monkeypatch.setattr(main_module, "_send_html_email_with_reason_impl", _fake_send)

    with TestClient(app, headers=auth_headers("surgeon", source="landing", email="intervention@test.local")) as client:
        r = client.post(
            f"/api/escalations/{escalation_id}/intervention",
            json={"message": "Please call us immediately."},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True
    assert body.get("emailed") is True
    assert sent["to_email"] == "patient@example.com"
    assert "New secure message" in sent["subject"] or "URGENT CARE MESSAGE" in sent["subject"]
    assert "Please call us immediately" not in sent["html_body"]
    assert sent["importance_headers"] == "True"

    logs = app.state.team_store.get_events(pid)
    sent_logs = [e for e in logs if e.get("event_type") == "care_team_message_sent"]
    assert sent_logs


def test_intervention_rejects_empty_message():
    _, escalation_id = _seed_escalation()
    with TestClient(app, headers=auth_headers("surgeon", source="landing", email="empty@test.local")) as client:
        r = client.post(
            f"/api/escalations/{escalation_id}/intervention",
            json={"message": "   "},
        )
    assert r.status_code == 400


def test_intervention_persists_without_patient_email(monkeypatch):
    _, escalation_id = _seed_escalation(email="")
    monkeypatch.setattr(main_module, "is_email_transport_configured", lambda: True)
    with TestClient(app, headers=auth_headers("surgeon", source="landing", email="missing-email@test.local")) as client:
        r = client.post(
            f"/api/escalations/{escalation_id}/intervention",
            json={"message": "Please contact clinic."},
        )
    assert r.status_code == 200
    assert r.json().get("emailed") is False


def test_intervention_persists_when_email_unconfigured(monkeypatch):
    _, escalation_id = _seed_escalation()
    monkeypatch.setattr(main_module, "is_email_transport_configured", lambda: False)
    monkeypatch.setattr("email_utils.is_email_transport_configured", lambda: False)
    with TestClient(app, headers=auth_headers("surgeon", source="landing", email="config@test.local")) as client:
        r = client.post(
            f"/api/escalations/{escalation_id}/intervention",
            json={"message": "Please contact clinic."},
        )
    assert r.status_code == 200
    assert r.json().get("emailed") is False
