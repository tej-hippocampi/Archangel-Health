"""Every link an email carries must resolve, and every stop button must stop.

Two live defects motivated this file.

``digest.py`` built the unsubscribe link as ``/community/unsubscribe`` while the
only route is ``/api/community/unsubscribe`` (``community.router`` is mounted at
``prefix="/api/community"``; the page router serves ``/community/unread`` and
``/community/handoff`` and nothing else). Every news-digest unsubscribe link
404'd. ``newsletter.py`` built the same link correctly, which is exactly how the
divergence survived: one of the two worked.

``notify.flush_pending`` mailed every user with queued rows without ever reading
``email_prefs``, and the mail it sent carried no unsubscribe link at all. A
physician who pressed unsubscribe kept receiving mention, DM, broadcast and
announcement digests every five minutes.

The first test below is the one that matters: it asserts the URL against the
app's own routing table rather than against a second copy of the string, so a
prefix change moves the test and a typo fails it.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from tests._asclepius import app

from community import links, notify
from community.store import get_community_store

import onboarding_emails as oe


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _uid(tag: str) -> str:
    """A member id nothing else has touched.

    The suite's community DB lives at a fixed temp path and OUTLIVES the run
    (tests/conftest.py points COMMUNITY_DB_PATH at /tmp/asclepius_suite). A
    fixed id like uid_stop_all is therefore pre-unsubscribed on the second run
    of the file, and the test passes once and then fails forever.
    """
    return f"u-{tag}-{uuid.uuid4().hex[:8]}"


@pytest.fixture()
def origin(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://portal.example.org")
    return "https://portal.example.org"


# ─── The link resolves ───────────────────────────────────────────────────────

def test_the_unsubscribe_url_points_at_a_path_the_app_actually_serves(origin):
    """Asserted against the mounted routes, not against a second copy of the
    string. This is the test that would have caught the 404."""
    url = links.unsubscribe_url("tok-123")
    path = url.split("?")[0][len(origin):]
    served = {getattr(r, "path", None) for r in app.routes}
    assert path in served, f"{path} is not a route this app serves"


def test_the_unsubscribe_link_answers_without_a_session(client, origin):
    """An unsubscribe that demands a login is an unsubscribe that gets reported
    as spam instead."""
    uid_link_live = _uid("link-live")
    cstore = get_community_store()
    prefs = cstore.email_prefs(uid_link_live)
    url = links.unsubscribe_url(prefs["unsubscribe_token"])
    resp = client.get(url[len(origin):])
    assert resp.status_code == 200


def test_both_senders_build_the_identical_link(origin):
    """digest.py and newsletter.py disagreed for months. They now read one
    definition, so they cannot drift again without this failing."""
    import community.digest  # noqa: F401 — imported for the module-level binding
    import community.newsletter  # noqa: F401

    assert community.digest.links is links
    assert community.newsletter.links is links


def test_no_origin_means_no_link_rather_than_a_relative_one(monkeypatch):
    """A relative href in an email client resolves against the mail host and
    lands nowhere. Empty is honest; relative is a broken link that looks live."""
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("BASE_URL", raising=False)
    assert links.unsubscribe_url("tok") == ""
    assert links.community_url() == ""


def test_a_member_without_a_token_gets_no_link(origin):
    assert links.unsubscribe_url("") == ""
    assert links.unsubscribe_url("   ") == ""


# ─── One click stops everything ──────────────────────────────────────────────

def test_unsubscribing_stops_activity_mail_too_not_only_news():
    """The button said "stop these". If the five-minute activity digest kept
    arriving afterwards, the button was a lie and the next click is the spam
    button."""
    uid_stop_all = _uid("stop-all")
    cstore = get_community_store()
    prefs = cstore.email_prefs(uid_stop_all)
    assert cstore.wants_activity_email(uid_stop_all) is True

    cstore.unsubscribe_by_token(prefs["unsubscribe_token"])

    after = cstore.email_prefs(uid_stop_all)
    assert after["news_frequency"] == "off"
    assert cstore.wants_activity_email(uid_stop_all) is False


def test_activity_mail_is_a_separate_switch_from_the_news_cadence():
    """A physician who wants less news still wants to know they were
    @mentioned, so turning news off by itself must not silence mentions."""
    uid_two_switches = _uid("two-switches")
    cstore = get_community_store()
    cstore.email_prefs(uid_two_switches)
    cstore.set_news_frequency(uid_two_switches, "off")
    assert cstore.wants_activity_email(uid_two_switches) is True

    cstore.set_activity_emails(uid_two_switches, False)
    assert cstore.wants_activity_email(uid_two_switches) is False


def test_absence_of_a_row_is_not_an_opt_out():
    """Prefs rows are written lazily. A member who has never been emailed has
    not opted out of anything."""
    uid_never_seen = _uid("never-seen")
    assert get_community_store().wants_activity_email(uid_never_seen) is True


# ─── The flush honours the opt-out ───────────────────────────────────────────

def test_the_flush_does_not_mail_a_member_who_opted_out(monkeypatch):
    uid_quiet = _uid("quiet")
    cstore = get_community_store()
    ch = cstore.get_channel_by_slug("general")
    msg = cstore.insert_message(
        channel_id=ch["id"], author_user_id="u-author", body="a mention for you",
        mentions=[uid_quiet],
    )
    cstore.enqueue_notification(user_id=uid_quiet, kind="mention", message_id=msg["id"])
    cstore.email_prefs(uid_quiet)
    cstore.set_activity_emails(uid_quiet, False)

    sent: list = []

    async def _capture(to, subject, body):
        sent.append(to)
        return True

    monkeypatch.setattr("email_utils.send_html_email", _capture)

    n = asyncio.run(notify.flush_pending(
        cstore, resolve_member=lambda uid: {"email": f"{uid}@example.org", "display_name": uid}
    ))
    assert n == 0
    assert sent == []


def test_an_opted_out_members_rows_are_marked_handled_not_left_pending(monkeypatch):
    """Left pending, the queue grows forever and every later flush re-reads
    them. The in-app notification is unaffected; only the email stops."""
    uid_quiet2 = _uid("quiet2")
    cstore = get_community_store()
    ch = cstore.get_channel_by_slug("general")
    msg = cstore.insert_message(
        channel_id=ch["id"], author_user_id="u-author2", body="another mention",
        mentions=[uid_quiet2],
    )
    cstore.enqueue_notification(user_id=uid_quiet2, kind="mention", message_id=msg["id"])
    cstore.email_prefs(uid_quiet2)
    cstore.set_activity_emails(uid_quiet2, False)

    async def _noop(to, subject, body):
        return True

    monkeypatch.setattr("email_utils.send_html_email", _noop)
    asyncio.run(notify.flush_pending(
        cstore, resolve_member=lambda uid: {"email": f"{uid}@example.org", "display_name": uid}
    ))
    still_pending = [n for n in cstore.unsent_notifications() if n["user_id"] == uid_quiet2]
    assert still_pending == []


def test_a_subscribed_member_still_gets_the_digest(monkeypatch, origin):
    uid_loud = _uid("loud")
    cstore = get_community_store()
    ch = cstore.get_channel_by_slug("general")
    msg = cstore.insert_message(
        channel_id=ch["id"], author_user_id="u-author3", body="hello there",
        mentions=[uid_loud],
    )
    cstore.enqueue_notification(user_id=uid_loud, kind="mention", message_id=msg["id"])

    bodies: list = []

    async def _capture(to, subject, body):
        bodies.append(body)
        return True

    monkeypatch.setattr("email_utils.send_html_email", _capture)
    n = asyncio.run(notify.flush_pending(
        cstore, resolve_member=lambda uid: {"email": f"{uid}@example.org", "display_name": uid}
    ))
    assert n == 1
    assert len(bodies) == 1


# ─── The mail carries the way out ────────────────────────────────────────────

def test_the_activity_digest_email_carries_an_unsubscribe_link():
    """It carried none at all, which is the part that makes the five-minute
    cadence a trust problem rather than a preference problem."""
    html = oe.build_community_digest_email(
        activity_items=[("Dr Chen mentioned you", "the troponin thread")],
        community_url="https://portal.example.org/community",
        unsubscribe_url="https://portal.example.org/api/community/unsubscribe?token=abc",
    )
    assert "/api/community/unsubscribe?token=abc" in html


def test_the_digest_email_omits_the_sentence_when_there_is_no_link():
    """Better no sentence than a dead one."""
    html = oe.build_community_digest_email(
        activity_items=[("Dr Chen mentioned you", "the troponin thread")],
        community_url="",
        unsubscribe_url="",
    )
    assert "unsubscribe" not in html.lower()
