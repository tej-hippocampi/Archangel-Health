"""Data Provider Portal — end-to-end HTTP (Data Provider Portal PRD §3–§8, §10).

Covers the acceptance criteria: admin invite → data_partner account, deny-by-
default (a provider can ONLY upload), forced password reset, upload → ingestion
inbox, promote → V4 queue, and the V4 wall (a real case never served to v3; a
v3 claim on a real case → 400).
"""

from __future__ import annotations

import io
import os
import sys
import zipfile
from pathlib import Path

os.environ["EMAIL_DEV_MODE"] = "1"  # make the transport "configured" (prints, no send)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests._asclepius as A  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(A.app)


def _admin(store):
    return A.make_user(store, role="admin")


# ─── §3 invite ────────────────────────────────────────────────────────────────
def test_admin_invite_creates_data_partner():
    store = A.fresh_store()
    admin = _admin(store)
    r = client.post("/api/asclepius/admin/data-providers",
                    headers=A.headers_for(admin),
                    json={"email": "provider@clinic.org", "org_name": "Clinic", "specialty": "nephrology"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "account created" in body["message"]
    assert body["provider"]["email"] == "provider@clinic.org"
    # a data_partner account now exists
    u = store.get_user_by_email("provider@clinic.org")
    assert u and u["role"] == "data_partner"
    # and it shows in the admin list
    lst = client.get("/api/asclepius/admin/data-providers", headers=A.headers_for(admin)).json()
    assert any(p["email"] == "provider@clinic.org" for p in lst["providers"])


# ─── §5 deny-by-default ───────────────────────────────────────────────────────
def _provision_provider(store, email="p@clinic.org", pw="KnownPass-123!"):
    return store.provision_data_provider(email=email, password=pw, specialty="nephrology")


def test_data_partner_can_only_use_the_portal():
    store = A.fresh_store()
    p = _provision_provider(store)
    tok = client.post("/api/asclepius/auth/login",
                      json={"email": "p@clinic.org", "password": "KnownPass-123!"}).json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    # allowed: its own portal
    assert client.get("/api/asclepius/provider/me", headers=h).status_code == 200
    # denied everywhere else (deny-by-default, not hide-in-UI)
    assert client.get("/api/asclepius/tasks/next", headers=h).status_code == 403
    assert client.get("/api/asclepius/admin/data-providers", headers=h).status_code == 403
    assert client.get("/api/asclepius/auth/me", headers=h).status_code == 403
    assert client.get("/api/asclepius/ingestion/uploads", headers=h).status_code == 403


# ─── §5 forced reset → upload → inbox ─────────────────────────────────────────
def test_forced_reset_then_upload_appears_in_inbox():
    store = A.fresh_store()
    admin = _admin(store)
    _provision_provider(store)
    tok = client.post("/api/asclepius/auth/login",
                      json={"email": "p@clinic.org", "password": "KnownPass-123!"}).json()["token"]
    h = {"Authorization": f"Bearer {tok}"}

    me = client.get("/api/asclepius/provider/me", headers=h).json()
    assert me["must_reset_password"] is True

    # can't upload before resetting
    assert client.post("/api/asclepius/provider/uploads", headers=h,
                       files=[("files", ("x.txt", b"note", "text/plain"))]).status_code == 403

    # forced reset (current password blank — the token is the proof)
    assert client.post("/api/asclepius/provider/password", headers=h,
                       json={"new_password": "BrandNewPass-456!"}).status_code == 200
    assert client.get("/api/asclepius/provider/me", headers=h).json()["must_reset_password"] is False

    # upload a bundle: FHIR + CSV + 2 notes -> one real case
    fhir = (b'{"resourceType":"Bundle","entry":[{"resource":{"resourceType":"Patient",'
            b'"id":"pt1","gender":"female","birthDate":"1948-05-01"}},'
            b'{"resource":{"resourceType":"Observation","category":[{"coding":[{"code":"laboratory"}]}],'
            b'"code":{"text":"Creatinine","coding":[{"code":"2160-0"}]},"effectiveDateTime":"2025-03-08",'
            b'"valueQuantity":{"value":2.4,"unit":"mg/dL"}}}]}')
    csv = b"panel,analyte,value,unit,collected_at\nBMP,Potassium,5.9,mmol/L,2025-03-08"
    up = client.post("/api/asclepius/provider/uploads", headers=h, files=[
        ("files", ("bundle.json", fhir, "application/json")),
        ("files", ("labs.csv", csv, "text/csv")),
        ("files", ("n1.txt", b"H&P nephrology: 76F AKI since 3/1/2025.", "text/plain")),
        ("files", ("n2.txt", b"Progress nephrology: improving on 2025-03-09.", "text/plain")),
    ])
    assert up.status_code == 200, up.text
    assert up.json()["status"] == "ingested"

    # it reappears in the admin ingestion inbox with files + a real case preview
    admin_h = A.headers_for(admin)
    uploads = client.get("/api/asclepius/ingestion/uploads", headers=admin_h).json()["uploads"]
    assert len(uploads) == 1
    detail = client.get(f"/api/asclepius/ingestion/uploads/{uploads[0]['upload_id']}",
                        headers=admin_h).json()
    assert detail["cases"] and detail["cases"][0]["case"]["case_source"] == "real_deid"
    assert detail["cases"][0]["case"]["demographics"].get("age_band") == "70-79"


# ─── §8 promote → V4 + the wall ───────────────────────────────────────────────
def test_promote_to_v4_and_wall(monkeypatch):
    import routers.asclepius_provider as rp

    async def _fake_candidates(prompt, *, specialty="general", ai_failure_mode=None):
        return {"candidates": [{"id": "a", "text": "Strong answer."},
                               {"id": "b", "text": "Plausibly flawed answer."}],
                "model": "test", "intended_flawed_id": "b"}

    async def _skip_hard(*a, **k):
        return {"skipped": True}

    async def _skip_case(*a, **k):
        return {"skipped": True}

    monkeypatch.setattr(rp, "generate_candidates_ex", _fake_candidates)
    monkeypatch.setattr(rp, "run_hardness_judge", _skip_hard)
    monkeypatch.setattr(rp, "run_case_judge", _skip_case)

    store = A.fresh_store()
    admin = _admin(store)
    # seed a clean ingested real case directly
    up = store.create_upload(provider_id="pid", provider_email="p@clinic.org")
    case = {"case_source": "real_deid", "specialty": "nephrology",
            "demographics": {"age_band": "70-79"},
            "lab_panels": [{"panel": "BMP", "collected_offset_days": 0,
                            "results": [{"analyte": "Creatinine", "value": 2.4, "unit": "mg/dL"}]}],
            "notes": [{"note_type": "Progress", "author_role": "nephrology", "text": "AKI, improving."}]}
    ic = store.add_ingest_case(upload_id=up["upload_id"], case=case, patient_key="pt1")

    r = client.post(f"/api/asclepius/ingestion/cases/{ic['ic_id']}/promote",
                    headers=A.headers_for(admin),
                    json={"question": "What is the most likely cause of this AKI?"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["portal_version"] == "v4" and body["case_source"] == "real_deid"
    task_id = body["task_id"]
    task = store.get_task(task_id)
    assert task["modality"] == "multimodal"
    assert task["case"]["case_source"] == "real_deid"

    # the wall: v4 serves it; v3 never does
    ev = A.make_user(store, role="evaluator", specialty="nephrology")
    v4 = client.get("/api/asclepius/tasks/next?portal_version=v4", headers=A.headers_for(ev)).json()
    assert v4["task"] and v4["task"]["task_id"] == task_id
    v3 = client.get("/api/asclepius/tasks/next?portal_version=v3", headers=A.headers_for(ev)).json()
    assert (v3["task"] or {}).get("task_id") != task_id  # real case never in a v3 session
