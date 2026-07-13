"""HL7 v2 pipe-delimited adapter (ORU_R01 spirit).

Parses segments (one per line). Maps OBR -> LabPanel, OBX -> a result on the
current panel, PID -> demographics (age band only), NTE/TX OBX -> notes. Missing
``MSH`` raises :class:`CaseIngestError`.

Deliberately never emits PID-5 (name) or PID-3 (MRN); ``patient_key`` is left
None (no identifying value is derived from the message).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from asclepius.case_formats import CaseIngestError

from ._common import birthdate_to_age_band, normalize_sex, to_text


def _split_fields(segment: str, field_sep: str) -> List[str]:
    return segment.split(field_sep)


def _component(field: str, index: int, comp_sep: str = "^") -> Optional[str]:
    """Return the 1-based component `index` of an HL7 field, or None."""
    if field is None:
        return None
    parts = field.split(comp_sep)
    if 0 <= index - 1 < len(parts):
        v = parts[index - 1].strip()
        return v or None
    return None


def _coerce_number(value: Optional[str]) -> Any:
    if value is None:
        return None
    s = str(value).strip()
    if s == "":
        return None
    try:
        if re.fullmatch(r"[+-]?\d+", s):
            return int(s)
        return float(s)
    except ValueError:
        return s


def _split_ref_range(ref: Optional[str]):
    """OBX-7 reference range -> (low, high). Handles 'lo-hi', '<hi', '>lo'."""
    if not ref:
        return (None, None)
    s = ref.strip()
    if s.startswith("<"):
        return (None, _coerce_number(s[1:]))
    if s.startswith(">"):
        return (_coerce_number(s[1:]), None)
    # Split on a hyphen that separates two numbers (allow negatives/decimals).
    m = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)\s*$", s)
    if m:
        return (_coerce_number(m.group(1)), _coerce_number(m.group(2)))
    return (None, None)


def parse(raw, *, specialty: str = "general") -> dict:
    text = to_text(raw)
    # Normalize segment separators: HL7 uses \r; tolerate \n too.
    lines = [ln for ln in re.split(r"\r\n|\r|\n", text) if ln.strip()]
    if not lines or not lines[0].startswith("MSH"):
        raise CaseIngestError("hl7v2: missing MSH header segment")

    msh = lines[0]
    # MSH: the char right after 'MSH' is the field separator; the next 4 are
    # encoding characters (component, repeat, escape, subcomponent).
    field_sep = msh[3] if len(msh) > 3 else "|"
    enc = msh[4:8] if len(msh) >= 8 else "^~\\&"
    comp_sep = enc[0] if enc else "^"

    demographics: Dict[str, Any] = {}
    lab_panels: List[dict] = []
    current_panel: Optional[dict] = None
    note_lines: List[str] = []

    for line in lines:
        fields = _split_fields(line, field_sep)
        seg = fields[0]

        if seg == "PID":
            # PID-7 DOB, PID-8 sex. Fields are 1-based in HL7; fields[0] is the
            # segment id, so PID-7 == fields[7].
            dob = fields[7] if len(fields) > 7 else None
            sex = fields[8] if len(fields) > 8 else None
            band = birthdate_to_age_band(_component(dob, 1) if dob else None)
            demographics = {"age_band": band, "sex": normalize_sex(_component(sex, 1) if sex else None)}

        elif seg == "OBR":
            # OBR-4 universal service id (code^text), OBR-7 observation datetime.
            obr4 = fields[4] if len(fields) > 4 else None
            panel_name = (_component(obr4, 2, comp_sep) or _component(obr4, 1, comp_sep) or "Labs") if obr4 else "Labs"
            obr7 = fields[7] if len(fields) > 7 else None
            collected = _component(obr7, 1, comp_sep) if obr7 else None
            current_panel = {"panel": panel_name, "collected_at": collected, "results": []}
            lab_panels.append(current_panel)

        elif seg == "OBX":
            value_type = fields[2] if len(fields) > 2 else ""
            obx3 = fields[3] if len(fields) > 3 else None
            obx5 = fields[5] if len(fields) > 5 else None
            # TX / FT free-text OBX -> treat as a note line.
            if value_type in ("TX", "FT") and obx5:
                note_lines.append(obx5.replace(comp_sep, " ").strip())
                continue
            loinc = _component(obx3, 1, comp_sep) if obx3 else None
            analyte = (_component(obx3, 2, comp_sep) or loinc) if obx3 else None
            if not analyte:
                continue
            unit = fields[6] if len(fields) > 6 else None
            ref = fields[7] if len(fields) > 7 else None
            flag = fields[8] if len(fields) > 8 else ""
            ref_low, ref_high = _split_ref_range(ref)
            result = {
                "analyte": analyte,
                "loinc": loinc,
                "value": _coerce_number(obx5),
                "unit": (unit or None) if unit is None else (unit.strip() or None),
                "ref_low": ref_low,
                "ref_high": ref_high,
                "flag": (flag or "").strip(),
            }
            if current_panel is None:
                current_panel = {"panel": "Labs", "collected_at": None, "results": []}
                lab_panels.append(current_panel)
            current_panel["results"].append(result)

        elif seg == "NTE":
            # NTE-3 comment.
            comment = fields[3] if len(fields) > 3 else None
            if comment:
                note_lines.append(comment.replace(comp_sep, " ").strip())

    lab_panels = [p for p in lab_panels if p["results"]]

    fragment: Dict[str, Any] = {"specialty": specialty, "patient_key": None}
    if demographics:
        fragment["demographics"] = demographics
    if lab_panels:
        fragment["lab_panels"] = lab_panels
    if note_lines:
        fragment["notes"] = [{
            "note_type": "Progress",
            "author_role": "clinician",
            "text": "\n".join(note_lines).strip(),
        }]
    return fragment
