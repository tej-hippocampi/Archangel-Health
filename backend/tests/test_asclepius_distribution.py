"""Who a task may be served to — the column that makes the longitudinal merge safe.

**The defect this prevents, stated plainly.** A promoted trajectory point is an
ordinary task row carrying two extra columns. The labeler queue has no notion of
"generated but not released", so on the day the longitudinal branch merges, every
approved physician's next draw could hand them decision point 0 of a chart nobody
chose to send them. Nothing errors; the queue is simply wrong about what exists.

``tasks.distribution`` is the fix, and it is deliberately not clever:

  * ``'open'``          — today's behaviour, and every existing row's backfill.
  * ``'assigned_only'`` — reachable ONLY through an assignment row.

The interesting design point is that this is ORTHOGONAL to assignment, not a
restatement of it. Main's priority order already sorts an assigned case to the top
of its assignee's queue while leaving it visible to everyone else — "an assignment
is a priority, not a permission", in the store's own words. ``distribution`` is the
switch that turns the same assignment into a permission. Both use one definition of
"assigned to me" (``_PRD_ASSIGN_MINE``), asked two different questions, so the sort
and the filter cannot drift apart.

The resting state of a promoted walk is therefore ``assigned_only`` with zero
assignments: invisible to every doctor, listed for admin as unrouted. That is
correct, not a bug, and the tests below assert it as such.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated():
    A.fresh_store()
    yield


def _store():
    from asclepius.store import get_store
    return get_store()


def _task(store, **kw):
    kw.setdefault("prompt", "q?")
    kw.setdefault("specialty", "cardiology")
    return store.insert_task(**kw)


def _visible(store, user_id, *, specialty="cardiology"):
    return [t["task_id"] for t in store.eligible_tasks_for_evaluator(
        evaluator_id=user_id, specialty=specialty)]


# ═══════════════════════════════════════════════════════════════════════════════
# The default — every existing creation path is untouched
# ═══════════════════════════════════════════════════════════════════════════════
def test_every_ordinary_creation_path_still_produces_an_open_task():
    """The migration's backfill restates what is already true of every row rather
    than asserting anything new, so nothing that exists today changes behaviour."""
    store = _store()
    for kw in ({}, {"source": "lab_supplied"}, {"modality": "multimodal"},
               {"trajectory_id": None, "sequence_index": None}):
        t = _task(store, **kw)
        assert t["distribution"] == "open"


def test_an_unknown_distribution_is_refused_at_the_write():
    """It would otherwise fail CLOSED and silently: the predicate compares against
    the exact string 'open', so a typo hides the task from every queue forever
    with nothing raised anywhere."""
    store = _store()
    with pytest.raises(ValueError, match="distribution must be one of"):
        _task(store, distribution="assigned-only")     # hyphen, not underscore
    with pytest.raises(ValueError):
        _task(store, distribution="private")


# ═══════════════════════════════════════════════════════════════════════════════
# The gate
# ═══════════════════════════════════════════════════════════════════════════════
def test_assigned_only_with_no_assignment_is_invisible_to_everyone():
    """The resting state of a promoted, unrouted walk."""
    store = _store()
    a, b = A.make_user(store, specialty="cardiology"), A.make_user(store, specialty="cardiology")
    held = _task(store, distribution="assigned_only", prompt="withheld")
    plain = _task(store, prompt="ordinary")

    for who in (a, b):
        seen = _visible(store, who["id"])
        assert held["task_id"] not in seen
        assert plain["task_id"] in seen


def test_an_assignment_is_what_makes_it_visible_and_only_to_that_doctor():
    store = _store()
    a, b = A.make_user(store, specialty="cardiology"), A.make_user(store, specialty="cardiology")
    held = _task(store, distribution="assigned_only", prompt="routed to A")
    store.upsert_assignment(task_id=held["task_id"], user_id=a["id"],
                            role="label", assigned_by="u-admin")

    assert held["task_id"] in _visible(store, a["id"])
    assert held["task_id"] not in _visible(store, b["id"])


def test_the_dashboard_count_and_the_draw_agree():
    """A doctor told "1 case available" who is then handed nothing is the product
    knowing something and not saying it. Both read one predicate, so they cannot
    disagree — asserted rather than assumed because they are different call sites."""
    store = _store()
    doc = A.make_user(store, specialty="cardiology")
    _task(store, distribution="assigned_only", prompt="withheld")

    assert _visible(store, doc["id"]) == []
    assert store.next_task_for_evaluator(
        evaluator_id=doc["id"], specialty="cardiology") is None


def test_a_revoked_assignment_hides_the_task_again():
    """Visibility follows the assignment's live status, so un-routing works without
    a second bookkeeping step that could fall out of sync."""
    store = _store()
    doc = A.make_user(store, specialty="cardiology")
    held = _task(store, distribution="assigned_only")
    asg = store.upsert_assignment(task_id=held["task_id"], user_id=doc["id"],
                                  role="label", assigned_by="u-admin")
    assert held["task_id"] in _visible(store, doc["id"])

    store.set_assignment_status(asg["assignment_id"], "revoked")
    assert held["task_id"] not in _visible(store, doc["id"])


def test_distribution_and_assignment_are_orthogonal():
    """The design in one test.

    An OPEN task that is assigned to A is visible to B as well — it just outranks
    other work in A's queue. Flipping the same task to assigned_only is what turns
    that priority into a permission. Two switches, four states, and the pair that
    matters is (open + assigned): a case can be routed without being hidden."""
    store = _store()
    a, b = A.make_user(store, specialty="cardiology"), A.make_user(store, specialty="cardiology")
    t = _task(store, prompt="assigned but still open")
    store.upsert_assignment(task_id=t["task_id"], user_id=a["id"],
                            role="label", assigned_by="u-admin")

    assert _visible(store, a["id"])[0] == t["task_id"], "assigned sorts first for A"
    assert t["task_id"] in _visible(store, b["id"]), (
        "an assignment is a priority, not a permission — B still sees it")


# ═══════════════════════════════════════════════════════════════════════════════
# §5 — the merge-order invariant, as an executable definition of done
# ═══════════════════════════════════════════════════════════════════════════════
def test_the_doctor_queue_is_byte_identical_with_and_without_a_promoted_walk():
    """The PRD's definition of done for the merge, run rather than promised.

    Snapshot a physician's whole eligible queue, promote a 5-point trajectory
    beside it, snapshot again: the two must be the SAME LIST. If they differ, the
    longitudinal merge changed what doctors see on deploy, which is the exact
    outcome this column exists to prevent."""
    store = _store()
    doc = A.make_user(store, specialty="cardiology")
    for i in range(3):
        _task(store, prompt=f"ordinary {i}")

    before = _visible(store, doc["id"])
    assert before, "the fixture must have real work in it or this proves nothing"

    for i in range(5):
        _task(store, prompt=f"walk point {i}", trajectory_id="traj-merge",
              sequence_index=i, distribution="assigned_only")

    assert _visible(store, doc["id"]) == before, (
        "promoting a trajectory changed the doctor's queue — this is the merge "
        "landmine, and it is what §1 exists to disarm")


def test_a_walk_promoted_without_the_column_would_have_flooded_the_queue():
    """The counterfactual, executed. Same fixture, points written 'open' instead —
    the physician is now offered decision point 0 of a chart nobody sent them.

    This is here so the previous test cannot pass vacuously: it proves the queue
    snapshot is sensitive to exactly the thing being prevented."""
    store = _store()
    doc = A.make_user(store, specialty="cardiology")
    before = _visible(store, doc["id"])
    for i in range(5):
        _task(store, prompt=f"walk point {i}", trajectory_id="traj-flood",
              sequence_index=i)                     # distribution defaults to 'open'
    after = _visible(store, doc["id"])
    assert after != before and len(after) == len(before) + 1, (
        "point 0 leaks into the open queue without the column — the sequence gate "
        "holds back 1..4, which is precisely why this looked harmless")
