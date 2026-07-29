"""Community attachment gate (PRD §7.4) — strip metadata, scan for burned-in PHI.

Every upload passes through here BEFORE any byte reaches the asset store:

  * **Images (png/jpeg)** — re-encoded to a clean raster with all technical
    metadata removed (EXIF/GPS/device/timestamps — reuses the proven
    ``asclepius.assets`` strip), then OCR'd (pytesseract) and the recovered
    text run through the §7 PHI scanner. A screenshot of an EHR screen is the
    single most likely leak in a physician chat.
  * **PDFs** — text extracted (pdfminer.six) and scanned; a textless PDF (a
    scan/fax) is rasterized and OCR'd page-by-page instead, or refused when
    the OCR toolchain is unavailable (fail closed: we will not store what we
    cannot scan). The document is rewritten (PyPDF2) so document-info/XMP
    metadata is dropped.
  * **Plain text (txt/csv/md)** — decoded and scanned directly.

On any PHI hit the upload is rejected with the same masked, category-only
explanation as a blocked message; nothing is stored.

**Fail-closed by default** (audit finding): this is the highest-risk surface
in the product (PRD §7), so when the OCR engine is unavailable image uploads
are REFUSED (with a message telling the physician to paste the finding as
text), and a PDF whose text cannot be extracted (a scan/fax) is OCR'd
page-by-page or refused. Deployments that explicitly accept advisory-only
screening can set ``COMMUNITY_OCR_STRICT=0`` — the control degrades only by
operator choice, never silently.
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
    """Default ON: no screening → no upload (PRD §7 fail-closed). Set
    ``COMMUNITY_OCR_STRICT=0`` to explicitly accept advisory-only behavior."""
    raw = (os.getenv("COMMUNITY_OCR_STRICT") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


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


def _ocr_pdf_text(data: bytes, *, max_pages: int = 10) -> Optional[str]:
    """OCR a textless (scanned) PDF page-by-page. Returns the recovered text,
    or None when the raster/OCR toolchain is unavailable."""
    try:
        import pytesseract
        from pdf2image import convert_from_bytes
    except Exception:
        return None
    try:
        pages = convert_from_bytes(data, dpi=200, first_page=1, last_page=max_pages)
        return "\n".join(pytesseract.image_to_string(p) or "" for p in pages)
    except Exception:
        return None


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
    if not text.strip():
        # No text layer — a scanned chart or fax, exactly the highest-risk
        # artifact (audit finding: pdfminer returns "" here, it does not
        # raise). OCR the rendered pages; with no OCR toolchain, refuse.
        text = _ocr_pdf_text(data)
        if text is None:
            raise _reject(
                "attachment_unscannable",
                "This PDF has no extractable text (it looks like a scan) and image "
                "screening is unavailable, so it was not uploaded. Attach the page "
                "as a PNG/JPEG screenshot or paste the relevant text instead.",
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
    """Decode (BOM-aware), scan, and store the CANONICAL UTF-8 encoding of
    exactly the text that was scanned — never the original bytes. Storing the
    original allowed a UTF-16 file to be scanned as mojibake (identifiers
    invisible to the scanner) yet served intact (audit finding)."""
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        text = data.decode("utf-16", errors="replace")
    elif data.startswith(b"\xef\xbb\xbf"):
        text = data.decode("utf-8-sig", errors="replace")
    else:
        text = data.decode("utf-8", errors="replace")
    findings = phi_gate.scan_text(text)
    if findings:
        raise _phi_reject(phi_gate.categories_of(findings))
    return text.encode("utf-8"), mime


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
