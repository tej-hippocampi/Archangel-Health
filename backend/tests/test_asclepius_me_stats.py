"""GET /me/stats: the dashboard's personal tracking widget.

Real numbers only, no earnings/streak (that data doesn't exist anywhere in
this schema) — total cases completed, cases in the last 7 days, and the
timestamp of the last submission, scoped to the calling evaluator.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests._asclepius as A  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(A.app)


def _submit(store, evaluator_id, task_id):
    store.insert_submission(
        submission_id=uuid.uuid4().hex, task_id=task_id, evaluator_id=evaluator_id,
        verdict="A_better", chosen_id="A", rejected_id="B", confidence="high",
        time_spent_sec=30, payload={}, annotator={},
        dedupe_hash=None, grounded=False, grounding_mode="optional",
        portal_version="v3", status="submitted",
    )


def _insert_task(store, specialty="nephrology"):
    return store.insert_task(
        prompt=f"case {uuid.uuid4().hex[:6]}", specialty=specialty,
        difficulty="medium", source="synthetic", created_by="system:test",
        candidate_answers=[{"id": "A", "text": "a"}, {"id": "B", "text": "b"}],
    )


def test_me_stats_counts_only_this_evaluators_submissions():
    store = A.fresh_store()
    ev = A.make_user(store, role="evaluator", specialty="nephrology")
    other = A.make_user(store, role="evaluator", specialty="nephrology")
    t1, t2, t3 = _insert_task(store), _insert_task(store), _insert_task(store)
    _submit(store, ev["id"], t1["task_id"])
    _submit(store, ev["id"], t2["task_id"])
    _submit(store, other["id"], t3["task_id"])  # not this evaluator's

    r = client.get("/api/asclepius/me/stats", headers=A.headers_for(ev))
    assert r.status_code == 200
    body = r.json()
    assert body["submissions_total"] == 2
    assert body["submissions_this_week"] == 2
    assert body["last_submission_at"] is not None


def test_me_stats_zero_for_evaluator_with_no_submissions():
    store = A.fresh_store()
    ev = A.make_user(store, role="evaluator", specialty="nephrology")
    r = client.get("/api/asclepius/me/stats", headers=A.headers_for(ev))
    assert r.status_code == 200
    body = r.json()
    assert body["submissions_total"] == 0
    assert body["submissions_this_week"] == 0
    assert body["last_submission_at"] is None


def test_me_stats_requires_auth():
    A.fresh_store()
    r = client.get("/api/asclepius/me/stats")
    assert r.status_code in (401, 403)
