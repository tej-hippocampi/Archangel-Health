"""
Smoke tests for the Initial Pre-Op Triage HTTP surface (Pass 2 §1.2).

Algorithm correctness is exhaustively covered by `test_initial_tier.py`;
here we only verify the wire format and router-level behavior:

  * `POST /api/triage/initial-tier/compute` — pure preview.
  * `POST /api/episodes/{id}/initial-tier`  — persist + idempotent.
  * `POST /api/episodes/{id}/initial-tier/override` — reason length guard.
  * `GET  /api/triage/tuning/initial-tier/current` — read-only tuning.
  * `POST /api/triage/tuning/initial-tier`         — admin no-op stub.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("ADMIN_AUTH_TOKEN", "test-admin-token")

from main import app  # noqa: E402
from tests._role_auth import auth_headers  # noqa: E402


@pytest.fixture()
def client():
    """Pass-4: attach a `surgeon` Bearer by default so every request to the
    triage routers passes `require_roles(staff, WRITE_CLINICAL)`. Use a
    landing-flavored token so seeded patients (without health_system_id)
    aren't filtered by the tenant-scope guard in `_resolve_patient`.
    """
    with TestClient(app, headers=auth_headers("surgeon", source="landing")) as c:
        yield c


def _seed_patient() -> str:
    pid = f"initial_tier_{uuid.uuid4().hex[:8]}"
    app.state.patient_store[pid] = {
        "id": pid,
        "phase": "pre_op",
        "structured_data": {"procedure_name": "Total Knee Arthroplasty"},
        "anchor_procedure_family": "LEJR",
    }
    return pid


def _tier_input(*, emergency: bool = False) -> dict:
    return {
        "procedure": {
            "cpt_code": "27447",
            "anchor_procedure_family": "LEJR",
            "scheduled_date": "2099-06-15",
            "is_emergency": emergency,
        },
        "active_problems": {
            "problems": [{"icd10": "I10", "description": "HTN", "status": "ACTIVE"}],
            "functional_status": "INDEPENDENT",
        },
        "medications": {"medications": [{"name": "lisinopril"}]},
        "allergies": {"allergies": []},
        "social_history": {
            "age": 62, "smoking_status": "NEVER",
            "lives_alone": False, "has_reliable_caregiver": True,
        },
        "recent_labs": {"labs": [], "studies": []},
    }


# ─── Compute preview ────────────────────────────────────────────────────────


def test_compute_returns_tier_assignment(client):
    r = client.post("/api/triage/initial-tier/compute", json=_tier_input())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tier"] in ("TIER_1", "TIER_2", "TIER_3")
    assert "reasons" in body
    assert "model_version" in body


def test_compute_emergency_short_circuits_to_tier_3(client):
    r = client.post("/api/triage/initial-tier/compute", json=_tier_input(emergency=True))
    assert r.status_code == 200
    body = r.json()
    assert body["tier"] == "TIER_3"
    assert body["score"] is None
    assert any(reason.get("kind") == "HARD" for reason in body["reasons"])


# ─── Persist (idempotent) ───────────────────────────────────────────────────


def test_persist_assigns_tier_and_writes_blob(client):
    pid = _seed_patient()
    r = client.post(
        f"/api/episodes/{pid}/initial-tier",
        json={"input": _tier_input()},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["initialTier"] in ("TIER_1", "TIER_2", "TIER_3")

    # Blob persists the algorithmic outcome
    blob = app.state.patient_store[pid]
    assert blob["initial_tier"] == body["initialTier"]
    assert isinstance(blob["initial_tier_was_hard_escalator"], bool)
    assert blob["current_tier"] == body["currentTier"]

    # event_logs INITIAL_TIER_ASSIGNED row written
    logs = app.state.team_store.get_events(pid)
    assert any(e.get("event_type") == "INITIAL_TIER_ASSIGNED" for e in logs)


def test_persist_is_idempotent_for_same_input(client):
    pid = _seed_patient()
    body = {"input": _tier_input()}
    r1 = client.post(f"/api/episodes/{pid}/initial-tier", json=body)
    r2 = client.post(f"/api/episodes/{pid}/initial-tier", json=body)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r2.json()["idempotent"] is True
    # Only one INITIAL_TIER_ASSIGNED event row
    assigned = [e for e in app.state.team_store.get_events(pid)
                if e.get("event_type") == "INITIAL_TIER_ASSIGNED"]
    assert len(assigned) == 1


def test_persist_unknown_patient_returns_404(client):
    r = client.post(
        f"/api/episodes/missing-{uuid.uuid4().hex[:6]}/initial-tier",
        json={"input": _tier_input()},
    )
    assert r.status_code == 404


# ─── Override ───────────────────────────────────────────────────────────────


def test_override_short_reason_rejected(client):
    pid = _seed_patient()
    client.post(f"/api/episodes/{pid}/initial-tier", json={"input": _tier_input()})
    r = client.post(
        f"/api/episodes/{pid}/initial-tier/override",
        json={"targetTier": "TIER_3", "reason": "too short"},
    )
    assert r.status_code == 422


def test_override_changes_current_tier_only(client):
    pid = _seed_patient()
    client.post(f"/api/episodes/{pid}/initial-tier", json={"input": _tier_input()})
    blob_before = dict(app.state.patient_store[pid])
    r = client.post(
        f"/api/episodes/{pid}/initial-tier/override",
        json={
            "targetTier": "TIER_3",
            "reason": "Coordinator escalation: documented red flag missed by intake.",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["currentTier"] == "TIER_3"
    # initial_tier itself does not change (sticky-hard-guard reads it)
    assert body["initialTier"] == blob_before.get("initial_tier")
    # event_logs INITIAL_TIER_OVERRIDDEN row written
    overrides = [e for e in app.state.team_store.get_events(pid)
                 if e.get("event_type") == "INITIAL_TIER_OVERRIDDEN"]
    assert len(overrides) == 1


# ─── Tuning ─────────────────────────────────────────────────────────────────


def test_tuning_current_returns_config(client):
    r = client.get("/api/triage/tuning/initial-tier/current")
    assert r.status_code == 200
    body = r.json()
    assert "modelVersion" in body or "model_version" in body


def test_tuning_post_requires_admin_token(client):
    r = client.post("/api/triage/tuning/initial-tier", json={})
    assert r.status_code == 401


def test_tuning_post_with_admin_token_returns_config(client):
    r = client.post(
        "/api/triage/tuning/initial-tier",
        headers={"X-Admin-Token": os.environ["ADMIN_AUTH_TOKEN"]},
        json={"weights": {"foo": 1}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["deployed"] is False
    assert "config" in body
