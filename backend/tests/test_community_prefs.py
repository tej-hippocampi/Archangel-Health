"""Email preferences, and a queued email that actually gets a second chance.

Four defects motivated this file, and they compound: together they meant a
physician who wanted less mail had exactly one control, it was too blunt, and
the mail they did want could vanish without a trace.

``community_email_prefs`` carried two knobs, ``news_frequency`` and
``activity_emails``, and ``set_activity_emails`` had no HTTP route and no UI at
all. So the only way to stop community email was the unsubscribe token in a
message footer, and that token turns off EVERYTHING. "Stop telling me about
pins" and "stop telling me a colleague asked me something" had one button
between them.

``notify.flush_pending`` then marked every queued row sent whether or not the
send succeeded, so one transient vendor error silently ate a member's activity
mail and left a queue that looked perfectly healthy afterwards.

The two rules these tests exist to hold:

  * A kinded unsubscribe stops ONE stream. A bare one stops everything, exactly
    as it always has, because links in mail already sent carry no kind and must
    not come to mean less than they said when they were sent.
  * A failed send is retried, and giving up is a decision with a number on it
    rather than an accident.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from tests._asclepius import app, fresh_store, headers_for, uniq

from community import links, notify
from community.store import get_community_store

BASE = "/api/community"


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _uid(tag: str) -> str:
    """A member id nothing else has touched.

    The suite's community DB lives at a fixed temp path and OUTLIVES the run
    (tests/conftest.py points COMMUNITY_DB_PATH at /tmp/asclepius_suite), so a
    fixed id is pre-unsubscribed on the second run of the file and the test
    passes once and then fails forever.
    """
    return f"u-{tag}-{uuid.uuid4().hex[:8]}"


def _verified_physician(store, specialty="nephrology"):
    """A member who passes the §1 gate, so the prefs routes answer them."""
    user = store.create_user(
        email=f"dr-{uniq()}@asclepius.example.com",
        password="pw-12345678",
        role="evaluator",
        specialty=specialty,
        years_experience=12,
        organization="Riverside Nephrology Associates",
    )
    store.upsert_contributor_credentials(
        id_hashed=user["id_hashed"],
        user_id=user["id"],
        organization="Riverside Nephrology Associates",
        role_title="Physician (MD)",
        credentials_verified=True,
        ship={"degree": "MD", "primary_specialty": specialty,
              "years_in_active_practice": 12, "credentials_verified": True},
        verify={"full_legal_name": "Jane A. Doe, MD"},
    )
    return user


# ─── All four knobs, over HTTP ───────────────────────────────────────────────

def test_the_prefs_route_returns_every_switch_the_store_keeps(client):
    """It returned the news cadence alone, which is why the panel could not be
    built: three of the four switches had no way to be read or written."""
    doc = _verified_physician(fresh_store())
    body = client.get(f"{BASE}/prefs", headers=headers_for(doc)).json()
    assert body["news_frequency"] in ("daily", "weekly", "off")
    assert body["activity_emails"] is True
    assert body["post_emails"] is True
    assert body["pin_emails"] is True


@pytest.mark.parametrize("key", ["activity_emails", "post_emails", "pin_emails"])
def test_each_switch_round_trips(client, key):
    doc = _verified_physician(fresh_store())
    headers = headers_for(doc)

    off = client.post(f"{BASE}/prefs", json={key: False}, headers=headers)
    assert off.status_code == 200, off.text
    assert off.json()[key] is False
    assert client.get(f"{BASE}/prefs", headers=headers).json()[key] is False

    client.post(f"{BASE}/prefs", json={key: True}, headers=headers)
    assert client.get(f"{BASE}/prefs", headers=headers).json()[key] is True


def test_the_news_cadence_still_round_trips_and_still_validates(client):
    doc = _verified_physician(fresh_store())
    headers = headers_for(doc)

    assert client.post(f"{BASE}/prefs", json={"news_frequency": "weekly"},
                       headers=headers).json()["news_frequency"] == "weekly"
    bad = client.post(f"{BASE}/prefs", json={"news_frequency": "hourly"},
                      headers=headers)
    assert bad.status_code == 400


def test_writing_one_switch_leaves_the_others_alone(client):
    """The panel toggles one thing at a time. A whole-object write from a tab
    that has been open all afternoon would silently revert a change made in
    another one."""
    doc = _verified_physician(fresh_store())
    headers = headers_for(doc)
    client.post(f"{BASE}/prefs", json={"pin_emails": False}, headers=headers)

    client.post(f"{BASE}/prefs", json={"post_emails": False}, headers=headers)

    after = client.get(f"{BASE}/prefs", headers=headers).json()
    assert after["pin_emails"] is False and after["post_emails"] is False
    assert after["activity_emails"] is True


def test_the_prefs_routes_are_behind_the_gate(client):
    assert client.get(f"{BASE}/prefs").status_code == 401
    assert client.post(f"{BASE}/prefs", json={"pin_emails": False}).status_code == 401


# ─── One click, and how much it stops ────────────────────────────────────────

@pytest.mark.parametrize(
    "kind, column, untouched",
    [
        ("pin", "pin_emails", ("post_emails", "activity_emails")),
        ("post", "post_emails", ("pin_emails", "activity_emails")),
        ("activity", "activity_emails", ("post_emails", "pin_emails")),
    ],
)
def test_a_kinded_unsubscribe_stops_exactly_one_stream(client, kind, column, untouched):
    """The link at the foot of a pin notification should mean "stop telling me
    about pins", not "stop everything", which is the only thing the token could
    say before."""
    cstore = get_community_store()
    uid = _uid(f"kinded-{kind}")
    token = cstore.email_prefs(uid)["unsubscribe_token"]

    resp = client.get(f"{BASE}/unsubscribe", params={"token": token, "kind": kind})
    assert resp.status_code == 200

    prefs = cstore.email_prefs(uid)
    assert int(prefs[column]) == 0
    for other in untouched:
        assert int(prefs[other]) == 1, f"{other} should not have been touched"
    # The news cadence is a cadence, not a boolean, and no kinded link owns it.
    assert prefs["news_frequency"] != "off"


def test_a_bare_unsubscribe_behaves_exactly_as_it_always_has(client):
    """Every unsubscribe link in mail already sent carries no kind. If the bare
    form quietly came to stop less than it said, the button would be a lie in
    precisely the mail that has already gone out."""
    cstore = get_community_store()
    uid = _uid("bare")
    token = cstore.email_prefs(uid)["unsubscribe_token"]

    assert client.get(f"{BASE}/unsubscribe", params={"token": token}).status_code == 200

    prefs = cstore.email_prefs(uid)
    assert prefs["news_frequency"] == "off"
    assert int(prefs["activity_emails"]) == 0
    # The two new streams are non-transactional email too, so "stop these" has
    # to reach them or the button is a lie in a new way.
    assert int(prefs["post_emails"]) == 0
    assert int(prefs["pin_emails"]) == 0


def test_an_unknown_kind_stops_everything_rather_than_nothing(client):
    """The safe failure for a button that says "stop these" is stopping too
    much. Stopping nothing is how a member reaches for the spam button
    instead."""
    cstore = get_community_store()
    uid = _uid("weird-kind")
    token = cstore.email_prefs(uid)["unsubscribe_token"]

    client.get(f"{BASE}/unsubscribe", params={"token": token, "kind": "trumpets"})

    assert cstore.email_prefs(uid)["news_frequency"] == "off"


def test_an_unknown_token_changes_nothing_and_still_answers(client):
    resp = client.get(f"{BASE}/unsubscribe", params={"token": "not-a-token", "kind": "pin"})
    assert resp.status_code == 200
    assert "not valid" in resp.text


def test_the_kinded_link_is_the_ordinary_link_plus_one_parameter(monkeypatch):
    """Built here rather than in the sender, so the two cannot drift: the
    kinded and bare forms have to hit the same route."""
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://portal.example.org")
    bare = links.unsubscribe_url("tok-123")
    kinded = links.unsubscribe_url("tok-123", kind="pin")
    assert kinded == bare + "&kind=pin"
    assert links.unsubscribe_url("tok-123", kind="") == bare


# ─── The flush honours the new switches ──────────────────────────────────────

def _queue(cstore, uid, kind, body="something happened"):
    channel = cstore.get_channel_by_slug("general")
    msg = cstore.insert_message(channel_id=channel["id"],
                                author_user_id=_uid("author"), body=body)
    cstore.enqueue_notification(user_id=uid, kind=kind, message_id=msg["id"])
    return msg


def _resolver(uid):
    return lambda user_id: {"email": f"{user_id}@example.org", "display_name": user_id}


@pytest.mark.parametrize("kind, stream", [("post", "post"), ("pin", "pin")])
def test_the_flush_skips_a_stream_the_member_switched_off(monkeypatch, kind, stream):
    cstore = get_community_store()
    uid = _uid(f"off-{kind}")
    cstore.set_email_stream(uid, stream, False)
    _queue(cstore, uid, kind)

    sent: list = []

    async def _capture(to, subject, body):
        sent.append(to)
        return True

    monkeypatch.setattr("email_utils.send_html_email", _capture)
    asyncio.run(notify.flush_pending(cstore, resolve_member=_resolver(uid)))

    assert f"{uid}@example.org" not in sent
    # Marked handled, not left pending: a dropped row that stays queued is
    # re-read by every later flush, forever.
    assert not [n for n in cstore.unsent_notifications() if n["user_id"] == uid]


def test_a_mail_about_one_stream_offers_a_link_that_stops_only_that_stream(monkeypatch):
    cstore = get_community_store()
    uid = _uid("pin-only")
    _queue(cstore, uid, "pin")

    bodies: list = []

    async def _capture(to, subject, body):
        bodies.append(body)
        return True

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://portal.example.org")
    monkeypatch.setattr("email_utils.send_html_email", _capture)
    asyncio.run(notify.flush_pending(cstore, resolve_member=_resolver(uid)))

    assert bodies
    assert "kind=pin" in bodies[0]


def test_a_mixed_batch_keeps_the_broad_link(monkeypatch):
    """The template takes ONE unsubscribe URL. A link that stopped an arbitrary
    one of the three streams in a mixed mail would be a lie about which button
    the reader pressed, so a mixed batch keeps the honest blunt one."""
    cstore = get_community_store()
    uid = _uid("mixed")
    _queue(cstore, uid, "pin")
    _queue(cstore, uid, "mention")

    bodies: list = []

    async def _capture(to, subject, body):
        bodies.append(body)
        return True

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://portal.example.org")
    monkeypatch.setattr("email_utils.send_html_email", _capture)
    asyncio.run(notify.flush_pending(cstore, resolve_member=_resolver(uid)))

    assert bodies
    assert "unsubscribe?token=" in bodies[0]
    assert "kind=" not in bodies[0]


# ─── A failed send is retried ────────────────────────────────────────────────

def test_a_failed_send_leaves_the_rows_queued_for_the_next_flush(monkeypatch):
    """It marked every row sent whether or not the send worked, so one
    transient vendor error ate a member's mail and the queue looked healthy
    afterwards."""
    cstore = get_community_store()
    uid = _uid("retry")
    _queue(cstore, uid, "mention")

    async def _fail(to, subject, body):
        return False

    monkeypatch.setattr("email_utils.send_html_email", _fail)
    asyncio.run(notify.flush_pending(cstore, resolve_member=_resolver(uid)))

    still = [n for n in cstore.unsent_notifications() if n["user_id"] == uid]
    assert len(still) == 1
    assert still[0]["attempts"] == 1

    delivered: list = []

    async def _ok(to, subject, body):
        delivered.append(to)
        return True

    monkeypatch.setattr("email_utils.send_html_email", _ok)
    asyncio.run(notify.flush_pending(cstore, resolve_member=_resolver(uid)))

    assert f"{uid}@example.org" in delivered
    assert not [n for n in cstore.unsent_notifications() if n["user_id"] == uid]


def test_the_queue_gives_up_after_three_attempts(monkeypatch, caplog):
    """Retrying forever is the other failure: a dead address never clears, and
    the row would outlive the member. Three, and it says so in the log."""
    cstore = get_community_store()
    uid = _uid("giveup")
    _queue(cstore, uid, "mention")

    async def _fail(to, subject, body):
        return False

    monkeypatch.setattr("email_utils.send_html_email", _fail)
    for attempt in (1, 2):
        asyncio.run(notify.flush_pending(cstore, resolve_member=_resolver(uid)))
        rows = [n for n in cstore.unsent_notifications() if n["user_id"] == uid]
        assert len(rows) == 1 and rows[0]["attempts"] == attempt

    with caplog.at_level("ERROR", logger="community.notify"):
        asyncio.run(notify.flush_pending(cstore, resolve_member=_resolver(uid)))

    assert not [n for n in cstore.unsent_notifications() if n["user_id"] == uid], \
        "the queue must stop growing once it has given up"
    assert any("GAVE UP" in r.message for r in caplog.records), \
        "giving up on a member's mail is not something to do quietly"


def test_a_successful_send_never_counts_an_attempt(monkeypatch):
    cstore = get_community_store()
    uid = _uid("clean")
    _queue(cstore, uid, "mention")

    async def _ok(to, subject, body):
        return True

    monkeypatch.setattr("email_utils.send_html_email", _ok)
    assert asyncio.run(notify.flush_pending(cstore, resolve_member=_resolver(uid))) >= 1
    assert not [n for n in cstore.unsent_notifications() if n["user_id"] == uid]
