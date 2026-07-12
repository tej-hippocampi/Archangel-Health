"""Real EHR ingestion end-to-end (EHR Ingestion PRD §11 acceptance criteria).

Covers: secure-link mint → token upload (no app account) → expiry/one-time →
mixed bundle (FHIR + lab CSV + notes + manifest) → ONE assembled case with all
sections → B1 (zero date strings, guard passes) → planted identifier lands in
quarantine MASKED → DICOM entry rejected while the rest ingests → scrub/override/
reject triage → promote to a gradable V4 task (stubbed LLM) → the value premium.
"""

from __future__ import annotations

import base64
import io
import json
import sys
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import pipeline as asc_pipeline  # noqa: E402
from asclepius import profiles as asc_profiles  # noqa: E402

client = TestClient(A.app)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    A.fresh_store()
    asc_profiles.clear_cache()
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(tmp_path / "ingest"))

    async def _ok_critic(task, submission):
        return {"consistent": True, "issues": [], "skipped": True}

    async def _ok_grounding(task, submission):
        return {"grounding_ok": True, "issues": [], "skipped": True, "checked_anchors": 0}

    monkeypatch.setattr(asc_pipeline, "run_critic", _ok_critic)
    monkeypatch.setattr(asc_pipeline, "run_grounding_check", _ok_grounding)
    yield


def _store():
    from asclepius.store import get_store
    return get_store()


def _admin_h():
    return A.headers_for(A.make_user(_store(), role="admin"))


def _mint(admin_h, **over):
    body = {"partner_id": "mercy-health", "partner_label": "Mercy Health",
            "specialty": "nephrology", "expires_hours": 24, "one_time": True}
    body.update(over)
    r = client.post("/api/asclepius/admin/upload-links", json=body, headers=admin_h)
    assert r.status_code == 200, r.text
    return r.json()


def _zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


_CSV = """patient_key,panel,analyte,loinc,value,unit,ref_low,ref_high,flag,collected_at
p1,BMP,Sodium,2951-2,112,mmol/L,135,145,LL,2031-03-14
p1,BMP,Sodium,2951-2,124,mmol/L,135,145,L,2031-03-19
"""

_NOTE = "Consult note: admitted 3/14/2031 obtunded; slow correction started 2031-03-15."


def _fhir():
    return json.dumps({
        "resourceType": "Bundle", "type": "collection",
        "entry": [
            {"resource": {"resourceType": "Patient", "id": "pat-1", "gender": "male",
                          "birthDate": "1957-06-02"}},
            {"resource": {"resourceType": "Condition", "code": {"text": "Chronic thiazide use"}}},
            {"resource": {"resourceType": "MedicationStatement",
                          "medicationCodeableConcept": {"text": "Hydrochlorothiazide"}}},
            {"resource": {"resourceType": "Observation", "status": "final",
                          "category": [{"coding": [{"code": "laboratory"}]}],
                          "code": {"text": "Serum osmolality"},
                          "valueQuantity": {"value": 254, "unit": "mOsm/kg"},
                          "effectiveDateTime": "2031-03-19T07:00:00Z"}},
        ],
    })


def _manifest(**over):
    m = {"patient_key": "p1", "specialty": "nephrology", "index_event": "2031-03-19"}
    m.update(over)
    return json.dumps(m)


def _upload(token, zip_bytes, expect=200):
    r = client.post(f"/api/asclepius/partner/uploads?t={token}",
                    files={"file": ("bundle.zip", zip_bytes, "application/zip")})
    assert r.status_code == expect, r.text
    return r.json() if expect == 200 else r


# ─── Secure link lifecycle (PRD §4) ───────────────────────────────────────────
def test_link_mint_upload_and_one_time_reuse_blocked():
    link = _mint(_admin_h())
    assert link["token"] and "/partner/upload?t=" in link["upload_url"]
    zb = _zip({"manifest.json": _manifest(), "labs.csv": _CSV})
    res = _upload(link["token"], zb)
    assert res["status"] == "received" and res["sha256"]
    # one-time link: a second upload is 410
    r2 = client.post(f"/api/asclepius/partner/uploads?t={link['token']}",
                     files={"file": ("b2.zip", zb, "application/zip")})
    assert r2.status_code == 410
    # …but status polling still works for the upload already made
    st = client.get(f"/api/asclepius/partner/uploads/{res['upload_id']}?t={link['token']}")
    assert st.status_code == 200
    assert st.json()["status"] == "ingested"


def test_expired_and_revoked_links_rejected():
    admin_h = _admin_h()
    st = _store()
    link = _mint(admin_h)
    # Expire it directly in the DB (no time travel needed).
    with st._conn() as conn:  # noqa: SLF001
        conn.execute("UPDATE ingest_upload_links SET expires_at = ? WHERE link_id = ?",
                     ((datetime.utcnow() - timedelta(hours=1)).isoformat(), link["link_id"]))
    r = client.post(f"/api/asclepius/partner/uploads?t={link['token']}",
                    files={"file": ("b.zip", _zip({"n.txt": "x"}), "application/zip")})
    assert r.status_code == 410
    link2 = _mint(admin_h)
    client.post(f"/api/asclepius/admin/upload-links/{link2['link_id']}/revoke", headers=admin_h)
    r2 = client.post(f"/api/asclepius/partner/uploads?t={link2['token']}",
                     files={"file": ("b.zip", _zip({"n.txt": "x"}), "application/zip")})
    assert r2.status_code == 410
    bad = client.post("/api/asclepius/partner/uploads?t=not-a-token",
                      files={"file": ("b.zip", _zip({"n.txt": "x"}), "application/zip")})
    assert bad.status_code == 401


def test_raw_token_never_stored():
    link = _mint(_admin_h())
    rows = _store().list_upload_links()
    assert all(link["token"] not in json.dumps(r) for r in rows)  # only the hash at rest


# ─── The mixed bundle → ONE case (PRD §11 criterion 2 + 3, the B1 regression) ─
def test_mixed_bundle_assembles_one_case_with_all_sections():
    link = _mint(_admin_h())
    zb = _zip({
        "manifest.json": _manifest(),
        "fhir_export.json": _fhir(),
        "labs.csv": _CSV,
        "consult_note.txt": _NOTE,
        "progress_note.txt": "Progress: sodium improving on [prior plan]; family updated.",
    })
    res = _upload(link["token"], zb)
    st = client.get(f"/api/asclepius/partner/uploads/{res['upload_id']}?t={link['token']}").json()
    assert st["status"] == "ingested", st
    cases = _store().list_ingest_cases(upload_id=res["upload_id"])
    assert len(cases) == 1                                # ONE case per patient
    case = cases[0]["case"]
    assert case["case_source"] == "real_deid"
    # labs from CSV + FHIR merged; offsets are ints anchored to the manifest index
    all_offsets = sorted(lp["collected_offset_days"] for lp in case["lab_panels"])
    assert all_offsets == [-5, 0, 0]
    assert all(isinstance(o, int) for o in all_offsets)
    assert case["demographics"]["age_band"] == "70-79"    # band only, no birthdate
    assert case["problem_list"][0]["condition"] == "Chronic thiazide use"
    assert case["medications"][0]["drug"] == "Hydrochlorothiazide"
    assert len(case["notes"]) == 2
    # B1 explicit: zero date strings anywhere (notes included) + rewritten form
    blob = json.dumps(case)
    assert "2031" not in blob and "3/14" not in blob
    assert "[day -5]" in json.dumps(case["notes"])
    # chain of custody
    events = [e["event_type"] for e in _store().list_events(entity_type="ingest_upload", limit=50)]
    assert "upload_received" in events and "upload_processed" in events and "malware_scan" in events


# ─── Quarantine (PRD §11 criterion 4) + triage actions ────────────────────────
def test_planted_identifier_quarantines_with_masked_finding():
    link = _mint(_admin_h())
    dirty = _NOTE + " Call the family at 555-123-4567."
    zb = _zip({"manifest.json": _manifest(), "labs.csv": _CSV, "note.txt": dirty})
    res = _upload(link["token"], zb)
    st = client.get(f"/api/asclepius/partner/uploads/{res['upload_id']}?t={link['token']}").json()
    assert st["status"] == "quarantined"
    q = client.get("/api/asclepius/ingestion/quarantine", headers=_admin_h()).json()["cases"]
    assert len(q) == 1
    findings = q[0]["report"]["verification"]["findings"]
    assert findings
    blob = json.dumps(q[0])
    assert "555-123-4567" not in blob                     # NEVER cleartext
    assert any("•" in f["snippet_masked"] for f in findings)


def test_quarantine_scrub_redacts_exact_span_and_ingests():
    admin_h = _admin_h()
    link = _mint(admin_h)
    dirty = "Stable overnight. Callback 555-123-4567 if worse."
    zb = _zip({"manifest.json": _manifest(), "labs.csv": _CSV, "note.txt": dirty})
    res = _upload(link["token"], zb)
    qcase = _store().list_ingest_cases(status="quarantined")[0]
    r = client.post(f"/api/asclepius/ingestion/quarantine/{qcase['ingest_case_id']}/scrub",
                    headers=admin_h)
    assert r.status_code == 200 and r.json()["status"] == "ingested", r.text
    fixed = _store().get_ingest_case(qcase["ingest_case_id"])
    note_text = fixed["case"]["notes"][-1]["text"]
    assert "[redacted]" in note_text and "555" not in note_text
    assert "Stable overnight" in note_text                # only the span was touched


def test_quarantine_override_cannot_bypass_hard_guard():
    admin_h = _admin_h()
    link = _mint(admin_h)
    dirty = "Contact: jane.doe@example.com for follow-up."
    zb = _zip({"manifest.json": _manifest(), "labs.csv": _CSV, "note.txt": dirty})
    _upload(link["token"], zb)
    qcase = _store().list_ingest_cases(status="quarantined")[0]
    # Override requires a documented reason AND still runs deidentify() — the
    # email is real PHI, so the hard guard refuses (409).
    r = client.post(f"/api/asclepius/ingestion/quarantine/{qcase['ingest_case_id']}/override",
                    json={"reason": "reviewed: believed to be a false positive"}, headers=admin_h)
    assert r.status_code == 409
    r2 = client.post(f"/api/asclepius/ingestion/quarantine/{qcase['ingest_case_id']}/reject",
                     headers=admin_h)
    assert r2.status_code == 200


# ─── Imaging policy (PRD §11 criterion 5) ─────────────────────────────────────
def test_dicom_entry_rejected_rest_ingests():
    link = _mint(_admin_h())
    zb = _zip({"manifest.json": _manifest(), "labs.csv": _CSV,
               "scan.dcm": b"\x00" * 128 + b"DICM" + b"\x00" * 64})
    res = _upload(link["token"], zb)
    st = client.get(f"/api/asclepius/partner/uploads/{res['upload_id']}?t={link['token']}").json()
    assert st["status"] == "ingested"                     # the rest still lands
    detail = client.get(f"/api/asclepius/ingestion/uploads/{res['upload_id']}",
                        headers=_admin_h()).json()
    outcomes = {f["name"]: f["outcome"] for f in detail["files"]}
    assert outcomes["scan.dcm"] == "rejected_imaging"


def test_imaging_only_bundle_rejected():
    link = _mint(_admin_h())
    zb = _zip({"scan.dcm": b"\x00" * 128 + b"DICM"})
    res = _upload(link["token"], zb)
    st = client.get(f"/api/asclepius/partner/uploads/{res['upload_id']}?t={link['token']}").json()
    assert st["status"] == "rejected"


def test_zip_bomb_defenses():
    from asclepius import ingestion as ing
    with pytest.raises(ing.BundleRejected):
        ing.unpack_bundle(b"not a zip at all")
    traversal = _zip({"../../etc/passwd": "x", "ok.txt": "fine"})
    bundle = ing.unpack_bundle(traversal)
    kinds = {e["name"]: e["kind"] for e in bundle["entries"]}
    assert kinds["../../etc/passwd"] == "rejected"
    exe = ing.unpack_bundle(_zip({"payload.exe": "MZ", "ok.txt": "fine"}))
    kinds2 = {e["name"]: e["kind"] for e in exe["entries"]}
    assert kinds2["payload.exe"] == "rejected"


# ─── Promote → gradable V4 task (PRD §11 criterion 6) ─────────────────────────
def _stub_promote_llm(monkeypatch, coherence=0.9):
    from routers import asclepius as R
    from asclepius import critic

    async def fake_candidates(prompt, **kw):
        return {"candidates": [{"id": "A", "text": "Thiazide-associated hyponatremia; correct slowly."},
                               {"id": "B", "text": "SIADH; fluid restrict."}],
                "model": "cand", "intended_flawed_id": "B"}

    async def fake_hardness(prompt, candidates=None, **kw):
        return {"skipped": False, "hardness_score": 0.85, "hardness_axes": ["multi_step"]}

    async def fake_case_judge(case, case_source="synthetic"):
        assert case_source == "real_deid"
        return {"skipped": False, "coherence": coherence, "ground_truth_determinable": None,
                "multimodal_necessity": 0.85, "reasoning_divergence_potential": 0.7,
                "explanation": "", "model": "cj"}

    monkeypatch.setattr(R, "generate_candidates_ex", fake_candidates)
    monkeypatch.setattr(critic, "run_hardness_judge", fake_hardness)
    monkeypatch.setattr(critic, "run_case_judge", fake_case_judge)


def _ingest_one():
    link = _mint(_admin_h())
    zb = _zip({"manifest.json": _manifest(), "fhir.json": _fhir(),
               "labs.csv": _CSV, "note.txt": _NOTE})
    res = _upload(link["token"], zb)
    cases = _store().list_ingest_cases(upload_id=res["upload_id"], status="ingested")
    assert cases
    return cases[0]


def test_promote_creates_v4_task_served_only_to_v4(monkeypatch):
    _stub_promote_llm(monkeypatch)
    admin_h = _admin_h()
    ic = _ingest_one()
    r = client.post(f"/api/asclepius/ingestion/cases/{ic['ingest_case_id']}/promote",
                    json={"question": "Classify the hyponatremia and set a safe correction plan."},
                    headers=admin_h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["case_source"] == "real_deid" and body["modality"] == "multimodal"
    task = _store().get_task(body["task_id"])
    assert task["source"] == "partner_ehr" and task["difficulty"] == "hard"
    assert task["capture_reasoning"] is True
    assert "CLINICAL CASE" in task["prompt"] and "Sodium" in task["prompt"]
    assert (task["generation"] or {}).get("case_judge", {}).get("ground_truth_determinable") is None
    # Served ONLY to an approved v4 session (the wall).
    st = _store()
    ev = A.make_user(st, role="evaluator", specialty="nephrology",
                     board_cert="board_certified_nephrology", years_experience=12)
    st.set_real_data_approved(ev["id"], True)
    h = A.headers_for(st.get_user_by_id(ev["id"]))
    t3 = client.get("/api/asclepius/tasks/next?portal_version=v3", headers=h).json()["task"]
    assert t3 is None or (t3.get("case") or {}).get("case_source") != "real_deid"
    t4 = client.get("/api/asclepius/tasks/next?portal_version=v4", headers=h).json()["task"]
    assert t4 is not None and t4["task_id"] == body["task_id"]
    # promoted case is consumed (can't double-promote)
    r2 = client.post(f"/api/asclepius/ingestion/cases/{ic['ingest_case_id']}/promote",
                     json={"question": "again?"}, headers=admin_h)
    assert r2.status_code == 409


def test_promote_gates_on_real_case_judge_floors(monkeypatch):
    _stub_promote_llm(monkeypatch, coherence=0.2)   # below the 0.8 floor
    ic = _ingest_one()
    r = client.post(f"/api/asclepius/ingestion/cases/{ic['ingest_case_id']}/promote",
                    json={"question": "Classify."}, headers=_admin_h())
    assert r.status_code == 422
    assert "coherence" in json.dumps(r.json())
    assert _store().get_ingest_case(ic["ingest_case_id"])["status"] == "ingested"  # not consumed


# ─── Value premium (PRD §11 criterion 7) ──────────────────────────────────────
def test_real_case_value_premium_applies():
    from asclepius import value as V
    recs = [{"type": "preference"}, {"type": "ideal_answer"}]
    sub = {"grounded": False}
    synth = V.estimate_value(recs, {"difficulty": "hard", "modality": "multimodal",
                                    "case": {"case_source": "synthetic"}}, sub)
    real = V.estimate_value(recs, {"difficulty": "hard", "modality": "multimodal",
                                   "case": {"case_source": "real_deid"},
                                   "case_source": "real_deid"}, sub)
    from asclepius.constants import value_real_case_mult, value_tier_mult_cap
    assert real["realized_value"] > synth["realized_value"]
    assert real["tier_mult"] <= value_tier_mult_cap()   # still capped
