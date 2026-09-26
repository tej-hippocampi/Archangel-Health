"""PDF worksheets: per-visit text extraction, optional local OCR, no image assets."""
from __future__ import annotations

import io
import math
import os
import re
import shutil
import tempfile
from pathlib import Path

from .ehr_mapping import PatientUnmapped, mapped_key
from .note_text import _date_shaped

try:
    import ocrmypdf
except ImportError:
    ocrmypdf = None
try:
    import pdfplumber
except ImportError:
    pdfplumber = None

DATE_HEADER = re.compile(r'^[ \t]*(?:date[ \t]+of[ \t]+service|service[ \t]+date|DOS|visit[ \t]+date)[ \t]*(?::[ \t]*|[ \t]+)([^\n]+)', re.I | re.M)
WORKSHEET = re.compile(r'\b(?:assessment|plan|impression)\b|\bA/P\b', re.I)


class PdfParseError(ValueError):
    pass


def service_date(text):
    for match in DATE_HEADER.finditer(text):
        value = _date_shaped(match.group(1).strip())
        if value:
            return value
    return None


def _ocr_page(page):
    from pypdf import PdfReader, PdfWriter
    from asclepius.ehr_sandbox.constants import settings
    if settings().ocr_enabled == '0':
        raise PdfParseError('ocr_unavailable: scanned page requires local OCR')
    if ocrmypdf is None and not (shutil.which('tesseract') and shutil.which('pdftoppm')):
        raise PdfParseError('ocr_unavailable: install Tesseract and Poppler or OCRmyPDF')
    # Disposable decrypted scratch, never a study asset or a replacement original.
    with tempfile.TemporaryDirectory(prefix='ehr-ocr-') as folder:
        source, target = Path(folder) / 'input.pdf', Path(folder) / 'output.pdf'
        writer = PdfWriter()
        writer.add_page(page)
        writer.write(source)
        try:
            width,height=float(page.mediabox.width),float(page.mediabox.height)
            if not all(math.isfinite(v) and v>0 for v in (width,height)) or width*height*(300/72)**2>25_000_000:
                raise ValueError('OCR page exceeds supported bounds')
            if ocrmypdf is not None:
                ocrmypdf.ocr(str(source), str(target), force_ocr=True, progress_bar=False,
                             jobs=1, tesseract_timeout=60, optimize=0)
                text = PdfReader(target).pages[0].extract_text() or ''
            else:
                # These dependencies already ship in the application image.
                # One bounded page at a time; no OCR images become study assets.
                from pdf2image import convert_from_path
                import pytesseract
                images=convert_from_path(str(source),dpi=300,first_page=1,last_page=1,
                                         grayscale=True,thread_count=1,timeout=60)
                try:
                    text=pytesseract.image_to_string(images[0],timeout=60) if images else ''
                finally:
                    for image in images: image.close()
        except Exception:
            raise PdfParseError('ocr_failed: scanned page could not be read') from None
    if len(text.strip()) < 40:
        raise PdfParseError('ocr_failed: insufficient text after OCR')
    return text


def _lab_tables(raw, page_indices, collected_at):
    """Only recognize explicit analyte/result/unit headers; never infer columns."""
    if pdfplumber is None or not collected_at:
        return []
    from .lab_csv import parse as parse_csv
    import csv
    panels = []
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        for index in page_indices:
            for table in pdf.pages[index].extract_tables() or []:
                if len(table) < 2:
                    continue
                header = [str(x or '').strip().lower() for x in table[0]]
                if not any(x in header for x in ('analyte', 'test', 'test name')) or not any(x in header for x in ('value', 'result')):
                    continue
                out = io.StringIO()
                writer = csv.writer(out)
                writer.writerow(header + ['collected_at'])
                writer.writerows([list(row) + [collected_at] for row in table[1:]])
                try:
                    panels.extend(parse_csv(out.getvalue())['lab_panels'])
                except ValueError:
                    continue  # Full original text is retained in the note.
    return panels


def parse(raw, *, specialty='general', manifest=None):
    from pypdf import PdfReader
    manifest = manifest or {}
    key = mapped_key(manifest)
    if key is None:
        base = os.path.basename(manifest.get('filename') or '')
        prefix = base.split('__', 1)[0] if '__' in base else ''
        if not prefix:
            raise PatientUnmapped('PDF requires a per-file patient mapping or patient filename prefix')
        key = 'prefix-' + prefix
    if not isinstance(raw, (bytes, bytearray)) or not bytes(raw).startswith(b'%PDF'):
        raise PdfParseError('invalid PDF signature')
    try:
        reader = PdfReader(io.BytesIO(raw), strict=True)
        if reader.is_encrypted:
            raise PdfParseError('encrypted PDF requires an unlocked de-identified re-upload')
        pages = list(reader.pages)
        if not pages:
            raise PdfParseError('PDF contains no pages')
        texts = [(page.extract_text() or '') for page in pages]
    except PdfParseError:
        raise
    except Exception:
        raise PdfParseError('unreadable PDF') from None
    segments = []
    for i, text in enumerate(texts):
        if len(text.strip()) < 40:
            text = _ocr_page(pages[i])
        at = service_date(text)
        # A new valid service-date header starts a visit; undated continuation
        # pages attach to the last segment, while an undated first page stays so.
        explicit_undated = DATE_HEADER.search(text) is not None and at is None
        if not segments or explicit_undated or (at is not None and at != segments[-1]['collected_at']):
            segments.append({'collected_at': at, 'texts': [], 'pages': []})
        segments[-1]['texts'].append(text)
        segments[-1]['pages'].append(i)
    frag = {'notes': [], 'lab_panels': [], '_patient_keys': [key]}
    for segment in segments:
        text = '\n'.join(segment['texts']).strip()
        kind = 'Visit worksheet' if WORKSHEET.search(text) else (
            'Lab report' if re.search(r'reference\s+range|\bref\.?\s*range', text, re.I) else
            'Correspondence' if re.search(r'\bDear\b', text) else 'Other')
        frag['notes'].append({'note_type': kind, 'text': text, 'author_role': 'clinician',
                              'collected_at': segment['collected_at']})
        if kind == 'Lab report':
            frag['lab_panels'].extend(_lab_tables(bytes(raw), segment['pages'], segment['collected_at']))
    return frag
