"""Persistence for Gold Standard visits (PRD §7).

Backed by the same SQLite file as ``TeamStore`` (``TEAM_DB_PATH``) so submitted /
export-ready records survive restarts. We keep this in a self-contained module
(mirroring ``audit/audit_log.py``) rather than editing the large ``team_store``
schema block — it owns its own ``gold_visits`` table on the shared DB.

Clinical free-text fields (transcript, AI draft note, gold note, signature) are
encrypted at rest with ``field_crypto`` (AES-256-GCM). The de-identified copies
(``transcript_deid`` / ``gold_note_deid``) are stored in clear so the operator
QA panel and export can read them without a key — by definition they no longer
contain PHI once the de-id step + human QA have run.

SSE progress queues for the async draft pipeline are ephemeral and kept in a
module-level in-memory registry keyed by ``gold_visit_id`` (the eligibility
router uses the same in-memory queue/ring pattern).
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional

import field_crypto

_LOCK = threading.Lock()

# ─── Visit status lifecycle ───────────────────────────────────────────────────
ST_CAPTURING = "CAPTURING"            # allocated; awaiting consent + audio
ST_CONSENT_DECLINED = "CONSENT_DECLINED"
ST_DRAFTING = "DRAFTING"              # audio uploaded; STT + draft running
ST_NEEDS_REVIEW = "NEEDS_REVIEW"      # draft ready for surgeon
ST_DEIDENTIFYING = "DEIDENTIFYING"    # submitted; PHI scrub running
ST_NEEDS_QA = "NEEDS_QA"              # de-id done; awaiting operator approval
ST_EXPORT_READY = "EXPORT_READY"      # operator approved
ST_EXPORTED = "EXPORTED"
ST_ERROR = "ERROR"

_ENCRYPTED_FIELDS = (
    "transcript",
    "transcript_turns",
    "ai_draft_note",
    "ai_draft_sections",
    "gold_note",
    "signature_image",
    "patient_name",
)

# Columns added after the initial release — applied as idempotent ALTERs so an
# existing team.db is migrated forward without a destructive rebuild.
_EXTRA_COLUMNS = {
    "patient_name_enc": "TEXT",
    "submitted_by": "TEXT",
    "submitted_by_role": "TEXT",
    "ai_draft_note_deid": "TEXT",
    "error_labels_deid_json": "TEXT",
    "prior_auth_deid_json": "TEXT",
    "tasks_json": "TEXT",
    "split": "TEXT",
    "deid_method_detail": "TEXT",
    "ai_draft_sections_enc": "TEXT",
}


def _db_path() -> str:
    base_dir = os.path.dirname(os.path.dirname(__file__))
    return os.getenv("TEAM_DB_PATH") or os.path.join(base_dir, "team.db")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _utcnow_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS gold_visits (
            id TEXT PRIMARY KEY,
            tenant_id TEXT,
            tenant_slug TEXT,
            record_num INTEGER NOT NULL DEFAULT 0,
            specialty TEXT,
            encounter_type TEXT,
            status TEXT NOT NULL,
            consent_given INTEGER,
            consent_method TEXT,
            consent_timestamp TEXT,
            baa_on_file INTEGER NOT NULL DEFAULT 0,
            signature_image_enc TEXT,
            audio_path TEXT,
            audio_mime TEXT,
            audio_duration_sec REAL,
            audio_deleted INTEGER NOT NULL DEFAULT 0,
            difficulty_tags_json TEXT,
            languages_json TEXT,
            stt_provider TEXT,
            transcript_enc TEXT,
            transcript_turns_enc TEXT,
            transcript_deid TEXT,
            ai_draft_note_enc TEXT,
            suggested_codes_json TEXT,
            gold_note_enc TEXT,
            gold_note_deid TEXT,
            error_labels_json TEXT,
            billing_codes_json TEXT,
            prior_auth_json TEXT,
            clinician_review_seconds INTEGER,
            clinician_id_hashed TEXT,
            patient_name_enc TEXT,
            submitted_by TEXT,
            submitted_by_role TEXT,
            ai_draft_note_deid TEXT,
            error_labels_deid_json TEXT,
            prior_auth_deid_json TEXT,
            tasks_json TEXT,
            split TEXT,
            deid_method TEXT,
            deid_method_detail TEXT,
            deid_meta_json TEXT,
            verified_by_operator INTEGER NOT NULL DEFAULT 0,
            pipeline_error TEXT,
            created_by TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            submitted_at TEXT,
            approved_at TEXT,
            approved_by TEXT,
            exported_at TEXT,
            export_destination TEXT
        );

        CREATE TABLE IF NOT EXISTS gold_declined_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_gold_visits_tenant ON gold_visits(tenant_id);
        CREATE INDEX IF NOT EXISTS idx_gold_visits_status ON gold_visits(status);
        """
    )
    # Forward-migrate older gold_visits tables (idempotent).
    existing = {r[1] for r in conn.execute("PRAGMA table_info(gold_visits)").fetchall()}
    for col, decl in _EXTRA_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE gold_visits ADD COLUMN {col} {decl}")


def init() -> None:
    with _LOCK:
        with _conn() as conn:
            _ensure_tables(conn)


# ─── Serialization helpers ────────────────────────────────────────────────────
def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    """Decrypt encrypted columns and parse JSON columns into a plain dict."""
    d = dict(row)
    out: Dict[str, Any] = {}
    for key, val in d.items():
        if key.endswith("_enc"):
            base = key[:-4]
            try:
                out[base] = field_crypto.decrypt_field(val) if val is not None else None
            except Exception:
                out[base] = None
        elif key.endswith("_json"):
            base = key[:-5]
            out[base] = json.loads(val) if val else None
        else:
            out[key] = val
    # Normalize booleans
    for b in ("consent_given", "baa_on_file", "audio_deleted", "verified_by_operator"):
        if out.get(b) is not None:
            out[b] = bool(out[b])
    return out


# ─── CRUD ─────────────────────────────────────────────────────────────────────
def create_visit(
    *,
    visit_id: str,
    tenant_id: Optional[str],
    tenant_slug: Optional[str],
    specialty: str,
    encounter_type: str,
    created_by: str,
) -> Dict[str, Any]:
    now = _utcnow_iso()
    with _LOCK:
        with _conn() as conn:
            _ensure_tables(conn)
            row = conn.execute(
                "SELECT COALESCE(MAX(record_num), 0) AS n FROM gold_visits WHERE IFNULL(tenant_id,'') = ?",
                (tenant_id or "",),
            ).fetchone()
            record_num = int(row["n"]) + 1
            conn.execute(
                """
                INSERT INTO gold_visits
                  (id, tenant_id, tenant_slug, record_num, specialty, encounter_type,
                   status, created_by, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    visit_id, tenant_id, tenant_slug, record_num, specialty,
                    encounter_type, ST_CAPTURING, created_by, now, now,
                ),
            )
    return get_visit(visit_id)  # type: ignore[return-value]


def get_visit(visit_id: str) -> Optional[Dict[str, Any]]:
    with _conn() as conn:
        _ensure_tables(conn)
        row = conn.execute("SELECT * FROM gold_visits WHERE id = ?", (visit_id,)).fetchone()
    return _row_to_dict(row) if row else None


def get_raw_row(visit_id: str) -> Optional[Dict[str, Any]]:
    """Like get_visit but does NOT decrypt — for ops that only need metadata."""
    with _conn() as conn:
        _ensure_tables(conn)
        row = conn.execute("SELECT * FROM gold_visits WHERE id = ?", (visit_id,)).fetchone()
    return dict(row) if row else None


def list_visits(
    *,
    tenant_id: Optional[str],
    tenant_scoped: bool,
    status: Optional[str] = None,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    clauses: List[str] = []
    params: List[Any] = []
    if tenant_scoped:
        clauses.append("IFNULL(tenant_id,'') = ?")
        params.append(tenant_id or "")
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(max(1, min(limit, 2000)))
    with _conn() as conn:
        _ensure_tables(conn)
        rows = conn.execute(
            f"SELECT * FROM gold_visits{where} ORDER BY created_at DESC LIMIT ?", params
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _set_columns(visit_id: str, columns: Dict[str, Any]) -> None:
    columns = dict(columns)
    columns["updated_at"] = _utcnow_iso()
    cols = ", ".join(f"{k} = ?" for k in columns)
    params = list(columns.values()) + [visit_id]
    with _LOCK:
        with _conn() as conn:
            _ensure_tables(conn)
            conn.execute(f"UPDATE gold_visits SET {cols} WHERE id = ?", params)


def update_visit(visit_id: str, **fields: Any) -> None:
    """Update arbitrary fields. Encrypts clinical free-text, JSON-encodes lists/dicts.

    Pass logical field names (e.g. ``transcript=``, ``error_labels=``); this maps
    them to the correct ``*_enc`` / ``*_json`` columns automatically.
    """
    columns: Dict[str, Any] = {}
    for key, val in fields.items():
        if key in _ENCRYPTED_FIELDS:
            columns[f"{key}_enc"] = field_crypto.encrypt_field(val) if val is not None else None
        elif key in (
            "difficulty_tags", "languages", "suggested_codes", "error_labels",
            "billing_codes", "prior_auth", "deid_meta",
            "error_labels_deid", "prior_auth_deid", "tasks",
        ):
            columns[f"{key}_json"] = json.dumps(val) if val is not None else None
        else:
            columns[key] = val
    if columns:
        _set_columns(visit_id, columns)


def delete_visit(visit_id: str) -> None:
    with _LOCK:
        with _conn() as conn:
            _ensure_tables(conn)
            conn.execute("DELETE FROM gold_visits WHERE id = ?", (visit_id,))


def record_declined(tenant_id: Optional[str]) -> None:
    with _LOCK:
        with _conn() as conn:
            _ensure_tables(conn)
            conn.execute(
                "INSERT INTO gold_declined_events (tenant_id, created_at) VALUES (?,?)",
                (tenant_id or "", _utcnow_iso()),
            )


def declined_count(tenant_id: Optional[str], tenant_scoped: bool) -> int:
    with _conn() as conn:
        _ensure_tables(conn)
        if tenant_scoped:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM gold_declined_events WHERE IFNULL(tenant_id,'') = ?",
                (tenant_id or "",),
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) AS c FROM gold_declined_events").fetchone()
    return int(row["c"]) if row else 0


def status_counts(tenant_id: Optional[str], tenant_scoped: bool) -> Dict[str, int]:
    where, params = "", []
    if tenant_scoped:
        where = " WHERE IFNULL(tenant_id,'') = ?"
        params = [tenant_id or ""]
    with _conn() as conn:
        _ensure_tables(conn)
        rows = conn.execute(
            f"SELECT status, COUNT(*) AS c FROM gold_visits{where} GROUP BY status", params
        ).fetchall()
    return {r["status"]: int(r["c"]) for r in rows}


def contributions_by_clinician(tenant_id: Optional[str], tenant_scoped: bool) -> Dict[str, int]:
    where, params = "", []
    if tenant_scoped:
        where = " WHERE IFNULL(tenant_id,'') = ?"
        params = [tenant_id or ""]
    with _conn() as conn:
        _ensure_tables(conn)
        rows = conn.execute(
            f"SELECT clinician_id_hashed AS h, COUNT(*) AS c FROM gold_visits"
            f"{where}{' AND' if where else ' WHERE'} clinician_id_hashed IS NOT NULL "
            f"GROUP BY clinician_id_hashed",
            params,
        ).fetchall()
    return {r["h"]: int(r["c"]) for r in rows}


def expired_audio_visits(retention_days: int) -> List[Dict[str, Any]]:
    """Visits whose audio is past the retention window and not yet deleted.

    Only returns rows that have completed STT (transcript present) so we never
    delete audio that hasn't been transcribed yet.
    """
    from datetime import timedelta

    cutoff = (datetime.utcnow() - timedelta(days=max(0, retention_days))).replace(microsecond=0).isoformat() + "Z"
    with _conn() as conn:
        _ensure_tables(conn)
        rows = conn.execute(
            "SELECT id, audio_path FROM gold_visits "
            "WHERE audio_deleted = 0 AND audio_path IS NOT NULL "
            "AND transcript_enc IS NOT NULL AND updated_at < ?",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


# ─── Ephemeral SSE queues for the async draft pipeline ────────────────────────
_QUEUES: Dict[str, "asyncio.Queue"] = {}
_RINGS: Dict[str, Deque[Dict[str, Any]]] = {}


def new_queue(visit_id: str) -> "asyncio.Queue":
    q: "asyncio.Queue" = asyncio.Queue()
    _QUEUES[visit_id] = q
    _RINGS[visit_id] = deque(maxlen=50)
    return q


def get_queue(visit_id: str) -> Optional["asyncio.Queue"]:
    return _QUEUES.get(visit_id)


def get_ring(visit_id: str) -> Deque[Dict[str, Any]]:
    return _RINGS.get(visit_id, deque(maxlen=50))


def emit(visit_id: str, event: str, data: Any) -> None:
    """Push an SSE event onto the visit's live queue + ring buffer (if any)."""
    entry = {"event": event, "data": data}
    ring = _RINGS.get(visit_id)
    if ring is not None:
        ring.append(entry)
    q = _QUEUES.get(visit_id)
    if q is not None:
        q.put_nowait(entry)


def finalize_stream(visit_id: str) -> None:
    """Pipeline reached a terminal state — drop the live queue so no client
    blocks waiting on it. The ring is kept briefly for late-connect replay."""
    _QUEUES.pop(visit_id, None)


def drop_streams(visit_id: str) -> None:
    """Fully release a visit's SSE state (queue + ring) — call once a late
    client has replayed the terminal event."""
    _QUEUES.pop(visit_id, None)
    _RINGS.pop(visit_id, None)
