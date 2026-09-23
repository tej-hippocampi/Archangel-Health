"""Current clinical-data approval applies to review draws, claims and writes."""
import time

import pytest

from tests import _asclepius as A
from tests.test_assigned_review_access import assign, case, draw, reviewer, submit
from routers import asclepius_review


@pytest.fixture
def review_store(monkeypatch):
    monkeypatch.delenv("ASCLEPIUS_OPEN_CASE_POOL_ENABLED", raising=False)
    monkeypatch.setenv("ASCLEPIUS_DOUBLE_LABEL_HALT", "1")
    monkeypatch.setenv("ASCLEPIUS_REVIEW_RATE", "1")
    monkeypatch.setitem(asclepius_review._SWEEP_STATE, "last", time.monotonic())
    return A.fresh_store()


def _real_case(store, *, paired):
    task, submissions = case(store, labels=2 if paired else 1)
    store.update_task_case(task["task_id"], {
        "case_source": "real_deid", "specialty": "nephrology",
        "notes": [{"note_type": "Synthetic fixture", "author_role": "clinician",
                   "text": "Retained clinical chart fixture."}],
    })
    return store.get_task(task["task_id"]), submissions


@pytest.mark.parametrize("paired", [False, True])
@pytest.mark.parametrize("already_claimed", [False, True])
def test_revoked_approval_blocks_real_review_draw_claim_and_submit(review_store, paired, already_claimed):
    store = review_store
    user = reviewer(store)
    store.set_real_data_approved(user["id"], True)
    task, submissions = _real_case(store, paired=paired)
    assign(store, task, user)
    key = "pair" if paired else "submission"
    if already_claimed:
        assert draw(user, paired=paired)[key]["task_id"] == task["task_id"]

    # Keep the session and assignment; a permission change must apply to the
    # next request even when previously authorized work is still in flight.
    store.set_real_data_approved(user["id"], False)
    assert draw(user, paired=paired)[key] is None
    assert submit(user, task, submissions, paired=paired).status_code == 403
    if paired:
        assert not store.claim_task_for_review(task["task_id"], reviewer_id=user["id"])
    else:
        assert not store.claim_submission_for_review(submissions[0]["submission_id"], reviewer_id=user["id"])
    assert store.reviews_for_task(task["task_id"]) == []
    assert len(store.submissions_for_task(task["task_id"])) == len(submissions)
    assert store.get_task(task["task_id"])["case"] == task["case"]


@pytest.mark.parametrize("paired", [False, True])
def test_approved_reviewer_can_draw_and_submit_real_work(review_store, paired):
    store = review_store
    user = reviewer(store)
    store.set_real_data_approved(user["id"], True)
    task, submissions = _real_case(store, paired=paired)
    assign(store, task, user)
    assert draw(user, paired=paired)["pair" if paired else "submission"]["task_id"] == task["task_id"]
    response = submit(user, task, submissions, paired=paired)
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("paired", [False, True])
def test_unapproved_reviewer_can_still_draw_and_submit_synthetic_work(review_store, paired):
    store = review_store
    user = reviewer(store)
    store.set_real_data_approved(user["id"], False)
    task, submissions = case(store, labels=2 if paired else 1)
    assign(store, task, user)
    assert draw(user, paired=paired)["pair" if paired else "submission"]["task_id"] == task["task_id"]
    response = submit(user, task, submissions, paired=paired)
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("paired", [False, True])
@pytest.mark.parametrize("role", ["admin", "qa_reviewer"])
def test_staff_real_data_access_keeps_its_existing_exemption(review_store, paired, role):
    store = review_store
    user = reviewer(store, role=role)
    store.set_real_data_approved(user["id"], False)
    task, submissions = _real_case(store, paired=paired)
    assign(store, task, user)
    assert draw(user, paired=paired)["pair" if paired else "submission"]["task_id"] == task["task_id"]
    response = submit(user, task, submissions, paired=paired)
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("paired", [False, True])
def test_real_data_filter_precedes_queue_limits_and_also_applies_to_open_pool(review_store, monkeypatch, paired):
    store = review_store
    user = reviewer(store)
    real, _ = _real_case(store, paired=paired)
    synthetic, _ = case(store, labels=2 if paired else 1)
    for task in (real, synthetic):
        assign(store, task, user)
    with store._conn() as conn:
        conn.execute("UPDATE tasks SET created_at='2000-01-01T00:00:00' WHERE task_id=?", (real["task_id"],))
        conn.execute("UPDATE submissions SET created_at='2000-01-01T00:00:00' WHERE task_id=?", (real["task_id"],))
    store.set_real_data_approved(user["id"], False)
    next_work = store.next_review_pair_for if paired else store.next_review_for
    assert next_work(user["id"], scan_limit=1)["task_id"] == synthetic["task_id"]
    monkeypatch.setenv("ASCLEPIUS_OPEN_CASE_POOL_ENABLED", "1")
    assert draw(user, paired=paired)["pair" if paired else "submission"]["task_id"] == synthetic["task_id"]
