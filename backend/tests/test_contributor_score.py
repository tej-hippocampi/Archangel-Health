"""PRD-SCORE — the contributor score is deterministic, explainable, and safe.

The score is a pure function of the graded record: same inputs, same number,
and every point has a named component. These tests drive the formula directly
(table-driven), then the storage layer, then the seam: a QA decision and a
review verdict each fold into the physician's score without being able to
take the grade down with them.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import contributor_score as cs  # noqa: E402
from asclepius.store import get_store  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated():
    A.fresh_store()
    yield


def _store():
    return get_store()


def _sub(**kw):
    base = {"submission_id": "sub-1", "status": "export_ready", "qa": None,
            "payload": {}, "time_spent_sec": None, "agreement_score": None}
    base.update(kw)
    return base


# ═══ The formula ═════════════════════════════════════════════════════════════
def test_outcomes_map_to_their_bases():
    assert cs.case_score(_sub(status="export_ready"), None, [])["score"] == 85
    assert cs.case_score(_sub(status="rejected"), None, [])["score"] == 30
    scored = cs.case_score(_sub(), None, [{"verdict": "accept_with_edits"}])
    assert scored["components"]["outcome"] == "accepted_with_edits"
    assert scored["score"] == 70


def test_an_ungraded_submission_scores_nothing():
    assert cs.case_score(_sub(status="submitted"), None, []) is None


def test_the_worst_review_verdict_stands():
    scored = cs.case_score(_sub(), None, [{"verdict": "accept"}, {"verdict": "reject"}])
    assert scored["components"]["outcome"] == "rejected"


def test_citations_and_reasoning_are_bounded_bonuses():
    payload = {
        "chosen_revision": {"evidence_anchors": [{"quote": "q"}] * 9},
        "reasoning_steps": [{"step": i} for i in range(14)],
    }
    scored = cs.case_score(_sub(payload=payload), None, [])
    assert scored["components"]["citation_bonus"] == 5
    assert scored["components"]["reasoning_bonus"] == 5.0
    assert scored["score"] == 95


def test_time_is_normalized_by_measured_difficulty_over_declared():
    task_declared = {"difficulty": "hard"}
    task_measured = {"difficulty": "easy", "empirical_difficulty": 1.0,
                     "difficulty_measured": 1}
    # 20 minutes on a declared-hard case (expected 35m): inside the band.
    inside = cs.case_score(_sub(time_spent_sec=20 * 60), task_declared, [])
    assert inside["components"]["time_adj"] == 3.0
    # 4 minutes on a case every frontier model fails (expected 40m): rushed.
    rushed = cs.case_score(_sub(time_spent_sec=4 * 60), task_measured, [])
    assert rushed["components"]["time_adj"] == -5.0
    assert rushed["components"]["expected_minutes"] == 40.0


def test_agreement_adjusts_by_at_most_five_points_each_way():
    up = cs.case_score(_sub(agreement_score=1.0), None, [])
    down = cs.case_score(_sub(agreement_score=0.0), None, [])
    assert up["components"]["agreement_adj"] == 5.0
    assert down["components"]["agreement_adj"] == -5.0


def test_the_case_score_is_clamped_to_the_scale():
    payload = {"reasoning_steps": [{}] * 10,
               "from_scratch": {"evidence_anchors": [{}] * 5}}
    scored = cs.case_score(
        _sub(payload=payload, agreement_score=1.0, time_spent_sec=30 * 60),
        {"difficulty": "medium"}, [])
    assert scored["score"] <= 100


# ═══ Shrinkage ═══════════════════════════════════════════════════════════════
def _physician(tier_score=None):
    store = _store()
    u = A.make_user(store, role="evaluator", specialty="nephrology")
    with store._conn() as conn:
        conn.execute(
            "UPDATE users SET verification_status = 'approved', tier = 'labeler', "
            "tier_score = ? WHERE id = ?", (tier_score, u["id"]))
    return store.get_user_by_id(u["id"])


def _graded_submission(store, user, sid, *, status="export_ready"):
    task_id = f"task-{sid}"
    store.insert_task(task_id=task_id, specialty="nephrology", difficulty="hard",
                      capture_reasoning=False, source="lab_supplied",
                      prompt="p", candidate_answers=[{"id": "a", "text": "x"},
                                                     {"id": "b", "text": "y"}])
    store.insert_submission(
        submission_id=sid, task_id=task_id, evaluator_id=user["id"],
        verdict="a_better", chosen_id="a", rejected_id="b",
        confidence="4", time_spent_sec=1500, payload={}, annotator={},
        dedupe_hash=sid)
    with store._conn() as conn:
        conn.execute("UPDATE submissions SET status = ? WHERE submission_id = ?",
                     (status, sid))
    return sid


def test_one_case_cannot_crater_a_strong_prior():
    """K=5 shrinkage: a 90-prior physician's first rejected case moves them,
    but nowhere near the raw case score."""
    store = _store()
    doc = _physician(tier_score=90)
    _graded_submission(store, doc, "sub-bad", status="rejected")
    result = cs.compute(store, doc["id"])
    assert result["n_cases"] == 1
    # Case = 30 (rejected) + 3 (25 careful minutes on a hard case)
    #        + 3.6 (a DECLARED-hard case: 0.8 on the 0..1 scale, and the term is
    #              centred on medium, so 6.0 * (0.8 - 0.5) * 2) = 36.6;
    # blended = (5*90 + 36.6) / 6. The prior holds the floor up.
    #
    # The difficulty term is why this is 81.1 rather than the 80.5 it was before
    # difficulty entered the score directly. The property under test is
    # unchanged and is the point: a 90-prior physician's first rejected case
    # moves them by single digits, not to the raw case score.
    assert result["score"] == 81.1
    assert result["score"] < result["prior"]
    assert result["prior"] == 90.0


def test_the_score_is_deterministic_and_idempotent():
    store = _store()
    doc = _physician(tier_score=72)
    _graded_submission(store, doc, "sub-1")
    first = cs.recompute_and_store(store, doc["id"])
    second = cs.recompute_and_store(store, doc["id"])
    assert first["score"] == second["score"]
    # The history did not stack a duplicate row for the same submission.
    hist = store.contributor_score_history(doc["id"])
    assert len([r for r in hist if r["submission_id"] == "sub-1"]) == 1


def test_a_pre_verification_physician_still_gets_a_live_prior():
    store = _store()
    u = A.make_user(store, role="evaluator", specialty="nephrology")
    with store._conn() as conn:
        conn.execute("UPDATE users SET verification_status = 'pending', "
                     "tier = NULL, tier_score = NULL WHERE id = ?", (u["id"],))
    result = cs.compute(store, store.get_user_by_id(u["id"])["id"])
    assert result is not None
    assert result["prior_source"] in ("proposal", "default")
    assert 0 <= result["score"] <= 100


# ═══ The seam ════════════════════════════════════════════════════════════════
def test_a_qa_decision_folds_into_the_stored_score():
    from asclepius import pipeline

    store = _store()
    doc = _physician(tier_score=60)
    sid = _graded_submission(store, doc, "sub-qa", status="submitted")
    sub = store.get_submission(sid)
    pipeline.apply_qa_decision(store, sub, decision="approve",
                               reviewer_id="admin@x", notes=None)
    stored = store.get_contributor_score(doc["id"])
    assert stored is not None and stored["n_cases"] == 1
    assert stored["score"] > 60  # an accepted case lifts a 60 prior


def test_a_scoring_failure_never_takes_the_grade_down(monkeypatch):
    from asclepius import pipeline, contributor_score

    store = _store()
    doc = _physician(tier_score=60)
    sid = _graded_submission(store, doc, "sub-boom", status="submitted")
    monkeypatch.setattr(contributor_score, "recompute_and_store",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    sub = store.get_submission(sid)
    status = pipeline.apply_qa_decision(store, sub, decision="approve",
                                        reviewer_id="admin@x", notes=None)
    assert status == "export_ready"  # the decision stood


def test_band_words_align_with_the_tiering_thresholds():
    assert cs.band_word(84) == "Reviewer band"
    assert cs.band_word(70) == "Reviewer band"
    assert cs.band_word(45) == "Labeler band"
    assert cs.band_word(10) == "Building"
    assert cs.band_word(None) == "Unrated"
