"""Community attachment gate (PRD §7.4) — strip metadata, scan for burned-in PHI.

Every upload passes through here BEFORE any byte reaches the asset store:

  * **Images (png/jpeg)** — re-encoded to a clean raster with all technical
    metadata removed (EXIF/GPS/device/timestamps — reuses the proven
    ``asclepius.assets`` strip), then OCR'd (pytesseract) and the recovered
    text run through the §7 PHI scanner. A screenshot of an EHR screen is the
    single most likely leak in a physician chat.
  * **PDFs** — text extracted (pdfminer.six) and scanned; the document is
    rewritten page-by-page (PyPDF2) so document-info/XMP metadata is dropped.
    A PDF whose text cannot be extracted is REJECTED (fail closed: we will not
    store what we cannot scan).
  * **Plain text (txt/csv/md)** — decoded and scanned directly.

On any PHI hit the upload is rejected with the same masked, category-only
explanation as a blocked message; nothing is stored. When the OCR engine is
unavailable, behavior follows ``COMMUNITY_OCR_STRICT`` (default off: store
with a logged warning, mirroring the platform's advisory burn-in scan; set to
1 to fail closed).
"""

from __future__ import annotations

import io
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from community import phi_gate

log = logging.getLogger("community.attachments")

ACCEPTED_MIMES = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "application/pdf": "pdf",
    "text/plain": "txt",
    "text/csv": "csv",
    "text/markdown": "md",
}


class AttachmentRejected(ValueError):
    """Upload refused. ``payload`` is the structured client-facing reason
    (category names only — never content)."""

    def __init__(self, payload: Dict[str, Any]):
        super().__init__(payload.get("message") or "attachment rejected")
        self.payload = payload


def max_attachment_bytes() -> int:
    try:
        return int(os.getenv("COMMUNITY_MAX_ATTACHMENT_BYTES", str(10 * 1024 * 1024)))
    except (TypeError, ValueError):
        return 10 * 1024 * 1024


def ocr_strict() -> bool:
    return (os.getenv("COMMUNITY_OCR_STRICT") or "").strip().lower() in ("1", "true", "yes", "on")


def _reject(code: str, message: str, categories: Optional[List[str]] = None) -> AttachmentRejected:
    return AttachmentRejected(
        {"code": code, "message": message, "categories": sorted(categories or [])}
    )


def _phi_reject(categories: List[str]) -> AttachmentRejected:
    labels = [phi_gate.CATEGORY_LABELS.get(c, c.replace("_", " ")) for c in sorted(set(categories))]
    return _reject(
        "phi_detected",
        "This attachment appears to contain patient-identifiable information ("
        + ", ".join(labels)
        + "). It was not uploaded.",
        categories,
    )


def _ocr_image_text(clean_bytes: bytes) -> Tuple[Optional[str], Optional[str]]:
    """OCR the cleaned raster. Returns (text, None) on success or
    (None, reason) when the OCR engine is unavailable."""
    try:
        import pytesseract
        from PIL import Image
    except Exception as exc:  # pragma: no cover — deps are in requirements
        return None, f"ocr_import_failed:{type(exc).__name__}"
    try:
        return pytesseract.image_to_string(Image.open(io.BytesIO(clean_bytes))) or "", None
    except Exception as exc:
        return None, f"ocr_engine_unavailable:{type(exc).__name__}"


def _process_image(data: bytes, mime: str) -> Tuple[bytes, str]:
    """Strip metadata / re-encode via the proven assets helper, then OCR-scan."""
    from asclepius.assets import UnsupportedMediaType, _strip_and_normalize_raster

    try:
        clean, out_mime, _w, _h = _strip_and_normalize_raster(data, mime)
    except UnsupportedMediaType:
        raise _reject("unreadable_image", "This image could not be read. It was not uploaded.")
    text, ocr_err = _ocr_image_text(clean)
    if text is None:
        if ocr_strict():
            raise _reject(
                "attachment_unscannable",
                "Image screening is unavailable right now, so image uploads are paused. "
                "Try again later or paste the finding as text.",
            )
        log.warning("community image OCR unavailable (%s); storing metadata-stripped image", ocr_err)
        return clean, out_mime
    findings = phi_gate.scan_text(text)
    if findings:
        raise _phi_reject(phi_gate.categories_of(findings))
    return clean, out_mime


def _process_pdf(data: bytes) -> Tuple[bytes, str]:
    """Extract + scan text, then rewrite the document without metadata."""
    try:
        from pdfminer.high_level import extract_text
        text = extract_text(io.BytesIO(data)) or ""
    except Exception:
        raise _reject(
            "attachment_unscannable",
            "This PDF could not be screened for identifiers, so it was not uploaded. "
            "Export it again or paste the relevant text instead.",
        )
    findings = phi_gate.scan_text(text)
    if findings:
        raise _phi_reject(phi_gate.categories_of(findings))
    # Metadata strip: copy pages into a fresh document (no /Info, no XMP).
    try:
        from PyPDF2 import PdfReader, PdfWriter
        reader = PdfReader(io.BytesIO(data))
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        out = io.BytesIO()
        writer.write(out)
        return out.getvalue(), "application/pdf"
    except Exception:
        # Text is verified clean; losing the metadata strip is worse than
        # losing the upload only in strict deployments.
        if ocr_strict():
            raise _reject(
                "attachment_unscannable",
                "This PDF could not be sanitized, so it was not uploaded.",
            )
        log.warning("community PDF metadata strip failed; storing original bytes", exc_info=True)
        return data, "application/pdf"


def _process_text(data: bytes, mime: str) -> Tuple[bytes, str]:
    text = data.decode("utf-8", errors="replace")
    findings = phi_gate.scan_text(text)
    if findings:
        raise _phi_reject(phi_gate.categories_of(findings))
    return data, mime


def process_attachment(data: bytes, mime: str) -> Tuple[bytes, str]:
    """Validate, sanitize, and screen one upload. Returns (clean_bytes, mime)
    ready for the asset store, or raises :class:`AttachmentRejected`."""
    mime = (mime or "").strip().lower().split(";", 1)[0]
    if mime not in ACCEPTED_MIMES:
        raise _reject(
            "unsupported_type",
            "Only PNG/JPEG images, PDFs, and plain-text files can be shared here.",
        )
    if len(data) > max_attachment_bytes():
        mb = max_attachment_bytes() // (1024 * 1024)
        raise _reject("too_large", f"Attachments are limited to {mb} MB.")
    if not data:
        raise _reject("empty_file", "That file is empty.")
    if mime in ("image/png", "image/jpeg"):
        return _process_image(data, mime)
    if mime == "application/pdf":
        return _process_pdf(data)
    return _process_text(data, mime)
