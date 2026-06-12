"""End-to-end FHIR import router tests with FastAPI's TestClient.

The FHIR fetchers are monkeypatched (no network); everything downstream —
document registration, audit, eligibility-check attachment — runs for real
against the in-memory stores, mirroring test_eligibility_router.py.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os

os.environ.setdefault("UPLOAD_DIR", "/tmp/elysium-eligibility-tests")

from eligibility import store as elig_store  # noqa: E402
from eligibility import pipeline as elig_pipeline  # noqa: E402
from integrations.fhir import fetch as fhir_fetch  # noqa: E402
from main import app  # noqa: E402
from tests._role_auth import tenant_token  # noqa: E402

MBI_SYSTEM = "http://hl7.org/fhir/sid/us-mbi"


@pytest.fixture(autouse=True)
def _clean_state():
    elig_store.ELIGIBILITY_CHECKS.clear()
    elig_store.ELIGIBILITY_DOCS.clear()
    elig_store.BATCHES.clear()
    elig_store.AUDIT_LOG.clear()
    elig_store._RATE_BUCKETS.clear()  # type: ignore[attr-defined]
    app.state.patient_store.clear()
    yield


@pytest.fixture(autouse=True)
def _event_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def auth():
    return {"Authorization": f"Bearer {tenant_token('surgeon')}"}


@pytest.fixture
def local_patient():
    app.state.patient_store["p1"] = {
        "id": "p1",
        "name": "Margaret Okafor",
        "is_draft": True,
        "health_system_id": "demo_hs",
        "relevant_files": [],
        "structured_data": {"procedure_date": "2026-07-14"},
    }
    return "p1"


@pytest.fixture
def fhir_enabled(monkeypatch):
    monkeypatch.setenv("FHIR_ENABLED", "1")
    monkeypatch.setenv("FHIR_BASE_URL", "http://fhir.test/fhir")
    monkeypatch.setenv("FHIR_AUTH_MODE", "none")


def _coverage_bundle() -> Dict[str, Any]:
    return {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [
            {"resource": {
                "resourceType": "Patient",
                "id": "okafor-margaret",
                "identifier": [{"system": MBI_SYSTEM, "value": "4WH7QD2RT55"}],
                "name": [{"family": "Okafor", "given": ["Margaret"]}],
                "birthDate": "1954-09-17",
            }},
            {"resource": {
                "resourceType": "Coverage",
                "id": "cov-a",
                "status": "active",
                "payor": [{"display": "Original Medicare"}],
            }},
        ],
    }


@pytest.fixture
def stub_fetchers(monkeypatch):
    async def fake_bundle(client, fhir_patient_id):
        assert fhir_patient_id == "okafor-margaret"
        return _coverage_bundle()

    async def fake_attachments(client, fhir_patient_id, **kw):
        return [
            {"title": "preop summary", "content_type": "text/plain",
             "content": b"Pre-op eligibility summary text", "fhir_docref_id": "dr1"},
            {"title": "discharge", "content_type": "application/pdf",
             "content": b"%PDF-1.4 fake pdf bytes", "fhir_docref_id": "dr2"},
        ]

    async def fake_search(client, *, identifier=None, name=None, birthdate=None):
        return [{"fhirId": "okafor-margaret", "name": "Margaret Okafor",
                 "birthDate": "1954-09-17", "gender": "female",
                 "mbi": "4WH7QD2RT55", "identifiers": []}]

    monkeypatch.setattr(fhir_fetch, "fetch_eligibility_bundle", fake_bundle)
    monkeypatch.setattr(fhir_fetch, "fetch_document_attachments", fake_attachments)
    monkeypatch.setattr(fhir_fetch, "search_patients", fake_search)


# ─── Flag + auth gating ─────────────────────────────────────────────────────
def test_disabled_returns_503(client, auth, local_patient):
    r = client.get("/api/fhir/patients?name=Okafor", headers=auth)
    assert r.status_code == 503
    r = client.post("/api/fhir/import", headers=auth,
                    json={"patientId": "p1", "fhirPatientId": "x"})
    assert r.status_code == 503


def test_status_reports_disabled(client, auth):
    r = client.get("/api/fhir/status", headers=auth)
    assert r.status_code == 200
    assert r.json()["enabled"] is False


def test_unauthenticated_is_401(client, fhir_enabled):
    assert client.get("/api/fhir/status").status_code == 401
    assert client.get("/api/fhir/patients?name=x").status_code == 401
    assert client.post("/api/fhir/import",
                       json={"patientId": "p1", "fhirPatientId": "x"}).status_code == 401


def test_misconfigured_smart_backend_is_503(client, auth, monkeypatch):
    monkeypatch.setenv("FHIR_ENABLED", "1")
    monkeypatch.setenv("FHIR_BASE_URL", "http://fhir.test/fhir")
    monkeypatch.setenv("FHIR_AUTH_MODE", "smart_backend")  # no client id / key
    r = client.get("/api/fhir/patients?name=x", headers=auth)
    assert r.status_code == 503
    assert "FHIR_CLIENT_ID" in r.json()["detail"]


# ─── Search ─────────────────────────────────────────────────────────────────
def test_patient_search(client, auth, fhir_enabled, stub_fetchers):
    r = client.get("/api/fhir/patients?name=Okafor", headers=auth)
    assert r.status_code == 200
    patients = r.json()["patients"]
    assert patients[0]["fhirId"] == "okafor-margaret"
    assert patients[0]["mbi"] == "4WH7QD2RT55"


def test_patient_search_requires_a_param(client, auth, fhir_enabled, stub_fetchers):
    assert client.get("/api/fhir/patients", headers=auth).status_code == 400


# ─── Import ─────────────────────────────────────────────────────────────────
def test_import_registers_coverage_bundle(client, auth, fhir_enabled, stub_fetchers, local_patient):
    r = client.post("/api/fhir/import", headers=auth,
                    json={"patientId": "p1", "fhirPatientId": "okafor-margaret"})
    assert r.status_code == 200
    docs = r.json()["documents"]
    assert len(docs) == 1
    assert docs[0]["format"] == "FHIR_JSON"
    assert docs[0]["filename"] == "fhir_coverage_okafor-margaret.json"

    # Registered in the doc store with provenance, attached to the patient
    rec = elig_store.get_doc(docs[0]["id"])
    assert rec["source"] == "fhir"
    assert rec["source_meta"]["fhir_patient_id"] == "okafor-margaret"
    assert docs[0]["id"] in app.state.patient_store["p1"]["relevant_files"]

    # Persisted bytes are the verbatim FHIR Bundle
    saved = json.loads(Path(rec["path"]).read_bytes())
    assert saved["resourceType"] == "Bundle"

    actions = [a["action"] for a in elig_store.AUDIT_LOG]
    assert "fhir_document_imported" in actions
    assert "fhir_import_completed" in actions


def test_import_with_documents_reuses_format_detection(client, auth, fhir_enabled, stub_fetchers, local_patient):
    r = client.post("/api/fhir/import", headers=auth,
                    json={"patientId": "p1", "fhirPatientId": "okafor-margaret",
                          "includeDocuments": True})
    assert r.status_code == 200
    formats = {d["filename"]: d["format"] for d in r.json()["documents"]}
    assert formats["fhir_coverage_okafor-margaret.json"] == "FHIR_JSON"
    assert formats["discharge.pdf"] == "PDF"  # magic bytes → existing PDF parser path
    assert formats["preop summary.txt"] == "OTHER"


def test_import_unknown_patient_is_404(client, auth, fhir_enabled, stub_fetchers):
    r = client.post("/api/fhir/import", headers=auth,
                    json={"patientId": "ghost", "fhirPatientId": "okafor-margaret"})
    assert r.status_code == 404


def test_import_other_tenants_patient_is_404(client, fhir_enabled, stub_fetchers, local_patient):
    other = {"Authorization": f"Bearer {tenant_token('surgeon', health_system_id='other_hs')}"}
    r = client.post("/api/fhir/import", headers=other,
                    json={"patientId": "p1", "fhirPatientId": "okafor-margaret"})
    assert r.status_code == 404


def test_import_fhir_error_maps_to_502(client, auth, fhir_enabled, local_patient, monkeypatch):
    from integrations.fhir.client import FhirError

    async def boom(client_, fhir_patient_id):
        raise FhirError("FHIR server returned HTTP 500")

    monkeypatch.setattr(fhir_fetch, "fetch_eligibility_bundle", boom)
    r = client.post("/api/fhir/import", headers=auth,
                    json={"patientId": "p1", "fhirPatientId": "okafor-margaret"})
    assert r.status_code == 502


# ─── Full loop: imported doc feeds an eligibility check ─────────────────────
async def _fake_run_pipeline(check_id, patient, document_records, freeform_notes, surgery_date):
    rec = elig_store.get_check(check_id)
    if rec:
        rec["status"] = "DONE"
        rec["overall_verdict"] = "ELIGIBLE"


def test_imported_doc_attaches_to_eligibility_check(
    client, auth, fhir_enabled, stub_fetchers, local_patient, monkeypatch
):
    monkeypatch.setattr(elig_pipeline, "run_pipeline", _fake_run_pipeline)

    doc_id = client.post(
        "/api/fhir/import", headers=auth,
        json={"patientId": "p1", "fhirPatientId": "okafor-margaret"},
    ).json()["documents"][0]["id"]

    r = client.post("/api/eligibility-checks", headers=auth,
                    json={"patientId": "p1", "documentIds": [doc_id]})
    assert r.status_code == 202
    check = elig_store.get_check(r.json()["id"])
    assert check["document_ids"] == [doc_id]
