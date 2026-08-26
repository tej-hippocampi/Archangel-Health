"""The daily digest actually reaches people, and can be stopped in one click.

The pipeline already existed: fetch, filter, dedupe, two model passes, one
system post. It shipped switched OFF, and it posted in-app only, so the daily
reason to come back never left the building.

What is worth pinning is not "an email was composed" but the consent hygiene
around a daily send, because getting that wrong costs the sending domain that
every other physician's mail goes through.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests._asclepius import app, fresh_store, headers_for, make_user

from community import store as cstore_mod
from community.store import get_community_store

import onboarding_emails as oe

BASE = "/api/community"


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


# ─── Preferences ─────────────────────────────────────────────────────────────

def test_a_member_defaults_to_daily_and_gets_a_token_immediately():
    """The token has to exist the moment anyone could receive mail carrying it,
    so the row is written on first read rather than on first change."""
    cstore = get_community_store()
    prefs = cstore.email_prefs("u-defaults")
    assert prefs["news_frequency"] == cstore_mod.DEFAULT_NEWS_FREQUENCY == "daily"
    assert len(prefs["unsubscribe_token"]) > 20


def test_absence_of_a_row_is_never_read_as_opted_out():
    cstore = get_community_store()
    assert cstore.email_prefs("u-never-asked")["news_frequency"] == "daily"


def test_frequency_can_be_changed_and_rejects_anything_else():
    cstore = get_community_store()
    assert cstore.set_news_frequency("u-freq", "weekly")["news_frequency"] == "weekly"
    assert cstore.set_news_frequency("u-freq", "off")["news_frequency"] == "off"
    with pytest.raises(ValueError):
        cstore.set_news_frequency("u-freq", "hourly")


# ─── Unsubscribe ─────────────────────────────────────────────────────────────

def test_one_click_unsubscribe_needs_no_session(client):
    """An unsubscribe link that makes someone sign in first is one that gets a
    spam complaint instead."""
    cstore = get_community_store()
    token = cstore.email_prefs("u-unsub")["unsubscribe_token"]

    res = client.get(f"{BASE}/unsubscribe?token={token}")
    assert res.status_code == 200
    assert cstore.email_prefs("u-unsub")["news_frequency"] == "off"


def test_an_unsubscribe_token_can_only_turn_mail_off(client):
    """The worst a leaked token can do is stop an email. It is never a login."""
    cstore = get_community_store()
    token = cstore.email_prefs("u-leaked")["unsubscribe_token"]
    client.get(f"{BASE}/unsubscribe?token={token}")
    # Re-using it cannot flip anything back on, and cannot authenticate.
    client.get(f"{BASE}/unsubscribe?token={token}")
    assert cstore.email_prefs("u-leaked")["news_frequency"] == "off"


def test_a_bad_token_answers_calmly_rather_than_erroring(client):
    res = client.get(f"{BASE}/unsubscribe?token=not-a-real-token")
    assert res.status_code == 200
    assert "not valid" in res.text or "already been used" in res.text


def test_unsubscribing_does_not_remove_anyone_from_the_community(client):
    """Stopping an email is not leaving. The in-app channel stays readable."""
    cstore = get_community_store()
    token = cstore.email_prefs("u-stays")["unsubscribe_token"]
    res = client.get(f"{BASE}/unsubscribe?token={token}")
    assert "still a member" in res.text


# ─── Who a run mails ─────────────────────────────────────────────────────────

def test_a_daily_run_skips_weekly_and_off_members():
    cstore = get_community_store()
    cstore.set_news_frequency("u-d", "daily")
    cstore.set_news_frequency("u-w", "weekly")
    cstore.set_news_frequency("u-o", "off")

    daily = {r["user_id"] for r in cstore.news_email_recipients(weekly=False)}
    weekly = {r["user_id"] for r in cstore.news_email_recipients(weekly=True)}

    assert "u-d" in daily and "u-w" not in daily and "u-o" not in daily
    assert "u-w" in weekly and "u-d" not in weekly and "u-o" not in weekly


# ─── The email itself ────────────────────────────────────────────────────────

def test_the_digest_body_cannot_inject_markup():
    """The body is composed by a model from fetched headlines, so it is
    untrusted text arriving from the open web."""
    html_out = oe.build_community_news_digest_email(
        first_name="Amara",
        headline="<img src=x onerror=alert(1)>",
        body_markdown="- a story about <script>alert(1)</script> tooling",
        community_url="https://x/community",
        unsubscribe_url="https://x/unsub?token=t",
    )
    # The dangerous form is an UNESCAPED tag. The escaped text may legitimately
    # contain the characters, which is what escaping looks like working.
    assert "<script>" not in html_out
    assert "<img" not in html_out
    assert "&lt;script&gt;" in html_out and "&lt;img" in html_out


def test_every_digest_carries_a_working_unsubscribe_in_the_body():
    """Not only in a header. A physician who cannot find how to stop a daily
    email marks it as spam."""
    html_out = oe.build_community_news_digest_email(
        first_name="Amara", headline="Three things",
        body_markdown="- one\n- two",
        community_url="https://x/community",
        unsubscribe_url="https://x/unsub?token=abc123",
    )
    assert "unsub?token=abc123" in html_out


def test_the_digest_uses_the_product_palette():
    html_out = oe.build_community_news_digest_email(
        first_name="A", headline="h", body_markdown="- x",
        community_url="https://x", unsubscribe_url="https://x/u")
    assert "#eef0ef" in html_out and "#67E8F9" not in html_out


# ─── Shipping posture ────────────────────────────────────────────────────────

def test_the_news_digest_is_off_by_default(monkeypatch):
    """The community starts empty by policy: no bot-authored news until the
    dedicated news software replaces this pipeline. Opt in explicitly."""
    from community import digest as d
    monkeypatch.delenv("COMMUNITY_NEWS_ENABLED", raising=False)
    assert d.news_enabled() is False
    monkeypatch.setenv("COMMUNITY_NEWS_ENABLED", "1")
    assert d.news_enabled() is True
    monkeypatch.setenv("COMMUNITY_NEWS_ENABLED", "0")
    assert d.news_enabled() is False
