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
    ("Bicarbonate", 1.7, True), ("- Bicarbonate", 1.7, True), ("HCO3-", 1.7, True),
    ("Bicarbonate", 15.6, False),
    ("Arterial pH", 7.392, False), ("Arterial pH", 1.2, True),
    ("Potassium", 5.84, False), ("Potassium", 0.4, True), ("Potassium", 45.0, True),
    ("Sodium", 131.3, False), ("Sodium", 12.0, True),
    # Not in the table: an alarming value must never be deleted for being alarming.
    ("Troponin I", 0.855, False), ("Bilirubin (total)", 17.77, False),
    ("Gamma-glutamyl transferase", 1361, False), ("Haemoglobin", 5.4, False),
])
def test_the_plausibility_table_only_removes_the_impossible(analyte, value, implausible):
    assert real_cases.implausible_value(
        {"analyte": analyte, "value": value, "unit": "x"}) is implausible


@pytest.mark.parametrize("analyte,value,panel", [
    # A urine panel writes all three of these, and every one of them was deleted
    # as "impossible" while the table matched bare abbreviations against serum
    # bounds. Urine Na is the pre-renal vs ATN datum — the single value this
    # product's nephrology cases turn on.
    ("pH", 5.0, None), ("K", 45.0, None), ("Na", 20.0, None),
    ("Sodium", 20.0, "Urine studies"), ("Potassium", 45.0, "Urine electrolytes"),
    ("Urine sodium", 18.0, None), ("pH", 4.6, "Urinalysis"),
])
def test_a_non_serum_specimen_is_never_judged_against_serum_bounds(analyte, value, panel):
    assert real_cases.implausible_value(
        {"analyte": analyte, "value": value, "unit": "mmol/L"}, panel_name=panel) is False


@pytest.mark.parametrize("analyte,value", [
    ("Potassium", 9.4),      # dialysis emergency: measured, reported, acted on
    ("Sodium", 105.0),       # severe hyponatraemia
    ("Bicarbonate", 4.0),    # extreme DKA
    ("Arterial pH", 6.8),    # severe acidaemia
])
def test_an_extreme_but_real_value_is_never_deleted_for_being_extreme(analyte, value):
    """These ARE the decisive data of a hard case. A bound tight enough to delete
    them is worse than shipping the artifact it was added to catch."""
    assert real_cases.implausible_value({"analyte": analyte, "value": value,
                                         "unit": "mmol/L"}) is False


def test_the_urine_panel_survives_curation_intact():
    """The end-to-end version of the two tests above — the bug was that a second,
    panel-blind plausibility check in ``keep_lab_result`` dropped these rows even
    after the panel-aware one had correctly kept them."""
    out, stats = real_cases.curate_lab_panels([{
        "panel": "Urine studies", "collected_offset_days": 0, "results": [
            {"analyte": "Sodium", "value": 20, "unit": "mmol/L",
             "ref_low": 20, "ref_high": 40},
            {"analyte": "Potassium", "value": 45, "unit": "mmol/L",
             "ref_low": 20, "ref_high": 80}]}])
    assert [r["analyte"] for r in out[0]["results"]] == ["Sodium", "Potassium"]
    assert stats["implausible_value"] == 0


def test_an_implausible_value_is_dropped_even_when_the_lab_coded_it():
    """A LOINC does not make a number survivable — but the check lives in
    curation, where the panel (and so the specimen) is known."""
    out, stats = real_cases.curate_lab_panels([{
        "panel": "BMP", "collected_offset_days": 0, "results": [
            {"analyte": "Bicarbonate", "value": 1.7, "unit": "mmol/L", "loinc": "1963-8"},
            {"analyte": "Bicarbonate", "value": 15.6, "unit": "mmol/L", "loinc": "1963-8"}]}])
    assert [r["value"] for r in out[0]["results"]] == [15.6]
    assert stats["implausible_value"] == 1


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


def _evaluator(specialty, *, real=True, approved=True):
    st = _store()
    ev = A.make_user(st, role="evaluator", specialty=specialty,
                     board_cert=f"board_certified_{specialty}", years_experience=12)
    if approved:
        # A physician who can actually draw work: verified AND labeling. The
        # real-case access report reads both, so a fixture that skipped
        # verification would make "no blockers" unreachable for any account.
        st.set_verification_status(ev["id"], "approved")
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


def test_all_three_cases_ship_and_a_missing_study_is_named_not_hidden():
    """A cardiology case normally needs an ECG/echo, and patient-4's bundle has
    none. Fabricating a tracing inside a ``real_deid`` record, or relabelling the
    case into a specialty that does not describe it, are both worse than shipping
    a real case with a named gap — so the requirement is advisory for real charts
    and the gap is REPORTED."""
    res = v4_cases.load_v4_cases(_store())
    assert res["held"] == 0 and res["holds"] == {}
    assert res["loaded"] == 3
    gaps = res["study_gaps"]
    assert "v4-card-001" in gaps and "ecg" in gaps["v4-card-001"].lower()
    # It is in the queue, not merely valid in the abstract.
    assert _store().get_task(v4_cases.v4_task_id("v4-card-001")) is not None


def test_the_study_requirement_is_still_hard_for_an_AUTHORED_case():
    """The advisory is for real charts only. A synthetic cardiology case without a
    tracing is an authoring bug — the generator could always have produced one."""
    from asclepius.cases import MultimodalContentError, assert_multimodal_content

    case = {
        "specialty": "cardiology", "studies": [],
        "problem_list": [{"condition": "chest pain"}],
        "medications": [{"drug": "aspirin"}],
        "notes": [{"text": "x" * 250}],
        "lab_panels": [{"panel": "cardiac", "results": [
            {"analyte": "Troponin I", "value": 0.9, "unit": "ng/mL", "ref_high": 0.04},
            {"analyte": "Sodium", "value": 137, "unit": "mmol/L", "ref_low": 136, "ref_high": 145}]}],
    }
    with pytest.raises(MultimodalContentError):
        assert_multimodal_content({**case, "case_source": "synthetic"})
    assert_multimodal_content({**case, "case_source": "real_deid"})   # ships


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
    assert body["max_labels"] == 3
    # Omitted => follows the deployment's configured fan-out, so this route and the
    # boot seeder can never disagree about who sees the real cases.
    from asclepius.constants import v4_open_to_all_specialties
    assert body["open_to_all_specialties"] is v4_open_to_all_specialties()
    assert body["loaded"] + body["held"] + body["skipped"] == body["total"]
    assert set(body["holds"]) == set(v4_cases.V4_HOLDS())


def test_the_admin_load_endpoint_still_takes_an_explicit_fan_out_override():
    """The setting is the default, not a ceiling: an operator can still force
    either behaviour for one call without touching the environment."""
    for want in (True, False):
        r = client.post(
            f"/api/asclepius/generation/load-v4-real-cases?open_to_all_specialties={str(want).lower()}",
            headers=_admin_h())
        assert r.status_code == 200, r.text
        assert r.json()["open_to_all_specialties"] is want
        for e in v4_cases.V4_REAL_CASES:
            t = _store().get_task(v4_cases.v4_task_id(e["case_id"]))
            assert bool(t["open_to_all_specialties"]) is want


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


# ═════════════════════════════════════════════════════════════════════════════
# Reaching the dashboard  (the real cases must EXIST and be SERVED first)
# ═════════════════════════════════════════════════════════════════════════════
# Reported from the running product: "on my admin side I don't see it being
# pushed, and when I open a case it says synthetic multimodal." Four separate
# causes, one per test below. Each was individually sufficient to produce
# exactly that symptom, so each gets its own regression.
def test_the_real_cases_exist_without_anyone_having_drawn_them():
    """CAUSE 1. Seeding used to hang off a physician's V4 draw, so until someone
    cleared approval AND picked the real-cases experience AND had an empty queue,
    the tasks did not exist — an admin looking for them correctly saw nothing,
    with no way to tell 'not loaded' from 'nothing to load'."""
    st = _store()
    assert [t for t in st.list_tasks(limit=200) if t.get("case_source") == "real_deid"] == []
    v4_cases.load_v4_cases(st)          # what the startup hook now does at boot
    real = [t for t in st.list_tasks(limit=200) if t.get("case_source") == "real_deid"]
    assert real, "no real cases after seeding"
    assert all(t["source"] == "partner_ehr" for t in real)


def test_seeding_loads_every_specialty_not_just_the_one_being_drawn():
    """CAUSE 2. The lazy seeder filtered by the drawn specialty, so a
    nephrologist's draw created the nephrology case and left hepatology
    uncreated: which real cases existed depended on who logged in first."""
    from routers import asclepius as R

    st = _store()
    user = {"specialty": "nephrology"}
    R._ensure_v4_real_cases(st, user, "nephrology")
    specialties = {t["specialty"] for t in st.list_tasks(limit=200)
                   if t.get("case_source") == "real_deid"}
    # Drawing as a nephrologist must still have created the hepatology case.
    assert "hepatology" in specialties, specialties
    assert "nephrology" in specialties


def test_an_approved_physician_is_served_a_REAL_case_not_a_synthetic_one():
    """CAUSE 3, server half. The doctor's complaint was that the case they opened
    said 'synthetic multimodal' — that is the v3 queue, which is what the portal
    defaulted everyone to. On v4 they must get real data."""
    st = _store()
    v4_cases.load_v4_cases(st)
    headers = A.headers_for(_evaluator("nephrology"))
    body = client.get("/api/asclepius/tasks/next?portal_version=v4&specialty=nephrology",
                      headers=headers).json()
    assert body["task"] is not None
    assert (body["task"].get("case") or {}).get("case_source") == "real_deid"


def test_auth_me_carries_real_data_approval_so_the_portal_can_default_to_v4():
    """CAUSE 3, client half. The frontend defaults an approved contributor to the
    REAL-cases experience instead of the synthetic one, which it can only do if
    the flag reaches it on the endpoint it actually calls (/auth/me)."""
    st = _store()
    ev = _evaluator("nephrology", real=False)
    me = client.get("/api/asclepius/auth/me", headers=A.headers_for(ev)).json()
    assert me["real_data_approved"] is False
    st.set_real_data_approved(ev["id"], True)
    me = client.get("/api/asclepius/auth/me",
                    headers=A.headers_for(st.get_user_by_id(ev["id"]))).json()
    assert me["real_data_approved"] is True


def test_the_admin_roster_exposes_real_data_approval():
    """CAUSE 4. The flag gates the whole real queue and was API-only — not on the
    roster and not in the UI — so the first question an operator asks when the
    real cases are not being labelled ('is anyone cleared to see them?') had no
    answer on screen."""
    st = _store()
    ev = _evaluator("nephrology", real=False)
    admin_h = _admin_h()
    roster = client.get("/api/asclepius/admin/physicians", headers=admin_h).json()["physicians"]
    row = next(p for p in roster if p["id"] == ev["id"])
    assert row["real_data_approved"] is False

    r = client.post(f"/api/asclepius/users/{ev['id']}/real-data-approval",
                    json={"approved": True}, headers=admin_h)
    assert r.status_code == 200 and r.json()["real_data_approved"] is True

    roster = client.get("/api/asclepius/admin/physicians", headers=admin_h).json()["physicians"]
    assert next(p for p in roster if p["id"] == ev["id"])["real_data_approved"] is True
    # …and revoking is the same control, not a one-way door.
    client.post(f"/api/asclepius/users/{ev['id']}/real-data-approval",
                json={"approved": False}, headers=admin_h)
    roster = client.get("/api/asclepius/admin/physicians", headers=admin_h).json()["physicians"]
    assert next(p for p in roster if p["id"] == ev["id"])["real_data_approved"] is False


def test_an_unapproved_physician_still_cannot_reach_a_real_case():
    """Making the cases reachable must not make them reachable to everyone: the
    approval gate is the BAA boundary and none of the above touches it."""
    st = _store()
    v4_cases.load_v4_cases(st)
    headers = A.headers_for(_evaluator("nephrology", real=False))
    assert client.get("/api/asclepius/tasks/next?portal_version=v4&specialty=nephrology",
                      headers=headers).json()["task"] is None
    av = client.get("/api/asclepius/tasks/available?portal_version=v4&specialty=nephrology",
                    headers=headers).json()
    assert av["count"] == 0


# ═════════════════════════════════════════════════════════════════════════════
# The label has to be identifiable as REAL all the way into the buyer bundle
# ═════════════════════════════════════════════════════════════════════════════
def test_a_label_on_a_real_case_is_identifiable_as_real_in_the_export(monkeypatch):
    """The commercial point of a real case is that the buyer can tell it apart
    from a synthetic one. If ``case_source`` does not survive into records.jsonl,
    a physician's twenty minutes on a real chart ships indistinguishable from an
    AI-authored vignette — and the premium it was sold at is unevidenced.

    Walks the whole chain: seed → draw on v4 → submit → record → export."""
    import json
    import os
    import uuid

    from asclepius import pipeline as asc_pipeline
    from asclepius.export import build_export

    async def _ok(*a, **k):
        return {"consistent": True, "issues": [], "skipped": True}

    async def _ok_grounding(*a, **k):
        return {"grounding_ok": True, "issues": [], "skipped": True, "checked_anchors": 0}

    monkeypatch.setattr(asc_pipeline, "run_critic", _ok)
    monkeypatch.setattr(asc_pipeline, "run_grounding_check", _ok_grounding)

    st = _store()
    v4_cases.load_v4_cases(st)
    admin = A.make_user(st, role="admin")
    ev = _evaluator("nephrology")
    headers = A.headers_for(ev)

    task = client.get("/api/asclepius/tasks/next?portal_version=v4&specialty=nephrology",
                      headers=headers).json()["task"]
    assert task is not None
    sid = "s-" + uuid.uuid4().hex[:12]
    r = client.post("/api/asclepius/submissions", headers=headers, json={
        "submission_id": sid, "task_id": task["task_id"], "verdict": "B_better",
        "chosen_id": "B", "rejected_id": "A", "time_spent_sec": 400,
        "portal_version": "v4",
        "prompt_review": {"reviewed": True, "verdict": "valid"},
        "independent_answer": {"text": "Stop transfusing, target 7-8 g/dL; the AKI is "
                                       "pre-renal and volume-responsive, not hepatorenal."},
        "chosen_revision": {"edited": False, "why_better_notes": "names the restrictive threshold"},
        "rejected_critique": {"error_tags": ["unsafe_recommendation"],
                              "why_worse": "endorses continuing to transfuse"},
    })
    assert r.status_code == 200, r.text
    st.update_records_status_for_submission(sid, "export_ready")

    manifest = build_export(st, created_by=admin["id"], profile="default")
    line = json.loads(
        open(os.path.join(manifest["dir_path"], "records.jsonl")).read().splitlines()[0])
    ctx = line.get("context") or {}

    # THE assertion: a buyer can tell this was a real chart.
    assert ctx["case_source"] == "real_deid"
    assert line["portal_version"] == "v4"
    assert ctx["modality"] == "multimodal"
    # …which real case, and which partner bundle it came from.
    assert ctx["case"]["case_id"] == "v4-neph-001"
    assert any("patient-3" in (ref.get("identifier") or "")
               for ref in ctx["case"]["source_refs"])
    # …and who labelled it, by credential, never by name.
    assert line["annotator_credential"] == "board_certified_nephrology"
    assert line["annotator_specialty"] == "nephrology"
    # The answer key never ships.
    assert "ground_truth" not in ctx["case"]
    # The batch manifest counts it as real, which is what the datasheet reports.
    assert (manifest["counts"] or {}).get("by_case_source", {}).get("real_deid") == 1


def test_the_datasheet_tells_the_buyer_the_case_was_real(monkeypatch):
    """Counted in the manifest is not the same as SAID in the datasheet — the
    datasheet is the document a buyer actually reads about provenance."""
    import os
    import uuid

    from asclepius import pipeline as asc_pipeline
    from asclepius.export import build_export

    async def _ok(*a, **k):
        return {"consistent": True, "issues": [], "skipped": True}

    async def _ok_grounding(*a, **k):
        return {"grounding_ok": True, "issues": [], "skipped": True, "checked_anchors": 0}

    monkeypatch.setattr(asc_pipeline, "run_critic", _ok)
    monkeypatch.setattr(asc_pipeline, "run_grounding_check", _ok_grounding)

    st = _store()
    v4_cases.load_v4_cases(st)
    admin = A.make_user(st, role="admin")
    headers = A.headers_for(_evaluator("nephrology"))
    task = client.get("/api/asclepius/tasks/next?portal_version=v4&specialty=nephrology",
                      headers=headers).json()["task"]
    sid = "s-" + uuid.uuid4().hex[:12]
    client.post("/api/asclepius/submissions", headers=headers, json={
        "submission_id": sid, "task_id": task["task_id"], "verdict": "B_better",
        "chosen_id": "B", "rejected_id": "A", "time_spent_sec": 400,
        "portal_version": "v4",
        "prompt_review": {"reviewed": True, "verdict": "valid"},
        "independent_answer": {"text": "Stop transfusing; the AKI is pre-renal."},
        "chosen_revision": {"edited": False, "why_better_notes": "restrictive threshold"},
        "rejected_critique": {"error_tags": ["unsafe_recommendation"], "why_worse": "over-transfuses"},
    })
    st.update_records_status_for_submission(sid, "export_ready")
    manifest = build_export(st, created_by=admin["id"], profile="default")
    datasheet = open(os.path.join(manifest["dir_path"], "datasheet.md")).read()
    # Whitespace-normalised: the datasheet is markdown and hard-wraps, so a raw
    # substring assertion would break on a reflow rather than on a real change.
    flat = " ".join(datasheet.lower().split())
    assert "real_deid" in flat
    assert "case provenance: real_deid" in flat
    assert "de-identified from real encounters" in flat


# ═════════════════════════════════════════════════════════════════════════════
# Real-data approval follows labeling approval
# ═════════════════════════════════════════════════════════════════════════════
def _approve_for_labeling(st, user_id, tier="labeler"):
    st.set_verification_status(user_id, "approved")
    with st._conn() as conn:
        conn.execute("UPDATE users SET tier = ? WHERE id = ?", (tier, user_id))


def test_an_approved_labeling_physician_is_granted_real_data_access():
    """The product's own answer to 'who may see real patient data': a physician
    whose credentials we verified and who we cleared to LABEL. Keeping the two
    flags separate left the real queue gated on something nobody could set."""
    st = _store()
    ev = _evaluator("nephrology", real=False)
    _approve_for_labeling(st, ev["id"])
    assert not st.get_user_by_id(ev["id"])["real_data_approved"]
    out = st.sync_real_data_approval()
    assert out["granted"] == 1
    assert st.get_user_by_id(ev["id"])["real_data_approved"]


def test_a_physician_who_cannot_label_is_not_granted():
    st = _store()
    ev = _evaluator("nephrology", real=False)
    st.set_verification_status(ev["id"], "approved")
    with st._conn() as conn:                      # approved but NO tier
        conn.execute("UPDATE users SET tier = NULL WHERE id = ?", (ev["id"],))
    st.sync_real_data_approval()
    assert not st.get_user_by_id(ev["id"])["real_data_approved"]


def test_an_unapproved_physician_is_not_granted():
    st = _store()
    ev = _evaluator("nephrology", real=False)
    with st._conn() as conn:                      # labeler tier but NOT approved
        conn.execute("UPDATE users SET tier = 'labeler', verification_status = 'pending' "
                     "WHERE id = ?", (ev["id"],))
    st.sync_real_data_approval()
    assert not st.get_user_by_id(ev["id"])["real_data_approved"]


def test_an_admin_revoke_is_never_undone_by_the_sync():
    """THE reason the source column exists. ``real_data_approved`` is NOT NULL
    DEFAULT 0, so a deliberate revoke and 'never considered' are the same 0 — an
    auto-grant that could not tell them apart would hand access straight back to
    someone a human deliberately removed it from, on every boot."""
    st = _store()
    ev = _evaluator("nephrology", real=False)
    _approve_for_labeling(st, ev["id"])
    st.sync_real_data_approval()
    assert st.get_user_by_id(ev["id"])["real_data_approved"]

    st.set_real_data_approved(ev["id"], False, source="admin")   # a human says no
    for _ in range(3):
        st.sync_real_data_approval()
    assert not st.get_user_by_id(ev["id"])["real_data_approved"]


def test_the_auto_grant_is_withdrawn_when_the_physician_stops_qualifying():
    """An auto-grant that outlived the approval it was derived from would leave
    real-data access attached to someone whose tier was removed."""
    st = _store()
    ev = _evaluator("nephrology", real=False)
    _approve_for_labeling(st, ev["id"])
    st.sync_real_data_approval()
    assert st.get_user_by_id(ev["id"])["real_data_approved"]

    with st._conn() as conn:
        conn.execute("UPDATE users SET tier = NULL WHERE id = ?", (ev["id"],))
    out = st.sync_real_data_approval()
    assert out["revoked"] == 1
    assert not st.get_user_by_id(ev["id"])["real_data_approved"]


def test_the_sync_is_idempotent():
    st = _store()
    ev = _evaluator("nephrology", real=False)
    _approve_for_labeling(st, ev["id"])
    assert st.sync_real_data_approval()["granted"] == 1
    assert st.sync_real_data_approval()["granted"] == 0


# ═════════════════════════════════════════════════════════════════════════════
# V4 → V3 continuation
# ═════════════════════════════════════════════════════════════════════════════
def test_the_draw_reports_which_version_it_actually_served():
    """The record is stamped from the SERVED version, not the picked one, because
    the submit path refuses a v4 claim on a synthetic task outright."""
    st = _store()
    v4_cases.load_v4_cases(st)
    headers = A.headers_for(_evaluator("nephrology"))
    body = client.get("/api/asclepius/tasks/next?portal_version=v4&specialty=nephrology",
                      headers=headers).json()
    assert body["served_portal_version"] == "v4"
    assert body["continued_from"] is None       # served what they picked


def test_a_physician_who_finishes_the_real_cases_continues_onto_synthetic():
    """There are a finite number of real charts. Finishing them used to end in
    'queue cleared', which is the wrong end state for someone sitting down to
    work — the synthetic queue is the same task shape and is not empty."""
    import uuid

    from asclepius import pipeline as asc_pipeline
    from asclepius.gold_cases import load_gold_cases

    st = _store()
    v4_cases.load_v4_cases(st)
    load_gold_cases(st, specialty="nephrology")
    ev = _evaluator("nephrology")
    headers = A.headers_for(ev)

    first = client.get("/api/asclepius/tasks/next?portal_version=v4&specialty=nephrology",
                       headers=headers).json()
    assert first["served_portal_version"] == "v4"
    # Consume the only real nephrology case.
    r = client.post("/api/asclepius/submissions", headers=headers, json={
        "submission_id": "s-" + uuid.uuid4().hex[:12], "task_id": first["task"]["task_id"],
        "verdict": "B_better", "chosen_id": "B", "rejected_id": "A",
        "time_spent_sec": 300, "portal_version": first["served_portal_version"],
        "prompt_review": {"reviewed": True, "verdict": "valid"},
        "independent_answer": {"text": "Stop transfusing; the AKI is pre-renal."},
        "chosen_revision": {"edited": False, "why_better_notes": "restrictive threshold"},
        "rejected_critique": {"error_tags": ["unsafe_recommendation"], "why_worse": "over-transfuses"},
    })
    assert r.status_code == 200, r.text

    nxt = client.get("/api/asclepius/tasks/next?portal_version=v4&specialty=nephrology",
                     headers=headers).json()
    assert nxt["task"] is not None, "the physician was left with an empty queue"
    assert nxt["served_portal_version"] == "v3"
    assert nxt["continued_from"] == "v4"        # so the UI can say what happened
    assert (nxt["task"].get("case") or {}).get("case_source") == "synthetic"


def test_the_continued_case_submits_under_the_version_it_was_served_as():
    """The derivation wall refuses a v4 claim on a synthetic task. If the client
    stamped from the picker instead of the served version, every continued case
    would 400 — so this asserts the stamp the client is told to use is accepted,
    and that the v4 claim it replaced is still refused."""
    import uuid

    from asclepius.gold_cases import load_gold_cases

    st = _store()
    v4_cases.load_v4_cases(st)
    load_gold_cases(st, specialty="nephrology")
    headers = A.headers_for(_evaluator("nephrology"))

    # Consume the real case first — an empty v4 queue seeds itself, so without
    # this the draw below correctly returns a REAL case and proves nothing.
    real = client.get("/api/asclepius/tasks/next?portal_version=v4&specialty=nephrology",
                      headers=headers).json()
    assert real["served_portal_version"] == "v4"
    client.post("/api/asclepius/submissions", headers=headers, json={
        "submission_id": "s-" + uuid.uuid4().hex[:12], "task_id": real["task"]["task_id"],
        "verdict": "B_better", "chosen_id": "B", "rejected_id": "A",
        "time_spent_sec": 300, "portal_version": "v4",
        "prompt_review": {"reviewed": True, "verdict": "valid"},
        "independent_answer": {"text": "Stop transfusing; the AKI is pre-renal."},
        "chosen_revision": {"edited": False, "why_better_notes": "restrictive threshold"},
        "rejected_critique": {"error_tags": ["unsafe_recommendation"], "why_worse": "over-transfuses"},
    })

    body = client.get("/api/asclepius/tasks/next?portal_version=v4&specialty=nephrology",
                      headers=headers).json()
    assert body["served_portal_version"] == "v3"

    payload = {
        "task_id": body["task"]["task_id"], "verdict": "A_better",
        "chosen_id": "A", "rejected_id": "B", "time_spent_sec": 300,
        "prompt_review": {"reviewed": True, "verdict": "valid"},
        "independent_answer": {"text": "x" * 60},
        "chosen_revision": {"edited": False, "why_better_notes": "ok"},
        "rejected_critique": {"error_tags": ["omission"], "why_worse": "misses it"},
    }
    bad = client.post("/api/asclepius/submissions", headers=headers, json={
        **payload, "submission_id": "s-" + uuid.uuid4().hex[:12], "portal_version": "v4"})
    assert bad.status_code == 400, "a v4 claim on a synthetic task must still be refused"

    good = client.post("/api/asclepius/submissions", headers=headers, json={
        **payload, "submission_id": "s-" + uuid.uuid4().hex[:12],
        "portal_version": body["served_portal_version"]})
    assert good.status_code == 200, good.text


def test_an_unapproved_physician_is_not_continued_into_the_real_queue():
    """The continuation must never become a way around the wall: an unapproved
    physician asking for v4 still gets nothing real, and nothing at all here."""
    st = _store()
    v4_cases.load_v4_cases(st)
    headers = A.headers_for(_evaluator("nephrology", real=False))
    body = client.get("/api/asclepius/tasks/next?portal_version=v4&specialty=nephrology",
                      headers=headers).json()
    assert body["task"] is None
    assert body["served_portal_version"] is None


# ═════════════════════════════════════════════════════════════════════════════
# CAUSE 5 — the browser kept asking for the synthetic queue
#
# Every fix above made the real cases reachable on the SERVER. The physician
# still never saw one, because the client decides which queue to ask for and it
# was reading a value it had stored months earlier. V3 was the only recommended
# flow before the real cases existed, so every browser that had ever opened the
# portal held ``asclepius_portal_version='v3'`` — and that stale value outranked
# the V4 default. The client asked for synthetic, the server correctly served
# synthetic, and nothing on screen explained why.
#
# The JS fix (getPortalVersion) is asserted below against the file, because there
# is no JS test runner in this repo and a rule this load-bearing must not rest on
# a comment. What IS testable here is the server contract the fix depends on:
# every surface must agree about which queue answered, and say so out loud.
# ═════════════════════════════════════════════════════════════════════════════
def _gold_synthetic(st, n, specialty="nephrology"):
    """A populated synthetic multimodal queue — the 18 cases the physician was
    actually being shown."""
    for i in range(n):
        st.insert_task(task_id=f"t-gold-{i}", prompt="p", specialty=specialty,
                       difficulty="hard",
                       case={"case_source": "synthetic", "lab_panels": [{"panel": "bmp"}]})


def test_the_dashboard_names_the_queue_that_answered():
    """The count alone is ambiguous. '18 cases available' is true of the synthetic
    queue and of the real one, so a physician cleared for real patient data could
    not tell from the dashboard which work they were about to start — they found
    out from the badge INSIDE the case, which is where this bug was reported."""
    st = _store()
    _gold_synthetic(st, 18)
    v4_cases.load_v4_cases(st)
    headers = A.headers_for(_evaluator("nephrology"))
    av = client.get("/api/asclepius/tasks/available?portal_version=v4&specialty=nephrology",
                    headers=headers).json()
    assert av["served_portal_version"] == "v4"
    assert av["continued_from"] is None
    assert av["count"] >= 1
    assert all(t["case_source"] == "real_deid" for t in av["tasks"]), (
        "V4 must not mix synthetic cases into the real queue")


def test_the_dashboard_count_continues_to_v3_exactly_as_the_draw_does():
    """/tasks/next continues a physician who has finished the real charts onto the
    synthetic queue. The dashboard did not, so it read 'no cases available' while
    the very next click handed one out — the same list/draw disagreement the V4
    seed exists to prevent, pointing the other way."""
    import uuid

    st = _store()
    _gold_synthetic(st, 18)
    v4_cases.load_v4_cases(st)
    ev = _evaluator("nephrology")
    headers = A.headers_for(ev)

    # Exhaust the real queue the way a physician does — by labeling it.
    while True:
        drawn = client.get("/api/asclepius/tasks/next?portal_version=v4&specialty=nephrology",
                           headers=headers).json()
        if drawn["served_portal_version"] != "v4":
            break
        r = client.post("/api/asclepius/submissions", headers=headers, json={
            "submission_id": "s-" + uuid.uuid4().hex[:12], "task_id": drawn["task"]["task_id"],
            "verdict": "B_better", "chosen_id": "B", "rejected_id": "A",
            "time_spent_sec": 300, "portal_version": "v4",
            "prompt_review": {"reviewed": True, "verdict": "valid"},
            "independent_answer": {"text": "Stop transfusing; the AKI is pre-renal."},
            "chosen_revision": {"edited": False, "why_better_notes": "restrictive threshold"},
            "rejected_critique": {"error_tags": ["unsafe_recommendation"], "why_worse": "over-transfuses"},
        })
        assert r.status_code == 200, r.text

    av = client.get("/api/asclepius/tasks/available?portal_version=v4&specialty=nephrology",
                    headers=headers).json()
    assert av["served_portal_version"] == "v3", "the dashboard stopped at an empty V4 queue"
    assert av["continued_from"] == "v4", "and never said why the cases changed"
    assert av["count"] > 0
    # The list and the draw must never disagree about which queue is answering.
    assert av["served_portal_version"] == drawn["served_portal_version"]
    assert av["continued_from"] == drawn["continued_from"]


def test_an_unapproved_physician_is_never_continued_into_a_real_queue():
    """The continuation must not become a side door. A v4 request from someone
    without approval is answered with nothing, not with a fallback that quietly
    starts serving them work."""
    st = _store()
    _gold_synthetic(st, 3)
    v4_cases.load_v4_cases(st)
    headers = A.headers_for(_evaluator("nephrology", real=False))
    av = client.get("/api/asclepius/tasks/available?portal_version=v4&specialty=nephrology",
                    headers=headers).json()
    assert av["count"] == 0 and av["served_portal_version"] is None


def test_opening_a_case_from_the_dashboard_is_stamped_from_the_case_not_the_picker():
    """Clicking a card skips /tasks/next, so the client had no served version and
    stamped the draft from the picker instead. With the picker on v4 and a
    synthetic card on screen, that draft's own submission is a 400 at
    _derive_portal_version — the physician does twenty minutes of work and the
    save fails. The server now answers the question on the fetch."""
    st = _store()
    _gold_synthetic(st, 1)
    v4_cases.load_v4_cases(st)
    headers = A.headers_for(_evaluator("nephrology"))

    synth = client.get("/api/asclepius/tasks/t-gold-0", headers=headers).json()
    assert synth["served_portal_version"] == "v3"

    real_id = next(t["task_id"] for t in st.eligible_tasks_for_evaluator(
        evaluator_id=_evaluator("nephrology")["id"], specialty="nephrology",
        real_only=True, limit=5))
    got = client.get(f"/api/asclepius/tasks/{real_id}", headers=headers).json()
    assert got["served_portal_version"] == "v4"


def test_a_stale_pre_v4_portal_choice_does_not_outrank_the_real_cases():
    """THE REPORTED BUG. Pinned against the source because the rule lives in the
    browser: an approved contributor whose localStorage holds the pre-V4 default
    must be moved to v4, a deliberate pick made from today's menu must stick, and
    a stored v4 must not survive the approval being revoked."""
    js = (Path(__file__).resolve().parents[2]
          / "frontend" / "asclepius" / "asclepius.js").read_text()
    start = js.index("function getPortalVersion()")
    body = js[start:js.index("function setPortalVersion", start)]
    # ANY un-marked stored value predates V4 and is migrated; a marked one is a
    # real choice made from a menu that listed the real cases.
    assert "portalVersionWasPicked()" in body
    code = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("//"))
    assert "if (approved && !portalVersionWasPicked()) return 'v4';" in code
    # No per-version carve-out: rescuing only 'v3' left the same physician pinned
    # to 'v2' instead, which is the identical bug wearing a different number.
    assert "'v1'" not in code and "'v2'" not in code and "stored === 'v3'" not in code
    # A stored v4 must not outlive the approval that earned it.
    assert "stored === 'v4' && !approved" in body
    # The pick marker is only ever written by the picker.
    assert js.count("PORTAL_VERSION_PICKED_KEY, '1'") == 1


# ═════════════════════════════════════════════════════════════════════════════
# CAUSE 6 — three real cases, divided by a growing roster of specialties
#
# A physician reported an empty V4 queue on an account that was approved,
# labeling, and real-data cleared. Every gate was open. There simply was no real
# case routed to their specialty: the corpus is hepatology + nephrology +
# cardiology, and specialty routing hid all three from everyone else. "No cases
# exist" and "no cases for you" rendered as the same empty screen.
# ═════════════════════════════════════════════════════════════════════════════
def test_an_approved_physician_outside_the_three_specialties_sees_the_real_cases():
    """The reported failure. Oncology has no real chart, so under strict routing
    this physician sees zero — with nothing on screen distinguishing that from a
    corpus that does not exist yet."""
    st = _store()
    v4_cases.load_v4_cases(st, open_to_all_specialties=True)
    headers = A.headers_for(_evaluator("oncology"))
    av = client.get("/api/asclepius/tasks/available?portal_version=v4&specialty=oncology",
                    headers=headers).json()
    assert av["count"] == 3, "an approved physician saw none of the three real cases"
    nxt = client.get("/api/asclepius/tasks/next?portal_version=v4&specialty=oncology",
                     headers=headers).json()
    assert nxt["served_portal_version"] == "v4"
    assert (nxt["task"].get("case") or {}).get("case_source") == "real_deid"


def test_strict_routing_still_hides_them_when_the_fan_out_is_off():
    """The widening is a setting, not a new law. With it off, specialty routing is
    exactly what it was — so turning it back on once each specialty has its own
    corpus is one env var, not a revert."""
    st = _store()
    v4_cases.load_v4_cases(st, open_to_all_specialties=False)
    headers = A.headers_for(_evaluator("oncology"))
    av = client.get("/api/asclepius/tasks/available?portal_version=v4&specialty=oncology",
                    headers=headers).json()
    assert av["count"] == 0


def test_the_seed_corrects_visibility_on_cases_that_are_already_in_the_queue():
    """THE PART THAT MAKES THE FIX REACH PRODUCTION. The seed is idempotent on task
    id, so a deployed database already holding these three tasks would skip them
    and never apply the new setting — correct on a fresh install, wrong on every
    real one. Flipping the setting must reconcile rows that already exist."""
    st = _store()
    first = v4_cases.load_v4_cases(st, open_to_all_specialties=False)
    assert first["loaded"] == 3 and first["revisited"] == 0
    headers = A.headers_for(_evaluator("oncology"))
    assert client.get("/api/asclepius/tasks/available?portal_version=v4&specialty=oncology",
                      headers=headers).json()["count"] == 0

    again = v4_cases.load_v4_cases(st, open_to_all_specialties=True,
                                   reconcile_visibility=True)
    assert again["loaded"] == 0, "must not duplicate the tasks"
    assert again["skipped"] == 3
    assert again["revisited"] == 3, "the already-present tasks were not widened"
    assert client.get("/api/asclepius/tasks/available?portal_version=v4&specialty=oncology",
                      headers=headers).json()["count"] == 3

    # …and it narrows again, so the setting is genuinely reversible in place.
    back = v4_cases.load_v4_cases(st, open_to_all_specialties=False,
                                  reconcile_visibility=True)
    assert back["revisited"] == 3
    assert client.get("/api/asclepius/tasks/available?portal_version=v4&specialty=oncology",
                      headers=headers).json()["count"] == 0


def test_the_fan_out_widens_visibility_and_nothing_else():
    """It must not become a back door. Fan-out is a MATCHING control: the real-data
    wall and what we pay per case are both untouched by it."""
    st = _store()
    v4_cases.load_v4_cases(st, open_to_all_specialties=True)
    for e in v4_cases.V4_REAL_CASES:
        t = st.get_task(v4_cases.v4_task_id(e["case_id"]))
        assert t["max_labels"] == v4_cases.V4_DEFAULT_MAX_LABELS, "fan-out changed what we pay"
    # An unapproved physician is still walled off, fan-out or no fan-out.
    headers = A.headers_for(_evaluator("oncology", real=False))
    assert client.get("/api/asclepius/tasks/available?portal_version=v4&specialty=oncology",
                      headers=headers).json()["count"] == 0
    assert client.get("/api/asclepius/tasks/next?portal_version=v4&specialty=oncology",
                      headers=headers).json()["task"] is None


def test_the_access_report_names_the_gate_that_is_actually_shut():
    """Four gates produce one identical empty screen. An operator must be able to
    ask the product which one it is instead of reading source."""
    st = _store()
    v4_cases.load_v4_cases(st, open_to_all_specialties=False)
    admin_h = _admin_h()
    ev = _evaluator("oncology")

    r = client.get(f"/api/asclepius/admin/real-case-access?email={ev['email']}",
                   headers=admin_h)
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["can_see_real_cases"] is False
    assert rep["real_cases_they_can_draw"] == []
    assert any("No real case is routed to" in b for b in rep["blockers"]), rep["blockers"]
    assert rep["specialties_with_a_real_case"] == ["cardiology", "hepatology", "nephrology"]

    # Widen, and the report flips — and stops reporting a blocker that is now open.
    v4_cases.load_v4_cases(st, open_to_all_specialties=True, reconcile_visibility=True)
    rep = client.get(f"/api/asclepius/admin/real-case-access?email={ev['email']}",
                     headers=admin_h).json()
    assert rep["can_see_real_cases"] is True
    assert len(rep["real_cases_they_can_draw"]) == 3
    assert rep["blockers"] == []


def test_the_access_report_names_an_approval_gate_and_is_admin_only():
    st = _store()
    v4_cases.load_v4_cases(st, open_to_all_specialties=True)
    ev = _evaluator("nephrology", real=False)
    rep = client.get(f"/api/asclepius/admin/real-case-access?email={ev['email']}",
                     headers=_admin_h()).json()
    assert rep["can_see_real_cases"] is False
    assert any("real_data_approved is off" in b for b in rep["blockers"]), rep["blockers"]
    # It reports on accounts and their access; it is not an evaluator-facing screen.
    assert client.get("/api/asclepius/admin/real-case-access",
                      headers=A.headers_for(ev)).status_code in (401, 403)
    assert client.get("/api/asclepius/admin/real-case-access?email=nobody@example.com",
                      headers=_admin_h()).status_code == 404


# ═════════════════════════════════════════════════════════════════════════════
# "I pushed a fix and I am not seeing it"
#
# A deploy that never ran, a deploy that failed back to the previous image, and
# a real bug in the new code all present identically — as the old behaviour.
# Nothing the running process served said which commit it was, so telling them
# apart meant reading a dashboard or guessing.
# ═════════════════════════════════════════════════════════════════════════════
def test_the_running_build_is_reportable_without_a_login():
    r = client.get("/api/version")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) >= {"commit", "short_commit", "branch", "started_at"}
    assert body["started_at"], "a process that cannot say when it started is not answering the question"
    if body["commit"]:
        assert body["short_commit"] == body["commit"][:7]
    # Thin on purpose: a health surface must not become an environment dump.
    assert not (set(body) - {"commit", "short_commit", "branch", "started_at",
                             "deployment_id"}), body


def test_the_running_commit_prefers_the_platform_over_the_filesystem(monkeypatch):
    """In a container built from a tarball there is no .git, and a stale one
    would be worse than nothing — so the platform's own value wins."""
    import main as main_mod
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "0" * 40)
    monkeypatch.setenv("RAILWAY_GIT_BRANCH", "main")
    got = main_mod._running_commit()
    assert got["commit"] == "0" * 40 and got["branch"] == "main"
    assert got["short_commit"] == "0000000"


def test_the_access_report_says_what_the_running_process_did_at_boot():
    """The fan-out reconcile happens at startup. An operator checking whether a
    deploy took needs that answer from THIS container, not from the source."""
    rep = client.get("/api/asclepius/admin/real-case-access", headers=_admin_h()).json()
    assert "build" in rep and "v4_seeding_at_boot" in rep
    assert "open_to_all_specialties_setting" in rep


# ═════════════════════════════════════════════════════════════════════════════
# CAUSE 7 — an environment variable quietly owned a doctor's account
#
# ASCLEPIUS_ADMIN_EMAIL pointed at a physician. ensure_admin_from_env runs on
# EVERY boot and forces role='admin', so it was not a one-time promotion but a
# standing override: the console's "set role" button appeared to work and the
# next deploy silently undid it. The physician also lost the real-case queue,
# because real-data approval follows APPROVED + LABELING and an account parked at
# role='admin' is not being verified as a labeler. The visible symptom was an
# empty V4 queue with nothing on screen connecting it to an environment variable.
# ═════════════════════════════════════════════════════════════════════════════
def test_the_env_admin_bootstrap_will_not_take_over_a_physician_account(monkeypatch):
    from asclepius import auth as asc_auth

    st = _store()
    doc = _evaluator("nephrology")
    A.make_user(st, role="admin")                     # another way into the console
    monkeypatch.setenv("ASCLEPIUS_ADMIN_EMAIL", doc["email"])
    monkeypatch.setenv("ASCLEPIUS_ADMIN_PASSWORD", "unused-in-this-test")

    assert asc_auth.ensure_admin_from_env(st) is None, "it hijacked a physician account"
    assert st.get_user_by_id(doc["id"])["role"] == "evaluator", (
        "a deliberate physician role was reverted by an environment variable")


def test_it_still_promotes_when_that_would_otherwise_lock_the_console_out(monkeypatch):
    """The guard must not become its own outage. With no other admin, an operator
    who cannot get in cannot repair anything — so the bootstrap still runs."""
    from asclepius import auth as asc_auth

    st = _store()
    doc = _evaluator("nephrology")
    # No _admin_h() here on purpose: this store has no other admin, which is the
    # whole condition under test.
    assert st.count_active_admins(excluding=doc["id"]) == 0, "test setup left an admin standing"
    monkeypatch.setenv("ASCLEPIUS_ADMIN_EMAIL", doc["email"])
    monkeypatch.setenv("ASCLEPIUS_ADMIN_PASSWORD", "bootstrap-recovery-value")
    assert asc_auth.ensure_admin_from_env(st) is not None
    assert st.get_user_by_id(doc["id"])["role"] == "admin"


def test_the_access_report_names_the_env_pin_and_the_admin_role(monkeypatch):
    """Two gates that are invisible from the portal: the account's role, and the
    environment variable that keeps putting it back."""
    st = _store()
    v4_cases.load_v4_cases(st, open_to_all_specialties=True, reconcile_visibility=True)
    doc = _evaluator("nephrology")
    st.set_user_role(doc["id"], "admin")
    monkeypatch.setenv("ASCLEPIUS_ADMIN_EMAIL", doc["email"])

    rep = client.get(f"/api/asclepius/admin/real-case-access?email={doc['email']}",
                     headers=_admin_h()).json()
    assert rep["physician"]["role"] == "admin"
    assert rep["physician"]["pinned_admin_by_env"] is True
    # The env pin IS a blocker while the account sits at role='admin': the console
    # button works and the next deploy undoes it.
    assert any("ASCLEPIUS_ADMIN_EMAIL" in b for b in rep["blockers"]), rep["blockers"]
    # The admin role is NOT reported as a real-data blocker, because it is not one:
    # admins hold LABEL, so a verified admin does get the auto-grant. Claiming
    # otherwise would send an operator chasing the wrong gate.
    assert not any("role is 'admin'" in b for b in rep["blockers"]), rep["blockers"]
    assert any("role is 'admin'" in n for n in rep["notes"]), rep["notes"]


def test_a_verified_admin_is_not_told_the_role_is_what_blocks_them():
    """Measured, not assumed: an approved, labeling account still draws real cases
    at role='admin'. The report must not name the role as the cause."""
    st = _store()
    v4_cases.load_v4_cases(st, open_to_all_specialties=True, reconcile_visibility=True)
    doc = _evaluator("nephrology")
    st.set_user_role(doc["id"], "admin")
    st.sync_real_data_approval()
    rep = client.get(f"/api/asclepius/admin/real-case-access?email={doc['email']}",
                     headers=_admin_h()).json()
    assert rep["can_see_real_cases"] is True
    assert len(rep["real_cases_they_can_draw"]) == 3


def test_the_env_pin_is_a_note_not_a_blocker_once_the_guard_protects_it():
    """After the role is fixed and another admin exists, the bootstrap stands down
    — so the report must stop warning about a revert that can no longer happen."""
    st = _store()
    v4_cases.load_v4_cases(st, open_to_all_specialties=True, reconcile_visibility=True)
    admin_h = _admin_h()                      # the other active admin
    doc = _evaluator("nephrology")
    import os as _os
    _os.environ["ASCLEPIUS_ADMIN_EMAIL"] = doc["email"]
    try:
        rep = client.get(f"/api/asclepius/admin/real-case-access?email={doc['email']}",
                         headers=admin_h).json()
        assert rep["blockers"] == [], rep["blockers"]
        assert any("survive the next deploy" in n for n in rep["notes"]), rep["notes"]
        assert rep["can_see_real_cases"] is True
    finally:
        _os.environ.pop("ASCLEPIUS_ADMIN_EMAIL", None)


def test_a_plain_physician_is_not_accused_of_an_env_pin(monkeypatch):
    """The new blockers must not fire on an account they do not describe."""
    st = _store()
    v4_cases.load_v4_cases(st, open_to_all_specialties=True, reconcile_visibility=True)
    doc = _evaluator("nephrology")
    monkeypatch.setenv("ASCLEPIUS_ADMIN_EMAIL", "ops@example.test")
    rep = client.get(f"/api/asclepius/admin/real-case-access?email={doc['email']}",
                     headers=_admin_h()).json()
    assert rep["physician"]["pinned_admin_by_env"] is False
    assert rep["blockers"] == [] and rep["notes"] == []
    assert rep["can_see_real_cases"] is True


# ═════════════════════════════════════════════════════════════════════════════
# CAUSE 8 — a physician filed under an operator role is INVISIBLE, not mislabelled
#
# The Physicians roster is `role == 'evaluator'`. The verification queue and the
# tier backfill filter the same way. So an account whose row says role='admin'
# appears on no screen an operator has: it cannot be approved, cannot be tiered,
# and cannot be moved back, because every control that would do it lives on a row
# that is never rendered. It also never gets real-data approval — that follows
# APPROVED + LABELING — so the portal serves it synthetic cases forever.
#
# The self-serve director onboarding provisioned role="admin" until it was
# changed to "evaluator". The code fix did not repair the rows already written,
# and nothing surfaced them.
# ═════════════════════════════════════════════════════════════════════════════
def _doctor_filed_as_admin(st, specialty="nephrology"):
    u = A.make_user(st, role="admin", specialty=specialty,
                    board_cert=f"board_certified_{specialty}", years_experience=15)
    st.set_verification_status(u["id"], "approved")
    return st.get_user_by_id(u["id"])


def test_a_doctor_filed_as_admin_is_absent_from_the_roster():
    """The defect itself, pinned: not a cosmetic mislabel — an absence."""
    st = _store()
    doc = _doctor_filed_as_admin(st)
    body = client.get("/api/asclepius/admin/physicians", headers=_admin_h()).json()
    assert doc["email"] not in [p["email"] for p in body["physicians"]]


def test_but_the_console_now_says_the_account_exists_and_why():
    st = _store()
    doc = _doctor_filed_as_admin(st)
    body = client.get("/api/asclepius/admin/physicians", headers=_admin_h()).json()
    row = next((m for m in body["misfiled_physicians"] if m["email"] == doc["email"]), None)
    assert row is not None, "the account is still invisible on every screen"
    assert row["role"] == "admin"
    assert body["misfiled_count"] >= 1
    # Named evidence, so the operator can judge rather than trust the screen.
    assert "specialty" in row["physician_markers"]
    assert "verification_status" in row["physician_markers"]


def test_a_real_operator_is_not_flagged_as_a_misfiled_doctor():
    """The card must not accuse every admin of being a doctor, or it becomes
    noise an operator learns to scroll past."""
    st = _store()
    A.make_user(st, role="admin")           # a plain operator: no clinical markers
    body = client.get("/api/asclepius/admin/physicians", headers=_admin_h()).json()
    assert body["misfiled_count"] == 0, body["misfiled_physicians"]


def test_the_mock_contributor_is_never_listed_as_misfiled():
    st = _store()
    from asclepius.auth import ensure_mock_contributor
    ensure_mock_contributor(st)
    body = client.get("/api/asclepius/admin/physicians", headers=_admin_h()).json()
    assert all(not (m["email"] or "").startswith("mock")
               for m in body["misfiled_physicians"]), body["misfiled_physicians"]


def test_moving_the_account_back_restores_the_roster_and_the_real_cases():
    """The whole repair, through the console's own routes — this is the path the
    card's button drives, and it must end with the doctor holding real cases."""
    st = _store()
    v4_cases.load_v4_cases(st, open_to_all_specialties=True, reconcile_visibility=True)
    admin_h = _admin_h()
    doc = _doctor_filed_as_admin(st)

    before = client.get(f"/api/asclepius/admin/real-case-access?email={doc['email']}",
                        headers=admin_h).json()
    assert before["physician"]["role"] == "admin"

    r = client.post(f"/api/asclepius/admin/users/{doc['id']}/role",
                    json={"role": "evaluator"}, headers=admin_h)
    assert r.status_code == 200, r.text
    # One action, not two: the role move also restores the tier the boot backfill
    # skipped and re-runs the real-data policy, so the doctor can actually work.
    assert r.json()["tier"] == "labeler"
    assert r.json()["tier_assigned"] == "labeler"
    assert r.json()["real_data_approved"] is True

    body = client.get("/api/asclepius/admin/physicians", headers=admin_h).json()
    assert doc["email"] in [p["email"] for p in body["physicians"]], "still not on the roster"
    assert body["misfiled_count"] == 0

    after = client.get(f"/api/asclepius/admin/real-case-access?email={doc['email']}",
                       headers=admin_h).json()
    assert after["can_see_real_cases"] is True, after["blockers"]
    assert len(after["real_cases_they_can_draw"]) == 3
    assert after["blockers"] == []


def test_the_mock_contributors_approval_is_not_pinned_as_a_human_decision():
    """set_real_data_approved defaults to source='admin', which the sync treats as
    a human decision and never revisits. The mock account defaulting into that
    would pin it permanently outside the policy managing everyone else."""
    st = _store()
    from asclepius.auth import ensure_mock_contributor
    u = ensure_mock_contributor(st)
    if u is None:
        pytest.skip("mock contributor disabled in this environment")
    row = st.get_user_by_id(u["id"])
    assert (row.get("real_data_approval_source") or "").startswith("auto:"), (
        row.get("real_data_approval_source"))


def test_the_role_restore_does_not_invent_a_tier_for_a_rejected_account():
    """The tier backfill's own exclusion, kept: pending and rejected cannot label,
    so a tier would grant nothing and would report a decision nobody made."""
    st = _store()
    admin_h = _admin_h()
    doc = A.make_user(st, role="admin", specialty="nephrology",
                      board_cert="board_certified_nephrology", years_experience=15)
    st.set_verification_status(doc["id"], "rejected")
    r = client.post(f"/api/asclepius/admin/users/{doc['id']}/role",
                    json={"role": "evaluator"}, headers=admin_h)
    assert r.status_code == 200, r.text
    assert r.json()["tier_assigned"] is None
    assert st.get_user_by_id(doc["id"])["tier"] is None


def test_the_role_restore_never_overwrites_a_tier_someone_decided():
    st = _store()
    admin_h = _admin_h()
    doc = A.make_user(st, role="admin", specialty="nephrology",
                      board_cert="board_certified_nephrology", years_experience=15)
    st.set_verification_status(doc["id"], "approved")
    st.record_verification_decision(
        user_id=doc["id"], status="approved", decided_by="a@b.c", tier="reviewer")
    r = client.post(f"/api/asclepius/admin/users/{doc['id']}/role",
                    json={"role": "evaluator"}, headers=admin_h)
    assert r.json()["tier_assigned"] is None
    assert st.get_user_by_id(doc["id"])["tier"] == "reviewer"


# ═════════════════════════════════════════════════════════════════════════════
# The repair, in ONE call
#
# Putting a misfiled account back together is four writes across three screens —
# role, tier, verification, real-data approval — and an account filed under an
# operator role is on NONE of those screens, so the sequence could not be
# started from the console at all.
# ═════════════════════════════════════════════════════════════════════════════
def test_one_call_restores_a_misfiled_doctor_all_the_way_to_real_cases():
    st = _store()
    v4_cases.load_v4_cases(st, open_to_all_specialties=True, reconcile_visibility=True)
    admin_h = _admin_h()
    doc = A.make_user(st, role="admin", specialty="nephrology",
                      board_cert="board_certified_nephrology", years_experience=15)

    r = client.post("/api/asclepius/admin/physicians/restore"
                    f"?email={doc['email']}",
                    json={"approve_verification": True, "tier": "labeler"},
                    headers=admin_h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["after"] == {"role": "evaluator", "tier": "labeler",
                             "verification_status": "approved",
                             "real_data_approved": True}, body
    assert body["can_label_real_cases"] is True

    headers = A.headers_for(st.get_user_by_id(doc["id"]))
    nxt = client.get("/api/asclepius/tasks/next?portal_version=v4&specialty=nephrology",
                     headers=headers).json()
    assert nxt["served_portal_version"] == "v4"
    assert (nxt["task"].get("case") or {}).get("case_source") == "real_deid"


def test_the_restore_does_not_approve_credentials_unless_asked():
    """Verification is a credentialing decision. It must never be a side effect of
    fixing a role, or the audit trail says a review happened that did not."""
    st = _store()
    admin_h = _admin_h()
    doc = A.make_user(st, role="admin", specialty="nephrology",
                      board_cert="board_certified_nephrology", years_experience=15)
    body = client.post(f"/api/asclepius/admin/physicians/restore?email={doc['email']}",
                       json={}, headers=admin_h).json()
    assert body["after"]["role"] == "evaluator"
    assert body["after"]["verification_status"] is None
    assert body["after"]["real_data_approved"] is False
    assert body["can_label_real_cases"] is False


def test_the_restore_records_who_made_the_credentialing_decision():
    st = _store()
    admin = A.make_user(_store(), role="admin")
    doc = A.make_user(st, role="admin", specialty="nephrology",
                      board_cert="board_certified_nephrology", years_experience=15)
    client.post(f"/api/asclepius/admin/physicians/restore?email={doc['email']}",
                json={"approve_verification": True, "tier": "labeler"},
                headers=A.headers_for(admin))
    row = st.get_user_by_id(doc["id"])
    assert row["verified_by"] == admin["email"], row["verified_by"]
    assert row["verified_at"]


def test_the_restore_refuses_to_take_away_the_callers_own_admin_access():
    admin = A.make_user(_store(), role="admin")
    r = client.post(f"/api/asclepius/admin/physicians/restore?email={admin['email']}",
                    json={}, headers=A.headers_for(admin))
    assert r.status_code == 422
    assert "your own account" in r.json()["detail"]


def test_the_restore_is_admin_only_and_404s_on_an_unknown_account():
    st = _store()
    ev = _evaluator("nephrology")
    assert client.post("/api/asclepius/admin/physicians/restore?email=x@y.z",
                       json={}, headers=A.headers_for(ev)).status_code in (401, 403)
    assert client.post("/api/asclepius/admin/physicians/restore?email=nobody@example.test",
                       json={}, headers=_admin_h()).status_code == 404


def test_the_restore_cannot_become_a_side_door_around_the_real_data_policy():
    """It never writes real_data_approved directly — the APPROVED + LABELING
    policy derives it, so an account that does not qualify does not get it."""
    st = _store()
    admin_h = _admin_h()
    doc = A.make_user(st, role="admin", specialty="nephrology",
                      board_cert="board_certified_nephrology", years_experience=15)
    st.set_verification_status(doc["id"], "rejected")
    body = client.post(f"/api/asclepius/admin/physicians/restore?email={doc['email']}",
                       json={}, headers=admin_h).json()
    assert body["after"]["real_data_approved"] is False
    assert body["can_label_real_cases"] is False


def test_a_stored_v2_does_not_outrank_the_real_cases_either():
    """The v3-only carve-out was wrong and cost a round: the same physician turned
    up pinned to V2 instead. Any stored value without the pick marker predates V4."""
    js = (Path(__file__).resolve().parents[2]
          / "frontend" / "asclepius" / "asclepius.js").read_text()
    start = js.index("function getPortalVersion()")
    body = js[start:js.index("function setPortalVersion", start)]
    code = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("//"))
    assert "if (approved && !portalVersionWasPicked()) return 'v4';" in code
    # No version-specific carve-out survives in the migration branch.
    assert "stored === 'v3'" not in code


# ═════════════════════════════════════════════════════════════════════════════
# Audit findings against the restore endpoint itself
# ═════════════════════════════════════════════════════════════════════════════
def test_restoring_does_not_silently_demote_a_decided_tier():
    """record_verification_decision writes `tier = ?` unconditionally on its
    approved branch, so approving WITHOUT naming a tier passed None and NULLed
    out a tier somebody had decided — after which the default backfill filled in
    'labeler'. Measured: an existing reviewer came back a labeler, demoted by an
    operator doing the obvious thing."""
    st = _store()
    admin_h = _admin_h()
    doc = A.make_user(st, role="admin", specialty="nephrology",
                      board_cert="board_certified_nephrology", years_experience=15)
    st.record_verification_decision(user_id=doc["id"], status="approved",
                                    decided_by="prior@admin", tier="reviewer")
    st.set_verification_status(doc["id"], "pending")   # so the approve branch runs

    body = client.post(f"/api/asclepius/admin/physicians/restore?email={doc['email']}",
                       json={"approve_verification": True}, headers=admin_h).json()
    assert body["after"]["tier"] == "reviewer", body
    assert st.get_user_by_id(doc["id"])["tier"] == "reviewer"


def test_a_tier_cannot_be_set_on_an_account_nobody_has_approved():
    """Two defects in one call. The tier columns are written only on the approved
    branch, so a tier named without an approval wrote NOTHING while the response
    claimed it had — and routing it through a re-stamp of the current status also
    stamped verified_by on an account nobody verified, corrupting the
    credentialing trail. Refused now, because a tier on a pending account grants
    no access anyway."""
    st = _store()
    admin_h = _admin_h()
    doc = A.make_user(st, role="admin", specialty="nephrology",
                      board_cert="board_certified_nephrology", years_experience=15)
    st.set_verification_status(doc["id"], "pending")

    r = client.post(f"/api/asclepius/admin/physicians/restore?email={doc['email']}",
                    json={"tier": "reviewer"}, headers=admin_h)
    assert r.status_code == 422, r.text
    assert "part of the approval decision" in r.json()["detail"]
    row = st.get_user_by_id(doc["id"])
    assert row["tier"] is None
    assert row["verified_by"] is None, "stamped a credentialing decision nobody made"


def test_the_restore_reports_only_changes_it_actually_made():
    """A response that lists a change the database did not take is worse than an
    error: it ends the investigation."""
    st = _store()
    admin_h = _admin_h()
    doc = A.make_user(st, role="admin", specialty="nephrology",
                      board_cert="board_certified_nephrology", years_experience=15)
    body = client.post(f"/api/asclepius/admin/physicians/restore?email={doc['email']}",
                       json={"approve_verification": True, "tier": "labeler"},
                       headers=admin_h).json()
    row = st.get_user_by_id(doc["id"])
    assert body["after"]["tier"] == row["tier"]
    assert body["after"]["role"] == row["role"]
    assert body["after"]["verification_status"] == row["verification_status"]
    assert body["after"]["real_data_approved"] is bool(row["real_data_approved"])
    # Idempotent: a second identical call changes nothing and says so.
    again = client.post(f"/api/asclepius/admin/physicians/restore?email={doc['email']}",
                        json={"approve_verification": True, "tier": "labeler"},
                        headers=admin_h).json()
    assert again["changes"] == [], again["changes"]
    assert again["can_label_real_cases"] is True


def test_an_approved_contributor_is_never_stranded_on_the_synthetic_default():
    """Audit finding. The pick marker can outlive the version written beside it
    (a cleared key, a partly-failed storage write). With the marker set and no
    stored version, the rule fell through to the synthetic default and stranded
    an approved physician on v3 with no choice on file to justify it."""
    js = (Path(__file__).resolve().parents[2]
          / "frontend" / "asclepius" / "asclepius.js").read_text()
    start = js.index("function getPortalVersion()")
    body = js[start:js.index("function setPortalVersion", start)]
    code = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("//"))
    assert "return approved ? 'v4' : DEFAULT_PORTAL_VERSION;" in code
    # The bare fallthrough that produced the bug must not survive anywhere in the
    # function -- it is the last statement, so a stray copy is the whole defect.
    assert "return DEFAULT_PORTAL_VERSION;\n  }" not in code
