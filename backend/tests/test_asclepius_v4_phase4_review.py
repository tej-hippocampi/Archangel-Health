"""Asclepius V4 Build Spec — Phase 4: admin review queue.

Phases 2 and 3 raise "unknown" states (burned-in PHI unscreened, asset blob missing,
completeness unresolved) that had nowhere to go: the DICOM ``needs_review`` kwarg set a
flag on a nested dict nothing read, the upload still rendered green, and the case entered
the annotation queue. This phase gives the three unknowns a destination — a blocking
reason holds the case out of the queue until a human clears it; an advisory reason rides
along on an otherwise-intact case. Spec Phase 4 tests 18-25 / Audit §21.
"""
from __future__ import annotations

import base64
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import ingestion as I  # noqa: E402

pydicom = pytest.importorskip("pydicom")
import numpy as np  # noqa: E402
from pydicom.dataset import Dataset, FileDataset  # noqa: E402
from pydicom.uid import ExplicitVRLittleEndian, generate_uid  # noqa: E402

client = TestClient(A.app)
_KEY = base64.urlsafe_b64encode(b"phase4-review-queue-test-key-032").decode()

_CSV = """patient_key,panel,analyte,loinc,value,unit,ref_low,ref_high,flag,collected_at
p1,BMP,Sodium,2951-2,112,mmol/L,135,145,LL,2031-03-14
p1,BMP,Sodium,2951-2,124,mmol/L,135,145,L,2031-03-19
"""


@pytest.fixture(autouse=True)
def _iso(monkeypatch, tmp_path):
    A.fresh_store()
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(tmp_path / "ingest"))
    monkeypatch.setenv("ASCLEPIUS_ASSET_STORE", str(tmp_path / "assets"))
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", _KEY)
    yield


def _store():
    from asclepius.store import get_store
    return get_store()


# ── DICOM + bundle builders ───────────────────────────────────────────────────
def _make_ds(*, bia="NO", modality="CT"):
    meta = Dataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset("x", {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID = "SYN-MRN-p1"
    ds.Modality = modality
    ds.BurnedInAnnotation = bia
    ds.StudyInstanceUID = generate_uid()
    ds.SeriesInstanceUID = generate_uid()
    ds.SOPInstanceUID = generate_uid()
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
    ds.Rows, ds.Columns = 16, 16
    ds.BitsAllocated, ds.BitsStored, ds.HighBit = 16, 16, 15
    ds.PixelRepresentation, ds.SamplesPerPixel = 0, 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.RescaleSlope, ds.RescaleIntercept = 1, -1024
    ds.WindowCenter, ds.WindowWidth = 40, 400
    ds.PixelData = (np.arange(256).reshape(16, 16) % 2048).astype("uint16").tobytes()
    return ds


def _dcm_bytes(ds) -> bytes:
    buf = io.BytesIO()
    ds.save_as(buf, little_endian=True, implicit_vr=False)
    return buf.getvalue()


def _zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _manifest(**over):
    m = {"patient_key": "p1", "specialty": "nephrology", "index_event": "2031-03-19"}
    m.update(over)
    return json.dumps(m)


def _ingest(store, zip_bytes: bytes):
    uid = store.new_upload_id()
    rp = I.store_raw(uid, zip_bytes)
    store.insert_ingest_upload(upload_id=uid, link_id="L", partner_id="P",
                               filename="bundle.zip", sha256=I.sha256_hex(zip_bytes),
                               size_bytes=len(zip_bytes), raw_path=rp, source_ip=None)
    summary = I.process_upload(store, uid)
    summary["upload_id"] = uid
    return summary


def _suspect_bundle():
    """A clinically gradable bundle (labs → parseable content) plus a DICOM whose
    burned-in screening cannot run → the study screens ``suspect``."""
    return _zip({"manifest.json": _manifest(), "labs.csv": _CSV,
                 "scan.dcm": _dcm_bytes(_make_ds(bia="NO"))})


def _force_suspect(monkeypatch):
    """OCR could-not-run → burned_in_risk == 'suspect', regardless of tesseract."""
    from asclepius import dicom_deid as _dd
    def _raise(_ds):
        raise _dd.OcrUnavailable("tesseract not installed")
    monkeypatch.setattr(_dd, "_ocr_border_text", _raise)


# ── test 18 ───────────────────────────────────────────────────────────────────
def test_suspect_dicom_sets_needs_review(monkeypatch):
    """A suspect DICOM → the derived case is `needs_review`, carrying a blocking
    `burned_in_phi_unverified` reason (not a flag on a dict nothing reads)."""
    _force_suspect(monkeypatch)
    store = _store()
    summary = _ingest(store, _suspect_bundle())
    cases = store.list_ingest_cases(upload_id=summary["upload_id"])
    held = [c for c in cases if c.get("status") == "needs_review"]
    assert held, [c.get("status") for c in cases]
    reasons = held[0].get("review") or []
    codes = {r["reason"]: r["severity"] for r in reasons}
    assert codes.get("burned_in_phi_unverified") == "blocking", reasons


# ── test 19 ───────────────────────────────────────────────────────────────────
def test_upload_status_reflects_needs_review(monkeypatch):
    """One held case → the UPLOAD row reads `needs_review`, never a green `ingested`.
    Guards the green-row failure this whole spec is about."""
    _force_suspect(monkeypatch)
    store = _store()
    summary = _ingest(store, _suspect_bundle())
    assert summary["status"] == "needs_review", summary
    assert store.get_ingest_upload(summary["upload_id"])["status"] == "needs_review"


# ── test 20 ───────────────────────────────────────────────────────────────────
def test_blocking_review_holds_annotation_queue():
    """`next_task_for_evaluator` never returns a task whose ingest case still carries
    an unresolved blocking reason (`ingest_cases.status == 'needs_review'`)."""
    store = _store()
    case = {"case_source": "real_deid", "specialty": "nephrology",
            "notes": [{"text": "renal consult", "authored_at": "2031-03-19"}]}
    task = store.insert_task(specialty="nephrology", prompt="dx?", case=case,
                             difficulty="hard", source="real_deid")
    ic = store.insert_ingest_case(upload_id="u1", patient_key="pk1", specialty="nephrology",
                                  case=case, status="needs_review", report={},
                                  review_status="needs_review",
                                  review_json=[{"reason": "burned_in_phi_unverified",
                                                "severity": "blocking", "detail": "x",
                                                "raised_at": "2031-03-19T00:00:00Z"}])
    store.update_ingest_case(ic["ingest_case_id"], task_id=task["task_id"])
    ev = A.make_user(store, role="evaluator")
    got = store.next_task_for_evaluator(evaluator_id=ev["id_hashed"], specialty="nephrology",
                                        real_only=True, multimodal_only=True)
    assert got is None, "a blocked case must never be served for annotation"
    # …and releasing it (status → ingested) makes the task eligible again.
    store.update_ingest_case(ic["ingest_case_id"], status="ingested")
    got2 = store.next_task_for_evaluator(evaluator_id=ev["id_hashed"], specialty="nephrology",
                                         real_only=True, multimodal_only=True)
    assert got2 and got2["task_id"] == task["task_id"]


# ── test 21 ───────────────────────────────────────────────────────────────────
def test_advisory_review_does_not_hold_queue():
    """An advisory-only case (`completeness_unverified`) ingests cleanly and IS
    offered — holding it would rebuild the Phase 1 bug with a nicer UI."""
    store = _store()
    case = {"case_source": "real_deid", "specialty": "nephrology",
            "notes": [{"text": "renal consult", "authored_at": "2031-03-19"}]}
    task = store.insert_task(specialty="nephrology", prompt="dx?", case=case,
                             difficulty="hard", source="real_deid")
    ic = store.insert_ingest_case(upload_id="u1", patient_key="pk1", specialty="nephrology",
                                  case=case, status="ingested", report={},
                                  review_status="needs_review",
                                  review_json=[{"reason": "completeness_unverified",
                                                "severity": "advisory", "detail": "x",
                                                "raised_at": "2031-03-19T00:00:00Z"}])
    store.update_ingest_case(ic["ingest_case_id"], task_id=task["task_id"])
    ev = A.make_user(store, role="evaluator")
    got = store.next_task_for_evaluator(evaluator_id=ev["id_hashed"], specialty="nephrology",
                                        real_only=True, multimodal_only=True)
    assert got and got["task_id"] == task["task_id"]


# ── test 22 ───────────────────────────────────────────────────────────────────
def test_clear_review_releases_case():
    """Clearing all blocking reasons → case `ingested`, queue-eligible, and the
    clearing admin is stamped (`reviewed_by_hashed` + `reviewed_at`)."""
    store = _store()
    case = {"case_source": "real_deid", "specialty": "nephrology",
            "notes": [{"text": "renal consult", "authored_at": "2031-03-19"}]}
    task = store.insert_task(specialty="nephrology", prompt="dx?", case=case,
                             difficulty="hard", source="real_deid")
    ic = store.insert_ingest_case(upload_id="u1", patient_key="pk1", specialty="nephrology",
                                  case=case, status="needs_review", report={},
                                  review_status="needs_review",
                                  review_json=[{"reason": "burned_in_phi_unverified",
                                                "severity": "blocking", "detail": "x",
                                                "raised_at": "2031-03-19T00:00:00Z"}])
    store.update_ingest_case(ic["ingest_case_id"], task_id=task["task_id"])
    admin = A.headers_for(A.make_user(store, role="admin"))
    r = client.post(f"/api/asclepius/ingestion/cases/{ic['ingest_case_id']}/review/clear",
                    json={"note": "Reviewed both border bands at full size — no PHI."},
                    headers=admin)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ingested" and body["review_status"] == "cleared"
    assert body["reviewed_by_hashed"] and body["reviewed_at"]
    refreshed = store.get_ingest_case(ic["ingest_case_id"])
    assert refreshed["status"] == "ingested"
    assert refreshed["reviewed_by_hashed"] == body["reviewed_by_hashed"]
    ev = A.make_user(store, role="evaluator")
    got = store.next_task_for_evaluator(evaluator_id=ev["id_hashed"], specialty="nephrology",
                                        real_only=True, multimodal_only=True)
    assert got and got["task_id"] == task["task_id"]


# ── test 23 ───────────────────────────────────────────────────────────────────
def test_clear_requires_note_and_actor():
    """Clearing a blocking flag is an attributable human statement: a missing note
    is 400, and an unauthenticated caller is rejected outright."""
    store = _store()
    ic = store.insert_ingest_case(upload_id="u1", patient_key="pk1", specialty="nephrology",
                                  case={"case_source": "real_deid", "specialty": "nephrology"},
                                  status="needs_review", report={}, review_status="needs_review",
                                  review_json=[{"reason": "burned_in_phi_unverified",
                                                "severity": "blocking", "detail": "x",
                                                "raised_at": "2031-03-19T00:00:00Z"}])
    admin = A.headers_for(A.make_user(store, role="admin"))
    # blank note → 400
    r = client.post(f"/api/asclepius/ingestion/cases/{ic['ingest_case_id']}/review/clear",
                    json={"note": "   "}, headers=admin)
    assert r.status_code == 400, r.text
    # unauthenticated → 401/403, and the case is untouched
    r2 = client.post(f"/api/asclepius/ingestion/cases/{ic['ingest_case_id']}/review/clear",
                     json={"note": "no PHI"})
    assert r2.status_code in (401, 403), r2.text
    assert store.get_ingest_case(ic["ingest_case_id"])["status"] == "needs_review"


# ── test 24 ───────────────────────────────────────────────────────────────────
def test_review_status_nullable_for_unassessed():
    """A clean case (no reason raised) has `review_status IS NULL` — NULL means
    'nothing raised', which is NOT 'reviewed and cleared'. No DEFAULT backfill."""
    store = _store()
    summary = _ingest(store, _zip({"manifest.json": _manifest(), "labs.csv": _CSV}))
    assert summary["status"] == "ingested", summary
    cases = store.list_ingest_cases(upload_id=summary["upload_id"], status="ingested")
    assert cases
    with store._conn() as conn:  # noqa: SLF001
        row = conn.execute(
            "SELECT review_status FROM ingest_cases WHERE ingest_case_id = ?",
            (cases[0]["ingest_case_id"],)).fetchone()
    assert row["review_status"] is None


# ── test 25 ───────────────────────────────────────────────────────────────────
def test_uploads_filter_by_status(monkeypatch):
    """`?status=needs_review` returns only held uploads, and the chip `counts`
    reflect the true per-status totals across all uploads."""
    _force_suspect(monkeypatch)
    store = _store()
    held = _ingest(store, _suspect_bundle())
    monkeypatch.undo()  # a second, clean upload → ingested
    from asclepius import dicom_deid as _dd
    monkeypatch.setattr(_dd, "_ocr_border_text", lambda ds: False)
    clean = _ingest(store, _zip({"manifest.json": _manifest(), "labs.csv": _CSV}))
    assert held["status"] == "needs_review" and clean["status"] == "ingested"

    admin = A.headers_for(A.make_user(store, role="admin"))
    r = client.get("/api/asclepius/ingestion/uploads?status=needs_review", headers=admin)
    assert r.status_code == 200, r.text
    body = r.json()
    ids = {u["upload_id"] for u in body["uploads"]}
    assert ids == {held["upload_id"]}
    assert body["counts"]["needs_review"] == 1
    assert body["counts"]["ingested"] == 1
    assert body["counts"]["all"] >= 2


# ── review payload split ─────────────────────────────────────────────────────
def test_upload_review_endpoint_orders_blocking_first():
    """GET /uploads/{id}/review returns per-case reasons with blocking ahead of
    advisory, so the drawer renders the PHI hold above the advisory note."""
    store = _store()
    ic = store.insert_ingest_case(
        upload_id="u1", patient_key="pk1", specialty="nephrology",
        case={"case_source": "real_deid", "specialty": "nephrology", "studies": []},
        status="needs_review", report={}, review_status="needs_review",
        review_json=[
            {"reason": "completeness_unverified", "severity": "advisory",
             "detail": "a", "raised_at": "2031-03-19T00:00:00Z"},
            {"reason": "burned_in_phi_unverified", "severity": "blocking",
             "detail": "b", "raised_at": "2031-03-19T00:00:00Z"},
        ])
    store.insert_ingest_upload(upload_id="u1", link_id="L", partner_id="P",
                               filename="b.zip", sha256="x", size_bytes=1,
                               raw_path=None, source_ip=None)
    admin = A.headers_for(A.make_user(store, role="admin"))
    r = client.get("/api/asclepius/ingestion/uploads/u1/review", headers=admin)
    assert r.status_code == 200, r.text
    cases = r.json()["cases"]
    assert len(cases) == 1
    assert cases[0]["reasons"][0]["severity"] == "blocking"
    assert cases[0]["blocking_count"] == 1
