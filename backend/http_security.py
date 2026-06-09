"""HTTP security hardening: CORS allowlist, security headers + CSP, and a
production secret guard (PRD-2).

The security-headers middleware is pure-ASGI (it only rewrites the
``http.response.start`` message) so it is safe for streaming/SSE responses and
adds no per-request body buffering.
"""

from __future__ import annotations

import os
from typing import List

from starlette.datastructures import MutableHeaders


def is_production() -> bool:
    return os.getenv("ENV", "").strip().lower() == "production"


def allowed_origins() -> List[str]:
    """CORS allowlist. Explicit ``ALLOWED_ORIGINS`` (comma-separated) wins;
    otherwise default to BASE_URL + LANDING_URL so local dev works out of the box."""
    env = os.getenv("ALLOWED_ORIGINS", "").strip()
    if env:
        return [o.strip() for o in env.split(",") if o.strip()]
    out: List[str] = []
    for v in (
        os.getenv("BASE_URL", "http://localhost:8000"),
        os.getenv("LANDING_URL", "http://localhost:5173"),
    ):
        v = (v or "").strip().rstrip("/")
        if v and v not in out:
            out.append(v)
    return out


def allowed_hosts() -> List[str]:
    raw = os.getenv("ALLOWED_HOSTS", "").strip()
    return [h.strip() for h in raw.split(",") if h.strip()]


SECURITY_HEADERS = {
    b"x-frame-options": b"DENY",
    b"x-content-type-options": b"nosniff",
    b"referrer-policy": b"strict-origin-when-cross-origin",
    # microphone=self because the voice companion records the patient.
    b"permissions-policy": b"geolocation=(), camera=(), microphone=(self)",
}


def build_csp() -> str:
    """Phase-1 CSP: bounded but permissive (defaults to report-only). The
    frontend uses inline <script>/<style> (injected window.__PATIENT__, inline
    survey script, inline style attrs) and external media (ElevenLabs audio,
    Tavus avatar), so a strict nonce policy is deferred to phase 2 (PRD-2 §5)."""
    return "; ".join(
        [
            "default-src 'self'",
            "base-uri 'self'",
            "frame-ancestors 'none'",
            "object-src 'none'",
            "img-src 'self' data: blob:",
            "style-src 'self' 'unsafe-inline'",
            # TEMPORARY 'unsafe-inline' for scripts — replaced by nonces in phase 2.
            "script-src 'self' 'unsafe-inline'",
            "font-src 'self' data:",
            "media-src 'self' blob: https:",
            "connect-src 'self' https:",
            "frame-src 'self' https:",
        ]
    )


def _csp_header_name() -> bytes:
    report_only = os.getenv("CSP_REPORT_ONLY", "1").strip().lower() not in ("0", "false", "no", "off")
    return b"content-security-policy-report-only" if report_only else b"content-security-policy"


class SecurityHeadersMiddleware:
    """Pure-ASGI middleware that adds security headers to every HTTP response."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    if name.decode() not in headers:
                        headers[name.decode()] = value.decode()
                if is_production() and "strict-transport-security" not in headers:
                    headers["strict-transport-security"] = "max-age=63072000; includeSubDomains; preload"
                csp_name = _csp_header_name().decode()
                if "content-security-policy" not in headers and "content-security-policy-report-only" not in headers:
                    headers[csp_name] = build_csp()
            await send(message)

        await self.app(scope, receive, send_wrapper)


# ─── Production secret guard ──────────────────────────────────────────────────
def production_secret_problems() -> List[str]:
    """Report production-unsafe secrets. AUTH_SECRET is the crypto root (always
    used to mint/verify tokens) so it must be present + strong. ADMIN_PASSWORD and
    INTERNAL_TOOL_SECRET gate optional features that are *disabled* when unset, so
    we only flag the genuinely dangerous case of shipping the known placeholder
    value (or a too-short one) — not an intentional absence, which must not block
    an otherwise-valid deploy."""
    problems: List[str] = []

    auth = os.getenv("AUTH_SECRET", "")
    if not auth or auth == "change-me-in-production-elysium" or len(auth) < 32:
        problems.append("AUTH_SECRET")

    admin = os.getenv("ADMIN_PASSWORD", "")
    if admin and (admin == "change-me-strong-password" or len(admin) < 12):
        problems.append("ADMIN_PASSWORD")

    internal = os.getenv("INTERNAL_TOOL_SECRET", "")
    if internal and (internal == "change-me-internal-secret" or len(internal) < 32):
        problems.append("INTERNAL_TOOL_SECRET")

    return problems


def assert_production_secrets() -> None:
    """Refuse to boot in production with weak/default secrets. No-op outside
    production so local dev and tests are unaffected."""
    if not is_production():
        return
    problems = production_secret_problems()
    if problems:
        raise RuntimeError(
            "Refusing to start in production with weak or default secrets: "
            f"{', '.join(problems)}. Set strong, unique values (AUTH_SECRET / "
            "INTERNAL_TOOL_SECRET >= 32 chars)."
        )
