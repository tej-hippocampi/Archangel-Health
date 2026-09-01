"""Telling a physician that work was routed to them (Case Batches PRD §4).

An assignment is a database row. Until somebody is told, it changes what the queue
serves and nothing a human knows — the doctor finds their routed case only by
opening the portal and happening to draw it.

The test that matters most here is the one about failure. The assignment is the
truth and the ping is a courtesy, so a community outage must never roll back
routing the queue is already honouring: a doctor with neither the work nor the
message is strictly worse off than one with the work and no message. Everything
else in this file is about not becoming the sender people mute.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import route_notify as RN  # noqa: E402

client = TestClient(A.app)


@pytest.fixture(autouse=True)
def _isolated():
    A.fresh_store()
    yield


def _store():
    from asclepius.store import get_store
    return get_store()


def _cstore():
    from community.store import get_community_store
    return get_community_store()


def _admin(store):
    return A.headers_for(A.make_user(store, role="admin"))


def _doc(store, *, specialty="cardiology"):
    u = A.make_user(store, specialty=specialty, tier="labeler")
    store.set_real_data_approved(u["id"], True)
    return store.get_user_by_id(u["id"])


def _dms_to(user_id):
    from community.system_posts import SYSTEM_USER_ID
    cstore = _cstore()
    out = []
    for dm in cstore.list_dms_for(user_id):
        if SYSTEM_USER_ID not in (dm["user_a"], dm["user_b"]):
            continue
        out.extend(cstore.messages_for_channel(dm["id"])
                   if hasattr(cstore, "messages_for_channel") else [])
    return out


# ═══════════════════════════════════════════════════════════════════════════════
# Composition — the copy, which is the whole product surface here
# ═══════════════════════════════════════════════════════════════════════════════
def test_one_message_lists_every_case_rather_than_one_message_each():
    """13 DMs for a 13-point walk is not 13x more informative; it is a reason to
    mute the sender, and the physician who mutes us is the one we most need to
    reach next time."""
    body = RN.compose_dm(
        doctor={"name": "Kamran Faheem"},
        tasks=[{"specialty": "hepatology", "difficulty": "hard",
                "trajectory_id": "t", "sequence_index": i} for i in range(13)])
    assert body.count("New cases routed to you") == 1
    assert body.count("  · ") == 13
    assert "13 new longitudinal cases" in body


def test_the_copy_does_not_promise_an_interruption_it_cannot_deliver():
    """An assignment affects the NEXT draw. Promising instant replacement and then
    not delivering it reads as a broken queue, so the copy says what actually
    happens."""
    body = RN.compose_dm(doctor={"name": "A B"}, tasks=[{"specialty": "cardiology"}])
    assert "finish it" in body and "right after you submit" in body


def test_the_longitudinal_paragraph_renders_only_for_a_walk():
    walk = RN.compose_dm(doctor={"name": "A B"},
                         tasks=[{"trajectory_id": "t", "sequence_index": 0}])
    plain = RN.compose_dm(doctor={"name": "A B"}, tasks=[{"specialty": "cardiology"}])
    assert "walk one real patient forward in time" in walk
    assert "walk one real patient forward in time" not in plain


def test_no_deadline_language_unless_a_deadline_was_actually_set():
    """Contributors are volunteers with clinics to run. Inventing urgency is how a
    channel stops being read."""
    without = RN.compose_dm(doctor={"name": "A B"}, tasks=[{"specialty": "cardiology"}])
    assert "until" not in without.lower().split("questions")[0]
    withdue = RN.compose_dm(doctor={"name": "A B"}, tasks=[{"specialty": "cardiology"}],
                            due_at="2026-09-30T00:00:00Z")
    assert "yours first until 2026-09-30" in withdue


def test_the_name_degrades_without_ever_addressing_a_blank():
    for who, expect in (({"last_name": "Shafipour"}, "Dr. Shafipour"),
                        ({"name": "Anjali R Vadgama"}, "Dr. Vadgama"),
                        ({"email": "jdoe@x.test"}, "Dr. jdoe"),
                        ({}, "Dr. there")):
        assert expect in RN.compose_dm(doctor=who, tasks=[{"specialty": "x"}])


def test_a_mixed_send_does_not_claim_one_class():
    body = RN.compose_dm(doctor={"name": "A B"}, tasks=[
        {"specialty": "cardiology"},
        {"specialty": "cardiology", "trajectory_id": "t", "sequence_index": 0}])
    assert "2 new new cases" in body or "2 new cases" in body
    assert "synthetic multimodal" in body and "longitudinal" in body


def test_classify_uses_the_same_discriminators_as_the_queue():
    assert RN.classify({"trajectory_id": "t"}) == "longitudinal"
    assert RN.classify({"case_source": "real_deid"}) == "real_static"
    assert RN.classify({}) == "synthetic"
    assert RN.classify({"trajectory_id": "t", "case_source": "real_deid"}) == "longitudinal"


# ═══════════════════════════════════════════════════════════════════════════════
# Delivery, through the real send path
# ═══════════════════════════════════════════════════════════════════════════════
def test_a_targeted_send_dms_each_doctor_once():
    store = _store()
    ah = _admin(store)
    a, b = _doc(store), _doc(store)
    tasks = [store.insert_task(prompt=f"c{i}", specialty="cardiology") for i in range(3)]

    r = client.post("/api/asclepius/admin/assignments/allocate", headers=ah, json={
        "task_ids": [t["task_id"] for t in tasks],
        "user_ids": [a["id"], b["id"]], "dry_run": False})
    assert r.status_code == 200, r.text
    assert r.json()["notified"]["dms"] == 2, "one per doctor, not one per case"
    assert r.json()["notified"]["channel"] is False, "targeted sends do not announce"


def test_a_dry_run_tells_nobody():
    store = _store()
    ah, doc = _admin(store), _doc(store)
    t = store.insert_task(prompt="c", specialty="cardiology")
    r = client.post("/api/asclepius/admin/assignments/allocate", headers=ah, json={
        "task_ids": [t["task_id"]], "user_ids": [doc["id"]], "dry_run": True})
    assert r.json()["notified"] == {"dms": 0, "channel": False, "errors": []}


def test_send_to_all_announces_once_and_dms_nobody():
    store = _store()
    ah = _admin(store)
    _doc(store)
    tasks = [store.insert_task(prompt=f"c{i}", specialty="cardiology") for i in range(2)]
    r = client.post("/api/asclepius/admin/assignments/allocate", headers=ah, json={
        "task_ids": [t["task_id"] for t in tasks], "to_all": True, "dry_run": False})
    n = r.json()["notified"]
    assert n["dms"] == 0, "to_all writes no assignments, so there is nobody to DM"
    assert n["channel"] is True, (
        "the announcement IS the delivery for a send-to-all — without it nothing "
        "tells anyone the cases exist")
    assert not n["errors"], n["errors"]


def test_the_announcement_actually_lands_in_task_announcements():
    """Asserted on the stored message, not on the return value.

    This module swallows its own failures by design, which is right — and it is
    also how the first version of this shipped calling post_system_message with
    the wrong keyword: every post raised, every raise was caught, and the report
    said channel=False with nobody looking. The report is now checked above, and
    the row is checked here."""
    store = _store()
    ah = _admin(store)
    t = store.insert_task(prompt="c", specialty="cardiology")
    client.post("/api/asclepius/admin/assignments/allocate", headers=ah, json={
        "task_ids": [t["task_id"]], "to_all": True, "dry_run": False})

    cstore = _cstore()
    ch = cstore.get_channel_by_slug("task-announcements")
    assert ch, "the channel must exist for this to mean anything"
    msgs, _more = cstore.list_messages(ch["id"], limit=50)
    bodies = [m["body"] for m in msgs]
    assert any("open queue" in b for b in bodies), bodies


def test_a_failing_dm_never_rolls_back_the_assignment(monkeypatch):
    """The rule the whole module exists under, executed.

    A physician with the work and no message finds it on their next draw. A
    physician with neither has been silently un-routed by a community outage."""
    store = _store()
    ah, doc = _admin(store), _doc(store)
    t = store.insert_task(prompt="c", specialty="cardiology")

    def _boom(*a, **k):
        raise RuntimeError("community is down")
    monkeypatch.setattr(RN, "_dm_one", _boom)

    r = client.post("/api/asclepius/admin/assignments/allocate", headers=ah, json={
        "task_ids": [t["task_id"]], "user_ids": [doc["id"]], "dry_run": False})
    assert r.status_code == 200, "the send must succeed"
    assert len(r.json()["committed"]) == 1
    assert store.assignments_for_task(t["task_id"]), "the assignment survived"
    assert store.get_task(t["task_id"])["distribution"] == "assigned_only"
    assert r.json()["notified"]["errors"], "and the failure is reported, not hidden"


def test_the_report_says_what_went_out_not_what_was_intended():
    """An admin whose community is down must see "0 DMs" on the screen, not learn
    about it when a physician says nobody told them."""
    store = _store()
    ah, doc = _admin(store), _doc(store)
    t = store.insert_task(prompt="c", specialty="cardiology")
    r = client.post("/api/asclepius/admin/assignments/allocate", headers=ah, json={
        "task_ids": [t["task_id"]], "user_ids": [doc["id"]], "dry_run": False})
    assert set(r.json()["notified"]) == {"dms", "channel", "errors"}


# ═══════════════════════════════════════════════════════════════════════════════
# The gap this found: the bot as a DM peer
# ═══════════════════════════════════════════════════════════════════════════════
def test_a_system_dm_renders_as_archangel_not_as_a_ghost():
    """``_serialize_messages`` always special-cased the bot as an AUTHOR. The DM
    summary never did, because nothing had ever DM'd from the bot — so a routing
    DM would have landed in the doctor's inbox attributed to a deleted-looking
    ghost, telling them to go do work."""
    from community.router import _dm_summary
    from community.system_posts import SYSTEM_USER_ID

    summary = _dm_summary({"id": "dm-1", "user_a": SYSTEM_USER_ID, "user_b": "u-doc"},
                          "u-doc", {})
    assert summary["peer"]["display_name"] == "Archangel"
    assert summary["peer"].get("is_bot") is True
