"""The shipped review queue requires admin routing, even for approved reviewers."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from tests import _asclepius as A
from asclepius import review
from routers import asclepius_review

client = TestClient(A.app)


@pytest.fixture(autouse=True)
def restricted_pool(monkeypatch):
    monkeypatch.delenv("ASCLEPIUS_OPEN_CASE_POOL_ENABLED", raising=False)
    monkeypatch.setenv("ASCLEPIUS_DOUBLE_LABEL_HALT", "1")
    monkeypatch.setenv("ASCLEPIUS_REVIEW_RATE", "1")
    monkeypatch.setitem(asclepius_review._SWEEP_STATE, "last", time.monotonic())
    return A.fresh_store()


def reviewer(store, *, role="evaluator", tier="reviewer"):
    user = A.make_user(store, role=role, tier=tier, specialty="nephrology")
    with store._conn() as conn:
        conn.execute("UPDATE users SET verification_status = 'approved' WHERE id = ?",
                     (user["id"],))
    return store.get_user_by_id(user["id"])


def case(store, *, labels=2, max_labels=None, specialty="nephrology", author=None):
    task = store.insert_task(
        prompt=f"Review case {A.uniq()}", specialty=specialty, difficulty="hard",
        candidate_answers=[{"id": "a", "text": "Answer A"},
                           {"id": "b", "text": "Answer B"}],
        max_labels=max_labels if max_labels is not None else labels)
    subs = []
    for index in range(labels):
        user = author if index == 0 and author else A.make_user(store)
        subs.append(store.insert_submission(
            submission_id=f"sub-{A.uniq()}", task_id=task["task_id"],
            evaluator_id=user["id"], verdict="A_better", chosen_id="a", rejected_id="b",
            time_spent_sec=180, confidence="high", dedupe_hash=None, portal_version="v3",
            payload={"verdict": "A_better", "chosen_id": "a", "rejected_id": "b"},
            annotator={"id_hashed": user["id_hashed"], "specialty": specialty}))
    return task, subs


def assign(store, task, user, *, role="review", status="offered", expires_at=None):
    row = store.upsert_assignment(
        task_id=task["task_id"], user_id=user["id"], role=role,
        assigned_by="test-admin", expires_at=expires_at)
    if status != "offered":
        store.set_assignment_status(row["assignment_id"], status)
    return row


def draw(user, *, paired=True, preview=False):
    path = "/api/asclepius/review/pair/next" if paired else "/api/asclepius/review/next"
    response = client.get(path, params={"preview": str(preview).lower()},
                          headers=A.headers_for(user))
    assert response.status_code == 200, response.text
    return response.json()


def stats(user):
    response = client.get("/api/asclepius/review/stats", headers=A.headers_for(user))
    assert response.status_code == 200, response.text
    return response.json()


def submit(user, task, subs, *, paired=True):
    body = {"verdict": "accept", "dimensions": {k: "agree" for k in review.DIMENSION_KEYS},
            "time_spent_sec": 90}
    if paired:
        body.update(stronger="A", accepted_side="A")
        path = f"/api/asclepius/review/pair/{task['task_id']}"
    else:
        path = f"/api/asclepius/review/{subs[0]['submission_id']}"
    return client.post(path, json=body, headers=A.headers_for(user))


def test_approved_reviewer_has_no_work_or_billable_session_without_assignment(restricted_pool):
    store = restricted_pool
    user = reviewer(store)
    pair, pair_subs = case(store)
    single, single_subs = case(store, labels=1)
    assert draw(user)["pair"] is None
    assert draw(user, paired=False)["submission"] is None
    counts = stats(user)
    assert counts["review_ready"] == counts["unreviewed"] == counts["n_reviews"] == 0
    assert submit(user, pair, pair_subs).status_code == 403
    assert submit(user, single, single_subs, paired=False).status_code == 403
    response = client.post("/api/asclepius/sessions", json={"kind": "review"},
                           headers=A.headers_for(user))
    assert response.status_code == 403
    assert store.open_work_session_row(user_id=user["id"], kind="review") is None
    assert store.reviews_for_task(pair["task_id"]) == []
    assert store.task_review_claim(pair["task_id"])["holder"] is None


@pytest.mark.parametrize("paired", [False, True])
def test_assignment_filters_before_scan_limit_and_claim_rechecks_it(restricted_pool, paired):
    store = restricted_pool
    user = reviewer(store)
    first, first_subs = case(store, labels=2 if paired else 1)
    other, _ = case(store, labels=2 if paired else 1)
    target, target_subs = case(store, labels=2 if paired else 1)
    assign(store, first, user, role="label")
    assign(store, other, reviewer(store))
    assignment = assign(store, target, user)
    next_work = store.next_review_pair_for if paired else store.next_review_for
    got = next_work(user["id"], scan_limit=1)
    assert got["task_id"] == target["task_id"]
    store.set_assignment_status(assignment["assignment_id"], "revoked")
    if paired:
        assert not store.claim_task_for_review(target["task_id"], reviewer_id=user["id"])
    else:
        assert not store.claim_submission_for_review(
            target_subs[0]["submission_id"], reviewer_id=user["id"])
    assert submit(user, first, first_subs, paired=paired).status_code == 403
    assert next_work(user["id"], scan_limit=1) is None


@pytest.mark.parametrize("status,expires_at", [
    ("revoked", None), ("done", None), ("expired", None),
    ("offered", "2000-01-01T00:00:00+00:00"), ("claimed", "invalid-time"),
])
def test_inactive_or_expired_review_assignment_is_not_permission(restricted_pool, status, expires_at):
    store = restricted_pool
    user = reviewer(store)
    task, _ = case(store)
    assign(store, task, user, status=status, expires_at=expires_at)
    assert draw(user)["pair"] is None
    assert stats(user)["review_ready"] == 0


@pytest.mark.parametrize("paired", [False, True])
def test_live_claim_survives_assignment_revocation_and_can_finish(restricted_pool, paired):
    store = restricted_pool
    user = reviewer(store)
    task, subs = case(store, labels=2 if paired else 1)
    assignment = assign(store, task, user, status="claimed")
    key = "pair" if paired else "submission"
    assert draw(user, paired=paired)[key]["task_id"] == task["task_id"]
    store.set_assignment_status(assignment["assignment_id"], "revoked")
    assert draw(user, paired=paired)[key]["task_id"] == task["task_id"]
    response = submit(user, task, subs, paired=paired)
    assert response.status_code == 200, response.text
    assert stats(user)["n_reviews"] == 1
    assert draw(user, paired=paired)[key] is None


@pytest.mark.parametrize("paired", [False, True])
def test_drawn_review_keeps_chart_images_without_labeling_prior_points(restricted_pool, monkeypatch, paired):
    from asclepius import assets
    store = restricted_pool
    user = reviewer(store)
    store.set_real_data_approved(user["id"], True)
    user = store.get_user_by_id(user["id"])
    # A non-default lease must govern images and the review itself equally.
    monkeypatch.setenv("ASCLEPIUS_REVIEW_LEASE_MIN", "120")
    asset = {"asset_id": "review-chart", "sha256": "c" * 64, "mime": "image/png"}
    chart = {"case_source": "real_deid", "notes": [{"text": "Fixture chart"}],
             "studies": [{"kind": "image", "asset": asset}]}
    previous = store.insert_task(prompt="Earlier sealed chart point", trajectory_id="review-walk", sequence_index=0)
    task, _ = case(store, labels=2 if paired else 1)
    store.update_task_case(task["task_id"], chart)
    with store._conn() as conn:
        conn.execute("UPDATE tasks SET trajectory_id='review-walk', sequence_index=1 WHERE task_id=?", (task["task_id"],))
    # The same image is indexed against a different case. Access must follow
    # its actual chart membership, including a held claim after rerouting.
    other = store.insert_task(prompt="Other case with the same image", case=chart)
    store.insert_asset_ref(**asset, task_id=other["task_id"], case_source="real_deid")
    assignment = assign(store, task, user)
    reads = []
    def load(ref):
        reads.append(ref["asset_id"])
        return b"fixture-image", "image/png"
    monkeypatch.setattr(assets, "load_asset", load)
    url = "/api/asclepius/assets/review-chart"
    headers = A.headers_for(user)
    # An undrawn future review assignment is not permission to look ahead.
    assert client.get(url, headers=headers).status_code == 403
    key = "pair" if paired else "submission"
    assert draw(user, paired=paired)[key]["task_id"] == task["task_id"]
    assert client.get(url, headers=headers).content == b"fixture-image"
    store.set_assignment_status(assignment["assignment_id"], "revoked")
    table = "tasks" if paired else "submissions"
    with store._conn() as conn:
        stamp = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()
        conn.execute(f"UPDATE {table} SET review_claimed_at=? WHERE task_id=?", (stamp, task["task_id"]))
    assert client.get(url, headers=headers).content == b"fixture-image"
    monkeypatch.setenv("ASCLEPIUS_REVIEW_LEASE_MIN", "45")
    assert client.get(url, headers=headers).status_code == 403
    assert reads == ["review-chart", "review-chart"]
    assert store.submissions_for_task(previous["task_id"]) == []


@pytest.mark.parametrize("paired", [False, True])
def test_expired_claim_cannot_restore_revoked_work(restricted_pool, paired):
    store = restricted_pool
    user = reviewer(store)
    task, subs = case(store, labels=2 if paired else 1)
    assignment = assign(store, task, user)
    draw(user, paired=paired)
    store.set_assignment_status(assignment["assignment_id"], "revoked")
    with store._conn() as conn:
        table = "tasks" if paired else "submissions"
        conn.execute(f"UPDATE {table} SET review_claimed_at = '2000-01-01T00:00:00+00:00' "
                     "WHERE task_id = ?", (task["task_id"],))
    assert draw(user, paired=paired)["pair" if paired else "submission"] is None
    assert submit(user, task, subs, paired=paired).status_code == 403


def test_stats_only_count_assigned_pairs_that_this_reviewer_can_draw(restricted_pool):
    store = restricted_pool
    user = reviewer(store)
    eligible, _ = case(store)
    pending, _ = case(store, max_labels=3)
    self_authored, _ = case(store, author=user)
    elsewhere, _ = case(store, specialty="cardiology")
    held, _ = case(store)
    unseen, _ = case(store)
    for task in (eligible, pending, self_authored, elsewhere, held):
        assign(store, task, user)
    other = reviewer(store)
    assign(store, held, other)
    assert store.claim_task_for_review(held["task_id"], reviewer_id=other["id"])
    assert stats(user)["review_ready"] == 2  # named assignments can cross specialty
    assert draw(user)["pair"]["task_id"] == eligible["task_id"]
    assert stats(user)["review_ready"] == 2  # the held case remains resumable
    assert stats(reviewer(store))["review_ready"] == 0
    assert store.task_review_claim(unseen["task_id"])["holder"] is None


def test_qa_with_reviewer_tier_needs_assignment_but_preview_is_unbillable(restricted_pool):
    store = restricted_pool
    user = reviewer(store, role="qa_reviewer")
    task, subs = case(store)
    assert draw(user)["pair"] is None
    preview = draw(user, preview=True)
    assert preview["pair"]["task_id"] == task["task_id"]
    assert preview["preview"] is True and preview["session"] is None
    assert store.task_review_claim(task["task_id"])["holder"] is None
    assert submit(user, task, subs).status_code == 403
    assert store.open_work_session_row(user_id=user["id"], kind="review") is None


def test_operator_single_preview_never_creates_a_claim(restricted_pool):
    store = restricted_pool
    user = reviewer(store, role="admin", tier=None)
    task, subs = case(store, labels=1)
    preview = draw(user, paired=False)
    assert preview["submission"]["task_id"] == task["task_id"]
    assert preview["preview"] is True and preview["session"] is None
    assert store.review_claim(subs[0]["submission_id"])["holder"] is None
    assert submit(user, task, subs, paired=False).status_code == 409


def test_existing_session_can_resume_heartbeat_and_close_without_new_assignment(restricted_pool):
    store = restricted_pool
    user = reviewer(store)
    task, _ = case(store)
    assignment = assign(store, task, user)
    session = draw(user)["session"]
    assert session is not None
    store.set_assignment_status(assignment["assignment_id"], "revoked")
    with store._conn() as conn:
        conn.execute("UPDATE tasks SET review_claimed_at = '2000-01-01T00:00:00+00:00' "
                     "WHERE task_id = ?", (task["task_id"],))
    headers = A.headers_for(user)
    reopened = client.post("/api/asclepius/sessions", json={"kind": "review"}, headers=headers)
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["session_id"] == session["session_id"]
    resumed = client.post(f"/api/asclepius/sessions/{session['session_id']}/resume",
                          json={}, headers=headers)
    assert resumed.status_code == 200, resumed.text
    beat = client.post(f"/api/asclepius/sessions/{session['session_id']}/heartbeat",
                       json={"nonce": resumed.json()["nonce"], "seq": 1,
                             "active": True, "progress_key": task["task_id"]}, headers=headers)
    assert beat.status_code == 200, beat.text
    closed = client.post(f"/api/asclepius/sessions/{session['session_id']}/close",
                         json={"reason": "closed"}, headers=headers)
    assert closed.status_code == 200, closed.text
    assert client.post("/api/asclepius/sessions", json={"kind": "review"},
                       headers=headers).status_code == 403


def test_stale_session_cannot_bypass_assignment_gate(restricted_pool):
    store = restricted_pool
    user = reviewer(store)
    task, _ = case(store)
    assignment = assign(store, task, user)
    session = draw(user)["session"]
    store.set_assignment_status(assignment["assignment_id"], "revoked")
    with store._conn() as conn:
        conn.execute("UPDATE tasks SET review_claimed_at = '2000-01-01T00:00:00+00:00' "
                     "WHERE task_id = ?", (task["task_id"],))
        conn.execute("UPDATE work_sessions SET started_at = '2000-01-01T00:00:00+00:00', "
                     "last_beat_at = NULL WHERE session_id = ?", (session["session_id"],))
    response = client.post("/api/asclepius/sessions", json={"kind": "review"},
                           headers=A.headers_for(user))
    assert response.status_code == 403
    assert store.get_work_session(session["session_id"])["ended_at"] is not None
    assert store.open_work_session_row(user_id=user["id"], kind="review") is None


def test_claim_drawn_before_default_closed_pool_remains_finishable(restricted_pool, monkeypatch):
    store = restricted_pool
    user = reviewer(store)
    task, subs = case(store)
    monkeypatch.setenv("ASCLEPIUS_OPEN_CASE_POOL_ENABLED", "1")
    assert draw(user)["pair"]["task_id"] == task["task_id"]
    monkeypatch.delenv("ASCLEPIUS_OPEN_CASE_POOL_ENABLED")
    assert draw(user)["pair"]["task_id"] == task["task_id"]
    response = submit(user, task, subs)
    assert response.status_code == 200, response.text
    case(store)
    assert draw(user)["pair"] is None


@pytest.mark.parametrize("paired", [False, True])
@pytest.mark.parametrize("lease_minutes,claim_age_minutes,allowed", [(1, 2, False), (60, 46, True)])
def test_new_session_obeys_configured_review_claim_lease(
    restricted_pool, monkeypatch, paired, lease_minutes, claim_age_minutes, allowed,
):
    store = restricted_pool
    monkeypatch.setenv("ASCLEPIUS_REVIEW_LEASE_MIN", str(lease_minutes))
    user = reviewer(store)
    task, _ = case(store, labels=2 if paired else 1)
    assignment = assign(store, task, user)
    drawn = draw(user, paired=paired)
    headers = A.headers_for(user)
    if paired:
        session_id = drawn["session"]["session_id"]
        assert client.post(f"/api/asclepius/sessions/{session_id}/close",
                           json={"reason": "closed"}, headers=headers).status_code == 200
    store.set_assignment_status(assignment["assignment_id"], "revoked")
    claimed_at = (datetime.now(timezone.utc) - timedelta(minutes=claim_age_minutes)).isoformat()
    with store._conn() as conn:
        table = "tasks" if paired else "submissions"
        conn.execute(f"UPDATE {table} SET review_claimed_at = ? WHERE task_id = ?",
                     (claimed_at, task["task_id"]))
    next_work = store.next_review_pair_for if paired else store.next_review_for
    assert (next_work(user["id"], lease_minutes=review.review_lease_minutes()) is not None) is allowed
    if not allowed:
        assert draw(user, paired=paired)["pair" if paired else "submission"] is None
    response = client.post("/api/asclepius/sessions", json={"kind": "review"}, headers=headers)
    assert response.status_code == (200 if allowed else 403), response.text


@pytest.mark.parametrize("paired", [False, True])
def test_named_cross_specialty_assignment_is_drawable_counted_and_billable(
    restricted_pool, monkeypatch, paired,
):
    store = restricted_pool
    user = reviewer(store)
    task, _ = case(store, specialty="cardiology", labels=2 if paired else 1)
    key = "pair" if paired else "submission"
    count_key = "review_ready" if paired else "unreviewed"
    assert draw(user, paired=paired)[key] is None
    assert stats(user)[count_key] == 0
    assignment = assign(store, task, user)
    assert stats(user)[count_key] == 1
    # Operators and the legacy pool keep their existing specialty preference.
    next_work = store.next_review_pair_for if paired else store.next_review_for
    assert next_work(user["id"], specialty=user["specialty"], assignment_only=False) is None
    headers = A.headers_for(user)
    session = client.post("/api/asclepius/sessions", json={"kind": "review"}, headers=headers)
    assert session.status_code == 200, session.text
    assert draw(user, paired=paired)[key]["task_id"] == task["task_id"]
    store.set_assignment_status(assignment["assignment_id"], "revoked")
    assert draw(user, paired=paired)[key]["task_id"] == task["task_id"]
    assert stats(user)["review_ready" if paired else "in_review"] == 1
