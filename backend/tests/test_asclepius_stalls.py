"""Work that stopped moving — the sweep, the chain view, and reassignment (§8.7).

A relay is a queue of people waiting on each other, so one physician who gets busy
stops four. A solo walk is worse: at ``max_labels=1``, once somebody submits point
0 nobody else can ever satisfy the sequence gate for the rest of the chart, so an
abandoned solo walk is unrecoverable by anyone — and until this existed it was
unrecoverable AND invisible, dead stock nobody could see. That is why the chain
view is built for both modes and not only for relay (PRD §9.3).

Three things here are easy to get wrong in ways that look fine.

**Only the point the chart is actually waiting on is "waiting".** A 13-point walk
sitting at point 2 has one problem, not eleven. Reporting the later points as
stalled too would make the view unreadable exactly when it matters.

**The clock starts when the point became AVAILABLE, not when it was assigned.** On
a relay every point is assigned at send; if the clock ran from there, a 13-point
relay would report thirteen simultaneous stalls the day after it went out, and
twelve of those physicians would be nudged about work they cannot do.

**The nudge ships off.** This is the only place the product messages a physician on
a timer with nobody deciding to, and the sweep must therefore be able to run,
report, and mark nothing — so that turning it on later does not fire a backlog of
chases at everyone who was stalled during the observation window.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import route_notify as RN, trajectory as TJ  # noqa: E402

client = TestClient(A.app)


@pytest.fixture(autouse=True)
def _isolated():
    A.fresh_store()
    yield


def _store():
    from asclepius.store import get_store
    return get_store()


def _admin(store):
    return A.headers_for(A.make_user(store, role="admin"))


def _doc(store, *, specialty="cardiology"):
    u = A.make_user(store, specialty=specialty, tier="labeler")
    store.set_real_data_approved(u["id"], True)
    return store.get_user_by_id(u["id"])


def _iso(dt):
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ago(hours):
    return _iso(datetime.now(timezone.utc) - timedelta(hours=hours))


def _walk(store, n=4, *, tid="traj-s"):
    return [store.insert_task(prompt=f"p{i}", specialty="cardiology",
                              trajectory_id=tid, sequence_index=i,
                              distribution="assigned_only") for i in range(n)]


def _assign(store, task_id, user_id, *, when=None):
    row = store.upsert_assignment(task_id=task_id, user_id=user_id, role="label",
                                  assigned_by="u-admin")
    if when:
        with store._conn() as conn:  # noqa: SLF001
            conn.execute("UPDATE assignments SET assigned_at = ? WHERE assignment_id = ?",
                         (when, row["assignment_id"]))
    return row


def _submit(store, task_id, evaluator_id, *, when=None):
    sub = store.insert_submission(
        submission_id=f"s-{evaluator_id}-{task_id[-6:]}", task_id=task_id,
        evaluator_id=evaluator_id, verdict="A_better", chosen_id="a", rejected_id="b",
        confidence="high", time_spent_sec=60, payload={}, annotator={}, dedupe_hash=None)
    if when:
        with store._conn() as conn:  # noqa: SLF001
            conn.execute("UPDATE submissions SET created_at = ? WHERE submission_id = ?",
                         (when, sub["submission_id"]))
    return sub


# ═══════════════════════════════════════════════════════════════════════════════
# Detection
# ═══════════════════════════════════════════════════════════════════════════════
def test_a_point_nobody_can_act_on_yet_is_not_stalled():
    """The whole reason this is not "assigned and old". Points 1–3 are assigned
    and days old, and nothing is being asked of their holders."""
    store = _store()
    a, b = _doc(store), _doc(store)
    pts = _walk(store, 4)
    for i, pt in enumerate(pts):
        _assign(store, pt["task_id"], (a if i % 2 == 0 else b)["id"], when=_ago(72))
    store.set_walk_mode([p["task_id"] for p in pts], "relay")

    stalled = store.stalled_trajectory_points(older_than_hours=24)
    assert [r["sequence_index"] for r in stalled] == [0], (
        "only the point whose predecessors are done can be stalled")


def test_the_clock_runs_from_availability_not_from_assignment():
    """Every relay point is assigned at send. A clock started there would report a
    13-point relay as thirteen stalls the day after it went out."""
    store = _store()
    a, b = _doc(store), _doc(store)
    pts = _walk(store, 3)
    for i, pt in enumerate(pts):
        _assign(store, pt["task_id"], (a if i % 2 == 0 else b)["id"], when=_ago(72))
    store.set_walk_mode([p["task_id"] for p in pts], "relay")
    # Point 0 answered an HOUR ago, so point 1 has only been actionable an hour —
    # even though its assignment is three days old.
    _submit(store, pts[0]["task_id"], a["id"], when=_ago(1))

    assert store.stalled_trajectory_points(older_than_hours=24) == []
    fresh = store.stalled_trajectory_points(older_than_hours=0)
    assert [r["sequence_index"] for r in fresh] == [1]
    assert 0 <= (fresh[0]["waiting_hours"] or 0) <= 2, "hours since it UNLOCKED"


def test_a_solo_walk_stalls_too():
    """§9.3 — at max_labels=1 an abandoned solo walk is unrecoverable by anyone
    else, so it is the more urgent of the two, not the exempt one."""
    store = _store()
    a = _doc(store)
    pts = _walk(store, 3, tid="traj-solo")
    for pt in pts:
        _assign(store, pt["task_id"], a["id"], when=_ago(48))
    _submit(store, pts[0]["task_id"], a["id"], when=_ago(40))

    stalled = store.stalled_trajectory_points(older_than_hours=24)
    assert [(r["sequence_index"], r["walk_mode"]) for r in stalled] == [(1, "solo")]


def test_a_retired_point_neither_stalls_nor_blocks_the_next_one():
    store = _store()
    a = _doc(store)
    pts = _walk(store, 3)
    for pt in pts:
        _assign(store, pt["task_id"], a["id"], when=_ago(48))
    store.mark_task_status(pts[0]["task_id"], "void")

    stalled = store.stalled_trajectory_points(older_than_hours=24)
    assert [r["sequence_index"] for r in stalled] == [1], (
        "point 0 is out of the walk; point 1 is what the chart is waiting on")


# ═══════════════════════════════════════════════════════════════════════════════
# The nudge — staged rollout
# ═══════════════════════════════════════════════════════════════════════════════
def test_the_sweep_reports_without_sending_while_the_flag_is_off(monkeypatch):
    monkeypatch.delenv("ASCLEPIUS_RELAY_NUDGE_ENABLED", raising=False)
    store = _store()
    a = _doc(store)
    pts = _walk(store, 2)
    for pt in pts:
        _assign(store, pt["task_id"], a["id"], when=_ago(48))

    out = RN.sweep_stalled_points(store)
    assert out["enabled"] is False
    assert out["stalled"] == 1 and len(out["would_notify"]) == 1
    assert out["sent"] == 0, "log-only means log-only"


def test_the_observation_window_does_not_consume_anybodys_one_nudge(monkeypatch):
    """The trap in a staged rollout: if the dry run marked people as nudged, then
    turning the flag on would send nothing to exactly the physicians who had been
    stalled the longest."""
    monkeypatch.delenv("ASCLEPIUS_RELAY_NUDGE_ENABLED", raising=False)
    store = _store()
    a = _doc(store)
    pts = _walk(store, 2)
    _assign(store, pts[0]["task_id"], a["id"], when=_ago(48))

    for _ in range(3):
        RN.sweep_stalled_points(store)
    assert len(store.stalled_trajectory_points(older_than_hours=24)) == 1, (
        "still eligible — the dry runs marked nothing")

    monkeypatch.setenv("ASCLEPIUS_RELAY_NUDGE_ENABLED", "1")
    out = RN.sweep_stalled_points(store)
    assert out["sent"] == 1


def test_the_nudge_fires_once_and_never_again(monkeypatch):
    """Recurring chases to unpaid specialists is how a channel gets muted, and a
    muted physician is unreachable for the thing that matters next time."""
    monkeypatch.setenv("ASCLEPIUS_RELAY_NUDGE_ENABLED", "1")
    store = _store()
    a = _doc(store)
    pts = _walk(store, 2)
    _assign(store, pts[0]["task_id"], a["id"], when=_ago(48))

    assert RN.sweep_stalled_points(store)["sent"] == 1
    assert RN.sweep_stalled_points(store)["sent"] == 0
    assert RN.sweep_stalled_points(store)["stalled"] == 0


def test_the_nudge_reads_as_a_colleague_not_a_ticket():
    body = RN.compose_stall_nudge(doctor={"name": "R Shafipour"}, position=3,
                                  n_points=13, specialty="hepatology",
                                  waiting_hours=31, mode="relay")
    assert "Still with you, Dr. Shafipour" in body
    assert "only reminder you'll get" in body
    assert "hand it back" in body, "there must be a way out that is not silence"
    for pressure in ("URGENT", "immediately", "overdue", "failing", "required"):
        assert pressure.lower() not in body.lower(), pressure


def test_a_failing_nudge_does_not_stop_the_sweep(monkeypatch):
    monkeypatch.setenv("ASCLEPIUS_RELAY_NUDGE_ENABLED", "1")
    store = _store()
    a, b = _doc(store), _doc(store)
    for i, who in enumerate((a, b)):
        pts = _walk(store, 1, tid=f"traj-{i}")
        _assign(store, pts[0]["task_id"], who["id"], when=_ago(48))

    calls = {"n": 0}

    async def _flaky(*args, **kw):
        # async, because the real _dm_one is — a sync fake "succeeds" by returning
        # True, which _run_coro then chokes on, and the test would be measuring
        # its own stub rather than the sweep.
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("community hiccup")
        return True
    monkeypatch.setattr(RN, "_dm_one", _flaky)

    out = RN.sweep_stalled_points(store)
    assert out["stalled"] == 2 and out["sent"] == 1 and out["errors"]


# ═══════════════════════════════════════════════════════════════════════════════
# The chain view
# ═══════════════════════════════════════════════════════════════════════════════
def test_the_chain_names_one_waiting_point_and_calls_the_rest_later():
    """A 13-point walk sitting at point 2 has one problem, not eleven."""
    store = _store()
    a = _doc(store)
    pts = _walk(store, 5)
    for pt in pts:
        _assign(store, pt["task_id"], a["id"], when=_ago(48))
    _submit(store, pts[0]["task_id"], a["id"], when=_ago(40))
    _submit(store, pts[1]["task_id"], a["id"], when=_ago(36))

    chain = client.get("/api/asclepius/admin/batches/relay/traj-s",
                       headers=_admin(store)).json()
    assert [p["state"] for p in chain["points"]] == [
        "done", "done", "waiting", "later", "later"]
    assert chain["n_done"] == 2
    assert chain["waiting_on"]["sequence_index"] == 2
    assert chain["stalled"] is True, "36h past the point it unlocked"


def test_the_chain_is_served_for_a_solo_walk_too():
    store = _store()
    a = _doc(store)
    pts = _walk(store, 3, tid="traj-solo")
    for pt in pts:
        _assign(store, pt["task_id"], a["id"])
    chain = client.get("/api/asclepius/admin/batches/relay/traj-solo",
                       headers=_admin(store)).json()
    assert chain["walk_mode"] == "solo" and chain["n_points"] == 3


def test_an_unknown_walk_is_a_404():
    store = _store()
    assert client.get("/api/asclepius/admin/batches/relay/nope",
                      headers=_admin(store)).status_code == 404


# ═══════════════════════════════════════════════════════════════════════════════
# Reassignment
# ═══════════════════════════════════════════════════════════════════════════════
def test_reassigning_revokes_the_old_holder_and_tells_the_new_one():
    store = _store()
    ah = _admin(store)
    a, b = _doc(store), _doc(store)
    pts = _walk(store, 3)
    _assign(store, pts[0]["task_id"], a["id"], when=_ago(48))

    r = client.post("/api/asclepius/admin/batches/relay/traj-s/reassign", headers=ah,
                    json={"task_id": pts[0]["task_id"], "user_id": b["id"]})
    assert r.status_code == 200, r.text
    live = [x for x in store.assignments_for_task(pts[0]["task_id"])
            if x["status"] in ("offered", "claimed")]
    assert [x["user_id"] for x in live] == [b["id"]]
    assert r.json()["notified"]["dms"] == 1
    assert r.json()["chain"]["points"][0]["assigned_to"] == [b["email"]]


def test_the_nudge_clock_resets_for_the_replacement(monkeypatch):
    """``nudged_at`` lives on the assignment, so a new row starts unnudged — the
    replacement gets their own reminder rather than inheriting a spent one."""
    monkeypatch.setenv("ASCLEPIUS_RELAY_NUDGE_ENABLED", "1")
    store = _store()
    ah = _admin(store)
    a, b = _doc(store), _doc(store)
    pts = _walk(store, 2)
    _assign(store, pts[0]["task_id"], a["id"], when=_ago(48))
    assert RN.sweep_stalled_points(store)["sent"] == 1
    assert RN.sweep_stalled_points(store)["sent"] == 0

    client.post("/api/asclepius/admin/batches/relay/traj-s/reassign", headers=ah,
                json={"task_id": pts[0]["task_id"], "user_id": b["id"]})
    with store._conn() as conn:  # noqa: SLF001
        conn.execute("UPDATE assignments SET assigned_at = ? WHERE user_id = ?",
                     (_ago(48), b["id"]))
    assert RN.sweep_stalled_points(store)["sent"] == 1, "B gets their own one nudge"


def test_an_answered_point_cannot_be_reassigned():
    """It would take finished work away from the physician who did it."""
    store = _store()
    ah = _admin(store)
    a, b = _doc(store), _doc(store)
    pts = _walk(store, 2)
    _assign(store, pts[0]["task_id"], a["id"])
    _submit(store, pts[0]["task_id"], a["id"])

    r = client.post("/api/asclepius/admin/batches/relay/traj-s/reassign", headers=ah,
                    json={"task_id": pts[0]["task_id"], "user_id": b["id"]})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "already_answered"


def test_a_point_from_another_walk_is_refused():
    store = _store()
    ah = _admin(store)
    b = _doc(store)
    _walk(store, 2)
    other = _walk(store, 1, tid="traj-other")
    r = client.post("/api/asclepius/admin/batches/relay/traj-s/reassign", headers=ah,
                    json={"task_id": other[0]["task_id"], "user_id": b["id"]})
    assert r.status_code == 404


def test_the_reassignment_reaches_the_export_provenance():
    """A relay walk with a substitution in the middle is a handoff chain a buyer
    should see: the physician at point 5 read point 4's commitment, and point 4
    was written by whoever took over."""
    from asclepius import packaging as PK

    store = _store()
    ah = _admin(store)
    a, b = _doc(store), _doc(store)
    pts = _walk(store, 3)
    _assign(store, pts[0]["task_id"], a["id"])
    client.post("/api/asclepius/admin/batches/relay/traj-s/reassign", headers=ah,
                json={"task_id": pts[0]["task_id"], "user_id": b["id"]})

    assert store.point_was_reassigned(pts[0]["task_id"]) is True
    assert store.point_was_reassigned(pts[1]["task_id"]) is False

    task = dict(store.get_task(pts[0]["task_id"]))
    task["_reassigned"] = store.point_was_reassigned(task["task_id"])
    assert PK.trajectory_block(task, {})["reassigned"] is True
    # A point that never changed hands says nothing rather than "reassigned: false"
    # — absence must not read as a positive claim from a lookup that never ran.
    assert "reassigned" not in PK.trajectory_block(store.get_task(pts[1]["task_id"]), {})
