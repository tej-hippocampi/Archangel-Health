"""What happens to a health system after it submits the /partner form.

The form used to end at a Calendly button on its own success screen. A visitor
who did not click it in that second was gone: nothing had been sent, so there
was no address we had said anything to, nothing to reply to, and nothing to
follow up. The booking moved into an email, and this file holds the three
properties that move buys.

  * THE THANKS IS THE HANDOFF. It carries the only booking link there is now, so
    a submission that stores fine and mails nothing is a lead with no way
    forward. It is best effort in the router for the opposite reason: a mail
    failure must never turn nine answered questions into an error page.
  * THE REMINDER GOES ONCE. Idempotency is a conditional UPDATE, not something a
    scheduler remembers, so a restart cannot double-send and two workers racing
    one row cannot both send.
  * A BOOKED LEAD IS NEVER CHASED. Calendly does not call us back, so an
    operator marks the booking by hand, and what that stamp buys is the silence.

Self-contained, following ``test_leads.py``: the leads router on a throwaway
TeamStore, so none of the app's heavy import chain is needed to exercise the
real store and the real letters.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("EMAIL_DEV_MODE", "1")
os.environ.setdefault("RATE_LIMIT_ENABLED", "0")

import onboarding_emails as oe  # noqa: E402
import routers.leads as leads_mod  # noqa: E402
from asclepius import auth as asc_auth  # noqa: E402
from asclepius import partner_lead_nudge  # noqa: E402
from team_store import TeamStore  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
PARTNER_TSX = REPO / "landing" / "src" / "app" / "components" / "PartnerInterest.tsx"

#: What the form composes, in the shape ``composeMessage`` writes it. The two
#: labelled blocks at the top are what both letters read their greeting out of.
MESSAGE = ("Contact:\nDana Reyes\n\n"
           "Health system:\nSt Mary's Health\n\n"
           "Data they hold:\nEpic, 12 years of nephrology")


@pytest.fixture()
def store(tmp_path):
    return TeamStore(db_path=str(tmp_path / "leads.db"))


@pytest.fixture()
def sent(monkeypatch):
    """Every email this run would have sent, as (to, subject, html)."""
    box = []

    async def _capture(to, subject, html):
        box.append((to, subject, html))
        return True

    monkeypatch.setattr(leads_mod, "send_html_email", _capture)
    monkeypatch.setattr("email_utils.send_html_email", _capture)
    monkeypatch.setattr("email_utils.is_email_transport_configured", lambda: True)
    # The reminder ships OFF (partner_lead_nudge.enabled), because the control
    # that marks a call booked has no screen yet. Every test below is about what
    # the sweep does once it is on, so it is turned on here rather than in
    # fifteen places; the two tests that pin the OFF posture set it themselves.
    monkeypatch.setenv("PARTNER_LEAD_REMINDER_ENABLED", "1")
    return box


@pytest.fixture()
def client(store):
    app = FastAPI()
    app.state.team_store = store
    app.include_router(leads_mod.router)
    app.dependency_overrides[asc_auth.require_admin] = lambda: {"email": "founder@x.org"}
    with TestClient(app) as c:
        yield c


def _submit(client, **overrides):
    body = {
        "source": "health_system_partner",
        "email": "cio@stmarys.org",
        "message": MESSAGE,
    }
    body.update(overrides)
    return client.post("/api/leads", json=body)


def _row(store):
    with store._conn() as conn:
        return dict(conn.execute(
            "SELECT * FROM lead_submissions ORDER BY id DESC LIMIT 1").fetchone())


def _to(sent, address):
    return [m for m in sent if m[0] == address]


def _age_the_thanks(store, lead_id, iso="2020-01-01T00:00:00"):
    """Backdate the thanks so the reminder is due, without waiting three days."""
    with store._conn() as conn:
        conn.execute("UPDATE lead_submissions SET thanks_sent_at = ? WHERE id = ?",
                     (iso, lead_id))


# ─── The thanks ──────────────────────────────────────────────────────────────
def test_a_health_system_is_thanked_and_handed_the_booking_link(client, store, sent):
    """The link that used to be a button on the success screen. It has to be in
    this letter because it is nowhere else: the page deliberately offers no way
    to book, so that the next step is something the visitor can forward."""
    assert _submit(client).status_code == 200
    thanks = _to(sent, "cio@stmarys.org")
    assert len(thanks) == 1
    subject, html = thanks[0][1], thanks[0][2]
    assert subject == "Thank you for submitting"
    assert "Thank you for submitting." in html
    assert oe.PARTNER_BOOKING_CALENDLY in html
    assert "Book a time with us" in html
    # The path, stated plainly, because the call is a gate and not a chat.
    assert "verified" in html
    assert "data licensing agreement" in html


def test_the_letter_greets_the_person_and_names_the_organization(client, store, sent):
    """Both are read back out of the composed message, which is the only place
    the form puts them. A letter addressed to nobody about nothing reads as a
    mailshot, and this one is asking a hospital executive for a meeting."""
    _submit(client)
    html = _to(sent, "cio@stmarys.org")[0][2]
    assert "Dana Reyes" in html
    assert "St Mary" in html


def test_only_the_health_system_form_gets_a_letter_of_its_own(client, store, sent):
    """The other three forms are notes to us. A buyer who asked for a dataset is
    not owed a booking link, and sending one would answer a question they did
    not ask."""
    assert _submit(client, source="request_data",
                   email="buyer@lab.com").status_code == 200
    assert _to(sent, "buyer@lab.com") == []


def test_the_submission_survives_a_mail_failure(client, store, monkeypatch):
    """Best effort, and never allowed to raise. The answers are already stored
    at this point, and an error page here asks a CIO to fill the form in twice.
    """
    async def _boom_on_the_thanks(to, subject, html):
        if subject == "Thank you for submitting":
            raise RuntimeError("SendGrid is having a day")
        return True

    monkeypatch.setattr(leads_mod, "send_html_email", _boom_on_the_thanks)
    r = _submit(client)
    assert r.status_code == 200, r.text
    row = _row(store)
    # Stored, and honestly unstamped: nothing was sent, so nothing may claim it
    # was, and the reminder will not fire off a clock that never started.
    assert row["message"] == MESSAGE
    assert row["thanks_sent_at"] is None


def test_the_stamp_is_only_written_for_a_send_that_happened(client, store, sent):
    """``thanks_sent_at`` is what the reminder's age is measured from. A stamp on
    a letter that never left would start that clock on nothing, and the reminder
    would arrive referring to a message the recipient never had."""
    _submit(client)
    assert _row(store)["thanks_sent_at"] is not None


# ─── The reminder ────────────────────────────────────────────────────────────
def test_one_reminder_goes_out_and_only_one(client, store, sent):
    """The sweep claims the row before it sends, so the second pass finds
    nothing to claim. That is the whole idempotency: not a counter, not
    something the scheduler remembers between restarts."""
    _submit(client)
    lead = _row(store)
    _age_the_thanks(store, lead["id"])

    assert asyncio.run(partner_lead_nudge.sweep(store)) == {"reminder": 1}
    assert asyncio.run(partner_lead_nudge.sweep(store)) == {"reminder": 0}

    reminders = [m for m in _to(sent, "cio@stmarys.org")
                 if m[1].startswith("A time to talk")]
    assert len(reminders) == 1
    html = reminders[0][2]
    assert oe.PARTNER_BOOKING_CALENDLY in html
    assert "Dana" in html
    # It says it is the last one, because it is.
    assert "only reminder" in html


def test_a_lead_that_is_still_fresh_is_left_alone(client, store, sent):
    """Three days, measured from the thanks. A reminder the same afternoon reads
    as a system, not as a person waiting to hear back."""
    _submit(client)
    assert asyncio.run(partner_lead_nudge.sweep(store)) == {"reminder": 0}
    assert _row(store)["reminder_sent_at"] is None


def test_a_booked_lead_is_never_chased(client, store, sent):
    """What the admin button buys. Asking a partner to book a meeting they are
    already coming to is the letter that makes us look like a mailing list."""
    _submit(client)
    lead = _row(store)
    _age_the_thanks(store, lead["id"])
    assert store.mark_lead_call_booked(lead["id"]) is True

    assert asyncio.run(partner_lead_nudge.sweep(store)) == {"reminder": 0}
    assert _row(store)["reminder_sent_at"] is None


def test_a_lead_that_was_never_thanked_is_never_reminded(client, store, sent):
    """A reminder is a reminder OF something. Sending one to somebody whose
    first letter failed would be the only message they ever got, and it reads as
    a follow-up to a conversation that never happened."""
    store.record_lead_submission(
        "health_system_partner", "silent@stmarys.org", MESSAGE)
    assert asyncio.run(partner_lead_nudge.sweep(store)) == {"reminder": 0}
    assert _to(sent, "silent@stmarys.org") == []


def test_nothing_is_claimed_when_there_is_no_transport(client, store, sent,
                                                       monkeypatch):
    """A deployment with no mail transport must not silently burn every lead's
    one reminder. Same rule ``onboarding_nudge`` states: claim nothing, send
    nothing, and come back when there is a way out."""
    _submit(client)
    lead = _row(store)
    _age_the_thanks(store, lead["id"])
    monkeypatch.setattr("email_utils.is_email_transport_configured", lambda: False)

    assert asyncio.run(partner_lead_nudge.sweep(store)) == {"reminder": 0}
    assert _row(store)["reminder_sent_at"] is None


def test_the_other_lead_sources_are_not_swept(client, store, sent):
    """The sweep is scoped to one source. A lab that asked for a dataset has no
    call to book and no reason to hear from a nudge."""
    _submit(client, source="provide_data", email="ops@nephro.org")
    lead = _row(store)
    with store._conn() as conn:
        conn.execute("UPDATE lead_submissions SET thanks_sent_at = ? WHERE id = ?",
                     ("2020-01-01T00:00:00", lead["id"]))
    assert asyncio.run(partner_lead_nudge.sweep(store)) == {"reminder": 0}


# ─── The operator's view ─────────────────────────────────────────────────────
def test_the_admin_can_mark_a_call_booked_and_it_is_idempotent(client, store, sent):
    """Clicking it twice is an operator clicking it twice, and the first time is
    the one that is true."""
    _submit(client)
    lead_id = _row(store)["id"]
    assert client.post(f"/api/leads/admin/{lead_id}/booked").status_code == 200
    first = _row(store)["call_booked_at"]
    assert first is not None
    assert client.post(f"/api/leads/admin/{lead_id}/booked").status_code == 200
    assert _row(store)["call_booked_at"] == first


def test_marking_an_unknown_lead_is_a_404(client, store, sent):
    assert client.post("/api/leads/admin/99999/booked").status_code == 404


def test_the_console_sees_where_every_lead_got_to(client, store, sent):
    """An operator's question about a lead is no longer only what they said, it
    is where it got to. A console that cannot answer the second leaves the
    founder reconstructing it from a sent-mail folder."""
    _submit(client)
    lead = client.get("/api/leads/admin").json()["leads"][0]
    assert lead["thanks_sent_at"] is not None
    assert lead["reminder_sent_at"] is None
    assert lead["call_booked_at"] is None
    assert lead["referred_by"] == ""


# ─── The two ends of the message contract ────────────────────────────────────
def test_the_form_and_the_parser_agree_on_the_two_labels():
    """The name and the organization exist ONLY inside the composed message, and
    the reminder is written days later by a sweep that has nothing but the
    stored row. If the component renames a label, both letters quietly stop
    greeting anyone, which is the kind of failure nobody notices."""
    tsx = PARTNER_TSX.read_text(encoding="utf-8")
    for label in (leads_mod._MESSAGE_NAME_LABEL, leads_mod._MESSAGE_ORG_LABEL):
        assert f'["{label}"' in tsx, f"composeMessage no longer writes {label}"
    assert leads_mod.partner_lead_contact(MESSAGE) == ("Dana Reyes", "St Mary's Health")
    # A message with neither label degrades to empty strings rather than to a
    # greeting made of somebody's prose.
    assert leads_mod.partner_lead_contact("just some words") == ("", "")


def test_the_success_screen_offers_no_way_to_book():
    """A product decision, asserted because it is invisible: the booking lives
    in the email so that it can be forwarded and chased. A button back on this
    page would be a second door, and the second door is the one that stops being
    maintained."""
    tsx = PARTNER_TSX.read_text(encoding="utf-8")
    sent_state = tsx.split("if (sent) {", 1)[1].split("\n  }", 1)[0]
    assert "Thank you for submitting." in sent_state
    assert "calendly" not in sent_state.lower()
    assert "PrimaryButton" not in sent_state


def test_the_reminder_ships_off_because_nothing_can_mark_a_call_booked(monkeypatch):
    """WHY: the reminder is gated on ``call_booked_at``, and the control that
    sets it was going to live on the Systems tab's Partner-leads card, which
    Case Generation Fix PRD §B3 deleted in the same window this was written.

    Shipping it on anyway is the one option nobody chose: every health system
    that had already booked would be asked to book. So the default is off, and
    this pins the default rather than the behaviour, because a default is
    exactly the kind of thing a later reader flips without noticing what it was
    protecting.
    """
    monkeypatch.delenv("PARTNER_LEAD_REMINDER_ENABLED", raising=False)
    assert partner_lead_nudge.enabled() is False
    for on in ("1", "true", "yes", "on"):
        monkeypatch.setenv("PARTNER_LEAD_REMINDER_ENABLED", on)
        assert partner_lead_nudge.enabled() is True, on


def test_a_disabled_sweep_claims_nothing_so_turning_it_on_loses_no_reminder(
        client, store, sent, monkeypatch):
    """The claim is what makes a reminder single-use, so a sweep that ran while
    disabled must not spend one. If it did, the day this is switched on would be
    the day every waiting lead silently lost its only follow-up."""
    _submit(client)
    _age_the_thanks(store, _row(store)["id"])
    monkeypatch.setenv("PARTNER_LEAD_REMINDER_ENABLED", "0")
    assert asyncio.run(partner_lead_nudge.sweep(store)) == {"reminder": 0}

    monkeypatch.setenv("PARTNER_LEAD_REMINDER_ENABLED", "1")
    assert asyncio.run(partner_lead_nudge.sweep(store)) == {"reminder": 1}
