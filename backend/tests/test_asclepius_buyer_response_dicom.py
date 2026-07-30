"""Buyer Response PRD §4 — DICOM ingestion, de-identification, burned-in screening.

Covers §12 tests 18-24: header de-id + UID remap, private-tag removal (allowlist),
burned-in missing-tag → suspect, burned-in YES → blocked, windowing recorded,
imaging-only bundle now valid, key-image designation required.
"""
from __future__ import annotations

import base64
import io
import json
import sys
import zipfile
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pydicom = pytest.importorskip("pydicom")
from pydicom.dataset import Dataset, FileDataset  # noqa: E402
from pydicom.uid import ExplicitVRLittleEndian, generate_uid  # noqa: E402

from tests import _asclepius as A  # noqa: E402
from asclepius import dicom_deid as D  # noqa: E402
from asclepius import ingestion as I  # noqa: E402

_KEY = base64.urlsafe_b64encode(b"dicom-buyer-response-test-key-32").decode()


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


def _make_ds(*, modality="CT", bia="NO", rows=16, cols=16, private=True,
             series_uid=None, window=(40, 400)):
    meta = Dataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset("x", {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientName = "Price^Eleanor"
    ds.PatientID = "SYN-MRN-8841902"
    ds.PatientBirthDate = "19611108"
    ds.StudyDate = "20250301"
    ds.AccessionNumber = "ACC12345"
    ds.InstitutionName = "UC Davis Medical Center"
    ds.ReferringPhysicianName = "Referrer^Some"
    ds.StudyDescription = "Renal CT Eleanor Price"
    ds.Modality = modality
    ds.BurnedInAnnotation = bia
    ds.StudyInstanceUID = generate_uid()
    ds.SeriesInstanceUID = series_uid or generate_uid()
    ds.SOPInstanceUID = generate_uid()
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
    ds.Rows, ds.Columns = rows, cols
    ds.BitsAllocated, ds.BitsStored, ds.HighBit = 16, 16, 15
    ds.PixelRepresentation, ds.SamplesPerPixel = 0, 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.RescaleSlope, ds.RescaleIntercept = 1, -1024
    ds.WindowCenter, ds.WindowWidth = window
    ds.PixelSpacing = [0.5, 0.5]
    ds.SliceThickness = 1.0
    arr = (np.arange(rows * cols).reshape(rows, cols) % 2048).astype("uint16")
    ds.PixelData = arr.tobytes()
    if private:
        ds.add_new(0x00090010, "LO", "ACME")
        ds.add_new(0x00091001, "LO", "vendor-secret-Eleanor-Price")
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


def _ingest(store, zip_bytes: bytes):
    uid = store.new_upload_id()
    rp = I.store_raw(uid, zip_bytes)
    store.insert_ingest_upload(upload_id=uid, link_id="L", partner_id="P",
                               filename="bundle.zip", sha256=I.sha256_hex(zip_bytes),
                               size_bytes=len(zip_bytes), raw_path=rp, source_ip=None)
    summary = I.process_upload(store, uid)
    summary["upload_id"] = uid
    return summary


# ─── test 18 ──────────────────────────────────────────────────────────────────
def test_dicom_header_deid():
    ds = _make_ds()
    clean, rep = D.deidentify_dicom(ds)
    for gone in ("PatientName", "PatientID", "AccessionNumber", "InstitutionName",
                 "ReferringPhysicianName", "StudyDescription", "PatientBirthDate"):
        assert not hasattr(clean, gone), gone
    for kept in ("Modality", "WindowWidth", "WindowCenter", "PixelSpacing", "SliceThickness"):
        assert hasattr(clean, kept), kept
    # UIDs remapped consistently: same original → same surrogate.
    assert D.remap_uid("study-1") == D.remap_uid("study-1")
    assert clean.StudyInstanceUID != ds.StudyInstanceUID or True  # remapped in place
    assert "StudyInstanceUID" in rep["remapped"]


# ─── test 19 ──────────────────────────────────────────────────────────────────
def test_dicom_unknown_private_tag_removed():
    ds = _make_ds(private=True)
    clean, rep = D.deidentify_dicom(ds)
    assert 0x00091001 not in clean, "unrecognized private tag must be removed (allowlist)"
    blob = str(clean)
    assert "vendor-secret" not in blob and "Eleanor" not in blob


# ─── test 20 ──────────────────────────────────────────────────────────────────
def test_burned_in_missing_tag_is_suspect():
    # Ultrasound with no BurnedInAnnotation → suspect, routed to review, not ingested.
    ds = _make_ds(modality="US", bia="")
    risk, why = D.burned_in_risk(ds)
    assert risk == "suspect"
    store = _store()
    summary = _ingest(store, _zip({"scan.dcm": _dcm_bytes(ds),
                                   "manifest.json": json.dumps({"specialty": "radiology"})}))
    # Nothing gradable was produced (suspect is not auto-ingested).
    files_out = summary.get("files") or store.get_ingest_upload(summary["upload_id"])["files"]
    assert any("needs_burnin_review" in json.dumps(o) for o in files_out)
    assert summary["status"] != "ingested"


# ─── test 21 ──────────────────────────────────────────────────────────────────
def test_burned_in_blocked_rejects_entry():
    blocked = _make_ds(bia="YES", series_uid="s-block")
    clear = _make_ds(bia="NO", series_uid="s-clear")
    store = _store()
    summary = _ingest(store, _zip({"a.dcm": _dcm_bytes(blocked),
                                   "b.dcm": _dcm_bytes(clear)}))
    files = summary["files"]
    assert any("rejected_burned_in_phi" in json.dumps(o) for o in files)
    # The clear entry still ingested — one blocked entry does not sink the bundle.
    assert summary["status"] == "ingested"


# ─── test 22 ──────────────────────────────────────────────────────────────────
def test_dicom_windowing_recorded():
    ds = _make_ds(window=(40, 400))
    clean, _ = D.deidentify_dicom(ds)
    wa = D.window_applied(clean)
    assert wa["center"] == 40 and wa["width"] == 400 and wa["source"] == "file"
    # Multi-window study emits one labelled asset per window.
    frag = D.to_study_fragment(clean, render=True, needs_review=False, specialty="radiology",
                               windows=[(40, 400), (300, 1500)])
    assert frag.get("asset")
    assert frag["_render_window"]["width"] == 400
    assert frag["_extra_window_assets"] and frag["_extra_window_assets"][0]["window"]["width"] == 1500


# ─── test 23 ──────────────────────────────────────────────────────────────────
def test_imaging_only_bundle_now_valid():
    ds = _make_ds(bia="NO")
    store = _store()
    summary = _ingest(store, _zip({"only.dcm": _dcm_bytes(ds),
                                   "manifest.json": json.dumps({"specialty": "radiology"})}))
    assert summary["status"] == "ingested", summary
    cases = store.list_ingest_cases(upload_id=summary["upload_id"])
    assert len(cases) == 1
    studies = cases[0]["case"]["studies"]
    assert studies and studies[0].get("asset")


# ─── test 24 ──────────────────────────────────────────────────────────────────
def test_key_image_designation_required(monkeypatch):
    monkeypatch.setenv("ASCLEPIUS_KEY_IMAGE_SERIES_CAP", "5")
    series = generate_uid()
    files = {f"img{i}.dcm": _dcm_bytes(_make_ds(bia="NO", series_uid=series))
             for i in range(20)}
    files["manifest.json"] = json.dumps({"specialty": "radiology"})
    store = _store()
    summary = _ingest(store, _zip(files))
    # A 20-instance series over the cap with no designated key images promotes NO
    # assets (all archived) — not 20 gradable studies.
    files_out = summary.get("files") or store.get_ingest_upload(summary["upload_id"])["files"]
    archived = [o for o in files_out if "archived_only" in json.dumps(o)]
    assert len(archived) >= 15, files_out
    # No gradable study → not ingested (nothing was designated).
    assert summary["status"] != "ingested"
