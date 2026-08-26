#!/usr/bin/env python3
"""Render every transactional email to disk and open an index.

The onboarding flow is mostly email, and email is the one surface you cannot
see by clicking through the product. This renders all of them side by side with
realistic sample data so a design change can be reviewed as a person would
receive it, without sending anything to anyone.

    python3 backend/scripts/email_preview.py --open

Writes to /tmp/archangel-email-preview by default (--out to change).
"""

from __future__ import annotations

import argparse
import html
import os
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import onboarding_emails as oe  # noqa: E402

WORKSPACE = "https://app.archangelhealth.ai/asclepius"
ONBOARD = "https://archangelhealth.ai/onboard/sample-token"

# (filename, human label, why this email exists, html)
def _cases() -> list[tuple[str, str, str, str]]:
    return [
        (
            "01-verification-code", "Verification code",
            "Sent during onboarding step 2, to prove the mailbox.",
            oe.build_verification_email(code="418302"),
        ),
        (
            "02-doctor-verification", "Doctor verification (link + code)",
            "Landing-plane signup. Magic link primary, code as fallback.",
            oe.build_doctor_verification_email(
                code="418302", magic_link_url="https://archangelhealth.ai/verify-email/sample"),
        ),
        (
            "03-asclepius-admin-invite", "Admin invite to a physician",
            "An admin invites a named physician from the console.",
            oe.build_asclepius_admin_invite_email(
                invitee_name="Dr. Amara Okafor", onboarding_url=ONBOARD),
        ),
        (
            "04-asclepius-invite", "Colleague invite",
            "A director adds a teammate mid-onboarding.",
            oe.build_asclepius_invite_email(
                invitee_first_name="Amara", director_full_name="Dr. Elena Vasquez",
                role_label="Evaluator", org_name="Riverside Nephrology",
                specialty="Nephrology", onboarding_url=ONBOARD,
                invitee_email="a.okafor@riverside.example.org",
                referrer_name="Dr. Elena Vasquez"),
        ),
        (
            "05-asclepius-complete", "Workspace ready (credentials)",
            "Sent on finishing onboarding. Carries the access key today.",
            oe.build_asclepius_complete_email(
                email="a.okafor@riverside.example.org", full_name="Dr. Amara Okafor",
                role_label="Evaluator", org_name="Riverside Nephrology",
                specialty="Nephrology",
                workspace_url=WORKSPACE, is_director=True, team_count=0,
                verification_notice=True),
        ),
        (
            "06-asclepius-task-notification", "New tasks ready",
            "Specialty-matched work is available. Deliberately content-free.",
            oe.build_asclepius_task_notification_email(
                specialty_label="Nephrology", task_count=12, workspace_url=WORKSPACE),
        ),
        (
            "07-careguide-task-notification", "CareGuide task assigned",
            "Clinical portal nudge. PHI-free by construction.",
            oe.build_task_notification_email(login_url=WORKSPACE),
        ),
        (
            "08-team-invite", "Clinical team invite",
            "Director adds a team member on the CareGuide side.",
            oe.build_invite_email(
                invitee_first_name="Sam", director_full_name="Dr. Elena Vasquez",
                role_label="RN Coordinator", org_name="Riverside Health",
                department="Orthopedic Surgery", temporary_password="Kf3-tQ92mXbW7p",
                sign_in_url=WORKSPACE, invitee_email="s.reyes@riverside.example.org"),
        ),
        (
            "09-onboarding-complete", "CareGuide onboarding complete",
            "Clinical director finishes the wizard.",
            oe.build_complete_email(
                director_email="e.vasquez@riverside.example.org",
                org_name="Riverside Health", department="Orthopedic Surgery",
                member_count=3, temporary_password="Kf3-tQ92mXbW7p",
                workspace_url=WORKSPACE, rn_count=1, nppa_count=2),
        ),
        (
            "10-data-provider-invite", "Data provider invite",
            "A health system is asked to send clinical data.",
            oe.build_data_provider_invite_email(
                portal_url=WORKSPACE, email="data@riverside.example.org",
                temporary_password="Kf3-tQ92mXbW7p", org_name="Riverside Health",
                specialty="Nephrology"),
        ),
        (
            "12-self-serve-link", "Onboarding link (self-serve)",
            "The FIRST thing a self-serve physician ever receives from us.",
            oe.build_self_serve_link_email(onboarding_url=ONBOARD, expires_days=7),
        ),
        (
            "13-approved", "You're approved",
            "Credential verification passed; the account opens for real work.",
            oe.build_asclepius_approved_email(
                full_name="Dr. Amara Okafor", workspace_url=WORKSPACE),
        ),
        (
            "14-community-digest", "Community activity digest",
            "Batched mentions, DMs, announcements and broadcasts.",
            oe.build_community_digest_email(
                activity_items=[
                    ("Dr. Elena Vasquez mentioned you",
                     "curious what nephrology thinks of this one"),
                    ("Archangel posted in #task-announcements",
                     "12 new Nephrology tasks are ready to review."),
                ],
                community_url=WORKSPACE.replace("/asclepius", "/community")),
        ),
        (
            "15-event-reminder", "Event reminder",
            "An event the member marked Interested is starting soon.",
            oe.build_community_event_reminder_email(
                first_name="Amara", title="AI in Nephrology: what actually works",
                when_label="Tue 4 Sep, 5:00pm", timezone_label="America/Los_Angeles",
                community_url=WORKSPACE.replace("/asclepius", "/community"),
                location="Zoom", host="Dr. Elena Vasquez"),
        ),
        (
            "16-upload-failed", "Upload did not go through",
            "Data-partner reassurance. Never names the internal failure reason.",
            oe.build_upload_failed_email(
                recipient_name="Sam", filename="riverside-bundle-04.zip",
                reason="we couldn't finish processing it, so it was not added to our system"),
        ),
        (
            "17-internal-signup-alert", "Internal: new signup",
            "Goes to the team, not the physician.",
            oe.build_internal_signup_alert(
                physician_email="a.okafor@riverside.example.org",
                slug="pending-a1b2c3", expires_at="2026-09-01T12:00:00"),
        ),
        (
            "11-buyer-delivery", "Buyer dataset delivery",
            "A dataset a buyer purchased is ready.",
            oe.build_buyer_delivery_email(
                workspace_url=WORKSPACE, email="research@labexample.com",
                temporary_password="Kf3-tQ92mXbW7p", buyer_name="Lab Example",
                datasets_label="Nephrology reasoning traces", data_format="JSONL",
                record_count=4820),
        ),
    ]


_INDEX_CSS = """
  :root { color-scheme: light; }
  body { margin:0; background:#eef0ef; color:#1a1b1a;
         font-family:'Instrument Sans',system-ui,-apple-system,sans-serif; }
  header { padding:32px 40px 8px; }
  h1 { font-weight:400; font-size:1.8rem; letter-spacing:-0.015em; margin:0 0 6px; }
  p.lede { color:#5c5e5a; margin:0 0 4px; max-width:60ch; line-height:1.6; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(420px,1fr));
          gap:24px; padding:24px 40px 64px; }
  .card { background:#fbfcfa; border:1px solid rgba(26,27,26,0.08); border-radius:18px;
          overflow:hidden; box-shadow:0 1px 2px rgba(26,27,26,0.03); }
  .card h2 { font-size:0.95rem; font-weight:500; margin:0; padding:16px 18px 4px; }
  .card p { margin:0; padding:0 18px 14px; color:#8b8d89; font-size:0.8rem; line-height:1.5; }
  .card iframe { width:100%; height:520px; border:0; border-top:1px solid rgba(26,27,26,0.08);
                 background:#fff; display:block; }
  .card a { display:block; padding:10px 18px; font-size:0.7rem; letter-spacing:0.09em;
            text-transform:uppercase; color:#5c5e5a; text-decoration:none;
            border-top:1px solid rgba(26,27,26,0.08); }
  .card a:hover { color:#1a1b1a; }
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/archangel-email-preview")
    ap.add_argument("--open", action="store_true", dest="open_browser")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    cards = []
    for slug, label, why, markup in _cases():
        (out / f"{slug}.html").write_text(markup, encoding="utf-8")
        cards.append(
            f'<div class="card"><h2>{html.escape(label)}</h2>'
            f'<p>{html.escape(why)}</p>'
            f'<iframe src="{slug}.html" loading="lazy" title="{html.escape(label)}"></iframe>'
            f'<a href="{slug}.html" target="_blank">Open full size &rarr;</a></div>'
        )

    index = (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>Archangel email preview</title><style>{_INDEX_CSS}</style></head><body>"
        "<header><h1>Transactional email preview</h1>"
        "<p class=lede>Every email the product sends, rendered with sample data. "
        "Nothing here is delivered to anyone.</p></header>"
        f'<div class="grid">{"".join(cards)}</div></body></html>'
    )
    (out / "index.html").write_text(index, encoding="utf-8")

    print(f"{len(cards)} emails rendered to {out}/index.html")
    if args.open_browser:
        webbrowser.open(f"file://{out.resolve()}/index.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
