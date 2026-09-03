"""The founders' intro call, and the follow-up a held one sends.

The funnel the founders described is outreach, then a call one of them takes by
hand, then an onboarding link plus a one-pager, then the application. Every
stage of that had state in the product except the call, so the highest
converting email in the whole funnel was written by hand or not at all.

The properties these tests hold are the ones that decide whether this can be
trusted to mail physicians:

  * a held meeting sends exactly one email, whatever the founder clicks;
  * a no-show sends nothing, and neither does a meeting nobody has judged yet;
  * the email carries BOTH the application link and the one-pager, because an
    email missing one of them still looks like a perfectly good email.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import intro_meeting as IM  # noqa: E402
from team_store import TeamStore  # noqa: E402

client = TestClient(A.app)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """A fresh Asclepius store plus a throwaway tenant store, both bound to the
    real app: the follow-up mints its link out of one and queues its mail in the
    other, so a test that stubs either would not be testing the flow."""
    store = A.fresh_store()
    team = TeamStore(db_path=str(tmp_path / f"team_{uuid.uuid4().hex[:8]}.db"))
    monkeypatch.setattr(A.app.state, "team_store", team, raising=False)
    monkeypatch.setattr(A.app.state, "asclepius_store", store, raising=False)
    monkeypatch.setenv("LANDING_URL", "https://landing.test")
    monkeypatch.setenv("ASCLEPIUS_PORTAL_URL", "https://api.test")
    admin = A.make_user(store, role="admin")
    return {"store": store, "team": team, "headers": A.headers_for(admin)}


def _log_meeting(env, email: str, **kw):
    r = client.post("/api/asclepius/admin/intro-meetings",
                    json={"email": email, "full_name": kw.pop("full_name", "Ada Lovelace"),
                          **kw}, headers=env["headers"])
    assert r.status_code == 200, r.text
    return r.json()["meeting"]


def _outcome(env, meeting_id: str, outcome: str):
    return client.post(f"/api/asclepius/admin/intro-meetings/{meeting_id}/outcome",
                       json={"outcome": outcome}, headers=env["headers"])


def _queued(store, email: str):
    """Every follow-up sitting in the durable outbox for this address."""
    return [n for n in store.due_admin_notifications(limit=200)
            if n["kind"] == "intro_followup" and n["recipient_email"] == email]


# ─── The state ───────────────────────────────────────────────────────────────

def test_a_logged_meeting_starts_unknown_and_sends_nothing(env):
    """A meeting nobody has judged yet is not a meeting that happened. Logging
    one must never mail the physician on its own."""
    email = f"{A.uniq()}@example.com"
    meeting = _log_meeting(env, email)
    assert meeting["status"] == IM.SCHEDULED
    assert meeting["followup_sent"] is False
    assert _queued(env["store"], email) == []


def test_the_product_records_the_meeting_not_just_a_calendar(env):
    """The ask was that the intro call be a first-class thing the product knows
    about. The booking reference is kept so a real calendar integration has
    something to attach to later."""
    email = f"{A.uniq()}@example.com"
    meeting = _log_meeting(env, email, scheduled_at="2026-09-10T17:00:00Z",
                           booking_ref="https://calendly.com/events/abc123",
                           specialty="nephrology", organization="Ada Health")
    assert meeting["scheduled_at"] == "2026-09-10T17:00:00Z"
    assert meeting["booking_ref"] == "https://calendly.com/events/abc123"
    listed = client.get("/api/asclepius/admin/intro-meetings",
                        headers=env["headers"]).json()
    assert listed["counts"]["scheduled"] == 1
    assert listed["booking_url"], "the console must be able to show the live booking link"


def test_the_booking_link_comes_from_config(monkeypatch):
    """A booking link only a deploy can change is a link that stays wrong for as
    long as the next deploy takes."""
    monkeypatch.setenv("ASCLEPIUS_INTRO_BOOKING_URL", "https://cal.test/intro")
    assert IM.booking_url() == "https://cal.test/intro"
    monkeypatch.delenv("ASCLEPIUS_INTRO_BOOKING_URL")
    from onboarding_emails import FOUNDER_INTRO_CALENDLY
    assert IM.booking_url() == FOUNDER_INTRO_CALENDLY


# ─── The send ────────────────────────────────────────────────────────────────

def test_marking_a_meeting_held_sends_the_follow_up(env):
    """The whole feature: a founder marks the call done and the physician gets
    their link without anyone writing an email."""
    email = f"{A.uniq()}@example.com"
    meeting = _log_meeting(env, email)
    r = _outcome(env, meeting["meeting_id"], IM.HELD)
    assert r.status_code == 200, r.text
    assert r.json()["followup_queued"] is True
    assert r.json()["meeting"]["status"] == IM.HELD
    assert len(_queued(env["store"], email)) == 1


def test_the_follow_up_carries_both_the_link_and_the_one_pager(env):
    """The meeting treated these as one delivery. An email missing either of
    them still looks like a perfectly good email, which is why this is asserted
    on the queued body rather than trusted to the template."""
    email = f"{A.uniq()}@example.com"
    meeting = _log_meeting(env, email)
    assert _outcome(env, meeting["meeting_id"], IM.HELD).status_code == 200
    body = _queued(env["store"], email)[0]["body_html"]
    row = env["store"].get_intro_meeting(meeting["meeting_id"])
    assert row["onboarding_url"], "a held meeting must have minted an application link"
    assert row["onboarding_url"] in body
    assert "/api/onboarding/asclepius/one-pager.pdf" in body


def test_the_application_link_actually_opens_the_wizard(env):
    """A link that looks right in the admin toast and 404s in the physician's
    inbox is the specific failure _landing_base exists to prevent."""
    email = f"{A.uniq()}@example.com"
    meeting = _log_meeting(env, email)
    assert _outcome(env, meeting["meeting_id"], IM.HELD).status_code == 200
    url = env["store"].get_intro_meeting(meeting["meeting_id"])["onboarding_url"]
    assert url.startswith("https://landing.test/onboard/")
    token = url.rsplit("/", 1)[-1]
    r = client.get("/api/onboarding/session", params={"token": token})
    assert r.status_code == 200, r.text


# ─── Idempotency ─────────────────────────────────────────────────────────────

def test_marking_held_twice_sends_once(env):
    """A double click on a founder's laptop must not mail a physician twice. The
    outbox key is derived from the meeting, so the second row is dropped."""
    email = f"{A.uniq()}@example.com"
    meeting = _log_meeting(env, email)
    first = _outcome(env, meeting["meeting_id"], IM.HELD)
    second = _outcome(env, meeting["meeting_id"], IM.HELD)
    assert first.status_code == 200 and second.status_code == 200, second.text
    assert first.json()["followup_queued"] is True
    assert second.json()["followup_queued"] is False
    assert len(_queued(env["store"], email)) == 1


def test_a_second_mark_held_reuses_the_same_link(env):
    """Two links for one physician is two funnels, and the one they click is
    whichever email they happen to open."""
    email = f"{A.uniq()}@example.com"
    meeting = _log_meeting(env, email)
    _outcome(env, meeting["meeting_id"], IM.HELD)
    url = env["store"].get_intro_meeting(meeting["meeting_id"])["onboarding_url"]
    _outcome(env, meeting["meeting_id"], IM.HELD)
    assert env["store"].get_intro_meeting(meeting["meeting_id"])["onboarding_url"] == url


def test_the_send_stamp_records_when_they_were_written_to(env):
    """It is the evidence of how long a physician waited between the call and
    the link, so a retry must not overwrite it."""
    email = f"{A.uniq()}@example.com"
    meeting = _log_meeting(env, email)
    _outcome(env, meeting["meeting_id"], IM.HELD)
    first = env["store"].get_intro_meeting(meeting["meeting_id"])["followup_queued_at"]
    assert first
    _outcome(env, meeting["meeting_id"], IM.HELD)
    assert env["store"].get_intro_meeting(meeting["meeting_id"])["followup_queued_at"] == first


# ─── The outcomes that do not send ───────────────────────────────────────────

def test_a_no_show_sends_nothing(env):
    """The failure this exists to prevent: "great speaking with you" to somebody
    who never joined the call."""
    email = f"{A.uniq()}@example.com"
    meeting = _log_meeting(env, email)
    r = _outcome(env, meeting["meeting_id"], IM.NO_SHOW)
    assert r.status_code == 200, r.text
    assert r.json()["followup_queued"] is False
    assert r.json()["meeting"]["status"] == IM.NO_SHOW
    assert _queued(env["store"], email) == []


def test_a_cancelled_meeting_sends_nothing(env):
    """A call that never happened at all is representable, and representable
    means the outcome is recorded rather than left to be guessed."""
    email = f"{A.uniq()}@example.com"
    meeting = _log_meeting(env, email)
    assert _outcome(env, meeting["meeting_id"], IM.CANCELLED).json()["followup_queued"] is False
    assert _queued(env["store"], email) == []


def test_a_no_show_can_be_corrected_to_held(env):
    """They joined nine minutes late. Correcting that should not need a second
    meeting row, and it should send the follow-up they are owed."""
    email = f"{A.uniq()}@example.com"
    meeting = _log_meeting(env, email)
    _outcome(env, meeting["meeting_id"], IM.NO_SHOW)
    r = _outcome(env, meeting["meeting_id"], IM.HELD)
    assert r.status_code == 200, r.text
    assert r.json()["followup_queued"] is True
    assert len(_queued(env["store"], email)) == 1


def test_held_cannot_be_walked_back(env):
    """The follow-up has already left the building. A state machine that lets
    you mark a held meeting a no-show is lying to whoever reads it next."""
    email = f"{A.uniq()}@example.com"
    meeting = _log_meeting(env, email)
    _outcome(env, meeting["meeting_id"], IM.HELD)
    r = _outcome(env, meeting["meeting_id"], IM.NO_SHOW)
    assert r.status_code == 409, r.text
    assert env["store"].get_intro_meeting(meeting["meeting_id"])["status"] == IM.HELD


def test_an_unknown_outcome_is_refused(env):
    """Not a Literal on the request model, so the transition table and the API
    cannot drift. That makes this the test holding the API honest."""
    email = f"{A.uniq()}@example.com"
    meeting = _log_meeting(env, email)
    assert _outcome(env, meeting["meeting_id"], "attended").status_code == 422
    assert _outcome(env, meeting["meeting_id"], IM.SCHEDULED).status_code == 422
    assert env["store"].get_intro_meeting(meeting["meeting_id"])["status"] == IM.SCHEDULED


def test_a_physician_who_already_has_an_account_is_not_sent_an_application_link(env):
    """Meeting an existing contributor is a normal thing to do. Mailing them an
    application link invites them to start a second funnel."""
    store = env["store"]
    existing = A.make_user(store, role="evaluator")
    meeting = _log_meeting(env, existing["email"])
    r = _outcome(env, meeting["meeting_id"], IM.HELD)
    assert r.status_code == 200, r.text
    assert r.json()["followup_queued"] is False
    assert r.json()["meeting"]["status"] == IM.HELD, "the call still happened"
    assert _queued(store, existing["email"]) == []


# ─── The copy ────────────────────────────────────────────────────────────────

#: The banned glyph, written as an escape so this file can assert its absence
#: without containing one.
_EM_DASH = "\u2014"


def test_the_follow_up_holds_house_style():
    """Same rule the referral email is held to. It is the first thing a
    physician reads from us after meeting us."""
    from onboarding_emails import build_intro_followup_email

    html = build_intro_followup_email(
        full_name="Ada Lovelace", onboarding_url="https://landing.test/onboard/t",
        one_pager_url="https://api.test/api/onboarding/asclepius/one-pager.pdf")
    assert _EM_DASH not in html
    # The entities the atoms escape, printed literally, is a bug caught in
    # review on a sibling builder once already.
    assert "&amp;middot;" not in html
    assert "&amp;rarr;" not in html


def test_the_follow_up_opens_on_the_conversation_not_on_who_we_are():
    """The difference between this and the cold invite. Explaining Archangel to
    somebody we just spent twenty minutes with reads as a mail merge."""
    from onboarding_emails import build_intro_followup_email

    html = build_intro_followup_email(
        full_name="Ada Lovelace", onboarding_url="https://landing.test/onboard/t",
        one_pager_url="https://api.test/one-pager.pdf")
    assert "Great speaking with you, Ada." in html
    assert "You have been invited" not in html


# ─── Access ──────────────────────────────────────────────────────────────────

def test_the_meeting_surface_is_admin_only(env):
    """It mints onboarding links and mails physicians."""
    labeler = A.make_user(env["store"], role="evaluator")
    headers = A.headers_for(labeler)
    assert client.get("/api/asclepius/admin/intro-meetings",
                      headers=headers).status_code in (401, 403)
    assert client.post("/api/asclepius/admin/intro-meetings",
                       json={"email": "x@example.com"},
                       headers=headers).status_code in (401, 403)
