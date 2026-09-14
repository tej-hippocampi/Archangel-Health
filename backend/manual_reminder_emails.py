"""Manual application reminders. Automated reminder templates stay separate."""
import html

from onboarding_emails import _SANS, _MONO, _INK, _INK_SOFT, _CARD, _CARD_IN, _LIME

VERSION = "manual-2026-09-14-v1"
REPLY_TO = "tejpatel@berkeley.edu"
COPY = {
    "examination": {
        "subject": "One last step: complete your examination case",
        "heading": "This is the last step!",
        "message": "A quick reminder to complete your examination case. This is the last step of your Archangel Health application before we can review it.",
        "instructions": "Click the button below, log in with the email and password you created during signup, and complete and submit your examination case.",
        "button": "Sign in & complete examination",
    },
    "wizard": {
        "subject": "Continue your Archangel Health application",
        "heading": "Pick up where you left off.",
        "message": "A quick reminder to finish your Archangel Health onboarding wizard so you can move forward with your application.",
        "instructions": "Click the button below to open your onboarding wizard at your saved step. Complete the remaining steps and submit your application when you reach the end. If prompted, verify your email to continue.",
        "button": "Continue onboarding",
    },
}
HELP = "If you have any questions or run into any problems with the process, feel free to email Tej at tejpatel@berkeley.edu. He'll be sure to get back to you super quickly."


def render(kind, name, url):
    copy = COPY[kind]
    greeting = "Hi " + " ".join(name.split()) + "," if name and name.strip() else "Hello,"
    e = html.escape
    support = e(HELP).replace(REPLY_TO, f'<a href="mailto:{REPLY_TO}" style="color:{_INK};text-decoration:underline;">{REPLY_TO}</a>')
    body = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light"><meta name="supported-color-schemes" content="light">
<title>{e(copy['subject'])}</title>
<style>@media only screen and (max-width:420px) {{ .pad {{padding:30px 24px !important}} .heading {{font-size:30px !important}} .button {{padding:15px 18px !important;font-size:14px !important}} }}</style>
</head><body style="margin:0;background:{_CARD_IN};color:{_INK};font-family:{_SANS};">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;mso-hide:all;">{e(copy['message'])}</div>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td align="center" style="padding:24px 10px;">
<table role="presentation" width="600" cellspacing="0" cellpadding="0" style="width:100%;max-width:600px;background:{_CARD};border:1px solid #e5e6e2;border-radius:12px;">
<tr><td class="pad" style="padding:40px 42px;">
<p style="margin:0 0 34px;font-family:{_MONO};font-size:11px;letter-spacing:1.6px;color:{_INK_SOFT};">ARCHANGEL HEALTH</p>
<h1 class="heading" style="margin:0 0 26px;font-size:34px;line-height:1.15;font-weight:400;letter-spacing:-1px;">{e(copy['heading'])}</h1>
<p style="margin:0 0 14px;font-size:15px;line-height:1.65;">{e(greeting)}</p>
<p style="margin:0 0 18px;font-size:15px;line-height:1.65;color:{_INK_SOFT};">{e(copy['message'])}</p>
<p style="margin:0 0 24px;font-size:15px;line-height:1.65;color:{_INK_SOFT};">{e(copy['instructions'])}</p>
<table role="presentation" cellspacing="0" cellpadding="0"><tr><td bgcolor="{_LIME}" style="border-radius:28px;mso-padding-alt:15px 22px;">
<a class="button" href="{e(url, quote=True)}" style="display:inline-block;padding:15px 22px;border-radius:28px;background:{_LIME};color:{_INK};font-size:15px;line-height:1.4;font-weight:500;text-decoration:none;">{e(copy['button'])}&nbsp; →</a>
</td></tr></table>
<p style="margin:26px 0 20px;font-size:14px;line-height:1.7;color:{_INK_SOFT};">{support}</p>
<p style="margin:0;font-size:14px;line-height:1.7;">The Archangel Health team</p>
</td></tr><tr><td style="padding:16px 24px;border-top:1px solid #e5e6e2;background:{_CARD_IN};font-size:11px;line-height:1.5;color:{_INK_SOFT};">Archangel Health · Applications</td></tr>
</table></td></tr></table></body></html>'''
    text = "\n\n".join(["Archangel Health", copy["heading"], greeting, copy["message"],
                          copy["instructions"], copy["button"] + ": " + url,
                          HELP, "The Archangel Health team"])
    return {"subject": copy["subject"], "html": body, "text": text}
