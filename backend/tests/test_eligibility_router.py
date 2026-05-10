"""End-to-end router tests using FastAPI's TestClient.

Exercises the full HTTP surface (draft create / dup-MBI / docs / checks /
overrides / finalize / batch / notes / audit) without ever hitting the LLM.
The pipeline is monkey-patched to a deterministic stub so each test is
hermetic and runs in milliseconds.
"""

from __future__ import annotations

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
from main import app  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset all in-memory state between tests."""
    elig_store.ELIGIBILITY_CHECKS.clear()
    elig_store.ELIGIBILITY_DOCS.clear()
    elig_store.BATCHES.clear()
    elig_store.AUDIT_LOG.clear()
    elig_store._RATE_BUCKETS.clear()  # type: ignore[attr-defined]
    app.state.patient_store.clear()
    yield


# ─── Pipeline stub: deterministic, no LLM ──────────────────────────────────
async def _fake_run_pipeline(check_id, patient, document_records, freeform_notes, surgery_date):
    rec = elig_store.get_check(check_id)
    if not rec:
        return
    rec["status"] = "DONE"
    rec["stage"] = "DONE"
    rec["verdicts"] = {
        "partA_active": "PASS",
        "partB_active": "PASS",
        "not_ma": "PASS",
        "medicare_primary": "PASS",
        "not_esrd_basis": "PASS",
        "not_umwa": "PASS",
    }
    rec["overall_verdict"] = "ELIGIBLE"
    rec["extracted_fields"] = {"partA": {"status": "ACTIVE"}}
    rec["parse_meta"] = []
    if patient.get("eligibility_status") not in ("ELIGIBLE", "INELIGIBLE"):
        patient["eligibility_status"] = "ELIGIBLE"


@pytest.fixture
def stub_pipeline(monkeypatch):
    monkeypatch.setattr(elig_pipeline, "run_pipeline", _fake_run_pipeline)


@pytest.fixture
def client():
    return TestClient(app)


# ─── Draft patient lifecycle ───────────────────────────────────────────────
def test_create_draft_patient_basic(client):
    r = client.post("/api/eligibility-draft-patient", json={"name": "Test Patient"})
    assert r.status_code == 200
    body = r.json()
    assert body["eligibility_status"] == "DRAFT"
    pid = body["id"]
    assert pid in app.state.patient_store
    assert app.state.patient_store[pid]["is_draft"] is True


def test_create_draft_patient_rejects_empty_name(client):
    r = client.post("/api/eligibility-draft-patient", json={"name": "   "})
    assert r.status_code == 400


def test_create_draft_patient_rejects_too_long_name(client):
    r = client.post("/api/eligibility-draft-patient", json={"name": "x" * 121})
    assert r.status_code == 400


def test_create_draft_patient_invalid_mbi(client):
    r = client.post("/api/eligibility-draft-patient", json={"name": "Bob", "mbi": "ABC123"})
    assert r.status_code == 400


def test_create_draft_patient_duplicate_mbi_returns_existing(client):
    valid_mbi = "1AA1A11AA11"  # Pattern: digit, letter, alphanum, digit, letter, alphanum, digit, letter, letter, 2 digits
    r1 = client.post("/api/eligibility-draft-patient", json={"name": "Original Patient", "mbi": valid_mbi})
    assert r1.status_code == 200, r1.json()
    pid_1 = r1.json()["id"]

    r2 = client.post("/api/eligibility-draft-patient", json={"name": "Different Name", "mbi": valid_mbi})
    body = r2.json()
    assert body["id"] == pid_1
    assert body["conflict"] == "existing"
    assert body["existing_name"] == "Original Patient"


def test_delete_draft_patient_removes_record(client):
    r = client.post("/api/eligibility-draft-patient", json={"name": "Bob"})
    pid = r.json()["id"]
    d = client.delete(f"/api/eligibility-draft-patients/{pid}")
    assert d.status_code == 200
    assert pid not in app.state.patient_store


def test_delete_draft_patient_refuses_non_draft(client):
    r = client.post("/api/eligibility-draft-patient", json={"name": "Bob"})
    pid = r.json()["id"]
    app.state.patient_store[pid]["is_draft"] = False
    d = client.delete(f"/api/eligibility-draft-patients/{pid}")
    assert d.status_code == 409


def test_delete_missing_draft_patient_is_noop(client):
    d = client.delete("/api/eligibility-draft-patients/nonexistent-id")
    assert d.status_code == 200
    assert d.json() == {"ok": True, "already_gone": True}


# ─── Document upload / list / delete ──────────────────────────────────────
def _make_x12_271() -> bytes:
    return (
        "ISA*00*          *00*          *ZZ*SUBMITTER      "
        "*ZZ*RECEIVER       *241201*1200*^*00501*000000001*0*P*:~"
        "GS*HB*SUB*REC*20241201*1200*1*X*005010X279A1~"
        "ST*271*0001*005010X279A1~"
        "BHT*0022*11*REF*20241201*1200~"
        "HL*1**20*1~"
        "NM1*PR*2*PAYER*****PI*PAYER01~"
        "HL*2*1*21*1~"
        "NM1*1P*2*PROVIDER*****XX*1234567890~"
        "HL*3*2*22*0~"
        "NM1*IL*1*DOE*JANE****MI*1AA1A11AA11~"
        "DMG*D8*19500215*F~"
        "EB*1**30**MEDICARE A**********Y~"
        "EB*1**30**MEDICARE B**********Y~"
        "DTP*356*D8*20100101~"
        "SE*13*0001~"
        "GE*1*1~"
        "IEA*1*000000001~"
    ).encode()


def test_upload_document_attaches_to_patient(client):
    r = client.post("/api/eligibility-draft-patient", json={"name": "Bob"})
    pid = r.json()["id"]
    files = {"file": ("test.x12", _make_x12_271(), "application/octet-stream")}
    up = client.post(
        "/api/eligibility-documents",
        data={"patientId": pid},
        files=files,
    )
    assert up.status_code == 200, up.json()
    body = up.json()
    assert body["format"] == "X12_271"
    assert body["status"] == "validated"
    # Document recorded on patient
    assert body["id"] in app.state.patient_store[pid]["relevant_files"]


def test_upload_rejects_oversized_x12(client):
    r = client.post("/api/eligibility-draft-patient", json={"name": "Bob"})
    pid = r.json()["id"]
    big = b"ISA*" + b"X" * (6 * 1024 * 1024)  # 6 MB, X12 limit is 5 MB
    files = {"file": ("big.x12", big, "application/octet-stream")}
    up = client.post("/api/eligibility-documents", data={"patientId": pid}, files=files)
    assert up.status_code == 413


def test_upload_rejects_empty_file(client):
    r = client.post("/api/eligibility-draft-patient", json={"name": "Bob"})
    pid = r.json()["id"]
    files = {"file": ("empty.txt", b"", "text/plain")}
    up = client.post("/api/eligibility-documents", data={"patientId": pid}, files=files)
    assert up.status_code == 400


def test_upload_password_protected_pdf_returns_422(client, tmp_path):
    """PRD §11.2: encrypted PDFs are rejected at upload time with a clear error."""
    try:
        from pypdf import PdfWriter
    except ImportError:
        pytest.skip("pypdf not available")

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.encrypt("secret")
    out = tmp_path / "secret.pdf"
    with open(out, "wb") as fh:
        writer.write(fh)
    encrypted_bytes = out.read_bytes()

    r = client.post("/api/eligibility-draft-patient", json={"name": "Bob"})
    pid = r.json()["id"]
    files = {"file": ("secret.pdf", encrypted_bytes, "application/pdf")}
    up = client.post("/api/eligibility-documents", data={"patientId": pid}, files=files)
    assert up.status_code == 422
    assert "password" in up.json()["detail"].lower()


def test_list_patient_documents(client):
    r = client.post("/api/eligibility-draft-patient", json={"name": "Bob"})
    pid = r.json()["id"]
    files = {"file": ("test.x12", _make_x12_271(), "application/octet-stream")}
    up = client.post("/api/eligibility-documents", data={"patientId": pid}, files=files)
    doc_id = up.json()["id"]

    listing = client.get(f"/api/patient/{pid}/eligibility-documents")
    assert listing.status_code == 200
    docs = listing.json()["documents"]
    assert len(docs) == 1
    assert docs[0]["id"] == doc_id


def test_delete_document(client):
    r = client.post("/api/eligibility-draft-patient", json={"name": "Bob"})
    pid = r.json()["id"]
    files = {"file": ("test.x12", _make_x12_271(), "application/octet-stream")}
    up = client.post("/api/eligibility-documents", data={"patientId": pid}, files=files)
    doc_id = up.json()["id"]

    d = client.delete(f"/api/eligibility-documents/{doc_id}")
    assert d.status_code == 200
    listing = client.get(f"/api/patient/{pid}/eligibility-documents").json()
    assert len(listing["documents"]) == 0


# ─── Eligibility checks ────────────────────────────────────────────────────
def test_create_check_requires_surgery_date(client, stub_pipeline):
    r = client.post("/api/eligibility-draft-patient", json={"name": "Bob"})
    pid = r.json()["id"]
    files = {"file": ("test.x12", _make_x12_271(), "application/octet-stream")}
    up = client.post("/api/eligibility-documents", data={"patientId": pid}, files=files)
    doc_id = up.json()["id"]

    chk = client.post(
        "/api/eligibility-checks",
        json={"patientId": pid, "documentIds": [doc_id]},
    )
    assert chk.status_code == 400


def test_create_check_requires_doc_or_freeform(client, stub_pipeline):
    r = client.post(
        "/api/eligibility-draft-patient",
        json={"name": "Bob", "scheduled_surgery_date": "2026-09-01"},
    )
    pid = r.json()["id"]

    chk = client.post(
        "/api/eligibility-checks",
        json={"patientId": pid, "documentIds": []},
    )
    assert chk.status_code == 400


def test_full_check_flow_to_finalize_team(client, stub_pipeline):
    r = client.post(
        "/api/eligibility-draft-patient",
        json={"name": "Bob", "scheduled_surgery_date": "2026-09-01"},
    )
    pid = r.json()["id"]
    files = {"file": ("test.x12", _make_x12_271(), "application/octet-stream")}
    up = client.post("/api/eligibility-documents", data={"patientId": pid}, files=files)
    doc_id = up.json()["id"]

    chk = client.post(
        "/api/eligibility-checks",
        json={"patientId": pid, "documentIds": [doc_id]},
    )
    assert chk.status_code == 202
    check_id = chk.json()["id"]

    import asyncio
    asyncio.get_event_loop().run_until_complete(asyncio.sleep(0.05))

    g = client.get(f"/api/eligibility-checks/{check_id}")
    assert g.status_code == 200
    assert g.json()["overall_verdict"] == "ELIGIBLE"

    fin = client.post(
        f"/api/eligibility-checks/{check_id}/finalize",
        json={"decision": "SAVE_AS_TEAM"},
    )
    assert fin.status_code == 200
    assert app.state.patient_store[pid]["eligibility_status"] == "ELIGIBLE"
    assert app.state.patient_store[pid].get("is_draft") is None


def test_finalize_save_as_standard_clears_eligibility(client, stub_pipeline):
    """When user chooses Standard episode, badge should disappear."""
    r = client.post(
        "/api/eligibility-draft-patient",
        json={"name": "Bob", "scheduled_surgery_date": "2026-09-01"},
    )
    pid = r.json()["id"]
    files = {"file": ("test.x12", _make_x12_271(), "application/octet-stream")}
    up = client.post("/api/eligibility-documents", data={"patientId": pid}, files=files)
    doc_id = up.json()["id"]

    chk = client.post(
        "/api/eligibility-checks",
        json={"patientId": pid, "documentIds": [doc_id]},
    )
    check_id = chk.json()["id"]

    import asyncio
    asyncio.get_event_loop().run_until_complete(asyncio.sleep(0.05))

    fin = client.post(
        f"/api/eligibility-checks/{check_id}/finalize",
        json={"decision": "SAVE_AS_STANDARD"},
    )
    assert fin.status_code == 200
    # Saved as standard → no TEAM badge / no eligibility_status
    assert app.state.patient_store[pid]["eligibility_status"] is None
    assert app.state.patient_store[pid].get("is_draft") is None


def test_finalize_team_blocked_when_overall_not_eligible(client, stub_pipeline, monkeypatch):
    """Cannot save as TEAM when overall is BLOCKED_UNKNOWN — must override or rerun."""
    async def blocked_pipeline(check_id, patient, *_a, **_kw):
        rec = elig_store.get_check(check_id)
        rec["status"] = "DONE"
        rec["verdicts"] = {
            "partA_active": "PASS",
            "partB_active": "PASS",
            "not_ma": "UNKNOWN",
            "medicare_primary": "PASS",
            "not_esrd_basis": "PASS",
            "not_umwa": "PASS",
        }
        rec["overall_verdict"] = "BLOCKED_UNKNOWN"

    monkeypatch.setattr(elig_pipeline, "run_pipeline", blocked_pipeline)

    r = client.post(
        "/api/eligibility-draft-patient",
        json={"name": "Bob", "scheduled_surgery_date": "2026-09-01"},
    )
    pid = r.json()["id"]
    files = {"file": ("test.x12", _make_x12_271(), "application/octet-stream")}
    up = client.post("/api/eligibility-documents", data={"patientId": pid}, files=files)
    doc_id = up.json()["id"]

    chk = client.post("/api/eligibility-checks", json={"patientId": pid, "documentIds": [doc_id]})
    check_id = chk.json()["id"]
    import asyncio
    asyncio.get_event_loop().run_until_complete(asyncio.sleep(0.05))

    fin = client.post(
        f"/api/eligibility-checks/{check_id}/finalize",
        json={"decision": "SAVE_AS_TEAM"},
    )
    assert fin.status_code == 409


# ─── Override validation ──────────────────────────────────────────────────
def test_override_unknown_field_rejected(client, stub_pipeline):
    r = client.post(
        "/api/eligibility-draft-patient",
        json={"name": "Bob", "scheduled_surgery_date": "2026-09-01"},
    )
    pid = r.json()["id"]
    files = {"file": ("test.x12", _make_x12_271(), "application/octet-stream")}
    up = client.post("/api/eligibility-documents", data={"patientId": pid}, files=files)
    chk = client.post("/api/eligibility-checks", json={"patientId": pid, "documentIds": [up.json()["id"]]})
    check_id = chk.json()["id"]
    import asyncio
    asyncio.get_event_loop().run_until_complete(asyncio.sleep(0.05))

    o = client.post(
        f"/api/eligibility-checks/{check_id}/override",
        json={"field": "evil_field", "to": "PASS", "reason": "because"},
    )
    assert o.status_code == 400


def test_override_requires_reason(client, stub_pipeline):
    r = client.post(
        "/api/eligibility-draft-patient",
        json={"name": "Bob", "scheduled_surgery_date": "2026-09-01"},
    )
    pid = r.json()["id"]
    files = {"file": ("test.x12", _make_x12_271(), "application/octet-stream")}
    up = client.post("/api/eligibility-documents", data={"patientId": pid}, files=files)
    chk = client.post("/api/eligibility-checks", json={"patientId": pid, "documentIds": [up.json()["id"]]})
    check_id = chk.json()["id"]
    import asyncio
    asyncio.get_event_loop().run_until_complete(asyncio.sleep(0.05))

    o = client.post(
        f"/api/eligibility-checks/{check_id}/override",
        json={"field": "not_ma", "to": "PASS", "reason": ""},
    )
    assert o.status_code == 400


def test_override_invalid_to_value_rejected(client, stub_pipeline):
    r = client.post(
        "/api/eligibility-draft-patient",
        json={"name": "Bob", "scheduled_surgery_date": "2026-09-01"},
    )
    pid = r.json()["id"]
    files = {"file": ("test.x12", _make_x12_271(), "application/octet-stream")}
    up = client.post("/api/eligibility-documents", data={"patientId": pid}, files=files)
    chk = client.post("/api/eligibility-checks", json={"patientId": pid, "documentIds": [up.json()["id"]]})
    check_id = chk.json()["id"]
    import asyncio
    asyncio.get_event_loop().run_until_complete(asyncio.sleep(0.05))

    o = client.post(
        f"/api/eligibility-checks/{check_id}/override",
        json={"field": "not_ma", "to": "MAYBE", "reason": "test"},
    )
    assert o.status_code == 400


def test_override_persists_through_rerun(client, stub_pipeline):
    """PRD §11.16: overrides survive re-run."""
    r = client.post(
        "/api/eligibility-draft-patient",
        json={"name": "Bob", "scheduled_surgery_date": "2026-09-01"},
    )
    pid = r.json()["id"]
    files = {"file": ("test.x12", _make_x12_271(), "application/octet-stream")}
    up = client.post("/api/eligibility-documents", data={"patientId": pid}, files=files)
    chk = client.post("/api/eligibility-checks", json={"patientId": pid, "documentIds": [up.json()["id"]]})
    check_id = chk.json()["id"]
    import asyncio
    asyncio.get_event_loop().run_until_complete(asyncio.sleep(0.05))

    o = client.post(
        f"/api/eligibility-checks/{check_id}/override",
        json={"field": "not_ma", "to": "PASS", "reason": "phone-verified"},
    )
    assert o.status_code == 200

    rr = client.post(f"/api/eligibility-checks/{check_id}/rerun")
    assert rr.status_code == 200
    asyncio.get_event_loop().run_until_complete(asyncio.sleep(0.05))

    after = client.get(f"/api/eligibility-checks/{check_id}").json()
    assert after["overrides"]["not_ma"]["reason"] == "phone-verified"


# ─── Rate limiter ─────────────────────────────────────────────────────────
def test_rate_limit_blocks_after_30_requests(client, stub_pipeline):
    r = client.post(
        "/api/eligibility-draft-patient",
        json={"name": "Bob", "scheduled_surgery_date": "2026-09-01"},
    )
    pid = r.json()["id"]
    files = {"file": ("test.x12", _make_x12_271(), "application/octet-stream")}
    up = client.post("/api/eligibility-documents", data={"patientId": pid}, files=files)
    doc_id = up.json()["id"]

    last_status = None
    for i in range(31):
        last = client.post(
            "/api/eligibility-checks",
            json={"patientId": pid, "documentIds": [doc_id]},
        )
        last_status = last.status_code
    assert last_status == 429


# ─── Notes / Track B ──────────────────────────────────────────────────────
def test_postop_notes_get_default_empty(client):
    r = client.post("/api/eligibility-draft-patient", json={"name": "Bob"})
    pid = r.json()["id"]
    g = client.get(f"/api/patient/{pid}/postop-notes")
    assert g.status_code == 200
    assert g.json()["text"] == ""
    assert g.json()["source"] == "ai"


def test_postop_confirm_rejects_empty(client):
    r = client.post("/api/eligibility-draft-patient", json={"name": "Bob"})
    pid = r.json()["id"]
    p = client.post(f"/api/patient/{pid}/postop-notes/confirm", json={"text": "   "})
    assert p.status_code == 400


# ─── Audit ────────────────────────────────────────────────────────────────
def test_audit_endpoint_requires_auth(client):
    """Anonymous callers must NOT see the audit log (PHI leak prevention)."""
    r = client.get("/admin/audit/eligibility")
    assert r.status_code == 401


# ─── Error paths ──────────────────────────────────────────────────────────
def test_get_check_not_found(client):
    g = client.get("/api/eligibility-checks/does-not-exist")
    assert g.status_code == 404


def test_finalize_invalid_decision(client, stub_pipeline):
    r = client.post(
        "/api/eligibility-draft-patient",
        json={"name": "Bob", "scheduled_surgery_date": "2026-09-01"},
    )
    pid = r.json()["id"]
    files = {"file": ("test.x12", _make_x12_271(), "application/octet-stream")}
    up = client.post("/api/eligibility-documents", data={"patientId": pid}, files=files)
    chk = client.post("/api/eligibility-checks", json={"patientId": pid, "documentIds": [up.json()["id"]]})
    check_id = chk.json()["id"]

    fin = client.post(
        f"/api/eligibility-checks/{check_id}/finalize",
        json={"decision": "WHATEVER"},
    )
    assert fin.status_code == 400


def test_create_check_unknown_document(client, stub_pipeline):
    r = client.post(
        "/api/eligibility-draft-patient",
        json={"name": "Bob", "scheduled_surgery_date": "2026-09-01"},
    )
    pid = r.json()["id"]
    chk = client.post(
        "/api/eligibility-checks",
        json={"patientId": pid, "documentIds": ["nonexistent-doc-id"]},
    )
    assert chk.status_code == 404


# ─── Defense-in-depth: cross-patient document attach ──────────────────────
def test_create_check_rejects_other_patients_document(client, stub_pipeline):
    """A doctor must not be able to attach another patient's docs to their check."""
    a = client.post(
        "/api/eligibility-draft-patient",
        json={"name": "Alice", "scheduled_surgery_date": "2026-09-01"},
    ).json()["id"]
    b = client.post(
        "/api/eligibility-draft-patient",
        json={"name": "Bob", "scheduled_surgery_date": "2026-09-01"},
    ).json()["id"]
    files = {"file": ("test.x12", _make_x12_271(), "application/octet-stream")}
    up_a = client.post("/api/eligibility-documents", data={"patientId": a}, files=files)
    doc_id = up_a.json()["id"]

    chk = client.post(
        "/api/eligibility-checks",
        json={"patientId": b, "documentIds": [doc_id]},
    )
    assert chk.status_code == 403


# ─── Surgery date validation ───────────────────────────────────────────────
def test_draft_patient_rejects_malformed_surgery_date(client):
    r = client.post(
        "/api/eligibility-draft-patient",
        json={"name": "Bob", "scheduled_surgery_date": "tomorrow"},
    )
    assert r.status_code == 400


def test_draft_patient_rejects_malformed_dob(client):
    r = client.post(
        "/api/eligibility-draft-patient",
        json={"name": "Bob", "dob": "not-a-date"},
    )
    assert r.status_code == 400


def test_create_check_rejects_malformed_surgery_date(client, stub_pipeline):
    r = client.post(
        "/api/eligibility-draft-patient",
        json={"name": "Bob", "scheduled_surgery_date": "2026-09-01"},
    )
    pid = r.json()["id"]
    files = {"file": ("test.x12", _make_x12_271(), "application/octet-stream")}
    up = client.post("/api/eligibility-documents", data={"patientId": pid}, files=files)

    chk = client.post(
        "/api/eligibility-checks",
        json={"patientId": pid, "documentIds": [up.json()["id"]], "surgeryDate": "2026-13-99"},
    )
    assert chk.status_code == 400


# ─── Freeform notes length cap ────────────────────────────────────────────
def test_create_check_freeform_notes_length_cap(client, stub_pipeline):
    r = client.post(
        "/api/eligibility-draft-patient",
        json={"name": "Bob", "scheduled_surgery_date": "2026-09-01"},
    )
    pid = r.json()["id"]
    huge = "x" * 60_000  # >50KB
    chk = client.post(
        "/api/eligibility-checks",
        json={"patientId": pid, "documentIds": [], "freeformNotes": huge},
    )
    assert chk.status_code == 413


# ─── In-flight pipeline guards ────────────────────────────────────────────
def test_finalize_rejected_while_pipeline_running(client, monkeypatch):
    """Finalize must not silently disagree with verdicts that arrive moments later."""
    import asyncio as _asyncio

    finished = _asyncio.Event()

    async def slow_pipeline(check_id, patient, *_a, **_kw):
        rec = elig_store.get_check(check_id)
        rec["status"] = "EXTRACTING"
        try:
            await _asyncio.wait_for(finished.wait(), timeout=2.0)
        except _asyncio.TimeoutError:
            pass
        rec["status"] = "DONE"
        rec["verdicts"] = {}
        rec["overall_verdict"] = "ELIGIBLE"

    monkeypatch.setattr(elig_pipeline, "run_pipeline", slow_pipeline)
    r = client.post(
        "/api/eligibility-draft-patient",
        json={"name": "Bob", "scheduled_surgery_date": "2026-09-01"},
    )
    pid = r.json()["id"]
    files = {"file": ("test.x12", _make_x12_271(), "application/octet-stream")}
    up = client.post("/api/eligibility-documents", data={"patientId": pid}, files=files)
    chk = client.post("/api/eligibility-checks", json={"patientId": pid, "documentIds": [up.json()["id"]]})
    check_id = chk.json()["id"]

    fin = client.post(
        f"/api/eligibility-checks/{check_id}/finalize",
        json={"decision": "SAVE_AS_TEAM"},
    )
    assert fin.status_code == 409
    assert "still running" in fin.json()["detail"]
    finished.set()


def test_override_rejected_while_pipeline_running(client, monkeypatch):
    import asyncio as _asyncio

    finished = _asyncio.Event()

    async def slow_pipeline(check_id, patient, *_a, **_kw):
        rec = elig_store.get_check(check_id)
        rec["status"] = "EXTRACTING"
        try:
            await _asyncio.wait_for(finished.wait(), timeout=2.0)
        except _asyncio.TimeoutError:
            pass
        rec["status"] = "DONE"
        rec["verdicts"] = {}

    monkeypatch.setattr(elig_pipeline, "run_pipeline", slow_pipeline)
    r = client.post(
        "/api/eligibility-draft-patient",
        json={"name": "Bob", "scheduled_surgery_date": "2026-09-01"},
    )
    pid = r.json()["id"]
    files = {"file": ("test.x12", _make_x12_271(), "application/octet-stream")}
    up = client.post("/api/eligibility-documents", data={"patientId": pid}, files=files)
    chk = client.post("/api/eligibility-checks", json={"patientId": pid, "documentIds": [up.json()["id"]]})
    check_id = chk.json()["id"]

    o = client.post(
        f"/api/eligibility-checks/{check_id}/override",
        json={"field": "not_ma", "to": "PASS", "reason": "x"},
    )
    assert o.status_code == 409
    finished.set()


# ─── Roster filters drafts ─────────────────────────────────────────────────
def test_draft_patient_excluded_from_roster(client):
    """Draft patients (cancelled-but-not-yet-deleted) should not appear in the roster."""
    r = client.post("/api/eligibility-draft-patient", json={"name": "Ghost"})
    pid = r.json()["id"]

    # Simulate a Network blip during cleanup — patient stays in store_dict
    # with is_draft=True. The roster must not show it.
    roster = client.get("/api/patients").json()["patients"]
    visible_ids = [p["id"] for p in roster]
    assert pid not in visible_ids


def test_finalized_patient_appears_in_roster(client, stub_pipeline):
    """After SAVE_AS_TEAM, is_draft is removed and the patient is visible."""
    r = client.post(
        "/api/eligibility-draft-patient",
        json={"name": "Real Patient", "scheduled_surgery_date": "2026-09-01"},
    )
    pid = r.json()["id"]
    files = {"file": ("test.x12", _make_x12_271(), "application/octet-stream")}
    up = client.post("/api/eligibility-documents", data={"patientId": pid}, files=files)
    chk = client.post("/api/eligibility-checks", json={"patientId": pid, "documentIds": [up.json()["id"]]})
    check_id = chk.json()["id"]

    import asyncio
    asyncio.get_event_loop().run_until_complete(asyncio.sleep(0.05))

    fin = client.post(
        f"/api/eligibility-checks/{check_id}/finalize",
        json={"decision": "SAVE_AS_TEAM"},
    )
    assert fin.status_code == 200

    roster = client.get("/api/patients").json()["patients"]
    visible_ids = [p["id"] for p in roster]
    assert pid in visible_ids


# ─── Batch endpoint ───────────────────────────────────────────────────────
def test_batch_endpoint_rejects_empty_files(client):
    """No files at all → 400."""
    r = client.post("/api/eligibility-batches", files=[])
    # Empty files list is rejected by FastAPI validation as 422 OR by our 400 — both acceptable
    assert r.status_code in (400, 422)


def test_batch_endpoint_rejects_zero_byte_files(client):
    files = [("files", ("empty.csv", b"", "text/csv"))]
    r = client.post("/api/eligibility-batches", files=files)
    assert r.status_code == 400


# ─── Audit log size cap ────────────────────────────────────────────────────
# ─── Concurrent check / rerun guards ──────────────────────────────────────
def test_create_check_refuses_when_prior_in_flight(client, monkeypatch):
    """Two concurrent pipelines on one patient would race the `eligibility_status` field."""
    import asyncio as _asyncio

    finished = _asyncio.Event()

    async def slow_pipeline(check_id, patient, *_a, **_kw):
        rec = elig_store.get_check(check_id)
        rec["status"] = "EXTRACTING"
        try:
            await _asyncio.wait_for(finished.wait(), timeout=2.0)
        except _asyncio.TimeoutError:
            pass
        rec["status"] = "DONE"
        rec["overall_verdict"] = "ELIGIBLE"

    monkeypatch.setattr(elig_pipeline, "run_pipeline", slow_pipeline)
    r = client.post(
        "/api/eligibility-draft-patient",
        json={"name": "Bob", "scheduled_surgery_date": "2026-09-01"},
    )
    pid = r.json()["id"]
    files = {"file": ("test.x12", _make_x12_271(), "application/octet-stream")}
    up = client.post("/api/eligibility-documents", data={"patientId": pid}, files=files)
    doc_id = up.json()["id"]

    first = client.post("/api/eligibility-checks", json={"patientId": pid, "documentIds": [doc_id]})
    assert first.status_code == 202

    second = client.post("/api/eligibility-checks", json={"patientId": pid, "documentIds": [doc_id]})
    assert second.status_code == 409
    assert "already running" in second.json()["detail"]
    finished.set()


def test_rerun_refuses_while_in_flight(client, monkeypatch):
    import asyncio as _asyncio

    finished = _asyncio.Event()

    async def slow_pipeline(check_id, patient, *_a, **_kw):
        rec = elig_store.get_check(check_id)
        rec["status"] = "EXTRACTING"
        try:
            await _asyncio.wait_for(finished.wait(), timeout=2.0)
        except _asyncio.TimeoutError:
            pass
        rec["status"] = "DONE"

    monkeypatch.setattr(elig_pipeline, "run_pipeline", slow_pipeline)
    r = client.post(
        "/api/eligibility-draft-patient",
        json={"name": "Bob", "scheduled_surgery_date": "2026-09-01"},
    )
    pid = r.json()["id"]
    files = {"file": ("test.x12", _make_x12_271(), "application/octet-stream")}
    up = client.post("/api/eligibility-documents", data={"patientId": pid}, files=files)
    chk = client.post("/api/eligibility-checks", json={"patientId": pid, "documentIds": [up.json()["id"]]})
    check_id = chk.json()["id"]

    rr = client.post(f"/api/eligibility-checks/{check_id}/rerun")
    assert rr.status_code == 409
    finished.set()


def test_rerun_refused_after_finalize(client, stub_pipeline):
    r = client.post(
        "/api/eligibility-draft-patient",
        json={"name": "Bob", "scheduled_surgery_date": "2026-09-01"},
    )
    pid = r.json()["id"]
    files = {"file": ("test.x12", _make_x12_271(), "application/octet-stream")}
    up = client.post("/api/eligibility-documents", data={"patientId": pid}, files=files)
    chk = client.post("/api/eligibility-checks", json={"patientId": pid, "documentIds": [up.json()["id"]]})
    check_id = chk.json()["id"]
    import asyncio
    asyncio.get_event_loop().run_until_complete(asyncio.sleep(0.05))
    fin = client.post(f"/api/eligibility-checks/{check_id}/finalize", json={"decision": "SAVE_AS_TEAM"})
    assert fin.status_code == 200

    rr = client.post(f"/api/eligibility-checks/{check_id}/rerun")
    assert rr.status_code == 409


def test_finalize_refused_when_already_finalized(client, stub_pipeline):
    r = client.post(
        "/api/eligibility-draft-patient",
        json={"name": "Bob", "scheduled_surgery_date": "2026-09-01"},
    )
    pid = r.json()["id"]
    files = {"file": ("test.x12", _make_x12_271(), "application/octet-stream")}
    up = client.post("/api/eligibility-documents", data={"patientId": pid}, files=files)
    chk = client.post("/api/eligibility-checks", json={"patientId": pid, "documentIds": [up.json()["id"]]})
    check_id = chk.json()["id"]
    import asyncio
    asyncio.get_event_loop().run_until_complete(asyncio.sleep(0.05))
    fin1 = client.post(f"/api/eligibility-checks/{check_id}/finalize", json={"decision": "SAVE_AS_TEAM"})
    assert fin1.status_code == 200

    fin2 = client.post(f"/api/eligibility-checks/{check_id}/finalize", json={"decision": "SAVE_AS_TEAM"})
    assert fin2.status_code == 409


def test_override_refused_after_finalize(client, stub_pipeline):
    r = client.post(
        "/api/eligibility-draft-patient",
        json={"name": "Bob", "scheduled_surgery_date": "2026-09-01"},
    )
    pid = r.json()["id"]
    files = {"file": ("test.x12", _make_x12_271(), "application/octet-stream")}
    up = client.post("/api/eligibility-documents", data={"patientId": pid}, files=files)
    chk = client.post("/api/eligibility-checks", json={"patientId": pid, "documentIds": [up.json()["id"]]})
    check_id = chk.json()["id"]
    import asyncio
    asyncio.get_event_loop().run_until_complete(asyncio.sleep(0.05))
    fin = client.post(f"/api/eligibility-checks/{check_id}/finalize", json={"decision": "SAVE_AS_TEAM"})
    assert fin.status_code == 200

    o = client.post(
        f"/api/eligibility-checks/{check_id}/override",
        json={"field": "not_ma", "to": "PASS", "reason": "test"},
    )
    assert o.status_code == 409


def test_orphan_doc_can_be_deleted(client):
    """A document attached to a patient that was already deleted should still be deletable."""
    r = client.post("/api/eligibility-draft-patient", json={"name": "Bob"})
    pid = r.json()["id"]
    files = {"file": ("test.x12", _make_x12_271(), "application/octet-stream")}
    up = client.post("/api/eligibility-documents", data={"patientId": pid}, files=files)
    doc_id = up.json()["id"]

    # Forcibly drop the patient from the store, leaving the doc orphaned
    app.state.patient_store.pop(pid, None)

    d = client.delete(f"/api/eligibility-documents/{doc_id}")
    assert d.status_code == 200


def test_audit_log_trimmed_to_max(client):
    from eligibility import store as s

    # Set a low cap for testing
    original = s.AUDIT_LOG_MAX
    s.AUDIT_LOG_MAX = 10
    try:
        for i in range(25):
            s.append_audit(action=f"test_{i}", actor="test")
        assert len(s.AUDIT_LOG) <= 10
        # Newest preserved
        actions = [e["action"] for e in s.AUDIT_LOG]
        assert "test_24" in actions
        assert "test_0" not in actions
    finally:
        s.AUDIT_LOG_MAX = original


# ─── Roster: failing-rule tooltip plumbing ─────────────────────────────────
def test_roster_surfaces_first_failing_rule_for_ineligible_patient(client):
    """Backend must compute eligibilityFailingRule from the check's verdicts so
    the frontend can render a hover tooltip on the 'Not TEAM eligible' badge.
    """
    pid = "ineligible-patient-1"
    check_id = "check-ineligible-1"
    elig_store.save_check(
        check_id,
        {
            "id": check_id,
            "patient_id": pid,
            "verdicts": {
                "partA_active": "PASS",
                "partB_active": "PASS",
                "not_ma": "FAIL",
                "medicare_primary": "PASS",
                "not_esrd_basis": "PASS",
                "not_umwa": "PASS",
            },
            "overall_verdict": "INELIGIBLE",
            "status": "DONE",
        },
    )
    app.state.patient_store[pid] = {
        "name": "Robert Hayes",
        "structured_data": {
            "patient_name": "Robert Hayes",
            "procedure_name": "Total Hip Replacement",
            "procedure_date": "2026-06-20",
        },
        "eligibility_status": "INELIGIBLE",
        "eligibility_check_id": check_id,
        "pipeline_type": "pre_op",
    }

    r = client.get("/api/patients")
    assert r.status_code == 200
    rows = r.json()["patients"]
    row = next(p for p in rows if p["id"] == pid)
    assert row["eligibilityStatus"] == "INELIGIBLE"
    assert row["eligibilityFailingRule"] == "Medicare Advantage"


def test_roster_failing_rule_null_for_eligible_patient(client):
    pid = "eligible-1"
    check_id = "check-elig-1"
    elig_store.save_check(
        check_id,
        {
            "id": check_id,
            "patient_id": pid,
            "verdicts": {
                "partA_active": "PASS",
                "partB_active": "PASS",
                "not_ma": "PASS",
                "medicare_primary": "PASS",
                "not_esrd_basis": "PASS",
                "not_umwa": "PASS",
            },
            "overall_verdict": "ELIGIBLE",
            "status": "DONE",
        },
    )
    app.state.patient_store[pid] = {
        "name": "Margaret O'Sullivan",
        "structured_data": {"procedure_name": "TKR", "procedure_date": "2026-06-15"},
        "eligibility_status": "ELIGIBLE",
        "eligibility_check_id": check_id,
        "pipeline_type": "pre_op",
    }
    rows = client.get("/api/patients").json()["patients"]
    row = next(p for p in rows if p["id"] == pid)
    assert row["eligibilityFailingRule"] is None


def test_roster_failing_rule_picks_highest_priority_first(client):
    """When multiple rules fail, surface the most informative one first."""
    pid = "ineligible-multi"
    check_id = "check-multi"
    elig_store.save_check(
        check_id,
        {
            "id": check_id,
            "patient_id": pid,
            "verdicts": {
                "partA_active": "FAIL",
                "partB_active": "PASS",
                "not_ma": "PASS",
                "medicare_primary": "PASS",
                "not_esrd_basis": "FAIL",
                "not_umwa": "PASS",
            },
            "overall_verdict": "INELIGIBLE",
            "status": "DONE",
        },
    )
    app.state.patient_store[pid] = {
        "name": "Patricia Lin",
        "structured_data": {"procedure_name": "Spine", "procedure_date": "2026-07-10"},
        "eligibility_status": "INELIGIBLE",
        "eligibility_check_id": check_id,
        "pipeline_type": "pre_op",
    }
    rows = client.get("/api/patients").json()["patients"]
    row = next(p for p in rows if p["id"] == pid)
    # ESRD basis ranks ahead of partA_active in _TEAM_FAIL_ORDER
    assert row["eligibilityFailingRule"] == "ESRD-basis entitlement"


# ─── Prep notes for batch-onboarded patients ───────────────────────────────
def test_preop_notes_endpoint_returns_batch_extracted_notes(client):
    """Batch onboarding writes pre_op_instructions into structured_data; the
    /preop-notes endpoint must surface that text so 'Revise Prep Notes' opens
    with content (not a blank textarea)."""
    pid = "batch-margaret"
    app.state.patient_store[pid] = {
        "name": "Margaret O'Sullivan",
        "structured_data": {
            "patient_name": "Margaret O'Sullivan",
            "procedure_name": "TKR",
            "procedure_date": "2026-06-15",
            "pre_op_instructions": (
                "1. STOP eating solid food at midnight. 2. HOLD lisinopril. "
                "3. Shower with chlorhexidine 4%. 4. Arrive at 0530."
            ),
        },
        "pipeline_type": "pre_op",
    }
    r = client.get(f"/api/patient/{pid}/preop-notes")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "ai"
    assert "STOP eating solid food" in body["text"]
    assert "Shower with chlorhexidine" in body["text"]
