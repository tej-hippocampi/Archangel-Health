"""Shared helpers for the clinical-data format adapters.

Kept inside the adapters package so the adapters have a single place for the
cross-cutting concerns every format shares: decoding bytes, computing an age
BAND (never an exact age / raw DOB), and generalizing an author role.
"""

from __future__ import annotations

import datetime
import re
from typing import Optional

from asclepius.case_formats import age_to_band

# Department / service keywords -> generalized author role. Order matters only
# for readability; the first keyword found in a text wins.
_ROLE_KEYWORDS = {
    "nephrology": "nephrology",
    "renal": "nephrology",
    "cardiology": "cardiology",
    "cardiac": "cardiology",
    "icu": "ICU",
    "intensive care": "ICU",
    "critical care": "ICU",
    "pulmonology": "pulmonology",
    "pulmonary": "pulmonology",
    "endocrinology": "endocrinology",
    "gastroenterology": "gastroenterology",
    "hematology": "hematology",
    "oncology": "oncology",
    "neurology": "neurology",
    "nursing": "nursing",
    "surgery": "surgery",
    "surgical": "surgery",
    "emergency": "emergency",
    "ed ": "emergency",
    "infectious disease": "infectious disease",
    "psychiatry": "psychiatry",
    "radiology": "radiology",
    "pathology": "pathology",
    "internal medicine": "internal medicine",
    "family medicine": "family medicine",
    "hospitalist": "hospitalist",
}


def to_text(raw) -> str:
    """Decode ``raw`` (bytes or str) to text; bytes are utf-8 with replacement."""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    if raw is None:
        return ""
    return str(raw)


def _parse_date(value: str) -> Optional[datetime.date]:
    """Best-effort parse of a date from an ISO / HL7 / slash string. Returns a
    ``datetime.date`` or None. Never raises."""
    if not value:
        return None
    s = str(value).strip()
    # HL7 datetime: YYYYMMDD[HHMM...]. Take the leading 8 digits.
    m = re.match(r"^(\d{4})(\d{2})(\d{2})", s)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    # ISO date/datetime: YYYY-MM-DD...
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    # US slash: M/D/YYYY
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        try:
            return datetime.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    return None


def birthdate_to_age_band(value: Optional[str], today: Optional[datetime.date] = None) -> Optional[str]:
    """Convert a birthDate/DOB STRING to an age BAND (never a raw date). If the
    date cannot be parsed, returns None — the pipeline can still handle a date it
    is given separately, but this helper never emits a raw DOB."""
    d = _parse_date(value) if value else None
    if d is None:
        return None
    ref = today or datetime.date.today()
    age = ref.year - d.year - ((ref.month, ref.day) < (d.month, d.day))
    if age < 0:
        return None
    return age_to_band(age)


def normalize_sex(value: Optional[str]) -> Optional[str]:
    """Normalize a gender/sex code to a short label; unknown -> the raw stripped
    value (still non-identifying) or None."""
    if not value:
        return None
    v = str(value).strip().lower()
    if v in ("m", "male"):
        return "male"
    if v in ("f", "female"):
        return "female"
    if v in ("o", "other"):
        return "other"
    if v in ("u", "unk", "unknown", "und", "undifferentiated"):
        return "unknown"
    return str(value).strip() or None


def generalize_role(text: Optional[str], default: str = "clinician") -> str:
    """Map free text (a department, a title line) to a generalized author role.
    Never returns a person's name — only a role/department keyword or the
    default."""
    if not text:
        return default
    low = str(text).lower()
    for kw, role in _ROLE_KEYWORDS.items():
        if kw in low:
            return role
    return default
