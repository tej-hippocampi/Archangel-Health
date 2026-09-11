"""The approved personal letter, shared by every physician acceptance path.

Table layout, inline styles, system fonts and hosted PNG artwork keep the
approved design usable in inboxes. No preview JavaScript, base64 images, web
fonts, or changes to the other transactional email templates.
"""
from __future__ import annotations

import html
import os
import re
from urllib.parse import urlsplit

MONOGRAM = 'welcome-monogram-v1.png'
SIGNATURE = 'welcome-signature-v1.png'
TEMPLATE_MARKER = '<!-- physician-welcome:v1 -->'


def _greeting(full_name: str) -> str:
    name = re.sub(r'^(?:dr\.?|doctor)\s+', '', (full_name or '').strip(), flags=re.I)
    # Signup normally supplies given-name + family-name. Imported legal names
    # may instead be written family-name, given-name.
    if ',' in name:
        before, after = name.split(',', 1)
        if after.strip() and not re.match(r'^(?:md|do|phd|mbbs)\b', after.strip(), re.I):
            name = after.strip()
        else:
            name = before.strip()
    first = name.split()[0] if name.split() else ''
    return html.escape(f'Welcome, {first}.' if first else 'Welcome.')


def render_welcome_email(*, full_name: str, subject: str, sign_in_url: str,
                         calendly_url: str, email: str = '',
                         temp_password: str | None = None,
                         needs_password_setup: bool = False) -> str:
    """Render only. The acceptance queue owns recipient eligibility and delivery."""
    parts = urlsplit((os.getenv('BASE_URL') or sign_in_url).strip())
    asset_base = f'{parts.scheme}://{parts.netloc}'
    logo = html.escape(f'{asset_base}/email-assets/{MONOGRAM}', quote=True)
    signature = html.escape(f'{asset_base}/email-assets/{SIGNATURE}', quote=True)
    sign_in = html.escape(sign_in_url, quote=True)
    calendar = html.escape(calendly_url, quote=True)
    credential_block = ''
    helper = 'Use the email and password from your application.'
    if temp_password:
        credential_block = f'''<tr><td style="padding:0 0 22px;font:14px/24px Arial,Helvetica,sans-serif;color:#424e48;">
          Email: {html.escape(email)}<br>
          Temporary password: <strong>{html.escape(temp_password)}</strong>
        </td></tr>'''
        helper = 'Use these details to sign in. You’ll choose your own password when you arrive.'
    elif needs_password_setup:
        helper = 'Enter your email, then choose “Forgot your password?” on the sign-in page to set your password.'
    return f'''<!doctype html>
<html lang="en" xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office">
<head>
{TEMPLATE_MARKER}
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="x-apple-disable-message-reformatting">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
<title>{html.escape(subject)}</title>
<!--[if mso]><xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml><![endif]-->
<style>
@media only screen and (max-width:480px) {{
  .welcome-outer {{ padding:12px 7px!important; }}
  .welcome-inner {{ padding:28px 26px 30px!important; }}
  .welcome-title {{ font-size:37px!important;letter-spacing:-1.2px!important; }}
}}
@media only screen and (max-width:350px) {{
  .welcome-inner {{ padding-left:21px!important;padding-right:21px!important; }}
  .welcome-title {{ font-size:32px!important; }}
}}
</style>
</head>
<body style="margin:0;padding:0;background-color:#eeefe9;-webkit-text-size-adjust:100%;">
<div style="display:none;font-size:1px;line-height:1px;max-height:0;max-width:0;opacity:0;overflow:hidden;mso-hide:all;">You’re approved. We’re glad you’re here.</div>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" bgcolor="#eeefe9">
<tr><td class="welcome-outer" align="center" style="padding:24px 12px;">
<!--[if mso]><table role="presentation" width="620" cellspacing="0" cellpadding="0" border="0"><tr><td><![endif]-->
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" bgcolor="#fbf9f1" style="width:100%;max-width:620px;border:1px solid #dce0d5;">
<tr><td class="welcome-inner" style="padding:38px 48px 40px;">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="table-layout:fixed;">
<tr><td style="padding:0 0 34px;">
  <table role="presentation" cellspacing="0" cellpadding="0" border="0"><tr>
    <td width="56" valign="middle"><img src="{logo}" width="56" height="56" alt="AH" style="display:block;width:56px;height:56px;border:0;"></td>
    <td valign="middle" style="padding-left:16px;font:11px/18px Arial,Helvetica,sans-serif;letter-spacing:1.8px;color:#193d39;">ARCHANGEL<br>HEALTH</td>
  </tr></table>
</td></tr>
<tr><td><h1 class="welcome-title" style="margin:0 0 10px;font:400 42px/1.15 Georgia,'Times New Roman',serif;letter-spacing:-1.4px;color:#193d39;overflow-wrap:anywhere;word-wrap:break-word;">{_greeting(full_name)}</h1></td></tr>
<tr><td style="padding:0 0 29px;font:14px/23px Arial,Helvetica,sans-serif;color:#637166;">You’re approved. We’re glad you’re here.</td></tr>
<tr><td style="padding:0 0 20px;font:15px/26px Arial,Helvetica,sans-serif;color:#424e48;">We’re building a place where doctors earn from their judgment, models learn from it, and the hardest cases become the most valuable data.</td></tr>
<tr><td style="font:15px/26px Arial,Helvetica,sans-serif;color:#424e48;">The physicians who carry the consequences of care should define what correct means.</td></tr>
<tr><td style="padding:12px 0 27px;font:italic 35px/43px Georgia,'Times New Roman',serif;letter-spacing:-1.1px;color:#193d39;"><strong style="font-weight:400;background-color:#e3e8b7;background-image:linear-gradient(#fbf9f1 53%,#e3e8b7 53%,#e3e8b7 93%,#fbf9f1 93%);padding:0 7px 2px 3px;">That’s you.</strong></td></tr>
{credential_block}
<tr><td>
<!--[if mso]><v:roundrect href="{sign_in}" style="height:44px;v-text-anchor:middle;width:120px;" arcsize="18%" stroke="f" fillcolor="#21483f"><w:anchorlock xmlns:w="urn:schemas-microsoft-com:office:word"/><center style="color:#fbf9f1;font-family:Georgia,serif;font-size:17px;">Sign in</center></v:roundrect><![endif]-->
<!--[if !mso]><!--><a href="{sign_in}" style="display:inline-block;width:120px;line-height:44px;background-color:#21483f;border-radius:8px;color:#fbf9f1;font-family:Georgia,'Times New Roman',serif;font-size:17px;font-weight:400;text-align:center;text-decoration:none;-webkit-text-size-adjust:none;mso-hide:all;">Sign in</a><!--<![endif]-->
</td></tr>
<tr><td style="padding:10px 0 30px;font:12px/19px Arial,Helvetica,sans-serif;color:#738074;">{helper}</td></tr>
<tr><td style="border-top:1px solid #dbded3;padding:25px 0 0;font:15px/26px Arial,Helvetica,sans-serif;color:#424e48;">We’d love to have a 20-minute conversation about your specialty, the platform, or what you hope to contribute.</td></tr>
<tr><td style="padding:2px 0 0;"><a href="{calendar}" style="display:inline-block;padding:11px 0;font:14px/22px Arial,Helvetica,sans-serif;color:#193d39;text-decoration:underline;text-underline-offset:4px;">Find a time to talk</a></td></tr>
<tr><td style="padding:22px 0 0;font:14px/24px Arial,Helvetica,sans-serif;color:#424e48;">Warmly,</td></tr>
<tr><td style="padding:5px 0 0;"><img src="{signature}" width="214" height="55" alt="Tej &amp; Aryaa" style="display:block;width:214px;max-width:100%;height:auto;border:0;font:italic 26px Georgia,'Times New Roman',serif;color:#193d39;"></td></tr>
</table>
</td></tr></table>
<!--[if mso]></td></tr></table><![endif]-->
</td></tr></table>
</body></html>'''
