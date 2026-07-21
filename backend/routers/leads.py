"""Landing lead-capture forms — "Request data" (AI-lab / health-AI buyers) and
"Provide data" (health systems, practices & software companies).

Two-field public forms (see design/Landing_Request_and_Provide_Forms_PRD.md):
each submission is stored in `lead_submissions` and a notification is emailed to
``LEAD_NOTIFY_EMAIL`` (default tejpatel@berkeley.edu). No login, no PHI — the
free-text box is a *description* of what a provider holds, never patient data.
"""

import html
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field

from email_utils import is_email_transport_configured, send_html_email
from ratelimit import client_ip, rate_limiter

router = APIRouter(prefix="/api/leads", tags=["leads"])

# Human labels for the two source streams so the notification email + subject
# make the two pipelines obvious and separable.
_SOURCE_LABELS = {
    "request_data": "Request data · AI lab / buyer",
    "provide_data": "Provide data · data provider",
    "research_notify": "Research notify · publication list",
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


def _build_lead_email_html(source: str, email: str, message: str) -> str:
    label = _SOURCE_LABELS.get(source, source)
    safe_email = html.escape(email)
    safe_message = html.escape(message).replace("\n", "<br>")
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
    </tr>
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

    # Persist first (best-effort). Never fail the request on a storage hiccup —
    # the email is the primary delivery path.
    ua: Optional[str] = request.headers.get("user-agent")
    ip: Optional[str] = None
    try:
        ip = client_ip(request)
    except Exception:
        ip = None
    try:
        _ts(request).record_lead_submission(
            body.source, email, message, user_agent=ua, client_ip=ip
        )
    except Exception:
        pass

    if not is_email_transport_configured():
        # Stored, but we can't notify — surface a soft failure so the UI shows
        # its "or email us" fallback instead of a false success.
        raise HTTPException(
            status_code=503,
            detail="We couldn't send that just now — please email us instead.",
        )

    subject = f"[Lead] {_SOURCE_LABELS.get(body.source, body.source)} — {email}"
    ok = await send_html_email(_notify_email(), subject, _build_lead_email_html(body.source, email, message))
    if not ok:
        raise HTTPException(
            status_code=503,
            detail="We couldn't send that just now — please email us instead.",
        )
    return {"ok": True}
