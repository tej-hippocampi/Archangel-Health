"""Plain-text clinical-note adapter (.txt / .md / .rtf / extracted PDF text).

The whole document becomes ONE ClinicalNote. ``note_type`` is inferred
best-effort from the first line / keywords; ``author_role`` is generalized to a
department/role keyword when one is present, else "clinician". A trivial RTF
control-word wrapper is stripped best-effort. Blank input raises
:class:`CaseIngestError`.
"""

from __future__ import annotations

import re
from typing import Optional

from asclepius.case_formats import CaseIngestError

from ._common import generalize_role, to_text

# Keyword -> canonical note_type. Checked against the first line first, then the
# whole text.
_NOTE_TYPE_KEYWORDS = [
    ("h&p", "H&P"),
    ("history and physical", "H&P"),
    ("history & physical", "H&P"),
    ("admission note", "H&P"),
    ("discharge summary", "Discharge"),
    ("discharge", "Discharge"),
    ("consult", "Consult"),
    ("nursing", "Nursing"),
    ("progress note", "Progress"),
    ("progress", "Progress"),
]


def _strip_rtf(text: str) -> str:
    """Best-effort removal of a trivial RTF control-word wrapper. Not a full RTF
    parser — good enough to recover plain text from ``{\\rtf1 ... }`` blobs."""
    s = text
    # Drop RTF unicode escapes like ሴ? -> keep nothing (best effort).
    s = re.sub(r"\\u-?\d+\??", "", s)
    # Drop hex escapes \'ab
    s = re.sub(r"\\'[0-9a-fA-F]{2}", "", s)
    # Drop control words: backslash + letters + optional numeric arg + optional space
    s = re.sub(r"\\[a-zA-Z]+-?\d*\s?", " ", s)
    # Drop remaining control symbols like \* \{ \}
    s = re.sub(r"\\[^a-zA-Z]", "", s)
    # Remove braces
    s = s.replace("{", " ").replace("}", " ")
    # Collapse whitespace
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n\s*\n+", "\n\n", s)
    return s.strip()


def _infer_note_type(text: str) -> str:
    first_line = ""
    for line in text.splitlines():
        if line.strip():
            first_line = line.strip().lower()
            break
    for kw, ntype in _NOTE_TYPE_KEYWORDS:
        if kw in first_line:
            return ntype
    low = text.lower()
    for kw, ntype in _NOTE_TYPE_KEYWORDS:
        if kw in low:
            return ntype
    return "Progress"


def parse(raw, *, specialty: str = "general") -> dict:
    text = to_text(raw)

    if text.lstrip().startswith("{\\rtf"):
        text = _strip_rtf(text)

    if not text.strip():
        raise CaseIngestError("note_text: blank input")

    note_type = _infer_note_type(text)
    # Look for a department/role in the first few lines (a header), else scan all.
    header = "\n".join(text.splitlines()[:5])
    author_role = generalize_role(header)
    if author_role == "clinician":
        author_role = generalize_role(text)

    return {
        "specialty": specialty,
        "patient_key": None,
        "notes": [{
            "note_type": note_type,
            "author_role": author_role,
            "text": text.strip(),
        }],
    }
