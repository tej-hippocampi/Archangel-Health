"""Shared HTML email sending (SendGrid or SMTP)."""

import os
from typing import Optional


def _normalize_sendgrid_api_key(raw: Optional[str]) -> str:
    """Strip whitespace and common .env mistakes (quotes, accidental Bearer prefix)."""
    s = (raw or "").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1].strip()
    if s.lower().startswith("bearer "):
        s = s[7:].strip()
    return s


def is_email_transport_configured() -> bool:
    """True if SendGrid API key or full SMTP credentials are present (after normalization / strip)."""
    if _normalize_sendgrid_api_key(os.getenv("SENDGRID_API_KEY")):
        return True
    h = (os.getenv("SMTP_HOST") or "").strip()
    u = (os.getenv("SMTP_USER") or "").strip()
    p = (os.getenv("SMTP_PASS") or "").strip()
    return bool(h and u and p)


async def send_html_email(
    to_email: str,
    subject: str,
    html_body: str,
    *,
    importance_headers: bool = False,
) -> bool:
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
                return False
            return True

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
            msg.attach(MIMEText(html_body, "html", "utf-8"))
            port = int(os.getenv("SMTP_PORT", "587"))
            with smtplib.SMTP(smtp_host, port) as server:
                server.starttls()
                server.login(smtp_user, smtp_pass)
                server.send_message(msg)
            return True
    except Exception as e:
        print(f"[email_utils] send failed: {e}")
        msg = str(e).lower()
        if "401" in msg or "unauthorized" in msg:
            print(
                "[email_utils] SendGrid 401: the API key was rejected. "
                "For local dev, set SENDGRID_API_KEY in backend/.env to the same key as production (Railway) and restart uvicorn."
            )
        if "403" in msg or "forbidden" in msg:
            print(
                "[email_utils] SendGrid 403: often means the From address is not verified for this SendGrid account. "
                "Set SENDGRID_FROM_EMAIL to a verified sender (or verify your domain)."
            )
    return False
