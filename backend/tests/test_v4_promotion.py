"""V4 real cases + promotion pipeline repair (V4 Cases & Promotion PRD §6).

Every test here traces to a defect the PRD measured against the real
``patient-1`` / ``patient-3`` / ``patient-4`` bundles:

  * §0.1 no ``manifest.json`` → specialty resolves to ``general`` → promote 409.
    The gate is CORRECT; the input was wrong. These tests pin the gate shut.
  * §0.2 the partner's own de-identification header is scanned as clinical text,
    and one of them carries an unshifted original date — which either quarantines
    the chart or (worse) is silently laundered into a plausible relative offset.
  * §0.3 patient-key unification was never broken. Regression only — do not touch.
  * §0.4 hepatology and neurology are not registered specialties, so none of the
    three bundles routes anywhere as-is.
  * §2 42 polluted unit cells caught by nothing, 16 dates inside reference
    ranges, and one bicarbonate of 1.7 mmol/L that is not survivable.
  * §4 visibility and paid labels are two different things.
  * §5.1 there was no way to see what a case would become without committing it.
"""

from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import ingestion as asc_ingestion  # noqa: E402
from asclepius import real_cases  # noqa: E402
from asclepius import specialties as asc_specialties  # noqa: E402
from asclepius import timeline as asc_timeline  # noqa: E402
from asclepius import v4_cases  # noqa: E402

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


def _mint(admin_h, **over):
    # specialty="" reproduces the PRD's measured scenario: nothing anywhere has
    # declared one. (UploadLinkRequest.specialty still DEFAULTS to 'nephrology',
    # which is its own latent instance of this same mislabel — see the note in
    # ingestion.py:1721 — but changing that default is not this PRD's scope.)
    body = {"partner_id": "mercy-health", "purpose": "task_creation",
            "partner_label": "Mercy Health", "specialty": "",
            "expires_hours": 24, "one_time": False}
    body.update(over)
    r = client.post("/api/asclepius/admin/upload-links", json=body, headers=admin_h)
    assert r.status_code == 200, r.text
    return r.json()


def _zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


# Two dated panels so the timeline has an anchor and the content gate has labs.
_CSV = """patient_key,panel,analyte,loinc,value,unit,ref_low,ref_high,flag,collected_at
p1,LFT,Bilirubin (total),1975-2,15.04,mg/dL,0.2,1.2,H,2031-03-14
p1,LFT,Gamma-glutamyl transferase,2324-2,1358,U/L,8,61,H,2031-03-14
p1,LFT,Bilirubin (total),1975-2,17.77,mg/dL,0.2,1.2,H,2031-03-19
p1,LFT,Gamma-glutamyl transferase,2324-2,123,U/L,8,61,H,2031-03-19
"""

_NOTE = (
    "Hepatology progress note. Nineteen days after ERCP and plastic stent placement "
    "for portal biliopathy with a distal common bile duct stricture. The patient is "
    "clinically brighter, itch has resolved and the stools have darkened, but he "
    "remains visibly jaundiced. Bilirubin is up while GGT has fallen more than "
    "tenfold, and the team has asked whether he needs a repeat ERCP before discharge. "
    "Of note he developed post-ERCP pancreatitis after the index procedure and his "
    "platelets are 100 from hypersplenism."
)

# The measured line from patient-4: a partner de-identification header carrying an
# unshifted original date, sitting INSIDE an otherwise ordinary clinical note.
_PROVENANCE_LINE = (
    "De-identification: Omitted nurse name/designation fields (green redaction); "
    "year as printed (11/7/21)"
)


def _upload(token, zip_bytes, expect=200):
    r = client.post(f"/api/asclepius/partner/uploads?t={token}",
                    files={"file": ("bundle.zip", zip_bytes, "application/zip")})
    assert r.status_code == expect, r.text
    return r.json() if expect == 200 else r


def _manifest(**over):
    m = {"patient_key": "p1", "specialty": "hepatology", "index_event": "2031-03-19"}
    m.update(over)
    return json.dumps(m)


def _ingest(entries, *, link_specialty=None):
    """Upload a bundle and return its ingest cases (any status)."""
    link = _mint(_admin_h(), **({"specialty": link_specialty} if link_specialty else {}))
    res = _upload(link["token"], _zip(entries))
    return _store().list_ingest_cases(upload_id=res["upload_id"]), res["upload_id"]


def _stub_promote_llm(monkeypatch, *, coherence=0.9):
    from routers import asclepius as R
    from asclepius import critic

    async def fake_candidates(prompt, **kw):
        return {"candidates": [
            {"id": "A", "text": "Continue current management; the enzymes have normalised."},
            {"id": "B", "text": "Repeat the ERCP for stent revision."}],
            "model": "cand", "intended_flawed_id": "B"}

    async def fake_hardness(prompt, candidates=None, **kw):
        return {"skipped": False, "hardness_score": 0.85, "hardness_axes": ["multi_step"]}

    async def fake_case_judge(case, case_source="synthetic"):
        return {"skipped": False, "coherence": coherence, "ground_truth_determinable": None,
                "multimodal_necessity": 0.85, "reasoning_divergence_potential": 0.7,
                "explanation": "", "model": "cj"}

    monkeypatch.setattr(R, "generate_candidates_ex", fake_candidates)
    monkeypatch.setattr(critic, "run_hardness_judge", fake_hardness)
    monkeypatch.setattr(critic, "run_case_judge", fake_case_judge)


# ═════════════════════════════════════════════════════════════════════════════
# Manifest / specialty  (§0.1, §1.1, §5 steps 3 and 3b)
# ═════════════════════════════════════════════════════════════════════════════
def test_a_bundle_with_no_manifest_resolves_to_general_and_promote_409s(monkeypatch):
    """The exact failure the PRD reproduced. The gate is right; the input was wrong."""
    _stub_promote_llm(monkeypatch)
    cases, _ = _ingest({"labs.csv": _CSV, "note.txt": _NOTE})
    ic = [c for c in cases if c["status"] == "ingested"][0]
    assert ic["specialty"] == "general"
    r = client.post(f"/api/asclepius/ingestion/cases/{ic['ingest_case_id']}/promote",
                    json={"question": "What is your next step?"}, headers=_admin_h())
    assert r.status_code == 409
    assert "Specialty not determined" in r.json()["detail"]
    # Not consumed: the case is still promotable once a specialty is set.
    assert _store().get_ingest_case(ic["ingest_case_id"])["status"] == "ingested"


def test_a_manifest_specialty_carries_the_case_past_the_specialty_gate(monkeypatch):
    _stub_promote_llm(monkeypatch)
    cases, _ = _ingest({"manifest.json": _manifest(specialty="nephrology"),
                        "labs.csv": _CSV, "note.txt": _NOTE})
    ic = [c for c in cases if c["status"] == "ingested"][0]
    assert ic["specialty"] == "nephrology"
    r = client.post(f"/api/asclepius/ingestion/cases/{ic['ingest_case_id']}/promote",
                    json={"question": "Assess the management."}, headers=_admin_h())
    assert r.status_code == 200, r.text
    assert _store().get_task(r.json()["task_id"])["specialty"] == "nephrology"


def test_the_link_specialty_applies_when_the_manifest_has_none(monkeypatch):
    _stub_promote_llm(monkeypatch)
    cases, _ = _ingest({"labs.csv": _CSV, "note.txt": _NOTE}, link_specialty="cardiology")
    ic = [c for c in cases if c["status"] == "ingested"][0]
    assert ic["specialty"] == "cardiology"


def test_general_is_never_accepted_as_a_specialty():
    """The gate cannot be argued out of: 'general' is the ABSENCE of a specialty."""
    for value in ("general", "GENERAL", "  General  ", "", None):
        assert asc_ingestion.specialty_is_undetermined(value) is True
    for value in ("nephrology", "hepatology", "cardiology"):
        assert asc_ingestion.specialty_is_undetermined(value) is False


# ═════════════════════════════════════════════════════════════════════════════
# Hepatology registration  (§0.4, §1.4)
# ═════════════════════════════════════════════════════════════════════════════
def test_hepatology_is_a_registered_enabled_specialty_with_a_real_corpus():
    """§1.4. Before this landed, /generate returned 400 naming only three."""
    from asclepius.corpus import load_corpus

    assert asc_specialties.is_enabled("hepatology")
    cfg = asc_specialties.get_specialty_config("hepatology")
    assert cfg.taxonomy and cfg.accent in ("green", "orange", "pink")  # no blue
    corpus = load_corpus("hepatology")
    # NOT a stub. An empty corpus classifies every hepatology case into no bucket
    # and ships an unusable taxonomy field — the PRD's explicit "do not do this".
    assert len(corpus["items"]) >= 10
    assert {it["topic"] for it in corpus["items"]} <= set(cfg.bucket_ids())
    assert all(it["difficulty"] == "hard" for it in corpus["items"])


def test_an_enabled_specialty_with_a_missing_corpus_fails_loudly_not_silently():
    """§1.4. The failure this replaces was SILENT: every case into no bucket."""
    from dataclasses import replace

    original = dict(asc_specialties.SPECIALTY_REGISTRY)
    try:
        asc_specialties.SPECIALTY_REGISTRY["ghostology"] = replace(
            original["hepatology"], name="ghostology",
            seed_corpus="seed_corpus/ghostology.v1.json")
        with pytest.raises(asc_specialties.SpecialtyMisconfigured) as exc:
            asc_specialties._assert_enabled_specialties_have_corpora()
        assert "ghostology" in str(exc.value)
    finally:
        asc_specialties.SPECIALTY_REGISTRY.clear()
        asc_specialties.SPECIALTY_REGISTRY.update(original)
    # And the real registry is consistent, which is what runs at import.
    asc_specialties._assert_enabled_specialties_have_corpora()


def test_generate_refuses_an_unregistered_specialty_by_name(monkeypatch):
    """§5 step 3b: a 400 naming the enabled set is a DIFFERENT failure to a 409."""
    cases, _ = _ingest({"manifest.json": _manifest(), "labs.csv": _CSV, "note.txt": _NOTE})
    ic = [c for c in cases if c["status"] == "ingested"][0]
    r = client.post(f"/api/asclepius/ingestion/cases/{ic['ingest_case_id']}/generate",
                    json={"dry_run": True, "specialty": "neurology"}, headers=_admin_h())
    assert r.status_code == 400
    detail = json.dumps(r.json())
    assert "neurology" in detail and "hepatology" in detail   # names what IS served


def test_generate_accepts_hepatology_now_that_it_is_registered():
    cases, _ = _ingest({"manifest.json": _manifest(), "labs.csv": _CSV, "note.txt": _NOTE})
    ic = [c for c in cases if c["status"] == "ingested"][0]
    r = client.post(f"/api/asclepius/ingestion/cases/{ic['ingest_case_id']}/generate",
                    json={"dry_run": True, "specialty": "hepatology",
                          "derive_questions": False}, headers=_admin_h())
    assert r.status_code == 200, r.text


def test_a_hepatobiliary_chart_routes_to_hepatology_and_finds_a_bucket():
    """A registry entry with no signal table sorts every case into NO bucket."""
    case = {
        "problem_list": [
            {"condition": "Chronic portal vein thrombosis with cavernous transformation"},
            {"condition": "Portal biliopathy with recurrent obstructive jaundice"}],
        "lab_panels": [{"panel": "LFT", "collected_offset_days": 0, "results": [
            {"analyte": "Bilirubin (total)", "value": 17.77, "unit": "mg/dL", "flag": "H"},
            {"analyte": "Gamma-glutamyl transferase", "value": 123, "unit": "U/L", "flag": "H"},
            {"analyte": "Alkaline phosphatase", "value": 289, "unit": "U/L", "flag": "H"}]}],
        "notes": [{"text": "ERCP with CBD stent across a distal stricture; brisk bile "
                           "flow. Splenomegaly with hypersplenism. Jaundice improving."}],
    }
    specialty, confidence, scores = real_cases.infer_specialty(case)
    assert specialty == "hepatology" and confidence > 0.5
    bucket, subtopic = real_cases.classify_case_to_bucket(case, "hepatology")
    assert bucket in asc_specialties.get_specialty_config("hepatology").bucket_ids()
    assert subtopic


def test_an_aki_in_cirrhosis_chart_still_routes_to_nephrology():
    """§1.1: patient-3 goes to nephrology because that is where its question lives.
    Registering hepatology must not swallow every chart that mentions a liver."""
    case = {
        "problem_list": [{"condition": "Chronic liver disease with ascites"},
                         {"condition": "Acute kidney injury"}],
        "lab_panels": [{"panel": "Renal", "collected_offset_days": 0, "results": [
            {"analyte": "Creatinine", "value": 1.6, "unit": "mg/dL", "flag": "H"},
            {"analyte": "Potassium", "value": 5.2, "unit": "mmol/L", "flag": "H"},
            {"analyte": "Urea", "value": 60, "unit": "mg/dL", "flag": "H"}]}],
        "notes": [{"text": "AKI in cirrhosis: pre-renal versus hepatorenal syndrome. "
                           "Creatinine responding to volume repletion. Terlipressin?"}],
    }
    assert real_cases.infer_specialty(case)[0] == "nephrology"


# ═════════════════════════════════════════════════════════════════════════════
# Provenance headers  (§0.2, §1.2)
# ═════════════════════════════════════════════════════════════════════════════
def test_a_note_carrying_the_partner_deid_header_ingests_without_quarantine():
    dirty = _NOTE + "\n" + _PROVENANCE_LINE
    cases, _ = _ingest({"manifest.json": _manifest(), "labs.csv": _CSV, "note.txt": dirty})
    assert [c["status"] for c in cases] == ["ingested"], [
        c.get("report", {}).get("quarantine_reason") for c in cases]


def test_the_stripped_header_is_reported_not_silently_dropped():
    """Stripping without reporting is how a partner keeps shipping the same leak."""
    dirty = _NOTE + "\n" + _PROVENANCE_LINE
    cases, _ = _ingest({"manifest.json": _manifest(), "labs.csv": _CSV, "note.txt": dirty})
    report = cases[0]["report"]
    timeline = report["timeline"]
    assert timeline["provenance_lines_stripped"] >= 1
    assert timeline["provenance_header_dates"], "the leaked date must be reported"
    # MASKED — a report may never carry a cleartext identifier.
    assert all("•" in tok for tok in timeline["provenance_header_dates"])
    # Surfaced to a human as an ADVISORY, which is the whole distinction.
    reasons = {r["reason"]: r for r in (cases[0].get("review") or [])}
    assert reasons["provenance_header_dates"]["severity"] == "advisory"
    # The header itself is gone from the note the physician reads.
    assert "De-identification:" not in json.dumps(cases[0]["case"]["notes"])


def test_a_real_unshifted_date_in_CLINICAL_text_still_quarantines():
    """The exemption is for the header, not for the note. Do not weaken this."""
    dirty = _NOTE + "\nSeen again on 3/14 with worsening jaundice."
    cases, _ = _ingest({"manifest.json": json.dumps(
        {"patient_key": "p1", "specialty": "hepatology"}), "labs.csv": _CSV,
        "note.txt": dirty})
    assert cases[0]["status"] == "quarantined"
    assert "unresolved date-like tokens" in cases[0]["report"]["quarantine_reason"]


def test_a_gcs_out_of_fifteen_is_still_exempt():
    """Regression on the repair that already landed (§0.2) — it must keep working."""
    text = "Neuro obs: GCS 10/15 (E3 V3 M4), pupils equal and reactive."
    assert asc_timeline._datelike_unresolved(text) == []
    assert asc_timeline.datelike_leftovers_in_text(text) == []


def test_the_scrub_recheck_applies_the_same_provenance_exemption():
    """A scrub that still counted the header would hold the chart forever: scrub
    cannot remove a line the pipeline already removes."""
    assert asc_timeline.datelike_leftovers_in_text(_PROVENANCE_LINE) == []
    # …while the same date in clinical prose is still a leftover.
    assert asc_timeline.datelike_leftovers_in_text("Reviewed 11/7/21 in clinic.")


def test_curate_notes_strips_the_header_and_counts_it():
    notes = [{"text": _NOTE + "\n" + _PROVENANCE_LINE}]
    out, stats = real_cases.curate_notes(notes)
    assert stats["provenance_lines_stripped"] == 1
    assert "De-identification:" not in out[0]["text"]
    assert "post-ERCP pancreatitis" in out[0]["text"]     # clinical content untouched


@pytest.mark.parametrize("line", [
    "De-identification: year as printed (11/7/21)",
    "De-Identified: names removed",
    "  redaction: green redaction applied 2021-07-11",
    "Anonymisation: MRN suppressed",
    "Anonymization: dates shifted",
])
def test_the_header_shapes_the_partner_actually_writes_are_all_matched(line):
    assert asc_timeline.provenance_lines(line) == [line.rstrip("\n")]
    assert asc_timeline.strip_provenance_lines(line).strip() == ""


def test_a_progress_note_that_merely_mentions_deidentification_is_untouched():
    """Line-anchored on ``<word>:``, so prose about de-identification survives."""
    text = "Discussed de-identification of the research consent with the patient."
    assert asc_timeline.strip_provenance_lines(text) == text


def test_a_note_with_no_header_is_returned_byte_for_byte():
    """The blank-line tidy exists to close the gap a removed header leaves. It must
    never run on a note we did not edit — that rewrites a clinician's own spacing."""
    text = "Impression.\n\n\n\nPlan: continue current therapy.\n\n\n"
    assert asc_timeline.strip_provenance_lines(text) == text
    out, stats = real_cases.curate_notes([{"text": text + _NOTE}])
    assert out[0]["text"].startswith("Impression.\n\n\n\nPlan")
    assert stats["provenance_lines_stripped"] == 0


# ═════════════════════════════════════════════════════════════════════════════
# Lab curation  (§2, §2.1)
# ═════════════════════════════════════════════════════════════════════════════
def test_a_polluted_unit_is_split_and_the_value_is_kept():
    """§2: the original pattern caught 0 of 42 — every one starts with a real unit."""
    unit, recovered = real_cases.split_polluted_unit("mmol/L (19 - 24) — low")
    assert unit == "mmol/L" and recovered == "19 - 24"
    # And the row survives curation with the clean unit.
    out, stats = real_cases.curate_lab_panels([{
        "panel": "BMP", "collected_offset_days": 0, "results": [
            {"analyte": "- Bicarbonate", "value": 15.6,
             "unit": "mmol/L (19 - 24) — low", "flag": ""}]}])
    result = out[0]["results"][0]
    assert result["value"] == 15.6 and result["unit"] == "mmol/L"
    assert stats["units_split"] == 1 and stats["ranges_recovered"] == 1


def test_the_old_broken_unit_pattern_really_did_miss_these():
    """Pins the PRD's measurement so a future 'simplification' cannot undo it."""
    assert real_cases._BROKEN_UNIT_RE.match("mmol/L (19 - 24) — low") is None
    assert real_cases.keep_lab_result(
        {"analyte": "Bicarbonate", "value": 15.6, "unit": "mmol/L (19 - 24) — low"})


def test_a_clean_unit_is_left_exactly_alone():
    for unit in ("mmol/L", "10^3/uL", "mg/dL", "%", "U/L", "ng/mL"):
        assert real_cases.split_polluted_unit(unit) == (unit, None)


def test_a_unit_cell_that_is_only_a_range_is_not_a_unit():
    """OCR put a bare reference range in the unit column. Returning it unchanged
    would let a range ride into the case pretending to be a unit."""
    assert real_cases.split_polluted_unit("(19 - 24)") == ("", "19 - 24")
    assert real_cases.split_polluted_unit("— low") == ("", None)
    assert real_cases.keep_lab_result(
        {"analyte": "Bicarbonate", "value": 15.6, "unit": "(19 - 24)"}) is False


def test_a_date_in_a_reference_range_nulls_the_range_keeps_the_value_flags_the_row():
    """§2 rule 2. Do not attempt to parse 0.25-08-2021 into anything."""
    from asclepius.adapters import lab_csv

    csv = ("patient_key,panel,analyte,value,unit,ref_range,collected_at\n"
           "p1,CHEM,Serum Lactate,2.77,mmol/L,(0.25-08-2021),2031-03-19\n")
    fragment = lab_csv.parse(csv, specialty="nephrology", manifest={})
    result = fragment["lab_panels"][0]["results"][0]
    assert result["value"] == 2.77                      # never drop the number
    assert result.get("ref_low") is None and result.get("ref_high") is None
    assert result["ref_range_unusable"] is True         # …and say why


def test_derive_flag_never_invents_a_flag_for_a_row_whose_range_we_threw_away():
    """§2 rule 3: a flag from a repaired range is a silent clinical claim."""
    assert real_cases.derive_flag(
        {"analyte": "Serum Lactate", "value": 2.77, "unit": "mmol/L",
         "ref_low": None, "ref_high": None, "ref_range_unusable": True}) == ""
    # And a range the partner actually supplied still derives normally.
    assert real_cases.derive_flag(
        {"analyte": "Potassium", "value": 5.9, "unit": "mmol/L",
         "ref_low": 3.5, "ref_high": 5.1}) == "H"


def test_a_recovered_range_never_reaches_ref_low_or_ref_high():
    """The structural reason rule 3 holds: derive_flag cannot see what is not there."""
    out, _ = real_cases.curate_lab_panels([{
        "panel": "BMP", "collected_offset_days": 0, "results": [
            {"analyte": "Bicarbonate", "value": 15.6,
             "unit": "mmol/L (19 - 24) — low", "flag": ""}]}])
    result = out[0]["results"][0]
    assert result.get("ref_low") is None and result.get("ref_high") is None
    assert result["flag"] == ""


def test_the_unsurvivable_bicarbonate_is_dropped_and_the_chart_is_not_quarantined():
    """§2.1: 1.7 mmol/L is an OCR artifact contradicted by the same day's ABG."""
    out, stats = real_cases.curate_lab_panels([{
        "panel": "BMP", "collected_offset_days": 0, "results": [
            {"analyte": "- Bicarbonate", "value": 15.6, "unit": "mmol/L",
             "ref_low": 19, "ref_high": 24},
            {"analyte": "- Bicarbonate", "value": 1.7, "unit": "mmol/L",
             "ref_low": 19, "ref_high": 24}]}])
    kept = [r["value"] for r in out[0]["results"]]
    assert kept == [15.6]                     # the artifact is gone…
    assert stats["implausible_value"] == 1    # …and counted, never silent
    assert out                                # …and the chart survives


@pytest.mark.parametrize("analyte,value,implausible", [
    ("Bicarbonate", 1.7, True), ("Bicarbonate", 15.6, False), ("HCO3-", 1.7, True),
    ("pH", 7.392, False), ("pH", 1.2, True),
    ("Potassium", 5.84, False), ("Potassium", 0.4, True),
    ("Sodium", 131.3, False), ("Sodium", 12.0, True),
    # Not in the table: an alarming value must never be deleted for being alarming.
    ("Troponin I", 0.855, False), ("Bilirubin (total)", 17.77, False),
    ("Gamma-glutamyl transferase", 1361, False), ("Haemoglobin", 5.4, False),
])
def test_the_plausibility_table_only_removes_the_impossible(analyte, value, implausible):
    assert real_cases.implausible_value(
        {"analyte": analyte, "value": value, "unit": "x"}) is implausible


def test_an_implausible_value_is_dropped_even_when_the_lab_coded_it():
    """A LOINC does not make a number survivable."""
    assert real_cases.keep_lab_result(
        {"analyte": "Bicarbonate", "value": 1.7, "unit": "mmol/L", "loinc": "1963-8"}) is False


# ═════════════════════════════════════════════════════════════════════════════
# Unification — REGRESSION ONLY (§0.3, §8: do not touch)
# ═════════════════════════════════════════════════════════════════════════════
def test_one_patient_across_fhir_hl7_notes_and_csv_is_exactly_one_case():
    per_patient = {"ehr-1-patient": [{"a": 1}], "hl7-abc123": [{"b": 2}],
                   "default": [{"c": 3}]}
    sources = {"ehr-1-patient": "fhir_r4", "hl7-abc123": "hl7v2", "default": "default"}
    out, report = asc_ingestion.unify_patient_keys(per_patient, sources)
    assert list(out) == ["ehr-1-patient"] and len(out["ehr-1-patient"]) == 3
    assert report["unified"] is True and report["into_source"] == "fhir_r4"


def test_two_distinct_fhir_patient_ids_are_never_unified():
    """Merging two real patients is far worse than splitting one."""
    out, report = asc_ingestion.unify_patient_keys(
        {"pat-a": [{"a": 1}], "pat-b": [{"b": 2}]},
        {"pat-a": "fhir_r4", "pat-b": "fhir_r4"})
    assert set(out) == {"pat-a", "pat-b"} and report["unified"] is False


def test_a_manifest_patient_key_short_circuits_unification_entirely():
    per_patient = {"ehr-1-patient": [{"a": 1}]}
    out, report = asc_ingestion.unify_patient_keys(
        per_patient, {"ehr-1-patient": "manifest"},
        manifest={"patient_key": "ehr-1-patient"})
    assert out == per_patient and report is None


# ═════════════════════════════════════════════════════════════════════════════
# Fan-out  (§4)
# ═════════════════════════════════════════════════════════════════════════════
def test_max_labels_defaults_to_one():
    from asclepius.schemas import PromoteCaseRequest, UploadPromoteRequest

    assert PromoteCaseRequest(question="q").max_labels == 1
    assert UploadPromoteRequest().max_labels == 1
    assert _store().insert_task(prompt="p", specialty="nephrology")["max_labels"] == 1


def test_open_to_all_specialties_is_off_by_default():
    """Specialty routing is a quality control. It is never suspended by accident."""
    from asclepius.schemas import PromoteCaseRequest

    assert PromoteCaseRequest(question="q").open_to_all_specialties is False
    task = _store().insert_task(prompt="p", specialty="nephrology")
    assert task["open_to_all_specialties"] is False


def _evaluator(specialty, *, real=True):
    st = _store()
    ev = A.make_user(st, role="evaluator", specialty=specialty,
                     board_cert=f"board_certified_{specialty}", years_experience=12)
    if real:
        st.set_real_data_approved(ev["id"], True)
    return st.get_user_by_id(ev["id"])


def test_specialty_routing_applies_when_the_flag_is_off():
    st = _store()
    st.insert_task(task_id="t-neph", prompt="p", specialty="nephrology",
                   case={"case_source": "synthetic", "lab_panels": [{"panel": "x"}]})
    cardiologist = _evaluator("cardiology", real=False)
    seen = {t["task_id"] for t in st.eligible_tasks_for_evaluator(
        evaluator_id=cardiologist["id"], specialty="cardiology")}
    assert "t-neph" not in seen


def test_open_to_all_specialties_makes_a_task_visible_across_specialties():
    st = _store()
    st.insert_task(task_id="t-open", prompt="p", specialty="nephrology",
                   case={"case_source": "synthetic", "lab_panels": [{"panel": "x"}]},
                   open_to_all_specialties=True)
    cardiologist = _evaluator("cardiology", real=False)
    seen = {t["task_id"] for t in st.eligible_tasks_for_evaluator(
        evaluator_id=cardiologist["id"], specialty="cardiology")}
    assert "t-open" in seen


def test_the_fanout_flag_does_not_change_what_we_pay():
    """VISIBLE to everyone and LABELLED by everyone are different things."""
    st = _store()
    task = st.insert_task(task_id="t-open2", prompt="p", specialty="nephrology",
                          max_labels=3, open_to_all_specialties=True)
    assert task["max_labels"] == 3          # exactly as promoted, not widened
    assert task["open_to_all_specialties"] is True


def test_fanout_never_lets_a_real_case_cross_the_v4_wall():
    """Widening VISIBILITY must not widen anything else. This is the one that
    would be a breach rather than a bug."""
    st = _store()
    st.insert_task(task_id="t-real", prompt="p", specialty="nephrology",
                   case={"case_source": "real_deid", "lab_panels": [{"panel": "x"}]},
                   open_to_all_specialties=True)
    cardiologist = _evaluator("cardiology")
    v3 = {t["task_id"] for t in st.eligible_tasks_for_evaluator(
        evaluator_id=cardiologist["id"], specialty="cardiology", real_only=False)}
    assert "t-real" not in v3               # never served to a non-real session
    v4 = {t["task_id"] for t in st.eligible_tasks_for_evaluator(
        evaluator_id=cardiologist["id"], specialty="cardiology", real_only=True)}
    assert "t-real" in v4                   # …and IS visible on the real queue


def test_promote_carries_the_fanout_flag_onto_the_task(monkeypatch):
    _stub_promote_llm(monkeypatch)
    cases, _ = _ingest({"manifest.json": _manifest(), "labs.csv": _CSV, "note.txt": _NOTE})
    ic = [c for c in cases if c["status"] == "ingested"][0]
    r = client.post(f"/api/asclepius/ingestion/cases/{ic['ingest_case_id']}/promote",
                    json={"question": "Next step?", "max_labels": 3,
                          "open_to_all_specialties": True}, headers=_admin_h())
    assert r.status_code == 200, r.text
    task = _store().get_task(r.json()["task_id"])
    assert task["open_to_all_specialties"] is True and task["max_labels"] == 3


# ═════════════════════════════════════════════════════════════════════════════
# Dry run  (§5.1)
# ═════════════════════════════════════════════════════════════════════════════
def _dry_run(ic, admin_h, **body):
    payload = {"question": "Nineteen days after ERCP stenting, what is your next step?"}
    payload.update(body)
    return client.post(
        f"/api/asclepius/ingestion/cases/{ic['ingest_case_id']}/promote?dry_run=true",
        json=payload, headers=admin_h)


def test_a_dry_run_writes_nothing(monkeypatch):
    _stub_promote_llm(monkeypatch)
    admin_h = _admin_h()
    cases, _ = _ingest({"manifest.json": _manifest(), "labs.csv": _CSV, "note.txt": _NOTE})
    ic = [c for c in cases if c["status"] == "ingested"][0]
    before = len(_store().list_tasks(limit=1000))

    r = _dry_run(ic, admin_h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True and body["committed"] is False and body["task_id"] is None
    # No task, case untouched, no case_promoted event.
    assert len(_store().list_tasks(limit=1000)) == before
    assert _store().get_ingest_case(ic["ingest_case_id"])["status"] == "ingested"
    events = _store().list_events(entity_type="ingest_case",
                                  entity_id=ic["ingest_case_id"])
    assert not [e for e in events if e["event_type"] == "case_promoted"]
    assert [e for e in events if e["event_type"] == "promote_dry_run"]


def test_a_dry_run_returns_the_candidates_and_the_judge_scores(monkeypatch):
    _stub_promote_llm(monkeypatch)
    cases, _ = _ingest({"manifest.json": _manifest(), "labs.csv": _CSV, "note.txt": _NOTE})
    ic = [c for c in cases if c["status"] == "ingested"][0]
    sample = _dry_run(ic, _admin_h()).json()["sample"]
    assert len(sample["candidates"]) == 2
    assert sample["judges"]["case_judge"]["coherence"] == 0.9
    assert sample["judges"]["hardness"]["score"] == 0.85
    assert sample["prompt"] and sample["difficulty"]["band"]
    # The PUBLIC case — the answer key never rides along on a preview.
    assert "ground_truth" not in sample["case"]


def test_a_dry_run_is_idempotent(monkeypatch):
    _stub_promote_llm(monkeypatch)
    admin_h = _admin_h()
    cases, _ = _ingest({"manifest.json": _manifest(), "labs.csv": _CSV, "note.txt": _NOTE})
    ic = [c for c in cases if c["status"] == "ingested"][0]
    for _ in range(3):
        assert _dry_run(ic, admin_h).status_code == 200
    assert _store().get_ingest_case(ic["ingest_case_id"])["status"] == "ingested"
    # …and the real promote still works afterwards.
    r = client.post(f"/api/asclepius/ingestion/cases/{ic['ingest_case_id']}/promote",
                    json={"question": "Next step?"}, headers=admin_h)
    assert r.status_code == 200, r.text


def test_a_dry_run_returns_the_scores_for_a_case_that_FAILED_the_gate(monkeypatch):
    """A 422 carrying no scores tells you nothing about how to sharpen the question."""
    _stub_promote_llm(monkeypatch, coherence=0.2)   # below the floor
    cases, _ = _ingest({"manifest.json": _manifest(), "labs.csv": _CSV, "note.txt": _NOTE})
    ic = [c for c in cases if c["status"] == "ingested"][0]
    r = _dry_run(ic, _admin_h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["would_promote"] is False
    assert any("coherence" in f for f in body["sample"]["failures"])
    assert body["sample"]["judges"]["case_judge"]["coherence"] == 0.2


def test_a_dry_run_still_refuses_brokering_data(monkeypatch):
    """Rendering a brokering case and shipping it to an inference provider is the
    activity the rule exists to prevent, task or no task."""
    _stub_promote_llm(monkeypatch)
    admin_h = _admin_h()
    link = _mint(admin_h, purpose="brokering")
    res = _upload(link["token"], _zip({"manifest.json": _manifest(),
                                       "labs.csv": _CSV, "note.txt": _NOTE}))
    ic = _store().list_ingest_cases(upload_id=res["upload_id"], status="ingested")[0]
    r = _dry_run(ic, admin_h)
    assert r.status_code == 409 and "brokering" in r.json()["detail"].lower()


def test_a_dry_run_still_refuses_an_undetermined_specialty(monkeypatch):
    _stub_promote_llm(monkeypatch)
    cases, _ = _ingest({"labs.csv": _CSV, "note.txt": _NOTE})
    ic = [c for c in cases if c["status"] == "ingested"][0]
    assert _dry_run(ic, _admin_h()).status_code == 409


def test_the_query_parameter_can_only_turn_the_dry_run_on(monkeypatch):
    """A query string must never be able to force a commit the body refused."""
    _stub_promote_llm(monkeypatch)
    cases, _ = _ingest({"manifest.json": _manifest(), "labs.csv": _CSV, "note.txt": _NOTE})
    ic = [c for c in cases if c["status"] == "ingested"][0]
    r = client.post(
        f"/api/asclepius/ingestion/cases/{ic['ingest_case_id']}/promote?dry_run=false",
        json={"question": "Next step?", "dry_run": True}, headers=_admin_h())
    assert r.status_code == 200 and r.json()["committed"] is False


# ═════════════════════════════════════════════════════════════════════════════
# The three V4 cases  (§3, §7)
# ═════════════════════════════════════════════════════════════════════════════
def test_all_three_v4_cases_are_valid_clinical_cases_with_an_ab_pair():
    """The PRD sketched these with `label`, `kind` and `offset_days`; the real
    schema is `condition`, `modality` and `collected_offset_days`, and it is
    extra='forbid' — so the sketch would have raised on every case."""
    entries = v4_cases._validated()
    assert [e["case_id"] for e in entries] == ["v4-hep-001", "v4-neph-001", "v4-card-001"]
    for entry in entries:
        assert entry["case"]["case_source"] == "real_deid"
        assert len(entry["candidate_answers"]) == 2
        assert entry["intended_flawed_id"] in ("A", "B")
        assert entry["question"].strip()
        assert entry["case"]["ground_truth"]["answer"].strip()


def test_the_v4_cases_route_to_the_specialties_the_prd_names():
    by_id = {e["case_id"]: e["case"]["specialty"] for e in v4_cases._validated()}
    assert by_id == {"v4-hep-001": "hepatology", "v4-neph-001": "nephrology",
                     "v4-card-001": "cardiology"}
    for specialty in set(by_id.values()):
        assert asc_specialties.is_enabled(specialty)


def test_no_v4_case_ships_a_post_decision_panel():
    """A panel dated after the decision point IS the answer (§3, held-out rule)."""
    for entry in v4_cases._validated():
        for collection in ("lab_panels", "notes", "studies"):
            for item in entry["case"].get(collection) or []:
                assert item.get("collected_offset_days", 0) <= 0, (
                    f"{entry['case_id']}: {collection} leaks a post-decision item")


def test_no_v4_case_uses_the_corrupted_bicarbonate():
    """§2.1: 'Do not build a case on the 1.7.'"""
    for entry in v4_cases._validated():
        for panel in entry["case"].get("lab_panels") or []:
            for result in panel.get("results") or []:
                assert not real_cases.implausible_value(result), (
                    f"{entry['case_id']}: {result['analyte']} = {result['value']}")


def test_no_v4_case_ships_a_polluted_unit():
    """§7.7: zero polluted units in the promoted case bodies."""
    for entry in v4_cases._validated():
        for panel in entry["case"].get("lab_panels") or []:
            for result in panel.get("results") or []:
                unit = result.get("unit")
                assert real_cases.split_polluted_unit(unit) == ((unit or "").strip(), None)


def test_the_v4_case_answer_key_never_reaches_the_public_case():
    from asclepius.cases import public_case

    for entry in v4_cases._validated():
        public = public_case(entry["case"])
        for key in ("ground_truth", "hard_hook", "reasoning_divergence"):
            assert key not in public


def test_loading_the_v4_cases_creates_partner_ehr_tasks_at_three_labels():
    """§7.5/§7.8: real_deid tasks, routed to their own specialty, max_labels = 3."""
    st = _store()
    res = v4_cases.load_v4_cases(st)
    assert res["loaded"] >= 2
    for task_id in res["task_ids"]:
        task = st.get_task(task_id)
        assert task["case_source"] == "real_deid"
        assert task["source"] == "partner_ehr"
        assert task["modality"] == "multimodal"
        assert task["max_labels"] == v4_cases.V4_DEFAULT_MAX_LABELS == 3
        assert task["open_to_all_specialties"] is False   # routing still applies
        assert len(task["candidate_answers"]) == 2
        assert task["case"]["specialty"] == task["specialty"]


def test_loading_the_v4_cases_is_idempotent():
    st = _store()
    first = v4_cases.load_v4_cases(st)
    second = v4_cases.load_v4_cases(st)
    assert second["loaded"] == 0 and second["skipped"] == first["loaded"]


def test_a_specialty_filter_loads_only_that_specialtys_cases():
    st = _store()
    res = v4_cases.load_v4_cases(st, specialty="hepatology")
    assert res["task_ids"] == [v4_cases.v4_task_id("v4-hep-001")]


def test_a_held_case_is_named_rather_than_silently_missing():
    """A case silently absent from the queue is the failure this PRD is about.
    Case C is held on the cardiology ECG requirement (see v4_cases.CASE_C)."""
    holds = v4_cases.V4_HOLDS()
    res = v4_cases.load_v4_cases(_store())
    assert set(res["holds"]) == set(holds)
    for case_id, reason in holds.items():
        assert reason and case_id not in [t.split("v4real-")[-1] for t in res["task_ids"]]


def test_attaching_the_missing_study_releases_a_held_case(monkeypatch):
    """The hold is a statement about the DATA, and it lifts the moment the data
    arrives — no code change, no restart."""
    if "v4-card-001" not in v4_cases.V4_HOLDS():
        pytest.skip("v4-card-001 is no longer held; the real ECG has been attached")
    patched = dict(v4_cases.CASE_C)
    patched["studies"] = [{
        "modality": "ecg", "label": "12-lead ECG", "collected_offset_days": 0,
        "findings": "Sinus rhythm at 94/min. No ST elevation or depression. "
                    "T waves unremarkable. QTc 428 ms.",
    }]
    entry = dict(next(e for e in v4_cases.V4_REAL_CASES if e["case_id"] == "v4-card-001"))
    entry["case"] = patched
    monkeypatch.setattr(v4_cases, "_validated", lambda: [entry])
    res = v4_cases.load_v4_cases(_store(), specialty="cardiology")
    assert res["holds"] == {} and res["loaded"] == 1


def test_the_v4_cases_reach_an_approved_physicians_queue():
    """§7.8: visible to every approved physician in its specialty."""
    st = _store()
    v4_cases.load_v4_cases(st)
    hepatologist = _evaluator("hepatology")
    headers = A.headers_for(hepatologist)
    body = client.get("/api/asclepius/tasks/next?portal_version=v4&specialty=hepatology",
                      headers=headers).json()
    assert body["task"] is not None
    assert body["task"]["task_id"] == v4_cases.v4_task_id("v4-hep-001")


def test_an_empty_v4_queue_seeds_itself_rather_than_showing_nothing():
    """No explicit load: the draw itself fills an empty V4 queue."""
    headers = A.headers_for(_evaluator("nephrology"))
    body = client.get("/api/asclepius/tasks/next?portal_version=v4&specialty=nephrology",
                      headers=headers).json()
    assert body["task"] is not None
    assert (body["task"].get("case") or {}).get("case_source") == "real_deid"


def test_an_unapproved_physician_never_sees_a_v4_real_case():
    st = _store()
    v4_cases.load_v4_cases(st)
    headers = A.headers_for(_evaluator("nephrology", real=False))
    body = client.get("/api/asclepius/tasks/next?portal_version=v4&specialty=nephrology",
                      headers=headers).json()
    assert body["task"] is None


def test_the_admin_load_endpoint_reports_holds_alongside_loads():
    r = client.post("/api/asclepius/generation/load-v4-real-cases", headers=_admin_h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["max_labels"] == 3 and body["open_to_all_specialties"] is False
    assert body["loaded"] + body["held"] + body["skipped"] == body["total"]
    assert set(body["holds"]) == set(v4_cases.V4_HOLDS())


def test_the_admin_load_endpoint_refuses_an_unenabled_specialty():
    r = client.post("/api/asclepius/generation/load-v4-real-cases?specialty=neurology",
                    headers=_admin_h())
    assert r.status_code == 400 and "neurology" in json.dumps(r.json())


def test_the_admin_load_endpoint_can_fan_out_without_changing_the_label_count():
    r = client.post("/api/asclepius/generation/load-v4-real-cases"
                    "?open_to_all_specialties=true", headers=_admin_h())
    assert r.status_code == 200, r.text
    st = _store()
    for task_id in r.json()["task_ids"]:
        task = st.get_task(task_id)
        assert task["open_to_all_specialties"] is True
        assert task["max_labels"] == 3      # visibility widened, cost unchanged
