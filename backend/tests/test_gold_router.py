"""End-to-end HTTP tests for the Gold Standard router.

Drives the full capture → review → de-id → QA → export lifecycle through the
real FastAPI surface, with STT pinned to the offline stub and de-id pinned to
the deterministic regex baseline so no LLM / network is touched. Audio is
encrypted at rest (a test key is set) to exercise the decrypt-before-STT path.
"""

from __future__ import annotations

import asyncio
import base64
import os
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Env must be set before importing the app / stores (paths + providers resolved lazily).
_TMP = tempfile.mkdtemp(prefix="gold_test_")
os.environ["TEAM_DB_PATH"] = os.path.join(_TMP, "team.db")
os.environ["UPLOAD_DIR"] = os.path.join(_TMP, "uploads")
os.environ["STT_PROVIDER"] = "stub"
os.environ["GOLD_DEID_PROVIDER"] = "regex"
os.environ["GOLD_BAA_ON_FILE"] = "1"
os.environ["DATA_ENCRYPTION_KEY"] = base64.b64encode(os.urandom(32)).decode()
os.environ["DATA_ENCRYPTION_KEY_VERSION"] = "1"
os.environ.setdefault("RATE_LIMIT_ENABLED", "0")

import routers.gold as gold_router  # noqa: E402
from gold import schema as gold_schema  # noqa: E402
from main import app  # noqa: E402
from tests._role_auth import auth_headers, tenant_token  # noqa: E402

client = TestClient(app)


def _h(role="surgeon", **kw):
    return {"Authorization": f"Bearer {tenant_token(role, **kw)}"}


@pytest.fixture()
def _sync_bg(monkeypatch):
    """Neutralize the endpoints' fire-and-forget background tasks so the test can
    drive STT / de-id deterministically via asyncio.run()."""

    def _noop(coro):
        try:
            coro.close()
        except Exception:
            pass
        return None

    monkeypatch.setattr(gold_router.asyncio, "create_task", _noop)
    yield


def _run_pipeline_for(visit_id):
    from gold import store

    row = store.get_raw_row(visit_id)
    asyncio.run(gold_router._run_pipeline(visit_id, row["audio_path"], row["audio_mime"] or "audio/webm"))


def _drive_to_needs_review(headers, *, patient_name=None):
    r = client.post("/api/gold/visits", json={}, headers=headers)
    assert r.status_code == 200, r.text
    vid = r.json()["id"]
    consent = {"consent_given": True, "consent_method": "in_app_verbal"}
    if patient_name:
        consent["patient_name"] = patient_name
    r = client.post(f"/api/gold/visits/{vid}/consent", json=consent, headers=headers)
    assert r.status_code == 200, r.text
    files = {"file": ("visit.webm", b"\x00fake-audio-bytes" * 64, "audio/webm")}
    r = client.post(f"/api/gold/visits/{vid}/audio", data={"difficulty_tags": '["background_noise"]'}, files=files, headers=headers)
    assert r.status_code == 202, r.text
    _run_pipeline_for(vid)
    return vid


def test_taxonomy_requires_auth():
    assert client.get("/api/gold/taxonomy").status_code == 401
    r = client.get("/api/gold/taxonomy", headers=_h("surgeon"))
    assert r.status_code == 200
    assert any(t["type"] == "medication_error" for t in r.json()["types"])


def _operator(email="qa@hs.com"):
    """A second-person operator (different actor) for independent QA."""
    return _h("surgeon", email=email, is_team_director=True)


def test_full_lifecycle(_sync_bg):
    surgeon = _h("surgeon", email="dir@hs.com", is_team_director=True)
    operator = _operator()

    vid = _drive_to_needs_review(surgeon)

    v = client.get(f"/api/gold/visits/{vid}", headers=surgeon).json()
    assert v["status"] == "NEEDS_REVIEW"
    assert v["transcript"]  # stub transcript decrypted back
    assert v["stt_provider"] == "stub"

    # Submit gold record (include a phone in the note to verify de-id scrubbing).
    submit = {
        "gold_note": "Discontinue lisinopril; start cephalexin. Call clinic 415-555-1234.",
        "error_labels": [
            {"type": "medication_error", "subtype": "drug_discontinued_but_model_continued",
             "severity": "high", "section": "plan", "original_text": "Continue lisinopril",
             "corrected_text": "Discontinue lisinopril", "clinician_verified": True}
        ],
        "billing_codes": [{"system": "ICD-10", "code": "Z48.815", "verified_by": "clinician"}],
        "clinician_review_seconds": 33,
    }
    r = client.post(f"/api/gold/visits/{vid}/submit", json=submit, headers=surgeon)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "DEIDENTIFYING"

    asyncio.run(gold_router._run_deid(vid))

    v = client.get(f"/api/gold/visits/{vid}", headers=surgeon).json()
    assert v["status"] == "NEEDS_QA"
    assert "[PHONE]" in v["gold_note_deid"]
    assert "415-555-1234" not in v["gold_note_deid"]

    # Independent operator approve (different actor than submitter).
    r = client.post(f"/api/gold/visits/{vid}/approve", json={}, headers=operator)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "EXPORT_READY"

    # Export.
    r = client.post("/api/gold/export", json={"destination_label": "test-batch"}, headers=operator)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1
    assert body["jsonl"].strip()
    assert "Data Dictionary" in body["data_dictionary"]
    assert "Dataset Card" in body["dataset_card"]
    assert body["croissant"]["recordCount"] == 1

    rec = body["records"][0]
    assert rec["schema_version"] == gold_schema.SCHEMA_VERSION
    assert rec["content_sha256"]
    assert rec["correction"]["was_edited"] is True
    assert rec["correction"]["num_error_labels"] == 1
    assert "note_generation" in rec["tasks"] and "icd10_coding" in rec["tasks"]

    # The exported record validates against the schema.
    assert gold_schema.validate_record({**rec, "tenant_slug": "demo"}) == []

    # Visit is now EXPORTED.
    v = client.get(f"/api/gold/visits/{vid}", headers=surgeon).json()
    assert v["status"] == "EXPORTED"


def test_no_residual_phi_in_any_exported_field(_sync_bg):
    """A name + date + phone planted in the draft, a label, AND the prior-auth
    justification must NOT survive into the export (A1 + A3)."""
    import json as _json
    import re as _re

    surgeon = _h("surgeon", email="phi@hs.com", is_team_director=True)
    operator = _operator(email="phiqa@hs.com")

    vid = _drive_to_needs_review(surgeon, patient_name="John Smith")

    # Inject a PHI-bearing AI draft (normally produced by the LLM).
    from gold import store
    store.update_visit(
        vid,
        ai_draft_note="John Smith seen 03/04/2025; follow-up call 415-555-1234.",
    )

    submit = {
        "gold_note": "John Smith stable post-op. Call 415-555-1234 on 03/04/2025.",
        "error_labels": [
            {"type": "hallucination", "severity": "medium", "section": "plan",
             "original_text": "Call 212-555-9000", "corrected_text": "Call 415-555-1234",
             "clinician_verified": True}
        ],
        "billing_codes": [
            {"system": "ICD-10", "code": "Z48.815"},
            {"system": "CPT", "code": "99213"},
        ],
        "prior_auth": {
            "drug_or_service": "MRI lumbar",
            "justification_text": "John Smith failed PT; phone 415-555-1234, dated 03/04/2025.",
            "outcome": "pending",
        },
        "tasks": ["prior_auth"],
        "clinician_review_seconds": 40,
    }
    assert client.post(f"/api/gold/visits/{vid}/submit", json=submit, headers=surgeon).status_code == 200
    asyncio.run(gold_router._run_deid(vid))
    assert client.post(f"/api/gold/visits/{vid}/approve", json={}, headers=operator).status_code == 200

    r = client.post("/api/gold/export", json={"export_format": "both"}, headers=operator)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1
    rec = body["records"][0]

    blob = _json.dumps(rec)
    assert "415-555-1234" not in blob
    assert "212-555-9000" not in blob
    assert "03/04/2025" not in blob
    assert "John Smith" not in blob
    # tasks derived from billing + prior_auth + surgeon confirm.
    for t in ("note_generation", "icd10_coding", "cpt_coding", "prior_auth"):
        assert t in rec["tasks"]

    # sft_messages format is valid + clean.
    line = body["sft_jsonl"].strip().splitlines()[0]
    sft = _json.loads(line)
    roles = [m["role"] for m in sft["messages"]]
    assert roles == ["system", "user", "assistant"]
    assert sft["metadata"]["schema_version"] == gold_schema.SCHEMA_VERSION
    assert not _re.search(r"\d{3}-\d{3}-\d{4}", _json.dumps(sft))


def test_looks_correct_zero_edits(_sync_bg):
    surgeon = _h("surgeon", email="dir@hs.com", is_team_director=True)
    vid = _drive_to_needs_review(surgeon)
    r = client.post(
        f"/api/gold/visits/{vid}/submit",
        json={"gold_note": "Draft accepted as-is.", "error_labels": [], "billing_codes": []},
        headers=surgeon,
    )
    assert r.status_code == 200
    asyncio.run(gold_router._run_deid(vid))
    v = client.get(f"/api/gold/visits/{vid}", headers=surgeon).json()
    assert v["error_labels"] == []
    assert v["status"] == "NEEDS_QA"


def test_consent_declined_discards(_sync_bg):
    surgeon = _h("surgeon")
    vid = client.post("/api/gold/visits", json={}, headers=surgeon).json()["id"]
    r = client.post(f"/api/gold/visits/{vid}/consent", json={"consent_given": False}, headers=surgeon)
    assert r.status_code == 200
    # Visit is gone.
    assert client.get(f"/api/gold/visits/{vid}", headers=surgeon).status_code == 404
    stats = client.get("/api/gold/stats", headers=surgeon).json()
    assert stats["totals"]["declined"] >= 1


def test_role_gating():
    # np_pa cannot capture.
    assert client.post("/api/gold/visits", json={}, headers=_h("np_pa")).status_code == 403
    # rn can capture but cannot submit.
    rn = _h("rn_coordinator")
    vid = client.post("/api/gold/visits", json={}, headers=rn).json()["id"]
    r = client.post(f"/api/gold/visits/{vid}/submit", json={"gold_note": "x"}, headers=rn)
    assert r.status_code == 403


def test_non_operator_cannot_approve_or_export(_sync_bg):
    plain_surgeon = _h("surgeon", email="plain@hs.com", is_team_director=False)
    vid = _drive_to_needs_review(plain_surgeon)
    client.post(f"/api/gold/visits/{vid}/submit", json={"gold_note": "n", "error_labels": [], "billing_codes": []}, headers=plain_surgeon)
    asyncio.run(gold_router._run_deid(vid))
    assert client.post(f"/api/gold/visits/{vid}/approve", json={}, headers=plain_surgeon).status_code == 403
    assert client.post("/api/gold/export", json={}, headers=plain_surgeon).status_code == 403


def test_baa_gate_blocks_export(_sync_bg, monkeypatch):
    surgeon = _h("surgeon", email="dir2@hs.com", is_team_director=True)
    operator = _operator(email="qa2@hs.com")
    vid = _drive_to_needs_review(surgeon)
    client.post(f"/api/gold/visits/{vid}/submit", json={"gold_note": "n", "error_labels": [], "billing_codes": [{"system": "CPT", "code": "99213"}]}, headers=surgeon)
    asyncio.run(gold_router._run_deid(vid))
    client.post(f"/api/gold/visits/{vid}/approve", json={}, headers=operator)
    monkeypatch.setenv("GOLD_BAA_ON_FILE", "0")
    r = client.post("/api/gold/export", json={}, headers=operator)
    assert r.status_code == 409
    assert "BAA" in r.json()["detail"]


def test_self_qa_blocked_then_allowed_with_override(_sync_bg, monkeypatch):
    """A4: the submitter cannot approve their own record unless GOLD_ALLOW_SELF_QA."""
    surgeon = _h("surgeon", email="selfqa@hs.com", is_team_director=True)
    vid = _drive_to_needs_review(surgeon)
    client.post(f"/api/gold/visits/{vid}/submit", json={"gold_note": "ok", "error_labels": [], "billing_codes": []}, headers=surgeon)
    asyncio.run(gold_router._run_deid(vid))

    # Same actor → blocked.
    r = client.post(f"/api/gold/visits/{vid}/approve", json={}, headers=surgeon)
    assert r.status_code == 409
    assert "different person" in r.json()["detail"].lower()

    # A different operator can approve.
    other = _operator(email="othqa@hs.com")
    assert client.post(f"/api/gold/visits/{vid}/approve", json={}, headers=other).status_code == 200

    # Cannot re-approve an export-ready record.
    assert client.post(f"/api/gold/visits/{vid}/approve", json={}, headers=other).status_code == 409


def test_self_qa_allowed_with_env_override(_sync_bg, monkeypatch):
    monkeypatch.setenv("GOLD_ALLOW_SELF_QA", "1")
    surgeon = _h("surgeon", email="solo@hs.com", is_team_director=True)
    vid = _drive_to_needs_review(surgeon)
    client.post(f"/api/gold/visits/{vid}/submit", json={"gold_note": "ok", "error_labels": [], "billing_codes": []}, headers=surgeon)
    asyncio.run(gold_router._run_deid(vid))
    r = client.post(f"/api/gold/visits/{vid}/approve", json={}, headers=surgeon)
    assert r.status_code == 200


def test_stt_baa_gate_refuses_without_signed_baa(monkeypatch):
    """A2: a configured STT vendor with no signed BAA must refuse to transmit."""
    from compliance.subprocessors import SubprocessorPHIError
    from integrations.stt.whisper import WhisperSTTProvider

    monkeypatch.setenv("WHISPER_API_KEY", "sk-test")
    monkeypatch.delenv("WHISPER_BAA_SIGNED", raising=False)

    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as fh:
        fh.write(b"\x00audio")
        audio_path = fh.name

    with pytest.raises(SubprocessorPHIError):
        asyncio.run(WhisperSTTProvider().transcribe(audio_path=audio_path, mime_type="audio/webm"))

    os.remove(audio_path)


def test_stt_baa_gate_surfaces_as_pipeline_error(_sync_bg, monkeypatch):
    """A2: the BAA refusal becomes status=ERROR with a clear pipeline_error."""
    from gold import store

    surgeon = _h("surgeon", email="baastt@hs.com")
    r = client.post("/api/gold/visits", json={}, headers=surgeon)
    vid = r.json()["id"]
    client.post(f"/api/gold/visits/{vid}/consent", json={"consent_given": True}, headers=surgeon)
    files = {"file": ("v.webm", b"\x00fake" * 64, "audio/webm")}
    client.post(f"/api/gold/visits/{vid}/audio", files=files, headers=surgeon)

    monkeypatch.setenv("STT_PROVIDER", "whisper")
    monkeypatch.setenv("WHISPER_API_KEY", "sk-test")
    monkeypatch.delenv("WHISPER_BAA_SIGNED", raising=False)
    _run_pipeline_for(vid)

    v = store.get_raw_row(vid)
    assert v["status"] == store.ST_ERROR
    assert v["pipeline_error"] == "STT vendor has no BAA on file"


def test_tenant_isolation(_sync_bg):
    a = _h("surgeon", email="a@hs.com", health_system_id="hs_a")
    b = _h("surgeon", email="b@hs.com", health_system_id="hs_b")
    vid = client.post("/api/gold/visits", json={}, headers=a).json()["id"]
    # Tenant B cannot see tenant A's visit.
    assert client.get(f"/api/gold/visits/{vid}", headers=b).status_code == 404


def test_audio_retention_purge(_sync_bg, monkeypatch):
    import sqlite3

    from gold import retention, store

    surgeon = _h("surgeon", email="ret@hs.com")
    vid = _drive_to_needs_review(surgeon)  # uploads audio + sets transcript
    row = store.get_raw_row(vid)
    assert row["audio_path"] and os.path.exists(row["audio_path"])

    con = sqlite3.connect(os.environ["TEAM_DB_PATH"])
    con.execute("UPDATE gold_visits SET updated_at=? WHERE id=?", ("2000-01-01T00:00:00Z", vid))
    con.commit()
    con.close()

    monkeypatch.setenv("GOLD_AUDIO_RETENTION_DAYS", "30")
    assert retention.purge_expired_audio() >= 1
    assert not os.path.exists(row["audio_path"])
    assert store.get_raw_row(vid)["audio_deleted"] == 1


def test_audit_chain_intact_after_gold_ops(_sync_bg):
    from audit import audit_log

    surgeon = _h("surgeon", email="audit@hs.com", is_team_director=True)
    _drive_to_needs_review(surgeon)
    assert audit_log.verify()["ok"] is True
