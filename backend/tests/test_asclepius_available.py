"""GET /tasks/available: the dashboard's list of tasks an evaluator can pick.

Reuses ``store.eligible_tasks_for_evaluator`` (same eligibility as /tasks/next),
so it must scope to the reviewer's specialty and drop tasks they have already
labeled or that are at capacity.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests._asclepius as A  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(A.app)


def _insert(store, specialty="nephrology", difficulty="medium"):
    return store.insert_task(
        prompt=f"case {uuid.uuid4().hex[:6]}", specialty=specialty,
        difficulty=difficulty, source="synthetic", created_by="system:test",
        candidate_answers=[{"id": "A", "text": "a"}, {"id": "B", "text": "b"}],
    )


def test_available_lists_open_tasks_for_evaluator():
    store = A.fresh_store()
    ev = A.make_user(store, role="evaluator", specialty="nephrology")
    t1 = _insert(store)
    t2 = _insert(store)
    r = client.get("/api/asclepius/tasks/available?specialty=nephrology", headers=A.headers_for(ev))
    assert r.status_code == 200
    body = r.json()
    ids = {t["task_id"] for t in body["tasks"]}
    assert body["count"] == len(body["tasks"])
    assert t1["task_id"] in ids and t2["task_id"] in ids


def test_available_excludes_mine_and_other_specialty():
    store = A.fresh_store()
    ev = A.make_user(store, role="evaluator", specialty="nephrology")
    mine = _insert(store, specialty="nephrology")
    other = _insert(store, specialty="cardiology")
    _insert(store, specialty="nephrology")  # a still-available one
    store.insert_submission(
        submission_id=uuid.uuid4().hex, task_id=mine["task_id"], evaluator_id=ev["id"],
        verdict="A_better", chosen_id="A", rejected_id="B", confidence="high",
        time_spent_sec=30, payload={}, annotator=store.annotator_block(ev),
        dedupe_hash=None, grounded=False, grounding_mode="optional",
        portal_version="v3", status="submitted",
    )
    r = client.get("/api/asclepius/tasks/available?specialty=nephrology", headers=A.headers_for(ev))
    ids = {t["task_id"] for t in r.json()["tasks"]}
    assert mine["task_id"] not in ids   # already labeled by this evaluator
    assert other["task_id"] not in ids  # scoped to the evaluator's specialty


def test_available_requires_auth():
    A.fresh_store()
    r = client.get("/api/asclepius/tasks/available")
    assert r.status_code in (401, 403)
