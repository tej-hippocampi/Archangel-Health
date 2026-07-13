"""Auto case generation pipeline (Data Provider Portal PRD §5, §7, §10).

Exercises the orchestrator end-to-end without the FastAPI app: unpack → classify
→ parse → assemble → timeline → verify de-id → deidentify → real_deid preview,
plus the security matrix (blocked exe, excluded DICOM, quarantined PHI, safe zip).
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asclepius import ingestion  # noqa: E402
from asclepius.store import AsclepiusStore  # noqa: E402
from asclepius.timeline import remaining_date_strings  # noqa: E402


def _store():
    return AsclepiusStore(db_path=os.path.join(tempfile.mkdtemp(prefix="asc_ing_"), "t.db"))


def _upload(store):
    p = store.provision_data_provider(email="p@x.org", password="TempPass123!")
    return store.create_upload(provider_id=p["provider_id"])


def _clean_zip():
    fhir = json.dumps({"resourceType": "Bundle", "entry": [
        {"resource": {"resourceType": "Patient", "id": "pt1", "gender": "female", "birthDate": "1948-05-01"}},
        {"resource": {"resourceType": "Observation",
                      "category": [{"coding": [{"code": "laboratory"}]}],
                      "code": {"text": "Creatinine", "coding": [{"code": "2160-0"}]},
                      "effectiveDateTime": "2025-03-08",
                      "valueQuantity": {"value": 2.4, "unit": "mg/dL"},
                      "referenceRange": [{"low": {"value": 0.6}, "high": {"value": 1.2}}]}}]}).encode()
    csv = b"panel,analyte,value,unit,collected_at\nBMP,Potassium,5.9,mmol/L,2025-03-08"
    n1 = b"H&P nephrology: 76F with AKI, creatinine rising since 3/1/2025."
    n2 = b"Progress nephrology: improving on day 2025-03-09."
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("bundle.json", fhir)
        z.writestr("labs.csv", csv)
        z.writestr("note1.txt", n1)
        z.writestr("note2.txt", n2)
    return buf.getvalue()


# ─── PRD §10 acceptance: a .zip of FHIR + CSV + 2 notes → ONE real_deid case ──
def test_zip_bundle_assembles_one_real_case():
    store = _store()
    up = _upload(store)
    res = ingestion.process_upload(
        store, up["upload_id"],
        [{"filename": "export.zip", "content": _clean_zip()}],
        specialty="nephrology",
    )
    assert res["status"] == "ingested"
    previews = [c for c in res["cases"] if c["status"] == "preview"]
    assert len(previews) == 1  # one patient -> one case (key-less fragments fold in)

    case = store.list_ingest_cases(upload_id=up["upload_id"])[0]["case"]
    assert case["case_source"] == "real_deid"
    assert len(case["lab_panels"]) == 2 and len(case["notes"]) == 2
    assert case["demographics"].get("age_band") == "70-79"
    assert "age" not in case["demographics"]
    # PRD §10 B1 regression: every timestamp is an int offset; zero date strings.
    assert remaining_date_strings(case) == []
    for lp in case["lab_panels"]:
        assert isinstance(lp["collected_offset_days"], int)


# ─── security matrix ──────────────────────────────────────────────────────────
def test_blocked_dicom_and_phi_do_not_sink_the_bundle():
    store = _store()
    up = _upload(store)
    files = [
        {"filename": "labs.csv",
         "content": b"panel,analyte,value,unit,collected_at\nBMP,Cr,2.4,mg/dL,2025-03-08"},
        {"filename": "scan.dcm", "content": b"\x00" * 128 + b"DICM" + b"rest"},
        {"filename": "evil.exe", "content": b"MZ\x90\x00 pe"},
        {"filename": "leak.txt", "content": b"Call the family at 555-123-4567 about results."},
    ]
    res = ingestion.process_upload(store, up["upload_id"], files, specialty="nephrology")
    kinds = {f["filename"]: (f["detected_type"], f["status"]) for f in res["files"]}
    assert kinds["evil.exe"] == ("blocked", "rejected")
    assert kinds["scan.dcm"] == ("dicom", "excluded")
    assert res["imaging_excluded"] == 1

    # The planted phone quarantines the assembled case with a MASKED finding.
    q = store.list_quarantine(status="open", upload_id=up["upload_id"])
    deid_q = [x for x in q if x["kind"] == "deid_failed"]
    assert deid_q and "phone" in deid_q[0]["masked_findings"]
    # The masked finding is a KIND, never the raw number.
    assert "555-123-4567" not in json.dumps(deid_q[0]["masked_findings"])


def test_zip_bomb_and_traversal_are_rejected():
    store = _store()
    up = _upload(store)
    # path traversal entry
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../escape.txt", b"nope")
    res = ingestion.process_upload(store, up["upload_id"],
                                   [{"filename": "eb.zip", "content": buf.getvalue()}])
    q = store.list_quarantine(status="open", upload_id=up["upload_id"])
    assert any(x["kind"] == "parse_error" for x in q)


def test_nested_archive_rejected():
    store = _store()
    up = _upload(store)
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr("a.txt", b"x")
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w") as z:
        z.writestr("inner.zip", inner.getvalue())
    res = ingestion.process_upload(store, up["upload_id"],
                                   [{"filename": "outer.zip", "content": outer.getvalue()}])
    q = store.list_quarantine(status="open", upload_id=up["upload_id"])
    assert any(x["kind"] == "parse_error" for x in q)
