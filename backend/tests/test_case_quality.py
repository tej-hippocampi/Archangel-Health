"""The internal case-quality metric: what it measures, and what it will not restate.

Three things were wrong or missing.

``contributor_score``'s docstring has always claimed "the recompute hooks ride
on QA decisions and review submissions". Only the review router ever called it,
so a QA-only-graded submission never moved the stored score, and nothing tested
it either way.

Difficulty entered the score only sideways, through the expected-minutes band,
so a hard case labeled adequately scored the same as an easy one. And what the
case ASKED for did not enter at all, so a bare A/B pick and a grounded
reasoning-trace case were graded on one scale, quietly penalizing whoever drew
the harder queue.

The last section is the one that matters most, because the next change attaches
this number to money: a case keeps the score it was given under the coefficients
in force when it was graded. Recomputing under new weights would silently
restate work a physician has already been paid for and been told the reason for.
Same rule as ``earnings.rate_cents``, which is stamped at accrual.
"""

from __future__ import annotations

import pytest

from tests._asclepius import fresh_store, headers_for, make_user

from asclepius import contributor_score as cs


@pytest.fixture()
def store():
    return fresh_store()


_PAYLOAD = {
    "verdict": "A_better",
    "chosen_id": "A",
    "reasoning_steps": [{"text": "one"}, {"text": "two"}],
    "chosen_revision": {"evidence_anchors": [{"citation_text": "KDIGO 2024"}]},
}


def _insert(store, *, submission_id, task_id, evaluator_id, status, **kw):
    """A stored submission. ``annotator`` and ``dedupe_hash`` are required
    keyword-only args on the store; the values are irrelevant here."""
    return store.insert_submission(
        submission_id=submission_id, task_id=task_id, evaluator_id=evaluator_id,
        verdict="A_better", chosen_id="A", rejected_id="B", confidence="high",
        time_spent_sec=1200, payload=dict(_PAYLOAD), status=status,
        annotator={}, dedupe_hash=submission_id,
    )


def _sub(**kw):
    base = {"submission_id": "s-1", "payload": dict(_PAYLOAD), "status": "export_ready",
            "time_spent_sec": 1200, "agreement_score": None}
    base.update(kw)
    return base


# ─── Difficulty ──────────────────────────────────────────────────────────────

def test_a_hard_case_done_well_outscores_an_easy_one_done_well():
    """It did not, and that is a direct incentive to cherry-pick easy work."""
    hard = cs.case_score(_sub(), {"difficulty": "hard"}, [])
    easy = cs.case_score(_sub(), {"difficulty": "easy"}, [])
    assert hard["score"] > easy["score"]


def test_measured_difficulty_beats_the_declared_label():
    """A declared "hard" that every frontier model aces is not hard."""
    declared_hard = cs.case_score(_sub(), {"difficulty": "hard"}, [])
    measured_easy = cs.case_score(
        _sub(), {"difficulty": "hard", "difficulty_measured": 1,
                 "empirical_difficulty": 0.1}, [])
    assert measured_easy["score"] < declared_hard["score"]


def test_an_unmeasured_unlabelled_case_is_neutral_not_guessed():
    """Guessing "medium" would hand out the same credit as a measured medium,
    which is how an unmeasured queue quietly inflates."""
    assert cs.difficulty_fraction({}) is None
    scored = cs.case_score(_sub(), {}, [])
    assert scored["components"]["difficulty_adj"] == 0.0


def test_a_medium_case_neither_helps_nor_hurts():
    scored = cs.case_score(_sub(), {"difficulty": "medium"}, [])
    assert scored["components"]["difficulty_adj"] == 0.0


# ─── What the case asked for ─────────────────────────────────────────────────

def test_a_case_that_demanded_more_is_worth_more():
    bare = cs.case_score(_sub(), {"difficulty": "medium"}, [])
    rich = cs.case_score(
        _sub(), {"difficulty": "medium", "capture_reasoning": 1,
                 "grounding_mode": "required", "modality": "multimodal"}, [])
    assert rich["score"] > bare["score"]


def test_the_shape_is_read_from_the_task_not_from_what_the_labeler_happened_to_do():
    """Reading it from the payload would credit a physician for work the case
    never asked for, and penalize one who correctly had nothing to add."""
    shape = cs.case_shape({"capture_reasoning": 0}, {"reasoning_steps": [{"text": "x"}] * 9})
    assert shape["reasoning"] is False


def test_the_shape_credit_is_capped():
    shape = {"reasoning": True, "grounded": True, "from_scratch": True, "multimodal": True}
    assert cs._shape_adj(shape) <= cs.SHAPE_ADJ_MAX


# ─── It stays explainable ────────────────────────────────────────────────────

def test_every_case_carries_an_itemized_reason_list():
    """This number gets attached to money, so it will be contested, and "71" is
    not an answer to "why"."""
    scored = cs.case_score(_sub(), {"difficulty": "hard"}, [])
    reasons = scored["components"]["reasons"]
    assert reasons and all(isinstance(r, str) for r in reasons)
    assert any("difficulty" in r for r in reasons)


def test_the_reasons_use_the_same_signed_convention_as_credentialing():
    scored = cs.case_score(_sub(), {"difficulty": "hard"}, [])
    joined = " ".join(scored["components"]["reasons"])
    assert "+" in joined


def test_scoring_the_same_inputs_twice_gives_the_same_answer():
    task = {"difficulty": "hard", "capture_reasoning": 1}
    assert cs.case_score(_sub(), task, []) == cs.case_score(_sub(), task, [])


def test_an_ungraded_submission_has_no_score_at_all():
    """None is the honest answer, and it is what keeps a never-graded case from
    rendering as a zero on the money screen."""
    assert cs.case_score(_sub(status="pending"), {"difficulty": "hard"}, []) is None


# ─── The stamp ───────────────────────────────────────────────────────────────

def test_the_score_is_stamped_with_the_ruleset_that_produced_it(store):
    u = make_user(store, role="evaluator", specialty="nephrology")
    task = store.insert_task(prompt="p", specialty="nephrology", difficulty="hard",
                             candidate_answers=[{"id": "A", "text": "a"},
                                                {"id": "B", "text": "b"}],
                             created_by=u["id"])
    sub = _insert(store, submission_id="s-stamp", task_id=task["task_id"],
                   evaluator_id=u["id"], status="export_ready")
    cs.recompute_for_submission(store, sub["submission_id"])

    stamped = store.submission_quality(sub["submission_id"])
    assert stamped is not None
    assert stamped["version"] == cs.CASE_QUALITY_VERSION
    assert stamped["score"] > 0
    assert stamped["components"]["reasons"]


def test_a_case_stamped_under_an_older_ruleset_is_never_restated(store):
    """The rule the payout depends on. Tuning a coefficient must not silently
    change what a physician was already paid for and told about."""
    u = make_user(store, role="evaluator", specialty="nephrology")
    task = store.insert_task(prompt="p", specialty="nephrology", difficulty="hard",
                             candidate_answers=[{"id": "A", "text": "a"},
                                                {"id": "B", "text": "b"}],
                             created_by=u["id"])
    sub = _insert(store, submission_id="s-old", task_id=task["task_id"],
                   evaluator_id=u["id"], status="export_ready")

    store.stamp_submission_quality(
        sub["submission_id"], score=42.0, components={"reasons": ["from the old rules"]},
        version="2020-01-01.0")

    # A write under the CURRENT ruleset must not overwrite it.
    wrote = store.stamp_submission_quality(
        sub["submission_id"], score=99.0, components={"reasons": ["new"]},
        version=cs.CASE_QUALITY_VERSION)
    assert wrote is False
    assert store.submission_quality(sub["submission_id"])["score"] == 42.0


def test_a_regrade_under_the_same_ruleset_does_land(store):
    """A second reviewer can legitimately turn an accept into a reject. That is
    a correction, not a restatement."""
    u = make_user(store, role="evaluator", specialty="nephrology")
    task = store.insert_task(prompt="p", specialty="nephrology", difficulty="hard",
                             candidate_answers=[{"id": "A", "text": "a"},
                                                {"id": "B", "text": "b"}],
                             created_by=u["id"])
    sub = _insert(store, submission_id="s-regrade", task_id=task["task_id"],
                   evaluator_id=u["id"], status="export_ready")

    store.stamp_submission_quality(sub["submission_id"], score=80.0, components={},
                                   version=cs.CASE_QUALITY_VERSION)
    store.stamp_submission_quality(sub["submission_id"], score=30.0, components={},
                                   version=cs.CASE_QUALITY_VERSION)
    assert store.submission_quality(sub["submission_id"])["score"] == 30.0


def test_a_never_graded_case_has_no_stamp_rather_than_a_zero(store):
    u = make_user(store, role="evaluator", specialty="nephrology")
    task = store.insert_task(prompt="p", specialty="nephrology", difficulty="hard",
                             candidate_answers=[{"id": "A", "text": "a"},
                                                {"id": "B", "text": "b"}],
                             created_by=u["id"])
    sub = _insert(store, submission_id="s-none", task_id=task["task_id"],
                   evaluator_id=u["id"], status="pending")
    assert store.submission_quality(sub["submission_id"]) is None


# ─── The QA hook the docstring always claimed ────────────────────────────────

def test_a_qa_decision_moves_the_stored_score(store):
    """contributor_score.py has said since it was written that "the recompute
    hooks ride on QA decisions and review submissions". Only the review router
    ever called it, so a QA-only-graded submission never moved the stored
    score, and no test looked."""
    from fastapi.testclient import TestClient

    from tests._asclepius import app

    u = make_user(store, role="evaluator", specialty="nephrology")
    qa = make_user(store, role="qa_reviewer", specialty="nephrology")
    task = store.insert_task(prompt="p", specialty="nephrology", difficulty="hard",
                             candidate_answers=[{"id": "A", "text": "a"},
                                                {"id": "B", "text": "b"}],
                             created_by=u["id"])
    sub = _insert(store, submission_id="s-qa", task_id=task["task_id"],
                  evaluator_id=u["id"], status="pending")

    assert store.get_contributor_score(u["id"]) is None

    with TestClient(app) as client:
        resp = client.post(
            f"/api/asclepius/qa/{sub['submission_id']}/decision",
            json={"decision": "approve"}, headers=headers_for(qa),
        )
    assert resp.status_code == 200

    scored = store.get_contributor_score(u["id"])
    assert scored is not None, "a QA decision still does not move the score"
    assert store.submission_quality(sub["submission_id"]) is not None


def test_a_scoring_failure_never_undoes_a_recorded_decision(store, monkeypatch):
    """Best-effort by contract: the grade stands even if the metric falls
    over."""
    from fastapi.testclient import TestClient

    from tests._asclepius import app

    u = make_user(store, role="evaluator", specialty="nephrology")
    qa = make_user(store, role="qa_reviewer", specialty="nephrology")
    task = store.insert_task(prompt="p", specialty="nephrology", difficulty="hard",
                             candidate_answers=[{"id": "A", "text": "a"},
                                                {"id": "B", "text": "b"}],
                             created_by=u["id"])
    sub = _insert(store, submission_id="s-qa-boom", task_id=task["task_id"],
                  evaluator_id=u["id"], status="pending")

    def _boom(*a, **kw):
        raise RuntimeError("scoring is down")

    monkeypatch.setattr(cs, "recompute_and_store", _boom)

    with TestClient(app) as client:
        resp = client.post(
            f"/api/asclepius/qa/{sub['submission_id']}/decision",
            json={"decision": "approve"}, headers=headers_for(qa),
        )
    assert resp.status_code == 200
