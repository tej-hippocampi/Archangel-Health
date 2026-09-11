"""HTML transactional emails for Archangel Health.

These are the only surface where the product speaks to a physician outside the
product, and for a self-serve signup they are the FIRST thing they ever see of
us. So they are built from the same tokens as the app and the landing site:
canvas #eef0ef, ink #1a1b1a, one hairline, and the four accents. Nothing here
is invented for email.

Shared shell + composable atoms; every builder below is
``_shell(subject=..., body_html=_eyebrow(...) + _h1(...) + ...)``. Changing the
look means changing the tokens here, once, and all of them follow.

Design laws inherited from frontend/asclepius/_tokens.css (they are load-bearing,
not decoration):
  - air is the design, scale not boldness (headings are weight 400, not bold)
  - zero black fills (--ink-hover exists so hover is never #000)
  - gradients only as blurred auras, never as a surface
  - mono chrome = wayfinding (eyebrows and data are mono; prose is not)
  - the accents are SEMANTIC: green = physician-verified, orange = model output,
    pink = PHI/critical, lime = new/active. Never decorative.

Client compatibility:
  - <table>/<td> + inline style="" throughout, for Outlook.
  - NO webfonts. Instrument Sans and IBM Plex Mono are base64-embedded in the
    app and are not on a CDN, and a webfont <link> is blocked by most mail
    clients anyway. System stacks carry the design; the palette does the work.
  - Gradients are set as background-image over a background-color, so a client
    that drops them lands on the flat token rather than on nothing.
"""

from __future__ import annotations

import html
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ─── Tokens ─────────────────────────────────────────────────────────────────
# Mirror of frontend/asclepius/_tokens.css §2.1 (itself a copy of the landing
# app's arch/baseStyles.ts consolePalette). Do not introduce a hex here that is
# not in that file.

_CANVAS = "#eef0ef"
_CARD = "#fbfcfa"
_CARD_IN = "#f4f5f3"
_HAIRLINE = "rgba(26, 27, 26, 0.08)"
_HAIRLINE_STRONG = "rgba(26, 27, 26, 0.16)"
_INK = "#1a1b1a"
_INK_SOFT = "#5c5e5a"
_INK_FAINT = "#8b8d89"
_GREEN = "#4ca63c"
_GREEN_DEEP = "#3c7a31"   # AA-contrast green for text on a light surface
_ORANGE = "#ec9440"
_PINK = "#e8447b"
_LIME = "#d5e14e"
#: The lime wash, flattened onto the card background. Written as a 6-digit hex
#: rather than "#d5e14e33", because 8-digit hex is CSS Color 4 and Outlook's
#: Word renderer drops the declaration outright -- and §1.1 of the digest design
#: makes this wash THE label for why-it-matters, so losing it means that
#: sentence arrives unlabelled in the client most likely to be reading it.
#: 20% of --lime over #fbfcfa, the same ratio --lime-wash uses on the web.
_LIME_WASH = "#f3f7d8"

# No webfonts in email. See the module docstring.
_SANS = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"
_MONO = "ui-monospace, 'SF Mono', SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace"

# Kept as aliases so the builders below (and anything vendored against them)
# keep working; both now resolve to the system stacks above.
_INTER = _SANS
_FRAUNCES = _SANS

_FOOTER_TEXT = (
    "Archangel Health &middot; Confidential. This email and any attached files are "
    "intended only for the named recipient."
)

# The brandmark, at 20px. Ink strokes with a single green node. Green is the
# "physician-verified" accent, which is the one claim the mark should make.
_SHIELD_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 120" '
    'width="20" height="20" fill="none" aria-hidden="true">'
    f'<rect x="58" y="20" width="4" height="80" fill="{_INK}" rx="2"/>'
    f'<circle cx="60" cy="28" r="5" fill="{_GREEN}"/>'
    f'<path d="M60 45 Q50 50 48 58 Q46 66 54 70" stroke="{_INK}" stroke-width="2.5" '
    'fill="none" stroke-linecap="round"/>'
    f'<path d="M60 55 Q70 60 72 68 Q74 76 66 80" stroke="{_INK}" stroke-width="2.5" '
    'fill="none" stroke-linecap="round"/>'
    "</svg>"
)


def _shell(*, subject: str, body_html: str) -> str:
    """Wrap body content in the console shell: canvas ground, one card, hairlines."""
    safe_subject = html.escape(subject, quote=True)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="x-apple-disable-message-reformatting">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
<title>{safe_subject}</title>
</head>
<body style="margin:0;padding:0;background:{_CANVAS};font-family:{_SANS};color:{_INK_SOFT};-webkit-font-smoothing:antialiased;">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:{_CANVAS};">
  <tr>
    <td align="center" style="padding:40px 12px;">
      <table role="presentation" width="600" cellspacing="0" cellpadding="0" border="0" style="max-width:600px;width:100%;background:{_CARD};border-radius:18px;overflow:hidden;border:1px solid {_HAIRLINE};box-shadow:0 1px 2px rgba(26,27,26,0.03);">
        <tr>
          <td style="padding:36px 44px 40px;background-color:{_CARD};background-image:radial-gradient(36rem 24rem at 8% -10%, rgba(76,166,60,0.05), transparent 70%), radial-gradient(30rem 22rem at 100% 8%, rgba(236,148,64,0.045), transparent 70%);">
            <table role="presentation" cellspacing="0" cellpadding="0" border="0" style="margin-bottom:30px;">
              <tr>
                <td style="vertical-align:middle;line-height:1;">{_SHIELD_SVG}</td>
                <td style="padding-left:9px;vertical-align:middle;font-family:{_MONO};font-size:11px;font-weight:500;letter-spacing:0.09em;text-transform:uppercase;color:{_INK_SOFT};">
                  Archangel Health
                </td>
              </tr>
            </table>
            {body_html}
          </td>
        </tr>
        <tr>
          <td style="background:{_CARD_IN};padding:18px 44px;font-family:{_SANS};font-size:11px;color:{_INK_FAINT};line-height:1.6;border-top:1px solid {_HAIRLINE};">
            {_FOOTER_TEXT}
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>
</body>
</html>"""


def _eyebrow(text: str) -> str:
    """Mono chrome: the wayfinding line above a heading."""
    return (
        f'<div style="font-family:{_MONO};font-size:11px;font-weight:500;'
        f'letter-spacing:0.09em;text-transform:uppercase;color:{_INK_FAINT};'
        'margin-bottom:14px;">'
        f"{html.escape(text)}</div>"
    )


def _h1(text: str) -> str:
    """Scale, not boldness: weight 400, negative tracking, ink.

    DOES NOT ESCAPE. Several callers pass markup deliberately (``&rsquo;``, an
    escaped name inside a sentence), so escaping here would double-escape them.
    The convention is that the CALLER escapes anything a person can type, and
    every caller below that interpolates a value does exactly that.

    Three health-system alert builders did not, and the value they passed was an
    organization name straight off the public signup form — rendered into the
    headline of the email that lands next to the Approve button. Fixed at the
    call sites; ``test_email_escaping.py`` now holds all of them.
    """
    return (
        f'<h1 style="margin:0 0 16px;font-family:{_SANS};font-size:30px;'
        f'font-weight:400;letter-spacing:-0.015em;color:{_INK};line-height:1.2;">'
        f"{text}</h1>"
    )


def _p(html_content: str, *, muted: bool = False, small: bool = False) -> str:
    color = _INK_FAINT if muted else _INK_SOFT
    size = "13px" if small else "15px"
    return (
        f'<p style="margin:0 0 16px;font-family:{_SANS};font-size:{size};'
        f'line-height:1.6;color:{color};">{html_content}</p>'
    )


def _strong(text: str) -> str:
    return f'<strong style="color:{_INK};font-weight:600;">{html.escape(text)}</strong>'


def _exam_url(portal_url: str) -> str:
    """The portal URL with the examination card focused.

    ``#examination`` is the deep link the applicant home already answers to
    (asclepius.js), so a mail that names the examination lands ON it rather than
    on a page the physician then has to read to find it.

    Idempotent, and it never invents a second fragment: a caller that already
    passed one is left alone, because appending would produce a URL no router
    resolves. An empty portal_url stays empty — callers treat that as "render
    the copy without a link" and a bare "#examination" is not a destination.
    """
    url = (portal_url or "").strip()
    if not url or "#" in url:
        return url
    return url + "#examination"


def _cta(href: str, label: str) -> str:
    """The product's emphatic button: a lime pill with ink text (.btn-lime)."""
    safe_href = html.escape(href, quote=True)
    safe_label = html.escape(label)
    return f"""<table role="presentation" cellspacing="0" cellpadding="0" border="0" style="margin:24px 0 20px;">
  <tr>
    <td style="border-radius:999px;background:{_LIME};">
      <a href="{safe_href}" style="display:inline-block;padding:13px 26px;border-radius:999px;background:{_LIME};color:{_INK};font-family:{_SANS};font-size:15px;font-weight:700;text-decoration:none;letter-spacing:-0.005em;">
        {safe_label}
      </a>
    </td>
  </tr>
</table>"""


#: One line, offered at the end of onboarding, pointing at /partner.
#:
#: The Sep 1 meeting asked for this in both places a new physician looks: the
#: screen they finish on and the email they keep. The reason is that a physician
#: who happens to know someone at a health system is the cheapest introduction
#: we will ever get, and without a link in front of them the thought arrives with
#: nowhere to go.
#:
#: It is a SENTENCE, never a second call to action. These emails already carry
#: one button, and a second competing with it costs us the click that matters
#: more, which is the one that opens their workspace.
def _partner_intro_line(partner_url: str) -> str:
    """A physician's path to hand us a health system, or nothing at all.

    Empty in, empty out: a caller with no configured landing URL says nothing
    rather than shipping a dead link into somebody's inbox.
    """
    url = (partner_url or "").strip()
    if not url:
        return ""
    safe = html.escape(url, quote=True)
    return _p(
        "Know a health system with clinical data? "
        f'<a href="{safe}" style="color:{_GREEN_DEEP};">Send them here</a>'
        " and we will take the conversation from there.",
        muted=True, small=True,
    )


def _inset_card(inner_html: str) -> str:
    return f"""<div style="background:{_CARD_IN};border:1px solid {_HAIRLINE};border-radius:14px;padding:18px 22px;margin:22px 0;">
  {inner_html}
</div>"""


def _code_block(code: str, *, size: int = 40) -> str:
    """A one-time code, rendered as data: mono, generous tracking, no glow."""
    safe = html.escape(code)
    return f"""<div style="background:{_CARD_IN};border:1px solid {_HAIRLINE_STRONG};border-radius:14px;padding:26px 24px;text-align:center;margin:24px 0;">
  <div style="font-family:{_MONO};font-size:{size}px;font-weight:500;letter-spacing:0.28em;color:{_INK};padding-left:0.28em;">{safe}</div>
</div>"""


_LONG_DASH_RE = re.compile(r"\s*[–—]+\s*")


def _scrub_dashes(text: str) -> str:
    """Model-composed digest text tends to lean on long dashes as separators,
    which read as clutter in a mail client and are banned by house style."""
    return _LONG_DASH_RE.sub(", ", text or "")


def _split_lead(escaped_text: str) -> Tuple[str, str]:
    """Split one digest item into a bold lead phrase and the rest.

    The lead is what a scanning reader gets; whitespace and weight carry the
    hierarchy, so no separator glyph is ever rendered between the two parts.
    Input must already be HTML-escaped (split points all contain a space, so a
    split can never land inside an entity).
    """
    t = escaped_text.strip()
    for stop in (". ", "? ", "! "):
        idx = t.find(stop)
        if 0 < idx <= 90:
            return t[: idx + 1], t[idx + 2 :]
    idx = t.find(": ")
    if 0 < idx <= 60:
        return t[:idx], t[idx + 2 :]
    words = t.split()
    if len(words) > 8:
        return " ".join(words[:7]), " ".join(words[7:])
    return t, ""


def _lead_list(items: Iterable[Tuple[str, str]]) -> str:
    """Digest items as table rows: bold lead-in phrase, plain remainder,
    hairline between rows. Replaces <ul> entirely; mail clients render tables
    far more consistently than lists, and there is no bullet glyph to argue
    about. Both parts of each item must already be HTML-escaped."""
    rows = []
    for i, (lead, rest) in enumerate(items):
        border = "" if i == 0 else f"border-top:1px solid {_HAIRLINE};"
        rest_html = (
            f' <span style="color:{_INK_SOFT};font-weight:400;">{rest}</span>'
            if rest
            else ""
        )
        rows.append(
            f'<tr><td style="padding:14px 0;{border}font-family:{_SANS};'
            f"font-size:15px;line-height:1.6;color:{_INK_SOFT};\">"
            f'<strong style="color:{_INK};font-weight:600;">{lead}</strong>'
            f"{rest_html}</td></tr>"
        )
    return (
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        'border="0" style="margin:4px 0 14px;">' + "".join(rows) + "</table>"
    )


def _detail_rows(rows: Iterable[Tuple[str, str, bool]]) -> str:
    """Render <label, value, mono?> rows separated by hairlines (last row has no border)."""
    rows_list = list(rows)
    out = []
    for i, (label, value, mono) in enumerate(rows_list):
        last = i == len(rows_list) - 1
        border = "" if last else f"border-bottom:1px solid {_HAIRLINE};"
        value_font = _MONO if mono else _SANS
        out.append(
            f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" '
            f'style="{border}">'
            "<tr>"
            f'<td style="padding:11px 0;font-family:{_MONO};font-size:11px;font-weight:500;'
            f'letter-spacing:0.09em;text-transform:uppercase;color:{_INK_FAINT};">'
            f"{html.escape(label)}</td>"
            f'<td align="right" style="padding:11px 0;font-family:{value_font};font-size:14px;'
            f'font-weight:500;color:{_INK};text-align:right;">{html.escape(value)}</td>'
            "</tr></table>"
        )
    return "".join(out)


def _pullquote(text: str) -> str:
    """A quiet, ink-weight pull quote: a lime rule on the left and nothing else.

    The one place the emails raise their voice, and it does it with position and
    a hairline rather than a colour block, because the sentence is doing the
    work. Used for the mission lines, which are the same words as the landing
    page's /mission section on purpose.
    """
    return (
        f'<div style="margin:24px 0;padding:4px 0 4px 20px;border-left:3px solid {_LIME};">'
        f'<p style="margin:0;font-family:{_SANS};font-size:17px;line-height:1.55;'
        f'font-weight:500;letter-spacing:-0.01em;color:{_INK};">{text}</p>'
        "</div>"
    )


#: The founders' photo, shown above their names in the emails a physician gets
#: while deciding whether to trust us with their licence number. Served from the
#: existing ``/email-assets`` mount (``backend/assets``), which is why there is
#: no new route here.
#:
#: Absent by default, and that is fine. A remote image in an email is blocked by
#: most clients until the reader allows it, and a BROKEN one is worse than none
#: at all, so the signature degrades to the names alone when the file is not
#: there. Drop a photo at ``backend/assets/founders.jpg`` (or point
#: ``FOUNDER_PHOTO_URL`` at a hosted one) and it appears with no code change.
FOUNDER_PHOTO_FILENAME = "founders.jpg"


def _founder_photo_url() -> str:
    """An absolute URL for the founder photo, or "" when there is nothing to show.

    Absolute on purpose: an email is read outside our origin, so a relative
    ``/email-assets/...`` resolves against the mail client and 404s.
    """
    override = (os.getenv("FOUNDER_PHOTO_URL") or "").strip()
    if override:
        return override
    here = os.path.dirname(os.path.abspath(__file__))
    if not os.path.exists(os.path.join(here, "assets", FOUNDER_PHOTO_FILENAME)):
        return ""
    base = (os.getenv("BASE_URL") or "").strip().rstrip("/")
    if not base:
        # With no base URL configured there is no absolute URL to build, and a
        # relative one in an inbox is a broken image. Show the names instead.
        return ""
    return f"{base}/email-assets/{FOUNDER_PHOTO_FILENAME}"


def _founder_signoff(line: str) -> str:
    """The founders sign their own emails, with their faces where we have them.

    Set slightly apart from the body so it reads as a signature and not as one
    more paragraph. The photo is the point: these messages ask a physician to
    hand over a licence number and a CV, and a name with a face behind it is a
    different ask from a name alone.

    Laid out with a table rather than flexbox because Outlook's rendering engine
    is Word, which has neither flexbox nor grid and would stack the two cells.
    """
    photo = _founder_photo_url()
    if not photo:
        return (
            f'<p style="margin:26px 0 0;padding-top:20px;border-top:1px solid {_HAIRLINE};'
            f'font-family:{_SANS};font-size:15px;line-height:1.6;color:{_INK};">'
            f"{html.escape(line)}</p>"
        )
    text = (
        f'<span style="font-family:{_SANS};font-size:15px;line-height:1.6;'
        f'color:{_INK};">{html.escape(line)}</span>'
    )
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'style="margin:26px 0 0;padding-top:20px;border-top:1px solid {_HAIRLINE};'
        'width:100%;"><tr>'
        '<td width="64" valign="middle" style="padding-right:14px;">'
        f'<img src="{html.escape(photo, quote=True)}" width="56" height="56" '
        f'alt="{html.escape(line, quote=True)}" '
        'style="display:block;width:56px;height:56px;border-radius:28px;'
        'object-fit:cover;border:0;outline:none;text-decoration:none;" />'
        '</td>'
        f'<td valign="middle">{text}</td>'
        '</tr></table>'
    )


def _section_label(text: str) -> str:
    """Mono label INSIDE the body, for the two card-topped sections of the
    welcome email. Distinct from ``_eyebrow``, which is the one wayfinding line
    at the top of a message and must stay singular to keep meaning that."""
    return (
        f'<div style="font-family:{_MONO};font-size:11px;font-weight:500;'
        f'letter-spacing:0.09em;text-transform:uppercase;color:{_INK_FAINT};'
        'margin:30px 0 10px;">'
        f"{html.escape(text)}</div>"
    )


def _first_name(full_or_first: str) -> str:
    """First token of whatever name we hold, or "Doctor".

    Onboarding stores first and last separately, but several call sites only
    have a full name, and greeting a physician "Hello Elena Vasquez" reads like
    a mail merge that did not run.
    """
    part = (full_or_first or "").strip().split()
    return part[0] if part else "Doctor"


def _last_name(full_or_last: str) -> str:
    """Last token, for "Dr. {last_name}". Falls back to nothing so the caller's
    copy degrades to "Doctor" rather than to "Dr. "."""
    parts = (full_or_last or "").strip().split()
    return parts[-1] if parts else ""


#: Onboarding v2 §4.4 §4: the founders meet every physician one on one. One
#: constant, because it appears in the welcome email, the walkthrough and the
#: applicant's own dashboard, and the three must never drift.
#:
#: ONE PHYSICIAN LINK. This pointed at a second calendar for a while, so a
#: doctor invited to "book twenty minutes with us" from an email and the same
#: doctor booking from the portal landed on different founders' calendars. They
#: are one audience having one conversation, so they get one link.
#:
#: The health-system pair below is deliberately NOT collapsed into this: those
#: are a different audience, and which founder takes that call is a routing
#: decision rather than a tidiness one.
#:
#: Env-overridable for the same reason PARTNER_BOOKING_CALENDLY is: a founder
#: moving their calendar should be a deploy variable, not a release.
FOUNDER_INTRO_CALENDLY = (
    os.getenv("FOUNDER_INTRO_URL")
    or "https://calendly.com/aryaabhatia-berkeley/new-meeting"
).strip()

#: Where a health system books the call that /partner used to book on its own
#: success screen. A DIFFERENT calendar from the one above, on a different
#: founder's account, which is why it is a second constant rather than a second
#: use of the first.
#:
#: The same string is ``PARTNER_BOOKING_FALLBACK`` in
#: ``landing/src/app/config.ts``, and the two are held together by
#: ``tests/test_landing_config.py``. They have to be stated twice because the
#: page and the email are built by different toolchains, and the test is what
#: stops the pair from drifting the way the two hardcoded Calendly links in the
#: landing components once did.
#:
#: Env-overridable so a founder moving their calendar is a deploy variable
#: rather than a release, and named for the landing variable it mirrors so one
#: value can drive both.
PARTNER_BOOKING_CALENDLY = (
    os.getenv("PARTNER_BOOKING_URL") or os.getenv("VITE_CALENDLY_URL")
    or "https://calendly.com/aryaabhatia-berkeley/new-meeting?month=2026-03"
).strip()


# ─── Public builders ────────────────────────────────────────────────────────


def build_verification_email(*, code: str) -> str:
    """Email 1: the 6-digit code mailed during onboarding step 2."""
    safe_code = html.escape(code)
    body = (
        _eyebrow("Verification code")
        + _h1("Confirm it&rsquo;s you.")
        + _p(
            "Enter this code in your browser to continue setting up your "
            "health system on Archangel Health."
        )
        + _code_block(safe_code, size=40)
        + _p(
            "This code expires in 15 minutes. If you didn&rsquo;t request it, "
            "ignore this email.",
            muted=True,
            small=True,
        )
    )
    return _shell(subject="Your Archangel Health verification code", body_html=body)


def build_doctor_verification_email(*, code: str, magic_link_url: str) -> str:
    """Doctor sign-up verification — dual format: magic link as the primary
    action (better fit for professional/B2B users), 6-digit code as a fallback
    for corporate email scanners that pre-click and burn single-use links."""
    safe_code = html.escape(code)
    body = (
        _eyebrow("Verify your email")
        + _h1("Confirm it&rsquo;s you.")
        + _p(
            "Click below to verify your email and finish setting up your "
            "Archangel Health account."
        )
        + _cta(magic_link_url, "Verify my email")
        + _p(
            "Having trouble with the link? Enter this code instead:",
            muted=True,
            small=True,
        )
        + _code_block(safe_code, size=32)
        + _p(
            "This link and code expire in 15 minutes. If you didn&rsquo;t request "
            "this, ignore this email.",
            muted=True,
            small=True,
        )
    )
    return _shell(subject="Verify your email for Archangel Health", body_html=body)


def build_task_notification_email(
    *,
    login_url: str,
    is_reminder: bool = False,
    is_escalation: bool = False,
) -> str:
    """Task-assignment nudge. Deliberately content-free by construction — this
    function takes no patient/task-detail parameters, only a login URL, so no
    PHI can end up in a non-BAA-covered email transport (see
    backend/compliance/subprocessors.py)."""
    if is_escalation:
        eyebrow, headline, lede, subject = (
            "Escalation",
            "This still needs your review.",
            "An item assigned to you in CareGuide has been waiting for a while. "
            "Please take a look when you can.",
            "This still needs your review in CareGuide",
        )
    elif is_reminder:
        eyebrow, headline, lede, subject = (
            "Reminder",
            "You have a pending item.",
            "You have a new item to review in CareGuide that hasn&rsquo;t been "
            "opened yet.",
            "You have a pending item in CareGuide",
        )
    else:
        eyebrow, headline, lede, subject = (
            "New item",
            "You have a new item to review.",
            "You have a new item to review in CareGuide.",
            "You have a new item to review in CareGuide",
        )
    body = (
        _eyebrow(eyebrow)
        + _h1(headline)
        + _p(lede)
        + _cta(login_url, "Sign in to review")
        + _p(
            "If you weren&rsquo;t expecting this, you can safely ignore this email.",
            muted=True,
            small=True,
        )
    )
    return _shell(subject=subject, body_html=body)


def build_invite_email(
    *,
    invitee_first_name: str,
    director_full_name: str,
    role_label: str,
    org_name: str,
    department: str,
    temporary_password: str,
    sign_in_url: str,
    invitee_email: str = "",
) -> str:
    """Email 2 — invite for a newly added team member, with their access key.

    ``temporary_password`` is the member's permanent credential (kept under the
    legacy kwarg name for callers): it does not expire and is not force-rotated,
    so this email is their standing access key. ``invitee_email`` is surfaced
    alongside it so the recipient has the full email + password pair to sign in.
    """
    safe_org = html.escape(org_name or "your health system")
    safe_dept = html.escape(department or "")
    org_dept_label = (safe_org + " " + safe_dept).strip()

    cred_rows = []
    if invitee_email:
        cred_rows.append(("Email", invitee_email, True))
    cred_rows.append(("Password (access key)", temporary_password, True))

    body = (
        _eyebrow("Invitation")
        + _h1(f"You&rsquo;re invited to {org_dept_label} workspace.")
        + _p(
            f"Hello {html.escape(invitee_first_name or 'there')}, "
            + _strong(director_full_name or "your director")
            + " has added you as a "
            + _strong(role_label)
            + " at the "
            + _strong((org_name + " " + department).strip())
            + " workspace on Archangel Health."
        )
        + _inset_card(_detail_rows(cred_rows))
        + _cta(sign_in_url, f"Sign in to {department} workspace →" if department else "Sign in to your workspace →")
        + _p(
            "Keep this email. Your password is your standing access key and stays "
            "valid for future sign-ins.",
            muted=True,
            small=True,
        )
    )

    subject_dept = (department or "").strip()
    subject_org = (org_name or "your health system").strip()
    if subject_dept:
        subject = f"You're invited to {subject_org} {subject_dept} workspace"
    else:
        subject = f"You're invited to {subject_org} workspace"
    return _shell(subject=subject, body_html=body)


# ─── Asclepius (data-training product) emails ────────────────────────────────


def build_asclepius_invite_email(
    *,
    invitee_first_name: str,
    director_full_name: str,
    role_label: str,
    org_name: str,
    specialty: str,
    onboarding_url: str,
    invitee_email: str = "",
    referrer_name: str = "",
) -> str:
    """Asclepius member invite — links the clinician to *start* onboarding.

    Unlike the clinical invite, no password is issued here: the member sets up
    their own credentials + attestations first, and receives their standing
    access key in the completion email once they finish.

    ``referrer_name`` adds ONE sentence naming the physician who suggested them
    (Advisor PRD §3.2). That sentence is the entire referral mechanism — a named
    referral converts several times better than a cold invite — and it is added
    here rather than in a second invite email, because two invite emails is how
    they drift apart.
    """
    safe_org = html.escape(org_name or "your organization")
    safe_spec = html.escape(specialty or "")
    org_spec_label = (safe_org + (" · " + safe_spec if safe_spec else "")).strip()

    rows = []
    if invitee_email:
        rows.append(("Email", invitee_email, True))
    rows.append(("Role", role_label, False))
    rows.append(("Organization", org_name or "Not given", False))
    if specialty:
        rows.append(("Specialty", specialty, False))

    referral_line = ""
    referral_exit = ""
    if (referrer_name or "").strip():
        safe_referrer = html.escape(referrer_name.strip())
        referral_line = _p(
            _strong(safe_referrer)
            + " suggested you&rsquo;d be a good fit."
        )
        # The exit line, and it is not politeness. A physician who cannot see how
        # to decline a message from a name they may not recognise marks it spam,
        # and ONE spam complaint costs the sending domain that every other
        # physician's invite goes through. Giving the recipient a way out is the
        # cheapest possible protection for the channel the whole referral
        # mechanism runs on.
        referral_exit = _p(
            f"{safe_referrer} asked us to reach out. If this isn&rsquo;t for you, "
            "ignore this and we won&rsquo;t follow up.",
            muted=True,
            small=True,
        )

    body = (
        _eyebrow("Invitation · Archangel Health")
        + _h1(f"You&rsquo;re invited to contribute to {org_spec_label}.")
        + referral_line
        + _p(
            f"Hello {html.escape(invitee_first_name or 'there')}, "
            + _strong(director_full_name or "your director")
            + " has invited you to join "
            + _strong((org_name or "your organization"))
            + " on Archangel Health, our expert data-training product, where "
            "clinicians review and label AI answers in their specialty."
        )
        + _inset_card(_detail_rows(rows))
        + _cta(onboarding_url, "Start your onboarding →")
        + _p(
            "You&rsquo;ll confirm your clinical credentials and sign a short set of "
            "attestations, then get your workspace access key. This invite link "
            "expires in 30 days.",
            muted=True,
            small=True,
        )
        + referral_exit
    )
    subject = f"You're invited to label data with {(org_name or 'your organization').strip()}"
    return _shell(subject=subject, body_html=body)


def build_asclepius_admin_invite_email(
    *,
    invitee_name: str,
    onboarding_url: str,
) -> str:
    """Admin-initiated Asclepius onboarding invite: the cold, personalized first
    touch for an outreach lead. Distinct from ``build_asclepius_invite_email``
    above, which is a director inviting a team member *mid-onboarding* (that one
    references an org and specialty which do not exist yet here, since this
    recipient has not started onboarding at all)."""
    body = (
        _eyebrow("Invitation · Archangel Health")
        + _h1(f"Welcome to Archangel Health, {html.escape(invitee_name or 'there')}.")
        + _p(
            "You have been invited to join Archangel Health, our expert data-training "
            "product, where physicians review and label AI answers in their specialty."
        )
        + _cta(onboarding_url, "Start your onboarding →")
        + _p(
            "This link is personal to you and expires in 30 days.",
            muted=True,
            small=True,
        )
    )
    return _shell(subject="Your Archangel Health onboarding link", body_html=body)


def build_asclepius_complete_email(
    *,
    email: str,
    full_name: str,
    role_label: str,
    org_name: str,
    specialty: str,
    workspace_url: str,
    is_director: bool,
    team_count: int = 0,
    verification_notice: bool = False,
    partner_url: str = "",
) -> str:
    """Asclepius workspace-ready email — same visual format as the clinical
    completion email, addressed to the data-training product.

    ``temporary_password`` is the person&rsquo;s permanent, standing access key
    (kwarg name kept for parity with the clinical builders).

    ``partner_url`` is optional and defaults to saying nothing. This builder is
    called from three places and a caller that has no landing URL configured
    should send a complete email without a dead link in it, not fail."""
    safe_org = (org_name or "your organization").strip()
    safe_spec = (specialty or "").strip()

    rows = [
        ("Email", email, True),
        ("Role", role_label, False),
        ("Organization", safe_org, False),
        ("Specialty", safe_spec or "Not given", False),
    ]
    if is_director and team_count > 0:
        rows.append(("Team", f"{team_count} {'person' if team_count == 1 else 'people'}", False))


    intro = (
        html.escape(safe_org)
        + (" · " + html.escape(safe_spec) if safe_spec else "")
        + " is live on Archangel Health. You can now open your training console, pick up "
        "evaluation tasks, and start contributing expert-labeled data."
    )

    # PRD-B: the credential-verification notice. Deliberately says nothing
    # about tiers — the admin has not decided yet, and the score is advice.
    verification_html = (
        _p(
            _strong("We’re verifying your credentials")
            + ". You&rsquo;ll hear from us within 24 hours. Your account opens "
            "for evaluation work as soon as our clinical team completes the "
            "review.",
        )
        if verification_notice
        else ""
    )

    body = (
        _eyebrow("Onboarding complete · Archangel Health")
        + _h1("Your workspace is ready.")
        + _p(intro)
        + verification_html
        + _inset_card(_detail_rows(rows))
        + _cta(workspace_url, "Open your workspace →")
        + _p(
            "&#11088; "
            + _strong("Star this email")
            + ". Everything you need to contribute data lives here. Your password is "
            "the password you chose during sign-up. Forgot it? Use the reset link on the "
            "sign-in page and we will email you a new one.",
            small=True,
        )
        # Last, under the practical housekeeping. It is an offer, not an
        # instruction, and it must not come between them and their workspace.
        + _partner_intro_line(partner_url)
    )
    return _shell(subject="Your Archangel Health workspace is ready", body_html=body)


def build_asclepius_task_notification_email(
    *, specialty_label: str, task_count: int, workspace_url: str,
) -> str:
    """New-work ping for evaluators when an admin uploads a specialty-tagged
    task batch. Deliberately content-free — specialty name, count, and a login
    URL only, no case text or PHI."""
    plural = "task" if task_count == 1 else "tasks"
    is_are = "is" if task_count == 1 else "are"
    body = (
        _eyebrow("New work · Archangel Health")
        + _h1(f"{task_count} new {html.escape(specialty_label)} {plural} ready.")
        + _p(
            f"{task_count} new {html.escape(specialty_label)} {plural} "
            f"{is_are} ready to review in your Archangel Health workspace."
        )
        + _cta(workspace_url, "Open my workspace →")
    )
    subject = f"{task_count} new {specialty_label} {plural} ready in your Archangel Health workspace"
    return _shell(subject=subject, body_html=body)


def build_data_provider_invite_email(
    *,
    portal_url: str,
    email: str,
    temporary_password: str,
    org_name: str = "",
    specialty: str = "",
    note: str = "",
    invite_ttl_days: int = 14,
    magic_link: str = "",
) -> str:
    """Data Provider Portal invite (Data Provider Portal PRD §4) — "Place your data
    right here." Carries the portal URL + the credentials (email + temporary
    password) in the inset card, matching the existing Asclepius brand.

    Security posture (PRD §4): emailing a password is weaker than a magic link, so
    the password is TEMPORARY, single-use, forced-reset on first login, and
    expires in ``invite_ttl_days``; login is rate-limited. An optional one-click
    ``magic_link`` is offered IN ADDITION to the credentials when provided.
    """
    safe_org = (org_name or "").strip()
    safe_spec = (specialty or "").strip()

    rows = [
        ("Portal", portal_url, False),
        ("Email", email, True),
        ("Temporary password", temporary_password, True),
    ]
    if safe_org:
        rows.append(("Organization", safe_org, False))
    if safe_spec:
        rows.append(("Specialty", safe_spec, False))

    what_to_send = _p(
        _strong("What to send: ")
        + "a structured EHR export (FHIR / HL7 / CSV), lab results, clinical "
        "notes, and medication &amp; problem lists. "
        + _strong("Already de-identified and date-shifted.")
        + " Optionally include a <code>manifest.json</code> "
        "(<code>patient_key</code>, <code>index_event</code>, "
        "<code>specialty</code>). It makes ingestion far more reliable. "
        + _strong("No imaging."),
        small=True,
    )

    intro = (
        "You&rsquo;ve been invited to securely send your de-identified clinical "
        "data to " + _strong("Archangel Health") + ". Your upload portal is ready "
        "and a locked-down account has been created for you. The credentials are "
        "below."
    )
    if note:
        intro += " " + html.escape(note.strip())

    body = (
        _eyebrow("Upload access · Archangel Health")
        + _h1("Place your data right here.")
        + _p(intro)
        + _inset_card(_detail_rows(rows))
        + _cta((magic_link or (portal_url.rstrip("/") + "/provider")), "Open the upload portal →")
        + what_to_send
        + _p(
            "For your security, this is a "
            + _strong("temporary password")
            + f": you&rsquo;ll be required to reset it on first login, and this "
            f"invite expires in {int(invite_ttl_days)} days. If it lapses, ask your "
            "Archangel Health contact to re-send it.",
            muted=True,
            small=True,
        )
    )
    subject = "Your Archangel Health upload access is ready"
    return _shell(subject=subject, body_html=body)


def build_buyer_delivery_email(
    *,
    workspace_url: str,
    email: str,
    temporary_password: str,
    buyer_name: str = "",
    datasets_label: str = "",
    data_format: str = "",
    record_count: int = 0,
    note: str = "",
    invite_ttl_days: int = 14,
    first_delivery: bool = True,
) -> str:
    """Buyer data-delivery email — "Your dataset has been delivered." Carries the
    secure workspace URL + credentials (email + temporary password). The buyer
    opens the workspace with these credentials; every dataset sent to this email
    always appears there. Same brand + security posture as the provider invite
    (temporary, forced-reset, expiring password)."""
    rows = [
        ("Workspace", workspace_url, False),
        ("Email", email, True),
    ]
    if first_delivery:
        rows.append(("Temporary password", temporary_password, True))
    if datasets_label:
        rows.append(("Dataset", datasets_label, False))
    if data_format:
        rows.append(("Format", data_format, False))
    if record_count:
        rows.append(("Records", str(record_count), False))

    greeting = ("Hi " + _strong(buyer_name.strip()) + ", ") if (buyer_name or "").strip() else ""
    intro = (
        greeting
        + "a dataset has been exported to you by " + _strong("Archangel Health")
        + ". It&rsquo;s waiting in your secure workspace. Open it with the "
        "button below and it will always be there when you sign in."
    )
    if note:
        intro += " " + html.escape(note.strip())

    if first_delivery:
        security = _p(
            "For your security this is a " + _strong("temporary password")
            + f": you&rsquo;ll reset it on first sign-in, and this invite expires in "
            f"{int(invite_ttl_days)} days. Every future delivery to this email lands "
            "in the same workspace, no new account needed.",
            muted=True, small=True,
        )
    else:
        security = _p(
            "Sign in with your existing workspace password. This new dataset is "
            "already waiting alongside your previous deliveries.",
            muted=True, small=True,
        )

    body = (
        _eyebrow("Data delivery · Archangel Health")
        + _h1("Your dataset has been delivered.")
        + _p(intro)
        + _inset_card(_detail_rows(rows))
        + _cta(workspace_url, "Open your secure workspace →")
        + security
    )
    subject = "Your Archangel Health dataset is ready"
    return _shell(subject=subject, body_html=body)


def build_complete_email(
    *,
    director_email: str,
    org_name: str,
    department: str,
    member_count: int,
    temporary_password: str,
    workspace_url: str,
    rn_count: int = 0,
    nppa_count: int = 0,
) -> str:
    """Email 3 — welcome with full details inset and director temp password.

    `member_count` reflects the total `team_members` rows (post-finalize, this
    includes the director seat). `rn_count` and `nppa_count` describe the pod
    composition so the email matches the pass-4 4-person cap.
    """
    safe_org = (org_name or "your health system").strip()
    safe_dept = (department or "").strip()
    pod_total = max(member_count, 1)
    composition_bits = ["1 director (surgeon)"]
    if rn_count:
        composition_bits.append(f"{rn_count} RN coordinator")
    if nppa_count:
        composition_bits.append(f"{nppa_count} NP / PA" + ("s" if nppa_count != 1 else ""))
    composition = ", ".join(composition_bits)
    body = (
        _eyebrow("Onboarding complete")
        + _h1("Your workspace is ready.")
        + _p(
            html.escape(safe_org)
            + (" " + html.escape(safe_dept) if safe_dept else "")
            + " is live on Archangel Health. You can now open your patient roster, "
            "send discharge materials, and start tracking TEAM episodes."
        )
        + _inset_card(
            _detail_rows(
                [
                    ("Email", director_email, True),
                    ("Role", "Director of TEAM Initiative", False),
                    ("Health system", safe_org, False),
                    ("Department", safe_dept or "Not given", False),
                    (
                        "Pod",
                        f"{pod_total} of 4, {composition}",
                        False,
                    ),
                    ("Password (access key)", temporary_password, True),
                ]
            )
        )
        + _cta(workspace_url, "Open your workspace →")
        + _p(
            "Your team members have been sent their own credentials. Keep this "
            "email. Your password is your standing access key and stays valid for "
            "future sign-ins.",
            muted=True,
            small=True,
        )
    )
    return _shell(
        subject="Welcome to Archangel Health, onboarding is complete",
        body_html=body,
    )


# ─── Physician-facing emails that used to be authored inline ────────────────
# Each of these was a hand-rolled <div> in the router that sent it, which is how
# five different palettes ended up in production. They live here now so they
# inherit the shell like everything else.


def build_internal_signup_alert(*, physician_email: str, slug: str, expires_at: str) -> str:
    """Internal notice that someone started physician onboarding."""
    body = (
        _eyebrow("Internal · New signup")
        + _h1("A physician started onboarding.")
        + _inset_card(
            _detail_rows(
                [
                    ("Email", physician_email, True),
                    ("Pending row", slug, True),
                    ("Link expires", expires_at, True),
                ]
            )
        )
        + _p(
            "They requested a contributor onboarding link from the landing page. "
            "They will not appear on the roster until they finish the wizard.",
            muted=True,
            small=True,
        )
    )
    return _shell(subject=f"[Onboarding] Physician contributor started: {physician_email}", body_html=body)


# ─── Onboarding v2 §4: the four application emails ───────────────────────────
# Copy is part of the PRD and is used verbatim. Written in the founders' voice
# and signed by them, because a physician deciding whether to give us their
# evenings should be able to see who is asking. Every one of these renders in
# scripts/email_preview.py.


def build_application_start_email(
    *, first_name: str, onboarding_url: str, expires_days: int = 7
) -> str:
    """§4.1 — the link that starts and, more importantly, RESUMES an application.

    Replaces ``build_self_serve_link_email``. The distinction that changed: in v2
    the landing page drops the physician straight into the wizard, so this email
    is no longer the way in. It is the way BACK in, and the copy says so.
    """
    body = (
        _eyebrow("Your application")
        + _h1(f"{html.escape(_first_name(first_name))}, your application is open.")
        + _p("Archangel Health is paid clinical AI evaluation, on your schedule: "
             "you read real cases, you judge the answers, and the models learn from "
             "what you know.")
        + _cta(onboarding_url, "Continue your application")
        + _p(f"Your progress saves automatically, so you can stop anywhere and come "
             f"back to exactly where you were. This link is yours for {expires_days} days.",
             muted=True, small=True)
        + _founder_signoff("Tej and Aryaa, founders")
    )
    return _shell(subject="Pick up your Archangel Health application any time",
                  body_html=body)


def build_application_nudge_email(*, first_name: str, onboarding_url: str) -> str:
    """§4.2 — the ONE nudge, sent 24 hours after an unfinished start.

    No guilt language and no countdown, deliberately: the reason a physician
    stopped is almost always a pager, and a deadline is the wrong answer to that.
    Exactly one of these is ever sent — the scheduler is idempotent on a stamp.
    """
    body = (
        _eyebrow("Your application")
        + _h1(f"{html.escape(_first_name(first_name))}, you&rsquo;re nearly there.")
        + _p("You&rsquo;re most of the way there. Your answers are saved exactly where "
             "you left them.")
        + _cta(onboarding_url, "Finish my application")
        + _p("We read every application personally. We&rsquo;d love to see yours.")
        + _founder_signoff("Tej and Aryaa, founders")
    )
    return _shell(subject="Your application is waiting: 2 minutes to finish",
                  body_html=body)


def build_application_expiring_email(
    *, first_name: str, onboarding_url: str
) -> str:
    """§3 — the day-6 note, sent once, before a 7-day link dies.

    Not one of the four §4 emails and deliberately smaller than them: it exists
    so a link expires with warning rather than silently, and it says the one
    thing that is actually urgent without pretending anything else is.
    """
    body = (
        _eyebrow("Your application")
        + _h1("Your link expires tomorrow.")
        + _p(f"{_strong(_first_name(first_name))}, the link to your saved application "
             "stops working tomorrow. Everything you filled in is still there until "
             "then.")
        + _cta(onboarding_url, "Finish my application")
        + _p("If it lapses, just start again from the website and write to us, "
             "we&rsquo;ll pick it back up with you.", muted=True, small=True)
        + _founder_signoff("Tej and Aryaa, founders")
    )
    return _shell(subject="Your Archangel Health link expires tomorrow",
                  body_html=body)


def build_credentials_nudge_email(*, first_name: str, portal_url: str) -> str:
    """The credentials half of the post-submit nudge, sent once ever.

    An application with nothing to check it against cannot be reviewed at all,
    so this is the one nudge where the reason is worth stating plainly: the
    physician is not being chased for tidiness, they are waiting on a decision
    that literally cannot be made yet. Same restraint as the pre-submit nudge:
    one send, no countdown, no second chase.
    """
    body = (
        _eyebrow("Your application")
        + _h1("We still need something to check.")
        + _p(f"{_strong(_first_name(first_name))}, your application is with us, but "
             "we have nothing on file to verify you against yet. A CV, or your NPI "
             "or registration number, is all it takes.")
        + _cta(portal_url, "Add my credentials")
        + _p("Once that is there, one of us reviews your application personally.",
             muted=True, small=True)
        + _founder_signoff("Tej and Aryaa, founders")
    )
    return _shell(subject="One thing missing from your application",
                  body_html=body)


def build_practice_case_nudge_email(*, first_name: str, portal_url: str) -> str:
    """The practice-case half of the post-submit nudge, sent once ever.

    Deliberately framed as the interesting part rather than as homework: it is
    the thing most applicants enjoy, so the copy leads with what it is instead
    of with the fact that it is outstanding.

    IT NO LONGER CLAIMS TO BE WHAT WE READ. The examination is, and it says so
    in the mail below and on every screen. Two messages arriving days apart,
    each naming a different case as the decisive one, is how a physician ends
    up doing neither properly.
    """
    body = (
        _eyebrow("Your application")
        + _h1("Your practice case is waiting.")
        + _p(f"{_strong(_first_name(first_name))}, there is one short case sitting in "
             "your account. It takes about ten minutes, it is a real piece of "
             "clinical reasoning rather than a form, and physicians who do it "
             "find the examination afterwards much easier.")
        # Still the practice case — this mail IS the practice-case nudge, and it
        # says so in its own docstring. Only the link is corrected: it lands on
        # the applicant home, where the practice case is a row on card 1 and the
        # examination is the primary button, so a physician who opens it can do
        # either without being told the wrong thing about which one counts.
        + _cta(portal_url, "Open my account")
        + _p("No grade is published and there is no time limit on it.",
             muted=True, small=True)
        + _founder_signoff("Tej and Aryaa, founders")
    )
    return _shell(subject="Your practice case is waiting",
                  body_html=body)


EXAM_REMINDER_SUBJECT = "One last step for your application"
_EXAM_REMINDER_COPY = "Complete your examination case so we can review your application."


def _exam_reminder_greeting(name: str) -> str:
    name = " ".join((name or "").split())
    return f"Hi {name}," if name else "Hello,"


def build_exam_nudge_text(*, first_name: str, portal_url: str) -> str:
    return ("Archangel Health\n\nThis is the last step!\n\n"
            + _exam_reminder_greeting(first_name) + "\n\n" + _EXAM_REMINDER_COPY
            + "\n\nSign in & complete case: " + _exam_url(portal_url)
            + "\n\nUse the email and password you applied with.\n\n"
            + "Archangel Health · Physician applications")


def build_exam_nudge_email(*, first_name: str, portal_url: str) -> str:
    """Approved, brief 36-hour reminder. Same light palette as existing mail.

    Uses its own compact shell so other transactional messages retain their
    layout. System fonts, inline styles, table CTA, no image dependency.
    """
    greeting = html.escape(_exam_reminder_greeting(first_name))
    url = html.escape(_exam_url(portal_url), quote=True)
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="x-apple-disable-message-reformatting"><meta name="color-scheme" content="light"><meta name="supported-color-schemes" content="light">
<title>{EXAM_REMINDER_SUBJECT}</title>
<style>@media only screen and (max-width:420px) {{ .exam-pad {{ padding:31px 26px !important; }} .exam-heading {{font-size:31px !important;}} .exam-footer {{padding:15px 26px !important;}} }}</style>
</head><body style="margin:0;padding:0;background:{_CANVAS};font-family:{_SANS};color:{_INK};">
<div style="display:none;font-size:1px;line-height:1px;max-height:0;max-width:0;opacity:0;overflow:hidden;mso-hide:all;">{_EXAM_REMINDER_COPY}</div>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:{_CANVAS};"><tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="600" cellspacing="0" cellpadding="0" border="0" style="width:100%;max-width:600px;background:{_CARD};border:1px solid {_HAIRLINE};border-radius:12px;overflow:hidden;">
<tr><td class="exam-pad" style="padding:40px 44px 36px;">
<p style="margin:0 0 42px;font-family:{_MONO};font-size:11px;line-height:1.5;letter-spacing:1.6px;color:{_INK_SOFT};">ARCHANGEL HEALTH</p>
<h1 class="exam-heading" style="margin:0 0 25px;font-family:{_SANS};font-size:34px;line-height:1.15;font-weight:400;letter-spacing:-1px;color:{_INK};">This is the last step!</h1>
<p style="margin:0 0 10px;font-size:15px;line-height:1.6;color:{_INK};">{greeting}</p>
<p style="margin:0;max-width:390px;font-size:16px;line-height:1.65;color:{_INK_SOFT};">{_EXAM_REMINDER_COPY}</p>
<table role="presentation" cellspacing="0" cellpadding="0" border="0" style="margin:27px 0 13px;"><tr><td align="center" bgcolor="{_LIME}" style="border-radius:999px;background:{_LIME};mso-padding-alt:15px 24px;">
<a href="{url}" style="display:inline-block;padding:15px 24px;font-family:{_SANS};font-size:15px;line-height:1.35;font-weight:500;color:{_INK};background:{_LIME};border-radius:999px;text-decoration:none;">Sign in &amp; complete case&nbsp; →</a>
</td></tr></table>
<p style="margin:0;font-size:12px;line-height:1.65;color:{_INK_SOFT};">Use the email and password you applied with.</p>
</td></tr><tr><td class="exam-footer" style="padding:16px 44px;font-size:11px;line-height:1.5;color:{_INK_SOFT};background:{_CARD_IN};border-top:1px solid {_HAIRLINE};">Archangel Health · Physician applications</td></tr>
</table></td></tr></table></body></html>'''


def build_onboarding_started_email(*, first_name: str, portal_url: str) -> str:
    """A receipt, sent when somebody chooses to start onboarding now.

    Not a nudge and not on the sweep: it fires on the action, at the moment of
    the action, and its idempotency comes from the choice being first-write-wins
    rather than from a stamp column. A receipt that can arrive four hours late
    is not a receipt.

    It exists because the choice is the one place an applicant commits to
    something, and a commitment nobody acknowledges reads as a button that did
    nothing.
    """
    body = (
        _eyebrow("Your application")
        + _h1("Good. Here is what it involves.")
        + _p(f"{_strong(_first_name(first_name))}, you chose to get started rather "
             "than wait, which is the thing that actually moves your application "
             "along. Thank you.")
        + _p("There is a short explainer, then two optional pieces of help, then "
             "one examination case in your own specialty. About fifteen minutes "
             "in total, and you can stop at any point.")
        + _cta(portal_url, "Pick up where I left off")
        + _founder_signoff("Tej and Aryaa, founders")
    )
    return _shell(subject="Picking up your onboarding",
                  body_html=body)


def build_exam_received_email(*, first_name: str) -> str:
    """A receipt for the filed examination. No verdict, by construction.

    The rule the founders were explicit about: no applicant is told they are
    not ready, and nothing outside the admin console has an opinion. So this
    confirms arrival and says what happens next, and there is nothing in it a
    reader could mistake for a result.
    """
    body = (
        _eyebrow("Your examination")
        + _h1("It is with us.")
        + _p(f"{_strong(_first_name(first_name))}, your examination is filed. One of "
             "us reads it personally, alongside the credentials you sent.")
        + _p("Usually one to two business days, and we will write to you either "
             "way. There is nothing else you need to do.")
        + _founder_signoff("Tej and Aryaa, founders")
    )
    return _shell(subject="We have your examination",
                  body_html=body)


def build_profile_nudge_email(*, first_name: str, field_label: str,
                              profile_url: str) -> str:
    """ONE question about ONE missing profile field.

    The single-question rule is the whole design and it is a rule about
    restraint, not about layout. A list of gaps reads as a scorecard of what a
    physician has failed to do; one question reads as a colleague asking
    something specific, and it is answerable in the thirty seconds somebody
    actually has. The scheduler enforces the rest of the discipline: each field
    is asked about once ever, and nobody hears from us more than once a month.
    """
    asked = _scrub_dashes(field_label).strip().rstrip("?")
    body = (
        _eyebrow("Your profile")
        + _h1("One quick question.")
        + _p(f"{_strong(_first_name(first_name))}, would you add "
             f"{html.escape(asked)} to your profile?")
        + _p("It takes a moment, and it is what lets us route the right cases to "
             "you rather than the average ones.")
        + _cta(profile_url, "Add it to my profile")
        + _p("Nothing on your profile is required, and none of it gates your work.",
             muted=True, small=True)
        + _founder_signoff("Tej and Aryaa, founders")
    )
    return _shell(subject="One quick question about your profile",
                  body_html=body)


def build_application_submitted_email(*, full_name: str, portal_url: str = "") -> str:
    """§4.3 — sent the moment an application is submitted.

    The whole message is one paragraph in the founders' own words. It sets the
    24–48h expectation, and it explains WHY review is human, because that
    explanation is the product's argument about itself.

    ``portal_url`` is the way back in, and without it this email was the only
    message we send that contains no link at all. That was survivable while an
    applicant genuinely had nowhere to go; it stopped being survivable when the
    practice case became the thing the wait is FOR. Callers pass
    ``ASCLEPIUS_PORTAL_URL``-then-``BASE_URL`` + /asclepius, the same door the
    approval welcome opens, so the two mails never point at different hosts.
    Optional only so a preview or a test can render the copy without inventing
    a hostname; a caller that omits it sends the old dead end.
    """
    last = _last_name(full_name)
    # Unescaped here on purpose: ``_strong`` escapes what it is given, so
    # escaping first would render "O&#x27;Brien" to a physician named O'Brien.
    greeting = f"Dr. {last}" if last else "Doctor"
    # The practice case is the honest answer to "what do I do now", and it is
    # also evidence the reviewer reads, so the copy says both: it is waiting,
    # and it counts. Kept BELOW the review paragraph on purpose, so the message
    # still leads with the reassurance rather than with homework.
    # THE EXAMINATION, NAMED, AND A TRUE SENTENCE ABOUT SIGNING IN (§3.2 step 4).
    #
    # This block used to point at the practice case and tell the physician
    # "there is no password to remember yet: signing in is a single-use link we
    # email you". Both halves stopped being true. Screen 1 of the wizard takes
    # a password, so they have an ordinary credential; and no sign-in link is
    # minted at finish, so the link the sentence promised did not exist. A mail
    # that names the wrong case AND describes a door that was never built is
    # how an applicant ends up doing neither thing.
    #
    # The CTA carries the fragment so the card is focused on arrival rather than
    # leaving them to find it.
    waiting = (
        _section_label("While you wait")
        + _p("There is one short examination sitting in your account. It is one "
             "real case in your specialty, it takes about fifteen minutes, and "
             "it is the part of your application we read most closely.")
        + _cta(_exam_url(portal_url), "Open my examination")
        + _p("Sign in with the email and password you chose. If you forget it, "
             "use Forgot your password on the sign-in page.",
             muted=True, small=True)
    ) if portal_url else ""
    body = (
        _eyebrow("Application received")
        + _h1("We&rsquo;ve got your application.")
        + _p(f"{_strong(greeting)}, thank you. "
             "Your application is with us now, and one of us will personally review it "
             "within 24–48 hours. We keep review human on purpose: the whole premise of "
             "Archangel is that medicine needs qualified people at every decision point, "
             "and that starts with how we welcome physicians. You&rsquo;ll hear from us "
             "either way.")
        + waiting
        + _founder_signoff("Tej and Aryaa")
    )
    return _shell(subject="We&rsquo;ve got your application", body_html=body)


def application_welcome_subject(full_name: str) -> str:
    """The §4.4 subject line.

    Its own function because the mail transport needs the subject as an argument
    and the builder needs it for the document ``<title>``. Two spellings of one
    string is how a subject line and the page it opens drift apart.
    """
    last = _last_name(full_name)
    return f"Welcome to Archangel Health, Dr. {last}" if last \
        else "Welcome to Archangel Health"


def build_application_welcome_email(
    *, full_name: str, email: str, sign_in_url: str,
    temp_password: Optional[str] = None,
    calendly_url: str = FOUNDER_INTRO_CALENDLY,
    needs_password_setup: bool = False,
) -> str:
    """The personal acceptance letter for every approved physician.

    Delivery is owned by the verification-decision outbox, independent of tier.
    The optional temporary-password rendering remains available to existing
    callers; new acceptance sends use the existing password-recovery flow for
    legacy accounts, so no secret needs to be persisted in the mail queue.
    """
    from physician_welcome_email import render_welcome_email
    return render_welcome_email(
        full_name=full_name, subject=application_welcome_subject(full_name),
        email=email, sign_in_url=sign_in_url, calendly_url=calendly_url,
        temp_password=temp_password, needs_password_setup=needs_password_setup,
    )


def build_asclepius_approved_email(*, full_name: str, workspace_url: str,
                                   tier_word: str = "", can_review: bool = False,
                                   needs_password_setup: bool = False) -> str:
    """Compatibility entry point: labeling and reviewing get the same welcome."""
    return build_application_welcome_email(
        full_name=full_name, email="", sign_in_url=workspace_url,
        needs_password_setup=needs_password_setup,
    )


def build_asclepius_promoted_email(*, full_name: str, workspace_url: str,
                                   tier_word: str) -> str:
    """A physician's tier moved up after they were already approved.

    Sent on promotion only. A demotion gets no automated mail, deliberately:
    the reasons are specific to the work and belong in a conversation somebody
    has, not in a template that tells a physician their standing dropped and
    offers them nobody to ask about it.

    Says what opens and what it was earned by, and nothing about a score. The
    number that drove the decision is internal, and quoting it would both leak
    it and invite an argument about a figure the physician cannot inspect.
    """
    first = (full_name or "").strip() or "Doctor"
    body = (
        _eyebrow("Archangel Health")
        + _h1("You&rsquo;re now a reviewer.")
        + _p(
            f"{_strong(first)}, on the strength of the cases you have filed, your "
            f"account has been moved up to {_strong(tier_word)}."
        )
        + _p(
            "That opens the review queue. Alongside labeling cases in your specialty, "
            "you will now grade work other physicians have filed, which is the part of "
            "this that decides what we are able to ship."
        )
        + _cta(workspace_url, "Open your workspace →")
        + _p("Questions? Reply to this email and a person will read it.", muted=True, small=True)
    )
    return _shell(subject="You're now a reviewer on Archangel Health", body_html=body)


def build_asclepius_rejected_email(*, full_name: str, sign_in_url: str = "") -> str:
    """Credential verification did not pass.

    We send one, unlike the health-system refusal above, and the reasoning does
    not transfer: that one argues from deal size, where an automated rejection
    to a CIO costs a relationship a person could have kept. A rejected physician
    is an individual who was told they would hear from us within 24 hours, and
    who otherwise hears nothing while their account 403s forever with a message
    about being pending. We already made them a promise; silence breaks it, and
    the limbo is worse than the no.

    It gives NO reason, deliberately. The rejection note is mandatory and is
    written by an admin for an audit trail: it may carry an accusation, a
    suspicion, or a third party's name, none of which was drafted to be read by
    its subject. And a rejection that names the check that failed is a rejection
    an adversarial applicant tunes the next attempt against. The note stays in
    verification_notes.

    What HAS changed is the ending. This used to close the door ("your account
    will not receive case work"), which is a permanent answer to a question
    that is usually about one case read on one afternoon. A rejection now
    offers another go at the case work, with every credential they already gave
    us kept, so the message has somewhere to send them.
    """
    first = (full_name or "").strip() or "Doctor"
    body = (
        _eyebrow("Archangel Health")
        + _h1("About your application.")
        + _p(
            f"{_strong(first)}, thank you for applying to contribute to Archangel Health. Our "
            "clinical team has reviewed the credentials you submitted, and we are not "
            "able to open your account for evaluation work."
        )
        # THE DOOR IS OPEN, and this is the change.
        #
        # A rejected physician used to be told their account would never
        # receive case work, which was a permanent answer to a question that is
        # usually about one case read on one afternoon. They keep their
        # credentials, their CV and everything else they gave us; what they are
        # asked to do again is the case work, and the demo and practice case
        # are offered again first.
        + _p(
            "This is not final. Your account is still open, and everything you already "
            "sent us is still on it: your credentials, your CV, all of it. What we would "
            "like you to do again is the case work. Sign in and you will find the walk "
            "through and the practice case waiting, and a fresh examination case after "
            "them."
        )
        + _cta(sign_in_url, "Sign in and try again")
        + _p(
            "If you think we have this wrong, reply to this email. Tell us which licence "
            "or registration you would like us to check, and a person will look again. We "
            "keep the record of every decision, so a second look starts from what we "
            "already hold.",
            muted=True, small=True,
        )
        + _founder_signoff("Tej and Aryaa, founders")
    )
    # Not "You were rejected". That is the line they read on a phone in a
    # corridor, and the subject is not where the decision has to land.
    return _shell(subject="About your Archangel Health application", body_html=body)


def build_enterprise_note_email(
    *,
    sender_name: str,
    sender_email: str,
    specialty: str,
    organization: str,
    note: str,
) -> str:
    """Internal: a physician says their health system might sell data or
    partner on enterprise labeling. Straight to a founder inbox; every field
    is untrusted physician input and is escaped by the primitives."""
    rows = [
        ("Physician", sender_name or "Not given", False),
        ("Email", sender_email or "Not given", True),
        ("Specialty", specialty or "Not given", False),
        ("Organization", organization or "Not given", False),
    ]
    body = (
        _eyebrow("Internal · Enterprise")
        + _h1("A physician flagged a health-system deal.")
        + _inset_card(_detail_rows(rows))
        + _p(html.escape(_scrub_dashes(note)))
        + _p(
            "Sent from the Referral tab's health-system note card. Reply goes "
            "to you, not to the physician; reach them at the address above.",
            muted=True,
            small=True,
        )
    )
    return _shell(subject="Enterprise note", body_html=body)


def build_hs_referral_intro_email(
    *,
    contact_first_name: str,
    contact_role: str,
    hs_name: str,
    referrer_name: str,
    referrer_specialty: str,
    relationship: str,
    partner_url: str,
    enrichment_sentence: str = "",
) -> str:
    """The introduction a referred health-system contact actually receives.

    ─── Why this is not the physician invite with the nouns swapped ─────────
    ``build_asclepius_invite_email`` above sells paid evaluation work to a
    clinician: an hourly rate, async hours, your own schedule. Sent to a COO or a
    chief medical officer, every one of those sentences is aimed at the wrong
    person, they are not looking for shift work, and quoting an hourly rate to
    the executive who would be authorising their physicians' participation makes
    us look like we misread who we were writing to. So the offer here is
    institutional and two-pronged: license properly de-identified data, and
    separately, their physicians can earn on expert evaluation work. One leads,
    the other is the door left open.

    ─── No figure appears in this email, and that is a rule ────────────────
    ``docs/asclepius/REFERRALS.md`` records why the Referral tab prints no
    percentage for a health-system introduction: institutional terms are
    negotiated one deal at a time, so a number stated first and unprompted
    becomes a promise the negotiation then has to keep. Adding outreach to that
    flow does not change the reasoning. There is no rate, no introducer share,
    and no worked example in this body, and ``test_hs_referral_email`` asserts
    the absence rather than trusting this docstring.

    ``enrichment_sentence`` arrives already gated by
    ``hs_enrich.may_personalize`` and is dropped in verbatim as ONE sentence, or
    is empty. This function does not decide whether the research was good enough;
    it only decides where a sentence goes if there is one.
    """
    safe_org = html.escape(hs_name or "your health system")
    referrer = (referrer_name or "").strip()
    who = html.escape(contact_first_name or "there")

    # The referrer's name is the entire mechanism, exactly as on the physician
    # invite. With no name on file this degrades to neutral copy rather than
    # disclosing the referrer's address to a third party (referrals.py defect 3).
    if referrer:
        safe_referrer = html.escape(referrer)
        spec = html.escape((referrer_specialty or "").strip())
        who_line = safe_referrer + (f" ({spec})" if spec else "")
        opener = _p(
            _strong(who_line)
            + " suggested we get in touch, and mentioned "
            + html.escape((relationship or "you know each other").strip())
            + "."
        )
        subject = f"{referrer} suggested I reach out"
        exit_line = _p(
            f"{safe_referrer} asked us to reach out. If this isn&rsquo;t the right "
            "conversation for you, ignore this and we won&rsquo;t follow up.",
            muted=True, small=True,
        )
    else:
        opener = _p("A physician we work with suggested we get in touch.")
        subject = "An introduction to Archangel Health"
        exit_line = _p(
            "A colleague of yours asked us to reach out. If this isn&rsquo;t the "
            "right conversation for you, ignore this and we won&rsquo;t follow up.",
            muted=True, small=True,
        )

    role_line = ""
    if (contact_role or "").strip():
        role_line = _p(
            "We understand you&rsquo;re " + html.escape(contact_role.strip())
            + " at " + _strong(hs_name or "your health system") + "."
        )

    fact_line = ""
    if (enrichment_sentence or "").strip():
        fact_line = _p(html.escape(_scrub_dashes(enrichment_sentence.strip())))

    body = (
        _eyebrow("Introduction · Archangel Health")
        + _h1("Two ways health systems work with us.")
        + opener
        + role_line
        + fact_line
        + _p(
            "We&rsquo;re Archangel Health. We build the physician-graded data that "
            "frontier AI labs use to evaluate medical models, and we work with "
            "health systems in two directions."
        )
        + _inset_card(
            _p(
                _strong("License de-identified records.")
                + " Expert Determination de-identification, no PHI touches our "
                "systems, and we are DUA and BAA ready. The temporal structure "
                "clinicians actually reason over is preserved rather than stripped."
            )
            + _p(
                _strong("Your physicians can earn on evaluation work.")
                + " Flexible, remote, async review of medical-AI output in their "
                "own specialty, around clinic. Named credit on what we publish."
            )
        )
        + _p(
            "Either one is a reasonable place to start, and neither commits you to "
            "the other. Worth a short call to see whether one of them fits?"
        )
        + _cta(partner_url, "Tell us about your system →")
        + _p(
            "A few quick questions so the call starts from something real, then "
            "pick a time that works. Happy to loop in your compliance lead early.",
            muted=True, small=True,
        )
        + exit_line
    )
    return _shell(subject=subject, body_html=body)


def hs_referral_subject(referrer_name: str) -> str:
    """The subject line for :func:`build_hs_referral_intro_email`.

    Lives beside the builder rather than in the router so the two can never
    drift: the referrer's name carries the open, and a subject that stopped
    matching the first line of the body would quietly break the mechanism. The
    caller still runs it through ``header_safe`` before it reaches a header.
    """
    name = (referrer_name or "").strip()
    return f"{name} suggested I reach out" if name else "An introduction to Archangel Health"


def build_hs_referral_alert_email(
    *,
    referrer_name: str,
    referrer_email: str,
    contact_name: str,
    contact_email: str,
    contact_role: str,
    hs_name: str,
    relationship: str,
    note: str,
    enrich_state: str,
    enrich_summary: str,
    outcome: str,
) -> str:
    """Internal: a physician introduced a named health-system contact.

    Distinct from ``build_enterprise_note_email``, which stays exactly as it was
    for the no-contact-details note path. This one exists because the founder
    reading it now needs to know something that note never carried: an email
    went out to a third party, or deliberately did not, and which body they saw.
    Folding both into one template would mean the reader has to work out which
    kind of alert they are looking at.

    Every field is untrusted physician input and is escaped by the primitives.
    """
    rows = [
        ("Contact", contact_name or "Not given", False),
        ("Their email", contact_email or "Not given", True),
        ("Their role", contact_role or "Not given", False),
        ("Health system", hs_name or "Not given", False),
        ("Referred by", referrer_name or "Not given", False),
        ("Referrer email", referrer_email or "Not given", True),
        ("Relationship", relationship or "Not given", False),
    ]
    body = (
        _eyebrow("Internal · Health-system referral")
        + _h1("A physician introduced a health system.")
        + _inset_card(_detail_rows(rows))
        + _p(_strong("What we sent: ") + html.escape(outcome or "unknown"))
        + _p(
            _strong("Enrichment: ") + html.escape(enrich_state or "unknown")
            + (". " + html.escape(_scrub_dashes(enrich_summary)) if enrich_summary else "")
        )
        + (_p(_strong("Their note: ") + html.escape(_scrub_dashes(note))) if note else "")
        + _p(
            "Sent from the Referral tab&rsquo;s health-system card. The contact "
            "above was emailed on the physician&rsquo;s behalf with reply-to set "
            "to the physician, so a reply lands with them, not with you.",
            muted=True, small=True,
        )
    )
    return _shell(subject="Health-system referral", body_html=body)


def build_community_digest_email(
    *,
    activity_items: Iterable[Tuple[str, str]],
    community_url: str,
    unsubscribe_url: str = "",
) -> str:
    """Batched community activity: mentions, DMs, announcements, broadcasts.

    ``activity_items`` is (lead, detail) pairs of PLAIN text, e.g.
    ("Dr. Chen mentioned you", "the cardiology thread about troponin cutoffs").
    Escaping happens here; callers never hand this function HTML.
    """
    items = [
        (html.escape(_scrub_dashes(lead).strip()), html.escape(_scrub_dashes(rest).strip()))
        for lead, rest in activity_items
    ]
    body = (
        _eyebrow("Community · Archangel Health")
        + _h1("While you were away.")
        + _lead_list(items)
        + _cta(community_url, "Open the community →")
        + _p(
            "Colleague discussion only. No patient-identifiable information is "
            "permitted in the community.",
            muted=True,
            small=True,
        )
        + (
            _p(
                f'<a href="{html.escape(unsubscribe_url, quote=True)}" '
                f'style="color:{_GREEN_DEEP};">Stop these emails</a>.',
                muted=True,
                small=True,
            )
            if unsubscribe_url
            else ""
        )
    )
    return _shell(subject="New activity in your Archangel Health community", body_html=body)


def build_community_event_reminder_email(
    *,
    first_name: str,
    title: str,
    when_label: str,
    timezone_label: str,
    community_url: str,
    location: str = "",
    host: str = "",
) -> str:
    """An event the member marked Interested is starting soon."""
    rows = [("When", f"{when_label} ({timezone_label})", False)]
    if (location or "").strip():
        rows.append(("Where", location.strip(), False))
    if (host or "").strip():
        rows.append(("Host", host.strip(), False))
    body = (
        _eyebrow("Event · Archangel Health")
        + _h1(html.escape(title))
        + _p(f"Hi {_strong(first_name or 'there')}, this is starting soon.")
        + _inset_card(_detail_rows(rows))
        + _cta(community_url, "Open the community →")
        + _p(
            "You are getting this because you tapped Interested.",
            muted=True,
            small=True,
        )
    )
    return _shell(subject=f"Reminder: {title} is coming up", body_html=body)


def build_upload_failed_email(*, recipient_name: str, filename: str, reason: str) -> str:
    """An upload did not process. The job of this email is reassurance first."""
    body = (
        _eyebrow("Upload · Archangel Health")
        + _h1("Your upload did not go through.")
        + _p(
            f"Hi {_strong(recipient_name or 'there')}, we received your recent upload "
            f"({_strong(filename)}), but {html.escape(reason)}. "
            + _strong("It has not been ingested.")
        )
        + _inset_card(
            _p(
                _strong("Your data is safe.")
                + " Nothing was leaked and there was no data breach. The file simply did "
                "not make it through our intake, and any partial copy has been discarded."
            )
        )
        + _p(
            _strong("What to do next: ")
            + "please re-send the bundle using your secure upload link. If the link has "
            "expired or you need a fresh one, reply to this email and we will issue a new one."
        )
        + _p("Thanks for helping us get this right.", muted=True, small=True)
    )
    return _shell(subject="Your upload to Archangel Health didn't go through", body_html=body)


def build_asclepius_password_reset_email(*, email: str, reset_url: str, expires_minutes: int) -> str:
    """A reset link. Carries no credential and names no account detail beyond
    the address it was sent to, because it is mailed on request from anyone who
    can type an email address."""
    safe_url = html.escape(reset_url, quote=True)
    body = (
        _eyebrow("Password reset · Archangel Health")
        + _h1("Set a new password.")
        + _p(
            f"Use the button below to choose a new password for {_strong(email)}. "
            f"This link works once and expires in {_strong(str(expires_minutes) + ' minutes')}."
        )
        + _cta(reset_url, "Choose a new password →")
        + _p(
            f'If the button does not work, paste this into your browser:<br>'
            f'<a href="{safe_url}" style="color:{_GREEN_DEEP};">{html.escape(reset_url)}</a>',
            muted=True,
            small=True,
        )
        + _p(
            "If you did not ask for this, ignore this email. Your password has not "
            "changed and nobody has been given access to your account.",
            muted=True,
            small=True,
        )
    )
    return _shell(subject="Reset your Archangel Health password", body_html=body)


def build_asclepius_signin_link_email(*, signin_url: str, expires_minutes: int) -> str:
    """A way back in for an applicant who has no password yet.

    Names no account detail at all, not even the address it was sent to. This
    is mailed on request from anyone who can type an email address, and the
    fact worth protecting here is not "does this account exist" but "is this
    named physician waiting on a decision from us", which is a statement about
    their professional standing. So the body reads the same whether it reached
    an applicant, an approved physician, or nobody at all."""
    safe_url = html.escape(signin_url, quote=True)
    body = (
        _eyebrow("Sign in · Archangel Health")
        + _h1("Pick up where you left off.")
        + _p(
            "Use the button below to get back into your application. This link "
            f"works once and expires in {_strong(str(expires_minutes) + ' minutes')}."
        )
        + _cta(signin_url, "Sign in →")
        + _p(
            f'If the button does not work, paste this into your browser:<br>'
            f'<a href="{safe_url}" style="color:{_GREEN_DEEP};">{html.escape(signin_url)}</a>',
            muted=True,
            small=True,
        )
        + _p(
            "If you did not ask for this, ignore this email. Nobody has been "
            "given access to anything.",
            muted=True,
            small=True,
        )
    )
    return _shell(subject="Your Archangel Health sign in link", body_html=body)


def build_asclepius_password_changed_email(*, email: str) -> str:
    """Notification, not an action. This is the only channel by which a
    physician finds out their account was taken over, so it is sent on every
    password write and never suppressed."""
    body = (
        _eyebrow("Security · Archangel Health")
        + _h1("Your password was changed.")
        + _p(f"The password for {_strong(email)} has just been changed.")
        + _p(
            _strong("If this was not you")
            + ", reply to this email straight away. We will lock the account while "
            "we sort it out."
        )
    )
    return _shell(subject="Your Archangel Health password was changed", body_html=body)


def build_asclepius_admin_signup_alert(
    *,
    physician_name: str,
    email: str,
    specialty: str,
    decision: str,
    recommendation: str,
    reasons: Iterable[str] = (),
) -> str:
    """Internal: a physician signed up, and what the verification agent made of it.

    Sent for EVERY signup, enriched in place when the agent reports. Before this
    the only signal an admin got was the pending-count chip on a screen they had
    to already be looking at.
    """
    reason_list = [r for r in (reasons or []) if r]
    body = (
        _eyebrow("Internal · Verification")
        + _h1(f"{html.escape(decision)}: {html.escape(physician_name)}")
        + _inset_card(
            _detail_rows(
                [
                    ("Physician", physician_name, False),
                    ("Email", email, True),
                    ("Specialty", specialty or "Not given", False),
                    ("Decision", decision, False),
                ]
            )
        )
        + _p(html.escape(recommendation) if recommendation else "No recommendation recorded.")
        + (
            "<ul style=\"margin:0 0 16px;padding-left:20px;font-family:"
            + _SANS
            + f";font-size:14px;line-height:1.7;color:{_INK_SOFT};\">"
            + "".join(f"<li>{html.escape(r)}</li>" for r in reason_list)
            + "</ul>"
            if reason_list
            else ""
        )
        + _p(
            "Open the verification queue in the admin console to see the full "
            "dossier, including the NPPES record and the parsed CV.",
            muted=True,
            small=True,
        )
    )
    return _shell(subject=f"[Archangel Health] {decision}: {physician_name}", body_html=body)


def digest_email_subject(payload: Dict[str, Any]) -> str:
    """``Medical AI Digest · 4 items``.

    The count is in the subject because it is the one thing a physician can act
    on from the notification shade: four items is a minute, and a subject that
    said only "Medical AI Digest" every single day taught people to swipe it
    away without opening it.
    """
    from community import digest_contract  # noqa: PLC0415 - avoids an import cycle

    title = str((payload or {}).get("title") or digest_contract.DEFAULT_TITLE)
    # Counted the way the body counts, not off the raw list. A headline-less
    # item is drawn by nothing, so subjecting a mail "4 items" over a mail
    # showing three is the same "one post says two things about how many
    # stories it has" split -- in the one surface read before the post is even
    # opened.
    lead, rest = digest_contract.lead_and_rest(payload or {})
    n = (1 if lead else 0) + len(rest)
    return f"{title} · {n} item" + ("" if n == 1 else "s")


def build_community_digest_post_email(
    *,
    payload: Dict[str, Any],
    community_url: str,
    unsubscribe_url: str,
    first_name: str = "",
) -> str:
    """The digest, rendered from its STRUCTURE (Community News PRD §2.4).

    The old builder parsed a markdown-lite body the model had written, and the
    parse was necessarily lossy: ``**Medical AI Digest**`` is not a heading to
    an escaper, and ``[Opinion: ...](url)`` is not a link, so both arrived in
    physicians' inboxes as literal punctuation. Nothing was wrong with the
    parser. The input was prose, and prose is not a layout.

    Now the same object the web card renders is rendered here, in the same
    hierarchy (Digest Design PRD §1.3): the TOP STORY first with its deck and
    its why-it-matters, then the compact items, each a headline, one line and a
    link out. There is no markdown anywhere in the path, so there is nothing
    left to leak.

    The lead comes from ``digest_contract.lead_and_rest``, which is also what
    the web card and the pinned home card read. Deciding the lead here as well
    would be a second opinion about which story matters most, and the reader who
    opens the email and then the room is exactly the person who would find the
    two disagreeing.

    Sections no longer head the list. They are the tag beside each headline now,
    which is the same information in a line the reader was already going to
    read, and the redesign's governing rule is that an element earns its place
    by being removed and missed.

    Every interpolated string is escaped here. They were written by a model over
    somebody else's web page, which makes them exactly the untrusted input the
    escaping convention in ``_h1`` exists for.
    """
    from community import digest_contract  # noqa: PLC0415 - avoids an import cycle

    title = str((payload or {}).get("title") or digest_contract.DEFAULT_TITLE)
    parts: list[str] = [_eyebrow(title)]
    if first_name.strip():
        parts.append(_p(f"Morning {_strong(first_name.strip())}."))

    def _tag(item: Dict[str, Any]) -> str:
        section = html.escape(str(item.get("section") or ""))
        if not section:
            return ""
        return (
            f'<div style="font-family:{_MONO};font-size:11px;font-weight:500;'
            f'letter-spacing:0.09em;text-transform:uppercase;color:{_INK_FAINT};'
            'margin:0 0 6px;">'
            f"{section.upper()}</div>"
        )

    lead, rest = digest_contract.lead_and_rest(payload or {})

    if lead:
        url = html.escape(str(lead.get("url") or ""), quote=True)
        badge = "BREAKING" if lead.get("urgent") else "TOP STORY"
        deck = html.escape(str(lead.get("deck") or ""))
        why = html.escape(str(lead.get("why_it_matters") or ""))
        lead_tag = html.escape(str(lead.get("section") or "")).upper()
        lead_head = html.escape(str(lead.get("headline") or ""))
        lead_src = html.escape(str(lead.get("source") or ""))
        parts.append(
            f'<table role="presentation" width="100%" cellspacing="0" '
            f'cellpadding="0" border="0" style="margin:18px 0 6px;"><tr>'
            f'<td style="padding:0 0 0 14px;border-left:3px solid {_ORANGE};'
            f'font-family:{_SANS};">'
            f'<div style="font-family:{_MONO};font-size:11px;font-weight:600;'
            f'letter-spacing:0.09em;color:{_INK_FAINT};margin:0 0 8px;">'
            f'{badge} &nbsp;·&nbsp; {lead_tag}</div>'
            f'<a href="{url}" style="font-size:21px;font-weight:600;'
            f'line-height:1.3;color:{_INK};text-decoration:none;">{lead_head}</a>'
            + (f'<div style="margin-top:8px;font-size:15px;line-height:1.55;'
               f'color:{_INK_SOFT};">{deck}</div>' if deck else "")
            + (f'<div style="margin-top:10px;padding:8px 11px;'
               f'background:{_LIME_WASH};font-size:14px;line-height:1.5;'
               f'color:{_INK};">{why}</div>' if why else "")
            + f'<div style="margin-top:10px;font-size:13px;">'
              f'<a href="{url}" style="color:{_GREEN_DEEP};font-weight:600;'
              f'text-decoration:none;">Full article →</a>'
              f'<span style="font-family:{_MONO};font-size:11px;'
              f'letter-spacing:0.06em;text-transform:uppercase;'
              f'color:{_INK_FAINT};margin-left:10px;">{lead_src}</span></div>'
            "</td></tr></table>"
        )

    rows = []
    for item in rest:
        url = html.escape(str(item.get("url") or ""), quote=True)
        rows.append(
            f'<tr><td style="padding:15px 0;border-top:1px solid {_HAIRLINE};'
            f'font-family:{_SANS};">'
            + _tag(item)
            + f'<a href="{url}" style="font-size:15px;font-weight:600;'
              f'line-height:1.45;color:{_INK};text-decoration:none;">'
              f'{html.escape(str(item.get("headline") or ""))}</a>'
            f'<div style="margin-top:5px;font-size:14px;line-height:1.55;'
            f'color:{_INK_SOFT};">'
            f'{html.escape(str(item.get("why_it_matters") or ""))}</div>'
            f'<div style="margin-top:6px;font-size:13px;">'
            f'<a href="{url}" style="color:{_GREEN_DEEP};font-weight:600;'
            f'text-decoration:none;">Full article →</a>'
            f'<span style="font-family:{_MONO};font-size:11px;'
            f'letter-spacing:0.06em;text-transform:uppercase;'
            f'color:{_INK_FAINT};margin-left:10px;">'
            f'{html.escape(str(item.get("source") or ""))}</span></div>'
            "</td></tr>"
        )
    if rows:
        parts.append(
            '<table role="presentation" width="100%" cellspacing="0" '
            'cellpadding="0" border="0" style="margin:14px 0 6px;">'
            + "".join(rows) + "</table>"
        )

    parts.append(_cta(community_url, "Open the community →"))
    parts.append(_p(
        "You get this because you are an Archangel Health contributor. "
        f'<a href="{html.escape(unsubscribe_url, quote=True)}" '
        f'style="color:{_GREEN_DEEP};">Change how often, or stop these</a>.',
        muted=True,
    ))
    return _shell(subject=digest_email_subject(payload), body_html="".join(parts))


def build_community_news_digest_email(
    *,
    first_name: str,
    headline: str,
    body_markdown: str,
    community_url: str,
    unsubscribe_url: str,
) -> str:
    """The daily medical-AI digest, rendered from a markdown-lite body.

    NO LONGER ON THE DIGEST PATH. The compose pass returns structure now, and
    both the web card and the email are built from it by
    ``build_community_digest_post_email``. Kept because the parse below is the
    only thing that can render a digest written BEFORE that change — the bodies
    are still in the database and still markdown — and because deleting a
    tested renderer to save a function is how a migration loses its fallback.

    Carries a one-click unsubscribe in the body, not only in a header. A
    physician who cannot find how to stop a daily email marks it as spam
    instead, and one complaint costs the sending domain that every other
    physician's mail goes through.
    """
    # The digest body is composed by the model as light markdown, so it is
    # untrusted text arriving from the open web. Every branch below escapes
    # its line before any markup is added, so a story title containing a
    # bracket cannot inject markup. The layout is deliberately list-free:
    # bullet runs become _lead_list tables (bold lead phrase, plain remainder,
    # hairline between rows), headings become eyebrows, and anything else is a
    # paragraph. No <ul>, no bullet glyphs, no dash separators.
    parts: list[str] = []
    items: list[Tuple[str, str]] = []

    def _flush() -> None:
        if items:
            parts.append(_lead_list(items))
            items.clear()

    for raw in (body_markdown or "").split("\n"):
        line = _scrub_dashes(raw).strip().lstrip(",").strip()
        if not line:
            continue
        if line.startswith(("- ", "* ")):
            items.append(_split_lead(html.escape(line[2:].strip())))
            continue
        if line.startswith("•"):
            items.append(_split_lead(html.escape(line.lstrip("•").strip())))
            continue
        if line.startswith("#"):
            _flush()
            parts.append(_eyebrow(line.lstrip("#").strip()))
            continue
        _flush()
        parts.append(_p(html.escape(line)))
    _flush()
    inner = "".join(parts)

    safe_headline = _scrub_dashes(headline or "").strip() or "What moved in medical AI"
    body = (
        _eyebrow("Medical AI · Today")
        + _h1(html.escape(safe_headline))
        + _p(f"Morning {_strong(first_name or 'there')}.")
        + inner
        + _cta(community_url, "Discuss in the community →")
        + _p(
            f'You get this because you are an Archangel Health contributor. '
            f'<a href="{html.escape(unsubscribe_url, quote=True)}" '
            f'style="color:{_GREEN_DEEP};">Change how often, or stop these</a>.',
            muted=True,
            small=True,
        )
    )
    return _shell(subject=safe_headline, body_html=body)


def build_community_morning_email(
    *,
    first_name: Optional[str],
    sections: List[Dict[str, Any]],
    task_line: str,
    community_url: str,
    unsubscribe_url: str,
) -> str:
    """The daily morning email: what landed in this doctor's rooms overnight,
    and whether there is work waiting.

    The cards are the point. A physician should be able to decide from the
    email whether anything here is worth their time, which means the summary
    travels with the link rather than living behind it.

    Every string below arrives from an external web page by way of the model,
    so it is escaped before any markup is added, exactly as the news digest
    does with its composed body.
    """
    parts: List[str] = []

    if task_line:
        parts.append(_p(_strong(html.escape(_scrub_dashes(task_line)))))

    seen_channels: List[str] = []
    for section in sections or []:
        channel = str(section.get("channel") or "").strip()
        cards = section.get("cards") or []
        if not cards:
            continue
        if channel and channel not in seen_channels:
            seen_channels.append(channel)
            parts.append(_eyebrow(html.escape("#" + channel)))
        rows: List[Tuple[str, str]] = []
        for card in cards:
            title = _scrub_dashes(str(card.get("title") or "")).strip()
            if not title:
                continue
            url = str(card.get("url") or "").strip()
            description = _scrub_dashes(str(card.get("description") or "")).strip()
            meta = _scrub_dashes(str(card.get("meta") or "")).strip()
            lead = (
                f'<a href="{html.escape(url, quote=True)}" '
                f'style="color:{_GREEN_DEEP};text-decoration:none;">'
                f'{html.escape(title)}</a>'
                if url.lower().startswith(("http://", "https://"))
                else html.escape(title)
            )
            rest = " ".join(p for p in (html.escape(meta), html.escape(description)) if p)
            rows.append((lead, rest))
        if rows:
            parts.append(_lead_list(rows))

    if not parts:
        # run_newsletter refuses to send an empty one; this is the belt.
        parts.append(_p("Nothing new this morning."))

    body = (
        _eyebrow("Your morning")
        + _h1("What is new for you")
        + _p(f"Morning {_strong(html.escape(first_name or 'there'))}.")
        + "".join(parts)
        + _cta(community_url, "Open the community →")
        + _p(
            'You get this because you are an Archangel contributor. '
            f'<a href="{html.escape(unsubscribe_url, quote=True)}" '
            f'style="color:{_GREEN_DEEP};">Change how often, or stop these</a>.',
            muted=True,
            small=True,
        )
    )
    return _shell(subject="Your morning in Archangel", body_html=body)


# ─── Health-system portal: self-signup, intake, approval ─────────────────────
# The portal's second door. Everything here is addressed either to a hospital
# contact who just signed themselves up, or to us about one who did.


def build_hs_signup_code_email(*, code: str, organization: str,
                               expires_minutes: int = 15) -> str:
    """The six digits that turn a staged signup into an account."""
    body = (
        _eyebrow("Confirm your email")
        + _h1("Here is your code.")
        + _p(
            f"Enter this to finish setting up the upload portal for "
            f"{_strong(organization)}."
        )
        + _code_block(code, size=34)
        + _p(
            f"It expires in {expires_minutes} minutes. If you did not ask for "
            "this, you can ignore this message and nothing is created.",
            muted=True,
            small=True,
        )
    )
    return _shell(subject="Your Archangel Health confirmation code", body_html=body)


def build_hs_signup_welcome_email(*, organization: str, username: str,
                                  portal_url: str) -> str:
    """Sent once the code clears.

    This is where the username is delivered, and that matters more than it
    looks: a self-signup never chose one, we derived it from the organization
    name, and the sign-in form asks for it rather than for their email. If this
    mail is the only place it appears and it gets buried, they cannot get back
    in. It is also on screen at the end of signup and prefilled in the browser.
    """
    body = (
        _eyebrow("Upload portal")
        + _h1("Your portal is ready.")
        + _p(f"Welcome. This is the secure upload portal for {_strong(organization)}.")
        + _inset_card(
            _detail_rows([
                ("Sign in with", username, True),
                ("Password", "The one you just chose", False),
            ])
        )
        + _p(
            "Write the username down somewhere. You sign in with it rather than "
            "with your email address."
        )
        + _cta(portal_url, "Open the portal →")
        + _p(
            "You can look around now. Uploading opens once we have reviewed the "
            "account, which is usually the same day, and we will email you when "
            "it does.",
            muted=True,
            small=True,
        )
    )
    return _shell(subject="Your Archangel Health upload portal", body_html=body)


def build_hs_approved_email(*, organization: str, portal_url: str) -> str:
    body = (
        _eyebrow("Upload portal")
        + _h1("Uploading is open.")
        + _p(
            f"We have reviewed the portal account for {_strong(organization)} and "
            "the upload screen is now live."
        )
        + _cta(portal_url, "Upload data →")
        + _p(
            "Send a .zip, or individual .json, .csv, .hl7 or .txt files and we "
            "package them for you. Large files are sent in pieces and resume if "
            "the connection drops. Please make sure data is de-identified and "
            "date-shifted before it reaches us."
        )
    )
    return _shell(subject=f"Uploading is open for {organization}", body_html=body)


def build_hs_intake_alert(*, full_name: str, email: str, organization: str,
                          answers: "dict", hs_id: str) -> str:
    """To us, when a health system tells us who they are.

    Answers only, verbatim, in the order they were asked. No PHI reaches this
    by construction: the intake form asks a partner to DESCRIBE what they hold,
    and the portal never sends patient data through it.
    """
    rows = [
        ("Contact", full_name or "(not given)", False),
        ("Email", email or "(not given)", False),
        ("Organization", organization or "(not given)", False),
        ("Health system id", hs_id, True),
    ]
    labelled = [
        ("organization", "Who they are"),
        ("size_type", "Size"),
        ("data_held", "Data they hold"),
        ("licensable", "Open to licensing"),
        ("timeline", "Timeline"),
    ]
    parts = []
    for key, label in labelled:
        value = (answers or {}).get(key) or ""
        if not str(value).strip():
            continue
        parts.append(
            _p(f"{_strong(label)}<br>{html.escape(str(value)).replace(chr(10), '<br>')}")
        )
    body = (
        _eyebrow("Health system intake")
        + _h1(html.escape(organization) or "A health system told us about itself")
        + _inset_card(_detail_rows(rows))
        + ("".join(parts) or _p("They submitted the form without filling anything in.",
                                muted=True))
    )
    return _shell(subject=f"[Health system] Intake: {organization}", body_html=body)


def build_hs_signup_alert(*, full_name: str, email: str, organization: str,
                          hs_id: str, username: str,
                          name_collisions: "list" = None) -> str:
    """To us, when a health system signs itself up and is waiting on a decision."""
    rows = [
        ("Contact", full_name or "(not given)", False),
        ("Email", email or "(not given)", False),
        ("Organization", organization or "(not given)", False),
        ("Username", username, True),
        ("Health system id", hs_id, True),
    ]
    collision_note = ""
    if name_collisions:
        # The one thing an operator must not miss. create_health_system_unclaimed
        # deliberately refuses to merge by name, so a duplicate here is either a
        # second contact at a partner we already have or somebody typing a
        # hospital's name who does not work there. Both need a human.
        ids = ", ".join(name_collisions)
        collision_note = _inset_card(
            _p(
                f"{_strong('Another health system already uses this name.')}<br>"
                f"Existing: {html.escape(ids)}<br>"
                "This signup was given its own id and cannot see their uploads. "
                "Check who this is before approving."
            )
        )
    body = (
        _eyebrow("New health system")
        + _h1(html.escape(organization) or "A health system signed up")
        + _p("They signed themselves up through the portal and are waiting on a "
             "decision. Uploading is locked until someone approves it.")
        + _inset_card(_detail_rows(rows))
        + collision_note
    )
    return _shell(subject=f"[Health system] New signup: {organization}", body_html=body)


def build_founder_event_alert(*, eyebrow: str, headline: str, lede: str,
                              rows: "list" = None, note: str = "") -> str:
    """The generic internal alert for product events that need no special layout.

    Used by the notification hooks (a case submitted, a review finished, a
    referral made, a health system uploading). Deliberately plain: these arrive
    often, and every one of them is a glance rather than a read.
    """
    body = _eyebrow(eyebrow) + _h1(html.escape(headline)) + _p(html.escape(lede))
    if rows:
        body += _inset_card(_detail_rows(rows))
    if note:
        body += _p(html.escape(note), muted=True, small=True)
    return _shell(subject=headline, body_html=body)


# ─── Health-system onboarding (sign-in split → intake → DLA → uploads) ──────
# Five partner-facing letters and one internal alert. They are the entire
# outside-the-product voice of this flow, so they carry the same weight the
# physician letters do: the mission first, the mechanics second, and never a
# sentence that says "wait" without saying what for.

#: The block a health system reads before anything transactional, verbatim from
#: the mission page and from the physician letters. It is the same claim to the
#: same effect: the reason a hospital's records are worth licensing is that a
#: physician's judgment on them is scarce, and this is who is paying for it.
_MISSION_BLOCK = (
    _section_label("Our mission")
    + _p("Our mission is to help doctors earn from their judgment, models learn "
         "from it, and the hardest cases become the most valuable data.")
    + _p("The physicians who carry the consequences of care should define "
         "what correct means. Your records put that judgment to work.", muted=True)
)

#: WHERE THE CREDENTIALS CARD WENT. Both health-system letters below used to
#: render a derived username and a temporary passphrase, and both now send a
#: claim link instead: the recipient sets their own password on arrival and no
#: credential ever travels through email. That also fixed what the portal header
#: showed, which was the derived username ("Berkeley 2") because it was the only
#: identifier the account had.
#:
#: One door still mails a passphrase, and it is the one that cannot do this:
#: ``routers/asclepius_admin.py::_build_credentials_email``, for an account an
#: operator provisions from a call. Its own builder, in its own file, and it
#: stays that way.


def _bookmark_line(portal_url: str) -> str:
    """The literal instruction the PRD asks for. It reads like housekeeping and
    it is not: for a self-signup this mail is the only record of where the
    portal is, and a partner who cannot find the door does not ask, they wait
    for us to email them again."""
    return _p(
        f"Bookmark this email, your portal lives at "
        f"{_strong(portal_url.replace('https://', '').replace('http://', ''))}.",
        small=True,
    )


_SIGNED_OFF = _p("Tej and Aryaa<br>Archangel Health", muted=True, small=True)


def build_hs_access_email(*, organization: str, full_name: str, claim_url: str,
                          portal_url: str) -> str:
    """Email 1 of 5: sent the moment a health system clears its signup code.

    Sent immediately after the code verifies rather than at the end of intake,
    because the portal is reachable from that second and a session that is lost
    before this mail exists is an organization with no way back to it.

    It carries a CLAIM LINK, not a passphrase. This letter goes to the door
    where somebody signed up with three fields and chose no password, so the old
    version handed them a generated one and a username derived from their
    organization name. They then had to replace the password on first login and
    remember a username nobody picked, and the portal header greeted them by it.
    A link they follow once, and a password they type, removes all three.
    """
    greeting = f"{html.escape(full_name.strip())}," if (full_name or "").strip() else "Welcome."
    body = (
        _eyebrow("Your portal access")
        + _h1("Welcome to Archangel Health.")
        + _p(greeting)
        + _MISSION_BLOCK
        + _p(f"Your portal for {_strong(organization)} is open. It walks you "
             "through four questions about what your organization holds, and "
             "nothing in it commits you to anything until you sign an agreement.")
        + _p("Set a password of your own and it is yours. The link works once.")
        + _cta(claim_url, "Set up your account")
        + _bookmark_line(portal_url)
        + _SIGNED_OFF
    )
    return _shell(subject="Welcome to Archangel Health: your portal access",
                  body_html=body)


def build_hs_member_added_email(*, organization: str, added_by: str,
                                claim_url: str, portal_url: str,
                                awaiting_dla: bool = False) -> str:
    """Email 2 of 5: a colleague added you.

    Names who added them in the subject line and again in the first sentence. An
    unexpected email from a company you have not heard of, asking you to set a
    password, is indistinguishable from a phishing attempt; the name of a
    colleague is the single thing that makes it legible. That was true when this
    letter carried credentials and it is more true now that it carries a link.

    It says what the person is being added FOR, because the recipient usually
    did not fill the form in. Somebody else on their team did, and the reason
    they are being added is to look at those answers and join the team, so the
    letter says exactly that rather than leaving them to guess why a hospital
    data platform is writing to them.

    ``awaiting_dla`` closes a hole in the letter trail rather than adding a
    flourish. Email 3 goes to every member the moment the organization is
    approved; a member added AFTER that moment never existed when it was sent,
    so they arrive holding credentials and no idea that a contract is sitting
    unsigned. Same wording as ``build_hs_dla_request_email`` on purpose: two
    descriptions of one agreement is how a signer decides they are being asked
    for two things.
    """
    who = (added_by or "").strip() or "A colleague"
    agreement = ""
    if awaiting_dla:
        # Prose, and no second button. It used to carry its own "Read and sign"
        # CTA, which cannot work now: the agreement is behind a session and this
        # recipient has no account until they follow the link above. One door,
        # and the thing waiting behind it is described rather than linked.
        agreement = _p(
            "One thing is outstanding for your organization: the data licensing "
            "agreement is rendered and waiting for a signature. One person with "
            "signing authority signs it, once, on behalf of "
            f"{_strong(organization)}. You will find it in the portal, and "
            "uploading unlocks the moment it is signed.")
    body = (
        _eyebrow("Your portal access")
        + _h1(f"{html.escape(who)} added you.")
        + _p(f"{_strong(who)} added you to {_strong(organization)}'s Archangel "
             "Health workspace. You will have your own sign-in.")
        + _p("Somebody at your organization told us about the clinical data you "
             "hold. It may not have been you, and your teammate is adding you so "
             "you can read through what was submitted and join the team on it.")
        + _MISSION_BLOCK
        + _p("Set a password of your own and the account is yours. The link "
             "works once.")
        + _cta(claim_url, "Set up your account")
        + agreement
        + _bookmark_line(portal_url)
        + _SIGNED_OFF
    )
    return _shell(
        subject=f"{who} added you to {organization}'s Archangel Health workspace",
        body_html=body)


def build_hs_dla_request_email(*, organization: str, portal_url: str) -> str:
    """Email 3 of 5: approved, one signature away. Sent to EVERY member.

    Everyone is told; one person signs. The agreement binds the organization on
    one authorized signature, so the letter says who it needs rather than
    implying every recipient must act -- otherwise five people sign the same
    contract and we have five rows to explain.
    """
    body = (
        _eyebrow("Data licensing agreement")
        + _h1("One signature away.")
        + _p(f"We have reviewed what {_strong(organization)} told us and we would "
             "like to move ahead.")
        + _p("What is left is the data licensing agreement. Sign in with your "
             "existing credentials, read it in full on screen, and sign it "
             "there. Uploading unlocks the moment it is signed.")
        + _cta(portal_url, "Read and sign →")
        + _p("One person with signing authority for your organization signs it, "
             "once. Everyone else on your team is copied on this so nobody is "
             "waiting on a forward, and the portal shows who signed and when.",
             muted=True, small=True)
        + _SIGNED_OFF
    )
    return _shell(subject="One signature away: your data licensing agreement",
                  body_html=body)


def build_hs_agreement_receipt_email(*, organization: str, doc_version: str,
                                     signer_name: str, signer_title: str,
                                     signed_at: str, doc_sha256: str) -> str:
    """Email 4 of 5: the countersigned copy, to the signer and to us.

    This is a legal requirement rather than a courtesy. E-SIGN conditions the
    enforceability of an electronic record on the signer being able to RETAIN a
    copy of it, so the signed PDF is attached to this mail and the hash of the
    exact text signed is printed in the body -- a version label alone is a claim
    about a file that can be edited afterwards.
    """
    body = (
        _eyebrow("Signed agreement")
        + _h1("Your countersigned copy.")
        + _p(f"This confirms the data licensing agreement between "
             f"{_strong(organization)} and Archangel Health Inc. The signed PDF "
             "is attached to this email; keep it with your contract records.")
        + _inset_card(
            _detail_rows([
                ("Signed by", signer_name, False),
                ("Title", signer_title, False),
                ("Agreement", doc_version, True),
                ("Signed at (UTC)", signed_at, True),
                ("Document hash", (doc_sha256 or "")[:32] + "…", True),
            ])
        )
        + _p("The document hash is a fingerprint of the exact text that was on "
             "screen when it was signed. It is printed here so either party can "
             "prove, later, which words were agreed.", muted=True, small=True)
        + _SIGNED_OFF
    )
    return _shell(subject=f"Signed: your data licensing agreement, {organization}",
                  body_html=body)


def build_hs_uploads_open_email(*, organization: str, portal_url: str,
                                signer_name: str, signed_at: str) -> str:
    """Email 5 of 5: to every member, the moment the agreement is signed."""
    signed_line = (
        f"{_strong(signer_name)} signed the data licensing agreement for "
        f"{_strong(organization)} on {html.escape((signed_at or '')[:10])}."
        if (signer_name or "").strip()
        else f"The data licensing agreement for {_strong(organization)} is signed."
    )
    body = (
        _eyebrow("Uploads are open")
        + _h1("You can send data now.")
        + _p(signed_line)
        + _p("The upload screen is live for everyone on your team.")
        + _cta(portal_url, "Upload data →")
        + _p("Send a .zip, or individual files, and we package them for you. "
             "Large files are sent in pieces and resume if the connection "
             "drops, and every upload shows you its size and checksum once we "
             "have verified it. Please make sure data is de-identified and "
             "date-shifted before it reaches us.")
        + _SIGNED_OFF
    )
    return _shell(subject=f"Uploads are open for {organization}", body_html=body)


def build_hs_data_request_email(*, title: str, specialty_label: str,
                                case_count: int, due_date: str, details: str,
                                portal_url: str) -> str:
    """A data request, to every member of every partner who may upload.

    A plain what-we-need letter and nothing more: the specialty, the number, the
    date, and whatever the operator typed. It says outright that several partners
    are being asked and that we confirm what we take, because a request that
    reads as exclusive turns an invitation into a race, and the first partner to
    reply would be the only one who ever answered a second one.
    """
    noun = "case" if case_count == 1 else "cases"
    # A message-only request (Case Generation Fix PRD §B4) carries no specialty
    # and no count; the message is the request, and a card reading "Any · 0
    # cases" would say less than nothing.
    rows = []
    if (specialty_label or "").strip().lower() not in ("", "any"):
        rows.append(("Specialty", specialty_label, False))
    if int(case_count or 0) > 0:
        rows.append(("How many", f"{case_count} {noun}", False))
    if (due_date or "").strip():
        rows.append(("Useful by", due_date.strip()[:10], True))
    detail_block = ""
    if (details or "").strip():
        detail_block = _p(html.escape(details.strip()).replace("\n", "<br>"))
    body = (
        _eyebrow("Data request")
        + _h1(html.escape(title))
        + _p("We are looking for de-identified cases from our partner health "
             "systems, and this is what we need right now.")
        + (_inset_card(_detail_rows(rows)) if rows else "")
        + detail_block
        + _cta(portal_url, "Upload in your portal →")
        + _p("Several partners are being asked for this, and more than one may "
             "send cases. Nothing is reserved and nothing is first come first "
             "served: our team reviews what arrives and confirms what we accept. "
             "If you have nothing that fits, no reply is needed.",
             muted=True, small=True)
        + _p("Please make sure data is de-identified and date-shifted before it "
             "reaches us.", muted=True, small=True)
        + _SIGNED_OFF
    )
    return _shell(subject=f"Data request: {title}", body_html=body)


def build_hs_application_alert(*, organization: str, hs_id: str, full_name: str,
                               email: str, answers: "list",
                               members: "list" = None) -> str:
    """Internal: a health system finished the four questions.

    The four answers VERBATIM, in the order they were asked, because the whole
    point of a structured intake is that the operator reads what they actually
    chose rather than a summary of it. ``answers`` arrives as (label, value)
    pairs already resolved to their human wording by the router that owns the
    question list.
    """
    rows = [
        ("Contact", full_name or "(not given)", False),
        ("Email", email or "(not given)", False),
        ("Organization", organization or "(not given)", False),
        ("Health system id", hs_id, True),
    ]
    answer_rows = [(label, value or "(not answered)", False)
                   for label, value in (answers or [])]
    member_block = ""
    if members:
        member_block = (
            _p(_strong("Team members on the account"))
            + _lead_list([(html.escape(str(m)), "") for m in members])
        )
    body = (
        _eyebrow("Health system application")
        + _h1(html.escape(organization) or "A health system applied")
        + _p("They answered the four questions. Nothing is approved and nothing "
             "can be uploaded until someone decides.")
        + _inset_card(_detail_rows(rows))
        + _inset_card(_detail_rows(answer_rows))
        + member_block
    )
    return _shell(subject=f"[Health system] Application: {organization}",
                  body_html=body)


# ─── /partner, before there is an account ───────────────────────────────────
# These two are the only letters a health system gets BEFORE it has a portal at
# all. They exist because the /partner form used to end at a Calendly link on
# its own success screen, and a CIO who did not click it in that second was
# never heard from again: nothing was sent, so there was nothing to reply to and
# nothing to follow up. The booking now lives here instead, where it can be
# forwarded to whoever actually holds the calendar.


def _partner_call_path(booking_url: str) -> str:
    """The three sentences that say what a call is FOR.

    One block, shared by both letters, because a reminder that describes the
    process differently from the letter it is reminding you of reads as a
    second, different ask. Same rule ``build_hs_member_added_email`` follows
    with the agreement wording.
    """
    return (
        _p("A short call is how a partner gets verified. We go through what you "
           "hold, how it is de-identified, and what you would be able to "
           "license.")
        + _p("The data licensing agreement and your partner access follow that "
             "call. Nothing moves and no data is shared before it is signed.")
        + _cta(booking_url, "Book a time with us")
    )


def build_hs_interest_thanks_email(*, full_name: str, organization: str,
                                   booking_url: str) -> str:
    """Sent the moment a health system submits the /partner interest form.

    The form's success screen says thank you and nothing else, deliberately: the
    booking control was taken off it so that the one place to book is a message
    the recipient keeps. That makes this letter load-bearing rather than a
    courtesy. If it does not arrive, the lead has no way forward at all, which
    is why ``submit_lead`` stamps ``thanks_sent_at`` only on a send that
    actually happened and why the reminder is gated on that stamp.
    """
    greeting = (f"{_strong(full_name.strip())}," if (full_name or "").strip()
                else "Hello,")
    org = (organization or "").strip()
    about = (f"We would love to understand more about what {_strong(org)} holds "
             "and the scope of it." if org else
             "We would love to understand more about what you hold and the "
             "scope of it.")
    body = (
        _eyebrow("Health systems")
        + _h1("Thank you for submitting.")
        + _p(greeting)
        + _p("We read every one of these ourselves. " + about)
        + _partner_call_path(booking_url)
        + _p("If none of the times work, reply to this email and we will find "
             "one. If someone else at your organization should be on the call, "
             "forward this to them.", muted=True, small=True)
        + _SIGNED_OFF
    )
    return _shell(subject="Thank you for submitting", body_html=body)


def build_hs_interest_reminder_email(*, full_name: str, organization: str,
                                     booking_url: str) -> str:
    """The ONE reminder, sent by ``asclepius/partner_lead_nudge.py``.

    Short, and short on purpose. The recipient already has the long version;
    what they do not have is a time in their calendar, so this is the same link
    with as little around it as the sentence can carry. There is no second
    reminder, ever: at this deal size a person who has not booked after two
    letters is telling us something, and a third is how a partnership
    conversation becomes a spam complaint.
    """
    first = _first_name(full_name) if (full_name or "").strip() else ""
    greeting = f"{_strong(first)}," if first else "Hello,"
    org = (organization or "").strip()
    subject_org = f" about {org}" if org else ""
    body = (
        _eyebrow("Health systems")
        + _h1("Still keen to talk.")
        + _p(greeting)
        + _p("You wrote to us about licensing clinical data and we have not "
             "found a time yet. The call is twenty minutes and it is where we "
             "work out whether there is something here for both of us.")
        + _cta(booking_url, "Book a time with us")
        + _p("This is the only reminder we will send. If the timing is wrong, "
             "reply and tell us when to come back.", muted=True, small=True)
        + _SIGNED_OFF
    )
    return _shell(subject=f"A time to talk{subject_org}", body_html=body)
