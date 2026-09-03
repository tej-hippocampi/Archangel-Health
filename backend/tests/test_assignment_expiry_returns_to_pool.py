"""An expiring assignment must put the case back, not make it disappear.

``expire_stale_assignments`` had no caller until the hourly maintenance sweep
shipped, which is why this was never seen in production. Its docstring said
"return timed-out exclusive assignments to the pool", and it did half of that:
it expired the assignment row. But routing a case to named doctors also flips it
to ``distribution='assigned_only'``, and that gate serves such a case to nobody
except a LIVE assignee. So the moment the last live assignment expired, the case
left the assignee's queue and was still hidden from everyone else: not returned
to any pool, just gone from the product, silently, on a timer.

The tests below hold the fix to being exactly that and nothing more. The
narrowness is the point: an expiry is a clock running out on work nobody did, a
revoke is an admin deciding somebody should not have it, and only the first one
means "put it back".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402

PAST = "2020-01-01T00:00:00Z"


@pytest.fixture()
def store():
    A.fresh_store()
    from asclepius.store import get_store

    return get_store()


def _task(store, **kw):
    kw.setdefault("prompt", "q?")
    kw.setdefault("specialty", "cardiology")
    return store.insert_task(**kw)


def _visible(store, user_id, *, specialty="cardiology"):
    return [t["task_id"] for t in store.eligible_tasks_for_evaluator(
        evaluator_id=user_id, specialty=specialty)]


def test_an_expired_exclusive_hold_returns_a_routed_case_to_the_queue(store):
    """THE test. Before the fix both lists below were empty and the case had
    left the product."""
    doc = A.make_user(store, specialty="cardiology")
    anyone_else = A.make_user(store, specialty="cardiology")
    routed = _task(store, distribution="assigned_only", prompt="routed, then lapsed")
    store.upsert_assignment(task_id=routed["task_id"], user_id=doc["id"], role="label",
                            assigned_by="u-admin", exclusive=True, expires_at=PAST)
    assert _visible(store, doc["id"]) == [routed["task_id"]]
    assert _visible(store, anyone_else["id"]) == []

    assert store.expire_stale_assignments() == 1

    assert store.get_task(routed["task_id"])["distribution"] == "open"
    assert routed["task_id"] in _visible(store, anyone_else["id"])
    assert routed["task_id"] in _visible(store, doc["id"])


def test_a_case_still_held_by_a_second_doctor_stays_theirs(store):
    """Two labelers, one lapsed hold. The case is not unrouted while somebody
    still legitimately holds it."""
    lapsed = A.make_user(store, specialty="cardiology")
    holder = A.make_user(store, specialty="cardiology")
    stranger = A.make_user(store, specialty="cardiology")
    routed = _task(store, distribution="assigned_only")
    store.upsert_assignment(task_id=routed["task_id"], user_id=lapsed["id"], role="label",
                            assigned_by="u-admin", exclusive=True, expires_at=PAST)
    store.upsert_assignment(task_id=routed["task_id"], user_id=holder["id"], role="label",
                            assigned_by="u-admin")

    assert store.expire_stale_assignments() == 1

    assert store.get_task(routed["task_id"])["distribution"] == "assigned_only"
    assert routed["task_id"] in _visible(store, holder["id"])
    assert routed["task_id"] not in _visible(store, stranger["id"])


def test_a_lapsed_review_hold_does_not_open_a_case_for_labeling(store):
    """The release matches ``_PRD_ASSIGN_MINE``, which is label-only. A reviewer's
    clock running out says nothing about who may label the case."""
    reviewer = A.make_user(store, specialty="cardiology")
    stranger = A.make_user(store, specialty="cardiology")
    routed = _task(store, distribution="assigned_only")
    store.upsert_assignment(task_id=routed["task_id"], user_id=reviewer["id"],
                            role="review", assigned_by="u-admin",
                            exclusive=True, expires_at=PAST)

    assert store.expire_stale_assignments() == 1

    assert store.get_task(routed["task_id"])["distribution"] == "assigned_only"
    assert _visible(store, stranger["id"]) == []


def test_an_already_open_case_is_left_alone(store):
    """An assignment on an open case is a priority, not a permission, so its
    expiry costs the assignee their place at the head of the queue and nothing
    else. Nothing needs releasing and nothing is written."""
    doc = A.make_user(store, specialty="cardiology")
    other = A.make_user(store, specialty="cardiology")
    open_case = _task(store, prompt="ordinary")
    store.upsert_assignment(task_id=open_case["task_id"], user_id=doc["id"], role="label",
                            assigned_by="u-admin", exclusive=True, expires_at=PAST)

    assert store.expire_stale_assignments() == 1

    assert store.get_task(open_case["task_id"])["distribution"] == "open"
    for who in (doc, other):
        assert open_case["task_id"] in _visible(store, who["id"])


def test_a_revoked_assignment_still_hides_the_case(store):
    """The line between the two. Revoking is an admin un-routing work on
    purpose; if this fix had leaked into that path, un-routing would have become
    a way to publish a case to the whole fleet."""
    doc = A.make_user(store, specialty="cardiology")
    stranger = A.make_user(store, specialty="cardiology")
    routed = _task(store, distribution="assigned_only")
    asg = store.upsert_assignment(task_id=routed["task_id"], user_id=doc["id"],
                                  role="label", assigned_by="u-admin")

    store.set_assignment_status(asg["assignment_id"], "revoked")
    store.expire_stale_assignments()

    assert store.get_task(routed["task_id"])["distribution"] == "assigned_only"
    assert _visible(store, stranger["id"]) == []


def test_an_assignment_with_no_expiry_is_never_swept(store):
    """Only exclusivity carries a clock. A relay walk's assignments have no
    ``expires_at``, and the sweep must not touch the seal on one."""
    doc = A.make_user(store, specialty="cardiology")
    routed = _task(store, distribution="assigned_only")
    store.upsert_assignment(task_id=routed["task_id"], user_id=doc["id"],
                            role="label", assigned_by="u-admin")

    assert store.expire_stale_assignments() == 0
    assert store.get_task(routed["task_id"])["distribution"] == "assigned_only"


def test_the_release_is_recorded_rather_than_silent(store):
    """"Why is this case back in the general queue" is a question an admin asks
    about one specific case, and a sweep that changes visibility with no trace is
    indistinguishable from a bug."""
    doc = A.make_user(store, specialty="cardiology")
    routed = _task(store, distribution="assigned_only")
    store.upsert_assignment(task_id=routed["task_id"], user_id=doc["id"], role="label",
                            assigned_by="u-admin", exclusive=True, expires_at=PAST)

    store.expire_stale_assignments()

    events = [e for e in store.list_events(entity_type="task")
              if e["event_type"] == "assignment_expired_returned_to_pool"]
    assert [e["entity_id"] for e in events] == [routed["task_id"]]


def test_the_sweep_is_idempotent(store):
    """It runs hourly forever. The second pass must find nothing to do."""
    doc = A.make_user(store, specialty="cardiology")
    routed = _task(store, distribution="assigned_only")
    store.upsert_assignment(task_id=routed["task_id"], user_id=doc["id"], role="label",
                            assigned_by="u-admin", exclusive=True, expires_at=PAST)

    assert store.expire_stale_assignments() == 1
    assert store.expire_stale_assignments() == 0
    assert store.get_task(routed["task_id"])["distribution"] == "open"
