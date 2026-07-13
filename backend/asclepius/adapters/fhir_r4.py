"""FHIR R4 JSON adapter.

Accepts either a ``Bundle`` (``resourceType == "Bundle"`` with
``entry[].resource``) or a single resource. Maps the common resource types onto
a ClinicalCase fragment. Invalid JSON or no recognizable resource raises
:class:`CaseIngestError`.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from asclepius.case_formats import CaseIngestError

from ._common import birthdate_to_age_band, generalize_role, normalize_sex, to_text


def _iter_resources(doc: Any):
    if isinstance(doc, dict) and doc.get("resourceType") == "Bundle":
        for entry in doc.get("entry") or []:
            if isinstance(entry, dict) and isinstance(entry.get("resource"), dict):
                yield entry["resource"]
    elif isinstance(doc, dict) and doc.get("resourceType"):
        yield doc


def _coding_display(cc: Optional[dict]) -> Optional[str]:
    """text, then first coding.display, then first coding.code."""
    if not isinstance(cc, dict):
        return None
    if cc.get("text"):
        return cc["text"]
    for coding in cc.get("coding") or []:
        if isinstance(coding, dict) and coding.get("display"):
            return coding["display"]
    for coding in cc.get("coding") or []:
        if isinstance(coding, dict) and coding.get("code"):
            return coding["code"]
    return None


def _first_loinc(cc: Optional[dict]) -> Optional[str]:
    if not isinstance(cc, dict):
        return None
    for coding in cc.get("coding") or []:
        if not isinstance(coding, dict):
            continue
        system = (coding.get("system") or "").lower()
        if "loinc" in system:
            return coding.get("code")
    # No explicit LOINC system — fall back to first code present.
    for coding in cc.get("coding") or []:
        if isinstance(coding, dict) and coding.get("code"):
            return coding.get("code")
    return None


def _has_category(resource: dict, wanted: str) -> bool:
    cats = resource.get("category")
    if isinstance(cats, dict):
        cats = [cats]
    for cat in cats or []:
        for coding in (cat.get("coding") if isinstance(cat, dict) else None) or []:
            if isinstance(coding, dict) and (coding.get("code") or "").lower() == wanted:
                return True
        if isinstance(cat, dict) and (cat.get("text") or "").lower() == wanted:
            return True
    return False


_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(html: Optional[str]) -> str:
    if not html:
        return ""
    text = _TAG_RE.sub(" ", html)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", text).strip()


def _interpretation_flag(resource: dict) -> str:
    interp = resource.get("interpretation")
    if isinstance(interp, dict):
        interp = [interp]
    for i in interp or []:
        for coding in (i.get("coding") if isinstance(i, dict) else None) or []:
            if isinstance(coding, dict) and coding.get("code"):
                return coding["code"]
    return ""


def _observation_lab_result(resource: dict) -> Dict[str, Any]:
    code = resource.get("code") or {}
    vq = resource.get("valueQuantity") or {}
    value: Any = vq.get("value")
    unit = vq.get("unit") or vq.get("code")
    if value is None:
        # Non-quantity values (string/codeable/boolean)
        if resource.get("valueString") is not None:
            value = resource.get("valueString")
        elif resource.get("valueCodeableConcept") is not None:
            value = _coding_display(resource.get("valueCodeableConcept"))
        elif resource.get("valueBoolean") is not None:
            value = resource.get("valueBoolean")
    ref_low = ref_high = None
    ranges = resource.get("referenceRange") or []
    if ranges and isinstance(ranges[0], dict):
        ref_low = (ranges[0].get("low") or {}).get("value")
        ref_high = (ranges[0].get("high") or {}).get("value")
    return {
        "analyte": _coding_display(code) or "unknown",
        "loinc": _first_loinc(code),
        "value": value,
        "unit": unit,
        "ref_low": ref_low,
        "ref_high": ref_high,
        "flag": _interpretation_flag(resource),
    }


def _dosage_fields(resource: dict) -> Dict[str, Optional[str]]:
    dosages = resource.get("dosage") or resource.get("dosageInstruction") or []
    if not dosages or not isinstance(dosages[0], dict):
        return {"dose": None, "route": None, "freq": None}
    d = dosages[0]
    dose = None
    for dq in d.get("doseAndRate") or []:
        q = (dq.get("doseQuantity") if isinstance(dq, dict) else None) or {}
        if q.get("value") is not None:
            dose = f"{q.get('value')} {q.get('unit') or q.get('code') or ''}".strip()
            break
    if dose is None and isinstance(d.get("doseQuantity"), dict):
        q = d["doseQuantity"]
        dose = f"{q.get('value')} {q.get('unit') or ''}".strip() or None
    route = _coding_display(d.get("route"))
    freq = None
    timing = (d.get("timing") or {}).get("code") if isinstance(d.get("timing"), dict) else None
    freq = _coding_display(timing) or d.get("text")
    return {"dose": dose or None, "route": route, "freq": freq}


def parse(raw, *, specialty: str = "general") -> dict:
    text = to_text(raw)
    try:
        doc = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise CaseIngestError(f"fhir_r4: not valid JSON ({exc})")

    resources = list(_iter_resources(doc))
    if not resources:
        raise CaseIngestError("fhir_r4: no recognizable FHIR resource (need resourceType)")

    fragment: Dict[str, Any] = {"specialty": specialty}
    problem_list: List[dict] = []
    medications: List[dict] = []
    vitals: Dict[str, Any] = {}
    notes: List[dict] = []
    patient_key: Optional[str] = None
    demographics: Dict[str, Any] = {}

    # Group lab Observations into panels keyed by effectiveDateTime.
    lab_panels_by_date: "Dict[Optional[str], Dict[str, Any]]" = {}
    lab_order: List[Optional[str]] = []

    recognized = False
    for r in resources:
        rtype = r.get("resourceType")
        if rtype == "Observation":
            recognized = True
            if _has_category(r, "vital-signs"):
                label = _coding_display(r.get("code")) or "vital"
                vq = r.get("valueQuantity") or {}
                val = vq.get("value")
                if val is None:
                    val = r.get("valueString")
                if vq.get("unit"):
                    vitals[label] = f"{val} {vq['unit']}".strip()
                else:
                    vitals[label] = val
            elif _has_category(r, "laboratory") or r.get("valueQuantity") or r.get("referenceRange"):
                collected = r.get("effectiveDateTime") or (r.get("effectivePeriod") or {}).get("start")
                if collected not in lab_panels_by_date:
                    lab_panels_by_date[collected] = {
                        "panel": "Labs",
                        "collected_at": collected,
                        "results": [],
                    }
                    lab_order.append(collected)
                lab_panels_by_date[collected]["results"].append(_observation_lab_result(r))
        elif rtype == "Condition":
            recognized = True
            problem_list.append({
                "condition": _coding_display(r.get("code")) or "unknown",
                "since": r.get("onsetDateTime") or (r.get("onsetPeriod") or {}).get("start"),
            })
        elif rtype in ("MedicationStatement", "MedicationRequest"):
            recognized = True
            drug = _coding_display(r.get("medicationCodeableConcept"))
            med = {"drug": drug or "unknown"}
            med.update(_dosage_fields(r))
            medications.append(med)
        elif rtype in ("DiagnosticReport", "DocumentReference"):
            recognized = True
            body = ""
            div = (r.get("text") or {}).get("div")
            if div:
                body = _strip_tags(div)
            if not body:
                # presentedForm / content attachments (data may be base64 -> skip)
                forms = r.get("presentedForm") or []
                for f in forms:
                    if isinstance(f, dict) and f.get("title"):
                        body = f["title"]
                        break
            if not body:
                for c in r.get("content") or []:
                    att = c.get("attachment") if isinstance(c, dict) else None
                    if isinstance(att, dict) and att.get("title"):
                        body = att["title"]
                        break
            role = "clinician"
            for p in (r.get("performer") or r.get("author") or []):
                disp = p.get("display") if isinstance(p, dict) else None
                role = generalize_role(disp)
                if role != "clinician":
                    break
            note_type = _coding_display(r.get("code")) or ("Report" if rtype == "DiagnosticReport" else "Document")
            notes.append({"note_type": note_type, "author_role": role, "text": body})
        elif rtype == "Patient":
            recognized = True
            patient_key = r.get("id") or patient_key
            band = birthdate_to_age_band(r.get("birthDate"))
            demographics = {"age_band": band, "sex": normalize_sex(r.get("gender"))}

    if not recognized:
        raise CaseIngestError("fhir_r4: no supported resource types found")

    lab_panels = [lab_panels_by_date[k] for k in lab_order if lab_panels_by_date[k]["results"]]

    if demographics:
        fragment["demographics"] = demographics
    if patient_key is not None:
        fragment["patient_key"] = patient_key
    if problem_list:
        fragment["problem_list"] = problem_list
    if medications:
        fragment["medications"] = medications
    if vitals:
        fragment["vitals"] = vitals
    if lab_panels:
        fragment["lab_panels"] = lab_panels
    if notes:
        fragment["notes"] = notes
    return fragment
