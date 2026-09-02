"""Shared HTML email sending (SendGrid or SMTP).

Dev mode: set ``EMAIL_DEV_MODE=1`` in your env to bypass real email transport
entirely. The body is printed to stdout so OTP codes and invite links are
visible in the uvicorn terminal — the "send" call returns success. Useful for
local end-to-end testing of onboarding flows without configuring SendGrid.
"""

import os
import re
from typing import Optional, Tuple


def _normalize_sendgrid_api_key(raw: Optional[str]) -> str:
    """Strip whitespace and common .env mistakes (quotes, accidental Bearer prefix)."""
    s = (raw or "").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1].strip()
    if s.lower().startswith("bearer "):
        s = s[7:].strip()
    return s


def _is_dev_mode() -> bool:
    return (os.getenv("EMAIL_DEV_MODE") or "").strip().lower() in ("1", "true", "yes", "on")


def is_email_dev_mode() -> bool:
    """Public alias for the dev-mode check.

    Callers outside this module need to know that outgoing mail is being printed
    rather than delivered — the onboarding router logs the OTP on that basis,
    because in dev mode the code exists nowhere else a developer can reach it.
    """
    return _is_dev_mode()


def is_email_transport_configured() -> bool:
    """True if SendGrid API key, full SMTP credentials, or dev-mode are present.

    In dev mode (``EMAIL_DEV_MODE=1``) returns True so onboarding endpoints don't
    503 — outgoing email is logged to stdout instead of actually delivered.
    """
    if _is_dev_mode():
        return True
    if _normalize_sendgrid_api_key(os.getenv("SENDGRID_API_KEY")):
        return True
    h = (os.getenv("SMTP_HOST") or "").strip()
    u = (os.getenv("SMTP_USER") or "").strip()
    p = (os.getenv("SMTP_PASS") or "").strip()
    return bool(h and u and p)


def active_email_vendor() -> Optional[str]:
    """Which transport an outgoing email would actually use right now."""
    if _is_dev_mode():
        return "dev"
    if _normalize_sendgrid_api_key(os.getenv("SENDGRID_API_KEY")):
        return "sendgrid"
    h = (os.getenv("SMTP_HOST") or "").strip()
    u = (os.getenv("SMTP_USER") or "").strip()
    p = (os.getenv("SMTP_PASS") or "").strip()
    if h and u and p:
        return "smtp"
    return None


def email_phi_allowed() -> bool:
    """True if PHI may be placed in an outgoing email body given the active
    transport (PRD-4). SendGrid is not HIPAA-eligible unless a BAA is flagged; a
    self-hosted SMTP relay is assumed covered; dev mode never leaves the host."""
    vendor = active_email_vendor()
    if vendor in ("dev", "smtp"):
        return True
    if vendor == "sendgrid":
        from compliance.subprocessors import phi_allowed  # local: avoid import cost

        return phi_allowed("sendgrid")
    return False


def _strip_html(html: str) -> str:
    """Best-effort HTML→text for the dev-mode console preview."""
    text = re.sub(r"<\s*br\s*/?>", "\n", html, flags=re.I)
    text = re.sub(r"</p>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


#: A bare ``local@domain.tld`` and nothing else. Deliberately stricter than the
#: addresses we accept at signup: this value is written into a mail header, so
#: the bar is "cannot possibly be two headers" rather than "is deliverable".
_REPLY_TO_RE = re.compile(r"[^@\s,<>;:\\\"]+@[^@\s,<>;:\\\"]+\.[A-Za-z]{2,}")


async def send_html_email(
    to_email: str,
    subject: str,
    html_body: str,
    *,
    importance_headers: bool = False,
    attachments: Optional[list] = None,
    reply_to: str | None = None,
) -> bool:
    ok, _reason = await send_html_email_with_reason(
        to_email, subject, html_body, importance_headers=importance_headers,
        attachments=attachments, reply_to=reply_to,
    )
    return ok


async def send_html_email_with_reason(
    to_email: str,
    subject: str,
    html_body: str,
    *,
    importance_headers: bool = False,
    attachments: Optional[list] = None,
    reply_to: str | None = None,
) -> "tuple[bool, str]":
    """Send an HTML email. Returns (ok, reason). `reason` is a short, human-
    readable explanation suitable for surfacing in the UI when ok is False.

    ``attachments`` is a list of ``(filename, mime_type, bytes)``. It exists for
    one requirement and should stay rare: the E-SIGN Act conditions the
    enforceability of an electronic agreement on the signer being able to RETAIN
    a copy, and a link into a portal they may lose access to is not retention.
    Nothing carrying PHI goes through here -- every caller is a document we
    generated about the relationship, not about a patient.

    ``reply_to`` sets the Reply-To header and changes nothing else: the From
    address stays the verified sending identity, because that is what the
    domain's SPF and DKIM records authorise and forging it is how a message
    lands in spam instead of an inbox. It exists for mail we send ON SOMEBODY'S
    BEHALF, a physician's introduction to a colleague, where a reply belongs
    with that physician rather than with a noreply mailbox nobody reads.

    Validated before it reaches a header. A display name arrives from user
    input, and a CR/LF in a MIME header is header injection on the SMTP path;
    anything that does not look like a bare address is dropped rather than sent.
    """
    # Dev mode short-circuit: print the message to stdout and return success.
    # This lets onboarding / OTP / invite flows run end-to-end without SendGrid.
    if _is_dev_mode():
        print("\n" + "=" * 72)
        print(f"[email_utils] DEV MODE — pretending to send email")
        print(f"  To:      {to_email}")
        if reply_to:
            print(f"  Reply-To: {reply_to}")
        print(f"  Subject: {subject}")
        print("-" * 72)
        print(_strip_html(html_body))
        for name, _mime, blob in (attachments or []):
            print(f"  [attachment] {name} ({len(blob)} bytes)")
        print("=" * 72 + "\n", flush=True)
        return True, "dev_mode"

    # One bare address, or nothing. No display name, no comma-separated list,
    # no whitespace: each of those is a way to smuggle a second header or a
    # second recipient through a field that is meant to carry one mailbox.
    clean_reply_to = (reply_to or "").strip()
    if clean_reply_to and not _REPLY_TO_RE.fullmatch(clean_reply_to):
        print(f"[email_utils] ignoring malformed reply_to={clean_reply_to!r}")
        clean_reply_to = ""

    try:
        api_key = _normalize_sendgrid_api_key(os.getenv("SENDGRID_API_KEY"))
        from_email = (os.getenv("SENDGRID_FROM_EMAIL") or "noreply@archangelhealth.ai").strip()
        from_name = (os.getenv("SENDGRID_FROM_NAME") or "Archangel Health").strip()
        if api_key:
            from sendgrid import SendGridAPIClient
            from sendgrid.helpers.mail import Header, Mail

            message = Mail(
                from_email=(from_email, from_name),
                to_emails=to_email,
                subject=subject,
                html_content=html_body,
            )
            if importance_headers:
                message.add_header(Header("Importance", "high"))
                message.add_header(Header("X-Priority", "1"))
            for name, mime_type, blob in (attachments or []):
                import base64 as _b64

                from sendgrid.helpers.mail import (
                    Attachment, Disposition, FileContent, FileName, FileType,
                )

                message.add_attachment(Attachment(
                    FileContent(_b64.b64encode(blob).decode("ascii")),
                    FileName(name), FileType(mime_type), Disposition("attachment"),
                ))
            if clean_reply_to:
                message.reply_to = clean_reply_to
            sg = SendGridAPIClient(api_key)
            response = sg.send(message)
            status_code = getattr(response, "status_code", None)
            if status_code not in (200, 202):
                raw = getattr(response, "body", b"") or b""
                try:
                    body_preview = raw.decode("utf-8", errors="replace")[:4000]
                except Exception:
                    body_preview = str(raw)[:4000]
                print(f"[email_utils] SendGrid HTTP {status_code} for to={to_email!r}: {body_preview}")
                if status_code == 403:
                    reason = (
                        f"SendGrid rejected the send (403). The From address "
                        f"'{from_email}' is almost certainly not a verified sender — "
                        f"verify it (or your domain) in SendGrid."
                    )
                elif status_code == 401:
                    reason = "SendGrid rejected the API key (401). Check SENDGRID_API_KEY."
                else:
                    reason = f"SendGrid returned HTTP {status_code}."
                return False, reason
            return True, "sent"

        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart

        smtp_host = (os.getenv("SMTP_HOST") or "").strip()
        smtp_user = (os.getenv("SMTP_USER") or "").strip()
        smtp_pass = (os.getenv("SMTP_PASS") or "").strip()
        if smtp_host and smtp_user and smtp_pass:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = f"{from_name} <{from_email}>"
            msg["To"] = to_email
            if importance_headers:
                msg["Importance"] = "high"
                msg["X-Priority"] = "1"
            if clean_reply_to:
                msg["Reply-To"] = clean_reply_to
            msg.attach(MIMEText(html_body, "html", "utf-8"))
            if attachments:
                # "alternative" means "the same content in two formats", so a
                # file added to it is a format of the message rather than an
                # attachment and most clients hide it. Re-wrap in "mixed", which
                # is what an attachment actually is.
                from email.mime.base import MIMEBase
                from email.encoders import encode_base64

                outer = MIMEMultipart("mixed")
                for header in ("Subject", "From", "To", "Importance", "X-Priority"):
                    if msg.get(header):
                        outer[header] = msg[header]
                outer.attach(msg)
                for name, mime_type, blob in attachments:
                    maintype, _, subtype = (mime_type or "application/octet-stream").partition("/")
                    part = MIMEBase(maintype, subtype or "octet-stream")
                    part.set_payload(blob)
                    encode_base64(part)
                    part.add_header("Content-Disposition", "attachment", filename=name)
                    outer.attach(part)
                msg = outer
            port = int(os.getenv("SMTP_PORT", "587"))
            with smtplib.SMTP(smtp_host, port) as server:
                server.starttls()
                server.login(smtp_user, smtp_pass)
                server.send_message(msg)
            return True, "sent"
        return False, "Email transport is not configured (no SendGrid key or SMTP credentials)."
    except Exception as e:
        print(f"[email_utils] send failed: {e}")
        msg = str(e).lower()
        if "401" in msg or "unauthorized" in msg:
            print(
                "[email_utils] SendGrid 401: the API key was rejected. "
                "For local dev, set SENDGRID_API_KEY in backend/.env to the same key as production (Railway) and restart uvicorn."
            )
            return False, "SendGrid rejected the API key (401). Check SENDGRID_API_KEY."
        if "403" in msg or "forbidden" in msg:
            print(
                "[email_utils] SendGrid 403: often means the From address is not verified for this SendGrid account. "
                "Set SENDGRID_FROM_EMAIL to a verified sender (or verify your domain)."
            )
            return False, "SendGrid 403 — the From address is not a verified sender. Verify SENDGRID_FROM_EMAIL in SendGrid."
        return False, f"Email send failed: {e}"
