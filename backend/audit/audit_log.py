"""Tamper-evident, persistent audit log for ePHI access (PRD-5).

HIPAA §164.312(b) requires recording and examining activity in systems that
contain ePHI, and §164.316(b)(2) requires retaining that documentation for six
years. This module provides an **append-only, hash-chained** audit trail:

  - Every record stores ``prev_hash`` (the previous row's hash) and
    ``row_hash = sha256(prev_hash + canonical(row))``. Altering or deleting any
    row breaks the chain, which ``verify()`` detects.
  - Only minimum-necessary metadata is stored (who / what / when / where /
    outcome) — never request bodies or clinical content.

Backed by the same SQLite file as TeamStore (``TEAM_DB_PATH``). In production point
that at a WORM / object-lock-backed volume (or ship events to an append-only sink)
so the trail is immutable at the storage layer too. We never auto-delete: 6-year
retention is a floor, and deleting would break the chain — archive/rotate at the
ops layer instead.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from team_store import connect_team_db

log = logging.getLogger("audit")

_LOCK = threading.Lock()

# Paths whose audit_events table we have already created this process. The
# CREATE TABLE used to run on EVERY write, inside the global lock and ahead of
# the chain SELECT + INSERT: a schema round-trip per ePHI access, on a database
# that was in rollback-journal mode. It only ever needs to happen once per file.
_TABLE_READY: set = set()
_TABLE_LOCK = threading.Lock()


def _db_path() -> str:
    # Realm-keyed (Sandbox PRD §1.2): a sandbox tenant's requests are audited
    # into the sandbox team DB, not the live trail an admin reads.
    import realm as _realm  # noqa: PLC0415

    return _realm.paths()["team"]


def _conn() -> sqlite3.Connection:
    # Shared team.db opener: WAL (readers no longer block the writer) + a 30s
    # busy timeout. Without it, a busy moment raised "database is locked" here
    # and the except below ate the ePHI access record.
    return connect_team_db(_db_path())


def _ensure_table_once(conn: sqlite3.Connection, db_path: str) -> None:
    """_ensure_table, hoisted off the per-write hot path. Keyed on the resolved
    path so a test that repoints TEAM_DB_PATH still gets its table created."""
    key = os.path.abspath(db_path)
    if key in _TABLE_READY:
        return
    with _TABLE_LOCK:
        _ensure_table(conn)
        _TABLE_READY.add(key)


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            actor_type TEXT NOT NULL,
            actor_id TEXT,
            action TEXT NOT NULL,
            resource_type TEXT,
            resource TEXT,
            patient_id TEXT,
            source_ip TEXT,
            user_agent TEXT,
            outcome TEXT NOT NULL,
            detail_json TEXT,
            prev_hash TEXT NOT NULL,
            row_hash TEXT NOT NULL
        )
        """
    )


def _utcnow_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _canonical(fields: Dict[str, Any]) -> str:
    return json.dumps(fields, sort_keys=True, separators=(",", ":"), default=str)


def record(
    *,
    actor_type: str,
    actor_id: Optional[str],
    action: str,
    outcome: str,
    resource_type: Optional[str] = None,
    resource: Optional[str] = None,
    patient_id: Optional[str] = None,
    source_ip: Optional[str] = None,
    user_agent: Optional[str] = None,
    detail: Optional[Dict[str, Any]] = None,
) -> None:
    """Append one tamper-evident audit event. Never raises into the request path —
    a failure to audit must not break the user's request (it is logged instead)."""
    ts = _utcnow_iso()
    fields = {
        "ts": ts,
        "actor_type": actor_type,
        "actor_id": actor_id or "",
        "action": action,
        "resource_type": resource_type or "",
        "resource": resource or "",
        "patient_id": patient_id or "",
        "source_ip": source_ip or "",
        "user_agent": (user_agent or "")[:300],
        "outcome": outcome,
        "detail": detail or {},
    }
    db_path = _db_path()
    try:
        with _LOCK:
            with connect_team_db(db_path) as conn:
                _ensure_table_once(conn, db_path)
                row = conn.execute(
                    "SELECT row_hash FROM audit_events ORDER BY id DESC LIMIT 1"
                ).fetchone()
                prev_hash = row["row_hash"] if row else ""
                row_hash = hashlib.sha256(
                    (prev_hash + _canonical(fields)).encode("utf-8")
                ).hexdigest()
                conn.execute(
                    """
                    INSERT INTO audit_events
                      (ts, actor_type, actor_id, action, resource_type, resource,
                       patient_id, source_ip, user_agent, outcome, detail_json,
                       prev_hash, row_hash)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        ts, actor_type, fields["actor_id"], action,
                        fields["resource_type"], fields["resource"], fields["patient_id"],
                        fields["source_ip"], fields["user_agent"], outcome,
                        _canonical(detail or {}), prev_hash, row_hash,
                    ),
                )
    except Exception as exc:  # audit must never break a request
        # A dropped record is a HIPAA §164.312(b) gap: the access happened and
        # nothing says so. Still no re-raise (an audit outage must not become a
        # clinical outage), but it goes out at ERROR with the actor and the
        # route so alerting can see it. A print on stdout could not.
        log.error(
            "DROPPED ePHI audit event: actor=%s/%s action=%s resource=%s "
            "patient=%s outcome=%s db=%s: %s",
            actor_type, actor_id or "-", action, resource or "-",
            patient_id or "-", outcome, db_path, exc,
            exc_info=True,
        )


def _recompute_hash(r: sqlite3.Row, prev_hash: str) -> str:
    fields = {
        "ts": r["ts"],
        "actor_type": r["actor_type"],
        "actor_id": r["actor_id"] or "",
        "action": r["action"],
        "resource_type": r["resource_type"] or "",
        "resource": r["resource"] or "",
        "patient_id": r["patient_id"] or "",
        "source_ip": r["source_ip"] or "",
        "user_agent": r["user_agent"] or "",
        "outcome": r["outcome"],
        "detail": json.loads(r["detail_json"] or "{}"),
    }
    return hashlib.sha256((prev_hash + _canonical(fields)).encode("utf-8")).hexdigest()


def verify() -> Dict[str, Any]:
    """Recompute the hash chain end-to-end. Returns {ok, count, broken_at_id}."""
    with _conn() as conn:
        _ensure_table(conn)
        rows = conn.execute("SELECT * FROM audit_events ORDER BY id ASC").fetchall()
    prev = ""
    for r in rows:
        if r["prev_hash"] != prev or _recompute_hash(r, prev) != r["row_hash"]:
            return {"ok": False, "count": len(rows), "broken_at_id": r["id"]}
        prev = r["row_hash"]
    return {"ok": True, "count": len(rows), "broken_at_id": None}


def list_events(
    *, limit: int = 200, patient_id: Optional[str] = None, actor_id: Optional[str] = None,
    since: Optional[str] = None,
) -> List[Dict[str, Any]]:
    clauses, params = [], []
    if patient_id:
        clauses.append("patient_id = ?"); params.append(patient_id)
    if actor_id:
        clauses.append("actor_id = ?"); params.append(actor_id)
    if since:
        clauses.append("ts >= ?"); params.append(since)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(max(1, min(limit, 1000)))
    with _conn() as conn:
        _ensure_table(conn)
        rows = conn.execute(
            f"SELECT id, ts, actor_type, actor_id, action, resource_type, resource, "
            f"patient_id, source_ip, user_agent, outcome, detail_json FROM audit_events"
            f"{where} ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["detail"] = json.loads(d.pop("detail_json") or "{}")
        out.append(d)
    return out
