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


def _founder_signoff(line: str) -> str:
    """The founders sign their own emails. Set slightly apart from the body so it
    reads as a signature and not as one more paragraph."""
    return (
        f'<p style="margin:26px 0 0;padding-top:20px;border-top:1px solid {_HAIRLINE};'
        f'font-family:{_SANS};font-size:15px;line-height:1.6;color:{_INK};">'
        f"{html.escape(line)}</p>"
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
#: constant, because it appears in the welcome email and the walkthrough and the
#: two must never drift.
FOUNDER_INTRO_CALENDLY = "https://calendly.com/tejpatel-berkeley/intro-with-tej-patel"


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
        _eyebrow("Invitation · Asclepius")
        + _h1(f"You&rsquo;re invited to contribute to {org_spec_label}.")
        + referral_line
        + _p(
            f"Hello {html.escape(invitee_first_name or 'there')}, "
            + _strong(director_full_name or "your director")
            + " has invited you to join "
            + _strong((org_name or "your organization"))
            + " on Asclepius, Archangel Health&rsquo;s expert data-training product, where "
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
        _eyebrow("Invitation · Asclepius")
        + _h1(f"Welcome to Asclepius, {html.escape(invitee_name or 'there')}.")
        + _p(
            "You have been invited to join Asclepius, Archangel Health&rsquo;s expert "
            "data-training product, where physicians review and label AI answers in "
            "their specialty."
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
) -> str:
    """Asclepius workspace-ready email — same visual format as the clinical
    completion email, addressed to the data-training product.

    ``temporary_password`` is the person&rsquo;s permanent, standing access key
    (kwarg name kept for parity with the clinical builders)."""
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
        + " is live on Asclepius. You can now open your training console, pick up "
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
        _eyebrow("Onboarding complete · Asclepius")
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
    )
    return _shell(subject="Your Asclepius workspace is ready", body_html=body)


def build_asclepius_task_notification_email(
    *, specialty_label: str, task_count: int, workspace_url: str,
) -> str:
    """New-work ping for evaluators when an admin uploads a specialty-tagged
    task batch. Deliberately content-free — specialty name, count, and a login
    URL only, no case text or PHI."""
    plural = "task" if task_count == 1 else "tasks"
    is_are = "is" if task_count == 1 else "are"
    body = (
        _eyebrow("New work · Asclepius")
        + _h1(f"{task_count} new {html.escape(specialty_label)} {plural} ready.")
        + _p(
            f"{task_count} new {html.escape(specialty_label)} {plural} "
            f"{is_are} ready to review in your Asclepius workspace."
        )
        + _cta(workspace_url, "Open my workspace →")
    )
    subject = f"{task_count} new {specialty_label} {plural} ready in your Asclepius workspace"
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
        + _founder_signoff("— Tej & Aryaa, founders")
    )
    return _shell(subject="Your Archangel Health application — pick up any time",
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
        + _p("We read every application personally — we&rsquo;d love to see yours.")
        + _founder_signoff("— Tej & Aryaa, founders")
    )
    return _shell(subject="Your application is waiting — 2 minutes to finish",
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
        + _p("If it lapses, just start again from the website and write to us — "
             "we&rsquo;ll pick it back up with you.", muted=True, small=True)
        + _founder_signoff("— Tej & Aryaa, founders")
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

    Deliberately framed as the interesting part rather than as homework. It is
    the only clinical judgment we get to see before deciding about somebody,
    and it is also the thing most applicants enjoy, so the copy leads with what
    it is instead of with the fact that it is outstanding.
    """
    body = (
        _eyebrow("Your application")
        + _h1("Your practice case is waiting.")
        + _p(f"{_strong(_first_name(first_name))}, there is one short case sitting in "
             "your account. It takes about ten minutes, it is a real piece of "
             "clinical reasoning rather than a form, and it is the part of your "
             "application we read most closely.")
        + _cta(portal_url, "Open my practice case")
        + _p("No grade is published and there is no time limit on it.",
             muted=True, small=True)
        + _founder_signoff("Tej and Aryaa, founders")
    )
    return _shell(subject="Your practice case is waiting",
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


def build_application_submitted_email(*, full_name: str) -> str:
    """§4.3 — sent the moment an application is submitted.

    The whole message is one paragraph in the founders' own words. It sets the
    24–48h expectation, and it explains WHY review is human, because that
    explanation is the product's argument about itself.
    """
    last = _last_name(full_name)
    # Unescaped here on purpose: ``_strong`` escapes what it is given, so
    # escaping first would render "O&#x27;Brien" to a physician named O'Brien.
    greeting = f"Dr. {last}" if last else "Doctor"
    body = (
        _eyebrow("Application received")
        + _h1("We&rsquo;ve got your application.")
        + _p(f"{_strong(greeting)} — thank you. "
             "Your application is with us now, and one of us will personally review it "
             "within 24–48 hours. We keep review human on purpose: the whole premise of "
             "Archangel is that medicine needs qualified people at every decision point, "
             "and that starts with how we welcome physicians. You&rsquo;ll hear from us "
             "either way.")
        + _founder_signoff("— Tej Patel & Aryaa Bhatia")
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
    *,
    full_name: str,
    email: str,
    temp_password: str,
    sign_in_url: str,
    calendly_url: str = FOUNDER_INTRO_CALENDLY,
) -> str:
    """§4.4 — sent on admin approval. Carries the credentials.

    The password in this email is TEMPORARY and is rotated on first sign-in
    (§0.1 decision 1). That is the one place this build departs from the ask, and
    it departs in the physician's favour: the doctor experience is identical —
    credentials in the email, sign in from the website, works first time — but a
    credential that lives in an inbox forever does not survive an inbox breach,
    and neither does the answer we give a hospital partner who asks.
    """
    subject = application_welcome_subject(full_name)
    creds = _detail_rows([
        ("Email", email, True),
        ("Temporary password", temp_password, True),
    ])
    body = (
        _eyebrow("Approved · Archangel Health")
        + _h1("We just approved your application — welcome.")
        # §4.4 section 2: the mission block, verbatim from the landing /mission.
        + _pullquote("Doctors earn from their judgment. Models learn from it. "
                     "The hardest cases become the most valuable data.")
        + _p("Verification is the scarce input in medical AI. A 70% benchmark score is "
             "irrelevant when a patient is downstream — the people who carry the "
             "consequences should define what correct means. That&rsquo;s you.")
        # §4.4 section 3: the credentials card.
        + _section_label("Your credentials")
        + _inset_card(creds)
        + _p("You&rsquo;ll choose your own password when you first sign in — this one is "
             "temporary and stops working the moment you do.", muted=True, small=True)
        + _cta(sign_in_url, "Sign in")
        # §4.4 section 4: meet us.
        + _section_label("Meet us")
        + _p("We meet every physician one on one — it&rsquo;s the part of this we like "
             "most. Book 20 minutes with us: about the mission, the platform, your "
             "specialty, or anything else. We&rsquo;d genuinely love to learn from you.")
        + _cta(calendly_url, "Book 20 minutes")
        + _founder_signoff("— Tej & Aryaa, co-founders")
    )
    return _shell(subject=subject, body_html=body)


def build_asclepius_approved_email(*, full_name: str, workspace_url: str,
                                   tier_word: str = "", can_review: bool = False) -> str:
    """Credential verification passed and the account is open for real work.

    Names the tier, framed as what work opens rather than as a rank. It is the
    decision, it is written in the same transaction, and it decides which queue
    is populated: a Labeler who expected Reviewer used to find out from an empty
    review queue, which reads as a broken product rather than as a decision
    somebody made.

    There is deliberately no promotion sentence HERE, and that is now a
    different decision than it used to be. It used to mean there was no
    promotion mechanism at all, so implying one was a promise the codebase
    could not keep. A promotion flow exists now
    (``build_asclepius_promoted_email`` below), and this email still does not
    mention it: on the day somebody is approved, what work opens today is the
    useful thing to say, and a sentence about a rung above the one they just
    reached reads as a hint that this one is not enough.

    ``tier_word`` is a WORD, resolved by the caller. Empty means the paragraph
    is omitted entirely rather than rendering "Unassigned" at a physician:
    restore_physician can approve carrying no tier, and a placeholder in a
    congratulations email is worse than silence about it.
    """
    first = (full_name or "").strip() or "Doctor"
    tier_para = ""
    if (tier_word or "").strip():
        tier_para = _p(
            f"You are approved as a {_strong(tier_word)}. "
            + ("That opens both queues. You can label cases in your specialty, and "
               "you can review the work other physicians have filed."
               if can_review else
               "Cases in your specialty are in your queue now. You read each one and "
               "record your clinical judgment, and every case you file is graded.")
        )
    body = (
        _eyebrow("Verified · Asclepius")
        + _h1("You&rsquo;re approved.")
        + _p(
            f"{_strong(first)}, your credentials have been verified and your Asclepius "
            "account is now open for evaluation work."
        )
        + tier_para
        + _cta(workspace_url, "Open your workspace →")
        + _p(
            "Your seat in the "
            + _strong("Asclepius Community")
            + " is open too, a private space for verified physicians. Find it in the "
            "side panel: introduce yourself, follow the medical-AI digest, and meet the "
            "colleagues you will be working alongside."
        )
        + _p("Questions? Reply to this email and a person will read it.", muted=True, small=True)
    )
    return _shell(subject="You're approved for Asclepius", body_html=body)


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
        _eyebrow("Asclepius")
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
    return _shell(subject="You're now a reviewer on Asclepius", body_html=body)


def build_asclepius_rejected_email(*, full_name: str) -> str:
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
    verification_notes; the door back is a reply to a person.
    """
    first = (full_name or "").strip() or "Doctor"
    body = (
        _eyebrow("Asclepius")
        + _h1("About your application.")
        + _p(
            f"{_strong(first)}, thank you for applying to contribute to Asclepius. Our "
            "clinical team has reviewed the credentials you submitted, and we are not "
            "able to open your account for evaluation work."
        )
        + _p(
            "If you believe that is wrong, reply to this email. Tell us which licence or "
            "registration you would like us to check, and a person will look again. We "
            "keep the record of every decision, so a second look starts from what we "
            "already hold."
        )
        + _p(
            "Your account will not receive case work. We will not email you again about "
            "this unless you reply.",
            muted=True, small=True,
        )
    )
    # Not "You were rejected". That is the line they read on a phone in a
    # corridor, and the subject is not where the decision has to land.
    return _shell(subject="About your Asclepius application", body_html=body)


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
            "Five quick questions so the call starts from something real, then "
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
        _eyebrow("Community · Asclepius")
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
    return _shell(subject="New activity in your Asclepius community", body_html=body)


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
        _eyebrow("Event · Asclepius")
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
        _eyebrow("Password reset · Asclepius")
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
        _eyebrow("Security · Asclepius")
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
    return _shell(subject=f"[Asclepius] {decision}: {physician_name}", body_html=body)


def build_community_news_digest_email(
    *,
    first_name: str,
    headline: str,
    body_markdown: str,
    community_url: str,
    unsubscribe_url: str,
) -> str:
    """The daily medical-AI digest.

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
            f'You get this because you are an Asclepius contributor. '
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
    _p(f"{_strong('Doctors earn from their judgment. Models learn from it.')}<br>"
       "The hardest cases become the most valuable data.")
    + _p("Verification is the scarce input in medical AI. A 70% benchmark score "
         "is irrelevant when a patient is downstream, and the people who carry "
         "the consequences should define what correct means. Your records are "
         "where that judgment gets exercised.")
)

#: Rendered under every credentials card. The password in this mail is a
#: one-time credential and the letter has to say so in the same breath it hands
#: it over -- §0.1.1, and the same compromise the physician onboarding makes.
_TEMP_PASSWORD_NOTE = (
    "This password is temporary. You will choose your own the first time you "
    "sign in, and this one stops working the moment you do."
)


def _credentials_card(*, username: str, temp_password: str) -> str:
    return _inset_card(
        _detail_rows([
            ("Sign in with", username, True),
            ("Temporary password", temp_password, True),
        ])
        + _p(_TEMP_PASSWORD_NOTE, muted=True, small=True)
    )


def _bookmark_line(portal_url: str) -> str:
    """The literal instruction the PRD asks for. It reads like housekeeping and
    it is not: the username is derived rather than chosen, so for a self-signup
    this mail is the only record of how to get back in."""
    return _p(
        f"Bookmark this email, your portal lives at "
        f"{_strong(portal_url.replace('https://', '').replace('http://', ''))}.",
        small=True,
    )


_SIGNED_OFF = _p("Tej &amp; Aryaa<br>Archangel Health", muted=True, small=True)


def build_hs_access_email(*, organization: str, full_name: str, username: str,
                          temp_password: str, portal_url: str) -> str:
    """Email 1 of 5: sent the moment a health system clears its signup code.

    Sent immediately after the code verifies rather than at the end of intake,
    because the portal is reachable from that second and a session that is lost
    before this mail exists is an organization with no way back to it.
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
        + _credentials_card(username=username, temp_password=temp_password)
        + _cta(portal_url, "Open your portal →")
        + _bookmark_line(portal_url)
        + _SIGNED_OFF
    )
    return _shell(subject="Welcome to Archangel Health: your portal access",
                  body_html=body)


def build_hs_member_added_email(*, organization: str, added_by: str, username: str,
                                temp_password: str, portal_url: str,
                                awaiting_dla: bool = False) -> str:
    """Email 2 of 5: a colleague added you.

    Names who added them in the subject line and again in the first sentence. An
    unexpected credentials email from a company you have not heard of is
    indistinguishable from a phishing attempt; the name of a colleague is the
    single thing that makes it legible.

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
        agreement = (
            _p("One thing is outstanding for your organization: the data "
               "licensing agreement is rendered and waiting for a signature. "
               "One person with signing authority signs it, once, on behalf of "
               f"{_strong(organization)}. Uploading unlocks the moment it is "
               "signed.")
            + _cta(portal_url, "Read and sign →")
        )
    body = (
        _eyebrow("Your portal access")
        + _h1(f"{html.escape(who)} added you.")
        + _p(f"{_strong(who)} added you to {_strong(organization)}'s Archangel "
             "Health workspace. You have your own sign-in below.")
        + _MISSION_BLOCK
        + _credentials_card(username=username, temp_password=temp_password)
        + _cta(portal_url, "Open your portal →")
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
    rows = [
        ("Specialty", specialty_label, False),
        ("How many", f"{case_count} {noun}", False),
    ]
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
        + _inset_card(_detail_rows(rows))
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
