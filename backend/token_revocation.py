"""Server-side token revocation (PRD-3).

All staff/admin JWTs now carry a ``jti`` claim. Logout records the jti here, and
every decode path checks it, so a token can be invalidated before its natural
expiry. Backed by the same SQLite file as TeamStore (``TEAM_DB_PATH``); tokens
minted before this change simply have no jti and are treated as not-revoked.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from typing import Optional

import jwt

AUTH_SECRET = os.getenv("AUTH_SECRET", "change-me-in-production-elysium")
ALGORITHM = "HS256"


def _db_path() -> str:
    base_dir = os.path.dirname(__file__)
    return os.getenv("TEAM_DB_PATH") or os.path.join(base_dir, "team.db")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS revoked_tokens (
            jti TEXT PRIMARY KEY,
            exp INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )


def _now_ts() -> int:
    return int(datetime.utcnow().timestamp())


def revoke_jti(jti: str, exp_ts: int) -> None:
    if not jti:
        return
    with _conn() as conn:
        _ensure_table(conn)
        conn.execute(
            "INSERT OR IGNORE INTO revoked_tokens (jti, exp, created_at) VALUES (?, ?, ?)",
            (jti, int(exp_ts or 0), datetime.utcnow().replace(microsecond=0).isoformat()),
        )
        # Opportunistic GC of rows whose tokens expired over a day ago.
        conn.execute("DELETE FROM revoked_tokens WHERE exp > 0 AND exp < ?", (_now_ts() - 86400,))


def is_revoked(jti: Optional[str]) -> bool:
    if not jti:
        return False
    # Called on every token decode. Fail OPEN on a DB hiccup (locked/unavailable):
    # the token is still cryptographically valid + unexpired, and revocation is a
    # secondary control — a transient DB error must not 500 all authentication.
    try:
        with _conn() as conn:
            _ensure_table(conn)
            row = conn.execute("SELECT 1 FROM revoked_tokens WHERE jti = ?", (jti,)).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def revoke_token(token: str, *, secret: str = AUTH_SECRET) -> bool:
    """Decode (ignoring expiry) just enough to read jti+exp, then revoke. Returns
    True if a jti was found and revoked. Used by the logout endpoints."""
    try:
        payload = jwt.decode(
            token, secret, algorithms=[ALGORITHM], options={"verify_exp": False}
        )
    except jwt.PyJWTError:
        return False
    jti = payload.get("jti")
    if not jti:
        return False
    revoke_jti(str(jti), int(payload.get("exp") or 0))
    return True
