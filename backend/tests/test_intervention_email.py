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


def _seed_escalation(*, email: str = "patient@example.com") -> tuple[str, int]:
    pid = f"intervention_case_{uuid.uuid4().hex[:8]}"
    app.state.patient_store[pid] = {
        "id": pid,
        "name": "John Doe",
        "email": email,
        "phase": "post_op",
        "pipeline_type": "post_op",
        "initial_tier": "TIER_1",
        "current_tier": "TIER_3",
        "structured_data": {"procedure_date": "2026-06-01"},
    }
    ts = app.state.team_store
    ts.ensure_episode(patient_id=pid)
    esc_id = ts.create_escalation(
        patient_id=pid,
        tier=3,
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
        return True

    monkeypatch.setattr(main_module, "is_email_transport_configured", lambda: True)
    monkeypatch.setattr(main_module, "_send_html_email_impl", _fake_send)

    with TestClient(app, headers=auth_headers("surgeon", source="landing", email="intervention@test.local")) as client:
        r = client.post(
            f"/api/escalations/{escalation_id}/intervention",
            json={"message": "Please call us immediately."},
        )
    assert r.status_code == 200, r.text
    assert r.json().get("ok") is True
    assert sent["to_email"] == "patient@example.com"
    assert sent["subject"] == "Tester, Surgeon, Archangel Health — URGENT CARE MESSAGE"
    assert sent["importance_headers"] == "True"

    logs = app.state.team_store.get_events(pid)
    intervention_logs = [e for e in logs if e.get("event_type") == "provider_intervention_email"]
    assert intervention_logs
    payload = intervention_logs[-1].get("payload") or {}
    assert payload.get("escalation_id") == escalation_id
    assert payload.get("provider_email") == "intervention@test.local"
    assert "URGENT CARE MESSAGE" in str(payload.get("subject") or "")


def test_intervention_rejects_empty_message():
    _, escalation_id = _seed_escalation()
    with TestClient(app, headers=auth_headers("surgeon", source="landing", email="empty@test.local")) as client:
        r = client.post(
            f"/api/escalations/{escalation_id}/intervention",
            json={"message": "   "},
        )
    assert r.status_code == 400


def test_intervention_returns_409_without_patient_email(monkeypatch):
    _, escalation_id = _seed_escalation(email="")
    monkeypatch.setattr(main_module, "is_email_transport_configured", lambda: True)
    with TestClient(app, headers=auth_headers("surgeon", source="landing", email="missing-email@test.local")) as client:
        r = client.post(
            f"/api/escalations/{escalation_id}/intervention",
            json={"message": "Please contact clinic."},
        )
    assert r.status_code == 409
    assert "No email on file" in r.json().get("detail", "")


def test_intervention_returns_503_when_email_unconfigured(monkeypatch):
    _, escalation_id = _seed_escalation()
    monkeypatch.setattr(main_module, "is_email_transport_configured", lambda: False)
    with TestClient(app, headers=auth_headers("surgeon", source="landing", email="config@test.local")) as client:
        r = client.post(
            f"/api/escalations/{escalation_id}/intervention",
            json={"message": "Please contact clinic."},
        )
    assert r.status_code == 503
    assert "not configured" in str(r.json().get("detail") or "").lower()
