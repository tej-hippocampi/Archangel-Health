import copy
import json
import re
from datetime import date
from typing import Any, Dict, List, Tuple


def _norm(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip().lower()


def _schema() -> Dict[str, Any]:
    # Mirrors the 11-section intake form schema from the feature spec.
    return {
        "section1_demographics": {
            "fullLegalName": {"value": "", "source": "patient_record"},
            "preferredName": {"value": "", "source": "interview"},
            "dateOfBirth": {"value": "", "source": "patient_record"},
            "age": {"value": None, "source": "calculated"},
            "sexAssignedAtBirth": {"value": "", "source": "patient_record"},
            "genderIdentity": {"value": "", "source": "interview|patient_record"},
            "ethnicity": {"value": "", "source": "patient_record|interview"},
            "primaryLanguage": {"value": "", "source": "patient_record|interview"},
            "interpreterNeeded": {"value": None, "source": "interview"},
            "address": {"value": "", "source": "patient_record"},
            "phonePrimary": {"value": "", "source": "patient_record"},
            "phoneEmergency": {"value": "", "source": "patient_record"},
            "email": {"value": "", "source": "patient_record"},
            "emergencyContactName": {"value": "", "source": "interview|patient_record"},
            "emergencyContactRelationship": {"value": "", "source": "interview|patient_record"},
            "emergencyContactPhone": {"value": "", "source": "interview|patient_record"},
            "insuranceProvider": {"value": "", "source": "patient_record"},
            "insurancePolicyNumber": {"value": "", "source": "patient_record"},
            "insuranceGroupNumber": {"value": "", "source": "patient_record"},
            "referringPhysician": {"value": "", "source": "prep_document"},
        },
        "section2_surgicalInfo": {
            "scheduledProcedure": {"value": "", "source": "prep_document"},
            "procedureCPTCodes": {"value": [], "source": "prep_document"},
            "surgicalSite": {"value": "", "source": "prep_document"},
            "laterality": {"value": "", "source": "prep_document"},
            "surgeonName": {"value": "", "source": "prep_document"},
            "anesthesiologist": {"value": "", "source": "prep_document"},
            "scheduledDateTime": {"value": "", "source": "prep_document"},
            "facilityLocation": {"value": "", "source": "prep_document"},
            "procedureType": {"value": "", "source": "prep_document"},
            "estimatedDuration": {"value": "", "source": "prep_document"},
            "preOpDiagnosis": {"value": "", "source": "prep_document"},
        },
        "section3_medicalHistory": {
            "activeConditions": {"value": [], "source": "interview", "note": ""},
            "hypertension": {"value": None, "source": "interview", "controlled": None},
            "diabetes": {"value": None, "source": "interview", "type": "", "a1c": ""},
            "heartDisease": {"value": None, "source": "interview", "details": ""},
            "lungDisease": {"value": None, "source": "interview", "details": ""},
            "kidneyDisease": {"value": None, "source": "interview"},
            "liverDisease": {"value": None, "source": "interview"},
            "bleedingClottingDisorders": {"value": None, "source": "interview"},
            "seizureDisorder": {"value": None, "source": "interview"},
            "strokeTIA": {"value": None, "source": "interview"},
            "cancer": {"value": None, "source": "interview", "type": "", "treatmentStatus": ""},
            "thyroidDisorder": {"value": None, "source": "interview"},
            "autoimmuneConditions": {"value": None, "source": "interview", "details": ""},
            "mentalHealth": {"value": None, "source": "interview", "details": ""},
            "otherConditions": {"value": "", "source": "interview"},
        },
        "section4_surgicalAnesthesiaHistory": {
            "previousSurgeries": {"value": [], "source": "interview"},
            "previousAnesthesiaTypes": {"value": [], "source": "interview"},
            "adverseAnesthesiaReaction": {"value": None, "source": "interview", "details": ""},
            "familyAnesthesiaProblems": {"value": None, "source": "interview", "details": ""},
            "difficultIntubation": {"value": None, "source": "interview"},
            "postOpNauseaVomiting": {"value": None, "source": "interview"},
            "malignantHyperthermia": {"value": None, "source": "interview", "personal": None, "family": None},
        },
        "section5_medicationsAllergies": {
            "currentMedications": {"value": [], "source": "interview|patient_record"},
            "bloodThinners": {"value": [], "source": "interview", "holdStatus": ""},
            "insulinDiabetesMeds": {"value": [], "source": "interview"},
            "bloodPressureMeds": {"value": [], "source": "interview"},
            "herbalSupplementsOTC": {"value": [], "source": "interview"},
            "medicationAllergies": {"value": [], "source": "interview"},
            "latexAllergy": {"value": None, "source": "interview"},
            "iodineContrastAllergy": {"value": None, "source": "interview"},
            "foodAllergies": {"value": [], "source": "interview"},
            "adhesiveTapeAllergy": {"value": None, "source": "interview"},
            "otherAllergies": {"value": "", "source": "interview"},
        },
        "section6_socialHistory": {
            "tobaccoUse": {"value": "", "source": "interview", "status": "", "packYears": ""},
            "alcoholUse": {"value": "", "source": "interview", "frequency": "", "amount": ""},
            "recreationalDrugUse": {"value": "", "source": "interview", "type": "", "frequency": ""},
            "occupation": {"value": "", "source": "interview"},
            "exerciseTolerance": {"value": "", "source": "interview"},
            "livingSituation": {"value": "", "source": "interview"},
            "mobilityAids": {"value": "", "source": "interview"},
            "postOpCaregiverAvailable": {"value": None, "source": "interview", "name": ""},
        },
        "section7_familyHistory": {
            "heartDisease": {"value": None, "source": "interview"},
            "diabetes": {"value": None, "source": "interview"},
            "cancer": {"value": None, "source": "interview", "type": ""},
            "bleedingClottingDisorders": {"value": None, "source": "interview"},
            "anesthesiaComplications": {"value": None, "source": "interview"},
            "malignantHyperthermia": {"value": None, "source": "interview"},
            "suddenCardiacDeath": {"value": None, "source": "interview"},
            "otherHereditary": {"value": "", "source": "interview"},
        },
        "section8_reviewOfSystems": {
            "constitutional": {"value": "", "source": "interview"},
            "cardiovascular": {"value": "", "source": "interview"},
            "respiratory": {"value": "", "source": "interview"},
            "neurological": {"value": "", "source": "interview"},
            "gastrointestinal": {"value": "", "source": "interview"},
            "genitourinary": {"value": "", "source": "interview"},
            "musculoskeletal": {"value": "", "source": "interview"},
            "hematologic": {"value": "", "source": "interview"},
            "endocrine": {"value": "", "source": "interview"},
            "psychiatric": {"value": "", "source": "interview"},
        },
        "section9_functionalAssessment": {
            "functionalCapacityMETs": {"value": "", "source": "interview"},
            "fallRisk": {"value": None, "source": "interview"},
            "cognitiveStatus": {"value": "", "source": "interview"},
            "advanceDirectives": {"value": None, "source": "interview"},
            "healthcareProxy": {"value": None, "source": "interview", "name": ""},
        },
        "section10_dayOfSurgeryReadiness": {
            "transportationArranged": {"value": None, "source": "interview"},
            "responsibleAdultPostOp": {"value": None, "source": "interview", "name": ""},
            "npoStatusUnderstood": {"value": None, "source": "interview"},
            "preOpInstructionsReceived": {"value": None, "source": "prep_document"},
            "medicationsToHold": {"value": [], "source": "prep_document"},
            "medicationsToTakeMorningOf": {"value": [], "source": "prep_document"},
            "labsImagingCompleted": {"value": None, "source": "prep_document", "dates": ""},
            "preOpClearanceLetters": {"value": None, "source": "prep_document"},
        },
        "section11_acknowledgments": {
            "informationAccurate": {"value": None, "source": "patient"},
            "understandsEditRights": {"value": True, "source": "system"},
            "completionDate": {"value": "", "source": "system"},
        },
    }


INTAKE_SECTION_BY_INDEX: Dict[int, str] = {
    1: "section1_demographics",
    2: "section2_surgicalInfo",
    3: "section3_medicalHistory",
    4: "section4_surgicalAnesthesiaHistory",
    5: "section5_medicationsAllergies",
    6: "section6_socialHistory",
    7: "section7_familyHistory",
    8: "section8_reviewOfSystems",
    9: "section9_functionalAssessment",
    10: "section10_dayOfSurgeryReadiness",
    11: "section11_acknowledgments",
}

# Fields in section 10 that must not be overwritten by the intake interview model (prep-backed).
_SECTION10_PREP_ONLY_FIELDS = frozenset(
    {
        "preOpInstructionsReceived",
        "medicationsToHold",
        "medicationsToTakeMorningOf",
        "labsImagingCompleted",
        "preOpClearanceLetters",
    }
)


def _set_field(form_data: Dict[str, Any], section: str, field: str, value: Any, source: str) -> None:
    sec = form_data.get(section) or {}
    fld = sec.get(field)
    if isinstance(fld, dict):
        fld["value"] = value
        fld["source"] = source
        sec[field] = fld
        form_data[section] = sec


def _extract_text(transcript: List[Dict[str, Any]]) -> str:
    lines = []
    for turn in transcript or []:
        role = str(turn.get("role") or "")
        text = str(turn.get("text") or turn.get("content") or "").strip()
        if text:
            lines.append(f"{role}: {text}")
    return "\n".join(lines)


def _detect_red_flags(text: str) -> List[Dict[str, str]]:
    checks: List[Tuple[str, List[str]]] = [
        ("Active chest pain or new shortness of breath", ["chest pain", "shortness of breath", "can't breathe"]),
        ("Fever or active infection in the last 7 days", ["fever", "infection"]),
        ("New or worsening neurological symptoms", ["numb", "weakness", "neurologic", "new headache"]),
        ("Recent positive COVID test (within 7 days)", ["covid", "positive test"]),
        ("Pregnancy or possibility of pregnancy", ["pregnan", "might be pregnant"]),
        ("Active bleeding episode", ["active bleeding", "bleeding now", "bleeding won't stop"]),
        ("Reported allergy to anesthesia with prior anaphylaxis", ["anaphylaxis", "allergy to anesthesia"]),
        ("Malignant hyperthermia history (personal or family)", ["malignant hyperthermia"]),
        ("Patient was told to stop a blood thinner but did NOT stop it", ["didn't stop", "did not stop blood thinner"]),
        ("Patient has NOT arranged transportation or post-op caregiver", ["no ride", "no transportation", "no caregiver"]),
    ]
    lowered = text.lower()
    out: List[Dict[str, str]] = []
    for label, needles in checks:
        if any(n in lowered for n in needles):
            out.append({"flag": label, "source": "interview"})
    return out


def _parse_bool_signal(text: str, yes_words: List[str], no_words: List[str]) -> Any:
    lowered = text.lower()
    if any(w in lowered for w in yes_words):
        return True
    if any(w in lowered for w in no_words):
        return False
    return None


def parseTranscriptToFormData(
    transcript: List[Dict[str, Any]],
    patient_record: Dict[str, Any],
    prep_document: Dict[str, Any],
) -> Dict[str, Any]:
    form_data = copy.deepcopy(_schema())
    conflicts: List[Dict[str, Any]] = []
    not_obtained: List[str] = []
    transcript_text = _extract_text(transcript)
    sd = patient_record.get("structured_data") or {}

    _set_field(form_data, "section1_demographics", "fullLegalName", patient_record.get("name", ""), "patient_record")
    _set_field(form_data, "section1_demographics", "preferredName", patient_record.get("name", ""), "patient_record")
    dob = sd.get("date_of_birth") or sd.get("dob") or ""
    _set_field(form_data, "section1_demographics", "dateOfBirth", dob, "patient_record")
    if dob:
        m = re.search(r"(\d{4})", str(dob))
        if m:
            age = max(0, date.today().year - int(m.group(1)))
            _set_field(form_data, "section1_demographics", "age", age, "calculated")
    _set_field(form_data, "section1_demographics", "sexAssignedAtBirth", sd.get("sex", ""), "patient_record")
    _set_field(form_data, "section1_demographics", "genderIdentity", sd.get("gender", ""), "patient_record")
    _set_field(form_data, "section1_demographics", "ethnicity", sd.get("ethnicity", ""), "patient_record")
    _set_field(form_data, "section1_demographics", "primaryLanguage", sd.get("language", ""), "patient_record")
    _set_field(form_data, "section1_demographics", "address", sd.get("address", ""), "patient_record")
    _set_field(form_data, "section1_demographics", "phonePrimary", patient_record.get("phone", ""), "patient_record")
    _set_field(form_data, "section1_demographics", "email", patient_record.get("email", ""), "patient_record")

    _set_field(form_data, "section2_surgicalInfo", "scheduledProcedure", sd.get("procedure_name", ""), "prep_document")
    _set_field(form_data, "section2_surgicalInfo", "procedureCPTCodes", sd.get("cpt_codes") or prep_document.get("cpt_codes") or [], "doctor")
    _set_field(form_data, "section2_surgicalInfo", "surgicalSite", sd.get("surgical_site", "") or prep_document.get("procedure_site", ""), "prep_document")
    _set_field(form_data, "section2_surgicalInfo", "laterality", sd.get("laterality") or prep_document.get("laterality", ""), "prep_document")
    _set_field(form_data, "section2_surgicalInfo", "surgeonName", sd.get("surgeon_name", "") or prep_document.get("surgeon_name", ""), "prep_document")
    _set_field(form_data, "section2_surgicalInfo", "anesthesiologist", sd.get("anesthesiologist", "") or prep_document.get("anesthesiologist", ""), "prep_document")
    _set_field(form_data, "section2_surgicalInfo", "scheduledDateTime", sd.get("procedure_date", ""), "prep_document")
    _set_field(form_data, "section2_surgicalInfo", "facilityLocation", sd.get("facility", "") or prep_document.get("facility", ""), "prep_document")
    _set_field(form_data, "section2_surgicalInfo", "procedureType", prep_document.get("procedure_type", ""), "prep_document")
    _set_field(form_data, "section2_surgicalInfo", "estimatedDuration", sd.get("estimated_duration", "") or prep_document.get("estimated_duration", ""), "prep_document")
    _set_field(
        form_data,
        "section2_surgicalInfo",
        "preOpDiagnosis",
        prep_document.get("pre_op_diagnosis", "") or sd.get("pre_op_diagnosis", ""),
        "prep_document",
    )

    # Interview signals.
    if transcript_text:
        _set_field(form_data, "section8_reviewOfSystems", "constitutional", transcript_text[:400], "interview")
        _set_field(form_data, "section6_socialHistory", "tobaccoUse", "reported" if "smok" in transcript_text.lower() else "", "interview")
        _set_field(
            form_data,
            "section10_dayOfSurgeryReadiness",
            "transportationArranged",
            _parse_bool_signal(transcript_text, ["ride arranged", "transport arranged", "yes i have a ride"], ["no ride", "no transportation"]),
            "interview",
        )
        _set_field(
            form_data,
            "section10_dayOfSurgeryReadiness",
            "responsibleAdultPostOp",
            _parse_bool_signal(transcript_text, ["someone will stay", "caregiver available", "responsible adult"], ["no caregiver", "nobody can stay"]),
            "interview",
        )
        _set_field(
            form_data,
            "section5_medicationsAllergies",
            "medicationAllergies",
            ["reported allergy"] if "allerg" in transcript_text.lower() else [],
            "interview",
        )

    # Prep document merge for meds and readiness fields.
    _set_field(
        form_data,
        "section10_dayOfSurgeryReadiness",
        "medicationsToHold",
        prep_document.get("medications_to_hold") or [],
        "prep_document",
    )
    _set_field(
        form_data,
        "section10_dayOfSurgeryReadiness",
        "medicationsToTakeMorningOf",
        prep_document.get("medications_to_take_morning_of") or [],
        "prep_document",
    )
    _set_field(
        form_data,
        "section10_dayOfSurgeryReadiness",
        "preOpInstructionsReceived",
        bool(prep_document.get("pre_op_instructions")),
        "prep_document",
    )

    # Simple conflict example: preferredName mention different from record name.
    name_match = re.search(r"(?:my name is|call me)\s+([A-Za-z][A-Za-z\s'-]{1,40})", transcript_text, re.I)
    if name_match:
        spoken_name = name_match.group(1).strip()
        record_name = str(patient_record.get("name") or "").strip()
        if _norm(spoken_name) and _norm(record_name) and _norm(spoken_name) != _norm(record_name):
            _set_field(form_data, "section1_demographics", "preferredName", spoken_name, "interview")
            conflicts.append(
                {
                    "field": "section1_demographics.preferredName",
                    "recordValue": record_name,
                    "patientValue": spoken_name,
                }
            )

    red_flags = _detect_red_flags(transcript_text)
    for section, fields in form_data.items():
        for field_name, payload in fields.items():
            if not isinstance(payload, dict):
                continue
            if payload.get("source") == "doctor":
                continue
            if payload.get("value") in ("", None, []) and payload.get("source") not in ("system", "calculated"):
                payload["source"] = "not_obtained"
                not_obtained.append(f"{section}.{field_name}")

    return {
        "formData": form_data,
        "redFlags": red_flags,
        "conflicts": conflicts,
        "notObtained": not_obtained,
        "confidence": 0.68 if transcript_text else 0.4,
        "parsedTranscriptLength": len(transcript or []),
    }


def apply_health_system_facility_name(form_data: Dict[str, Any], facility_name: str) -> None:
    if not (facility_name or "").strip():
        return
    sec = form_data.get("section2_surgicalInfo") or {}
    fld = sec.get("facilityLocation")
    if not isinstance(fld, dict):
        return
    if str(fld.get("value") or "").strip():
        return
    fld["value"] = facility_name.strip()
    fld["source"] = "health_system"


def merge_intake_ai_patch(section_key: str, field_updates: Dict[str, Any], form_data: Dict[str, Any]) -> None:
    """Deep-merge model output into structured intake fields for one section."""
    if section_key not in (form_data or {}):
        return
    sec = form_data[section_key]
    for fk, incoming in (field_updates or {}).items():
        if fk not in sec:
            continue
        if section_key == "section10_dayOfSurgeryReadiness" and fk in _SECTION10_PREP_ONLY_FIELDS:
            continue
        base = sec[fk]
        if not isinstance(base, dict):
            continue
        if isinstance(incoming, dict):
            for ik, iv in incoming.items():
                if ik == "source":
                    continue
                if ik in base:
                    base[ik] = iv
            if incoming:
                base["source"] = "interview"
        else:
            base["value"] = incoming
            base["source"] = "interview"
        sec[fk] = base


def to_json(data: Dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=True)


def reset_intake_section_for_interview_redo(form_data: Dict[str, Any], section_num: int) -> None:
    """Reset one section to schema defaults for a redo interview; keep prep-backed section 10 fields."""
    key = INTAKE_SECTION_BY_INDEX.get(section_num)
    if not key:
        return
    base_schema = _schema()
    if key not in base_schema:
        return
    fresh = copy.deepcopy(base_schema[key])
    if section_num == 10:
        old = (form_data or {}).get(key) or {}
        for fld in _SECTION10_PREP_ONLY_FIELDS:
            if fld in old and isinstance(old[fld], dict):
                fresh[fld] = copy.deepcopy(old[fld])
    form_data[key] = fresh
