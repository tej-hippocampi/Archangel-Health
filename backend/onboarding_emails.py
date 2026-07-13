"""
HTML transactional emails for the Archangel Health onboarding flow.

Visual spec: design_handoff_onboarding_flow/README.md → "Emails"

Three emails, all sharing the dark cinematic-blue body shell:
  1. build_verification_email — 6-digit code mailed during step 2.
  2. build_invite_email       — standing access key mailed when the director
                                adds a team member on step 4.
  3. build_complete_email     — welcome / credentials mailed on /finish.

Email body == the dark-blue inner shell. The Gmail/iOS Mail "from / subject"
chrome is added by the inbox client, never by us (per handoff README).

Implementation notes for client compatibility:
  - Layout uses <table>/<td> + inline `style=""` for Outlook safety.
  - Fraunces is loaded via Google Fonts <link>. Apple Mail / iOS Mail / Gmail
    web honor it; Outlook ignores it and falls back to Georgia (acceptable).
  - SVG brandmark renders in modern clients; Outlook shows the gradient tile
    without the inner shield, which is graceful.
"""

from __future__ import annotations

import html
from typing import Iterable, Tuple

# ─── Shared design tokens (mirror of the React prototype) ───────────────────

_GOOGLE_FONTS_HEAD = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" '
    'href="https://fonts.googleapis.com/css2?'
    "family=Inter:wght@400;500;600;700"
    "&amp;family=Fraunces:opsz,wght@9..144,500"
    '&amp;display=swap">'
)

_INTER = "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"
_FRAUNCES = "'Fraunces', 'Iowan Old Style', 'Charter', Georgia, serif"
_MONO = "ui-monospace, 'SF Mono', Menlo, Monaco, Consolas, monospace"

_FOOTER_TEXT = (
    "Archangel Health · Confidential. This email and any attached files are "
    "intended only for the named recipient."
)

# Inline SVG used inside the 32×32 brand tile in the email header.
_SHIELD_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 120" '
    'width="18" height="18" fill="none" aria-hidden="true">'
    '<rect x="58" y="20" width="4" height="80" fill="#fff" rx="2"/>'
    '<circle cx="60" cy="28" r="4" fill="#67E8F9" opacity="0.95"/>'
    '<path d="M60 45 Q50 50 48 58 Q46 66 54 70" stroke="#fff" stroke-width="2.5" '
    'fill="none" stroke-linecap="round"/>'
    '<path d="M60 55 Q70 60 72 68 Q74 76 66 80" stroke="#fff" stroke-width="2.5" '
    'fill="none" stroke-linecap="round"/>'
    "</svg>"
)


def _shell(*, subject: str, body_html: str) -> str:
    """Wrap inner body content in the dark cinematic-blue email shell."""
    safe_subject = html.escape(subject, quote=True)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="x-apple-disable-message-reformatting">
<meta name="color-scheme" content="dark">
<meta name="supported-color-schemes" content="dark">
<title>{safe_subject}</title>
{_GOOGLE_FONTS_HEAD}
</head>
<body style="margin:0;padding:0;background:#06080F;font-family:{_INTER};color:#E6EAF2;-webkit-font-smoothing:antialiased;">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:#06080F;">
  <tr>
    <td align="center" style="padding:32px 12px;">
      <table role="presentation" width="640" cellspacing="0" cellpadding="0" border="0" style="max-width:640px;width:100%;background:#0B1220;border-radius:14px;overflow:hidden;border:1px solid rgba(103,232,249,0.10);box-shadow:0 24px 60px rgba(0,0,0,0.40);">
        <tr>
          <td style="padding:40px 48px 48px;background:radial-gradient(ellipse 800px 500px at 50% -10%, rgba(38,99,235,0.18) 0%, transparent 55%), radial-gradient(ellipse 600px 400px at 100% 100%, rgba(103,232,249,0.08) 0%, transparent 60%), #0B1220;">
            <table role="presentation" cellspacing="0" cellpadding="0" border="0" style="margin-bottom:28px;">
              <tr>
                <td style="vertical-align:middle;">
                  <div style="width:32px;height:32px;border-radius:8px;background:linear-gradient(135deg,#1A3C8F 0%,#2563EB 100%);box-shadow:0 0 0 1px rgba(103,232,249,0.25),0 4px 14px rgba(38,99,235,0.30);text-align:center;line-height:32px;">{_SHIELD_SVG}</div>
                </td>
                <td style="padding-left:10px;vertical-align:middle;font-family:{_INTER};font-size:12px;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;color:#F5F5F7;">
                  Archangel Health
                </td>
              </tr>
            </table>
            {body_html}
          </td>
        </tr>
        <tr>
          <td style="background:rgba(7,11,21,0.65);padding:18px 48px;font-family:{_INTER};font-size:11px;color:rgba(230,234,242,0.45);line-height:1.6;border-top:1px solid rgba(103,232,249,0.08);">
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
    return (
        f'<div style="font-family:{_INTER};font-size:11px;font-weight:700;'
        'letter-spacing:0.14em;text-transform:uppercase;color:#67E8F9;'
        'margin-bottom:12px;">'
        f"{html.escape(text)}</div>"
    )


def _h1(text: str) -> str:
    return (
        f'<h1 style="margin:0 0 14px;font-family:{_FRAUNCES};font-size:30px;'
        'font-weight:500;letter-spacing:-0.02em;color:#F5F5F7;line-height:1.15;">'
        f"{text}</h1>"
    )


def _p(html_content: str, *, muted: bool = False, small: bool = False) -> str:
    color = "rgba(230,234,242,0.5)" if muted else "rgba(230,234,242,0.78)"
    size = "13px" if small else "15px"
    return (
        f'<p style="margin:0 0 16px;font-family:{_INTER};font-size:{size};'
        f'line-height:1.65;color:{color};">{html_content}</p>'
    )


def _strong(text: str) -> str:
    return f'<strong style="color:#F5F5F7;font-weight:600;">{html.escape(text)}</strong>'


def _cta(href: str, label: str) -> str:
    safe_href = html.escape(href, quote=True)
    safe_label = html.escape(label)
    return f"""<table role="presentation" cellspacing="0" cellpadding="0" border="0" style="margin:22px 0 18px;">
  <tr>
    <td>
      <a href="{safe_href}" style="display:inline-block;padding:14px 24px;border-radius:10px;background:#67E8F9;color:#07070A;font-family:{_INTER};font-size:14px;font-weight:600;text-decoration:none;letter-spacing:-0.005em;box-shadow:0 4px 16px rgba(103,232,249,0.30),0 0 0 1px rgba(103,232,249,0.45);">
        {safe_label}
      </a>
    </td>
  </tr>
</table>"""


def _inset_card(inner_html: str) -> str:
    return f"""<div style="background:rgba(15,23,42,0.65);border:1px solid rgba(103,232,249,0.18);border-radius:12px;padding:18px 22px;margin:20px 0;">
  {inner_html}
</div>"""


def _detail_rows(rows: Iterable[Tuple[str, str, bool]]) -> str:
    """Render <label, value, mono?> rows separated by hairlines (last row has no border)."""
    rows_list = list(rows)
    out = []
    for i, (label, value, mono) in enumerate(rows_list):
        last = i == len(rows_list) - 1
        border = "" if last else "border-bottom:1px solid rgba(103,232,249,0.08);"
        value_font = _MONO if mono else _INTER
        out.append(
            f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" '
            f'style="{border}">'
            "<tr>"
            f'<td style="padding:10px 0;font-family:{_INTER};font-size:12px;font-weight:700;'
            f'letter-spacing:0.10em;text-transform:uppercase;color:rgba(230,234,242,0.5);">'
            f"{html.escape(label)}</td>"
            f'<td align="right" style="padding:10px 0;font-family:{value_font};font-size:14px;'
            f'font-weight:500;color:#F5F5F7;text-align:right;">{html.escape(value)}</td>'
            "</tr></table>"
        )
    return "".join(out)


# ─── Public builders ────────────────────────────────────────────────────────


def build_verification_email(*, code: str) -> str:
    """Email 1 — 6-digit verification code, large cyan Fraunces digits."""
    safe_code = html.escape(code)
    body = (
        _eyebrow("Verification code")
        + _h1("Confirm it&rsquo;s you.")
        + _p(
            "Enter this code in your browser to continue setting up your "
            "health system on Archangel Health."
        )
        + (
            '<div style="background:rgba(15,23,42,0.65);border:1px solid rgba(103,232,249,0.25);'
            'border-radius:14px;padding:28px 24px;text-align:center;margin:24px 0;'
            'box-shadow:inset 0 1px 0 rgba(255,255,255,0.04),0 0 32px rgba(103,232,249,0.08);">'
            f'<div style="font-family:{_FRAUNCES};font-size:44px;font-weight:500;'
            'letter-spacing:0.32em;color:#67E8F9;padding-left:0.32em;'
            f'text-shadow:0 0 24px rgba(103,232,249,0.35);">{safe_code}</div>'
            "</div>"
        )
        + _p(
            "This code expires in 15 minutes. If you didn&rsquo;t request it, "
            "ignore this email.",
            muted=True,
            small=True,
        )
    )
    return _shell(subject="Your Archangel Health verification code", body_html=body)


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
) -> str:
    """Asclepius member invite — links the clinician to *start* onboarding.

    Unlike the clinical invite, no password is issued here: the member sets up
    their own credentials + attestations first, and receives their standing
    access key in the completion email once they finish.
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

    body = (
        _eyebrow("Invitation · Asclepius")
        + _h1(f"You&rsquo;re invited to contribute to {org_spec_label}.")
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
    )
    subject = f"You're invited to label data with {(org_name or 'your organization').strip()}"
    return _shell(subject=subject, body_html=body)


def build_asclepius_complete_email(
    *,
    email: str,
    full_name: str,
    role_label: str,
    org_name: str,
    specialty: str,
    temporary_password: str,
    workspace_url: str,
    is_director: bool,
    team_count: int = 0,
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
    rows.append(("Password (access key)", temporary_password, True))

    intro = (
        html.escape(safe_org)
        + (" · " + html.escape(safe_spec) if safe_spec else "")
        + " is live on Asclepius. You can now open your training console, pick up "
        "evaluation tasks, and start contributing expert-labeled data."
    )

    body = (
        _eyebrow("Onboarding complete · Asclepius")
        + _h1("Your workspace is ready.")
        + _p(intro)
        + _inset_card(_detail_rows(rows))
        + _cta(workspace_url, "Open your workspace →")
        + _p(
            "&#11088; "
            + _strong("Star this email")
            + " — everything you need to contribute data lives here. Your password is "
            "your standing access key: it does not expire, and you&rsquo;ll use this "
            "email + password every time you sign in to your workspace.",
            small=True,
        )
    )
    return _shell(subject="Your Asclepius workspace is ready", body_html=body)


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
