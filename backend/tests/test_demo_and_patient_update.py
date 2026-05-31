"""Admin demo credentials catalog and patient detail PATCH."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("ADMIN_AUTH_TOKEN", "test-admin-token")
os.environ.setdefault("AUTH_SECRET", "test-auth-secret")

from main import app  # noqa: E402
from tests._role_auth import admin_headers, landing_token, tenant_token  # noqa: E402
from tenant_constants import ARCH_TRIAGE_DEMO_HEALTH_SYSTEM_ID, DEMO_HEALTH_SYSTEM_ID  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def test_demo_sign_in_routes_public(client):
    r = client.get("/api/demo/sign-in-routes")
    assert r.status_code == 200
    routes = r.json().get("routes") or {}
    assert routes["dr.thompson@archangeldemo.com"]["type"] == "tenant"
    assert routes["dr.thompson@archangeldemo.com"]["slug"] == "archangel-triage-demo"
    assert routes["manan.vyas@cedarssinai.com"]["type"] == "landing"


def test_admin_demo_credentials_requires_auth(client):
    r = client.get("/admin/demo-credentials")
    assert r.status_code == 401


def test_admin_demo_credentials_lists_accounts(client):
    r = client.get("/admin/demo-credentials", headers=admin_headers())
    assert r.status_code == 200
    accounts = r.json().get("accounts") or []
    emails = {a.get("email") for a in accounts}
    assert "dr.thompson@archangeldemo.com" in emails
    assert "rn.castillo@archangeldemo.com" in emails
    assert "manan.vyas@cedarssinai.com" in emails
    director = next(a for a in accounts if a.get("email") == "dr.thompson@archangeldemo.com")
    assert director.get("password")
    assert director.get("authType") == "tenant"


def test_patch_patient_updates_fields(client):
    pid = "test_patch_patient_001"
    store = app.state.patient_store
    store[pid] = {
        "name": "Old Name",
        "phone": "5550000000",
        "email": "old@example.com",
        "health_system_id": ARCH_TRIAGE_DEMO_HEALTH_SYSTEM_ID,
        "pipeline_type": "pre_op",
        "structured_data": {
            "patient_name": "Old Name",
            "procedure_name": "LEJR",
            "procedure_date": "2026-06-01",
            "mbi": "1EG4TE5MK73",
            "dob": "1950-01-01",
        },
        "anchor_procedure_family": "LEJR",
        "mbi": "1EG4TE5MK73",
    }
    token = tenant_token("surgeon", health_system_id=ARCH_TRIAGE_DEMO_HEALTH_SYSTEM_ID)
    headers = {"Authorization": f"Bearer {token}"}
    r = client.patch(
        f"/api/patient/{pid}",
        headers=headers,
        json={
            "name": "Patricia Alvarez",
            "phone": "5551112222",
            "email": "patricia@example.com",
            "mbi": "1EG4TE5MK74",
            "dob": "1958-07-14",
            "scheduled_surgery_date": "2026-06-02",
            "anchor_procedure": "HIP_FEMUR",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["patient"]["name"] == "Patricia Alvarez"
    assert body["patient"]["anchorProcedure"] == "HIP_FEMUR"
    blob = store[pid]
    sd = blob["structured_data"]
    assert sd["patient_name"] == "Patricia Alvarez"
    assert sd["mbi"] == "1EG4TE5MK74"
    assert sd["dob"] == "1958-07-14"
    assert sd["date_of_birth"] == "1958-07-14"
    assert sd["procedure_date"] == "2026-06-02"
    assert blob["anchor_procedure_family"] == "HIP_FEMUR"


def test_patch_patient_rejects_duplicate_mbi(client):
    store = app.state.patient_store
    store["mbi_owner"] = {
        "name": "Owner",
        "health_system_id": ARCH_TRIAGE_DEMO_HEALTH_SYSTEM_ID,
        "structured_data": {"mbi": "1EG4TE5MK99"},
        "mbi": "1EG4TE5MK99",
    }
    store["mbi_other"] = {
        "name": "Other",
        "health_system_id": ARCH_TRIAGE_DEMO_HEALTH_SYSTEM_ID,
        "structured_data": {"mbi": "1EG4TE5MK88"},
        "mbi": "1EG4TE5MK88",
    }
    token = tenant_token("surgeon", health_system_id=ARCH_TRIAGE_DEMO_HEALTH_SYSTEM_ID)
    r = client.patch(
        "/api/patient/mbi_other",
        headers={"Authorization": f"Bearer {token}"},
        json={"mbi": "1EG4TE5MK99"},
    )
    assert r.status_code == 409


def test_list_patients_requires_auth(client):
    r = client.get("/api/patients")
    assert r.status_code == 401


def test_patch_patient_requires_auth(client):
    pid = "test_patch_requires_auth_001"
    app.state.patient_store[pid] = {
        "name": "Needs Auth",
        "health_system_id": ARCH_TRIAGE_DEMO_HEALTH_SYSTEM_ID,
        "pipeline_type": "pre_op",
        "structured_data": {"patient_name": "Needs Auth", "procedure_name": "LEJR"},
    }
    r = client.patch(f"/api/patient/{pid}", json={"name": "Nope"})
    assert r.status_code == 401


def test_landing_token_cannot_access_triage_patient(client):
    pid = "test_landing_blocked_001"
    app.state.patient_store[pid] = {
        "name": "Triage Only",
        "health_system_id": ARCH_TRIAGE_DEMO_HEALTH_SYSTEM_ID,
        "pipeline_type": "post_op",
        "structured_data": {"patient_name": "Triage Only", "procedure_name": "CABG"},
    }
    headers = {"Authorization": f"Bearer {landing_token(email='landing.blocked@example.com')}"}
    r_timeline = client.get(f"/api/patient/{pid}/timeline", headers=headers)
    assert r_timeline.status_code == 404
    r_patch = client.patch(f"/api/patient/{pid}", headers=headers, json={"phone": "5550000000"})
    assert r_patch.status_code == 404


def test_landing_token_sees_only_cedar_patients(client):
    store = app.state.patient_store
    store["landing_visible_demo"] = {
        "name": "Cedar Demo",
        "health_system_id": DEMO_HEALTH_SYSTEM_ID,
        "pipeline_type": "pre_op",
        "structured_data": {"patient_name": "Cedar Demo", "procedure_name": "LEJR"},
    }
    store["landing_hidden_triage"] = {
        "name": "Triage Hidden",
        "health_system_id": ARCH_TRIAGE_DEMO_HEALTH_SYSTEM_ID,
        "pipeline_type": "pre_op",
        "structured_data": {"patient_name": "Triage Hidden", "procedure_name": "CABG"},
    }
    headers = {"Authorization": f"Bearer {landing_token(email='landing.visible@example.com')}"}
    r = client.get("/api/patients", headers=headers)
    assert r.status_code == 200
    ids = {p["id"] for p in r.json().get("patients", [])}
    assert "landing_visible_demo" in ids
    assert "landing_hidden_triage" not in ids


def test_internal_run_daily_jobs_requires_internal_secret(client):
    r = client.post("/internal/team/run-daily-jobs")
    assert r.status_code == 401


def test_preop_audio_blocks_when_grounding_pending(client, monkeypatch):
    pid = "test_preop_audio_pending_001"
    store = app.state.patient_store
    store[pid] = {
        "name": "Pending Grounding",
        "health_system_id": ARCH_TRIAGE_DEMO_HEALTH_SYSTEM_ID,
        "pipeline_type": "pre_op",
        "structured_data": {"patient_name": "Pending Grounding", "procedure_name": "LEJR"},
        "grounding_pending_tracks": ["pre_op"],
        "requires_clinician_review": True,
        "resources": {"preop": {"voice_script": "script body", "voice_audio_url": None}},
        "voice_script": "script body",
    }

    class _NoCallEl:
        async def synthesize(self, *_args, **_kwargs):
            raise AssertionError("synthesize should not run while pre_op grounding is pending")

    import main as main_mod  # noqa: PLC0415

    monkeypatch.setattr(main_mod, "ElevenLabsClient", lambda: _NoCallEl())
    token = tenant_token("surgeon", health_system_id=ARCH_TRIAGE_DEMO_HEALTH_SYSTEM_ID)
    r = client.get(f"/api/patient/{pid}/preop-audio", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 503


def test_portal_handoff_round_trip(client):
    token = tenant_token("surgeon", health_system_id=ARCH_TRIAGE_DEMO_HEALTH_SYSTEM_ID)
    create = client.post(
        "/api/auth/portal-handoff",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert create.status_code == 200, create.text
    code = create.json()["handoff_code"]

    consume = client.post("/api/auth/portal-handoff/consume", json={"handoff_code": code})
    assert consume.status_code == 200, consume.text
    assert consume.json().get("access_token") == token

    consume_again = client.post("/api/auth/portal-handoff/consume", json={"handoff_code": code})
    assert consume_again.status_code == 404


def test_list_patients_includes_edit_prefill_fields(client):
    pid = "test_list_prefill_001"
    store = app.state.patient_store
    store[pid] = {
        "name": "List Test",
        "health_system_id": ARCH_TRIAGE_DEMO_HEALTH_SYSTEM_ID,
        "pipeline_type": "pre_op",
        "structured_data": {
            "patient_name": "List Test",
            "mbi": "1EG4TE5MK77",
            "dob": "1960-03-15",
            "procedure_name": "CABG",
        },
        "anchor_procedure_family": "CABG",
        "mbi": "1EG4TE5MK77",
    }
    token = tenant_token("surgeon", health_system_id=ARCH_TRIAGE_DEMO_HEALTH_SYSTEM_ID)
    r = client.get("/api/patients", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    row = next(p for p in r.json()["patients"] if p["id"] == pid)
    assert row["mbi"] == "1EG4TE5MK77"
    assert row["dob"] == "1960-03-15"
    assert row["anchorProcedure"] == "CABG"
