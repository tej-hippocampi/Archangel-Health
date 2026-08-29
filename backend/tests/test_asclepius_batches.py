"""Case batches and explicit routing — the admin end of "send these to Dr. X".

The allocator already on main answers "spread this fairly across whoever is
eligible". It has no answer for "send these three points to Dr. Faheem", which is
the only question the Batches screen asks. This file covers that gap and the two
safety properties that come with it.

**The predecessor rule (§2.2).** Sending point 5 of a chart walk without points
0–4 writes an assignment that can never be served: the sequence gate refuses point
5 to a physician who has not completed the earlier ones, so the case sits in their
queue permanently unservable and reads as the product being broken rather than as
a mis-click in admin. The server RE-DERIVES the required set and refuses a payload
that omits it, naming the points. It does not trust the client's arithmetic, which
is this branch's standing rule about ordering — the client contains no sequence
logic and a separate test asserts it.

**The preview rule (§2.3).** The admin preview is built by ``_blind_task``, the
same function that builds what a physician is served. A preview with its own idea
of the payload would be a second definition of "what may be seen", and the first
time the two drifted, admin would render a future the portal correctly hides. A
screenshot of encounter 6 in a Slack thread leaks the answer to decision 5 exactly
as thoroughly as serving it, and just as permanently.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402

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


def _doc(store, *, specialty="cardiology", approved=True):
    u = A.make_user(store, specialty=specialty, tier="labeler")
    if approved:
        store.set_real_data_approved(u["id"], True)
    return store.get_user_by_id(u["id"])


def _walk(store, n=4, *, specialty="cardiology"):
    return [store.insert_task(prompt=f"point {i}", specialty=specialty,
                              trajectory_id="traj-b", sequence_index=i,
                              distribution="assigned_only")
            for i in range(n)]


# ═══════════════════════════════════════════════════════════════════════════════
# Level 1 + 2 — the batch view
# ═══════════════════════════════════════════════════════════════════════════════
def test_the_three_classes_are_counted_separately():
    store = _store()
    ah = _admin(store)
    _walk(store, 3)
    store.insert_task(prompt="synthetic", specialty="cardiology")

    r = client.get("/api/asclepius/admin/batches", headers=ah)
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["longitudinal"]["n_trajectories"] == 1
    assert b["longitudinal"]["n_points"] == 3
    assert b["longitudinal"]["n_unrouted"] == 3, "nothing routed yet"
    assert b["synthetic"]["n_cases"] == 1


def test_a_walk_reports_routed_and_unrouted_as_admin_routes_it():
    store = _store()
    ah, doc = _admin(store), _doc(store)
    pts = _walk(store, 3)
    store.upsert_assignment(task_id=pts[0]["task_id"], user_id=doc["id"],
                            role="label", assigned_by="u-admin")

    b = client.get("/api/asclepius/admin/batches", headers=ah).json()
    walk = b["longitudinal"]["trajectories"][0]
    assert (walk["n_routed"], walk["n_unrouted"]) == (1, 2)


def test_batch_rows_carry_the_per_case_routing_facts():
    store = _store()
    ah, doc = _admin(store), _doc(store)
    pts = _walk(store, 2)
    store.upsert_assignment(task_id=pts[1]["task_id"], user_id=doc["id"],
                            role="label", assigned_by="u-admin")

    rows = client.get("/api/asclepius/admin/batches/longitudinal", headers=ah).json()["cases"]
    assert [r["sequence_index"] for r in rows] == [0, 1], "points in sequence order"
    assert rows[0]["assigned_to"] is None
    assert doc["email"] in (rows[1]["assigned_to"] or "")
    assert all(r["distribution"] == "assigned_only" for r in rows)


def test_an_unknown_batch_is_a_404_not_an_empty_list():
    """An empty list would read as "this class has no cases", which is a different
    and reassuring falsehood."""
    store = _store()
    assert client.get("/api/asclepius/admin/batches/nonsense",
                      headers=_admin(store)).status_code == 404


# ═══════════════════════════════════════════════════════════════════════════════
# §2.2 — implied predecessors, re-derived server-side
# ═══════════════════════════════════════════════════════════════════════════════
def test_sending_a_mid_walk_point_alone_is_refused_naming_the_missing_points():
    store = _store()
    ah, doc = _admin(store), _doc(store)
    pts = _walk(store, 5)

    r = client.post("/api/asclepius/admin/assignments/allocate", headers=ah, json={
        "task_ids": [pts[3]["task_id"]], "user_ids": [doc["id"]], "dry_run": False})
    assert r.status_code == 400, r.text
    d = r.json()["detail"]
    assert d["error"] == "missing_trajectory_predecessors"
    assert d["missing"][pts[3]["task_id"]] == [0, 1, 2], "names WHICH points"
    assert set(d["add_task_ids"]) == {p["task_id"] for p in pts[:3]}


def test_the_refusal_happens_before_anything_is_written():
    """A partial write would leave assignments for the points that were fine and
    none for the ones that were not — a half-sent walk nobody asked for."""
    store = _store()
    ah, doc = _admin(store), _doc(store)
    pts = _walk(store, 4)
    client.post("/api/asclepius/admin/assignments/allocate", headers=ah, json={
        "task_ids": [pts[0]["task_id"], pts[2]["task_id"]],
        "user_ids": [doc["id"]], "dry_run": False})
    assert store.assignments_for_user(doc["id"]) == []


def test_the_full_implied_set_is_accepted_and_the_gate_still_orders_the_walk():
    """Acceptance is not the same as unsealing: all four points are assigned, and
    the physician is still served exactly one."""
    store = _store()
    ah, doc = _admin(store), _doc(store)
    pts = _walk(store, 4)

    r = client.post("/api/asclepius/admin/assignments/allocate", headers=ah, json={
        "task_ids": [p["task_id"] for p in pts], "user_ids": [doc["id"]],
        "dry_run": False})
    assert r.status_code == 200, r.text
    assert len(r.json()["committed"]) == 4

    servable = [t["task_id"] for t in store.eligible_tasks_for_evaluator(
        evaluator_id=doc["id"], specialty="cardiology")]
    assert servable == [pts[0]["task_id"]], "routed, but still walked in order"


def test_a_non_trajectory_selection_is_unaffected_by_the_rule():
    store = _store()
    ah, doc = _admin(store), _doc(store)
    t = store.insert_task(prompt="ordinary", specialty="cardiology")
    r = client.post("/api/asclepius/admin/assignments/allocate", headers=ah, json={
        "task_ids": [t["task_id"]], "user_ids": [doc["id"]], "dry_run": False})
    assert r.status_code == 200, r.text


# ═══════════════════════════════════════════════════════════════════════════════
# §2.4 — the three ways to choose who
# ═══════════════════════════════════════════════════════════════════════════════
def test_user_ids_bypasses_the_allocator_and_assigns_exactly_those_doctors():
    store = _store()
    ah = _admin(store)
    a, b = _doc(store), _doc(store)
    t = store.insert_task(prompt="one case", specialty="cardiology")

    r = client.post("/api/asclepius/admin/assignments/allocate", headers=ah, json={
        "task_ids": [t["task_id"]], "user_ids": [a["id"]], "dry_run": False})
    assert r.json()["targeting"] == "explicit"
    holders = {x["user_id"] for x in store.assignments_for_task(t["task_id"])}
    assert holders == {a["id"]}, "exactly the named doctor, nobody the allocator liked"


def test_an_explicit_send_makes_the_cases_assigned_only():
    """Naming doctors is what turns a priority into a permission."""
    store = _store()
    ah, doc = _admin(store), _doc(store)
    other = _doc(store)
    t = store.insert_task(prompt="one case", specialty="cardiology")
    assert t["distribution"] == "open"

    client.post("/api/asclepius/admin/assignments/allocate", headers=ah, json={
        "task_ids": [t["task_id"]], "user_ids": [doc["id"]], "dry_run": False})
    assert store.get_task(t["task_id"])["distribution"] == "assigned_only"
    assert t["task_id"] not in [x["task_id"] for x in store.eligible_tasks_for_evaluator(
        evaluator_id=other["id"], specialty="cardiology")]


def test_a_specialty_send_resolves_its_roster_at_send_time():
    store = _store()
    ah = _admin(store)
    card = _doc(store, specialty="cardiology")
    neph = _doc(store, specialty="nephrology")
    t = store.insert_task(prompt="c", specialty="cardiology")

    r = client.post("/api/asclepius/admin/assignments/allocate", headers=ah, json={
        "task_ids": [t["task_id"]], "specialty": "cardiology", "dry_run": False})
    assert r.json()["targeting"] == "specialty"
    holders = {x["user_id"] for x in store.assignments_for_task(t["task_id"])}
    assert holders == {card["id"]} and neph["id"] not in holders


def test_send_to_all_opens_the_queue_and_writes_no_assignments():
    """The deliberate un-sealing. For a longitudinal walk this is what the admin
    chose; the endpoint reports it so the UI can say so before committing."""
    store = _store()
    ah = _admin(store)
    doc = _doc(store)
    pts = _walk(store, 3)

    r = client.post("/api/asclepius/admin/assignments/allocate", headers=ah, json={
        "task_ids": [p["task_id"] for p in pts], "to_all": True, "dry_run": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["targeting"] == "all" and body["distribution"] == "open"
    assert body["committed"] == [], "to_all writes no assignment rows"
    assert all(store.get_task(p["task_id"])["distribution"] == "open" for p in pts)
    # …and the walk is now drawable by an unassigned doctor, in order.
    assert [t["task_id"] for t in store.eligible_tasks_for_evaluator(
        evaluator_id=doc["id"], specialty="cardiology")] == [pts[0]["task_id"]]


def test_a_dry_run_changes_nothing():
    store = _store()
    ah, doc = _admin(store), _doc(store)
    t = store.insert_task(prompt="c", specialty="cardiology")
    r = client.post("/api/asclepius/admin/assignments/allocate", headers=ah, json={
        "task_ids": [t["task_id"]], "user_ids": [doc["id"]], "dry_run": True})
    assert r.status_code == 200
    assert r.json()["assignments"], "the proposal is still shown"
    assert store.assignments_for_task(t["task_id"]) == []
    assert store.get_task(t["task_id"])["distribution"] == "open", "no flip on a dry run"


def test_targeting_modes_are_mutually_exclusive():
    store = _store()
    r = client.post("/api/asclepius/admin/assignments/allocate", headers=_admin(store),
                    json={"task_ids": ["t1"], "user_ids": ["u1"], "to_all": True})
    assert r.status_code == 422, "combining modes would silently pick one"


def test_a_doctor_not_approved_for_real_data_is_refused_at_send():
    """The V4 wall is not negotiable from admin. An assignment written past it is
    a row that can never be served, which looks like a routing bug forever."""
    store = _store()
    ah = _admin(store)
    unapproved = _doc(store, approved=False)
    t = store.insert_task(prompt="c", specialty="cardiology")
    r = client.post("/api/asclepius/admin/assignments/allocate", headers=ah, json={
        "task_ids": [t["task_id"]], "user_ids": [unapproved["id"]], "dry_run": False})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "not_approved_for_real_data"


def test_unknown_user_ids_are_named_not_silently_dropped():
    store = _store()
    ah, doc = _admin(store), _doc(store)
    t = store.insert_task(prompt="c", specialty="cardiology")
    r = client.post("/api/asclepius/admin/assignments/allocate", headers=ah, json={
        "task_ids": [t["task_id"]], "user_ids": [doc["id"], "u-ghost"], "dry_run": False})
    assert r.status_code == 404
    assert r.json()["detail"]["user_ids"] == ["u-ghost"]


# ═══════════════════════════════════════════════════════════════════════════════
# §2.3 — the preview is the doctor's payload, not a second opinion of it
# ═══════════════════════════════════════════════════════════════════════════════
def test_the_preview_is_built_by_the_serve_paths_own_function():
    store = _store()
    ah = _admin(store)
    t = store.insert_task(prompt="What next?", specialty="cardiology")
    r = client.get(f"/api/asclepius/admin/batches/preview/{t['task_id']}", headers=ah)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["task"]["task_id"] == t["task_id"]
    assert "read-only" in body["eyebrow"]

    from routers.asclepius import _blind_task
    assert body["task"] == _blind_task(store.get_task(t["task_id"])), (
        "the preview must BE the served payload, not a parallel rendering of it")


def test_a_missing_task_previews_as_404():
    store = _store()
    assert client.get("/api/asclepius/admin/batches/preview/t-ghost",
                      headers=_admin(store)).status_code == 404


def test_a_longitudinal_preview_carries_no_data_past_the_decision_point():
    """§6's preview assertion, and the reason the endpoint reuses ``_blind_task``.

    The truncation is baked into the stored case — ``build_encounter_case`` writes
    the visible window, not the whole chart — so the admin preview inherits it by
    construction rather than by remembering to apply it. This test exists because
    "inherits it by construction" is exactly the kind of claim that quietly stops
    being true after a refactor that starts assembling the preview from the parent
    chart to show admin "more context"."""
    store = _store()
    ah = _admin(store)
    # A point whose stored case is already the truncated window: everything at or
    # before day 0, nothing after. That IS the product's invariant.
    case = {
        "case_source": "real_deid", "specialty": "cardiology",
        "lab_panels": [{"panel": "LFT", "collected_offset_days": -14, "results": []},
                       {"panel": "LFT", "collected_offset_days": 0, "results": []}],
        "notes": [{"text": "day of decision", "collected_offset_days": 0}],
    }
    t = store.insert_task(prompt="What now?", specialty="cardiology", case=case,
                          trajectory_id="traj-p", sequence_index=0,
                          distribution="assigned_only")

    body = client.get(f"/api/asclepius/admin/batches/preview/{t['task_id']}",
                      headers=ah).json()

    offsets = []
    served_case = body["task"].get("case") or {}
    for key in ("lab_panels", "notes", "studies", "medications"):
        for item in (served_case.get(key) or []):
            off = item.get("collected_offset_days")
            if isinstance(off, int):
                offsets.append(off)
    assert offsets, "the preview must actually contain the case, or this is vacuous"
    assert max(offsets) <= 0, (
        f"admin preview leaked data past the decision point: {sorted(offsets)}. "
        f"A screenshot of a future in a Slack thread is the same leak as serving it.")
    assert body["trajectory"] == {"n_points": 1, "position": 1}
