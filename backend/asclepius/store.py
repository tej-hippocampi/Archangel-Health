"""AsclepiusStore — SQLite persistence for the Expert Evaluation Portal.

Own DB file (asclepius.db / ASCLEPIUS_DB_PATH), raw sqlite3, mirroring the
team_store.py conventions (_conn / _init_schema / row_factory). No PHI.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime
from typing import Any, Optional

from .packaging import dedupe_hash, package_submission


def _utcnow_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


class AsclepiusStore:
    def __init__(self, db_path: Optional[str] = None):
        base_dir = os.path.dirname(__file__)
        self.db_path = (
            db_path
            or os.getenv("ASCLEPIUS_DB_PATH")
            or os.path.join(base_dir, "asclepius.db")
        )
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS submissions (
                    submission_id TEXT PRIMARY KEY,
                    task_id TEXT,
                    specialty TEXT,
                    difficulty TEXT,
                    verdict TEXT,
                    confidence TEXT,
                    annotator_credential TEXT,
                    grounded INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'submitted',
                    time_spent_sec INTEGER,
                    dedupe_hash TEXT,
                    task_json TEXT,
                    submission_json TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS records (
                    record_id TEXT PRIMARY KEY,
                    submission_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    specialty TEXT,
                    difficulty TEXT,
                    grounded INTEGER NOT NULL DEFAULT 0,
                    record_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS exports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT NOT NULL,
                    profile_name TEXT NOT NULL,
                    record_count INTEGER NOT NULL,
                    filters_json TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_records_type ON records(type);
                CREATE INDEX IF NOT EXISTS idx_records_specialty ON records(specialty);
                """
            )

    # ── Ingest ────────────────────────────────────────────────────────────
    def ingest_submission(self, submission: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        """Store a raw submission, auto-package into records. Returns a summary."""
        sid = submission.get("submission_id") or f"s-{uuid.uuid4().hex[:10]}"
        submission["submission_id"] = sid
        now = _utcnow_iso()
        cred = (submission.get("annotator") or {}).get("credentials")
        recs = package_submission(submission, task)
        grounded = 1 if any(r.get("grounded") for r in recs) else 0
        dh = dedupe_hash(
            task.get("prompt", ""),
            *[c.get("text", "") for c in task.get("candidate_answers", [])],
        )
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO submissions
                   (submission_id, task_id, specialty, difficulty, verdict, confidence,
                    annotator_credential, grounded, status, time_spent_sec, dedupe_hash,
                    task_json, submission_json, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    sid,
                    task.get("task_id"),
                    task.get("specialty"),
                    task.get("difficulty"),
                    submission.get("verdict"),
                    submission.get("confidence"),
                    cred,
                    grounded,
                    submission.get("status") or "submitted",
                    submission.get("time_spent_sec"),
                    dh,
                    json.dumps(task),
                    json.dumps(submission),
                    now,
                ),
            )
            conn.execute("DELETE FROM records WHERE submission_id = ?", (sid,))
            for r in recs:
                conn.execute(
                    """INSERT INTO records
                       (record_id, submission_id, type, specialty, difficulty, grounded, record_json, created_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        f"r-{uuid.uuid4().hex[:10]}",
                        sid,
                        r.get("type"),
                        task.get("specialty"),
                        task.get("difficulty"),
                        1 if r.get("grounded") else 0,
                        json.dumps(r),
                        now,
                    ),
                )
        return {"submission_id": sid, "records_created": len(recs)}

    # ── Reads ─────────────────────────────────────────────────────────────
    def list_submissions(self, limit: int = 500) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT submission_id, task_id, specialty, difficulty, verdict, confidence,
                          annotator_credential, grounded, status, time_spent_sec, created_at
                   FROM submissions ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_records(
        self,
        *,
        record_type: Optional[str] = None,
        specialty: Optional[str] = None,
        grounded_only: bool = False,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        clauses, params = [], []
        if record_type and record_type != "all":
            clauses.append("type = ?")
            params.append(record_type)
        if specialty and specialty != "all":
            clauses.append("specialty = ?")
            params.append(specialty)
        if grounded_only:
            clauses.append("grounded = 1")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT record_json FROM records{where} ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [json.loads(r["record_json"]) for r in rows]

    def stats(self) -> dict[str, Any]:
        with self._conn() as conn:
            subs = conn.execute("SELECT COUNT(*) c FROM submissions").fetchone()["c"]
            recs = conn.execute("SELECT COUNT(*) c FROM records").fetchone()["c"]
            grounded = conn.execute("SELECT COUNT(*) c FROM records WHERE grounded = 1").fetchone()["c"]
            by_type = {
                r["type"]: r["c"]
                for r in conn.execute("SELECT type, COUNT(*) c FROM records GROUP BY type").fetchall()
            }
            by_specialty = {
                (r["specialty"] or "unknown"): r["c"]
                for r in conn.execute("SELECT specialty, COUNT(*) c FROM records GROUP BY specialty").fetchall()
            }
            exports = conn.execute("SELECT COUNT(*) c FROM exports").fetchone()["c"]
        return {
            "submissions": subs,
            "records": recs,
            "grounded_records": grounded,
            "by_type": by_type,
            "by_specialty": by_specialty,
            "exports": exports,
        }

    def specialties(self) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT specialty FROM records WHERE specialty IS NOT NULL ORDER BY specialty"
            ).fetchall()
        return [r["specialty"] for r in rows]

    def log_export(self, batch_id: str, profile_name: str, count: int, filters: dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO exports (batch_id, profile_name, record_count, filters_json, created_at)
                   VALUES (?,?,?,?,?)""",
                (batch_id, profile_name, count, json.dumps(filters), _utcnow_iso()),
            )

    def list_exports(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT batch_id, profile_name, record_count, filters_json, created_at FROM exports ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


_STORE: Optional[AsclepiusStore] = None


def get_store() -> AsclepiusStore:
    global _STORE
    if _STORE is None:
        _STORE = AsclepiusStore()
    return _STORE
