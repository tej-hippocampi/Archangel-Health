"""GET /me/stats: the dashboard's personal tracking widget.

Real numbers only, and no earnings data, because none exists in this schema.
Total cases completed, cases in the last 7 days, the timestamp of the last
submission, a monthly series, and a day streak, all scoped to the calling
evaluator.

The streak is DERIVED from ``submissions.created_at`` every time it is read
rather than kept as a counter. That is the property most of the tests below
are about: a stored streak drifts from the history panel printed next to it
the first time a job misses or a submission is backfilled, and the physician
reading both is the one who finds out.
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta
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


def _submit_on(store, evaluator_id, days_ago):
    """One submission stamped a given number of days before today.

    The store stamps ``created_at`` itself, so the date is rewritten after the
    insert. Crafting the timestamps is the only way to test a streak without
    waiting days for one to accumulate.
    """
    task = _insert_task(store)
    sub_id = uuid.uuid4().hex
    store.insert_submission(
        submission_id=sub_id, task_id=task["task_id"], evaluator_id=evaluator_id,
        verdict="A_better", chosen_id="A", rejected_id="B", confidence="high",
        time_spent_sec=30, payload={}, annotator={},
        dedupe_hash=None, grounded=False, grounding_mode="optional",
        portal_version="v3", status="submitted",
    )
    when = (datetime.utcnow() - timedelta(days=days_ago)).replace(microsecond=0)
    with store._conn() as conn:
        conn.execute("UPDATE submissions SET created_at = ? WHERE submission_id = ?",
                     (when.isoformat(), sub_id))
    return sub_id


def test_a_run_of_consecutive_days_reads_back_as_a_streak():
    """The number a physician sees for their own consistency has to match the
    history panel beside it, so it is computed from the same submission rows
    that panel is drawn from rather than kept in a counter."""
    store = A.fresh_store()
    ev = A.make_user(store, role="evaluator", specialty="nephrology")
    for days_ago in (0, 1, 2):
        _submit_on(store, ev["id"], days_ago)

    body = client.get("/api/asclepius/me/stats", headers=A.headers_for(ev)).json()
    assert body["day_streak"] == 3


def test_a_missed_day_ends_the_streak_at_the_gap():
    """A streak that survives a gap is not a streak, it is a total wearing a
    different label."""
    store = A.fresh_store()
    ev = A.make_user(store, role="evaluator", specialty="nephrology")
    for days_ago in (0, 1, 3, 4, 5):
        _submit_on(store, ev["id"], days_ago)

    body = client.get("/api/asclepius/me/stats", headers=A.headers_for(ev)).json()
    assert body["day_streak"] == 2


def test_yesterday_still_counts_as_a_live_streak():
    """A physician who worked last night and has not opened the portal yet this
    morning has broken nothing. Resetting at midnight punishes the timezone."""
    store = A.fresh_store()
    ev = A.make_user(store, role="evaluator", specialty="nephrology")
    for days_ago in (1, 2):
        _submit_on(store, ev["id"], days_ago)

    body = client.get("/api/asclepius/me/stats", headers=A.headers_for(ev)).json()
    assert body["day_streak"] == 2


def test_an_old_run_of_days_is_not_a_current_streak():
    """"Current" is the whole claim. A run that ended a fortnight ago reported
    as live would be the dashboard telling somebody they are on a roll they
    stopped being on."""
    store = A.fresh_store()
    ev = A.make_user(store, role="evaluator", specialty="nephrology")
    for days_ago in (14, 15, 16):
        _submit_on(store, ev["id"], days_ago)

    body = client.get("/api/asclepius/me/stats", headers=A.headers_for(ev)).json()
    assert body["day_streak"] == 0


def test_two_submissions_on_one_day_are_one_day_of_streak():
    """Otherwise a single busy evening reports as a week of consistency."""
    store = A.fresh_store()
    ev = A.make_user(store, role="evaluator", specialty="nephrology")
    _submit_on(store, ev["id"], 0)
    _submit_on(store, ev["id"], 0)
    _submit_on(store, ev["id"], 1)

    body = client.get("/api/asclepius/me/stats", headers=A.headers_for(ev)).json()
    assert body["day_streak"] == 2


def test_no_submissions_is_a_zero_streak_not_an_absent_field():
    """The dashboard renders whatever it is handed. An absent key reads as a
    panel that failed rather than as a physician who has not started."""
    store = A.fresh_store()
    ev = A.make_user(store, role="evaluator", specialty="nephrology")
    body = client.get("/api/asclepius/me/stats", headers=A.headers_for(ev)).json()
    assert body["day_streak"] == 0


def test_me_stats_requires_auth():
    A.fresh_store()
    r = client.get("/api/asclepius/me/stats")
    assert r.status_code in (401, 403)
