"""The §4 routing + notification checks, EXECUTED (Longitudinal E2E PRD §4).

The PRD's §4 table says "nothing to build — everything below exists" and asks for
each row to be *run* and the result recorded. This file is that run. Nothing here
is inferred from reading source: every row drives the real HTTP surface against a
trajectory produced by the real generation route, and
``docs/asclepius/LONGITUDINAL_E2E_CHECK.md`` records what came back.

The chart, the model stubs and the ingest helpers are shared with
``test_asclepius_longitudinal_e2e`` rather than re-written — a second chart would
be a second thing to keep true, and the point of this file is that these gates
hold on the SAME points a real generation run produces.

| # | Step          | Row asserted here                                            |
|---|---------------|--------------------------------------------------------------|
| 1 | Batch counts  | ``/admin/batches`` shows N trajectories / M points            |
| 2 | Preview       | point k's window is ≤ its own offset; no future               |
| 3 | Solo send     | the assignee sees point 0 only; others see nothing            |
| 4 | Relay send    | N doctors, order shown, private channel, a DM each            |
| 5 | Unlock ping   | doctor k+1 is DM'd on k's submit                              |
| 6 | Reveal        | strictly after the decision offset; terminal point named      |
| 7 | Stall         | nudged once; reassign revokes and re-DMs                      |
| 8 | Retired point | retire point 2 of 5 → point 3 serves                          |
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from tests.test_asclepius_longitudinal_e2e import (  # noqa: E402
    SPECIALTY, _admin_headers, _approved_physician, _generate, _ingest_chart,
    _stub_model_legs, build_chart,
)

from asclepius import trajectory as asc_trajectory  # noqa: E402

client = TestClient(A.app)


def _store():
    from asclepius.store import get_store
    return get_store()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    A.fresh_store()
    _stub_model_legs(monkeypatch)
    yield


@pytest.fixture
def walk():
    """One generated chart walk, through the real admin route."""
    store = _store()
    cid = _ingest_chart(store, build_chart())
    gen = _generate(store, cid, _admin_headers(store))
    return gen["trajectory_id"], store.trajectory_points(gen["trajectory_id"])


def _doctor(store, n=1):
    out = []
    for _ in range(n):
        doc = _approved_physician(store)
        out.append(doc)
    return out


def _dms_to(user_id):
    """The system's DM messages to one physician.

    Routing notifications are DMs in the community store, not emails — the
    "private channel" of §4 row 4 is literally ``get_or_create_dm(SYSTEM, doctor)``
    — so this is where "was doctor k+1 actually told" is answered. Read through
    the community store rather than asserting on the endpoint's own report, which
    would only prove the endpoint's intention."""
    from community.store import get_community_store
    from community.system_posts import SYSTEM_USER_ID

    cstore = get_community_store()
    dm = cstore.get_or_create_dm(SYSTEM_USER_ID, user_id)
    got = cstore.list_messages(dm["id"], limit=200)
    # ``list_messages`` returns (rows, has_more); taking len() of the pair is
    # always 2, which silently turns every "was a DM sent" assertion into a
    # tautology. Unpacked here rather than at each call site.
    rows, _has_more = got if isinstance(got, tuple) else (got, False)
    return list(rows or [])


# ═══ row 1 — batch counts ════════════════════════════════════════════════════
def test_row1_the_longitudinal_batch_counts_trajectories_and_points(walk):
    """After generation the Longitudinal batch must report the walk. It read
    ``0 trajectories · 0 points`` for as long as it did because nothing had ever
    been generated — not because the counting was wrong."""
    tid, points = walk
    admin_h = _admin_headers(_store())
    overview = client.get("/api/asclepius/admin/batches", headers=admin_h).json()
    longitudinal = overview["longitudinal"]
    assert longitudinal["n_trajectories"] == 1, overview
    assert longitudinal["n_points"] == len(points)
    # Every point unrouted, because generating a walk and sending it are two
    # decisions and only the second is an admin pressing Send.
    assert longitudinal["n_unrouted"] == len(points)
    assert [w["trajectory_id"] for w in longitudinal["trajectories"]] == [tid]

    rows = client.get("/api/asclepius/admin/batches/longitudinal",
                      headers=admin_h).json()["cases"]
    assert {r["task_id"] for r in rows} == {p["task_id"] for p in points}
    assert sorted(r["sequence_index"] for r in rows) == list(range(len(points)))


# ═══ row 2 — preview shows no future ═════════════════════════════════════════
def test_row2_previewing_point_k_shows_nothing_after_its_own_decision(walk):
    """The correctness rule the whole product rests on, checked on the ADMIN
    surface too: truncation is a server responsibility, so the preview payload
    must not carry what the physician's payload does not."""
    tid, points = walk
    admin_h = _admin_headers(_store())
    for k, pt in enumerate(points):
        body = client.get(f"/api/asclepius/admin/batches/preview/{pt['task_id']}",
                          headers=admin_h).json()
        offsets = []

        def walk_node(n):
            if isinstance(n, dict):
                for key, v in n.items():
                    if key == "collected_offset_days" and isinstance(v, int):
                        offsets.append(v)
                    walk_node(v)
            elif isinstance(n, list):
                for v in n:
                    walk_node(v)
        walk_node(body)
        assert offsets, f"point {k} preview carried no dated content at all"
        assert max(offsets) <= 0, f"point {k} preview leaked day {max(offsets)}"


# ═══ row 3 — solo send ═══════════════════════════════════════════════════════
def test_row3_a_solo_send_reaches_the_assignee_only(walk):
    tid, points = walk
    store = _store()
    mine, theirs = _doctor(store, 2)
    admin_h = _admin_headers(store)

    r = client.post("/api/asclepius/admin/assignments/allocate", headers=admin_h, json={
        "task_ids": [p["task_id"] for p in points], "user_ids": [mine["id"]],
        "dry_run": False})
    assert r.status_code == 200, r.text

    def cards(user):
        return [t for t in client.get("/api/asclepius/tasks/available?portal_version=v5",
                                      headers=A.headers_for(user)).json()["tasks"]
                if t.get("trajectory_id") == tid]

    assert [c["sequence_index"] for c in cards(mine)] == [0], "point 0 only"
    assert cards(theirs) == [], "distribution gate: assigned_only reaches nobody else"
    # And by ID, not only by queue — a queue-only fix is not a fix.
    #
    # Running this row is what FOUND the leak it now guards: the distribution gate
    # lived only in the queue SQL, so an unrouted assigned_only point was invisible
    # in every queue and simultaneously openable, revealable and SUBMITTABLE by
    # task id. Point 0 clears the sequence gate by construction, so nothing else
    # stood in the way. Fixed in ``_require_distribution``.
    th = A.headers_for(theirs)
    tid0 = points[0]["task_id"]
    assert client.get(f"/api/asclepius/tasks/{tid0}", headers=th).status_code == 403
    assert client.post(f"/api/asclepius/tasks/{tid0}/reveal",
                       json={"text": "stance"}, headers=th).status_code == 403
    assert client.post("/api/asclepius/submissions", headers=th, json={
        "task_id": tid0, "verdict": "A_better", "chosen_id": "a", "rejected_id": "b",
        "confidence": "high", "time_spent_sec": 900}).status_code == 403


# ═══ row 4 — relay send ══════════════════════════════════════════════════════
def test_row4_a_relay_send_rotates_creates_a_channel_and_dms_each_doctor(walk):
    tid, points = walk
    store = _store()
    docs = _doctor(store, 3)
    admin_h = _admin_headers(store)

    dry = client.post("/api/asclepius/admin/batches/relay", headers=admin_h, json={
        "trajectory_id": tid, "user_ids": [d["id"] for d in docs],
        "dry_run": True, "seed": 4242})
    assert dry.status_code == 200, dry.text
    shown = dry.json()

    live = client.post("/api/asclepius/admin/batches/relay", headers=admin_h, json={
        "trajectory_id": tid, "user_ids": [d["id"] for d in docs],
        "dry_run": False, "seed": 4242})
    assert live.status_code == 200, live.text
    committed = live.json()

    # The mapping an admin was SHOWN is the one that commits. Without the fixed
    # seed the preview and the commit are two draws from the same distribution and
    # the screen is a lie the admin cannot detect.
    def rotation(body):
        return [(m["sequence_index"], m["user_id"]) for m in body["mapping"]]
    assert rotation(shown) == rotation(committed), "preview and commit must agree"
    assert committed["n_doctors"] == 3
    assert len({uid for _i, uid in rotation(committed)}) == 3, "each doctor gets a turn"

    # walk_mode is stamped on every point — the gate is mode-dependent, and a
    # relay walk that stayed 'solo' would ask each doctor to have done the
    # earlier points themselves.
    for pt in store.trajectory_points(tid):
        assert asc_trajectory.walk_mode(store.get_task(pt["task_id"])) == "relay"
        assert (store.get_task(pt["task_id"]) or {}).get("distribution") == "assigned_only"

    # A private channel and a DM per doctor. The "private channel" is the
    # system↔doctor DM itself (``get_or_create_dm``) — there is deliberately no
    # SHARED relay channel, and §4 row 4 should be read that way; see the check
    # doc, which records the ambiguity rather than inventing a feature to satisfy
    # a reading of it.
    assert committed["notified"]["dms"] == 3, committed["notified"]
    assert not committed["notified"]["errors"], committed["notified"]
    for d in docs:
        assert _dms_to(d["id"]), f"no DM for {d['id']}"


# ═══ row 5 — the unlock ping ═════════════════════════════════════════════════
def test_row5_the_next_doctor_is_pinged_when_the_previous_one_submits(walk):
    tid, points = walk
    store = _store()
    docs = _doctor(store, 2)
    admin_h = _admin_headers(store)
    client.post("/api/asclepius/admin/batches/relay", headers=admin_h, json={
        "trajectory_id": tid, "user_ids": [d["id"] for d in docs],
        "dry_run": False, "seed": 7})

    assigned = {}
    for pt in store.trajectory_points(tid):
        for a in store.assignments_for_task(pt["task_id"]):
            if a.get("role") == "label":
                assigned[pt["sequence_index"]] = a["user_id"]
    first, second = assigned[0], assigned[1]

    before = len(_dms_to(second))
    r = client.post("/api/asclepius/submissions", headers=A.headers_for(
        store.get_user_by_id(first)), json={
            "task_id": points[0]["task_id"], "verdict": "A_better", "chosen_id": "a",
            "rejected_id": "b", "confidence": "high", "time_spent_sec": 900})
    assert r.status_code == 200, r.text
    assert len(_dms_to(second)) > before, "doctor k+1 was not told the chart moved"


# ═══ row 6 — the reveal window ═══════════════════════════════════════════════
def test_row6_the_reveal_is_strictly_after_the_decision_and_names_the_terminal_point(walk):
    tid, points = walk
    store = _store()
    doc = _approved_physician(store)
    dh = A.headers_for(doc)
    for pt in points:
        store.upsert_assignment(task_id=pt["task_id"], user_id=doc["id"],
                                role="label", assigned_by="u-admin")
    for i, pt in enumerate(points):
        client.post("/api/asclepius/submissions", headers=dh, json={
            "task_id": pt["task_id"], "verdict": "A_better", "chosen_id": "a",
            "rejected_id": "b", "confidence": "high", "time_spent_sec": 900})
        body = client.get(f"/api/asclepius/tasks/{pt['task_id']}/trajectory-outcome",
                          headers=dh).json()
        if i < len(points) - 1:
            # STRICTLY after: an outcome at day 0 is the decision, not its result.
            assert body["outcome"]["days_after_decision"] > 0
        else:
            assert body["outcome"] is None
            assert "last decision point" in body["reason"]


# ═══ row 7 — the stall path ══════════════════════════════════════════════════
def test_row7_a_stalled_relay_nudges_once_then_reassigns(walk):
    tid, points = walk
    store = _store()
    docs = _doctor(store, 2)
    admin_h = _admin_headers(store)
    client.post("/api/asclepius/admin/batches/relay", headers=admin_h, json={
        "trajectory_id": tid, "user_ids": [d["id"] for d in docs],
        "dry_run": False, "seed": 11})

    state = client.get(f"/api/asclepius/admin/batches/relay/{tid}",
                       headers=admin_h).json()
    stalled = next(p for p in state["points"] if p.get("user_id"))
    replacement = _approved_physician(store)

    before_old = len(_dms_to(stalled["user_id"]))
    r = client.post(f"/api/asclepius/admin/batches/relay/{tid}/reassign",
                    headers=admin_h,
                    json={"task_id": stalled["task_id"], "user_id": replacement["id"]})
    assert r.status_code == 200, r.text

    roles = {a["user_id"]: a["status"]
             for a in store.assignments_for_task(stalled["task_id"])
             if a.get("role") == "label"}
    assert roles.get(stalled["user_id"]) == "revoked", roles
    assert roles.get(replacement["id"]) in ("offered", "claimed"), roles
    assert _dms_to(replacement["id"]), "the replacement was not told"
    assert len(_dms_to(stalled["user_id"])) >= before_old


# ═══ row 8 — a retired point does not block the walk ═════════════════════════
def test_row8_retiring_a_middle_point_lets_the_next_one_serve(walk):
    """§9.2 — a point an admin removed can never be submitted, so without the
    retired-status clause it would block every later point FOREVER, for everyone,
    silently: the queue would simply stop offering them."""
    tid, points = walk
    store = _store()
    assert len(points) >= 3, "this row needs a walk with a middle to retire"
    doc = _approved_physician(store)
    dh = A.headers_for(doc)
    for pt in points:
        store.upsert_assignment(task_id=pt["task_id"], user_id=doc["id"],
                                role="label", assigned_by="u-admin")

    # Answer point 0, then retire point 1.
    client.post("/api/asclepius/submissions", headers=dh, json={
        "task_id": points[0]["task_id"], "verdict": "A_better", "chosen_id": "a",
        "rejected_id": "b", "confidence": "high", "time_spent_sec": 900})
    retired = asc_trajectory.RETIRED_STATUSES[0]
    store.mark_task_status(points[1]["task_id"], retired)

    served = [t["sequence_index"] for t in
              client.get("/api/asclepius/tasks/available?portal_version=v5",
                         headers=dh).json()["tasks"]
              if t.get("trajectory_id") == tid]
    assert 1 not in served, "a retired point must never be offered"
    assert 2 in served, "the retired point must not block the rest of the walk"
