"""DICOM ingestion + de-identification (Buyer Response PRD §4).

A DICOM file is NOT "an image with a header". Four distinct PHI channels the
free-text ``residual_identifiers`` scanner sees none of:

  1. Header tags (PatientName/ID/BirthDate, StudyDate, AccessionNumber,
     InstitutionName, ReferringPhysicianName, StudyDescription, and unbounded
     private vendor tags).
  2. Burned-in pixel PHI (name/MRN rendered INTO the pixels — no header scrub touches
     it; ``BurnedInAnnotation`` is unreliable).
  3. UIDs (Study/Series/SOPInstanceUID are re-identification keys back to the partner
     PACS; they must be REMAPPED consistently, not removed, or the series collapses).
  4. Structured-report narrative content (PHI risk AND, per §3, potentially the answer).

This implements the spirit of PS3.15 Annex E Basic Application Level Confidentiality
Profile with an explicit retain/remove/remap policy table and an ALLOWLIST default of
REMOVE — private vendor tags are unbounded, so a blocklist can only ever be behind.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("asclepius.dicom_deid")

# A stable, private UID root for our deterministic surrogates. Same original UID →
# same surrogate, so a study's series hierarchy survives de-identification.
_UID_ROOT = "2.25"  # the UUID-derived DICOM root arc


class DicomDeidError(ValueError):
    """A DICOM file cannot be made safe → the entry quarantines rather than degrading."""


# ─── Policy per PS3.15 Annex E ────────────────────────────────────────────────
# Three dispositions; the DEFAULT for any tag not named here is REMOVE.
#   REMOVE  - deleted outright
#   REMAP   - replaced with a deterministic surrogate (UIDs; dates via the case shift)
#   RETAIN  - clinically load-bearing and non-identifying
RETAIN = frozenset({
    "Modality", "SOPClassUID", "PhotometricInterpretation", "SamplesPerPixel",
    "Rows", "Columns", "BitsAllocated", "BitsStored", "HighBit", "PixelRepresentation",
    "PlanarConfiguration", "PixelData", "WindowCenter", "WindowWidth",
    "RescaleSlope", "RescaleIntercept", "RescaleType", "PixelSpacing",
    "SliceThickness", "SpacingBetweenSlices", "ImageOrientationPatient",
    "ImagePositionPatient", "KVP", "ContrastBolusAgent", "BodyPartExamined",
    "ViewPosition", "ImageType", "TransferSyntaxUID", "NumberOfFrames",
    "BurnedInAnnotation",  # retained as a SIGNAL (never trusted as absence of PHI)
    "VOILUTFunction",
})
# UIDs remapped to keep the study/series hierarchy intact.
REMAP_UIDS = frozenset({
    "StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID",
    "FrameOfReferenceUID", "SynchronizationFrameOfReferenceUID",
})
# Dates/times remapped (blanked here in the standalone path; the bundle path applies
# the same day-shift the rest of the case uses so intervals survive).
REMAP_DATES = frozenset({
    "StudyDate", "SeriesDate", "AcquisitionDate", "ContentDate", "StudyTime",
    "SeriesTime", "AcquisitionTime", "ContentTime",
})

# Modalities that MOST OFTEN carry burned-in pixel PHI.
_HIGH_RISK_MODALITIES = frozenset({"US", "SC", "OT", "XC"})

# DICOM Modality → Study modality (asclepius.cases.STUDY_MODALITIES).
_MODALITY_MAP = {
    "CT": "ct", "MR": "mri", "PT": "pet", "PET": "pet", "NM": "pet",
    "XA": "cath", "US": "other", "CR": "other", "DX": "other", "MG": "other",
    "SM": "pathology", "GM": "pathology", "ECG": "ecg", "US_ECHO": "echo",
}


def read(data: bytes):
    """Parse DICOM bytes → a pydicom Dataset. Raises DicomDeidError on a non-DICOM or
    unreadable file."""
    try:
        import io
        import pydicom
        return pydicom.dcmread(io.BytesIO(data), force=True)
    except Exception as exc:  # pragma: no cover - depends on pydicom
        raise DicomDeidError(f"not a readable DICOM file: {exc}") from exc


def remap_uid(original: str) -> str:
    """Deterministic surrogate UID: same original → same surrogate (so a study's
    series hierarchy survives), never reversible to the partner PACS."""
    digest = hashlib.sha256(("asclepius-dicom-uid:" + str(original)).encode()).hexdigest()
    # Build a numeric-only suffix under our root, capped to the 64-char UID limit.
    num = str(int(digest[:24], 16))
    uid = f"{_UID_ROOT}.{num}"
    return uid[:64]


def deidentify_dicom(ds, *, date_shift_days: Optional[int] = None) -> Tuple[Any, Dict[str, Any]]:
    """(cleaned_dataset, report). Apply the allowlist policy: RETAIN load-bearing
    non-identifying tags, REMAP UIDs (and dates by the case shift when given), and
    REMOVE everything else — including every private/unknown tag. Raises
    DicomDeidError when the file cannot be made safe."""
    try:
        removed: List[str] = []
        remapped: List[str] = []
        retained: List[str] = []

        # Remove ALL private tags first (unbounded, vendor-specific).
        try:
            ds.remove_private_tags()
        except Exception:  # pragma: no cover
            pass

        for elem in list(ds):
            kw = elem.keyword or ""
            if not kw:
                # An unknown/unnamed (e.g. private, repeating-group) element → REMOVE.
                del ds[elem.tag]
                removed.append(str(elem.tag))
                continue
            if kw == "PixelData" or kw in RETAIN:
                retained.append(kw)
                continue
            if kw in REMAP_UIDS:
                try:
                    ds[elem.tag].value = remap_uid(str(elem.value))
                    remapped.append(kw)
                except Exception:
                    del ds[elem.tag]
                    removed.append(kw)
                continue
            if kw in REMAP_DATES:
                # Standalone path blanks dates; the bundle path shifts by the case's
                # day offset so intervals survive (never a real calendar date).
                ds[elem.tag].value = ""
                remapped.append(kw)
                continue
            # DEFAULT: REMOVE. Allowlist, never a blocklist.
            del ds[elem.tag]
            removed.append(kw)

        # File-meta identifiers: remap the SOP instance UID, and REMOVE the AE-title
        # elements — SourceApplicationEntityTitle / Sending / ReceivingApplication
        # AE titles routinely encode the sending institution or station name (PHI-
        # adjacent) and survive the main-dataset scrub because they live in file_meta.
        meta = getattr(ds, "file_meta", None)
        if meta is not None:
            if "MediaStorageSOPInstanceUID" in meta:
                try:
                    meta.MediaStorageSOPInstanceUID = remap_uid(str(meta.MediaStorageSOPInstanceUID))
                except Exception:  # pragma: no cover
                    pass
            for ae_kw in ("SourceApplicationEntityTitle", "SendingApplicationEntityTitle",
                          "ReceivingApplicationEntityTitle", "PrivateInformation",
                          "PrivateInformationCreatorUID"):
                if ae_kw in meta:
                    try:
                        del meta[ae_kw]
                        remapped.append("file_meta:" + ae_kw)
                    except Exception:  # pragma: no cover
                        pass

        report = {"removed": sorted(set(removed)), "remapped": sorted(set(remapped)),
                  "retained": sorted(set(retained)), "date_shift_days": date_shift_days}
        return ds, report
    except DicomDeidError:
        raise
    except Exception as exc:
        raise DicomDeidError(f"de-identification failed: {exc}") from exc


class OcrUnavailable(RuntimeError):
    """OCR could not run. Distinct from 'OCR ran and found no text' — collapsing the
    two is what let an unchecked image be reported as clear (Audit PRD §C1)."""


def ocr_available() -> bool:
    """Boot-time probe: is the tesseract BINARY present? The pip package installing
    successfully says nothing about the binary, which is the gap that made C1
    invisible — pytesseract shells out to a system binary pip cannot install."""
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def _ocr_border_text(ds) -> bool:
    """True when text is found in the top/bottom 12% border bands.

    Raises OcrUnavailable when the check could not be performed at all (tesseract
    binary missing, pixel data unreadable, render failure). The caller MUST treat
    that as 'suspect', never 'clear': an unchecked image is not a safe image, and
    this is the only layer that inspects pixels rather than trusting a sender-declared
    tag (Audit PRD §C1)."""
    try:
        import io

        import numpy as np
        from PIL import Image

        png = render_dicom_to_png(ds)
        im = Image.open(io.BytesIO(png)).convert("L")
        w, h = im.size
        band = max(1, int(h * 0.12))
        arr = np.asarray(im)
        top = Image.fromarray(arr[:band, :])
        bot = Image.fromarray(arr[-band:, :])
    except Exception as exc:
        raise OcrUnavailable(f"could not render/inspect pixels: {exc}") from exc

    try:
        import pytesseract
        text = pytesseract.image_to_string(top) + " " + pytesseract.image_to_string(bot)
    except Exception as exc:  # TesseractNotFoundError et al.
        raise OcrUnavailable(f"tesseract unavailable: {exc}") from exc

    return len(text.strip()) >= 3


def burned_in_risk(ds) -> Tuple[str, str]:
    """('clear' | 'suspect' | 'blocked', reason). Layered, because every single
    signal is individually unreliable:

      1. BurnedInAnnotation == 'YES'                          -> blocked
      2. Modality in HIGH_RISK (US, SC, OT, XC) and no 'NO'   -> suspect
      3. OCR over the border regions finds text               -> suspect
      3b. OCR COULD NOT RUN                                   -> suspect (Audit §C1)
      4. Missing BurnedInAnnotation                           -> suspect
      5. BurnedInAnnotation=NO and OCR found nothing          -> clear

    OCR now runs BEFORE the missing-annotation branch so a missing tag WITH detectable
    text reports the specific reason. Critically, 'OCR could not run' is a distinct
    outcome from 'OCR ran and found nothing' — the former routes to human review (an
    unchecked image is not a safe image, the same 'trust the partner's flag' failure
    the FHIR path refuses), the latter is the only path to 'clear'."""
    bia = str(getattr(ds, "BurnedInAnnotation", "") or "").strip().upper()
    modality = str(getattr(ds, "Modality", "") or "").strip().upper()

    if bia == "YES":
        return "blocked", "BurnedInAnnotation=YES (declared burned-in PHI)"
    if modality in _HIGH_RISK_MODALITIES and bia != "NO":
        return "suspect", f"high-risk modality {modality} without an explicit BurnedInAnnotation=NO"

    try:
        found = _ocr_border_text(ds)
    except OcrUnavailable as exc:
        # The ONLY pixel-level check could not run. A sender-declared NO is not
        # sufficient alone — route to human review (Audit PRD §C1).
        return "suspect", f"burned-in PHI screening unavailable ({exc}); manual review required"

    if found:
        return "suspect", "text detected in the image border regions (OCR)"
    if bia != "NO":
        return "suspect", "BurnedInAnnotation is absent; cannot certify no burned-in PHI"
    return "clear", "BurnedInAnnotation=NO and OCR found no border text"


def _first_numeric(value: Any) -> Optional[float]:
    """First numeric value from a DICOM element that may be a single DSfloat, a
    MultiValue/list, or None. Returns None when nothing numeric is present — never
    raises, so a malformed WindowCenter degrades to the modality default instead of
    crashing the render."""
    if value is None:
        return None
    try:
        if isinstance(value, str):
            return float(value)
        if hasattr(value, "__len__") and not isinstance(value, (str, bytes)):
            return float(value[0]) if len(value) else None
        return float(value)
    except (TypeError, ValueError, IndexError):
        return None


def render_dicom_to_png(ds, *, window: Optional[Tuple[float, float]] = None) -> bytes:
    """Apply Modality LUT (RescaleSlope/Intercept) → VOI LUT (window center/width) →
    8-bit PNG. Windowing is clinical, not cosmetic: the same CT slice at a stroke
    window vs a bone window supports different diagnoses. Uses the file's
    WindowCenter/WindowWidth when present unless an explicit ``window`` is given."""
    import io

    import numpy as np
    from PIL import Image

    arr = ds.pixel_array.astype("float64")
    # Modality LUT (rescale to output units).
    slope = float(getattr(ds, "RescaleSlope", 1) or 1)
    intercept = float(getattr(ds, "RescaleIntercept", 0) or 0)
    arr = arr * slope + intercept
    # VOI LUT (windowing).
    wc = wl = None
    if window is not None:
        wc, wl = window[0], window[1]
    else:
        wc = _first_numeric(getattr(ds, "WindowCenter", None))
        wl = _first_numeric(getattr(ds, "WindowWidth", None))
    if wc is not None and wl and wl > 0:
        lo, hi = wc - wl / 2.0, wc + wl / 2.0
        arr = np.clip(arr, lo, hi)
    # Normalize to 8-bit.
    amin, amax = float(arr.min()), float(arr.max())
    if amax > amin:
        arr = (arr - amin) / (amax - amin) * 255.0
    else:
        arr = np.zeros_like(arr)
    img = Image.fromarray(arr.astype("uint8"))
    if getattr(ds, "PhotometricInterpretation", "") == "MONOCHROME1":
        from PIL import ImageOps
        img = ImageOps.invert(img.convert("L"))
    buf = io.BytesIO()
    img.convert("L").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def window_applied(ds, window: Optional[Tuple[float, float]] = None) -> Optional[Dict[str, Any]]:
    """Record WHICH window was applied to a render (Buyer Response PRD §4 C2), so a
    case that turns on a specific window is auditable and a default-windowed render
    cannot silently make a finding invisible."""
    if window is not None:
        return {"center": window[0], "width": window[1], "source": "explicit"}
    c = _first_numeric(getattr(ds, "WindowCenter", None))
    w = _first_numeric(getattr(ds, "WindowWidth", None))
    if c is not None and w is not None:
        return {"center": c, "width": w, "source": "file"}
    return {"center": None, "width": None, "source": "modality_default"}


def study_modality(ds, specialty: str = "") -> str:
    modality = str(getattr(ds, "Modality", "") or "").strip().upper()
    return _MODALITY_MAP.get(modality, "other")


def to_study_fragment(ds, *, render: bool, needs_review: bool, specialty: str,
                      windows: Optional[List[Tuple[float, float]]] = None,
                      risk: Optional[str] = None, reason: Optional[str] = None) -> Dict[str, Any]:
    """Build a Study fragment from a cleaned DICOM (Buyer Response PRD §4 C2).

    * ``render`` (risk clear): render to PNG asset(s) — one per requested window, each
      labelled — and promote the PNG to ``Study.asset`` (the model-facing raster). The
      cleaned DICOM is the archival original (never a gradable MIME).
    * ``needs_review`` (risk suspect): NO resolvable asset is promoted; the study is
      flagged for the burned-in review queue so it can never auto-ingest as gradable.

    Records ``phi_screening`` on the study (Audit PRD §M2): the method (``ocr+tag``
    when the tesseract binary is present, else ``tag_only``), the risk, and the reason
    — so a buyer asking "how do you know there is no burned-in PHI?" has a per-record
    answer, and ``tag_only`` is self-evidently weaker than ``ocr+tag``."""
    from asclepius import assets

    modality = study_modality(ds, specialty)
    label = str(getattr(ds, "BodyPartExamined", "") or "").strip() or modality.upper()
    frag: Dict[str, Any] = {
        "modality": modality, "label": label, "findings": "", "measurements": [],
        "phi_screening": {
            "method": "ocr+tag" if ocr_available() else "tag_only",
            "burned_in_risk": risk, "reason": reason, "reviewed_by_hashed": None,
        },
    }
    render_windows = windows or [None]  # type: ignore[list-item]
    assets_out: List[Dict[str, Any]] = []
    if render and not needs_review:
        for win in render_windows:
            png = render_dicom_to_png(ds, window=win)
            stored = assets.process_upload(png, "image/png", source="partner_deidentified")
            wa = window_applied(ds, win)
            assets_out.append({
                "asset_id": stored["asset_id"], "sha256": stored["sha256"],
                "mime": stored["mime"], "width": stored.get("width"),
                "height": stored.get("height"), "byte_size": stored.get("byte_size") or 0,
                "window": wa,
            })
        if assets_out:
            # Primary asset promoted to Study.asset; the rest are recorded on the
            # fragment for multi-window studies.
            primary = dict(assets_out[0])
            window_meta = primary.pop("window", None)
            frag["asset"] = {k: v for k, v in primary.items()}
            frag["_render_window"] = window_meta
            if len(assets_out) > 1:
                frag["_extra_window_assets"] = assets_out[1:]
    if needs_review:
        frag["_needs_burnin_review"] = True
    return frag
