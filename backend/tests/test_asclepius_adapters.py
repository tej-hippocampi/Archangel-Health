"""Format adapters (EHR Ingestion PRD §6) — lab_csv / note_text / fhir_r4 /
hl7v2 parse realistic partner exports into ClinicalCase fragments, and the
single-file ``ingest_real_deid`` path (adapter → timeline → guard) produces a
stored-ready ``real_deid`` case with ZERO surviving date strings."""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests._asclepius as A  # noqa: E402,F401

from asclepius import case_formats as cf  # noqa: E402
from asclepius.adapters import fhir_r4, hl7v2, lab_csv, note_text  # noqa: E402


# ─── lab_csv ──────────────────────────────────────────────────────────────────
_CSV = """patient_key,panel,test_name,loinc,result,units,reference_low,reference_high,abnormal_flag,collection_date
p1,BMP,Sodium,2951-2,112,mmol/L,135,145,LL,2031-03-14
p1,BMP,Potassium,2823-3,5.1,mmol/L,3.5,5.0,H,2031-03-14
p1,BMP,Sodium,2951-2,124,mmol/L,135,145,L,2031-03-19
"""


def test_lab_csv_fuzzy_headers_and_grouping():
    frag = lab_csv.parse(_CSV, specialty="nephrology")
    panels = frag["lab_panels"]
    assert len(panels) == 2                       # grouped by (patient, panel, date)
    day1 = next(p for p in panels if p["collected_at"] == "2031-03-14")
    assert len(day1["results"]) == 2
    na = day1["results"][0]
    assert na["analyte"] == "Sodium" and na["value"] == 112 and na["loinc"] == "2951-2"
    assert na["ref_low"] == 135 and na["flag"] == "LL"
    assert frag["_patient_keys"] == ["p1"]


def test_lab_csv_column_map_override():
    csv_text = "id,weird_test,weird_val\np9,Sodium,133\n"
    frag = lab_csv.parse(csv_text, manifest={"column_map": {
        "patient_key": "id", "analyte": "weird_test", "value": "weird_val"}})
    assert frag["lab_panels"][0]["results"][0]["analyte"] == "Sodium"


def test_lab_csv_unmappable_raises():
    with pytest.raises(lab_csv.LabCsvError):
        lab_csv.parse("a,b,c\n1,2,3\n")


# ─── note_text ────────────────────────────────────────────────────────────────
def test_note_text_type_from_filename_and_safe_role():
    frag = note_text.parse("Pt improving.", specialty="nephrology",
                           manifest={"filename": "discharge_summary.txt"})
    n = frag["notes"][0]
    assert n["note_type"] == "Discharge" and n["author_role"] == "nephrology"


def test_note_text_never_carries_a_person_as_role():
    frag = note_text.parse("Seen and examined.",
                           specialty="nephrology",
                           manifest={"author_role": "Dr. Jane Doe"})
    assert frag["notes"][0]["author_role"] == "nephrology"   # generalized, never a person


# ─── fhir_r4 ──────────────────────────────────────────────────────────────────
def _bundle():
    note_b64 = base64.b64encode(b"Admitted 2031-03-14 with confusion.").decode()
    return {
        "resourceType": "Bundle", "type": "collection",
        "entry": [
            {"resource": {"resourceType": "Patient", "id": "pat-1", "gender": "male",
                          "birthDate": "1957-06-02",
                          "name": [{"family": "SHOULD-NEVER-BE-READ"}]}},
            {"resource": {"resourceType": "Observation", "status": "final",
                          "category": [{"coding": [{"code": "laboratory"}]}],
                          "code": {"text": "Sodium", "coding": [{"system": "http://loinc.org", "code": "2951-2"}]},
                          "valueQuantity": {"value": 112, "unit": "mmol/L"},
                          "referenceRange": [{"low": {"value": 135}, "high": {"value": 145}}],
                          "interpretation": [{"coding": [{"code": "LL"}]}],
                          "effectiveDateTime": "2031-03-14T08:00:00Z"}},
            {"resource": {"resourceType": "Observation", "status": "final",
                          "category": [{"coding": [{"code": "vital-signs"}]}],
                          "code": {"text": "Heart rate"},
                          "valueQuantity": {"value": 96, "unit": "bpm"},
                          "effectiveDateTime": "2031-03-19T08:00:00Z"}},
            {"resource": {"resourceType": "Condition", "code": {"text": "CKD stage 3"},
                          "onsetDateTime": "2027-01-15"}},
            {"resource": {"resourceType": "MedicationStatement",
                          "medicationCodeableConcept": {"text": "Hydrochlorothiazide"},
                          "dosage": [{"doseAndRate": [{"doseQuantity": {"value": 25, "unit": "mg"}}],
                                       "route": {"text": "oral"}}]}},
            {"resource": {"resourceType": "DocumentReference", "type": {"text": "Consult"},
                          "content": [{"attachment": {"contentType": "text/plain", "data": note_b64}}]}},
            {"resource": {"resourceType": "ImagingStudy", "id": "img-1"}},
        ],
    }


def test_fhir_bundle_maps_all_sections():
    frag = fhir_r4.parse(json.dumps(_bundle()), specialty="nephrology")
    assert frag["demographics"]["sex"] == "M"
    assert frag["demographics"]["age_band"] == "70-79"       # age at encounter, banded
    labs = frag["lab_panels"][0]["results"][0]
    assert labs["analyte"] == "Sodium" and labs["loinc"] == "2951-2" and labs["flag"] == "LL"
    assert frag["vitals"]["Heart rate"] == "96 bpm"
    assert frag["problem_list"][0]["condition"] == "CKD stage 3"
    assert frag["medications"][0]["drug"] == "Hydrochlorothiazide"
    assert frag["medications"][0]["dose"] == "25 mg"
    assert "Admitted" in frag["notes"][0]["text"]
    assert frag["_imaging_skipped"] == 1                     # imaging counted, never parsed
    blob = json.dumps(frag)
    assert "SHOULD-NEVER-BE-READ" not in blob                # names never copied
    assert "1957" not in blob                                # birthDate never carried


def test_fhir_not_a_bundle_raises():
    with pytest.raises(fhir_r4.FhirParseError):
        fhir_r4.parse('{"resourceType": "Patient"}')


# ─── hl7v2 ────────────────────────────────────────────────────────────────────
_HL7 = "\r".join([
    "MSH|^~\\&|LAB|HOSP|EHR|HOSP|203103190830||ORU^R01|MSG001|P|2.5.1",
    "PID|1||MRN12345^^^HOSP||DOE^JANE||19570602|F|||123 Main St^^Springfield",
    "OBR|1||ORD1|24323-8^Comprehensive metabolic panel^LN|||203103190800",
    "OBX|1|NM|2951-2^Sodium^LN||124|mmol/L|135-145|L|||F",
    "OBX|2|NM|2823-3^Potassium^LN||5.1|mmol/L|3.5-5.0|H|||F",
    "NTE|1||Hemolyzed specimen; repeat advised.",
])


def test_hl7_oru_maps_panel_results_and_demographics():
    frag = hl7v2.parse(_HL7, specialty="nephrology")
    panel = frag["lab_panels"][0]
    assert panel["panel"] == "Comprehensive metabolic panel"
    assert panel["collected_at"] == "203103190800"
    na = panel["results"][0]
    assert na["analyte"] == "Sodium" and na["value"] == 124
    assert na["loinc"] == "2951-2" and na["ref_low"] == 135 and na["flag"] == "L"
    assert frag["demographics"]["sex"] == "F"
    assert frag["demographics"]["age_band"] == "70-79"
    assert "Hemolyzed" in frag["notes"][0]["text"]
    blob = json.dumps(frag)
    # PID-5 name / PID-3 MRN / PID-11 address: never carried, not even scrubbed.
    assert "DOE" not in blob and "MRN12345" not in blob and "Main St" not in blob
    assert "19570602" not in blob


def test_hl7_junk_raises():
    with pytest.raises(hl7v2.Hl7ParseError):
        hl7v2.parse("this is not hl7")


# ─── end-to-end: ingest_real_deid (adapter → timeline → guard) ────────────────
def test_ingest_real_deid_csv_end_to_end():
    case = cf.ingest_real_deid(_CSV, "lab_csv", specialty="nephrology")
    assert case["case_source"] == "real_deid"
    offs = sorted(lp["collected_offset_days"] for lp in case["lab_panels"])
    assert offs == [-5, 0]                                   # intervals preserved
    assert "2031" not in json.dumps(case)                    # calendar destroyed


def test_ingest_real_deid_fhir_end_to_end():
    case = cf.ingest_real_deid(json.dumps(_bundle()), "fhir_r4", specialty="nephrology")
    assert case["case_source"] == "real_deid"
    assert case["demographics"]["age_band"] == "70-79"
    assert "[day -5]" in case["notes"][0]["text"]            # note date rewritten
    assert "2031" not in json.dumps(case)


def test_ingest_real_deid_hl7_end_to_end():
    case = cf.ingest_real_deid(_HL7, "hl7v2", specialty="nephrology")
    assert case["case_source"] == "real_deid"
    assert case["lab_panels"][0]["collected_offset_days"] == 0
    assert "203103" not in json.dumps(case)


def test_ingest_planted_identifier_still_rejected():
    """A residual identifier the partner missed (a phone number in a note) must
    still be caught by the final guard — the adapters do not weaken it."""
    csv_with_note_case = {"column_map": None}
    note = "Call the family at 555-123-4567 to discuss."
    with pytest.raises(cf.CaseIngestError):
        cf.ingest_real_deid(note, "note_text", specialty="nephrology",
                            manifest={"note_type": "Progress"})  # phone survives → guard rejects
        # (note_text has no dates to normalize; the deidentify guard is the catcher)