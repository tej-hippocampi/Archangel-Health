"""Landing lead-capture forms — "Request products" (AI-lab / health-AI buyers) and
"Provide data" (health systems, practices & software companies).

Two-field public forms (see design/Landing_Request_and_Provide_Forms_PRD.md):
each submission is stored in `lead_submissions` and a notification is emailed to
``LEAD_NOTIFY_EMAIL`` (default tejpatel@berkeley.edu). No login, no PHI — the
free-text box is a *description* of what a provider holds, never patient data.
"""

import html
import os
from typing import Optional

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
    partner = source == "health_system_partner"
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
    asks_qualifying = body.source == "health_system_partner"

    def _answer(raw: str) -> Optional[str]:
        text = (raw or "").strip()
        if text:
            return text
        return "" if asks_qualifying else None

    try:
        _ts(request).record_lead_submission(
            body.source, email, message, user_agent=ua, client_ip=ip,
            authority_answer=_answer(body.authority_answer),
            deidentification_answer=_answer(body.deidentification_answer),
            data_scale_answer=_answer(body.data_scale_answer),
        )
    except Exception:
        pass

    if not is_email_transport_configured():
        # Stored, but we can't notify — surface a soft failure so the UI shows
        # its "or email us" fallback instead of a false success.
        raise HTTPException(
            status_code=503,
            detail="We couldn't send that just now: please email us instead.",
        )

    subject = f"[Lead] {_SOURCE_LABELS.get(body.source, body.source)}: {email}"
    qualifying = _qualifying_rows(body.source, body.model_dump())
    ok = await send_html_email(
        _notify_email(), subject,
        _build_lead_email_html(body.source, email, message, qualifying))
    if not ok:
        raise HTTPException(
            status_code=503,
            detail="We couldn't send that just now: please email us instead.",
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
            }
            for r in rows
        ],
        "sources": [{"key": k, "label": v} for k, v in _SOURCE_LABELS.items()],
        # The cursor for the next page, or None when this page is the end.
        # Derived here rather than by the client, which would otherwise have to
        # know that the keyset is the id.
        "next_before_id": rows[-1]["id"] if len(rows) == limit else None,
    }
