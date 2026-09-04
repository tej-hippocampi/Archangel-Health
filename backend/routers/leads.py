"""Landing lead-capture forms — "Request products" (AI-lab / health-AI buyers) and
"Provide data" (health systems, practices & software companies).

Two-field public forms (see design/Landing_Request_and_Provide_Forms_PRD.md):
each submission is stored in `lead_submissions` and a notification is emailed to
``LEAD_NOTIFY_EMAIL`` (default tejpatel@berkeley.edu). No login, no PHI — the
free-text box is a *description* of what a provider holds, never patient data.
"""

import html
import os
from typing import Any, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, EmailStr, Field

from asclepius import auth as asc_auth
from email_utils import is_email_transport_configured, send_html_email
from ratelimit import client_ip, rate_limiter

router = APIRouter(prefix="/api/leads", tags=["leads"])

# Human labels for the two source streams so the notification email + subject
# make the two pipelines obvious and separable.
_SOURCE_LABELS = {
    "request_data": "Request products · AI lab / buyer",
    "provide_data": "Provide data · data provider",
    "research_notify": "Research notify · publication list",
    # /partner, the link on the health-system one-pager. Its descriptive answers
    # arrive folded into `message`, because the point of that page is to make the
    # intro call start from something real, and the three QUALIFYING answers
    # arrive as their own fields (see LeadBody). Its own label so the
    # health-system pipeline is separable from the generic "provide data" one in
    # an inbox.
    "health_system_partner": "Partner interest · health system",
}

_LEAD_SOURCES = tuple(_SOURCE_LABELS.keys())

#: The one source whose visitor is owed a letter of their own. Every other form
#: here is a note to us; this one is the start of a conversation with them, and
#: since the booking control came off the /partner success screen that
#: conversation begins in an inbox. Named rather than repeated, because four
#: places now branch on it and a mistyped string literal is a silent no-op.
HEALTH_SYSTEM_SOURCE = "health_system_partner"


def _ts(request: Request):
    return request.app.state.team_store


def _notify_email() -> str:
    return (os.getenv("LEAD_NOTIFY_EMAIL") or "tejpatel@berkeley.edu").strip()


class LeadBody(BaseModel):
    # Literal-free so an unknown source yields our own 422 message rather than a
    # noisy pydantic enum error; validated explicitly below.
    source: str = Field(min_length=1, max_length=32)
    email: EmailStr
    message: str = Field(min_length=1, max_length=5000)
    # Honeypot — real users never see or fill this; a non-empty value is a bot.
    company_website: str = Field(default="", max_length=200)
    #: Set by /partner when the visitor arrived from a physician's introduction
    #: (HS-REF). Opaque here: this router does not read it, it only hands it to
    #: the store to resolve, and an unknown value is a no-op.
    referral_token: str = Field(default="", max_length=128)
    #: Set by /partner when the visitor arrived on a physician's PLAIN referral
    #: link (``/partner?ref=CODE``), which is the link a physician copies out of
    #: their own dashboard and forwards. Opaque and unvalidated here: it is
    #: resolved against the asclepius store below and an unknown value is a
    #: silent no-op, never an error. A form that rejected a stale code would
    #: lose the submission to protect an attribution.
    referral_code: str = Field(default="", max_length=64)
    #: The three qualifying questions the Sep 1 meeting agreed belong ON this
    #: form: authority to license, ability to de-identify and date-shift, and
    #: the rough shape of the data. Typed fields rather than more prose folded
    #: into ``message`` because they are the part with legal weight.
    #:
    #: Free text, not an enum. The form offers choices, but the value stored is
    #: whatever the visitor's form sent, and pinning an enum here would mean a
    #: reworded option becomes a 422 on the one form we most want to complete.
    #: Validation of an attestation belongs with the person reading it.
    authority_answer: str = Field(default="", max_length=400)
    deidentification_answer: str = Field(default="", max_length=400)
    data_scale_answer: str = Field(default="", max_length=2000)


#: The three qualifying questions, in the order the Sep 1 meeting agreed them
#: and the order the form asks them. ONE tuple, read by the founder notification
#: and by the admin console both, so a reworded question cannot end up saying two
#: different things to the two people who read the same submission.
_QUALIFYING_QUESTIONS = (
    ("authority_answer", "Authority to license"),
    ("deidentification_answer", "De-identify and date-shift"),
    ("data_scale_answer", "Patients, years, specialties"),
)

#: What a health-system submission shows for a question it carries no answer to.
#: Not blank, and never an assumed "no". The attestation is the reason this row
#: is archived at all, so a missing one has to READ as missing to whoever opens
#: the submission later, rather than as a question we never thought to ask.
_UNANSWERED = "Not answered"


def _qualifying_rows(source: str, row: dict) -> list:
    """The labelled qualifying answers for one submission, as (label, value).

    A health-system submission always yields all three, filled or not, because
    an audit trail with a silently absent question is not one. Every other
    source yields only what it actually sent, which is normally nothing: those
    three forms do not ask, and printing three "Not answered" lines under a
    buyer's request would invent a gap rather than report one.
    """
    partner = source == HEALTH_SYSTEM_SOURCE
    out = []
    for key, label in _QUALIFYING_QUESTIONS:
        value = (row.get(key) or "").strip()
        if value:
            out.append((label, value))
        elif partner:
            out.append((label, _UNANSWERED))
    return out


def _build_lead_email_html(source: str, email: str, message: str,
                           qualifying: Optional[list] = None) -> str:
    label = _SOURCE_LABELS.get(source, source)
    safe_email = html.escape(email)
    safe_message = html.escape(message).replace("\n", "<br>")
    # Above the free-text message on purpose. These are the answers that decide
    # whether the call is worth taking, and a founder reading this on a phone
    # should not have to scroll past a paragraph of prose to find them.
    qualifying_html = "".join(
        f"""
    <tr>
      <td style="padding:8px 0;color:#8b8d89;vertical-align:top">{html.escape(q_label)}</td>
      <td style="padding:8px 0">{html.escape(q_value)}</td>
    </tr>"""
        for q_label, q_value in (qualifying or [])
    )
    return f"""\
<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#1a1b1a;line-height:1.6">
  <p style="font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:#8b8d89;margin:0 0 4px">
    New landing lead
  </p>
  <h2 style="margin:0 0 16px;font-weight:600">{html.escape(label)}</h2>
  <table style="border-collapse:collapse;width:100%;max-width:560px">
    <tr>
      <td style="padding:8px 0;color:#8b8d89;width:120px;vertical-align:top">Email</td>
      <td style="padding:8px 0"><a href="mailto:{safe_email}">{safe_email}</a></td>
    </tr>{qualifying_html}
    <tr>
      <td style="padding:8px 0;color:#8b8d89;vertical-align:top">Message</td>
      <td style="padding:8px 0">{safe_message}</td>
    </tr>
    <tr>
      <td style="padding:8px 0;color:#8b8d89;vertical-align:top">Source</td>
      <td style="padding:8px 0"><code>{html.escape(source)}</code></td>
    </tr>
  </table>
  <p style="margin:20px 0 0;font-size:13px;color:#8b8d89">
    Reply directly to <a href="mailto:{safe_email}">{safe_email}</a> to respond.
  </p>
</div>"""


def _resolve_referral_code(code: str) -> Optional[str]:
    """A physician's referral code, as the user id it belongs to, or None.

    Silent on every failure. An unknown code is somebody's stale link, a
    malformed one is somebody's mangled paste, and neither is a reason to lose a
    health system's submission. The store is imported lazily for the same reason
    the referral-token block below does it: this router runs in the landing app
    and must not pull the asclepius store into existence just to accept a form.
    """
    token = (code or "").strip()
    if not token:
        return None
    try:
        from asclepius.store import get_store

        user = get_store().get_user_by_referral_code(token)
    except Exception:
        return None
    return str(user["id"]) if user and user.get("id") else None


def _referrer_label(user_id: Optional[str]) -> str:
    """How the referring physician reads on an admin lead row.

    Their name, or their id when we hold no name. Not their email: this is a
    console an operator reads, the id is enough to find the row, and the same
    rule ``referrer_display_name`` states about not falling back to an address
    applies to every surface that prints a referrer.
    """
    uid = (user_id or "").strip()
    if not uid:
        return ""
    try:
        from asclepius.store import get_store

        user = get_store().get_user_by_id(uid) or {}
    except Exception:
        return uid
    return (user.get("full_name") or "").strip() or uid


async def _send_partner_thanks(store: Any, lead_id: Optional[int], email: str,
                               body: "LeadBody") -> None:
    """Thank a health system for writing in, and hand them the booking link.

    BEST EFFORT, and never allowed to raise: the form submission is the thing
    the visitor is waiting on and a mail failure must not turn their answers
    into an error page. The stamp is written only after a send that actually
    reported success, because ``partner_lead_nudge`` measures the reminder's
    delay from it and a stamp on an email that never left would start that clock
    on nothing.
    """
    from onboarding_emails import (  # noqa: PLC0415
        PARTNER_BOOKING_CALENDLY, build_hs_interest_thanks_email,
    )

    full_name, organization = partner_lead_contact(body.message)
    ok = await send_html_email(
        email, "Thank you for submitting",
        build_hs_interest_thanks_email(
            full_name=full_name, organization=organization,
            booking_url=PARTNER_BOOKING_CALENDLY))
    if ok and lead_id is not None:
        store.stamp_lead_thanks_sent(lead_id)


#: The two labels ``PartnerInterest.tsx`` writes at the top of the message it
#: composes. The /partner form sends one prose blob plus the three qualifying
#: answers, so the contact's name and their organization exist ONLY inside that
#: blob, and both letters below need them: a reminder addressed to nobody in
#: particular, about no organization in particular, reads as a mailshot.
#:
#: Read back out of the message rather than added as two more columns because
#: the reminder is sent days later by a sweep that has only the stored row, and
#: the row's one human-written field is the message. Keep these in step with
#: ``composeMessage`` in that component; the pair is asserted in
#: ``tests/test_partner_lead_followup.py``.
_MESSAGE_NAME_LABEL = "Contact"
_MESSAGE_ORG_LABEL = "Health system"


def partner_lead_contact(message: str) -> Tuple[str, str]:
    """``(full name, organization)`` for a health-system lead, from its message.

    Both degrade to an empty string and both letters handle that: they drop the
    greeting and the organization clause rather than printing a blank where a
    name should be. Imported by ``asclepius/partner_lead_nudge``, which has the
    row and nothing else, the same way the onboarding nudge borrows its
    predicates from the router that owns them rather than restating them.
    """
    found = {}
    for block in (message or "").split("\n\n"):
        label, _, value = block.strip().partition(":")
        found[label.strip().lower()] = value.strip()
    return (found.get(_MESSAGE_NAME_LABEL.lower(), ""),
            found.get(_MESSAGE_ORG_LABEL.lower(), ""))


@router.post("", dependencies=[Depends(rate_limiter("landing_lead", 8, 60))])
async def submit_lead(body: LeadBody, request: Request):
    # Honeypot: a filled hidden field means a bot. Accept silently — store
    # nothing, send nothing — so the bot can't tell it was caught.
    if body.company_website.strip():
        return {"ok": True}

    if body.source not in _LEAD_SOURCES:
        raise HTTPException(status_code=422, detail="Unknown form.")

    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="Please tell us a little about what you need.")
    email = str(body.email).strip()

    # A submission that came from a physician's introduction advances that
    # physician's funnel row. Best-effort, AFTER validation so a rejected
    # submission cannot tell the referrer "they told us about their system",
    # but deliberately BEFORE the transport check below: this endpoint 503s
    # when email is unconfigured, and the referring doctor should not lose the
    # one signal that their introduction is working because our SendGrid key
    # expired.
    if body.referral_token:
        try:
            from asclepius.store import get_store

            store = get_store()
            row = store.get_hs_referral_by_token(body.referral_token)
            if row:
                store.advance_hs_referral(row["hs_referral_id"], "submitted")
        except Exception:
            pass

    # The OTHER half of the same attribution, and the half that was missing.
    # ``asclepius/referrals.py::partner_url`` has always built
    # ``/partner?ref=CODE&hs=TOKEN``, but only ``hs`` was ever read, so a
    # physician who copied their plain referral link and sent it to a health
    # system themselves got no credit for the introduction at all. Resolved to a
    # user id here rather than stored raw: a code can be reissued, and the
    # question this answers later is which PERSON made the introduction.
    referred_by_user_id = _resolve_referral_code(body.referral_code)

    # Persist first (best-effort). Never fail the request on a storage hiccup —
    # the email is the primary delivery path.
    ua: Optional[str] = request.headers.get("user-agent")
    ip: Optional[str] = None
    try:
        ip = client_ip(request)
    except Exception:
        ip = None
    # Verbatim, and only trimmed of surrounding whitespace. Nothing here maps an
    # answer onto a token of ours: what is archived has to be what they chose.
    # ``None`` rather than "" for a form that does not ask, so "never asked" and
    # "asked and skipped" stay two different facts in the row.
    asks_qualifying = body.source == HEALTH_SYSTEM_SOURCE

    def _answer(raw: str) -> Optional[str]:
        text = (raw or "").strip()
        if text:
            return text
        return "" if asks_qualifying else None

    lead_id: Optional[int] = None
    try:
        lead_id = _ts(request).record_lead_submission(
            body.source, email, message, user_agent=ua, client_ip=ip,
            authority_answer=_answer(body.authority_answer),
            deidentification_answer=_answer(body.deidentification_answer),
            data_scale_answer=_answer(body.data_scale_answer),
            referred_by_user_id=referred_by_user_id,
        )
    except Exception:
        lead_id = None

    if not is_email_transport_configured():
        # Stored, but we can't notify — surface a soft failure so the UI shows
        # its "or email us" fallback instead of a false success.
        raise HTTPException(
            status_code=503,
            detail="We couldn't send that just now — please email us instead.",
        )

    # Them first, us second. A health system that just filled in nine fields is
    # waiting on the one message that tells them what happens next, and the
    # founder notification below can 503 this request; ordering the sends the
    # other way round would mean a SendGrid hiccup on OUR letter costs THEM
    # theirs. Wrapped because a mail failure must never fail the submission:
    # the answers are already stored, and an error page here would ask a CIO to
    # fill the form in twice.
    if body.source == HEALTH_SYSTEM_SOURCE:
        try:
            await _send_partner_thanks(_ts(request), lead_id, email, body)
        except Exception:
            pass

    subject = f"[Lead] {_SOURCE_LABELS.get(body.source, body.source)} — {email}"
    qualifying = _qualifying_rows(body.source, body.model_dump())
    ok = await send_html_email(
        _notify_email(), subject,
        _build_lead_email_html(body.source, email, message, qualifying))
    if not ok:
        raise HTTPException(
            status_code=503,
            detail="We couldn't send that just now — please email us instead.",
        )
    return {"ok": True}


@router.get("/admin")
async def list_leads_admin(
    request: Request,
    source: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    before_id: Optional[int] = Query(default=None),
    _admin: dict = Depends(asc_auth.require_admin),
):
    """Read the submissions back, newest first.

    Lives here rather than in the admin router because the write lives here: the
    table is a team-store table, this module already holds the team-store handle,
    and splitting reader from writer across two routers is how the two drift
    about what a source string means.

    Every source is returned by default and ``source`` narrows it, because the
    health-system pipeline and the buyer pipeline are read by different people
    and neither wants to scroll past the other. Honeypot submissions cannot
    appear: they were never stored (``submit_lead`` returns before the write).

    An unknown ``source`` is an empty page rather than a 422. The filter names a
    stream we may add to or rename, and a console that 500s because a chip is a
    release behind is worse than one that shows nothing.
    """
    src = (source or "").strip() or None
    rows = _ts(request).list_lead_submissions(
        source=src, limit=limit, before_id=before_id)
    return {
        "leads": [
            {
                "id": r["id"],
                "source": r["source"],
                "source_label": _SOURCE_LABELS.get(r["source"], r["source"]),
                "email": r["email"],
                "message": r["message"],
                "created_at": r["created_at"],
                # Labelled pairs rather than three raw keys, so the console
                # renders whatever the questions currently are without shipping
                # its own copy of their wording. The label lives in one place.
                "qualifying": [{"label": q_label, "answer": q_answer}
                               for q_label, q_answer in _qualifying_rows(r["source"], r)],
                # Where this lead got to, as three clocks rather than one
                # status word. An operator looking at the list is deciding who
                # to chase by hand, and "thanked, reminded, still not booked"
                # and "thanked, booked" are the two answers that decide it.
                "thanks_sent_at": r.get("thanks_sent_at"),
                "reminder_sent_at": r.get("reminder_sent_at"),
                "call_booked_at": r.get("call_booked_at"),
                # The physician who introduced them, when there was one. This is
                # money: the referral is the cheapest introduction we get, and
                # an operator who cannot see it cannot credit it.
                "referred_by": _referrer_label(r.get("referred_by_user_id")),
            }
            for r in rows
        ],
        "sources": [{"key": k, "label": v} for k, v in _SOURCE_LABELS.items()],
        # The cursor for the next page, or None when this page is the end.
        # Derived here rather than by the client, which would otherwise have to
        # know that the keyset is the id.
        "next_before_id": rows[-1]["id"] if len(rows) == limit else None,
    }


@router.post("/admin/{lead_id}/booked")
async def mark_lead_booked(
    lead_id: int,
    request: Request,
    _admin: dict = Depends(asc_auth.require_admin),
):
    """Record that this lead booked its call.

    Admin-gated exactly like the reader above, and for the same reason: the lead
    table is an audit trail of attestations, and who may write on one is the
    same question as who may read one.

    It exists because nothing else can know. The booking happens on Calendly,
    which does not call us back, so the only thing that can say a call was
    booked is the person who saw it appear in their calendar. What the stamp
    BUYS is the reminder: ``partner_lead_nudge`` will not chase a row whose call
    is booked, so this is how an operator stops a letter that would otherwise
    ask a partner to book a meeting they are already coming to.

    Idempotent. Clicking it twice is an operator clicking it twice, and the
    first time is the one that is true, so a second call succeeds and changes
    nothing.
    """
    if not _ts(request).mark_lead_call_booked(lead_id):
        raise HTTPException(status_code=404, detail="No such lead.")
    return {"ok": True}
