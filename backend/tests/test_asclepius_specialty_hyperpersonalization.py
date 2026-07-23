"""Specialty Hyper-Personalization PRD — acceptance tests (cardiology + oncology).

Covers the §10 acceptance criteria: the specialty picker + serving, the structured
``studies`` schema + per-specialty content gate, the empirical-difficulty column +
serving gate, the buyer-facing additive export fields + per-specialty manifest
breakdown, and the catastrophic critical-negative rubric auto-seed.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests._asclepius as A  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from routers.asclepius import _store  # noqa: E402
from asclepius import specialties as asc_specialties  # noqa: E402
from asclepius.cases import (  # noqa: E402
    assert_multimodal_content, MultimodalContentError, case_type_signature,
    render_case_prompt,
)

client = TestClient(A.app)


# ─── §10.4 taxonomies registered + enabled ────────────────────────────────────
def test_cardiology_and_oncology_registered_enabled():
    for sp in ("cardiology", "oncology"):
        cfg = asc_specialties.get_specialty_config(sp)  # raises if not enabled
        assert cfg.enabled
        assert sum(b.target_count for b in cfg.taxonomy) == 100
        assert all(b.min_difficulty == "hard" for b in cfg.taxonomy)


def test_specialties_endpoint_carries_accents_no_blue():
    ev = A.make_user(_store(), role="evaluator", specialty="nephrology")
    r = client.get("/api/asclepius/specialties", headers=A.headers_for(ev))
    assert r.status_code == 200
    accents = {s["specialty"]: s["accent"] for s in r.json()["specialties"]}
    assert accents["nephrology"] == "green"
    assert accents["cardiology"] == "orange"
    assert accents["oncology"] == "pink"
    assert "blue" not in set(accents.values())


# ─── §10.3 studies schema + per-specialty content gate ────────────────────────
def _base_case(**kw):
    c = {
        "specialty": "nephrology",
        "problem_list": [{"condition": "x"}], "medications": [{"drug": "y"}],
        "lab_panels": [{"panel": "BMP", "results": [
            {"analyte": "K", "value": 6, "unit": "mmol/L", "ref_low": 3.5, "ref_high": 5, "flag": "H"},
            {"analyte": "Cr", "value": 2, "unit": "mg/dL", "ref_low": 0.7, "ref_high": 1.3, "flag": "H"}]}],
        "notes": [{"text": "z" * 200}],
    }
    c.update(kw)
    return c


def test_nephrology_unaffected_by_study_requirement():
    assert_multimodal_content(_base_case())  # no studies, still valid


def test_cardiology_requires_ecg_or_echo():
    with __import__("pytest").raises(MultimodalContentError):
        assert_multimodal_content(_base_case(specialty="cardiology"))
    assert_multimodal_content(_base_case(specialty="cardiology",
                                         studies=[{"modality": "ecg", "findings": "ST elevation"}]))


def test_oncology_requires_path_imaging_or_molecular():
    with __import__("pytest").raises(MultimodalContentError):
        assert_multimodal_content(_base_case(specialty="oncology",
                                             studies=[{"modality": "ecg", "findings": "x"}]))
    assert_multimodal_content(_base_case(specialty="oncology",
                                         studies=[{"modality": "molecular", "findings": "EGFR"}]))


def test_render_case_prompt_includes_study_findings_and_measurements():
    case = _base_case(specialty="cardiology", studies=[{
        "modality": "ecg", "label": "12-lead ECG", "findings": "Wellens T-waves V2-V3",
        "measurements": [{"analyte": "QTc", "value": 432, "unit": "ms", "ref_low": 350, "ref_high": 450, "flag": ""}]}])
    text = render_case_prompt(case, "Disposition?")
    assert "Studies:" in text and "Wellens T-waves" in text and "QTc" in text


def test_case_type_signature():
    case = _base_case(specialty="cardiology", studies=[
        {"modality": "ecg", "findings": "x"}, {"modality": "echo", "findings": "y"}])
    assert case_type_signature(case) == "multimodal:labs+notes+ecg+echo"


# ─── §10.9 gold cases load for all three specialties ──────────────────────────
def test_gold_cases_load_per_specialty():
    A.fresh_store()
    admin = A.make_user(_store(), role="admin")
    ah = A.headers_for(admin)
    for sp in ("nephrology", "cardiology", "oncology"):
        r = client.post(f"/api/asclepius/generation/{sp}/load-gold", headers=ah)
        assert r.status_code == 200, r.text
        assert r.json()["loaded"] == 10


# ─── §10.1 the picker drives task fetch ───────────────────────────────────────
def test_tasks_next_specialty_param_serves_that_specialty(monkeypatch):
    monkeypatch.setenv("ASCLEPIUS_AUTOFILL", "0")
    A.fresh_store()
    ev = A.make_user(_store(), role="evaluator", specialty="nephrology")
    h = A.headers_for(ev)
    for sp in ("cardiology", "oncology"):
        r = client.get(f"/api/asclepius/tasks/next?portal_version=v3&specialty={sp}", headers=h)
        assert r.status_code == 200
        task = r.json()["task"]
        assert task is not None and task["specialty"] == sp
        assert task["modality"] == "multimodal"
        assert task["case"].get("studies")
        # §10.2/§9 empirical_difficulty is surfaced (declared, not yet measured).
        assert task["empirical_difficulty"] is not None
        assert task["difficulty_measured"] is False


def test_unknown_specialty_falls_back_not_error(monkeypatch):
    monkeypatch.setenv("ASCLEPIUS_AUTOFILL", "0")
    A.fresh_store()
    ev = A.make_user(_store(), role="evaluator", specialty="nephrology")
    r = client.get("/api/asclepius/tasks/next?portal_version=v3&specialty=dermatology",
                   headers=A.headers_for(ev))
    assert r.status_code == 200  # graceful — never a 500 on a bad specialty


# ─── §9 empirical-difficulty serving gate ─────────────────────────────────────
def test_require_measured_difficulty_gate_blocks_unmeasured(monkeypatch):
    from asclepius.gold_cases import load_gold_cases
    A.fresh_store()
    st = _store()
    load_gold_cases(st, specialty="cardiology")
    # Declared (measured=0) seeds serve with the gate off...
    assert st.next_task_for_evaluator(evaluator_id="e1", specialty="cardiology",
                                      hard_only=True, multimodal_only=True) is not None
    # ...and are refused when a live-measured floor is required (§9 prod posture).
    assert st.next_task_for_evaluator(evaluator_id="e1", specialty="cardiology",
                                      hard_only=True, multimodal_only=True,
                                      require_measured_difficulty=True,
                                      min_empirical_difficulty=0.5) is None


# ─── §2 additive export fields + per-specialty manifest breakdown ─────────────
def test_export_counts_carry_specialty_breakdown():
    from asclepius.export import _counts
    recs = [
        {"type": "preference", "specialty": "cardiology", "payload": {
            "context": {"modality": "multimodal"}, "taxonomy_bucket": "great_mimics",
            "case_type": "multimodal:labs+ecg+echo", "ai_failure_mode": "anchoring",
            "empirical_difficulty": 0.8, "empirical_difficulty_measured": False}},
        {"type": "reasoning_trace", "specialty": "oncology", "payload": {
            "context": {"modality": "multimodal"}, "taxonomy_bucket": "molecular_therapy_selection",
            "case_type": "multimodal:molecular", "ai_failure_mode": "right_answer_wrong_reason",
            "empirical_difficulty": 0.9, "empirical_difficulty_measured": True}},
    ]
    c = _counts(recs)
    assert c["by_specialty"] == {"cardiology": 1, "oncology": 1}
    assert "great_mimics" in c["by_taxonomy_bucket"]
    assert "multimodal:molecular" in c["by_case_type"]
    cb = c["specialty_breakdown"]["cardiology"]
    assert cb["count"] == 1 and cb["failure_modes"] == {"anchoring": 1}
    assert cb["mean_empirical_difficulty"] == 0.8


def test_packaging_stamps_additive_fields_on_records():
    from asclepius.packaging import _specialty_case_fields
    task = {"specialty": "cardiology", "empirical_difficulty": 0.8, "difficulty_measured": 0,
            "generation": {"taxonomy_bucket": "great_mimics", "subtopic": "dissection_as_mi",
                           "case_type": "multimodal:labs+ecg", "ai_failure_mode": "anchoring"}}
    f = _specialty_case_fields(task)
    for k in ("specialty", "taxonomy_bucket", "subtopic", "case_type", "ai_failure_mode",
              "empirical_difficulty", "empirical_difficulty_measured"):
        assert k in f
    assert f["specialty"] == "cardiology" and f["empirical_difficulty"] == 0.8


# ─── §8.3 catastrophic critical-negative rubric auto-seed ─────────────────────
def test_catastrophic_action_autoseeds_critical_negative():
    from asclepius.rubric import propose_rubric, normalize_rubric, has_critical_negative
    task = {"generation": {"ai_failure_mode": "anchoring; catastrophic unsafe_recommendation"},
            "case": {"ground_truth": {"key_data": ["inter-arm BP differential"]}}}
    crit = normalize_rubric(propose_rubric(task, {"verdict": "A_better"}))
    assert has_critical_negative(crit)
    assert any(c.get("source") == "catastrophic_unsafe_recommendation" for c in crit)
