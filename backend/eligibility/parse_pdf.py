"""PDF text extraction for eligibility documents.

Uses pdfminer.six for text extraction. If per-page average character count is
low (< 50), falls back to pytesseract OCR on that page (requires tesseract
binary installed at OS level).

Raises:
  PDFEncryptedError: password-protected (edge case §11.2)
  PDFParseError:     any other unrecoverable parse failure
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import List, Optional

log = logging.getLogger("eligibility.pdf")


class PDFEncryptedError(ValueError):
    """Raised for password-protected PDFs."""


class PDFParseError(RuntimeError):
    """Raised when a PDF can't be parsed for reasons other than encryption."""


@dataclass
class PDFParseResult:
    text: str
    pages: int
    ocr_used: bool
    ocr_unavailable: bool = False  # tesseract binary not installed

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "pages": self.pages,
            "ocr_used": self.ocr_used,
            "ocr_unavailable": self.ocr_unavailable,
        }


def parse_pdf(data: bytes) -> PDFParseResult:
    """Extract text from PDF bytes, falling back to OCR for image-only pages."""
    # Fast path: pdfminer.six
    try:
        from pdfminer.high_level import extract_text_to_fp
        from pdfminer.layout import LAParams
        from pdfminer.pdfdocument import PDFPasswordIncorrect
        from pdfminer.pdfparser import PDFSyntaxError
    except ImportError as e:
        raise PDFParseError(f"pdfminer.six not installed: {e}") from e

    buf = io.StringIO()
    try:
        with io.BytesIO(data) as src:
            extract_text_to_fp(src, buf, laparams=LAParams())
        text = buf.getvalue()
    except PDFPasswordIncorrect as e:
        raise PDFEncryptedError("PDF is password-protected") from e
    except PDFSyntaxError as e:
        # Sometimes encryption surfaces as a syntax error in pdfminer
        if "encrypted" in str(e).lower() or "password" in str(e).lower():
            raise PDFEncryptedError("PDF is password-protected") from e
        raise PDFParseError(f"Malformed PDF: {e}") from e
    except Exception as e:
        msg = str(e).lower()
        if "password" in msg or "encrypt" in msg:
            raise PDFEncryptedError("PDF is password-protected") from e
        raise PDFParseError(f"PDF parse failed: {e}") from e

    # Count pages for OCR threshold decision
    pages = _count_pages(data)
    avg_chars = (len(text.strip()) / max(pages, 1)) if text.strip() else 0.0
    ocr_used = False
    ocr_unavailable = False

    if avg_chars < 50 and pages > 0:
        ocr_text, ocr_unavailable = _ocr_pdf(data)
        if ocr_text.strip():
            text = ocr_text
            ocr_used = True

    return PDFParseResult(text=text, pages=pages, ocr_used=ocr_used, ocr_unavailable=ocr_unavailable)


def _count_pages(data: bytes) -> int:
    try:
        from pdfminer.pdfparser import PDFParser
        from pdfminer.pdfdocument import PDFDocument
        from pdfminer.pdfpage import PDFPage

        with io.BytesIO(data) as src:
            parser = PDFParser(src)
            doc = PDFDocument(parser)
            return sum(1 for _ in PDFPage.create_pages(doc))
    except Exception:
        return 1


def _ocr_pdf(data: bytes) -> tuple[str, bool]:
    """Run OCR across all pages. Returns (text, ocr_unavailable_flag)."""
    try:
        import pytesseract
        from pytesseract import TesseractNotFoundError
    except ImportError:
        log.warning("pytesseract not installed; skipping OCR")
        return ("", True)

    # Render pages via pdf2image if available; otherwise try pdfminer image
    # extraction fallback. pdf2image requires poppler. We try it, and if it
    # fails, return gracefully.
    try:
        from pdf2image import convert_from_bytes
    except ImportError:
        log.warning("pdf2image not installed; OCR skipped")
        return ("", True)

    try:
        images = convert_from_bytes(data, dpi=200)
    except Exception as e:
        log.warning("pdf2image render failed: %s", e)
        return ("", True)

    parts: List[str] = []
    ocr_unavailable = False
    for img in images:
        try:
            parts.append(pytesseract.image_to_string(img))
        except TesseractNotFoundError:
            log.warning("tesseract binary not found; OCR unavailable")
            ocr_unavailable = True
            break
        except Exception as e:
            log.warning("OCR page failed: %s", e)
    return ("\n\n".join(parts), ocr_unavailable)


def format_for_llm(result: PDFParseResult, filename: Optional[str] = None) -> str:
    header = f"=== PDF PARSED ({result.pages} page(s){', OCR used' if result.ocr_used else ''}) ==="
    if filename:
        header += f"  file={filename}"
    body = result.text.strip() or "(no extractable text)"
    if result.ocr_unavailable:
        body = "(OCR unavailable — please upload a text-extractable PDF)\n\n" + body
    return f"{header}\n{body}"
