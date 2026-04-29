"""
HTML transactional emails for the Archangel Health onboarding flow.

Visual spec: design_handoff_onboarding_flow/README.md → "Emails"

Three emails, all sharing the dark cinematic-blue body shell:
  1. build_verification_email — 6-digit code mailed during step 2.
  2. build_invite_email       — temporary password mailed when the director
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
) -> str:
    """Email 2 — invite for a newly added team member, with temp password."""
    safe_org = html.escape(org_name or "your health system")
    safe_dept = html.escape(department or "")
    org_dept_label = (safe_org + " " + safe_dept).strip()

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
        + _inset_card(
            (
                '<div style="font-family:' + _INTER + ';font-size:11px;font-weight:700;'
                'letter-spacing:0.12em;text-transform:uppercase;color:rgba(230,234,242,0.55);'
                'margin-bottom:8px;">Your temporary password</div>'
                f'<div style="font-family:{_MONO};font-size:18px;font-weight:600;color:#67E8F9;'
                f'letter-spacing:0.02em;word-break:break-all;">{html.escape(temporary_password)}</div>'
            )
        )
        + _cta(sign_in_url, f"Sign in to {department} workspace →" if department else "Sign in to your workspace →")
        + _p("Please change your password on first sign-in.", muted=True, small=True)
    )

    subject_dept = (department or "").strip()
    subject_org = (org_name or "your health system").strip()
    if subject_dept:
        subject = f"You're invited to {subject_org} {subject_dept} workspace"
    else:
        subject = f"You're invited to {subject_org} workspace"
    return _shell(subject=subject, body_html=body)


def build_complete_email(
    *,
    director_email: str,
    org_name: str,
    department: str,
    member_count: int,
    temporary_password: str,
    workspace_url: str,
) -> str:
    """Email 3 — welcome with full details inset and director temp password."""
    safe_org = (org_name or "your health system").strip()
    safe_dept = (department or "").strip()
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
                        "TEAM members",
                        f"{member_count + 1} (you + {member_count})",
                        False,
                    ),
                    ("Temporary password", temporary_password, True),
                ]
            )
        )
        + _cta(workspace_url, "Open your workspace →")
        + _p(
            "Your team members have been sent their own credentials. Please change "
            "your password after first sign-in.",
            muted=True,
            small=True,
        )
    )
    return _shell(
        subject="Welcome to Archangel Health — onboarding complete",
        body_html=body,
    )
