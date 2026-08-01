"""B → A integration seam: a verified physician can actually review.

The hackathon round's defining failure was two agents each building a correct
half that could not find each other (Seam 1: B exported
``window.AsclepiusVerification``, C looked for ``renderVerificationQueue``).
This file exists so the same class of failure cannot happen between PRD B's
tier assignment and PRD A's reviewer gate.

Every fact here is produced by the REAL routes: B's admin approval endpoint
writes the tier, and A's review portal reads it. Nothing is hand-stitched —
no direct ``UPDATE users SET tier``, no ``insert_case_review``.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import review as asc_review  # noqa: E402
from asclepius import store as asc_store  # noqa: E402

client = TestClient(A.app)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    A.fresh_store()
    from asclepius import pipeline as asc_pipeline

    async def _ok_critic(task, submission):
        return {"consistent": True, "issues": [], "skipped": True}

    async def _ok_grounding(task, submission):
        return {"grounding_ok": True, "issues": [], "skipped": True, "checked_anchors": 0}

    monkeypatch.setattr(asc_pipeline, "run_critic", _ok_critic)
    monkeypatch.setattr(asc_pipeline, "run_grounding_check", _ok_grounding)
    yield


def _admin():
    return A.make_user(asc_store.get_store(), role="admin")


def _physician(**kw):
    return A.make_user(asc_store.get_store(), role="evaluator",
                       specialty="nephrology", board_cert="board_certified_nephrology",
                       years_experience=22, **kw)


def _approve_via_b(admin_h, user_id, tier):
    """PRD B's REAL admin approval route — the only thing that assigns a tier."""
    return client.post(f"/api/asclepius/verify/queue/{user_id}/approve",
                       json={"tier": tier, "note": "Credentials verified."},
                       headers=admin_h)


def _labeled_submission(admin_h, labeler):
    body = {
        "specialty": "nephrology", "difficulty": "hard", "max_labels": 1,
        "prompt": f"Hyperkalemia case {A.uniq(8)}?",
        "candidate_answers": [{"id": "A", "text": "Calcium then dialyze."},
                              {"id": "B", "text": "Dialysate K+ 1.0."}],
    }
    tid = client.post("/api/asclepius/tasks", json={"tasks": [body]},
                      headers=admin_h).json()["created"][0]
    sid, salt = "s-" + uuid.uuid4().hex[:12], A.uniq(6)
    r = client.post("/api/asclepius/submissions", json={
        "submission_id": sid, "task_id": tid, "verdict": "A_better",
        "chosen_id": "A", "rejected_id": "B", "time_spent_sec": 130,
        "prompt_review": {"reviewed": True, "verdict": "valid"},
        "independent_answer": {"text": f"Stabilize then dialyze ({salt})."},
        "chosen_revision": {"edited": False, "why_better_notes": f"B over-lowers K+ ({salt})"},
        "rejected_critique": {"error_tags": ["dosing_error"], "why_worse": f"aggressive {salt}"},
    }, headers=A.headers_for(labeler))
    assert r.status_code == 200, r.text
    return tid, sid


def test_b_approval_as_reviewer_unlocks_the_a_review_portal():
    """THE SEAM. B writes users.tier; A's gate reads it. If the two ever disagree
    about the word 'reviewer', no physician can review and nothing errors."""
    store = asc_store.get_store()
    admin_h = A.headers_for(_admin())
    labeler = _physician()
    reviewer = _physician()
    _, sid = _labeled_submission(admin_h, labeler)

    # Before B approves, the reviewer tier is unassigned -> denied. NULL is not
    # 'no', but for access control both deny (PRD A §1.2).
    assert client.get("/api/asclepius/review/next",
                      headers=A.headers_for(reviewer)).status_code == 403

    assert _approve_via_b(admin_h, reviewer["id"], "reviewer").status_code == 200
    # The tier B actually wrote is the tier A actually requires.
    assert store.get_user_by_id(reviewer["id"])["tier"] == "reviewer"
    assert asc_review.can_review(store.get_user_by_id(reviewer["id"])) is True

    # And the portal now opens, end to end.
    drawn = client.get("/api/asclepius/review/next", headers=A.headers_for(reviewer))
    assert drawn.status_code == 200
    view = drawn.json()["submission"]
    assert view is not None and view["submission_id"] == sid
    assert view["blinded"] is True

    posted = client.post(f"/api/asclepius/review/{sid}", json={
        "verdict": "accept",
        "dimensions": {k: "agree" for k in asc_review.DIMENSION_KEYS},
        "time_spent_sec": 40,
    }, headers=A.headers_for(reviewer))
    assert posted.status_code == 200
    stored = store.reviews_for_submission(sid)[0]
    assert stored["verdict"] == "accept" and stored["blinded"] == 1


def test_b_approval_as_labeler_does_not_unlock_review():
    """The tier is a real distinction, not a formality: a labeler is a verified
    physician who may not grade other physicians' work."""
    store = asc_store.get_store()
    admin_h = A.headers_for(_admin())
    labeler = _physician()
    approved_labeler = _physician()
    _labeled_submission(admin_h, labeler)

    assert _approve_via_b(admin_h, approved_labeler["id"], "labeler").status_code == 200
    assert store.get_user_by_id(approved_labeler["id"])["tier"] == "labeler"
    assert asc_review.can_review(store.get_user_by_id(approved_labeler["id"])) is False
    assert client.get("/api/asclepius/review/next",
                      headers=A.headers_for(approved_labeler)).status_code == 403


def test_the_two_modules_share_one_tier_vocabulary():
    """Static guard against the Seam-1 failure mode: B validating one set of
    tier strings while A's gate compares against a different one."""
    import routers.asclepius_verify as verify

    assert "reviewer" in verify._TIERS
    # A's gate must accept exactly what B is allowed to write, and nothing else.
    for tier in verify._TIERS:
        expected = (tier == "reviewer")
        assert asc_review.can_review({"tier": tier}) is expected


def test_rejected_physician_never_reaches_the_review_portal():
    store = asc_store.get_store()
    admin_h = A.headers_for(_admin())
    rejected = _physician()
    r = client.post(f"/api/asclepius/verify/queue/{rejected['id']}/reject",
                    json={"note": "Could not verify board certification."},
                    headers=admin_h)
    assert r.status_code == 200
    user = store.get_user_by_id(rejected["id"])
    assert user["verification_status"] == "rejected"
    # Rejected and unassigned are different facts, and both deny.
    assert user["tier"] is None
    assert asc_review.can_review(user) is False
    assert client.get("/api/asclepius/review/next",
                      headers=A.headers_for(rejected)).status_code == 403
