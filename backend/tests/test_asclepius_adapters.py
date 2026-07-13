"""Clinical-data format adapters (real_deid ingestion seam).

One happy-path parse per adapter asserting the mapped fields, plus one
CaseIngestError case for junk input. Fixtures are small and inline.

PHI-safety assertions: demographics never carry a raw DOB / exact age (age_band
only), and note author_role is a generalized role — never a person's name.
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asclepius.adapters import ccda, fhir_r4, hl7v2, lab_csv, note_text  # noqa: E402
from asclepius.case_formats import CaseIngestError  # noqa: E402


def _birthdate_for_age(age: int) -> str:
    today = datetime.date.today()
    return (today.replace(year=today.year - age)).isoformat()


# ─────────────────────────── lab_csv ───────────────────────────
def test_lab_csv_happy_path():
    raw = (
        "patient_key,panel,test,loinc,result,units,low,high,abnormal_flag,collected\n"
        "P1,BMP,Sodium,2951-2,146,mmol/L,135,145,H,2025-03-01\n"
        "P1,BMP,Potassium,2823-3,4.1,mmol/L,3.5,5.1,,2025-03-01\n"
        "P1,BMP,,,,,,,,2025-03-01\n"  # missing analyte/value -> skipped
    )
    frag = lab_csv.parse(raw, specialty="nephrology")
    assert frag["specialty"] == "nephrology"
    assert frag["patient_key"] == "P1"
    panels = frag["lab_panels"]
    assert len(panels) == 1
    p = panels[0]
    assert p["panel"] == "BMP"
    assert p["collected_at"] == "2025-03-01"
    assert len(p["results"]) == 2  # incomplete row skipped
    na = p["results"][0]
    assert na["analyte"] == "Sodium"
    assert na["value"] == 146
    assert na["unit"] == "mmol/L"
    assert na["ref_low"] == 135 and na["ref_high"] == 145
    assert na["flag"] == "H"
    assert na["loinc"] == "2951-2"


def test_lab_csv_tab_delimited():
    raw = "name\tvalue\tunit\ncreatinine\t2.3\tmg/dL\n"
    frag = lab_csv.parse(raw)
    assert frag["lab_panels"][0]["results"][0]["analyte"] == "creatinine"
    assert frag["lab_panels"][0]["results"][0]["value"] == 2.3


def test_lab_csv_junk_raises():
    with pytest.raises(CaseIngestError):
        lab_csv.parse("")
    with pytest.raises(CaseIngestError):
        lab_csv.parse("just some prose with no columns at all")


# ─────────────────────────── note_text ───────────────────────────
def test_note_text_happy_path():
    raw = (
        "Nephrology Consult Note\n"
        "Patient seen for AKI. Cr trending up. Plan: hydration, hold NSAIDs.\n"
    )
    frag = note_text.parse(raw, specialty="nephrology")
    assert frag["patient_key"] is None
    note = frag["notes"][0]
    assert note["note_type"] == "Consult"
    assert note["author_role"] == "nephrology"  # generalized, not a name
    assert "AKI" in note["text"]


def test_note_text_rtf_stripped():
    raw = r"{\rtf1\ansi\deff0 {\b Progress Note} Patient stable overnight.}"
    frag = note_text.parse(raw)
    note = frag["notes"][0]
    assert "\\rtf" not in note["text"]
    assert "Patient stable" in note["text"]


def test_note_text_default_role_is_generalized():
    frag = note_text.parse("Some clinical narrative without a department header.")
    assert frag["notes"][0]["author_role"] == "clinician"


def test_note_text_blank_raises():
    with pytest.raises(CaseIngestError):
        note_text.parse("   \n  \n")


# ─────────────────────────── fhir_r4 ───────────────────────────
def test_fhir_r4_bundle_happy_path():
    import json

    bundle = {
        "resourceType": "Bundle",
        "entry": [
            {"resource": {
                "resourceType": "Patient",
                "id": "pat-123",
                "gender": "female",
                "birthDate": _birthdate_for_age(74),
            }},
            {"resource": {
                "resourceType": "Observation",
                "category": [{"coding": [{"code": "laboratory"}]}],
                "code": {"text": "Creatinine", "coding": [{"system": "http://loinc.org", "code": "2160-0", "display": "Creatinine"}]},
                "valueQuantity": {"value": 2.4, "unit": "mg/dL"},
                "referenceRange": [{"low": {"value": 0.6}, "high": {"value": 1.3}}],
                "interpretation": [{"coding": [{"code": "H"}]}],
                "effectiveDateTime": "2025-03-01",
            }},
            {"resource": {
                "resourceType": "Condition",
                "code": {"text": "Chronic kidney disease"},
                "onsetDateTime": "2019",
            }},
            {"resource": {
                "resourceType": "MedicationStatement",
                "medicationCodeableConcept": {"text": "Lisinopril"},
                "dosage": [{"route": {"text": "oral"}, "text": "daily",
                            "doseAndRate": [{"doseQuantity": {"value": 10, "unit": "mg"}}]}],
            }},
        ],
    }
    frag = fhir_r4.parse(json.dumps(bundle), specialty="nephrology")
    assert frag["patient_key"] == "pat-123"
    # Age band only, never a raw DOB / exact age.
    assert frag["demographics"]["age_band"] == "70-79"
    assert frag["demographics"]["sex"] == "female"
    assert "birthDate" not in frag["demographics"]
    lab = frag["lab_panels"][0]
    assert lab["collected_at"] == "2025-03-01"
    r = lab["results"][0]
    assert r["analyte"] == "Creatinine"
    assert r["loinc"] == "2160-0"
    assert r["value"] == 2.4 and r["unit"] == "mg/dL"
    assert r["ref_low"] == 0.6 and r["ref_high"] == 1.3
    assert r["flag"] == "H"
    assert frag["problem_list"][0]["condition"] == "Chronic kidney disease"
    med = frag["medications"][0]
    assert med["drug"] == "Lisinopril"
    assert med["route"] == "oral"
    assert "10" in med["dose"]


def test_fhir_r4_single_resource():
    import json

    obs = {
        "resourceType": "Observation",
        "category": [{"coding": [{"code": "vital-signs"}]}],
        "code": {"text": "Heart rate"},
        "valueQuantity": {"value": 88, "unit": "/min"},
    }
    frag = fhir_r4.parse(json.dumps(obs))
    assert frag["vitals"]["Heart rate"] == "88 /min"


def test_fhir_r4_junk_raises():
    with pytest.raises(CaseIngestError):
        fhir_r4.parse("not json at all {")
    with pytest.raises(CaseIngestError):
        fhir_r4.parse('{"foo": "bar"}')  # valid JSON, no resource


# ─────────────────────────── hl7v2 ───────────────────────────
def test_hl7v2_happy_path():
    dob = _birthdate_for_age(84).replace("-", "")  # YYYYMMDD
    msg = "\n".join([
        r"MSH|^~\&|LAB|HOSP|EHR|HOSP|202503011200||ORU^R01|MSG1|P|2.5",
        f"PID|1||MRN999^^^HOSP||DOE^JANE||{dob}|F",
        "OBR|1||ORD1|BMP^Basic Metabolic Panel^L|||20250301",
        "OBX|1|NM|2160-0^Creatinine^LN||2.4|mg/dL|0.6-1.3|H|||F",
        "OBX|2|NM|2951-2^Sodium^LN||146|mmol/L|135-145|H|||F",
        "NTE|1||Patient notified of results.",
    ])
    frag = hl7v2.parse(msg, specialty="nephrology")
    # Never emit MRN or name; patient_key stays None.
    assert frag["patient_key"] is None
    assert frag["demographics"]["age_band"] == "80-89"
    assert frag["demographics"]["sex"] == "female"
    panel = frag["lab_panels"][0]
    assert panel["panel"] == "Basic Metabolic Panel"
    assert panel["collected_at"] == "20250301"
    r = panel["results"][0]
    assert r["analyte"] == "Creatinine"
    assert r["loinc"] == "2160-0"
    assert r["value"] == 2.4 and r["unit"] == "mg/dL"
    assert r["ref_low"] == 0.6 and r["ref_high"] == 1.3
    assert r["flag"] == "H"
    assert "notified" in frag["notes"][0]["text"]
    # PHI check: neither MRN nor the surname leaked anywhere.
    import json as _json
    blob = _json.dumps(frag)
    assert "MRN999" not in blob and "DOE" not in blob


def test_hl7v2_junk_raises():
    with pytest.raises(CaseIngestError):
        hl7v2.parse("this is not an HL7 message")


# ─────────────────────────── ccda ───────────────────────────
CCDA_XML = """<?xml version="1.0"?>
<ClinicalDocument xmlns="urn:hl7-org:v3">
  <recordTarget><patientRole><patient>
    <administrativeGenderCode code="M" displayName="Male"/>
    <birthTime value="{dob}"/>
  </patient></patientRole></recordTarget>
  <component><structuredBody>
    <component><section>
      <text>Assessment and plan: patient with worsening renal function, monitor closely.</text>
      <entry><observation>
        <code code="2160-0" displayName="Creatinine"/>
        <value unit="mg/dL" value="2.4"/>
      </observation></entry>
      <entry><observation>
        <code code="44054006" displayName="Diabetes"/>
        <value displayName="Type 2 diabetes mellitus"/>
      </observation></entry>
    </section></component>
    <component><section>
      <entry><substanceAdministration>
        <consumable><manufacturedProduct><manufacturedMaterial>
          <code displayName="Metformin"/>
        </manufacturedMaterial></manufacturedProduct></consumable>
        <doseQuantity value="500" unit="mg"/>
        <routeCode displayName="oral"/>
      </substanceAdministration></entry>
    </section></component>
  </structuredBody></component>
</ClinicalDocument>"""


def test_ccda_happy_path():
    xml = CCDA_XML.format(dob=_birthdate_for_age(64).replace("-", ""))
    frag = ccda.parse(xml, specialty="nephrology")
    assert frag["demographics"]["age_band"] == "60-69"
    assert frag["demographics"]["sex"] == "male"
    assert "birthTime" not in str(frag["demographics"])
    lab = frag["lab_panels"][0]["results"][0]
    assert lab["analyte"] == "Creatinine"
    assert lab["value"] == 2.4 and lab["unit"] == "mg/dL"
    conditions = [p["condition"] for p in frag["problem_list"]]
    assert "Type 2 diabetes mellitus" in conditions
    assert frag["medications"][0]["drug"] == "Metformin"
    assert "500" in frag["medications"][0]["dose"]
    assert any("renal function" in n["text"] for n in frag["notes"])


def test_ccda_rejects_doctype():
    xml = '<!DOCTYPE foo [<!ENTITY x "y">]><ClinicalDocument/>'
    with pytest.raises(CaseIngestError):
        ccda.parse(xml)


def test_ccda_junk_raises():
    with pytest.raises(CaseIngestError):
        ccda.parse("<not-valid-xml <<<")
