import json
import os
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional


def _utcnow_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


class TeamStore:
    def __init__(self, db_path: Optional[str] = None):
        base_dir = os.path.dirname(__file__)
        self.db_path = db_path or os.getenv("TEAM_DB_PATH") or os.path.join(base_dir, "team.db")
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS episodes (
                    patient_id TEXT PRIMARY KEY,
                    open_date TEXT NOT NULL,
                    close_date TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    procedure_type TEXT,
                    clinic_code TEXT,
                    resource_code TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS event_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    patient_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    payload_json TEXT,
                    episode_open_date TEXT
                );

                CREATE TABLE IF NOT EXISTS survey_sends (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    patient_id TEXT NOT NULL,
                    survey_day INTEGER NOT NULL,
                    sent_at TEXT NOT NULL,
                    UNIQUE(patient_id, survey_day)
                );

                CREATE TABLE IF NOT EXISTS survey_responses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    patient_id TEXT NOT NULL,
                    survey_day INTEGER NOT NULL,
                    answers_json TEXT NOT NULL,
                    score REAL,
                    tier TEXT,
                    submitted_at TEXT NOT NULL,
                    UNIQUE(patient_id, survey_day)
                );

                CREATE TABLE IF NOT EXISTS escalations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    patient_id TEXT NOT NULL,
                    tier INTEGER NOT NULL,
                    trigger_type TEXT NOT NULL,
                    consent TEXT,
                    consent_at TEXT,
                    message TEXT,
                    resolved INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    conversation_snapshot TEXT
                );

                CREATE TABLE IF NOT EXISTS daily_reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    patient_id TEXT NOT NULL,
                    reminder_date TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    UNIQUE(patient_id, reminder_date)
                );

                CREATE TABLE IF NOT EXISTS preop_intake_submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    patient_id TEXT NOT NULL,
                    specialty TEXT,
                    form_template_name TEXT,
                    form_data_json TEXT NOT NULL,
                    submitted_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_event_logs_patient ON event_logs(patient_id);
                CREATE INDEX IF NOT EXISTS idx_event_logs_occured ON event_logs(occurred_at);
                CREATE INDEX IF NOT EXISTS idx_escalations_patient ON escalations(patient_id);
                CREATE INDEX IF NOT EXISTS idx_escalations_created ON escalations(created_at);
                CREATE INDEX IF NOT EXISTS idx_preop_intake_patient ON preop_intake_submissions(patient_id);
                """
            )

    def ensure_episode(
        self,
        *,
        patient_id: str,
        open_date: Optional[str] = None,
        procedure_type: str = "",
        clinic_code: str = "",
        resource_code: str = "",
    ) -> Dict[str, Any]:
        today = date.today()
        episode_open = date.fromisoformat(open_date) if open_date else today
        episode_close = episode_open + timedelta(days=29)
        created_at = _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO episodes (patient_id, open_date, close_date, status, procedure_type, clinic_code, resource_code, created_at)
                VALUES (?, ?, ?, 'open', ?, ?, ?, ?)
                ON CONFLICT(patient_id) DO UPDATE SET
                    procedure_type = excluded.procedure_type,
                    clinic_code = COALESCE(excluded.clinic_code, episodes.clinic_code),
                    resource_code = COALESCE(excluded.resource_code, episodes.resource_code)
                """,
                (
                    patient_id,
                    episode_open.isoformat(),
                    episode_close.isoformat(),
                    procedure_type,
                    clinic_code or None,
                    resource_code or None,
                    created_at,
                ),
            )
        return self.get_episode(patient_id) or {}

    def get_episode(self, patient_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM episodes WHERE patient_id = ?", (patient_id,)).fetchone()
            return dict(row) if row else None

    def list_active_episodes(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM episodes WHERE status = 'open'").fetchall()
            return [dict(r) for r in rows]

    def log_event(
        self,
        *,
        patient_id: str,
        event_type: str,
        occurred_at: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        ts = occurred_at or _utcnow_iso()
        episode = self.get_episode(patient_id)
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO event_logs (patient_id, event_type, occurred_at, payload_json, episode_open_date)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    patient_id,
                    event_type,
                    ts,
                    json.dumps(payload or {}),
                    (episode or {}).get("open_date"),
                ),
            )

    def get_events(self, patient_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM event_logs WHERE patient_id = ? ORDER BY occurred_at ASC",
                (patient_id,),
            ).fetchall()
            out: List[Dict[str, Any]] = []
            for row in rows:
                rec = dict(row)
                rec["payload"] = json.loads(rec.get("payload_json") or "{}")
                out.append(rec)
            return out

    def mark_survey_sent(self, patient_id: str, survey_day: int, sent_at: Optional[str] = None) -> bool:
        ts = sent_at or _utcnow_iso()
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO survey_sends (patient_id, survey_day, sent_at) VALUES (?, ?, ?)",
                    (patient_id, survey_day, ts),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def get_survey_sends(self, patient_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM survey_sends WHERE patient_id = ? ORDER BY survey_day ASC",
                (patient_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def save_survey_response(
        self,
        *,
        patient_id: str,
        survey_day: int,
        answers: List[Dict[str, Any]],
        score: Optional[float],
        tier: Optional[str],
        submitted_at: Optional[str] = None,
    ) -> None:
        ts = submitted_at or _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO survey_responses (patient_id, survey_day, answers_json, score, tier, submitted_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(patient_id, survey_day) DO UPDATE SET
                    answers_json = excluded.answers_json,
                    score = excluded.score,
                    tier = excluded.tier,
                    submitted_at = excluded.submitted_at
                """,
                (patient_id, survey_day, json.dumps(answers), score, tier, ts),
            )

    def get_survey_response(self, patient_id: str, survey_day: int) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM survey_responses WHERE patient_id = ? AND survey_day = ?",
                (patient_id, survey_day),
            ).fetchone()
            if not row:
                return None
            rec = dict(row)
            rec["answers"] = json.loads(rec.get("answers_json") or "[]")
            return rec

    def get_survey_responses(self, patient_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM survey_responses WHERE patient_id = ? ORDER BY survey_day ASC",
                (patient_id,),
            ).fetchall()
            out = []
            for row in rows:
                rec = dict(row)
                rec["answers"] = json.loads(rec.get("answers_json") or "[]")
                out.append(rec)
            return out

    def get_composite_score(self, patient_id: str) -> Optional[float]:
        rows = self.get_survey_responses(patient_id)
        points = [float(r["score"]) for r in rows if r.get("score") is not None and int(r.get("survey_day", 0)) in (7, 14, 30)]
        if not points:
            return None
        return round(sum(points) / len(points), 2)

    def mark_daily_reminder(self, patient_id: str, reminder_date: str, sent_at: Optional[str] = None) -> bool:
        ts = sent_at or _utcnow_iso()
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO daily_reminders (patient_id, reminder_date, sent_at) VALUES (?, ?, ?)",
                    (patient_id, reminder_date, ts),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def create_escalation(
        self,
        *,
        patient_id: str,
        tier: int,
        trigger_type: str,
        message: str,
        conversation_snapshot: List[Dict[str, Any]],
        created_at: Optional[str] = None,
    ) -> int:
        ts = created_at or _utcnow_iso()
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO escalations (patient_id, tier, trigger_type, message, resolved, created_at, conversation_snapshot)
                VALUES (?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    patient_id,
                    tier,
                    trigger_type,
                    message,
                    ts,
                    json.dumps(conversation_snapshot),
                ),
            )
            return int(cur.lastrowid)

    def set_escalation_consent(self, escalation_id: int, consent: str, consent_at: Optional[str] = None) -> None:
        ts = consent_at or _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                "UPDATE escalations SET consent = ?, consent_at = ? WHERE id = ?",
                (consent, ts, escalation_id),
            )

    def set_escalation_resolved(self, escalation_id: int, resolved: bool) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE escalations SET resolved = ? WHERE id = ?",
                (1 if resolved else 0, escalation_id),
            )

    def list_escalations(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM escalations ORDER BY created_at DESC").fetchall()
            out = []
            for row in rows:
                rec = dict(row)
                rec["conversation_snapshot"] = json.loads(rec.get("conversation_snapshot") or "[]")
                rec["resolved"] = bool(rec.get("resolved"))
                out.append(rec)
            return out

    def get_escalation(self, escalation_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM escalations WHERE id = ?", (escalation_id,)).fetchone()
            if not row:
                return None
            rec = dict(row)
            rec["conversation_snapshot"] = json.loads(rec.get("conversation_snapshot") or "[]")
            rec["resolved"] = bool(rec.get("resolved"))
            return rec

    def save_preop_intake_submission(
        self,
        *,
        patient_id: str,
        specialty: str,
        form_template_name: str,
        form_data: Dict[str, Any],
        submitted_at: Optional[str] = None,
    ) -> int:
        ts = submitted_at or _utcnow_iso()
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO preop_intake_submissions (patient_id, specialty, form_template_name, form_data_json, submitted_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (patient_id, specialty, form_template_name, json.dumps(form_data or {}), ts),
            )
            return int(cur.lastrowid)

    def get_latest_preop_intake_submission(self, patient_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM preop_intake_submissions
                WHERE patient_id = ?
                ORDER BY submitted_at DESC, id DESC
                LIMIT 1
                """,
                (patient_id,),
            ).fetchone()
            if not row:
                return None
            rec = dict(row)
            rec["form_data"] = json.loads(rec.get("form_data_json") or "{}")
            return rec

