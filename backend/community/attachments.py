"""Community attachment gate (PRD §7.4) — strip metadata, scan for burned-in PHI.

Every upload passes through here BEFORE any byte reaches the asset store:

  * **Images (png/jpeg)** — re-encoded to a clean raster with all technical
    metadata removed (EXIF/GPS/device/timestamps — reuses the proven
    ``asclepius.assets`` strip), then OCR'd (pytesseract) and the recovered
    text run through the §7 PHI scanner. A screenshot of an EHR screen is the
    single most likely leak in a physician chat.
  * **PDFs** — extracted text and every rendered page are screened, including
    images on pages that also contain text. Rendering/OCR proceeds one page at
    a time within page, pixel and execution budgets; incomplete screening is
    refused. The document is rewritten (pypdf) to drop document-info metadata.
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
import html
import logging
import math
import os
from pathlib import Path
import subprocess
import tempfile
import time
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

# These are supported-document limits, never a prefix to silently accept.
MAX_PDF_PAGES = 25
PDF_OCR_DPI = 200
MAX_PDF_PAGE_PIXELS = 10_000_000
MAX_PDF_PAGE_DIMENSION = 5000
MAX_PDF_TOTAL_PIXELS = 100_000_000
MAX_PDF_SCREEN_SECONDS = 60
MAX_PDF_SCREEN_TEXT = 1_000_000
MAX_PDF_OBJECTS = 50_000
MAX_PDF_OBJECT_DEPTH = 100


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


def _ocr_pdf_text(data: bytes, *, max_pages: int = MAX_PDF_PAGES) -> Optional[str]:
    """Screen EVERY rendered page; return None if any page cannot be screened.

    Bounds are checked before rendering, and each raster is released before
    the next page is loaded. A partial OCR result is never returned.
    """
    try:
        import pytesseract
        from PIL import Image
        from pypdf import PdfReader
    except Exception:
        return None
    try:
        reader = PdfReader(io.BytesIO(data))
        page_count = len(reader.pages)
        if reader.is_encrypted or not 0 < page_count <= min(max_pages, MAX_PDF_PAGES):
            return None
        total_pixels, page_dimensions = 0, []
        for page in reader.pages:
            scale = float(page.get("/UserUnit", 1)) * PDF_OCR_DPI / 72
            width, height = float(page.mediabox.width) * scale, float(page.mediabox.height) * scale
            if not all(math.isfinite(v) and 0 < v <= MAX_PDF_PAGE_DIMENSION for v in (width, height)):
                return None
            pixels = math.ceil(width) * math.ceil(height)
            total_pixels += pixels
            if pixels > MAX_PDF_PAGE_PIXELS or total_pixels > MAX_PDF_TOTAL_PIXELS:
                return None
            page_dimensions.append(math.ceil(max(width, height)))
        deadline = time.monotonic() + MAX_PDF_SCREEN_SECONDS
        text_parts, text_size = [], 0
        with tempfile.TemporaryDirectory(prefix="community_pdf_screen_") as work:
            source = Path(work) / "source.pdf"
            source.write_bytes(data)
            output = Path(work) / "page"
            for page_number in range(1, page_count + 1):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                # No output for a later page must fail, never reuse the
                # previous page's successfully screened disposable raster.
                output.with_suffix(".png").unlink(missing_ok=True)
                # Call the renderer directly: pdf2image's timeout does not
                # cover its preliminary pdfinfo subprocess. Output stays on
                # disk, and exactly one bounded page is decoded at a time.
                subprocess.run(
                    ["pdftoppm", "-f", str(page_number), "-l", str(page_number),
                     "-r", str(PDF_OCR_DPI), "-scale-to", str(page_dimensions[page_number - 1]),
                     "-singlefile", "-png", str(source), str(output)],
                    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=min(10, remaining),
                )
                with Image.open(output.with_suffix(".png")) as page:
                    if page.width * page.height > MAX_PDF_PAGE_PIXELS:
                        return None
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    text = pytesseract.image_to_string(page, timeout=min(10, remaining)) or ""
                    text_size += len(text)
                    if text_size > MAX_PDF_SCREEN_TEXT or time.monotonic() > deadline:
                        return None
                    text_parts.append(text)
        return "\n".join(text_parts)
    except Exception:
        return None


def _sanitize_pdf_objects(reader) -> None:
    """Remove reachable XMP and inspect content not exposed by page rendering.

    Ordinary text/link/markup annotations remain intact. Embedded files,
    forms, executable actions and multimedia need their own screening path,
    so those containers are refused rather than copied with unchecked bytes.
    Parent/page references create legitimate cycles; visited references plus
    explicit work/depth budgets keep that traversal finite.
    """
    from pypdf.generic import (
        ArrayObject, ByteStringObject, DictionaryObject, IndirectObject,
        NameObject, NullObject, TextStringObject,
    )

    unsupported_keys = {"/AcroForm", "/XFA", "/EmbeddedFiles", "/EF", "/AF",
                        "/Collection", "/RichMediaContent", "/PieceInfo", "/AA"}
    annotation_types = {"/Text", "/FreeText", "/Link", "/Line", "/Square", "/Circle",
                        "/Polygon", "/PolyLine", "/Highlight", "/Underline", "/Squiggly",
                        "/StrikeOut", "/Stamp", "/Caret", "/Ink", "/Popup"}
    text_fields = {"/Contents", "/T", "/Subj", "/RC", "/NM", "/TU", "/TM", "/URI"}
    seen_refs, seen_objects, texts = set(), set(), []
    work, text_size = 0, 0

    def unsupported():
        raise _reject("attachment_unscannable",
                      "This PDF contains content that could not be fully screened, so it was not uploaded. "
                      "Export a standard PDF or paste the relevant text instead.")

    def inspect_text(value):
        nonlocal text_size
        if isinstance(value, IndirectObject):
            value = value.get_object()
        if value is None or isinstance(value, NullObject):
            return
        if not isinstance(value, TextStringObject) or isinstance(value, NameObject):
            # Stream-backed/rich binary payloads cannot be treated as an empty
            # comment simply because they do not decode to a plain string.
            unsupported()
        text = html.unescape(str(value))
        text_size += len(text)
        if text_size > MAX_PDF_SCREEN_TEXT:
            unsupported()
        texts.append(text)

    reader.trailer.pop("/Info", None)
    pending = [(reader.trailer, 0, False)]
    while pending:
        obj, depth, annotation = pending.pop()
        work += 1
        if work > MAX_PDF_OBJECTS or depth > MAX_PDF_OBJECT_DEPTH:
            unsupported()
        if isinstance(obj, IndirectObject):
            ref = (id(obj.pdf), obj.idnum, obj.generation, annotation)
            if ref in seen_refs:
                continue
            seen_refs.add(ref)
            obj = obj.get_object()
        if not isinstance(obj, (DictionaryObject, ArrayObject)):
            continue
        identity = (id(obj), annotation)
        if identity in seen_objects:
            continue
        seen_objects.add(identity)
        if isinstance(obj, DictionaryObject):
            obj.pop("/Metadata", None)
            if unsupported_keys.intersection(obj):
                unsupported()
            if obj.get("/Type") in ("/Filespec", "/EmbeddedFile", "/Metadata"):
                unsupported()
            action = obj.get("/S")
            if action in ("/JavaScript", "/Launch", "/SubmitForm", "/ImportData", "/Rendition", "/GoToE", "/GoToR", "/Sound", "/Movie"):
                unsupported()
            if annotation or obj.get("/Type") == "/Annot":
                if obj.get("/Subtype") not in annotation_types:
                    unsupported()
                for key, value in obj.items():
                    if key in text_fields or isinstance(value, (TextStringObject, ByteStringObject)):
                        inspect_text(value)
            elif "/URI" in obj:
                inspect_text(obj.raw_get("/URI"))
            children = [(value, depth + 1, key == "/Annots") for key, value in obj.items()]
        else:
            children = [(value, depth + 1, annotation) for value in obj]
        if work + len(pending) + len(children) > MAX_PDF_OBJECTS:
            unsupported()
        pending.extend(children)
    findings = phi_gate.scan_text("\n".join(texts))
    if findings:
        raise _phi_reject(phi_gate.categories_of(findings))


def _process_pdf(data: bytes) -> Tuple[bytes, str]:
    """Screen pages and hidden containers; remove document-info and XMP metadata."""
    try:
        from pdfminer.high_level import extract_text
        # The OCR leg checks the actual total page count before acceptance.
        text = extract_text(io.BytesIO(data), maxpages=MAX_PDF_PAGES) or ""
        if len(text) > MAX_PDF_SCREEN_TEXT:
            raise ValueError("PDF text exceeds screening budget")
    except Exception:
        raise _reject(
            "attachment_unscannable",
            "This PDF could not be screened for identifiers, so it was not uploaded. "
            "Export it again or paste the relevant text instead.",
        )
    findings = phi_gate.scan_text(text)
    if findings:
        raise _phi_reject(phi_gate.categories_of(findings))
    # Text on one page (or even on the same page) does not prove its image
    # content is clean. Every page must complete visual screening as well.
    image_text = _ocr_pdf_text(data)
    if image_text is None:
        raise _reject(
            "attachment_unscannable",
            "This PDF could not be fully screened for identifiers, so it was not uploaded. "
            "Attach individual pages as PNG/JPEG screenshots or paste the relevant text instead.",
        )
    findings = phi_gate.scan_text(image_text)
    if findings:
        raise _phi_reject(phi_gate.categories_of(findings))
    # Sanitize BEFORE cloning: otherwise orphaned metadata stream objects can
    # remain in the output even when their dictionary reference is removed.
    try:
        from pypdf import PdfReader, PdfWriter
        reader = PdfReader(io.BytesIO(data))
        _sanitize_pdf_objects(reader)
        writer = PdfWriter()
        writer.metadata = None
        for page in reader.pages:
            writer.add_page(page)
        out = io.BytesIO()
        writer.write(out)
        return out.getvalue(), "application/pdf"
    except AttachmentRejected:
        raise
    except Exception:
        raise _reject(
            "attachment_unscannable",
            "This PDF could not be sanitized, so it was not uploaded.",
        )


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
