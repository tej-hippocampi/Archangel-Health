"""Patient session auth for the patient-facing dashboard (PRD-1).

Patients authenticate by entering their health-system code + resource code on the
(cross-origin) landing app, which calls ``/api/patient/by-codes``. That endpoint
mints a short-lived, single-use **entry token** and returns it in the dashboard
URL (``?k=<entry_token>``). The browser then performs a first-party navigation to
the backend-origin page route, which consumes the entry token and sets an
HttpOnly, Secure, SameSite=Lax ``pt_session`` cookie (an 8h signed session JWT).

All subsequent same-origin ``/api/patient/*`` calls carry the cookie automatically,
so the patient JS needs no Authorization-header changes. A pure-ASGI middleware
decodes the cookie once per request and stashes the resolved ``PatientSession`` in
a ContextVar that ``main._assert_staff_can_access_patient`` reads to authorize the
patient path (staff continue to use their existing Bearer tokens).

Single-use (entry tokens) and revocation (logout) are tracked by jti in a tiny
SQLite table that lives in the same DB file as ``TeamStore`` (``TEAM_DB_PATH``).
"""

from __future__ import annotations

import contextvars
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from typing import Any, Dict, Optional

import jwt
import realm

from team_store import connect_team_db

# ─── Config ──────────────────────────────────────────────────────────────────
AUTH_SECRET = os.getenv("AUTH_SECRET", "change-me-in-production-elysium")
ALGORITHM = "HS256"
PATIENT_SESSION_TTL_MIN = 8 * 60      # 8 hours
ENTRY_TOKEN_TTL_MIN = 5               # 5 minutes, single use
COOKIE_NAME = "pt_session"


def _is_production() -> bool:
    return os.getenv("ENV", "").strip().lower() == "production"


@dataclass
class PatientSession:
    patient_id: str
    health_system_id: Optional[str]
    jti: str


# ─── jti store (single-use entry tokens + session revocation) ────────────────
def _db_path() -> str:
    """Resolve at call time so tests can point TEAM_DB_PATH at a temp file."""
    # Preserve the existing live revocation ledger, including its default path.
    live_path = os.getenv("TEAM_DB_PATH") or os.path.join(os.path.dirname(__file__), "team.db")
    return realm.sandbox_db_path(live_path) if realm.is_sandbox() else live_path


def _conn() -> sqlite3.Connection:
    # Shared team.db opener: WAL + 30s busy timeout. The jti table is touched on
    # the patient auth path, so a 5s default timeout here shows up as a failed
    # login rather than a slow one.
    return connect_team_db(_db_path())


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS patient_session_jti (
            jti TEXT PRIMARY KEY,
            kind TEXT NOT NULL,          -- 'entry_consumed' | 'session_revoked'
            exp INTEGER NOT NULL,        -- unix seconds; row may be GC'd after this
            created_at TEXT NOT NULL
        )
        """
    )


def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _record_jti(jti: str, kind: str, exp_ts: int) -> bool:
    if not jti:
        return False
    with _conn() as conn:
        _ensure_table(conn)
        result = conn.execute(
            "INSERT OR IGNORE INTO patient_session_jti (jti, kind, exp, created_at) "
            "VALUES (?, ?, ?, ?)",
            (jti, kind, int(exp_ts), datetime.utcnow().replace(microsecond=0).isoformat()),
        )
        # Opportunistic GC of long-expired rows.
        conn.execute("DELETE FROM patient_session_jti WHERE exp < ?", (_now_ts() - 86400,))
        return result.rowcount == 1


def _has_jti(jti: str, kind: str) -> bool:
    if not jti:
        return False
    # If revocation state is unavailable, do not authorize the session.
    try:
        with _conn() as conn:
            _ensure_table(conn)
            row = conn.execute(
                "SELECT 1 FROM patient_session_jti WHERE jti = ? AND kind = ?",
                (jti, kind),
            ).fetchone()
        return row is not None
    except sqlite3.Error:
        return True


# ─── Token mint / decode ─────────────────────────────────────────────────────
def _encode(payload: Dict[str, Any]) -> str:
    return jwt.encode(payload, AUTH_SECRET, algorithm=ALGORITHM)


def _decode(token: str) -> Optional[Dict[str, Any]]:
    try:
        payload = jwt.decode(token, AUTH_SECRET, algorithms=[ALGORITHM])
        return payload if realm.token_matches(payload) else None
    except jwt.PyJWTError:
        return None


def create_entry_token(
    patient_id: str, health_system_id: Optional[str], *, ttl_minutes: int = ENTRY_TOKEN_TTL_MIN
) -> str:
    # Interactive code exchanges keep their five-minute lifetime. Scheduled
    # patient email links may explicitly allow time to open the message.
    exp = datetime.utcnow() + timedelta(minutes=max(1, min(ttl_minutes, 24 * 60)))
    return _encode(
        realm.stamp({
            "typ": "patient_entry",
            "pid": patient_id,
            "tid": health_system_id or "",
            "jti": uuid.uuid4().hex,
            "exp": exp,
        })
    )


def consume_entry_token(token: str) -> Optional[PatientSession]:
    """Validate + single-use consume an entry token. Returns None if invalid,
    expired, malformed, or already consumed."""
    payload = _decode(token or "")
    if not payload or payload.get("typ") != "patient_entry":
        return None
    jti = str(payload.get("jti") or "")
    pid = str(payload.get("pid") or "")
    if not jti or not pid:
        return None
    exp_ts = int(payload.get("exp") or 0)
    try:
        if not _record_jti(jti, "entry_consumed", exp_ts or (_now_ts() + ENTRY_TOKEN_TTL_MIN * 60)):
            return None
    except sqlite3.Error:
        return None
    return PatientSession(patient_id=pid, health_system_id=(payload.get("tid") or None), jti=jti)


def create_patient_session(patient_id: str, health_system_id: Optional[str]) -> str:
    exp = datetime.utcnow() + timedelta(minutes=PATIENT_SESSION_TTL_MIN)
    return _encode(
        realm.stamp({
            "typ": "patient",
            "pid": patient_id,
            "tid": health_system_id or "",
            "jti": uuid.uuid4().hex,
            "exp": exp,
        })
    )


def decode_patient_session(token: str) -> Optional[PatientSession]:
    payload = _decode(token or "")
    if not payload or payload.get("typ") != "patient":
        return None
    jti = str(payload.get("jti") or "")
    pid = str(payload.get("pid") or "")
    if not pid or not jti:
        return None
    if jti and _has_jti(jti, "session_revoked"):
        return None  # logged out / revoked
    return PatientSession(patient_id=pid, health_system_id=(payload.get("tid") or None), jti=jti)


def revoke_patient_session(token: str) -> None:
    payload = _decode(token or "")
    if not payload or payload.get("typ") != "patient":
        return
    jti = str(payload.get("jti") or "")
    exp_ts = int(payload.get("exp") or 0)
    _record_jti(jti, "session_revoked", exp_ts or (_now_ts() + PATIENT_SESSION_TTL_MIN * 60))


# ─── Cookie helpers (centralized attributes) ─────────────────────────────────
def patient_cookie_name() -> str:
    return COOKIE_NAME + "_sandbox" if realm.is_sandbox() else COOKIE_NAME


def set_patient_session_cookie(response, patient_id: str, health_system_id: Optional[str]) -> None:
    token = create_patient_session(patient_id, health_system_id)
    response.set_cookie(
        key=patient_cookie_name(),
        value=token,
        max_age=PATIENT_SESSION_TTL_MIN * 60,
        httponly=True,
        secure=_is_production(),
        samesite="lax",
        path="/",
    )


def clear_patient_session_cookie(response) -> None:
    response.delete_cookie(key=patient_cookie_name(), path="/")


# ─── Per-request ContextVar + ASGI middleware ────────────────────────────────
_current_patient_session: contextvars.ContextVar[Optional[PatientSession]] = (
    contextvars.ContextVar("current_patient_session", default=None)
)


def current_patient_session() -> Optional[PatientSession]:
    return _current_patient_session.get()


class PatientSessionMiddleware:
    """Pure-ASGI middleware: decode the pt_session cookie once per HTTP request
    and stash the resolved PatientSession in a ContextVar. Pure-ASGI (not
    BaseHTTPMiddleware) so the ContextVar is visible to the route handler."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        ps: Optional[PatientSession] = None
        try:
            # Merge all Cookie headers (HTTP/2 may split them) before parsing.
            cookie_blob = "; ".join(
                val.decode("latin-1") for key, val in scope.get("headers", []) if key == b"cookie"
            )
            if cookie_blob:
                jar = SimpleCookie()
                jar.load(cookie_blob)
                morsel = jar.get(patient_cookie_name())
                if morsel and morsel.value:
                    ps = decode_patient_session(morsel.value)
        except Exception:
            ps = None
        reset_token = _current_patient_session.set(ps)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_patient_session.reset(reset_token)
