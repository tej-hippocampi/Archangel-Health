"""The calendar hook, and the line drawn through it.

The founders asked for the scheduling routine to live in the product. What ships
is the part that is real without credentials nobody has set up: the booking
signal Calendly already produces, folded into the funnel so a booked call
appears without anyone typing it.

The test that matters most here is the one asserting what this CANNOT do. No
calendar knows whether a conversation happened, and the one outcome that mails a
physician has to stay a human assertion.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sys
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import calendar_sync as CS  # noqa: E402
from asclepius import intro_meeting as IM  # noqa: E402

client = TestClient(A.app)

_KEY = "a-test-signing-key"
_WEBHOOK = "/api/onboarding/asclepius/calendar-webhook"


@pytest.fixture()
def env(monkeypatch):
    store = A.fresh_store()
    monkeypatch.setattr(A.app.state, "asclepius_store", store, raising=False)
    monkeypatch.setenv("ASCLEPIUS_CALENDAR_SYNC", "1")
    monkeypatch.setenv("CALENDLY_WEBHOOK_SIGNING_KEY", _KEY)
    return {"store": store}


def _event(kind: str, *, uri: str, email: str = "ada@example.com",
           name: str = "Ada Lovelace", start: str = "2026-09-10T17:00:00Z") -> dict:
    return {"event": kind, "payload": {
        "uri": uri, "email": email, "name": name,
        "scheduled_event": {"start_time": start}}}


def _post(event: dict, *, key: str = _KEY, at: float = None):
    raw = json.dumps(event).encode("utf-8")
    ts = str(int(at if at is not None else time.time()))
    sig = hmac.new(key.encode("utf-8"), f"{ts}.".encode("utf-8") + raw,
                   hashlib.sha256).hexdigest()
    return client.post(_WEBHOOK, content=raw, headers={
        "Content-Type": "application/json",
        "Calendly-Webhook-Signature": f"t={ts},v1={sig}"})


# ─── The line ────────────────────────────────────────────────────────────────

def test_no_calendar_event_can_mark_a_meeting_held(env):
    """The whole safety property. A calendar knows an event existed, not that a
    conversation happened, and 'held' is the outcome that mails a physician."""
    store = env["store"]
    ref = f"https://api.calendly.com/invitees/{uuid.uuid4()}"
    assert _post(_event(CS.INVITEE_CREATED, uri=ref)).status_code == 200
    meeting = store.intro_meeting_by_booking_ref(ref)
    assert meeting["status"] == IM.SCHEDULED
    # Every event this module handles, and none of them reaches 'held'.
    for kind in CS.HANDLED_EVENTS:
        _post(_event(kind, uri=ref))
    assert store.intro_meeting_by_booking_ref(ref)["status"] != IM.HELD


def test_a_synced_meeting_sends_nothing_on_its_own(env):
    """A booking is not a conversation. The follow-up waits for a person."""
    store = env["store"]
    ref = f"https://api.calendly.com/invitees/{uuid.uuid4()}"
    _post(_event(CS.INVITEE_CREATED, uri=ref, email="quiet@example.com"))
    queued = [n for n in store.due_admin_notifications(limit=200)
              if n["kind"] == "intro_followup"]
    assert queued == []


# ─── What it does do ─────────────────────────────────────────────────────────

def test_a_booking_becomes_a_scheduled_meeting(env):
    """The point: a founder should not have to retype into the product what they
    just took in the scheduler."""
    store = env["store"]
    ref = f"https://api.calendly.com/invitees/{uuid.uuid4()}"
    r = _post(_event(CS.INVITEE_CREATED, uri=ref))
    assert r.status_code == 200, r.text
    assert r.json()["action"] == "scheduled"
    meeting = store.intro_meeting_by_booking_ref(ref)
    assert meeting["email"] == "ada@example.com"
    assert meeting["full_name"] == "Ada Lovelace"
    assert meeting["scheduled_at"] == "2026-09-10T17:00:00Z"


def test_the_same_booking_delivered_twice_makes_one_meeting(env):
    """Webhooks retry. A duplicate booking would put the same physician on the
    founders' screen twice and let one row's outcome contradict the other."""
    store = env["store"]
    ref = f"https://api.calendly.com/invitees/{uuid.uuid4()}"
    _post(_event(CS.INVITEE_CREATED, uri=ref))
    second = _post(_event(CS.INVITEE_CREATED, uri=ref))
    assert second.json()["action"] == "already_known"
    assert len(store.list_intro_meetings()) == 1


def test_a_cancellation_closes_the_meeting_it_belongs_to(env):
    store = env["store"]
    ref = f"https://api.calendly.com/invitees/{uuid.uuid4()}"
    _post(_event(CS.INVITEE_CREATED, uri=ref))
    r = _post(_event(CS.INVITEE_CANCELED, uri=ref))
    assert r.json()["action"] == IM.CANCELLED
    assert store.intro_meeting_by_booking_ref(ref)["status"] == IM.CANCELLED


def test_a_no_show_is_recorded_because_a_person_marked_it(env):
    """Calendly's no-show is a human assertion, not an inference, which is why
    it is the one negative outcome worth taking from a calendar."""
    store = env["store"]
    ref = f"https://api.calendly.com/invitees/{uuid.uuid4()}"
    _post(_event(CS.INVITEE_CREATED, uri=ref))
    r = _post(_event(CS.INVITEE_NO_SHOW, uri=ref))
    assert r.json()["action"] == IM.NO_SHOW
    assert store.intro_meeting_by_booking_ref(ref)["status"] == IM.NO_SHOW


def test_an_outcome_for_a_booking_we_never_saw_invents_nothing(env):
    """A row that opens in an outcome is a row the founders cannot act on, and
    it would be indistinguishable from a real cancelled call."""
    store = env["store"]
    r = _post(_event(CS.INVITEE_CANCELED, uri="https://api.calendly.com/invitees/ghost"))
    assert r.json()["action"] == "unknown_booking"
    assert store.list_intro_meetings() == []


def test_an_event_type_we_do_not_handle_is_accepted_and_ignored(env):
    """A subscription that 400s on an event we do not care about is one Calendly
    eventually disables, taking the two we do care about with it."""
    r = _post({"event": "routing_form_submission.created", "payload": {"uri": "x"}})
    assert r.status_code == 200, r.text
    assert r.json()["action"] == "ignored"


# ─── The two switches ────────────────────────────────────────────────────────

def test_the_endpoint_does_not_exist_until_it_is_turned_on(monkeypatch):
    """Default off. An endpoint that writes rows from the open internet is not
    something to ship on by accident."""
    A.fresh_store()
    monkeypatch.delenv("ASCLEPIUS_CALENDAR_SYNC", raising=False)
    monkeypatch.setenv("CALENDLY_WEBHOOK_SIGNING_KEY", _KEY)
    ref = "https://api.calendly.com/invitees/off"
    assert _post(_event(CS.INVITEE_CREATED, uri=ref)).status_code == 404


def test_the_flag_alone_is_not_enough(monkeypatch):
    """A flag with no signing key would accept unsigned writes, which is worse
    than the integration not existing."""
    monkeypatch.setenv("ASCLEPIUS_CALENDAR_SYNC", "1")
    monkeypatch.delenv("CALENDLY_WEBHOOK_SIGNING_KEY", raising=False)
    assert CS.enabled() is False


def test_an_unsigned_or_wrongly_signed_body_is_refused(env):
    """Anyone could otherwise fill the founders' screen with fabricated
    physicians and fabricated no-shows."""
    ref = "https://api.calendly.com/invitees/forged"
    assert _post(_event(CS.INVITEE_CREATED, uri=ref), key="wrong-key").status_code == 401
    raw = json.dumps(_event(CS.INVITEE_CREATED, uri=ref)).encode("utf-8")
    assert client.post(_WEBHOOK, content=raw).status_code == 401
    assert env["store"].list_intro_meetings() == []


def test_a_captured_request_cannot_be_replayed_later(env):
    """The timestamp is inside the signed material for exactly this reason."""
    ref = "https://api.calendly.com/invitees/replay"
    stale = time.time() - 3600
    assert _post(_event(CS.INVITEE_CREATED, uri=ref), at=stale).status_code == 401
    assert env["store"].list_intro_meetings() == []


def test_a_body_that_is_not_json_is_refused_after_the_signature(env):
    """Signature first, parse second: an unauthenticated caller should not be
    able to reach the parser at all."""
    raw = b"not json at all"
    ts = str(int(time.time()))
    sig = hmac.new(_KEY.encode("utf-8"), f"{ts}.".encode("utf-8") + raw,
                   hashlib.sha256).hexdigest()
    r = client.post(_WEBHOOK, content=raw,
                    headers={"Calendly-Webhook-Signature": f"t={ts},v1={sig}"})
    assert r.status_code == 422
