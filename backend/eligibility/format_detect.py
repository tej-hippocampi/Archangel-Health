"""Detect eligibility document format from bytes + filename.

Return one of: X12_271 | PDF | CSV | OTHER.
The PRD size limits are enforced by the upload endpoint, not here.
"""

from __future__ import annotations

import os
from typing import Literal

Format = Literal["X12_271", "PDF", "CSV", "OTHER"]

# PRD §4.2.2 max sizes (bytes)
MAX_SIZE_BY_FORMAT: dict[str, int] = {
    "X12_271": 5 * 1024 * 1024,
    "PDF": 25 * 1024 * 1024,
    "CSV": 10 * 1024 * 1024,
    "OTHER": 25 * 1024 * 1024,
}

X12_EXTS = {".x12", ".271", ".edi"}
PDF_EXTS = {".pdf"}
CSV_EXTS = {".csv", ".tsv"}
TEXT_EXTS = {".txt"}


def detect_format(filename: str, head_bytes: bytes) -> Format:
    """Detect via magic bytes + extension.

    Order:
      1. %PDF header → PDF (regardless of extension)
      2. ISA*... prefix → X12_271 (typical for .x12/.271/.edi/.txt)
      3. .csv/.tsv extension with UTF-8 + delimiter → CSV
      4. otherwise OTHER
    """
    ext = os.path.splitext(filename or "")[1].lower()
    head4 = head_bytes[:4]
    # 1. PDF magic
    if head4.startswith(b"%PDF"):
        return "PDF"
    # 2. X12 envelope — must be "ISA" followed by a non-alphanumeric element
    # delimiter (typically "*", but per the X12 spec it can be any single
    # non-letter, non-digit char like "|", "^", "~"). Without this guard we
    # would mis-classify any file starting with "ISA..." (e.g. "ISABEL").
    try:
        stripped = head_bytes.lstrip()
        if len(stripped) >= 4 and stripped[:3] == b"ISA":
            delim = bytes([stripped[3]])
            if not delim.isalnum() and delim not in (b"\n", b"\r", b" ", b"\t"):
                return "X12_271"
    except Exception:
        pass
    # 3. CSV
    if ext in CSV_EXTS:
        try:
            sample = head_bytes[:1024].decode("utf-8", errors="strict")
            if ("," in sample or "\t" in sample) and ("\n" in sample or len(sample) > 0):
                return "CSV"
        except UnicodeDecodeError:
            return "OTHER"
    # .txt could still be X12 (already handled above); otherwise fall through to OTHER
    if ext in X12_EXTS:
        # extension says X12 but header missed — treat as OTHER to avoid surprises
        return "OTHER"
    if ext in PDF_EXTS:
        return "OTHER"
    return "OTHER"


def max_size_for(fmt: Format) -> int:
    return MAX_SIZE_BY_FORMAT.get(fmt, MAX_SIZE_BY_FORMAT["OTHER"])
