"""Pure-ASGI middleware that records ePHI access to the audit log (PRD-5).

Installed as the innermost middleware so the patient-session ContextVar (set by
PatientSessionMiddleware) and the request headers are both visible when it records
the event after the route runs. Only minimum-necessary metadata is captured — the
request/response bodies (which contain PHI) are never read here.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

from audit import audit_log

# ePHI-bearing surfaces. Auth/onboarding/health/static/docs are intentionally
# excluded (no PHI), keeping the trail focused on patient-data access.
_AUDITED_PREFIXES = (
    "/api/patient", "/api/patients", "/api/episodes", "/api/escalations",
    "/api/intake-forms", "/api/eligibility", "/api/digital-care-companion",
    "/api/avatar/chat", "/api/pre-op/intake", "/api/doctor/patient", "/admin",
)
_AUDITED_PAGE_PREFIXES = ("/patient/", "/doctor/patient/")
_PID_RE = re.compile(r"/(?:patient|patients|episodes)/([^/?]+)")


def _is_audited(path: str) -> bool:
    if path.startswith(_AUDITED_PAGE_PREFIXES):
        return True
    return path.startswith(_AUDITED_PREFIXES)


def _header(scope, name: bytes) -> str:
    for k, v in scope.get("headers", []):
        if k == name:
            return v.decode("latin-1")
    return ""


def _client_ip(scope) -> str:
    xff = _header(scope, b"x-forwarded-for")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    client = scope.get("client")
    return client[0] if client else ""


def _bearer(scope) -> str:
    auth = _header(scope, b"authorization")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return ""


def _outcome(status: Optional[int]) -> str:
    if status is None:
        return "error"
    if status < 400:
        return "success"
    if status >= 500:
        return "error"
    return "denied"  # 4xx (401/403/404/422/429/...)


def _resolve_actor(scope, path: str) -> Tuple[str, Optional[str]]:
    # Patient session (set by PatientSessionMiddleware) takes precedence.
    try:
        from patient_session import current_patient_session

        ps = current_patient_session()
        if ps is not None:
            return "patient", ps.patient_id
    except Exception:
        pass

    token = _bearer(scope)
    if token:
        try:
            from tenant_jwt import decode_tenant_staff_token

            td = decode_tenant_staff_token(token)
            if td:
                return f"staff:{td.get('role', '') or 'staff'}", td.get("sub")
        except Exception:
            pass
        try:
            import auth as auth_module

            sub = auth_module._decode_token(token)  # noqa: SLF001
            if sub:
                return "staff", sub
        except Exception:
            pass
        if path.startswith("/admin"):
            return "admin", "admin"
        return "staff", "unknown"
    return "anonymous", None


class AuditMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if not _is_audited(path):
            await self.app(scope, receive, send)
            return

        status_box = {"status": None}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_box["status"] = message.get("status")
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            try:
                self._record(scope, path, status_box["status"])
            except Exception as exc:  # pragma: no cover
                print(f"[audit] middleware error: {exc}")

    def _record(self, scope, path: str, status: Optional[int]) -> None:
        actor_type, actor_id = _resolve_actor(scope, path)
        m = _PID_RE.search(path)
        patient_id = m.group(1) if m else None
        resource_type = "admin" if path.startswith("/admin") else "patient_data"
        audit_log.record(
            actor_type=actor_type,
            actor_id=actor_id,
            action=scope.get("method", ""),
            outcome=_outcome(status),
            resource_type=resource_type,
            resource=path,
            patient_id=patient_id,
            source_ip=_client_ip(scope),
            user_agent=_header(scope, b"user-agent"),
            detail={"status": status},
        )
