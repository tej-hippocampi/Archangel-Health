"""End-to-end trajectory generation, through the REAL admin route.

Every other trajectory test builds its points by calling ``insert_task`` directly.
This one does not: it drives ``POST /ingestion/cases/{id}/generate`` with
``trajectory: true`` and then walks the result the way a physician would, so the
seam between "an admin clicks the button" and "ordered, gated, revealable tasks
exist" is covered by something.

That seam had never executed once before this test. Writing it found three real
things about the shipped pipeline, all of which are asserted below so they cannot
regress into surprises:

  * the content gates are STRICT and they fire per encounter — a chart whose
    notes are short, or whose medication lines are not in order-sheet form, is
    correctly refused with a per-encounter reason;
  * ``parse_medication_line`` requires a leading dosage-form token (``Tab.``,
    ``Inj.``, ``IV``…) because it was written for the partner's OCR'd order
    sheets, so a plain ``"ceftriaxone 2 g IV OD"`` parses to None and the case
    then fails the empty-medication-list gate;
  * ``required_modalities`` genuinely differs point to point on real data — the
    §4.2.1 rule, observable rather than argued.

The FOUR model calls are stubbed, and only those (question authoring, the
frontier difficulty probe, candidate generation, and the two judges). The stubs
match the real return SHAPES exactly — ``failure_reasons`` is a list of dicts,
not strings, which is what ``derive_ai_failure_mode`` reads. What this test
proves is the wiring; model output quality needs live keys and is not asserted
here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import real_cases  # noqa: E402
from asclepius import trajectory as asc_trajectory  # noqa: E402

client = TestClient(A.app)

#: The chart routes to hepatology because that is what patient-1 is registered as
#: and the only enabled specialty this course belongs to. A chart whose specialty
#: is not enabled generates nothing, which is correct and is its own test.
SPECIALTY = "hepatology"


def _store():
    from asclepius.store import get_store
    return get_store()


def build_chart():
    ENCOUNTERS = [
        # (start_day, ggt, bili, alp, note, study)
        (-1810, 1361, 15.04, 640,
         "Presents with three weeks of painless jaundice, dark urine and pale stools, with "
         "6 kg of unintentional weight loss. No fever, no rigors, no right upper quadrant pain. "
         "Examination shows deep icterus and a palpable, non-tender gallbladder; no stigmata of "
         "chronic liver disease and no ascites. Bloods show a cholestatic pattern with markedly "
         "raised GGT and alkaline phosphatase and a bilirubin of 15.04, with only modest "
         "transaminase elevation. Synthetic function is preserved. The picture is one of distal "
         "biliary obstruction rather than hepatocellular injury, and a palpable gallbladder in "
         "painless jaundice raises the question of a periampullary lesion. Plan: cross-sectional "
         "imaging, tumour markers, and referral for ERCP with a view to tissue and drainage.",
         ("ultrasound", "Abdominal ultrasound", "Dilated intrahepatic and extrahepatic ducts. "
                                                "CBD 14 mm. Distal CBD obscured by bowel gas; "
                                                "gallbladder distended, no stones seen.")),
        (-1242, 1361, 17.77, 690,
         "Admitted for ERCP. Bilirubin has climbed further to 17.77 despite the cholestatic "
         "enzymes having plateaued, and the patient reports worsening pruritus. No fever and no "
         "features of cholangitis. Coagulation is normal. ERCP performed: a tight distal common "
         "bile duct stricture was crossed and a 10 French plastic stent placed with good flow of "
         "bile; brushings were taken from the stricture for cytology. The stricture appearance is "
         "suspicious rather than diagnostic, and cytology is awaited. Post-procedure the patient "
         "is comfortable. Plan: monitor for post-ERCP pancreatitis, repeat LFTs in 48 hours, and "
         "review with the cytology result at the multidisciplinary meeting.",
         ("fluoroscopy", "ERCP procedure report", "Tight distal CBD stricture approximately 2 cm "
                                                  "in length. Guidewire passed, sphincterotomy "
                                                  "performed, 10Fr x 7cm plastic stent placed. "
                                                  "Brushings taken. Good bile flow at completion.")),
        (-1202, 123, 9.10, 310,
         "Post-ERCP course was complicated by mild pancreatitis with epigastric pain and a raised "
         "amylase, managed conservatively with fluids and analgesia; this has now settled and the "
         "patient is eating normally. The cholestatic enzymes have fallen dramatically, with GGT "
         "down from 1361 to 123 and alkaline phosphatase more than halved. Bilirubin is falling "
         "more slowly at 9.10, which is expected: the conjugated fraction is albumin-bound and "
         "clears over weeks rather than days once drainage is established. Pruritus has improved. "
         "Cytology from the brushings was atypical but not diagnostic of malignancy. Plan: "
         "outpatient review with repeat LFTs, and plan for stent exchange at three months.",
         ("ct", "CT abdomen", "Stent in situ across the distal CBD with decompressed intrahepatic "
                              "ducts. Peripancreatic fat stranding consistent with resolving "
                              "post-ERCP pancreatitis. No discrete mass identified.")),
        (-980, 983, 6.40, 520,
         "Re-presents with recurrent jaundice, rigors and a temperature of 38.4. Right upper "
         "quadrant tenderness is present. The cholestatic enzymes have climbed again, with GGT "
         "back up to 983 and alkaline phosphatase to 520, while bilirubin has risen from its "
         "nadir. Inflammatory markers are raised. This is the pattern of stent occlusion with "
         "ascending cholangitis rather than progression of the underlying stricture, and it has "
         "occurred at the interval at which plastic stents typically block. Blood cultures sent. "
         "Plan: intravenous antibiotics, fluid resuscitation, and urgent ERCP for stent exchange "
         "once the patient is stabilised.",
         ("ultrasound", "Abdominal ultrasound", "Re-dilated intrahepatic ducts. Stent is "
                                                "echogenic along its length, appearances "
                                                "consistent with occlusion. No perihepatic "
                                                "collection.")),
        (-604, 210, 2.10, 180,
         "Stent exchanged at repeat ERCP; the occluded plastic stent was removed and replaced. "
         "The fever and rigors settled within 48 hours of drainage and antibiotics, and blood "
         "cultures grew a coliform sensitive to the empirical regimen. The cholestatic enzymes "
         "have fallen again and bilirubin is now 2.10, the lowest recorded in this episode. "
         "Repeat brushings were again atypical without frank malignancy, and the stricture "
         "appearance was unchanged from the index procedure. The patient is well and back to "
         "baseline weight. Plan: surveillance with planned stent exchange, repeat cross-sectional "
         "imaging in three months, and discussion at the hepatobiliary multidisciplinary meeting.",
         ("fluoroscopy", "ERCP procedure report", "Occluded 10Fr plastic stent removed with "
                                                  "sludge. Stricture unchanged in length and "
                                                  "calibre. New 10Fr stent placed with good "
                                                  "bile flow. Repeat brushings taken.")),
    ]
    panels, notes, studies = [], [], []
    for start, ggt, bili, alp, text, (mod, label, findings) in ENCOUNTERS:
        for i, day in enumerate((start, start + 1, start + 2, start + 3)):
            drift = i * 0.03
            panels.append({
                "panel": "Liver function tests", "collected_offset_days": day,
                "results": [
                    {"analyte": "GGT", "value": round(ggt * (1 - drift), 1), "unit": "U/L",
                     "ref_low": 5, "ref_high": 40, "flag": "H"},
                    {"analyte": "Total bilirubin", "value": round(bili * (1 - drift), 2),
                     "unit": "mg/dL", "ref_low": 0.2, "ref_high": 1.2, "flag": "H"},
                    {"analyte": "Alkaline phosphatase", "value": round(alp * (1 - drift)),
                     "unit": "U/L", "ref_low": 40, "ref_high": 130, "flag": "H"},
                    {"analyte": "ALT", "value": 88, "unit": "U/L",
                     "ref_low": 7, "ref_high": 56, "flag": "H"},
                ],
            })
            notes.append({"note_type": "Progress", "author_role": "hepatology",
                          "collected_offset_days": day, "text": text})
        studies.append({"modality": mod, "label": label, "collected_offset_days": start + 1,
                        "findings": findings})
    return {
        "case_source": "real_deid",
        "specialty": "hepatology",
        "demographics": {"age_band": "60-69", "sex": "M"},
        "lab_panels": panels,
        "notes": notes,
        "studies": studies,
        # Order-sheet form, because that is what the pipeline parses:
        # ``parse_medication_line`` requires a leading dosage-form token
        # (Tab./Inj./IV/PO…) and returns None without one, so a plain drug name
        # is dropped and the case then fails the empty-medication-list gate.
        "medications": [
            {"drug": "Tab. Ursodeoxycholic acid 300 mg PO BD", "collected_offset_days": -1808},
            {"drug": "Inj. Piperacillin-Tazobactam 4.5 g IV TDS", "collected_offset_days": -978},
            {"drug": "Tab. Colestyramine 4 g PO BD", "collected_offset_days": -1806},
            {"drug": "Inj. Vitamin K 10 mg IV OD", "collected_offset_days": -1241},
        ],
        "problem_list": [
            {"condition": "Obstructive jaundice", "collected_offset_days": -1811},
            {"condition": "Distal common bile duct stricture", "collected_offset_days": -1241},
        ],
    }

def _stub_model_legs(monkeypatch):
    from asclepius import critic, empirical_difficulty, real_cases
    import routers.asclepius as R

    async def _question(case, held_out, specialty):
        return ("Bilirubin continues to rise while the cholestatic enzymes fall. "
                "What is your assessment and what do you do next?"), "model"

    async def _difficulty(case, question, **kw):
        # Shape matches empirical_difficulty.measure_empirical_difficulty exactly:
        # failure_reasons is a list of DICTS, and derive_ai_failure_mode reads
        # r["failure_reason"] off each one.
        return {"measured": True, "value": 0.72, "k": 3, "n_models": 2,
                "model_failure_rate": 0.72,
                "failure_reasons": [
                    {"model": "stub/frontier-a",
                     "failure_reason": "anchoring on the falling cholestatic enzymes; called the "
                                       "obstruction relieved and missed that bilirubin lags"},
                    {"model": "stub/frontier-b",
                     "failure_reason": "anchoring on the falling cholestatic enzymes; called the "
                                       "obstruction relieved and missed that bilirubin lags"},
                ],
                "per_model": []}

    async def _candidates(prompt, specialty=None, ai_failure_mode=None, **kw):
        return {
            "candidates": [
                {"id": "a", "text": "Bilirubin is delta-bound and lags after drainage; the falling "
                                    "GGT and ALP say the obstruction is relieved. Continue supportive "
                                    "care and re-check LFTs in two weeks."},
                {"id": "b", "text": "The rising bilirubin means the stent has failed. Repeat ERCP "
                                    "urgently today."},
            ],
            "intended_flawed_id": "b",
            "model": "stub/candidate-gen",
        }

    async def _hardness(prompt, candidates):
        return {"skipped": False, "hardness_score": 0.78,
                "hardness_axes": ["counterintuitive lab trend", "timing of re-intervention"]}

    async def _case_judge(case, case_source="synthetic"):
        return {"skipped": False, "coherence": 0.93, "multimodal_necessity": 0.88,
                "reasoning_divergence_potential": 0.81}

    monkeypatch.setattr(real_cases, "derive_clinical_question", _question)
    monkeypatch.setattr(empirical_difficulty, "measure_empirical_difficulty", _difficulty)
    monkeypatch.setattr(critic, "run_hardness_judge", _hardness)
    monkeypatch.setattr(critic, "run_case_judge", _case_judge)
    monkeypatch.setattr(R, "generate_candidates_ex", _candidates)
    print("model legs: STUBBED (set ASCLEPIUS_E2E_LIVE=1 for real calls)\n")

@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    A.fresh_store()
    _stub_model_legs(monkeypatch)
    yield


def _ingest_chart(store, chart):
    upload = store.insert_ingest_upload(
        link_id="lnk-e2e", partner_id="e2e-partner", filename="patient-1.zip",
        sha256="0" * 64, size_bytes=1234, raw_path=None, source_ip=None)
    return store.insert_ingest_case(
        upload_id=upload["upload_id"], patient_key="ehr-1-patient", specialty=SPECIALTY,
        case=chart, status="ingested", report={})["ingest_case_id"]


def _admin_headers(store):
    return A.headers_for(A.make_user(store, role="admin"))


def _approved_physician(store):
    user = A.make_user(store, role="evaluator", specialty=SPECIALTY)
    store.set_real_data_approved(user["id"], True)
    return store.get_user_by_id(user["id"])


def _plan(store, cid, headers):
    r = client.post(f"/api/asclepius/ingestion/cases/{cid}/generate", headers=headers,
                    json={"dry_run": True, "derive_questions": True})
    assert r.status_code == 200, r.text
    return r.json()


def _generate(store, cid, headers):
    r = client.post(f"/api/asclepius/ingestion/cases/{cid}/generate", headers=headers,
                    json={"dry_run": False, "trajectory": True})
    assert r.status_code == 200, r.text
    return r.json()


# ═══════════════════════════════════════════════════════════════════════════════
# The plan
# ═══════════════════════════════════════════════════════════════════════════════
def test_the_plan_reports_both_numbers_a_chart_walk_is_priced_on():
    store = _store()
    cid = _ingest_chart(store, build_chart())
    plan = _plan(store, cid, _admin_headers(store))
    assert plan["encounters"] == 5
    assert plan["decision_points"] == 5
    # Always one fewer: the terminal point has nothing later to check it against.
    assert plan["verifiable_decision_points"] == plan["decision_points"] - 1
    assert plan["density_gate"] == {
        "min_distinct_dates": real_cases.ENCOUNTER_MIN_DISTINCT_DATES,
        "min_events": real_cases.ENCOUNTER_MIN_EVENTS,
        "min_resource_types": real_cases.ENCOUNTER_MIN_RESOURCE_TYPES,
    }
    # Every proposal carries its measurements, passing or not.
    for p in plan["proposals"]:
        d = p["density"]
        assert {"n_distinct_dates", "n_events", "n_resource_types", "reasons"} <= set(d)


def test_a_content_gate_refuses_per_encounter_with_a_reason():
    """The gates are strict and they fire one encounter at a time. An admin must
    be able to read WHICH encounter was refused and why, not just a count."""
    store = _store()
    chart = build_chart()
    # Strip the medication list: every encounter should now be refused, by name.
    chart["medications"] = []
    cid = _ingest_chart(store, chart)
    h = _admin_headers(store)
    plan = _plan(store, cid, h)
    assert all(not p["generatable"] for p in plan["proposals"])
    assert all(any("medication" in b for b in p["blockers"]) for p in plan["proposals"])
    r = client.post(f"/api/asclepius/ingestion/cases/{cid}/generate", headers=h,
                    json={"dry_run": False, "trajectory": True})
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "nothing_generatable"


def test_a_plain_drug_name_is_not_a_parsable_medication_line():
    """``parse_medication_line`` is built for the partner's OCR'd order sheets and
    requires a leading dosage-form token. Asserted here because the failure it
    causes is remote from its cause: the drug is silently dropped, and the case
    dies several steps later on 'empty medication list'."""
    assert real_cases.parse_medication_line("ceftriaxone 2 g IV OD") is None
    parsed = real_cases.parse_medication_line("Inj. Ceftriaxone 2 g IV OD")
    assert parsed and parsed["drug"] == "Ceftriaxone"


# ═══════════════════════════════════════════════════════════════════════════════
# Generation
# ═══════════════════════════════════════════════════════════════════════════════
def test_generation_produces_an_ordered_single_labelled_walk():
    store = _store()
    cid = _ingest_chart(store, build_chart())
    gen = _generate(store, cid, _admin_headers(store))
    tid = gen["trajectory_id"]
    assert tid and gen["trajectory_points"] >= 3
    assert gen["trajectory_verifiable_points"] == gen["trajectory_points"] - 1
    # §9.3 — the cost is stated by the route, not left to be discovered.
    assert gen["estimated_cost_usd"] > 0

    points = store.trajectory_points(tid)
    assert [p["sequence_index"] for p in points] == list(range(len(points)))
    # §9.6 — single-labelled, forced at generation.
    assert all(p["max_labels"] == asc_trajectory.TRAJECTORY_MAX_LABELS for p in points)
    # Chronological: the walk's order is the chart's order.
    offsets = [(p["generation"] or {}).get("index_event_offset") for p in points]
    assert offsets == sorted(offsets)


def test_required_modalities_differ_point_to_point_on_real_data():
    """§4.2.1, observable rather than argued: each window declares what IT carries.
    Inheriting the chart's declaration would put the ERCP report on every point
    and quarantine the early ones."""
    store = _store()
    cid = _ingest_chart(store, build_chart())
    gen = _generate(store, cid, _admin_headers(store))
    decls = [tuple((p["case"] or {}).get("required_modalities") or [])
             for p in store.trajectory_points(gen["trajectory_id"])]
    assert len(set(decls)) > 1, f"every point declared the same modalities: {decls}"
    from asclepius import ingestion as asc_ingestion
    for p in store.trajectory_points(gen["trajectory_id"]):
        case = p["case"] or {}
        comp = asc_ingestion.completeness_check(case.get("required_modalities") or [], case)
        assert comp["missing"] == [], (case.get("required_modalities"), comp)


# ═══════════════════════════════════════════════════════════════════════════════
# The walk, as a physician takes it
# ═══════════════════════════════════════════════════════════════════════════════
def test_a_physician_walks_the_generated_chart_in_order_and_cannot_read_ahead():
    store = _store()
    cid = _ingest_chart(store, build_chart())
    gen = _generate(store, cid, _admin_headers(store))
    points = store.trajectory_points(gen["trajectory_id"])
    doc = _approved_physician(store)
    dh = A.headers_for(doc)

    def _cards():
        return [t for t in client.get("/api/asclepius/tasks/available?portal_version=v4",
                                      headers=dh).json()["tasks"]
                if t.get("trajectory_id") == gen["trajectory_id"]]

    # PRD CASE-BATCHES §1 — a promoted walk is NOT in anybody's queue yet. It was
    # written 'assigned_only', so before an admin routes it the physician sees
    # nothing at all, which is the whole point of the column: promoting a chart and
    # releasing it to doctors are two decisions, and only the second is an admin
    # pressing Send.
    assert _cards() == [], "a promoted-but-unrouted walk must be invisible"
    assert all((store.get_task(pt["task_id"]) or {}).get("distribution") == "assigned_only"
               for pt in points)

    # Route it. From here the walk behaves exactly as it did before the column
    # existed — the gate, the 409 and the reveal are all unchanged.
    for pt in points:
        store.upsert_assignment(task_id=pt["task_id"], user_id=doc["id"],
                                role="label", assigned_by="u-admin")

    # NOW the dashboard offers exactly one point: the first.
    assert [c["sequence_index"] for c in _cards()] == [0]

    # The last point is refused by id, and the payload of the first carries no future.
    assert client.get(f"/api/asclepius/tasks/{points[-1]['task_id']}",
                      headers=dh).status_code == 409
    served = client.get(f"/api/asclepius/tasks/{points[0]['task_id']}", headers=dh).json()
    offsets = []

    def walk(n):
        if isinstance(n, dict):
            for k, v in n.items():
                if k == "collected_offset_days" and isinstance(v, int):
                    offsets.append(v)
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
    walk(served)
    assert offsets and max(offsets) <= 0

    # Walk the whole chart: commit, reveal, self-score, advance.
    for i, pt in enumerate(points):
        r = client.post("/api/asclepius/submissions", headers=dh, json={
            "task_id": pt["task_id"], "verdict": "A_better", "chosen_id": "a",
            "rejected_id": "b", "confidence": "high", "time_spent_sec": 900,
            "expected_trajectory": {
                "expectations": [{"expectation": "enzymes stay down and bilirubin falls",
                                  "horizon_days": 21}],
                "falsifiers": ["if GGT climbs again the stent has occluded"]},
        })
        assert r.status_code == 200, r.text
        out = client.get(f"/api/asclepius/tasks/{pt['task_id']}/trajectory-outcome",
                         headers=dh)
        assert out.status_code == 200, out.text
        body = out.json()
        if i < len(points) - 1:
            assert body["outcome"]["days_after_decision"] > 0
            assert body["outcome"]["n_events"] > 0
            ss = client.post(f"/api/asclepius/tasks/{pt['task_id']}/trajectory-self-score",
                             headers=dh, json={"marks": [{"index": 0, "state": "did_not_hold"}],
                                               "falsifier_fired": True})
            assert ss.status_code == 200, ss.text
        else:
            assert body["outcome"] is None and "last decision point" in body["reason"]

    prog = store.evaluator_trajectory_progress(
        trajectory_id=gen["trajectory_id"], evaluator_id=doc["id"])
    assert prog["complete"] is True and prog["n_answered"] == len(points)


def test_the_worked_example_the_prd_is_built_on():
    """§3.3, on the generated pipeline rather than in prose.

    At the decision point the physician sees the cholestatic enzymes FALLING
    after drainage. They commit: enzymes stay down, and if GGT climbs again the
    stent has occluded. The next encounter shows GGT back in the high hundreds —
    their own stated falsifier, fired, with the chart proving it.
    """
    store = _store()
    cid = _ingest_chart(store, build_chart())
    gen = _generate(store, cid, _admin_headers(store))
    points = store.trajectory_points(gen["trajectory_id"])

    def ggt(case):
        return sorted((p["collected_offset_days"], r["value"])
                      for p in (case.get("lab_panels") or [])
                      for r in (p.get("results") or []) if r["analyte"] == "GGT")

    # Find the point whose visible window ends on a FALLING, low GGT.
    idx = next(i for i, p in enumerate(points[:-1])
               if ggt(p["case"]) and ggt(p["case"])[-1][1] < 200)
    visible = ggt(points[idx]["case"])
    assert visible[-1][1] < 200, "the physician should be looking at a reassuring trend"

    delta = real_cases.outcome_delta(
        points[idx + 1]["case"],
        outcome_index_offset=(points[idx + 1]["generation"] or {})["index_event_offset"],
        decision_index_offset=(points[idx]["generation"] or {})["index_event_offset"])
    revealed = ggt(delta)
    assert revealed, "the reveal carried no GGT to check the prediction against"
    assert revealed[0][1] > 500, (
        f"the falsifier did not fire: saw {visible[-1][1]} then {revealed[0][1]}")
    assert all(day > 0 for day, _ in revealed), "the reveal leaked a pre-decision value"
