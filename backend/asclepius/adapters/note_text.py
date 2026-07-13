"""``note_text`` adapter (EHR PRD §6) — a plain-text/markdown clinical note →
``notes`` fragments. ``note_type`` comes from the manifest or a filename hint;
``author_role`` is always a GENERALIZED role (a specialty/service line), never a
person — if a manifest supplies something name-shaped we fall back to
"clinician" rather than carry it."""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

_KNOWN_TYPES = ("H&P", "Progress", "Consult", "Nursing", "Discharge", "ED Provider", "Operative")

# A role must look like a service line, not a person: letters/spaces only, no
# honorifics, no digits. "nephrology" ✓ · "Dr. Jane Doe" ✗ · "J. Smith RN" ✗
_ROLE_OK_RE = re.compile(r"^[a-z][a-z /&-]{1,40}$")
_HONORIFIC_RE = re.compile(r"\b(dr|md|do|rn|np|pa)\b\.?", re.IGNORECASE)


def _note_type_from(filename: Optional[str], manifest_type: Optional[str]) -> str:
    if manifest_type:
        for t in _KNOWN_TYPES:
            if t.lower() == str(manifest_type).strip().lower():
                return t
        return str(manifest_type).strip()[:40] or "Progress"
    name = (filename or "").lower()
    for t in _KNOWN_TYPES:
        if t.lower().replace("&", "") in name.replace("&", "").replace("_", " ").replace("-", " "):
            return t
    if "discharge" in name:
        return "Discharge"
    if "consult" in name:
        return "Consult"
    return "Progress"


def _safe_role(candidate: Optional[str], specialty: str) -> str:
    c = (candidate or "").strip().lower()
    if c and _ROLE_OK_RE.match(c) and not _HONORIFIC_RE.search(c):
        return c
    return (specialty or "clinician").strip().lower() or "clinician"


def parse(raw: Any, *, specialty: str = "general", manifest: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Note text (str/bytes) → ``{"notes": [{note_type, author_role, text}]}``.
    ``manifest`` may carry ``{note_type, author_role, filename, patient_key}``."""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw or "")
    text = text.strip()
    if not text:
        return {"notes": []}
    m = manifest or {}
    frag: Dict[str, Any] = {"notes": [{
        "note_type": _note_type_from(m.get("filename"), m.get("note_type")),
        "author_role": _safe_role(m.get("author_role"), specialty),
        "text": text,
    }]}
    if m.get("patient_key"):
        frag["_patient_keys"] = [str(m["patient_key"])]
    return frag
