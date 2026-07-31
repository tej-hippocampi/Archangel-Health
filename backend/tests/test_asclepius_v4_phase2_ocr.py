"""Asclepius V4 Build Spec — Phase 2: DICOM OCR fail-open (CRITICAL, PHI).

_ocr_border_text used to return False on any exception, so "OCR crashed" was
indistinguishable from "no text found", and a BurnedInAnnotation=NO study auto-
ingested as clear with zero pixel inspection. tesseract is a system binary the
Dockerfile did not install, so this was live in the deployed image. Spec Phase 2
tests 9-13 / Audit §C1.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest.importorskip("pydicom")
from pydicom.dataset import Dataset, FileDataset  # noqa: E402
from pydicom.uid import ExplicitVRLittleEndian, generate_uid  # noqa: E402

from asclepius import dicom_deid as D  # noqa: E402


def _ds(*, bia="NO", modality="CT"):
    meta = Dataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset("x", {}, file_meta=meta, preamble=b"\0" * 128)
    ds.Modality = modality
    ds.BurnedInAnnotation = bia
    ds.Rows, ds.Columns = 16, 16
    ds.BitsAllocated, ds.BitsStored, ds.HighBit = 16, 16, 15
    ds.PixelRepresentation, ds.SamplesPerPixel = 0, 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.RescaleSlope, ds.RescaleIntercept = 1, -1024
    ds.WindowCenter, ds.WindowWidth = 40, 400
    ds.PixelData = (np.arange(256).reshape(16, 16) % 2048).astype("uint16").tobytes()
    return ds


# ── test 9 — THE C1 REGRESSION TEST ───────────────────────────────────────────
def test_ocr_unavailable_is_suspect_not_clear(monkeypatch):
    def _raise(*a, **k):
        raise RuntimeError("tesseract is not installed")
    import pytesseract
    monkeypatch.setattr(pytesseract, "image_to_string", _raise)
    risk, why = D.burned_in_risk(_ds(bia="NO"))
    assert risk == "suspect", (risk, why)
    assert risk != "clear"


# ── test 10 ───────────────────────────────────────────────────────────────────
def test_ocr_unavailable_reason_is_honest(monkeypatch):
    import pytesseract
    monkeypatch.setattr(pytesseract, "image_to_string",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no binary")))
    risk, why = D.burned_in_risk(_ds(bia="NO"))
    assert "unavailable" in why.lower()
    assert "no border text" not in why.lower()


# ── test 11 — the fix must not make everything suspect ────────────────────────
def test_ocr_available_no_text_is_clear(monkeypatch):
    import pytesseract
    monkeypatch.setattr(pytesseract, "image_to_string", lambda *a, **k: "   ")  # no text
    risk, why = D.burned_in_risk(_ds(bia="NO"))
    assert risk == "clear", (risk, why)


def test_ocr_available_text_is_suspect(monkeypatch):
    import pytesseract
    monkeypatch.setattr(pytesseract, "image_to_string", lambda *a, **k: "PATIENT SMITH MRN 12345")
    risk, why = D.burned_in_risk(_ds(bia="NO"))
    assert risk == "suspect" and "ocr" in why.lower()


# ── test 12 ───────────────────────────────────────────────────────────────────
def test_ocr_probe_reports_binary_absence(monkeypatch):
    import pytesseract
    monkeypatch.setattr(pytesseract, "get_tesseract_version",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("not found")))
    assert D.ocr_available() is False
    # startup probe LOGS, does not raise
    import logging
    from unittest.mock import patch
    with patch.object(logging.getLogger("patient_auth"), "error") as err:
        try:
            from asclepius.dicom_deid import ocr_available
            if not ocr_available():
                logging.getLogger("patient_auth").error("tesseract not installed")
        except Exception:
            pytest.fail("probe must not raise")
        assert err.called


# ── test 13 ───────────────────────────────────────────────────────────────────
def test_dockerfile_installs_tesseract():
    dockerfile = (Path(__file__).resolve().parents[2] / "Dockerfile").read_text()
    assert "apt-get install" in dockerfile and "tesseract-ocr" in dockerfile


# ── M2 — screening method travels with the study ─────────────────────────────
def test_phi_screening_recorded_on_study(monkeypatch):
    import pytesseract
    monkeypatch.setattr(pytesseract, "image_to_string", lambda *a, **k: "")
    ds = _ds(bia="NO")
    clean, _ = D.deidentify_dicom(ds)
    risk, why = D.burned_in_risk(clean)
    frag = D.to_study_fragment(clean, render=(risk == "clear"),
                               needs_review=(risk == "suspect"), specialty="radiology",
                               risk=risk, reason=why)
    ps = frag["phi_screening"]
    assert ps["burned_in_risk"] == risk and ps["reason"] == why
    assert ps["method"] in ("ocr+tag", "tag_only")
