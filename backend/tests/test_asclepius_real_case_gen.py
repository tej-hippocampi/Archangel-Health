"""Real hospital record → tagged V4 cases (Real-Case Generation PRD).

Every assertion here traces to a defect the PRD measured against a real 14-month
partner export, so the fixtures are built to the shape that record actually has
rather than to a convenient synthetic one:

  * §2.1 a GCS of 10/15 parsed as October 15th and quarantined every neuro/ICU
    record permanently — a score can never be resolved into a date by a human;
  * §2.1(ext) the record is DAY-FIRST, so "04/03/2022" was silently read as
    3 April and 321 other tokens failed to parse at all;
  * §2.2 dates in ``medications[].drug`` were never rewritten, so the case
    cleared the timeline gate and died at the un-overridable ``deidentify()``;
  * §2.3 one patient split into three ingest cases, two quarantined, and the
    upload still rendered green;
  * §2.4 ``age_band`` was NULL on every real record;
  * §3.x there was no case CONSTRUCTION at all — no question, no bucket, no
    failure mode, and ``difficulty`` was the literal string "hard".
"""

from __future__ import annotations

import io
import json
import sys
import zipfile
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import ingestion as asc_ingestion  # noqa: E402
from asclepius import real_cases  # noqa: E402
from asclepius import timeline as asc_timeline  # noqa: E402
from asclepius.adapters import hl7v2, lab_csv  # noqa: E402

client = TestClient(A.app)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    A.fresh_store()
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(tmp_path / "ingest"))
    yield


def _store():
    from asclepius.store import get_store
    return get_store()


def _admin_h():
    return A.headers_for(A.make_user(_store(), role="admin"))


# ═════════════════════════════════════════════════════════════════════════════
# §2.1 — a clinical ratio is not a date
# ═════════════════════════════════════════════════════════════════════════════
_INDEX = date(2026, 1, 31)


@pytest.mark.parametrize("text", [
    "NEURO: - GCS 10/15 (E4 V2 M4) - Pupils equal",
    "GCS E4 M5 V3 = 12/15 (alt read E4 M6 V3)",
    "O/E: - GCS: M4 V1 E3 -> 9/15 - Pupils: PERL",
    "GCS: E3 M5 V2-3 (10-11/15) under Dormicum effect",
    "Orientation: *2 / *2; GCS 12/15 / 12/15",
    "Right upper limb power 3/5, left 5/5",
    "Apgar 8/10 at one minute",
    "pain score 7/10 on arrival",
])
def test_clinical_scores_are_never_unresolved_date_tokens(text):
    _out, _n, unresolved = asc_timeline.rewrite_note_dates(text, _INDEX)
    assert unresolved == [], f"{text!r} produced {unresolved}"


def test_an_ambiguous_partial_date_still_quarantines():
    """The exemption is specific, not a loosening. Without it the whole
    unresolved-token rule — never guess at an ambiguous date — would be gone."""
    _out, _n, unresolved = asc_timeline.rewrite_note_dates("Symptoms since 3/14.", _INDEX)
    assert unresolved == ["•/••"]


def test_a_bare_ratio_with_no_scale_cue_is_still_treated_as_a_date():
    """"3/15" next to nothing is an ambiguous March date, not a score. Silently
    exempting every /15 would turn a recoverable quarantine into a missed date."""
    _out, _n, unresolved = asc_timeline.rewrite_note_dates("Follow-up 3/15 in clinic.", _INDEX)
    assert unresolved == ["•/••"]


def test_the_scrub_recheck_applies_the_same_exemption():
    """A quarantine the rewriter would not have raised must not be re-raised by
    the scrub re-check, or a scrubbed case could never be released."""
    assert asc_timeline.datelike_leftovers_in_text("GCS 10/15 on arrival") == []


# ═════════════════════════════════════════════════════════════════════════════
# §2.1 (extended) — day-first records
# ═════════════════════════════════════════════════════════════════════════════
def test_day_first_order_is_inferred_from_unambiguous_tokens():
    order, evidence = asc_timeline.infer_date_order(
        ["Admitted 28/01/2026", "Reviewed 30/01/2026", "Med form 04/03/2022"])
    assert order == asc_timeline.DATE_ORDER_DMY
    assert evidence == {"day_first": 2, "month_first": 0}


def test_month_first_records_are_unchanged():
    order, evidence = asc_timeline.infer_date_order(["Admitted 3/14/2031"])
    assert order == asc_timeline.DATE_ORDER_MDY and evidence["month_first"] == 1


def test_no_evidence_keeps_the_documented_month_first_default():
    order, _ = asc_timeline.infer_date_order(["Seen 1/2/2026"])
    assert order == asc_timeline.DATE_ORDER_MDY


def test_a_day_first_token_resolves_to_the_right_day():
    out, n, unresolved = asc_timeline.rewrite_note_dates(
        "Cannulated 28/01/2026; form dated 30/01/2026.", _INDEX,
        asc_timeline.DATE_ORDER_DMY)
    assert n == 2 and unresolved == []
    assert "[day -3]" in out and "[day -1]" in out


def test_normalize_timeline_reports_the_inferred_order():
    frags = {
        "lab_panels": [{"panel": "BMP", "collected_at": "2026-01-31",
                        "results": [{"analyte": "Na", "value": 136}]}],
        "notes": [{"note_type": "Progress", "author_role": "icu",
                   "text": "Admitted 28/01/2026 with ALOC. GCS 10/15."}],
    }
    case, report = asc_timeline.normalize_timeline(frags)
    assert report["date_order"] == asc_timeline.DATE_ORDER_DMY
    assert report["unresolved"] == []
    assert "[day -3]" in case["notes"][0]["text"]
    assert "10/15" in case["notes"][0]["text"]          # the score survives intact


# ═════════════════════════════════════════════════════════════════════════════
# §2.2 — medications and problems are date-rewritten too
# ═════════════════════════════════════════════════════════════════════════════
def test_dates_in_medication_and_problem_text_are_rewritten():
    frags = {
        "lab_panels": [{"panel": "BMP", "collected_at": "2026-01-31",
                        "results": [{"analyte": "Na", "value": 136}]}],
        "medications": [{"drug": "Medication Form, date column 04/03/2026"}],
        "problem_list": [{"condition": "Seizure disorder, first noted 28/01/2026"}],
    }
    case, report = asc_timeline.normalize_timeline(frags)
    assert report["unresolved"] == []
    assert "2026" not in case["medications"][0]["drug"]
    assert "2026" not in case["problem_list"][0]["condition"]

    # …and the hard guard, which the admin override cannot bypass, now passes.
    from asclepius import case_formats as cf
    cf.deidentify({**case, "case_source": "real_deid"})


# ═════════════════════════════════════════════════════════════════════════════
# §2.3 — one patient is one case; the upload reports the WORST status
# ═════════════════════════════════════════════════════════════════════════════
def test_one_patient_across_three_formats_becomes_one_case():
    per_patient = {"patient-4-patient": [{"a": 1}], "hl7-abc123": [{"b": 2}],
                   "default": [{"c": 3}]}
    sources = {"patient-4-patient": "fhir_r4", "hl7-abc123": "hl7v2", "default": "default"}
    out, report = asc_ingestion.unify_patient_keys(per_patient, sources)
    assert list(out) == ["patient-4-patient"]
    assert len(out["patient-4-patient"]) == 3
    assert report["unified"] is True and report["into_source"] == "fhir_r4"
    # Only opaque keys are ever reported — a raw key may be an MRN.
    assert all(k.startswith("pk-") for k in report["merged"])


def test_two_patients_from_one_source_are_never_merged():
    per_patient = {"pat-a": [{"a": 1}], "pat-b": [{"b": 2}]}
    sources = {"pat-a": "fhir_r4", "pat-b": "fhir_r4"}
    out, report = asc_ingestion.unify_patient_keys(per_patient, sources)
    assert set(out) == {"pat-a", "pat-b"}
    assert report["unified"] is False


def test_a_declared_manifest_key_short_circuits_unification():
    per_patient = {"p1": [{"a": 1}]}
    out, report = asc_ingestion.unify_patient_keys(per_patient, {"p1": "manifest"},
                                                   manifest={"patient_key": "p1"})
    assert out == per_patient and report is None


def test_upload_status_is_the_worst_case_status_not_the_best():
    """Green over a 2-of-3 quarantine is how the real failure stayed hidden."""
    assert asc_ingestion._upload_status_from_cases(1, 2, 0) == "quarantined"
    assert asc_ingestion._upload_status_from_cases(1, 0, 1) == "needs_review"
    assert asc_ingestion._upload_status_from_cases(2, 0, 0) == "ingested"
    assert asc_ingestion._upload_status_from_cases(0, 0, 0) == "rejected"


# ═════════════════════════════════════════════════════════════════════════════
# §2.4 / §3.5 — adapters
# ═════════════════════════════════════════════════════════════════════════════
def test_the_patient_age_extension_yields_an_age_band():
    from asclepius.adapters import fhir_r4
    bundle = json.dumps({
        "resourceType": "Bundle", "type": "collection",
        "entry": [{"resource": {
            "resourceType": "Patient", "id": "patient-4-patient", "gender": "male",
            "extension": [{"url": "http://hl7.org/fhir/StructureDefinition/patient-age",
                           "valueString": "45Y"}]}}],
    })
    frag = fhir_r4.parse(bundle)
    assert frag["demographics"]["age_band"] == "40-49"
    assert frag["demographics"]["sex"] == "M"


def test_the_safe_harbor_ninety_plus_collapse_survives_the_extension_path():
    from asclepius.adapters import fhir_r4
    bundle = json.dumps({
        "resourceType": "Bundle", "type": "collection",
        "entry": [{"resource": {
            "resourceType": "Patient", "id": "p", "extension": [
                {"url": "http://hl7.org/fhir/StructureDefinition/patient-age",
                 "valueString": "94Y"}]}}],
    })
    assert fhir_r4.parse(bundle)["demographics"]["age_band"] == "90+"


def test_hl7_escape_sequences_are_decoded():
    msg = ("MSH|^~\\&|LAB|DEID|EHR|DEID|20260131000000||ORU^R01|P1|P|2.5\r"
           "OBR|1||P1^DEID|LAB^Laboratory Panel^L|||20260131000000\r"
           "OBX|1|NM|789-8^Erythrocytes^LN||4.11|10\\S\\12/L|4.0 - 5.2|N|||F\r")
    frag = hl7v2.parse(msg)
    result = frag["lab_panels"][0]["results"][0]
    assert result["unit"] == "10^12/L"
    assert "\\" not in result["unit"]


def test_the_partner_csv_date_and_range_columns_are_mapped():
    """``service_date``/``reference_range`` matched no alias, so all seven panels
    collapsed to day 0 and every reference range was dropped — silently."""
    csv_text = (
        "service_date,panel,test_name,loinc,value,unit,reference_range,flag\n"
        "2024-12-19,LFT,Bilirubin Direct,1968-7,0.2,mg/dL,Up to 0.4,\n"
        "2026-01-31,BMP,Magnesium,,2.01,mg/dl,1.9 - 2.5,\n"
    )
    frag = lab_csv.parse(csv_text)
    assert {p["collected_at"] for p in frag["lab_panels"]} == {"2024-12-19", "2026-01-31"}
    ranges = {r["analyte"]: (r.get("ref_low"), r.get("ref_high"))
              for p in frag["lab_panels"] for r in p["results"]}
    assert ranges["Bilirubin Direct"] == (None, 0.4)
    assert ranges["Magnesium"] == (1.9, 2.5)
    assert "_adapter_warnings" not in frag


def test_an_unmatched_date_column_warns_loudly_instead_of_defaulting_to_day_zero():
    frag = lab_csv.parse("when_drawn,test_name,value\nyesterday,Sodium,136\n")
    assert frag["_adapter_warnings"], "a lost timeline must not ingest silently"
    assert "collection-date column" in frag["_adapter_warnings"][0]


def test_prose_reference_ranges_are_refused_rather_than_guessed():
    assert lab_csv.split_reference_range(
        "Male: 270; Female: 240; Children < 5 Yr: 60-321") == (None, None)
    assert lab_csv.split_reference_range("< 7.90") == (None, 7.9)
    assert lab_csv.split_reference_range("10 to 50") == (10, 50)


def test_lab_panels_are_reconstructed_from_report_membership():
    """Grouping by date alone named a panel after whichever report was last that
    day — producing an "RFT/renal" panel containing Magnesium, Calcium and CRP."""
    from asclepius.adapters import fhir_r4

    def obs(oid, name, value):
        return {"resource": {
            "resourceType": "Observation", "id": oid, "status": "final",
            "category": [{"coding": [{"code": "laboratory"}]}],
            "code": {"text": name},
            "valueQuantity": {"value": value, "unit": "mg/dL"},
            "effectiveDateTime": "2026-01-31T00:00:00Z"}}

    bundle = json.dumps({"resourceType": "Bundle", "type": "collection", "entry": [
        {"resource": {"resourceType": "DiagnosticReport", "id": "dr-renal",
                      "code": {"text": "RFT/renal (source image 6)"},
                      "effectiveDateTime": "2026-01-31T00:00:00Z",
                      "result": [{"reference": "Observation/o-creat"}]}},
        {"resource": {"resourceType": "DiagnosticReport", "id": "dr-chem",
                      "code": {"text": "electrolytes (source image 7)"},
                      "effectiveDateTime": "2026-01-31T00:00:00Z",
                      "result": [{"reference": "Observation/o-mg"},
                                 {"reference": "Observation/o-ca"}]}},
        obs("o-creat", "Creatinine", 0.668),
        obs("o-mg", "Magnesium", 2.01),
        obs("o-ca", "Calcium", 8.14),
    ]})
    panels = {p["panel"]: [r["analyte"] for r in p["results"]]
              for p in fhir_r4.parse(bundle)["lab_panels"]}
    assert panels["RFT/renal"] == ["Creatinine"]
    assert sorted(panels["electrolytes"]) == ["Calcium", "Magnesium"]
    # The OCR provenance parenthetical is not part of the panel name — left in,
    # every panel in the chart is unique and no trend ever lines up.
    assert not any("source image" in name for name in panels)


# ═════════════════════════════════════════════════════════════════════════════
# §3.1 / §3.2 — segmentation and the decision point
# ═════════════════════════════════════════════════════════════════════════════
def _chart() -> dict:
    """A three-encounter chart shaped like the real one: two clustered draws far
    back, an isolated visit, then an admission that ends in a commitment."""
    def panel(off, na, flag=""):
        return {"panel": "BMP", "collected_offset_days": off,
                "results": [{"analyte": "Sodium", "value": na, "unit": "mmol/L",
                             "ref_low": 135, "ref_high": 145, "flag": flag},
                            {"analyte": "Creatinine", "value": 2.4, "unit": "mg/dL",
                             "ref_low": 0.7, "ref_high": 1.2, "flag": "H"}]}
    return {
        "case_source": "real_deid", "specialty": "general",
        "demographics": {"age_band": "40-49", "sex": "M"},
        "problem_list": [
            {"condition": "Chronic kidney disease", "since": "2019",
             "collected_offset_days": -60},
            {"condition": "Hypertension", "since": "2016", "collected_offset_days": -60},
            {"condition": "Hyponatremia", "since": "2026", "collected_offset_days": -2},
        ],
        "medications": [
            {"drug": "Tab Furosemide 40 mg x OD", "collected_offset_days": -60},
            {"drug": "Orders:", "collected_offset_days": -60},
            {"drug": "Inj Tolvaptan 15 mg I/V x OD", "collected_offset_days": -1},
        ],
        "lab_panels": [panel(-60, 138), panel(-59, 134, "L"),
                       panel(-30, 130, "L"), panel(-2, 118, "LL"), panel(-1, 121, "LL")],
        "notes": [
            {"note_type": "Progress", "author_role": "nephrology",
             "collected_offset_days": -60,
             "text": "Routine review. " + "Stable on current therapy. " * 12},
            {"note_type": "Progress", "author_role": "nephrology",
             "collected_offset_days": -30,
             "text": "Outpatient draw. " + "Sodium drifting down; advised fluid review. " * 8},
            {"note_type": "Consult", "author_role": "icu", "collected_offset_days": -2,
             "text": "Admitted with confusion. GCS 12/15. " + "Serum osmolality low. " * 12},
            {"note_type": "Progress", "author_role": "icu", "collected_offset_days": -1,
             "text": "Started on tolvaptan after the osmolality result confirmed SIADH. "
                     * 6},
        ],
        "studies": [], "vitals": {},
    }


def test_a_long_chart_segments_into_encounters_not_one_blob():
    encounters = real_cases.segment_longitudinal_record(_chart())
    assert [e["encounter_span"] if "encounter_span" in e
            else (e["start_offset"], e["end_offset"]) for e in encounters] == [
        (-60, -59), (-30, -30), (-2, -1)]


def test_problem_onset_dates_do_not_invent_ancient_encounters():
    """Clustering on a problem's first-noted date manufactured ten-year-old
    "encounters" containing one problem and no labs."""
    chart = _chart()
    chart["problem_list"].append(
        {"condition": "Stroke", "since": "2016", "collected_offset_days": -3650})
    assert len(real_cases.segment_longitudinal_record(chart)) == 3


def test_the_index_event_is_before_the_resolving_action_not_the_last_day():
    chart = _chart()
    admission = real_cases.segment_longitudinal_record(chart)[-1]
    index, rationale = real_cases.select_decision_point(chart, admission)
    assert index == -2, "the case must ask for the call, not summarise it"
    assert rationale["resolving_offset"] == -1
    assert rationale["signal"] in ("therapeutic_commitment", "definitive_result")


def test_a_single_day_encounter_still_has_a_decision_point():
    chart = _chart()
    isolated = real_cases.segment_longitudinal_record(chart)[1]
    index, rationale = real_cases.select_decision_point(chart, isolated)
    assert index == -30 and rationale["signal"] == "next_encounter"


def test_an_encounter_with_no_future_has_no_decision_point():
    chart = {"lab_panels": [{"panel": "BMP", "collected_offset_days": 0, "results": []}]}
    enc = real_cases.segment_longitudinal_record(chart)[0]
    index, rationale = real_cases.select_decision_point(chart, enc)
    assert index is None and "nothing recorded afterwards" in rationale["reason"]


def test_everything_after_the_index_event_is_held_out():
    chart = _chart()
    admission = real_cases.segment_longitudinal_record(chart)[-1]
    visible, held_out, _stats = real_cases.build_encounter_case(chart, admission, -2)

    assert all(p["collected_offset_days"] <= 0 for p in visible["lab_panels"])
    assert all(m["collected_offset_days"] <= 0 for m in visible["medications"])
    # The drug that names the diagnosis is sealed, not shown.
    assert not any("olvaptan" in m["drug"] for m in visible["medications"])
    assert any("olvaptan" in d for d in held_out["drugs_started_after"])
    # …and the case's internal answer key is built from it.
    assert "olvaptan" in visible["ground_truth"]["answer"]


def test_offsets_are_rebased_so_day_zero_is_the_decision_point():
    chart = _chart()
    admission = real_cases.segment_longitudinal_record(chart)[-1]
    visible, _held, _stats = real_cases.build_encounter_case(chart, admission, -2)
    assert max(p["collected_offset_days"] for p in visible["lab_panels"]) == 0


# ═════════════════════════════════════════════════════════════════════════════
# §3.5 — curation
# ═════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("result,keep", [
    ({"analyte": "Sodium", "value": 136, "unit": "mmol/L"}, True),
    ({"analyte": "- Bicarbonate", "value": 15.6, "unit": "mmol/L"}, True),
    ({"analyte": "Creatinine", "value": 0.668, "loinc": "2160-0"}, True),
    ({"analyte": "- AMIKACIN", "value": "S"}, True),          # culture sensitivity
    ({"analyte": "- Color", "value": "YELLOW"}, True),        # urinalysis descriptor
    ({"analyte": "Result KEYS", "value": "(key legend cut off)"}, False),
    ({"analyte": "Location", "value": "Intensive Care Unit (ICU) -- 007"}, False),
    ({"analyte": "Normal Ranges", "value": "Male 0.7-1.2; Female 0.5-0.9"}, False),
    ({"analyte": "- Adult", "value": 3.5, "unit": "-5.2"}, False),
    ({"analyte": "- > 60 Years", "value": 3.2, "unit": "-4.6"}, False),
    ({"analyte": "Status stamp", "value": "APPROVED"}, False),
])
def test_report_furniture_is_not_a_lab_result(result, keep):
    assert real_cases.keep_lab_result(result) is keep


def test_the_same_result_from_three_formats_appears_once_and_keeps_the_flag():
    """The CSV carries the LOINC and no flag; the HL7 export carries the flag.
    Keeping whichever arrived first threw the flag away on every duplicated
    analyte and zeroed the abnormal-ratio difficulty axis."""
    panels = [
        {"panel": "BMP", "collected_offset_days": 0,
         "results": [{"analyte": "Potassium", "value": 2.4, "unit": "mmol/L",
                      "loinc": "2823-3"}]},
        {"panel": "Laboratory Panel", "collected_offset_days": 0,
         "results": [{"analyte": "- Potassium", "value": "2.40", "unit": "mmol/L",
                      "flag": "L"}]},
    ]
    out, stats = real_cases.curate_lab_panels(panels)
    results = [r for p in out for r in p["results"]]
    assert len(results) == 1
    assert results[0]["flag"] == "L" and results[0]["loinc"] == "2823-3"
    assert stats["dropped_duplicate"] == 1


def test_an_out_of_range_value_is_flagged_even_when_the_lab_did_not_flag_it():
    out, stats = real_cases.curate_lab_panels([
        {"panel": "BMP", "collected_offset_days": 0, "results": [
            {"analyte": "Bicarbonate", "value": 10.0, "unit": "mmol/L",
             "ref_low": 20.0, "ref_high": 29.0},
            {"analyte": "Sodium", "value": 138, "unit": "mmol/L",
             "ref_low": 135, "ref_high": 145}]}])
    flags = {r["analyte"]: r.get("flag") for r in out[0]["results"]}
    assert flags == {"Bicarbonate": "L", "Sodium": None}   # in-range stays unflagged
    assert stats["flags_derived"] == 1


@pytest.mark.parametrize("line,drug", [
    ("Inj Tanzo 4.5 gm I/V x TDS", "Tanzo"),
    ("Tab Ascard 75 mg x OD", "Ascard"),
    ("T. Risp 1 mg 1/2 (1/2 + 1/2) half BD", "Risp"),
    ("Neb c̄ Clenil x BD", "Clenil"),        # the Latin "c̄" (with) is not the drug
    ("Neb c̄ Atem x 6 H°", "Atem"),
    ("Tab Vita-6 1 x OD", "Vita-6"),         # the trailing count is not the drug
])
def test_a_real_order_line_parses_to_a_drug(line, drug):
    parsed = real_cases.parse_medication_line(line)
    assert parsed is not None and parsed["drug"] == drug


@pytest.mark.parametrize("line", [
    "Orders:", "Treatment:", "Time: 12:03 PM", "Form: Fresh Orders",
    "Form footer: August 2024, Rev # 01, Page 1 of 1; PTO noted",
    "1. CBC, U/E/E, LFTs, BSL",
    "Nurses Notes - 28/01/2021, 18:04:",
    "46 years old male pt received in ER c/o ALOC",
    "Form note: Please write all medication and IV fluid orders on the separate sheet",
    "Intake / Output tables: blank",
])
def test_order_sheet_furniture_is_not_a_medication(line):
    assert real_cases.parse_medication_line(line) is None


@pytest.mark.parametrize("a,b", [
    ("Extor 5/160", "Extor"),                 # the same drug, two order sheets
    ("Ascard 75 (", "Ascard"),
    ("Rivotril 0.5", "Rivotril"),
    ("c̄ Clenil", "Clenil"),
    ("Humulin-R", "Humulin"),
])
def test_one_drug_written_two_ways_has_one_identity(a, b):
    """A re-order is not a treatment decision. If the same drug resolves to two
    identities, every re-order reads as newly started and lands in the answer key —
    against a med list the visible chart already shows."""
    assert real_cases._drug_identity(a) == real_cases._drug_identity(b)


def test_a_continued_drug_is_not_part_of_the_answer():
    chart = _chart()
    chart["medications"].append(
        {"drug": "Tab Furosemide 40 mg (1+0+0)", "collected_offset_days": -1})
    enc = real_cases.segment_longitudinal_record(chart)[-1]
    _visible, held_out, _stats = real_cases.build_encounter_case(chart, enc, -2)
    assert any("urosemide" in d for d in held_out["drugs_started_after"])
    assert not any("urosemide" in d for d in held_out["newly_started_drugs"])
    assert any("olvaptan" in d for d in held_out["newly_started_drugs"])


def test_the_bundles_own_readme_is_not_a_clinical_note():
    notes = [
        {"note_type": "Progress", "text": "# patient-4 — De-identified EHR export\n\n"
                                          "## Contents\n\nFHIR bundle, HL7, CSV."},
        {"note_type": "Progress", "text": "Admitted with confusion. " * 20},
        {"note_type": "Progress", "text": "Admitted   with confusion.  " * 20},
    ]
    out, stats = real_cases.curate_notes(notes)
    assert len(out) == 1
    assert stats["dropped_non_clinical"] == 1 and stats["dropped_duplicate"] == 1


def test_untimed_items_fail_closed_and_are_counted():
    chart = _chart()
    chart["notes"].append({"note_type": "Progress", "text": "Undated compilation. " * 20})
    enc = real_cases.segment_longitudinal_record(chart)[-1]
    visible, _held, stats = real_cases.build_encounter_case(chart, enc, -2)
    assert stats["withheld_untimed"] == {"notes": 1}
    assert all(n.get("collected_offset_days") is not None for n in visible["notes"])


# ═════════════════════════════════════════════════════════════════════════════
# §3.4 — specialty and taxonomy
# ═════════════════════════════════════════════════════════════════════════════
def test_specialty_is_inferred_from_the_chart():
    chart = _chart()
    enc = real_cases.segment_longitudinal_record(chart)[-1]
    visible, _h, _s = real_cases.build_encounter_case(chart, enc, -2)
    specialty, confidence, scores = real_cases.infer_specialty(visible)
    assert specialty == "nephrology" and confidence >= 0.6
    assert scores["oncology"] == 0.0


def test_a_chart_with_no_served_signal_returns_none_rather_than_guessing():
    specialty, confidence, _scores = real_cases.infer_specialty({
        "problem_list": [{"condition": "Sinusitis"}], "notes": [], "lab_panels": []})
    assert specialty is None and confidence == 0.0


def test_the_bucket_comes_from_the_registry_or_is_honestly_none():
    from asclepius.specialties import SPECIALTY_REGISTRY
    chart = _chart()
    enc = real_cases.segment_longitudinal_record(chart)[-1]
    visible, _h, _s = real_cases.build_encounter_case(chart, enc, -2)
    bucket, subtopic = real_cases.classify_case_to_bucket(visible, "nephrology")
    cfg = SPECIALTY_REGISTRY["nephrology"]
    assert bucket in cfg.bucket_ids()
    assert subtopic in cfg.bucket(bucket).subtopics

    assert real_cases.classify_case_to_bucket(visible, "dermatology") == (None, None)


# ═════════════════════════════════════════════════════════════════════════════
# §3.6 — difficulty is measured or it is not hard
# ═════════════════════════════════════════════════════════════════════════════
def _rich_case() -> dict:
    chart = _chart()
    enc = real_cases.segment_longitudinal_record(chart)[-1]
    visible, _h, _s = real_cases.build_encounter_case(chart, enc, -2)
    return visible


def test_structure_alone_can_propose_hard_but_never_confer_it():
    scored = real_cases.score_difficulty(_rich_case(), encounters_spanned=4,
                                         bucket_id="recent_standard_of_care")
    assert scored["measured"] is False
    assert scored["band"] != "hard"
    if scored["score"] >= 0.66:
        assert "no frontier measurement" in scored["gate_note"]


def test_a_case_the_models_get_right_is_not_hard_however_baroque_the_chart():
    scored = real_cases.score_difficulty(_rich_case(), encounters_spanned=4,
                                         bucket_id="recent_standard_of_care",
                                         model_failure_rate=0.0)
    assert scored["measured"] is True and scored["band"] != "hard"


def test_hard_requires_both_the_structural_score_and_the_failure_rate():
    scored = real_cases.score_difficulty(_rich_case(), encounters_spanned=4,
                                         bucket_id="recent_standard_of_care",
                                         model_failure_rate=1.0)
    assert scored["band"] == "hard"
    assert scored["axes"]["model_failure_rate"] == 1.0
    assert scored["measured"] is True


def test_the_weights_are_the_prds_and_sum_to_one():
    assert real_cases.DIFFICULTY_WEIGHTS["model_failure_rate"] == 0.50
    assert abs(sum(real_cases.DIFFICULTY_WEIGHTS.values()) - 1.0) < 1e-9


# ═════════════════════════════════════════════════════════════════════════════
# §3.3 / §3.7 — question and failure mode
# ═════════════════════════════════════════════════════════════════════════════
def test_the_derived_question_is_case_specific_not_a_per_specialty_default():
    visible = _rich_case()
    question = real_cases._fallback_question(visible, "nephrology")
    assert "40-49" in question and "Sodium" in question
    assert "next step" in question.lower()


def test_a_question_that_states_the_answer_is_refused():
    held_out = {"problems_recorded_after": ["Syndrome of inappropriate antidiuresis"],
                "drugs_started_after": []}
    real_cases.assert_question_has_no_leakage(
        "What is driving this patient's hyponatremia?", held_out)
    with pytest.raises(real_cases.AnswerLeakage):
        real_cases.assert_question_has_no_leakage(
            "Given the syndrome of inappropriate antidiuresis, what next?", held_out)


def test_a_trap_the_models_did_not_fall_for_is_not_a_trap():
    unmeasured = real_cases.score_difficulty(_rich_case())
    assert real_cases.derive_ai_failure_mode(_rich_case(), unmeasured) is None
    passed = real_cases.score_difficulty(_rich_case(), model_failure_rate=0.0)
    assert real_cases.derive_ai_failure_mode(_rich_case(), passed) is None


def test_the_failure_mode_is_taken_from_what_the_models_actually_got_wrong():
    scored = real_cases.score_difficulty(_rich_case(), model_failure_rate=0.8)
    mode = real_cases.derive_ai_failure_mode(_rich_case(), scored, [
        {"model": "m1", "failure_reason": "Anchored on the latest sodium; missed the trend."},
        {"model": "m2", "failure_reason": "Anchored on the latest sodium; missed the trend."},
    ])
    assert mode.startswith("Anchored on the latest sodium")


# ═════════════════════════════════════════════════════════════════════════════
# §5 — the admin endpoint
# ═════════════════════════════════════════════════════════════════════════════
def _zip(entries: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _ingest_a_chart(admin_h, specialty="nephrology") -> dict:
    """Push a real-shaped multi-format bundle through the real pipeline."""
    r = client.post("/api/asclepius/admin/upload-links", headers=admin_h, json={
        "partner_id": "deid-partner", "purpose": "task_creation",
        "partner_label": "De-id Partner", "specialty": specialty,
        "expires_hours": 24, "one_time": True})
    assert r.status_code == 200, r.text
    token = r.json()["token"]

    fhir = json.dumps({"resourceType": "Bundle", "type": "collection", "entry": [
        {"resource": {"resourceType": "Patient", "id": "patient-4-patient",
                      "gender": "male", "extension": [
                          {"url": "http://hl7.org/fhir/StructureDefinition/patient-age",
                           "valueString": "45Y"}]}},
        {"resource": {"resourceType": "Condition",
                      "code": {"text": "Chronic kidney disease"},
                      "onsetDateTime": "2025-11-01T00:00:00Z"}},
        {"resource": {"resourceType": "Condition",
                      "code": {"text": "Hyponatremia"},
                      "onsetDateTime": "2026-01-29T00:00:00Z"}},
        {"resource": {"resourceType": "MedicationStatement",
                      "medicationCodeableConcept": {"text": "Tab Furosemide 40 mg x OD"},
                      "effectiveDateTime": "2025-11-01T00:00:00Z"}},
        {"resource": {"resourceType": "MedicationStatement",
                      "medicationCodeableConcept": {"text": "Inj Tolvaptan 15 mg I/V x OD"},
                      "effectiveDateTime": "2026-01-31T00:00:00Z"}},
        {"resource": {"resourceType": "DocumentReference", "id": "d1",
                      "type": {"text": "Progress"}, "date": "2025-11-01T00:00:00Z",
                      "content": [{"attachment": {"contentType": "text/plain",
                                                  "data": _b64("Routine nephrology review on 01/11/2025. "
                                                               "Creatinine stable, sodium 138. " * 6)}}]}},
        {"resource": {"resourceType": "DocumentReference", "id": "d2",
                      "type": {"text": "Consult"}, "date": "2026-01-29T00:00:00Z",
                      "content": [{"attachment": {"contentType": "text/plain",
                                                  "data": _b64("Admitted 29/01/2026 with confusion. GCS 12/15. "
                                                               "Serum osmolality low, urine sodium high. " * 6)}}]}},
        {"resource": {"resourceType": "DocumentReference", "id": "d3",
                      "type": {"text": "Progress"}, "date": "2026-01-31T00:00:00Z",
                      "content": [{"attachment": {"contentType": "text/plain",
                                                  "data": _b64("Started on tolvaptan after SIADH confirmed. " * 10)}}]}},
    ]})
    csv_text = "\n".join(
        ["service_date,panel,test_name,loinc,value,unit,reference_range,flag"]
        + [f"{d},BMP,Sodium,2951-2,{v},mmol/L,135 - 145,"
           for d, v in (("2025-11-01", 138), ("2025-11-02", 134),
                        ("2026-01-29", 118), ("2026-01-30", 120), ("2026-01-31", 121))]
        + [f"{d},BMP,Creatinine,2160-0,{v},mg/dL,0.7 - 1.2,"
           for d, v in (("2025-11-01", 1.9), ("2025-11-02", 2.1),
                        ("2026-01-29", 2.4), ("2026-01-30", 2.5), ("2026-01-31", 2.3))])

    res = client.post(f"/api/asclepius/partner/uploads?t={token}",
                      files={"file": ("bundle.zip",
                                      _zip({"fhir/bundle.json": fhir,
                                            "labs/lab_results.csv": csv_text}),
                                      "application/zip")})
    assert res.status_code == 200, res.text
    upload_id = res.json()["upload_id"]
    cases = _store().list_ingest_cases(upload_id=upload_id)
    assert len(cases) == 1, f"one patient must be one case, got {len(cases)}"
    return cases[0]


def _b64(text: str) -> str:
    import base64
    return base64.b64encode(text.encode()).decode()


def test_a_multi_format_chart_ingests_as_one_case_with_an_age_band():
    ic = _ingest_a_chart(_admin_h())
    assert ic["status"] == "ingested", (ic.get("report") or {}).get("quarantine_reason")
    case = ic["case"]
    assert case["demographics"]["age_band"] == "40-49"
    assert (ic["report"].get("timeline") or {})["unresolved"] == []
    assert len(case["lab_panels"]) >= 5 and case["problem_list"]


def test_dry_run_returns_a_full_plan_and_writes_nothing(monkeypatch):
    admin_h = _admin_h()
    ic = _ingest_a_chart(admin_h)
    before = len(_store().list_tasks(limit=500))

    r = client.post(f"/api/asclepius/ingestion/cases/{ic['ingest_case_id']}/generate",
                    headers=admin_h, json={"dry_run": True, "derive_questions": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True
    assert body["encounters"] >= 2
    assert len(_store().list_tasks(limit=500)) == before, "a dry run must write nothing"
    assert _store().get_ingest_case(ic["ingest_case_id"])["status"] == "ingested"

    generatable = [p for p in body["proposals"] if p["generatable"]]
    assert generatable, [p["blockers"] for p in body["proposals"]]
    for p in generatable:
        assert p["question"] and len(p["question"]) > 40
        # Model output until a physician accepts it — the UI renders it orange.
        assert p["question_source"] in ("model", "deterministic")
        assert p["specialty"] == "nephrology"
        assert p["difficulty"]["measured"] is False      # nothing measured in a plan
        assert p["difficulty"]["band"] in ("easy", "medium")
        assert p["case_type"].startswith("multimodal:real")
        assert p["prompt"]
        # The internal answer key never leaves the server.
        assert "ground_truth" not in p["case"]

    # Distinct index events, distinct questions — not one question N times.
    indices = [p["index_event_offset"] for p in generatable]
    assert len(set(indices)) == len(indices)


def test_the_plan_reports_the_encounters_it_skipped_and_why():
    admin_h = _admin_h()
    ic = _ingest_a_chart(admin_h)
    r = client.post(f"/api/asclepius/ingestion/cases/{ic['ingest_case_id']}/generate",
                    headers=admin_h, json={"dry_run": True, "derive_questions": False})
    body = r.json()
    assert len(body["proposals"]) == body["encounters"]
    for p in body["proposals"]:
        assert p["generatable"] or p["blockers"], "a skipped proposal must say why"


def test_generation_is_refused_for_a_brokering_case():
    admin_h = _admin_h()
    r = client.post("/api/asclepius/admin/upload-links", headers=admin_h, json={
        "partner_id": "broker", "purpose": "brokering", "specialty": "nephrology",
        "expires_hours": 24, "one_time": True})
    token = r.json()["token"]
    csv_text = ("service_date,panel,test_name,value,unit,reference_range\n"
                "2026-01-31,BMP,Sodium,118,mmol/L,135 - 145\n")
    res = client.post(f"/api/asclepius/partner/uploads?t={token}",
                      files={"file": ("b.zip", _zip({"labs.csv": csv_text}),
                                      "application/zip")})
    ic = _store().list_ingest_cases(upload_id=res.json()["upload_id"])[0]
    r2 = client.post(f"/api/asclepius/ingestion/cases/{ic['ingest_case_id']}/generate",
                     headers=admin_h, json={"dry_run": True})
    assert r2.status_code == 409 and "brokering" in r2.json()["detail"]


def test_an_unserved_specialty_override_is_refused():
    admin_h = _admin_h()
    ic = _ingest_a_chart(admin_h)
    r = client.post(f"/api/asclepius/ingestion/cases/{ic['ingest_case_id']}/generate",
                    headers=admin_h, json={"dry_run": True, "specialty": "dermatology"})
    assert r.status_code == 400 and "not enabled" in r.json()["detail"]


def test_generation_produces_fully_tagged_tasks_with_a_measured_difficulty(monkeypatch):
    """The tag contract (§4): a generated V4 case must be indistinguishable from a
    V3 case in its tagging, and its difficulty must be MEASURED — which is what
    ``ASCLEPIUS_REQUIRE_MEASURED_DIFFICULTY=1`` demands and what every real task
    previously failed, because every one of them was stamped "hard" by hand."""
    from asclepius import empirical_difficulty as ed
    from asclepius import critic as asc_critic
    import routers.asclepius as router_mod

    admin_h = _admin_h()
    ic = _ingest_a_chart(admin_h)

    async def _measured(case, question, **kw):
        return {"value": 0.8, "value_lower": 0.55, "value_upper": 0.93, "measured": True,
                "both_axes": True, "k": 5, "n_attempts": 10, "n_failures": 8,
                "n_models": 2, "per_provider": {}, "floor": 0.5, "passes_gate": True,
                "failure_reasons": [
                    {"model": "m1", "failure_reason": "Anchored on the latest sodium value."},
                    {"model": "m2", "failure_reason": "Anchored on the latest sodium value."}]}

    async def _candidates(prompt, *, specialty="general", ai_failure_mode=None):
        assert ai_failure_mode, "the flawed candidate must be keyed to the derived trap"
        return {"candidates": [{"id": "A", "text": "Strong answer."},
                               {"id": "B", "text": "Flawed answer."}],
                "model": "test-model", "intended_flawed_id": "B"}

    async def _hardness(prompt, candidates):
        return {"skipped": False, "hardness_score": 0.8, "hardness_axes": ["multi_step"]}

    async def _case_judge(case, case_source="synthetic"):
        return {"skipped": False, "coherence": 0.9, "multimodal_necessity": 0.9,
                "reasoning_divergence_potential": 0.9}

    monkeypatch.setattr(ed, "measure_empirical_difficulty", _measured)
    monkeypatch.setattr(router_mod, "generate_candidates_ex", _candidates)
    monkeypatch.setattr(asc_critic, "run_hardness_judge", _hardness)
    monkeypatch.setattr(asc_critic, "run_case_judge", _case_judge)

    r = client.post(f"/api/asclepius/ingestion/cases/{ic['ingest_case_id']}/generate",
                    headers=admin_h, json={"dry_run": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["generated"] >= 1, body.get("details")

    store = _store()
    for task_id in body["task_ids"]:
        task = store.get_task(task_id)
        gen = task["generation"]
        assert task["source"] == "partner_ehr"
        assert task["case_source"] == "real_deid"
        assert task["modality"] == "multimodal"
        assert task["specialty"] == "nephrology"
        assert task["difficulty"] in ("easy", "medium", "hard")
        assert len(task["candidate_answers"]) == 2

        assert gen["mode"] == "real_case_generated"
        assert gen["case_source"] == "real_deid"
        assert gen["case_type"].startswith("multimodal:real")
        assert gen["question"] and gen["question_source"]
        assert gen["ai_failure_mode"], "the A/B pair must be keyed to a real trap"
        assert gen["intended_flawed_id"] == "B"
        assert gen["empirical_difficulty"]["measured"] is True
        assert gen["empirical_difficulty"]["value"] == 0.8
        assert gen["hardness"]["score"] == 0.8
        assert gen["case_judge"]["coherence"] == 0.9
        assert gen["ingest_case_id"] == ic["ingest_case_id"]
        assert gen["encounter_index"] is not None
        # Provenance, never a calendar anchor back into the partner's timeline.
        assert "index_event" not in gen
        assert isinstance(gen["index_event_offset"], int)

        # The columns the serving gate reads.
        assert task["difficulty_measured"] == 1
        assert task["empirical_difficulty"] == 0.8

    assert store.get_ingest_case(ic["ingest_case_id"])["status"] == "promoted"


def test_generated_tasks_survive_the_measured_difficulty_serving_gate(monkeypatch):
    """``ASCLEPIUS_REQUIRE_MEASURED_DIFFICULTY=1`` emptied the V4 queue entirely,
    because every promoted real task had ``empirical_difficulty IS NULL``."""
    test_generation_produces_fully_tagged_tasks_with_a_measured_difficulty(monkeypatch)
    store = _store()
    evaluator = A.make_user(store, role="evaluator", specialty="nephrology")
    served = store.next_task_for_evaluator(
        evaluator_id=evaluator["id"], specialty="nephrology", real_only=True,
        multimodal_only=True, require_measured_difficulty=True,
        min_empirical_difficulty=0.5)
    assert served is not None, "the V4 queue must not be empty under the measured gate"
    assert served["case_source"] == "real_deid"


def test_the_case_list_can_be_scoped_to_one_upload():
    """The admin's per-upload "Preview cases" control must not pull every ingest
    case in the system to find the two it is about to plan."""
    admin_h = _admin_h()
    ic = _ingest_a_chart(admin_h)
    r = client.get("/api/asclepius/ingestion/cases?status=ingested&upload_id="
                   + ic["upload_id"], headers=admin_h)
    assert r.status_code == 200
    ids = [c["ingest_case_id"] for c in r.json()["cases"]]
    assert ids == [ic["ingest_case_id"]]


# ═════════════════════════════════════════════════════════════════════════════
# §4 — the tag contract applies to the ratified gold cases too
# ═════════════════════════════════════════════════════════════════════════════
def test_every_gold_case_carries_a_registry_bucket():
    """The 10 nephrology gold cases had ``taxonomy_bucket = None`` while the
    cardiology and oncology sets were fully tagged. Backfilled rather than matched
    — the schema was unevenly populated, and a real case must not inherit a broken
    precedent for the field a buyer filters on."""
    from asclepius.gold_cases import GOLD_CASE_SETS
    from asclepius.specialties import SPECIALTY_REGISTRY

    for specialty, cases in GOLD_CASE_SETS.items():
        cfg = SPECIALTY_REGISTRY[specialty]
        for c in cases:
            bucket, subtopic = c.get("taxonomy_bucket"), c.get("subtopic")
            assert bucket in cfg.bucket_ids(), (c["case_id"], bucket)
            # An honest None where no registry subtopic fits (AIN is not contrast
            # nephropathy) beats a near-miss label.
            assert subtopic is None or subtopic in cfg.bucket(bucket).subtopics, \
                (c["case_id"], bucket, subtopic)


def test_a_task_is_never_created_with_an_empty_question(monkeypatch):
    """``derive_questions=false`` on a LIVE run must not insert a prompt that asks
    nothing — the deterministic question is the floor, not an optional extra."""
    from asclepius import critic as asc_critic
    from asclepius import empirical_difficulty as ed
    import routers.asclepius as router_mod

    admin_h = _admin_h()
    ic = _ingest_a_chart(admin_h)

    async def _measured(case, question, **kw):
        assert question and question.strip(), "the difficulty probe needs a question"
        return {"value": 0.8, "measured": True, "k": 5, "n_models": 2,
                "per_provider": {}, "failure_reasons": []}

    async def _candidates(prompt, *, specialty="general", ai_failure_mode=None):
        return {"candidates": [{"id": "A", "text": "a"}, {"id": "B", "text": "b"}],
                "model": "m", "intended_flawed_id": "B"}

    async def _hardness(prompt, candidates):
        return {"skipped": True}

    async def _judge(case, case_source="synthetic"):
        return {"skipped": False, "coherence": 0.9, "multimodal_necessity": 0.9,
                "reasoning_divergence_potential": 0.9}

    monkeypatch.setattr(ed, "measure_empirical_difficulty", _measured)
    monkeypatch.setattr(router_mod, "generate_candidates_ex", _candidates)
    monkeypatch.setattr(asc_critic, "run_hardness_judge", _hardness)
    monkeypatch.setattr(asc_critic, "run_case_judge", _judge)

    r = client.post(f"/api/asclepius/ingestion/cases/{ic['ingest_case_id']}/generate",
                    headers=admin_h, json={"dry_run": False, "derive_questions": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["generated"] >= 1, body.get("details")
    store = _store()
    for task_id in body["task_ids"]:
        task = store.get_task(task_id)
        assert task["generation"]["question"].strip()
        assert task["prompt"].startswith("CLINICAL QUESTION:")


def test_the_case_judge_fails_closed_on_the_real_path(monkeypatch):
    from asclepius import critic as asc_critic
    from asclepius import empirical_difficulty as ed
    import routers.asclepius as router_mod

    admin_h = _admin_h()
    ic = _ingest_a_chart(admin_h)

    async def _measured(case, question, **kw):
        return {"value": 0.8, "measured": True, "k": 5, "n_models": 2,
                "per_provider": {}, "failure_reasons": []}

    async def _candidates(prompt, *, specialty="general", ai_failure_mode=None):
        return {"candidates": [{"id": "A", "text": "a"}, {"id": "B", "text": "b"}],
                "model": "m", "intended_flawed_id": "B"}

    async def _hardness(prompt, candidates):
        return {"skipped": True}

    async def _judge_unavailable(case, case_source="synthetic"):
        return {"skipped": True}

    monkeypatch.setattr(ed, "measure_empirical_difficulty", _measured)
    monkeypatch.setattr(router_mod, "generate_candidates_ex", _candidates)
    monkeypatch.setattr(asc_critic, "run_hardness_judge", _hardness)
    monkeypatch.setattr(asc_critic, "run_case_judge", _judge_unavailable)

    r = client.post(f"/api/asclepius/ingestion/cases/{ic['ingest_case_id']}/generate",
                    headers=admin_h, json={"dry_run": False})
    assert r.status_code == 200
    body = r.json()
    assert body["generated"] == 0
    assert any("Case judge unavailable" in (d.get("error") or "")
               for d in body["details"]["failed"])
