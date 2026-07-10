"""Multimodal ClinicalCase model + serialization + task integration (PR-A).

Covers the case value model, render_case_prompt, answer-key stripping
(public_case + blinded task), the additive task persistence, and that text
(non-case) tasks are byte-identical to today.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests._asclepius as A  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from asclepius import cases as C  # noqa: E402

client = TestClient(A.app)


def _store():
    from asclepius.store import get_store
    return get_store()


def _case(**over):
    base = dict(
        case_source="synthetic", specialty="nephrology",
        demographics={"age_band": "70-79", "sex": "M"},
        problem_list=[{"condition": "CKD stage 4", "since": "2019"}],
        medications=[{"drug": "hydrochlorothiazide", "dose": "25 mg", "route": "PO", "freq": "daily"}],
        vitals={"BP": "150/90", "HR": 88},
        lab_panels=[{"panel": "BMP", "collected_offset_days": 0, "results": [
            {"analyte": "Sodium", "value": 112, "unit": "mmol/L", "ref_low": 135, "ref_high": 145, "flag": "LL"},
        ]}],
        notes=[{"note_type": "Consult", "author_role": "nephrology", "text": "Euvolemic; on thiazide."}],
        ground_truth={"answer": "Thiazide-associated hyponatremia", "key_data": ["urine osm"]},
        hard_hook="answer hinges on urine studies, not the stem",
        reasoning_divergence="SIADH shortcut ignores the thiazide",
    )
    base.update(over)
    return base


# ─── Model + serialization (pure) ─────────────────────────────────────────────
def test_render_case_prompt_includes_question_labs_note_meds():
    p = C.render_case_prompt(_case(), "Classify the hyponatremia and set a safe correction rate.")
    assert "CLINICAL QUESTION:" in p and "correction rate" in p
    assert "Sodium" in p and "112" in p and "135–145" in p and "LL" in p  # labs table w/ ref + flag
    assert "Euvolemic" in p                                               # note verbatim
    assert "hydrochlorothiazide" in p                                     # meds
    assert "age 70-79" in p                                               # age band, never exact age


def test_render_strips_answer_key_even_if_passed_full():
    # The full case carries ground_truth; the rendered prompt must NOT contain it.
    p = C.render_case_prompt(_case(), "Q?")
    assert "Thiazide-associated hyponatremia" not in p
    assert "urine studies, not the stem" not in p


def test_public_case_strips_internal_only_keys():
    pub = C.public_case(_case())
    assert "ground_truth" not in pub and "hard_hook" not in pub and "reasoning_divergence" not in pub
    assert pub["lab_panels"] and pub["notes"]  # clinical content preserved
    assert C.public_case(None) is None


def test_lab_trend_renders_oldest_to_newest():
    case = _case(lab_panels=[
        {"panel": "Cr", "collected_offset_days": 0, "results": [{"analyte": "Creatinine", "value": 2.6}]},
        {"panel": "Cr", "collected_offset_days": -6, "results": [{"analyte": "Creatinine", "value": 0.9}]},
        {"panel": "Cr", "collected_offset_days": -3, "results": [{"analyte": "Creatinine", "value": 1.4}]},
    ])
    p = C.render_case_prompt(case, "AKI etiology?")
    # Oldest (day -6, 0.9) appears before newest (day 0, 2.6).
    assert p.index("0.9") < p.index("1.4") < p.index("2.6")
    assert "day -6" in p and "day 0 (today)" in p


def test_is_multimodal():
    assert C.is_multimodal({"case": _case()}) is True
    assert C.is_multimodal({"modality": "multimodal"}) is True
    assert C.is_multimodal({"prompt": "text only"}) is False


# ─── Task integration (API + store) ───────────────────────────────────────────
def _admin_h():
    return A.headers_for(A.make_user(_store(), role="admin"))


def _ev_h():
    return A.headers_for(A.make_user(_store(), role="evaluator", specialty="nephrology"))


def test_upload_case_task_renders_prompt_and_persists_case():
    A.fresh_store()
    admin_h = _admin_h()
    r = client.post("/api/asclepius/tasks", json={"tasks": [{
        "specialty": "nephrology", "difficulty": "hard", "source": "lab_supplied",
        "modality": "multimodal",
        "prompt": "Classify the hyponatremia and set a safe correction rate.",
        "case": _case(),
        "candidate_answers": [{"id": "A", "text": "a"}, {"id": "B", "text": "b"}],
    }]}, headers=admin_h)
    assert r.status_code == 200, r.text
    tid = r.json()["created"][0]

    task = _store().get_task(tid)
    assert task["modality"] == "multimodal"
    # Stored prompt is the rendered case (labs table present).
    assert "Sodium" in task["prompt"] and "correction rate" in task["prompt"]
    # Full case (incl. internal ground_truth) persisted server-side.
    assert task["case"]["ground_truth"]["answer"] == "Thiazide-associated hyponatremia"


def test_blinded_task_exposes_public_case_but_not_answer_key():
    A.fresh_store()
    admin_h = _admin_h()
    ev_h = _ev_h()
    r = client.post("/api/asclepius/tasks", json={"tasks": [{
        "specialty": "nephrology", "difficulty": "hard", "modality": "multimodal",
        "prompt": "Q?", "case": _case(),
        "candidate_answers": [{"id": "A", "text": "a"}, {"id": "B", "text": "b"}],
    }]}, headers=admin_h)
    tid = r.json()["created"][0]

    blinded = client.get(f"/api/asclepius/tasks/{tid}", headers=ev_h).json()["task"]
    assert blinded["modality"] == "multimodal"
    assert blinded["case"] is not None
    # Public case has clinical content but NO answer key.
    assert blinded["case"]["lab_panels"]
    assert "ground_truth" not in blinded["case"]
    assert "hard_hook" not in blinded["case"]
    # And the answer text never appears anywhere in the blinded payload.
    import json as _json
    assert "Thiazide-associated hyponatremia" not in _json.dumps(blinded)


def test_text_task_is_unchanged():
    A.fresh_store()
    admin_h = _admin_h()
    r = client.post("/api/asclepius/tasks", json={"tasks": [{
        "specialty": "nephrology", "difficulty": "medium",
        "prompt": "A one-line nephrology question.",
        "candidate_answers": [{"id": "A", "text": "a"}, {"id": "B", "text": "b"}],
    }]}, headers=admin_h)
    tid = r.json()["created"][0]
    task = _store().get_task(tid)
    assert task["modality"] == "text"
    assert task["case"] is None
    assert task["prompt"] == "A one-line nephrology question."  # verbatim, no rendering
