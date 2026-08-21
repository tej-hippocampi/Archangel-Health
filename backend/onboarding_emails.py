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
from typing import Iterable, Tuple

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
    """Scale, not boldness: weight 400, negative tracking, ink."""
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
    return _shell(subject="Verify your email — Archangel Health", body_html=body)


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
        eyebrow, headline, lede = (
            "Escalation",
            "This still needs your review.",
            "An item assigned to you in CareGuide has been waiting for a while. "
            "Please take a look when you can.",
        )
    elif is_reminder:
        eyebrow, headline, lede = (
            "Reminder",
            "You have a pending item.",
            "You have a new item to review in CareGuide that hasn&rsquo;t been "
            "opened yet.",
        )
    else:
        eyebrow, headline, lede = (
            "New item",
            "You have a new item to review.",
            "You have a new item to review in CareGuide.",
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
    return _shell(subject=f"CareGuide — {headline}", body_html=body)


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
            "Keep this email — your password is your standing access key and stays "
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
    rows.append(("Organization", org_name or "—", False))
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
            + " on Asclepius — Archangel Health&rsquo;s expert data-training product, where "
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
        ("Specialty", safe_spec or "—", False),
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
            + " — you&rsquo;ll hear from us within 24 hours. Your account opens "
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
            + " — everything you need to contribute data lives here. Your password is "
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
        "<code>specialty</code>) — it makes ingestion far more reliable. "
        + _strong("No imaging."),
        small=True,
    )

    intro = (
        "You&rsquo;ve been invited to securely send your de-identified clinical "
        "data to " + _strong("Archangel Health") + ". Your upload portal is ready "
        "and a locked-down account has been created for you — the credentials are "
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
    subject = "Send us your clinical data — your Archangel Health upload access"
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
        + ". It&rsquo;s waiting in your secure workspace — open it with the "
        "button below and it will always be there when you sign in."
    )
    if note:
        intro += " " + html.escape(note.strip())

    if first_delivery:
        security = _p(
            "For your security this is a " + _strong("temporary password")
            + f": you&rsquo;ll reset it on first sign-in, and this invite expires in "
            f"{int(invite_ttl_days)} days. Every future delivery to this email lands "
            "in the same workspace — no new account needed.",
            muted=True, small=True,
        )
    else:
        security = _p(
            "Sign in with your existing workspace password — this new dataset is "
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
                    ("Department", safe_dept or "—", False),
                    (
                        "Pod",
                        f"{pod_total} of 4 — {composition}",
                        False,
                    ),
                    ("Password (access key)", temporary_password, True),
                ]
            )
        )
        + _cta(workspace_url, "Open your workspace →")
        + _p(
            "Your team members have been sent their own credentials. Keep this "
            "email — your password is your standing access key and stays valid for "
            "future sign-ins.",
            muted=True,
            small=True,
        )
    )
    return _shell(
        subject="Welcome to Archangel Health — onboarding complete",
        body_html=body,
    )


# ─── Physician-facing emails that used to be authored inline ────────────────
# Each of these was a hand-rolled <div> in the router that sent it, which is how
# five different palettes ended up in production. They live here now so they
# inherit the shell like everything else.


def build_self_serve_link_email(*, onboarding_url: str, expires_days: int) -> str:
    """The onboarding link a physician asks for from the landing page.

    For a self-serve signup this is the FIRST thing we ever send them, so it is
    the email most worth getting right.
    """
    safe_url = html.escape(onboarding_url, quote=True)
    body = (
        _eyebrow("Onboarding · Asclepius")
        + _h1("Your onboarding link.")
        + _p(
            "Pick up where you left off any time. This link stays valid for "
            f"{_strong(str(expires_days) + ' days')} and remembers your progress, "
            "so you can stop after any step and come back later."
        )
        + _cta(onboarding_url, "Continue onboarding →")
        + _p(
            f'If the button does not work, paste this into your browser:<br>'
            f'<a href="{safe_url}" style="color:{_GREEN_DEEP};">{html.escape(onboarding_url)}</a>',
            muted=True,
            small=True,
        )
        + _p(
            "If you did not request this, you can ignore this email.",
            muted=True,
            small=True,
        )
    )
    return _shell(subject="Your Archangel Health onboarding link", body_html=body)


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


def build_asclepius_approved_email(*, full_name: str, workspace_url: str) -> str:
    """Credential verification passed and the account is open for real work."""
    first = (full_name or "").strip() or "Doctor"
    body = (
        _eyebrow("Verified · Asclepius")
        + _h1("You&rsquo;re approved.")
        + _p(
            f"{_strong(first)}, your credentials have been verified and your Asclepius "
            "account is now open for evaluation work."
        )
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


def build_community_digest_email(*, activity_rows_html: str, community_url: str) -> str:
    """Batched community activity: mentions, DMs, announcements, broadcasts."""
    body = (
        _eyebrow("Community · Asclepius")
        + _h1("While you were away.")
        + f'<ul style="margin:0 0 18px;padding-left:20px;font-family:{_SANS};font-size:15px;'
        f'line-height:1.7;color:{_INK_SOFT};">{activity_rows_html}</ul>'
        + _cta(community_url, "Open the community →")
        + _p(
            "Colleague discussion only. No patient-identifiable information is "
            "permitted in the community.",
            muted=True,
            small=True,
        )
    )
    return _shell(subject="Asclepius Community: new activity for you", body_html=body)


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
