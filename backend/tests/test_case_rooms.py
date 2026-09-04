"""Per-case group DM rooms (Task Pipeline PRD §B).

Routing used to send each doctor a solo DM from the bot and nothing connected
the people working the same case. `CASE_BATCHES_AND_ROUTING.md` §8.5 rejected a
private case channel and named the cheaper alternative in the same breath: a
group DM. This is that alternative, and these tests pin the conditions it was
approved under -- the room says nothing about the case, it is keyed on the case
rather than on who is currently in it, and ordinary two-party DMs keep exactly
the privacy they had.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Isolated audit DB for this module (the audit chain writes to TEAM_DB_PATH).
os.environ["TEAM_DB_PATH"] = os.path.join("/tmp", f"case_rooms_audit_{uuid.uuid4().hex}.db")
os.environ.setdefault("EMAIL_DEV_MODE", "1")

from fastapi.testclient import TestClient  # noqa: E402

from tests import _asclepius as A  # noqa: E402
from asclepius import route_notify as RN  # noqa: E402
from community import store as community_store  # noqa: E402

client = TestClient(A.app)
COMMUNITY = "/api/community"


def _store():
    from asclepius.store import get_store
    return get_store()


def _cstore():
    from community.store import get_community_store
    return get_community_store()


@pytest.fixture(autouse=True)
def _isolated():
    """Both planes fresh. The community store is a process-global singleton with
    a default on-disk path, so without the rebind these tests would write rooms
    into the developer's real community.db."""
    from community.ws import hub as _hub
    _hub._sockets.clear()                                        # noqa: SLF001
    A.fresh_store()
    community_store.reset_community_store_for_tests(
        db_path=os.path.join("/tmp", f"case_rooms_{uuid.uuid4().hex}.db"))
    yield


def _doc(store, *, specialty="cardiology"):
    """An approved physician who can actually reach the community."""
    user = A.make_user(store, specialty=specialty, tier="labeler")
    store.set_real_data_approved(user["id"], True)
    with store._conn() as conn:                                  # noqa: SLF001
        conn.execute("UPDATE users SET verification_status = 'approved' WHERE id = ?",
                     (user["id"],))
    store.upsert_contributor_credentials(
        id_hashed=user["id_hashed"], user_id=user["id"],
        organization="Riverside Cardiology", role_title="Physician (MD)",
        credentials_verified=True,
        ship={"degree": "MD", "primary_specialty": specialty,
              "years_in_active_practice": 12, "credentials_verified": True},
        verify={"full_legal_name": "Test Physician, MD", "npi": "1234567893"})
    return store.get_user_by_id(user["id"])


def _admin_headers(store):
    return A.headers_for(A.make_user(store, role="admin"))


def _rooms():
    with _cstore()._conn() as conn:                              # noqa: SLF001
        return [dict(r) for r in conn.execute(
            "SELECT * FROM community_dms WHERE kind = 'case_room'").fetchall()]


def _room_bodies(room_id):
    msgs, _more = _cstore().list_messages(room_id, limit=50)
    return [m["body"] for m in msgs]


# ═══ D2: one room per case, not per member set ══════════════════════════════
def test_room_created_once_per_case_ref():
    """WHY: a room keyed on who is in it forks on every substitution.

    Reassignment changes the members. If the key were the member set, the second
    send would open a second room and strand every message from the first --
    which is exactly the history a founder stepping into a stuck case needs.
    """
    store = _store()
    a, b = _doc(store), _doc(store)
    task = store.insert_task(prompt="c", specialty="cardiology")
    assignments = [{"task_id": task["task_id"], "user_id": a["id"], "role": "label"},
                   {"task_id": task["task_id"], "user_id": b["id"], "role": "label"}]

    first = RN.notify_routed(store, assignments=assignments)
    second = RN.notify_routed(store, assignments=assignments)

    assert first["rooms"] == 1 and second["rooms"] == 1, "both sends resolve a room"
    rooms = _rooms()
    assert len(rooms) == 1, "the second send must REUSE the room, never fork one"
    assert rooms[0]["case_ref"] == "task:" + task["task_id"]
    # And the team is introduced once, not once per send.
    intros = [b for b in _room_bodies(rooms[0]["id"]) if "Introductions first" in b]
    assert len(intros) == 1


def test_a_relay_gets_one_room_for_the_whole_walk():
    """WHY: a chart walk is ONE case taken forward by several people.

    Keying a relay on its points would give a five-point walk five rooms, and
    the handoff conversation would live somewhere different from the people
    handing off.
    """
    store = _store()
    a, b = _doc(store), _doc(store)
    pts = [store.insert_task(prompt=f"p{i}", specialty="cardiology",
                             trajectory_id="traj-room", sequence_index=i)
           for i in range(3)]
    mapping = [{"task_id": pts[0]["task_id"], "user_id": a["id"], "sequence_index": 0},
               {"task_id": pts[1]["task_id"], "user_id": b["id"], "sequence_index": 1},
               {"task_id": pts[2]["task_id"], "user_id": a["id"], "sequence_index": 2}]

    report = RN.notify_relay_send(store, mapping=mapping, trajectory_id="traj-room")

    assert report["rooms"] == 1
    rooms = _rooms()
    assert len(rooms) == 1 and rooms[0]["case_ref"] == "traj:traj-room"
    # A doctor holding two points is one person in the room, not two.
    assert sorted(_cstore().room_participants(rooms[0]["id"])) == sorted([a["id"], b["id"]])


# ═══ §8.5's condition: coordination only ════════════════════════════════════
def test_room_intro_names_no_case_content():
    """WHY: the no-case-content rule is the condition rooms were approved under.

    §8.5 rejected a private case channel partly because the case may not be
    discussed in it, which made the space nearly pointless. The room is worth
    having anyway -- introductions and coordination are real -- but only if it
    never becomes a second place the case is argued about, because the two
    labels have to stay independent for kappa to mean anything. So the intro
    carries names, roles, the case type and the specialty, and the rule itself.
    """
    store = _store()
    a, b = _doc(store), _doc(store)
    secret = "serum potassium of 6.8 with peaked T waves"
    task = store.insert_task(
        prompt=secret, specialty="cardiology",
        case={"notes": [{"text": secret}],
              "ground_truth": {"answer": "give calcium gluconate"}})
    RN.notify_routed(store, assignments=[
        {"task_id": task["task_id"], "user_id": a["id"], "role": "label"},
        {"task_id": task["task_id"], "user_id": b["id"], "role": "review"}])

    room = _rooms()[0]
    intro = next(x for x in _room_bodies(room["id"]) if "Introductions first" in x)

    assert secret not in intro and "calcium gluconate" not in intro
    assert task["task_id"] not in intro, "not even the id: it is a lookup into the case"
    assert "cardiology" in intro, "the specialty is allowed and is the point"
    assert "labeler" in intro and "reviewer" in intro, "roles, so people know who is who"
    assert "do not discuss the case" in intro
    assert RN.ADMIN_VISIBILITY_LINE in intro, (
        "a room people believe is private and is not is worse than no room")


def test_send_to_all_creates_no_room():
    """WHY: PRD B6 -- an open-queue send has no roster to introduce.

    Send-to-all deliberately writes no assignments; the cases enter the open
    queue and anyone eligible may draw them. A room there would introduce a team
    that does not exist.
    """
    store = _store()
    _doc(store)
    task = store.insert_task(prompt="c", specialty="cardiology")

    report = RN.notify_routed(store, assignments=[], to_all=True,
                              task_ids=[task["task_id"]])

    assert report["rooms"] == 0
    assert _rooms() == []


# ═══ B5: reassignment stops being silent ════════════════════════════════════
def test_reassignment_posts_roster_notice_and_swaps_membership():
    """WHY: closes the gap the routing doc records under "What is NOT built".

    The replacement was DMed and nobody else on the walk was told the roster had
    changed, so a physician waiting on a handoff was waiting on somebody who no
    longer had the point. The membership swap is the other half: a doctor taken
    off the case must lose the ability to post into its room, or "removed" is a
    label rather than a fact.
    """
    store = _store()
    ah = _admin_headers(store)
    a, b, c = _doc(store), _doc(store), _doc(store)
    pts = [store.insert_task(prompt=f"p{i}", specialty="cardiology",
                             trajectory_id="traj-s", sequence_index=i)
           for i in range(2)]
    for pt, who in zip(pts, (a, b)):
        store.upsert_assignment(task_id=pt["task_id"], user_id=who["id"],
                                role="label", assigned_by="admin@test")
    RN.notify_relay_send(store, mapping=[
        {"task_id": pts[0]["task_id"], "user_id": a["id"], "sequence_index": 0},
        {"task_id": pts[1]["task_id"], "user_id": b["id"], "sequence_index": 1},
    ], trajectory_id="traj-s")
    room = _rooms()[0]

    r = client.post("/api/asclepius/admin/batches/relay/traj-s/reassign", headers=ah,
                    json={"task_id": pts[0]["task_id"], "user_id": c["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["notified"]["room"] is True

    members = _cstore().room_participants(room["id"])
    assert c["id"] in members, "the replacement joins the room they now belong in"
    assert a["id"] not in members, "the departed member is off it"
    assert b["id"] in members, "everyone else is untouched"
    assert any("now has point 1" in x for x in _room_bodies(room["id"])), (
        "the rest of the chain is told, which is the whole gap this closes")

    # Removed means removed: the departed doctor can no longer post.
    posted = client.post(f"{COMMUNITY}/dms/{room['id']}/messages",
                         json={"body": "still here?"}, headers=A.headers_for(a))
    assert posted.status_code == 404
    # And the room is still readable to whoever remains.
    assert client.get(f"{COMMUNITY}/dms/{room['id']}/messages",
                      headers=A.headers_for(b)).status_code == 200


def test_reassigning_one_point_keeps_a_multi_point_holder_in_the_room():
    """WHY: losing one point of a walk is not leaving the case.

    A doctor can hold two points of the same walk. Reassigning one of them used
    to remove the doctor from the case room outright, so they lost read access
    and got 404s posting about a point they still held. Membership tracks the
    case, and they are still on the case until their LAST live point goes.
    """
    store = _store()
    ah = _admin_headers(store)
    a, b, c = _doc(store), _doc(store), _doc(store)
    pts = [store.insert_task(prompt=f"p{i}", specialty="cardiology",
                             trajectory_id="traj-m", sequence_index=i)
           for i in range(3)]
    for pt, who in zip(pts, (a, b, a)):
        store.upsert_assignment(task_id=pt["task_id"], user_id=who["id"],
                                role="label", assigned_by="admin@test")
    RN.notify_relay_send(store, mapping=[
        {"task_id": pts[0]["task_id"], "user_id": a["id"], "sequence_index": 0},
        {"task_id": pts[1]["task_id"], "user_id": b["id"], "sequence_index": 1},
        {"task_id": pts[2]["task_id"], "user_id": a["id"], "sequence_index": 2},
    ], trajectory_id="traj-m")
    room = _rooms()[0]

    r = client.post("/api/asclepius/admin/batches/relay/traj-m/reassign", headers=ah,
                    json={"task_id": pts[0]["task_id"], "user_id": c["id"]})
    assert r.status_code == 200, r.text

    members = _cstore().room_participants(room["id"])
    assert a["id"] in members, "still holds point 3, so still on the case"
    assert c["id"] in members and b["id"] in members
    posted = client.post(f"{COMMUNITY}/dms/{room['id']}/messages",
                         json={"body": "still working the last point"},
                         headers=A.headers_for(a))
    assert posted.status_code == 200, posted.text

    # Losing the LAST live point is a real departure: removal happens now.
    r = client.post("/api/asclepius/admin/batches/relay/traj-m/reassign", headers=ah,
                    json={"task_id": pts[2]["task_id"], "user_id": c["id"]})
    assert r.status_code == 200, r.text
    assert a["id"] not in _cstore().room_participants(room["id"])


# ═══ D3: the visibility exception is scoped ═════════════════════════════════
def test_admin_can_read_case_room_but_not_private_dm():
    """WHY: D3 is an exception for ``kind='case_room'`` and nothing else.

    Founders being able to step into a stuck case is the reason rooms exist. The
    community router otherwise gives admins NO read access to private
    conversations, and that property is stated in the product. Widening the
    exception to ordinary DMs would break it silently -- an admin reading two
    doctors' private messages, with the doctors still believing otherwise.
    """
    store = _store()
    admin = A.make_user(store, role="admin")
    a, b = _doc(store), _doc(store)
    task = store.insert_task(prompt="c", specialty="cardiology")
    RN.notify_routed(store, assignments=[
        {"task_id": task["task_id"], "user_id": a["id"], "role": "label"},
        {"task_id": task["task_id"], "user_id": b["id"], "role": "label"}])
    room = _rooms()[0]

    opened = client.post(f"{COMMUNITY}/dms", json={"user_id": b["id"]},
                         headers=A.headers_for(a))
    assert opened.status_code == 200, opened.text
    private_id = opened.json()["id"]
    mid = client.post(f"{COMMUNITY}/dms/{private_id}/messages",
                      json={"body": "between the two of us"},
                      headers=A.headers_for(a)).json()["id"]

    ah = A.headers_for(admin)
    assert client.get(f"{COMMUNITY}/dms/{room['id']}/messages", headers=ah).status_code == 200
    assert client.get(f"{COMMUNITY}/dms/{private_id}/messages", headers=ah).status_code == 404
    assert client.get(f"{COMMUNITY}/messages/{mid}/thread", headers=ah).status_code == 404
    # Stepping in means WRITING too: read-only could not unstick a case (D3).
    posted = client.post(f"{COMMUNITY}/dms/{room['id']}/messages",
                         json={"body": "checking in on this handoff"}, headers=ah)
    assert posted.status_code == 200, posted.text
    # And the write half of the exception is scoped exactly like the read half.
    assert client.post(f"{COMMUNITY}/dms/{private_id}/messages",
                       json={"body": "should never land"},
                       headers=ah).status_code == 404


def test_a_member_made_group_is_not_readable_by_an_uninvolved_admin():
    """WHY: the D3 exception is for ``kind='case_room'``, and a group is not one.

    A group is the SAME database object as a case room -- a title, a roster in
    ``community_dm_members``, no peer -- so the temptation when groups landed
    was to widen ``_dm_access`` to "anything with a roster". That would have
    handed admins a read of every conversation the physicians started
    themselves, silently, while the product still says otherwise. The room
    exception exists because a founder has to be able to step into a stuck CASE,
    and it says so out loud inside the room; a group carries no such notice and
    is entitled to none of it.
    """
    store = _store()
    admin = A.make_user(store, role="admin")
    a, b = _doc(store), _doc(store)

    made = client.post(f"{COMMUNITY}/dms/group",
                       json={"title": "Transplant call rota", "user_ids": [b["id"]]},
                       headers=A.headers_for(a))
    assert made.status_code == 200, made.text
    group_id = made.json()["id"]
    assert made.json()["kind"] == "group"

    mid = client.post(f"{COMMUNITY}/dms/{group_id}/messages",
                      json={"body": "swapping Thursday with Friday"},
                      headers=A.headers_for(a)).json()["id"]

    ah = A.headers_for(admin)
    assert client.get(f"{COMMUNITY}/dms/{group_id}/messages", headers=ah).status_code == 404
    assert client.get(f"{COMMUNITY}/messages/{mid}/thread", headers=ah).status_code == 404
    assert client.post(f"{COMMUNITY}/dms/{group_id}/messages",
                       json={"body": "should never land"}, headers=ah).status_code == 404
    # And the admin cannot add themselves to one either -- membership is the
    # access, so a route that let an outsider join would be the read exception
    # rebuilt one call further along.
    assert client.post(f"{COMMUNITY}/dms/{group_id}/members",
                       json={"user_ids": [admin["id"]]}, headers=ah).status_code == 404
    # The participant, meanwhile, reads it exactly as they should.
    assert client.get(f"{COMMUNITY}/dms/{group_id}/messages",
                      headers=A.headers_for(b)).status_code == 200


def test_a_group_roster_cannot_be_widened_into_a_case_room():
    """The members route is scoped to groups on purpose.

    A case room's roster IS the routed team: it changes when an assignment
    changes, and letting somebody in the room add a colleague would put a
    physician on a case nobody assigned them to, with a label that counts
    towards kappa.
    """
    store = _store()
    a, b, c = _doc(store), _doc(store), _doc(store)
    task = store.insert_task(prompt="c", specialty="cardiology")
    RN.notify_routed(store, assignments=[
        {"task_id": task["task_id"], "user_id": a["id"], "role": "label"},
        {"task_id": task["task_id"], "user_id": b["id"], "role": "label"}])
    room = _rooms()[0]

    r = client.post(f"{COMMUNITY}/dms/{room['id']}/members",
                    json={"user_ids": [c["id"]]}, headers=A.headers_for(a))
    assert r.status_code == 400, r.text
    assert c["id"] not in _cstore().room_participants(room["id"])


def test_two_party_dms_unchanged():
    """WHY: the migration backfilled every existing DM into a members table.

    A backfill that changed what a two-party DM does -- who can read it, who it
    lists for, what the peer is -- would be a privacy regression delivered as a
    schema change, which is the kind nobody reviews for privacy.
    """
    store = _store()
    a, b, outsider = _doc(store), _doc(store), _doc(store, specialty="oncology")
    opened = client.post(f"{COMMUNITY}/dms", json={"user_id": b["id"]},
                         headers=A.headers_for(a))
    dm_id = opened.json()["id"]
    assert opened.json()["peer"]["user_id"] == b["id"], "still peer-shaped"
    assert opened.json()["kind"] == "dm"

    client.post(f"{COMMUNITY}/dms/{dm_id}/messages", json={"body": "rubric axis 3"},
                headers=A.headers_for(a))
    seen = client.get(f"{COMMUNITY}/dms", headers=A.headers_for(b)).json()["dms"]
    convo = next(d for d in seen if d["id"] == dm_id)
    assert convo["unread"] == 1 and convo["peer"]["user_id"] == a["id"]

    for path in (f"{COMMUNITY}/dms/{dm_id}/messages",):
        assert client.get(path, headers=A.headers_for(outsider)).status_code == 404
    # The row itself is still the two-party shape the UNIQUE constraint needs.
    row = _cstore().get_dm(dm_id)
    assert row["kind"] == "dm" and row["case_ref"] is None
    assert sorted([row["user_a"], row["user_b"]]) == sorted([a["id"], b["id"]])


# ═══ B4: routing is never hostage to the community ══════════════════════════
def test_room_failure_does_not_fail_send(monkeypatch):
    """WHY: ``notify_routed`` never raises, and the assignment is already committed.

    A doctor with the work and no room finds the work on their next draw. A
    doctor whose routing was rolled back by a community write failure has
    neither the work nor the message, which is strictly worse and is invisible
    to everyone.
    """
    store = _store()
    a, b = _doc(store), _doc(store)
    task = store.insert_task(prompt="c", specialty="cardiology")

    def _explode(*args, **kwargs):
        raise RuntimeError("community is down")

    monkeypatch.setattr(type(_cstore()), "get_or_create_case_room", _explode)

    report = RN.notify_routed(store, assignments=[
        {"task_id": task["task_id"], "user_id": a["id"], "role": "label"},
        {"task_id": task["task_id"], "user_id": b["id"], "role": "label"}])

    assert report["dms"] == 2, "the DMs still went out"
    assert report["rooms"] == 0
    assert any("room:" in e for e in report["errors"]), (
        "reported rather than swallowed: an admin must see the room did not open")
    assert _rooms() == []


def test_room_events_are_audited_with_the_case_ref():
    """WHY: PRD B7/D4 -- the audit line is the evidence a blind pair gets checked
    against.

    Independence of the two labels rests on the pre-reveal blind commit, not on
    the labelers being strangers. The residual risk is that somebody breaks the
    no-case-content rule in the room anyway. That risk is managed by the room
    being admin-visible and by there being a durable record of which room
    existed for which case, and when its membership moved.
    """
    import sqlite3

    store = _store()
    a, b = _doc(store), _doc(store)
    task = store.insert_task(prompt="c", specialty="cardiology")
    RN.notify_routed(store, assignments=[
        {"task_id": task["task_id"], "user_id": a["id"], "role": "label"},
        {"task_id": task["task_id"], "user_id": b["id"], "role": "label"}])

    with sqlite3.connect(os.environ["TEAM_DB_PATH"]) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM audit_events WHERE action = 'community.case_room_created'"
        ).fetchall()]
    assert rows, "room creation must leave a record"
    assert "task:" + task["task_id"] in rows[-1]["detail_json"]
