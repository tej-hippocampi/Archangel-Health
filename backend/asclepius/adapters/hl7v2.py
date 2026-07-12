"""``hl7v2`` adapter (EHR PRD §6) — HL7 v2.x ``ORU_R01`` result messages →
ClinicalCase fragments, following the official HL7 v2-to-FHIR ``ORU_R01``
ConceptMap segment mapping. Dependency-free pipe parsing.

| segment | → fragment                                                        |
|---------|-------------------------------------------------------------------|
| PID     | demographics: PID-7 birthdate → AGE BAND vs obs date; PID-8 sex.  |
|         | PID-3 (MRN) / PID-5 (name) / PID-11 (address) / PID-13 (phone)    |
|         | are NEVER read.                                                   |
| OBR     | one LabPanel — OBR-4 panel name, OBR-7 observation datetime       |
| OBX     | a result — OBX-3 code^text (LOINC), OBX-5 value, OBX-6 unit,      |
|         | OBX-7 ref range "lo-hi", OBX-8 abnormal flag                      |
| NTE     | note lines (grouped into one Progress note)                        |

Dates stay RAW (``collected_at``) — ``timeline.normalize_timeline`` converts.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from asclepius.case_formats import age_to_band
from asclepius.timeline import parse_datetime

_VALID_FLAGS = {"L", "H", "LL", "HH"}


class Hl7ParseError(ValueError):
    """Not a parseable HL7 v2 message — the bundle entry should quarantine."""


def _fields(segment: str) -> List[str]:
    return segment.split("|")


def _comp(field: str, idx: int = 0) -> str:
    parts = (field or "").split("^")
    return parts[idx].strip() if idx < len(parts) else ""


def _flag(raw: str) -> str:
    f = (raw or "").strip().upper()
    if f in _VALID_FLAGS:
        return f
    return {"LL": "LL", "HH": "HH", "A": "", "AA": "", "N": ""}.get(f, "")


def _ref_range(raw: str) -> tuple:
    s = (raw or "").strip()
    if "-" in s:
        lo_s, _, hi_s = s.partition("-")
        def _n(x: str):
            x = x.strip()
            try:
                return int(x) if x.lstrip("+-").isdigit() else float(x)
            except ValueError:
                return None
        return _n(lo_s), _n(hi_s)
    return None, None


def _num(raw: str) -> Any:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        return int(s) if s.lstrip("+-").isdigit() else float(s)
    except ValueError:
        return s


def parse(raw: Any, *, specialty: str = "general", manifest: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One or more HL7 v2 messages (str/bytes) → ClinicalCase fragments."""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw or "")
    # HL7 segment separator is CR; be liberal (files arrive with \n or \r\n).
    segments = [s.strip() for s in text.replace("\r\n", "\r").replace("\n", "\r").split("\r") if s.strip()]
    if not segments or not segments[0].startswith("MSH|"):
        raise Hl7ParseError("not an HL7 v2 message (no MSH segment)")

    frag: Dict[str, Any] = {
        "demographics": {}, "lab_panels": [], "notes": [], "_patient_keys": [],
    }
    birth_date = None
    latest_obs = None
    current_panel: Optional[Dict[str, Any]] = None
    note_lines: List[str] = []

    for seg in segments:
        f = _fields(seg)
        sid = f[0]

        if sid == "PID":
            # PID-3 is field index 3, PID-7 index 7, PID-8 index 8 (index 0 = 'PID').
            # A patient GROUPING key only — an opaque per-bundle key, never shipped.
            pid3 = _comp(f[3]) if len(f) > 3 else ""
            if pid3:
                frag["_patient_keys"].append(f"hl7-{abs(hash(pid3)) % 10**10}")
            if len(f) > 7 and f[7]:
                birth_date = parse_datetime(f[7])
            if len(f) > 8:
                sex = _comp(f[8]).upper()
                if sex in ("M", "F"):
                    frag["demographics"]["sex"] = sex
            # PID-5 (name) / PID-11 (address) / PID-13 (phone): never read.

        elif sid == "OBR":
            panel_name = _comp(f[4], 1) or _comp(f[4]) or "Labs" if len(f) > 4 else "Labs"
            collected = f[7].strip() if len(f) > 7 and f[7].strip() else ""
            d = parse_datetime(collected)
            if d and (latest_obs is None or d > latest_obs):
                latest_obs = d
            current_panel = {
                "panel": panel_name, "results": [],
                **({"collected_at": collected} if collected else {"collected_offset_days": 0}),
            }
            frag["lab_panels"].append(current_panel)

        elif sid == "OBX" and len(f) > 5:
            analyte = _comp(f[3], 1) or _comp(f[3])
            value = _num(f[5])
            if not analyte or value is None:
                continue
            result: Dict[str, Any] = {"analyte": analyte, "value": value}
            code = _comp(f[3])
            coding_sys = _comp(f[3], 2).upper() if len(f) > 3 else ""
            if code and ("LN" in coding_sys or coding_sys == ""):
                # OBX-3.3 == LN marks a LOINC code; keep it when plausibly LOINC-shaped.
                if "LN" in coding_sys or (code.replace("-", "").isdigit() and "-" in code):
                    result["loinc"] = code
            if len(f) > 6 and f[6].strip():
                result["unit"] = _comp(f[6])
            lo, hi = _ref_range(f[7] if len(f) > 7 else "")
            if lo is not None:
                result["ref_low"] = lo
            if hi is not None:
                result["ref_high"] = hi
            result["flag"] = _flag(f[8] if len(f) > 8 else "")
            if current_panel is None:
                current_panel = {"panel": "Labs", "results": [], "collected_offset_days": 0}
                frag["lab_panels"].append(current_panel)
            current_panel["results"].append(result)

        elif sid == "NTE" and len(f) > 3 and f[3].strip():
            note_lines.append(f[3].strip())

    if note_lines:
        frag["notes"].append({
            "note_type": "Progress",
            "author_role": (specialty or "clinician").lower(),
            "text": "\n".join(note_lines),
        })

    if birth_date and latest_obs:
        years = latest_obs.year - birth_date.year - (
            (latest_obs.month, latest_obs.day) < (birth_date.month, birth_date.day)
        )
        band = age_to_band(years)
        if band:
            frag["demographics"]["age_band"] = band

    # Suggested index anchor (PRD §7): the latest OBR observation datetime.
    if latest_obs is not None:
        frag["_index_event"] = str(latest_obs)
    return frag
