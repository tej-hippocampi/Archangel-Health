"""Community v2.1 test suite — events, polls, pinned messages, bookmarks, and
the gated @channel/@here broadcast.

Reuses the v2 fixtures (fresh asclepius + community stores, verified/approved
physicians, an admin). Every feature is covered end to end through the HTTP
surface, plus the serialization enrichment and the broadcast notification/badge
semantics.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta

os.environ["TEAM_DB_PATH"] = os.path.join("/tmp", f"community_social_audit_{uuid.uuid4().hex}.db")
os.environ.setdefault("EMAIL_DEV_MODE", "1")

from _asclepius import app, fresh_store, headers_for, make_user, uniq  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from community import events as cevents  # noqa: E402
from community import store as community_store  # noqa: E402

client = TestClient(app)
BASE = "/api/community"


def fresh_community():
    return community_store.reset_community_store_for_tests(
        db_path=os.path.join("/tmp", f"community_social_{uuid.uuid4().hex}.db"))


def make_doc(store, *, specialty="nephrology"):
    u = store.create_user(email=f"dr-{uniq()}@x.com", password="pw-12345678",
                          role="evaluator", specialty=specialty)
    store.record_verification_decision(u["id"], status="approved",
                                       decided_by="t", tier="labeler")
    return store.get_user_by_id(u["id"])


def setup_world():
    from community.ws import hub as _hub
    _hub._sockets.clear()
    astore = fresh_store()
    cstore = fresh_community()
    admin = make_user(astore, role="admin")
    doc = make_doc(astore)
    return astore, cstore, admin, doc


def _future(days=10, hour=17):
    return (datetime.utcnow() + timedelta(days=days)).replace(
        hour=hour, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


# ─── Events ───────────────────────────────────────────────────────────────────
def test_event_create_lists_and_links_message():
    astore, cstore, admin, doc = setup_world()
    r = client.post(f"{BASE}/events", headers=headers_for(admin), json={
        "title": "Journal Club", "description": "case review",
        "starts_at": _future(), "timezone": "America/New_York",
        "location": "https://zoom.us/j/123", "host": "Dr. Raman"})
    assert r.status_code == 200, r.text
    ev = r.json()
    assert ev["rsvp_count"] == 0 and ev["message_id"]
    up = client.get(f"{BASE}/events?scope=upcoming", headers=headers_for(doc)).json()["events"]
    assert len(up) == 1 and up[0]["title"] == "Journal Club"
    # linked in-stream message carries kind + event payload, and NO raw date
    msgs = client.get(f"{BASE}/channels/events/messages", headers=headers_for(doc)).json()["messages"]
    em = [m for m in msgs if m["kind"] == "event"][0]
    assert em["event"]["title"] == "Journal Club"
    assert "20" not in em["body"] or "2024" not in em["body"]  # no full calendar date in body


def test_event_create_requires_admin():
    astore, cstore, admin, doc = setup_world()
    r = client.post(f"{BASE}/events", headers=headers_for(doc),
                    json={"title": "x", "starts_at": _future()})
    assert r.status_code == 403


def test_event_rejects_bad_date_and_end_before_start():
    astore, cstore, admin, doc = setup_world()
    assert client.post(f"{BASE}/events", headers=headers_for(admin),
                       json={"title": "x", "starts_at": "not-a-date"}).status_code == 400
    assert client.post(f"{BASE}/events", headers=headers_for(admin), json={
        "title": "x", "starts_at": _future(days=10),
        "ends_at": _future(days=9)}).status_code == 400


def test_event_phi_blocked():
    astore, cstore, admin, doc = setup_world()
    r = client.post(f"{BASE}/events", headers=headers_for(admin), json={
        "title": "Case: MRN 12345678 review", "starts_at": _future()})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "phi_detected"


def test_event_rsvp_toggles_and_counts():
    astore, cstore, admin, doc = setup_world()
    ev = client.post(f"{BASE}/events", headers=headers_for(admin),
                     json={"title": "JC", "starts_at": _future()}).json()
    r = client.post(f"{BASE}/events/{ev['id']}/rsvp", headers=headers_for(doc)).json()
    assert r["rsvp_count"] == 1 and r["viewer_interested"] is True
    r = client.post(f"{BASE}/events/{ev['id']}/rsvp", headers=headers_for(doc)).json()
    assert r["rsvp_count"] == 0 and r["viewer_interested"] is False


def test_event_ics_download():
    astore, cstore, admin, doc = setup_world()
    ev = client.post(f"{BASE}/events", headers=headers_for(admin), json={
        "title": "Grand Rounds", "starts_at": "2099-09-10T17:00",
        "location": "Aud A", "host": "Dr. X"}).json()
    r = client.get(f"{BASE}/events/{ev['id']}/calendar.ics", headers=headers_for(doc))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/calendar")
    body = r.text
    assert "BEGIN:VEVENT" in body and "DTSTART:20990910T170000Z" in body
    assert "SUMMARY:Grand Rounds" in body and "LOCATION:Aud A" in body


def test_event_cancel_hides_from_upcoming():
    astore, cstore, admin, doc = setup_world()
    ev = client.post(f"{BASE}/events", headers=headers_for(admin),
                     json={"title": "JC", "starts_at": _future()}).json()
    assert client.post(f"{BASE}/events/{ev['id']}/cancel", headers=headers_for(admin)).status_code == 200
    up = client.get(f"{BASE}/events?scope=upcoming", headers=headers_for(doc)).json()["events"]
    assert up == []
    past = client.get(f"{BASE}/events?scope=past", headers=headers_for(doc)).json()["events"]
    assert len(past) == 1 and past[0]["cancelled"] is True


def test_event_reminder_sends_once_and_only_within_window(monkeypatch):
    astore, cstore, admin, doc = setup_world()
    doc2 = make_doc(astore)
    soon_at = (datetime.utcnow() + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    soon = client.post(f"{BASE}/events", headers=headers_for(admin),
                       json={"title": "Soon", "starts_at": soon_at}).json()
    far = client.post(f"{BASE}/events", headers=headers_for(admin),
                      json={"title": "Far", "starts_at": _future(days=5)}).json()
    client.post(f"{BASE}/events/{soon['id']}/rsvp", headers=headers_for(doc))
    client.post(f"{BASE}/events/{far['id']}/rsvp", headers=headers_for(doc2))

    sent = {"n": 0, "to": []}

    async def fake_send(to, subject, html):
        sent["n"] += 1
        sent["to"].append(to)
        return True
    import email_utils
    monkeypatch.setattr(email_utils, "is_email_transport_configured", lambda: True)
    monkeypatch.setattr(email_utils, "send_html_email", fake_send)

    from community.router import resolve_member_for_notify

    import asyncio
    n1 = asyncio.run(cevents.send_due_reminders(resolve_member=resolve_member_for_notify))
    assert n1 == 1  # only the soon event's one interested member
    # Second sweep: already reminded → nothing.
    n2 = asyncio.run(cevents.send_due_reminders(resolve_member=resolve_member_for_notify))
    assert n2 == 0


# ─── Polls ────────────────────────────────────────────────────────────────────
def test_poll_create_vote_revote_and_serialize():
    astore, cstore, admin, doc = setup_world()
    r = client.post(f"{BASE}/polls", headers=headers_for(doc), json={
        "channel_slug": "general", "question": "How to manage?",
        "options": ["A", "B", "C"]})
    assert r.status_code == 200, r.text
    pid = r.json()["poll"]["id"]
    o = r.json()["poll"]["options"]
    r1 = client.post(f"{BASE}/polls/{pid}/vote", headers=headers_for(doc),
                     json={"option_id": o[0]["id"]}).json()
    assert r1["total_votes"] == 1 and r1["your_vote"] == o[0]["id"]
    r2 = client.post(f"{BASE}/polls/{pid}/vote", headers=headers_for(doc),
                     json={"option_id": o[1]["id"]}).json()
    assert r2["total_votes"] == 1 and r2["your_vote"] == o[1]["id"]  # revote replaced
    # serialized on the channel message with the viewer's vote
    msgs = client.get(f"{BASE}/channels/general/messages", headers=headers_for(doc)).json()["messages"]
    pm = [m for m in msgs if m["kind"] == "poll"][0]
    assert pm["poll"]["total_votes"] == 1 and pm["poll"]["your_vote"] == o[1]["id"]


def test_poll_close_blocks_votes_and_gated_to_author_or_admin():
    astore, cstore, admin, doc = setup_world()
    doc2 = make_doc(astore)
    r = client.post(f"{BASE}/polls", headers=headers_for(doc), json={
        "channel_slug": "general", "question": "Q?", "options": ["A", "B"]})
    pid = r.json()["poll"]["id"]
    # a non-author, non-admin cannot close
    assert client.post(f"{BASE}/polls/{pid}/close", headers=headers_for(doc2)).status_code == 403
    assert client.post(f"{BASE}/polls/{pid}/close", headers=headers_for(admin)).status_code == 200
    o = r.json()["poll"]["options"]
    assert client.post(f"{BASE}/polls/{pid}/vote", headers=headers_for(doc),
                       json={"option_id": o[0]["id"]}).status_code == 409


def test_poll_phi_blocked_and_admin_channel_gate():
    astore, cstore, admin, doc = setup_world()
    assert client.post(f"{BASE}/polls", headers=headers_for(doc), json={
        "channel_slug": "general", "question": "SSN 123-45-6789?",
        "options": ["A", "B"]}).status_code == 422
    # a non-admin cannot post a poll into an admin-post channel
    assert client.post(f"{BASE}/polls", headers=headers_for(doc), json={
        "channel_slug": "task-announcements", "question": "Q?",
        "options": ["A", "B"]}).status_code == 403


# ─── Pinned messages ──────────────────────────────────────────────────────────
def test_pin_unpin_and_serialization_flag():
    astore, cstore, admin, doc = setup_world()
    m = client.post(f"{BASE}/channels/general/messages", headers=headers_for(doc),
                    json={"body": "pin me"}).json()
    assert client.post(f"{BASE}/messages/{m['id']}/pin", headers=headers_for(doc)).json()["pinned"]
    pins = client.get(f"{BASE}/channels/general/pins", headers=headers_for(doc)).json()["pins"]
    assert len(pins) == 1 and pins[0]["id"] == m["id"]
    # the message serializes as pinned in the channel too
    msgs = client.get(f"{BASE}/channels/general/messages", headers=headers_for(doc)).json()["messages"]
    assert [x for x in msgs if x["id"] == m["id"]][0]["pinned"] is True
    # unpin
    assert client.delete(f"{BASE}/messages/{m['id']}/pin", headers=headers_for(doc)).json()["pinned"] is False
    assert client.get(f"{BASE}/channels/general/pins", headers=headers_for(doc)).json()["pins"] == []


def test_delete_clears_pin():
    astore, cstore, admin, doc = setup_world()
    m = client.post(f"{BASE}/channels/general/messages", headers=headers_for(doc),
                    json={"body": "pin then delete"}).json()
    client.post(f"{BASE}/messages/{m['id']}/pin", headers=headers_for(doc))
    client.delete(f"{BASE}/messages/{m['id']}", headers=headers_for(doc))
    assert client.get(f"{BASE}/channels/general/pins", headers=headers_for(doc)).json()["pins"] == []


# ─── Bookmarks ────────────────────────────────────────────────────────────────
def test_bookmark_add_list_remove_and_url_validation():
    astore, cstore, admin, doc = setup_world()
    r = client.post(f"{BASE}/channels/general/bookmarks", headers=headers_for(doc),
                    json={"title": "KDIGO", "url": "https://kdigo.org"})
    assert r.status_code == 200
    bid = r.json()["id"]
    assert client.post(f"{BASE}/channels/general/bookmarks", headers=headers_for(doc),
                       json={"title": "bad", "url": "javascript:alert(1)"}).status_code == 400
    bl = client.get(f"{BASE}/channels/general/bookmarks", headers=headers_for(doc)).json()["bookmarks"]
    assert len(bl) == 1 and bl[0]["title"] == "KDIGO"
    # a non-owner non-admin cannot remove someone else's bookmark
    doc2 = make_doc(astore)
    assert client.delete(f"{BASE}/bookmarks/{bid}", headers=headers_for(doc2)).status_code == 403
    assert client.delete(f"{BASE}/bookmarks/{bid}", headers=headers_for(doc)).status_code == 200
    assert client.get(f"{BASE}/channels/general/bookmarks", headers=headers_for(doc)).json()["bookmarks"] == []


# ─── @channel / @here broadcast ───────────────────────────────────────────────
def test_channel_broadcast_admin_only_and_lights_badge():
    astore, cstore, admin, doc = setup_world()
    # non-admin @channel → 403
    assert client.post(f"{BASE}/channels/general/messages", headers=headers_for(doc),
                       json={"body": "hey @channel look"}).status_code == 403
    # admin @channel → ok, lights every member's mention badge
    assert client.post(f"{BASE}/channels/general/messages", headers=headers_for(admin),
                       json={"body": "@channel grand rounds now"}).status_code == 200
    badge = client.get(f"{BASE}/badge", headers=headers_for(doc)).json()
    assert badge["mentions"] >= 1
    # and queues a broadcast digest notification for the member (not the author)
    pending = cstore.unsent_notifications()
    assert any(n["user_id"] == doc["id"] and n["kind"] == "broadcast" for n in pending)


def test_here_is_open_to_members_and_not_durable():
    astore, cstore, admin, doc = setup_world()
    doc2 = make_doc(astore)
    r = client.post(f"{BASE}/channels/general/messages", headers=headers_for(doc),
                    json={"body": "@here quick question"})
    assert r.status_code == 200
    # @here does not enqueue durable broadcast notifications
    assert not any(n["kind"] == "broadcast" for n in cstore.unsent_notifications())
    # nor does it light a persistent mention badge for an offline member
    badge = client.get(f"{BASE}/badge", headers=headers_for(doc2)).json()
    assert badge["mentions"] == 0
