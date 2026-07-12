"""``fhir_r4`` adapter (EHR PRD §6) — a FHIR R4 ``Bundle`` (JSON) →
ClinicalCase fragments. Dependency-free: a Bundle is JSON; we traverse the
resource shapes named in the PRD mapping table directly.

| FHIR resource                              | → fragment                       |
|--------------------------------------------|----------------------------------|
| Patient                                    | demographics (age band + sex)    |
| Observation (category=laboratory)          | lab_panels[].results[]           |
| Observation (category=vital-signs)         | vitals                           |
| Condition                                  | problem_list[]                   |
| MedicationStatement / MedicationRequest    | medications[]                    |
| DocumentReference / DiagnosticReport text  | notes[]                          |
| ImagingStudy / Media / Binary(image/*)     | counted in _imaging_skipped      |

Identifier discipline: Patient.name / identifier / address / telecom are never
read. Note authors reduce to a generalized role. Dates stay RAW here
(``collected_at``) — ``timeline.normalize_timeline`` destroys the calendar.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, List, Optional

from asclepius.case_formats import age_to_band
from asclepius.timeline import parse_datetime

_IMAGING_TYPES = ("ImagingStudy", "Media", "ImagingSelection")


class FhirParseError(ValueError):
    """Not a parseable FHIR R4 Bundle — the bundle entry should quarantine."""


def _codeable_text(cc: Any) -> str:
    if not isinstance(cc, dict):
        return ""
    if cc.get("text"):
        return str(cc["text"])
    for coding in cc.get("coding") or []:
        if coding.get("display"):
            return str(coding["display"])
        if coding.get("code"):
            return str(coding["code"])
    return ""


def _loinc_of(cc: Any) -> Optional[str]:
    for coding in (cc or {}).get("coding") or []:
        if "loinc" in str(coding.get("system") or "").lower() and coding.get("code"):
            return str(coding["code"])
    return None


def _obs_category(res: Dict[str, Any]) -> str:
    for cat in res.get("category") or []:
        for coding in (cat or {}).get("coding") or []:
            code = str(coding.get("code") or "").lower()
            if code in ("laboratory", "vital-signs"):
                return code
    return ""


def _quantity(res: Dict[str, Any]) -> tuple:
    q = res.get("valueQuantity") or {}
    if q:
        return q.get("value"), q.get("unit") or q.get("code")
    if res.get("valueString") is not None:
        return res.get("valueString"), None
    if res.get("valueCodeableConcept"):
        return _codeable_text(res["valueCodeableConcept"]), None
    return None, None


def _flag_of(res: Dict[str, Any]) -> str:
    for interp in res.get("interpretation") or []:
        for coding in (interp or {}).get("coding") or []:
            code = str(coding.get("code") or "").upper()
            if code in ("L", "H", "LL", "HH"):
                return code
    return ""


def _ref_range(res: Dict[str, Any]) -> tuple:
    for rr in res.get("referenceRange") or []:
        lo = (rr.get("low") or {}).get("value")
        hi = (rr.get("high") or {}).get("value")
        if lo is not None or hi is not None:
            return lo, hi
    return None, None


def _effective(res: Dict[str, Any]) -> str:
    return str(res.get("effectiveDateTime") or res.get("issued")
               or (res.get("effectivePeriod") or {}).get("start") or "")


def _note_from_attachment(att: Dict[str, Any]) -> Optional[str]:
    if not isinstance(att, dict):
        return None
    ct = str(att.get("contentType") or "")
    if ct.startswith("image/") or ct == "application/dicom":
        return None  # imaging content is never decoded
    if att.get("data"):
        try:
            return base64.b64decode(att["data"]).decode("utf-8", errors="replace")
        except Exception:
            return None
    return None


def _dosage_bits(res: Dict[str, Any]) -> Dict[str, Optional[str]]:
    for d in res.get("dosage") or res.get("dosageInstruction") or []:
        dose = None
        for dr in d.get("doseAndRate") or []:
            dq = dr.get("doseQuantity") or {}
            if dq.get("value") is not None:
                dose = f"{dq.get('value')} {dq.get('unit') or ''}".strip()
                break
        route = _codeable_text(d.get("route")) or None
        freq = None
        timing = ((d.get("timing") or {}).get("code") or {})
        if timing:
            freq = _codeable_text(timing) or None
        if not freq and d.get("text"):
            freq = str(d["text"])[:60]
        return {"dose": dose, "route": route, "freq": freq}
    return {"dose": None, "route": None, "freq": None}


def parse(raw: Any, *, specialty: str = "general", manifest: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """FHIR R4 Bundle JSON (str/bytes/dict) → ClinicalCase fragments."""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception as exc:
            raise FhirParseError(f"not valid JSON: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("resourceType") != "Bundle":
        raise FhirParseError("not a FHIR R4 Bundle (resourceType != 'Bundle')")

    resources = [e.get("resource") for e in raw.get("entry") or [] if isinstance(e.get("resource"), dict)]

    frag: Dict[str, Any] = {
        "demographics": {}, "lab_panels": [], "notes": [], "medications": [],
        "problem_list": [], "vitals": {}, "_imaging_skipped": 0, "_patient_keys": [],
    }

    birth_date = None
    lab_by_key: Dict[tuple, Dict[str, Any]] = {}
    report_names: Dict[str, str] = {}   # DiagnosticReport date → panel name
    latest_obs = None

    # First pass: DiagnosticReport names (so lab panels get real names) + latest date.
    for res in resources:
        rt = res.get("resourceType")
        if rt == "DiagnosticReport":
            eff = _effective(res)
            d = parse_datetime(eff)
            if d and _codeable_text(res.get("code")):
                report_names[str(d)] = _codeable_text(res.get("code"))
        if rt in ("Observation", "DiagnosticReport"):
            d = parse_datetime(_effective(res))
            if d and (latest_obs is None or d > latest_obs):
                latest_obs = d

    for res in resources:
        rt = res.get("resourceType")
        if rt in _IMAGING_TYPES or (
            rt == "Binary" and str(res.get("contentType") or "").startswith(("image/", "application/dicom"))
        ):
            frag["_imaging_skipped"] += 1
            continue

        if rt == "Patient":
            if res.get("id"):
                frag["_patient_keys"].append(str(res["id"]))
            gender = str(res.get("gender") or "").lower()
            if gender in ("male", "female"):
                frag["demographics"]["sex"] = "M" if gender == "male" else "F"
            birth_date = parse_datetime(res.get("birthDate"))
            # Deliberately untouched: name / identifier / address / telecom.

        elif rt == "Observation":
            cat = _obs_category(res)
            value, unit = _quantity(res)
            if value is None:
                continue
            name = _codeable_text(res.get("code")) or "Observation"
            if cat == "vital-signs":
                frag["vitals"][name] = f"{value} {unit}".strip() if unit else value
                continue
            if cat != "laboratory":
                continue
            eff = _effective(res)
            d = parse_datetime(eff)
            key = (str(d) if d else "", )
            panel = lab_by_key.setdefault(key, {
                "panel": report_names.get(str(d), "Labs") if d else "Labs",
                "results": [],
                **({"collected_at": eff} if eff else {"collected_offset_days": 0}),
            })
            lo, hi = _ref_range(res)
            result: Dict[str, Any] = {"analyte": name, "value": value, "flag": _flag_of(res)}
            loinc = _loinc_of(res.get("code"))
            if loinc:
                result["loinc"] = loinc
            if unit:
                result["unit"] = unit
            if lo is not None:
                result["ref_low"] = lo
            if hi is not None:
                result["ref_high"] = hi
            panel["results"].append(result)

        elif rt == "Condition":
            cond = _codeable_text(res.get("code"))
            if cond:
                frag["problem_list"].append({
                    "condition": cond,
                    "since": str(res.get("onsetDateTime") or res.get("recordedDate") or "") or None,
                })

        elif rt in ("MedicationStatement", "MedicationRequest"):
            drug = _codeable_text(res.get("medicationCodeableConcept"))
            if drug:
                frag["medications"].append({"drug": drug, **_dosage_bits(res)})

        elif rt in ("DocumentReference", "Composition", "DiagnosticReport"):
            texts: List[str] = []
            for content in res.get("content") or []:
                t = _note_from_attachment((content or {}).get("attachment") or {})
                if t:
                    texts.append(t)
            for form in res.get("presentedForm") or []:
                t = _note_from_attachment(form)
                if t:
                    texts.append(t)
            note_type = _codeable_text(res.get("type")) or ("Report" if rt == "DiagnosticReport" else "Progress")
            for t in texts:
                frag["notes"].append({
                    "note_type": note_type[:40],
                    "author_role": (specialty or "clinician").lower(),
                    "text": t.strip(),
                })

    # Age band: birthDate against the bundle's latest observation date — the age
    # AT THE ENCOUNTER, banded (never an exact age or the birth date itself).
    if birth_date and latest_obs:
        years = latest_obs.year - birth_date.year - (
            (latest_obs.month, latest_obs.day) < (birth_date.month, birth_date.day)
        )
        band = age_to_band(years)
        if band:
            frag["demographics"]["age_band"] = band

    frag["lab_panels"] = list(lab_by_key.values())
    # Suggested index anchor (PRD §7 "latest encounter/lab collection datetime"):
    # the adapter sees ALL observation timestamps (incl. vitals), so it proposes
    # the true latest; the manifest's index_event still overrides downstream.
    if latest_obs is not None:
        frag["_index_event"] = str(latest_obs)
    return frag
