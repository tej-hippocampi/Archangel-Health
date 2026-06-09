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
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from typing import Any, Dict, Optional

from jose import JWTError, jwt

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
    base_dir = os.path.dirname(__file__)
    return os.getenv("TEAM_DB_PATH") or os.path.join(base_dir, "team.db")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    return conn


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
    return int(datetime.utcnow().timestamp())


def _record_jti(jti: str, kind: str, exp_ts: int) -> None:
    if not jti:
        return
    with _conn() as conn:
        _ensure_table(conn)
        conn.execute(
            "INSERT OR IGNORE INTO patient_session_jti (jti, kind, exp, created_at) "
            "VALUES (?, ?, ?, ?)",
            (jti, kind, int(exp_ts), datetime.utcnow().replace(microsecond=0).isoformat()),
        )
        # Opportunistic GC of long-expired rows.
        conn.execute("DELETE FROM patient_session_jti WHERE exp < ?", (_now_ts() - 86400,))


def _has_jti(jti: str, kind: str) -> bool:
    if not jti:
        return False
    # Fail open on a DB hiccup so a transient lock can't 500 the auth path; the
    # JWT itself is still validated cryptographically by the caller.
    try:
        with _conn() as conn:
            _ensure_table(conn)
            row = conn.execute(
                "SELECT 1 FROM patient_session_jti WHERE jti = ? AND kind = ?",
                (jti, kind),
            ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


# ─── Token mint / decode ─────────────────────────────────────────────────────
def _encode(payload: Dict[str, Any]) -> str:
    return jwt.encode(payload, AUTH_SECRET, algorithm=ALGORITHM)


def _decode(token: str) -> Optional[Dict[str, Any]]:
    try:
        return jwt.decode(token, AUTH_SECRET, algorithms=[ALGORITHM])
    except JWTError:
        return None


def create_entry_token(patient_id: str, health_system_id: Optional[str]) -> str:
    exp = datetime.utcnow() + timedelta(minutes=ENTRY_TOKEN_TTL_MIN)
    return _encode(
        {
            "typ": "patient_entry",
            "pid": patient_id,
            "tid": health_system_id or "",
            "jti": uuid.uuid4().hex,
            "exp": exp,
        }
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
    if _has_jti(jti, "entry_consumed"):
        return None  # already used
    exp_ts = int(payload.get("exp") or 0)
    _record_jti(jti, "entry_consumed", exp_ts or (_now_ts() + ENTRY_TOKEN_TTL_MIN * 60))
    return PatientSession(patient_id=pid, health_system_id=(payload.get("tid") or None), jti=jti)


def create_patient_session(patient_id: str, health_system_id: Optional[str]) -> str:
    exp = datetime.utcnow() + timedelta(minutes=PATIENT_SESSION_TTL_MIN)
    return _encode(
        {
            "typ": "patient",
            "pid": patient_id,
            "tid": health_system_id or "",
            "jti": uuid.uuid4().hex,
            "exp": exp,
        }
    )


def decode_patient_session(token: str) -> Optional[PatientSession]:
    payload = _decode(token or "")
    if not payload or payload.get("typ") != "patient":
        return None
    jti = str(payload.get("jti") or "")
    pid = str(payload.get("pid") or "")
    if not pid:
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
def set_patient_session_cookie(response, patient_id: str, health_system_id: Optional[str]) -> None:
    token = create_patient_session(patient_id, health_system_id)
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=PATIENT_SESSION_TTL_MIN * 60,
        httponly=True,
        secure=_is_production(),
        samesite="lax",
        path="/",
    )


def clear_patient_session_cookie(response) -> None:
    response.delete_cookie(key=COOKIE_NAME, path="/")


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
                morsel = jar.get(COOKIE_NAME)
                if morsel and morsel.value:
                    ps = decode_patient_session(morsel.value)
        except Exception:
            ps = None
        reset_token = _current_patient_session.set(ps)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_patient_session.reset(reset_token)
