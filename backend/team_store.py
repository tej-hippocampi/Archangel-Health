import hashlib
import json
import os
import re
import secrets
import sqlite3
import string
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from passlib.context import CryptContext

_pwd = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


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
                    survey_type TEXT NOT NULL DEFAULT 'postop',
                    survey_day INTEGER NOT NULL,
                    answers_json TEXT NOT NULL,
                    score REAL,
                    tier TEXT,
                    submitted_at TEXT NOT NULL,
                    UNIQUE(patient_id, survey_type, survey_day)
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

                CREATE TABLE IF NOT EXISTS health_systems (
                    id TEXT PRIMARY KEY,
                    slug TEXT NOT NULL UNIQUE,
                    name TEXT,
                    surgery_department TEXT,
                    phone TEXT,
                    health_system_code TEXT,
                    status TEXT NOT NULL DEFAULT 'pending_onboarding',
                    onboarding_token_hash TEXT,
                    onboarding_token_expires_at TEXT,
                    onboarding_completed_at TEXT,
                    director_email TEXT,
                    director_first_name TEXT,
                    director_last_name TEXT,
                    onboarding_step INTEGER NOT NULL DEFAULT 0,
                    last_generated_invite_url TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS otp_challenges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    health_system_id TEXT NOT NULL,
                    email TEXT NOT NULL,
                    code_hash TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS team_members (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    health_system_id TEXT NOT NULL,
                    email TEXT NOT NULL,
                    name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(health_system_id, email)
                );

                CREATE TABLE IF NOT EXISTS audit_sign_ins (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    health_system_id TEXT NOT NULL,
                    user_email TEXT NOT NULL,
                    display_name TEXT,
                    role TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS preop_intake_sessions (
                    patient_id TEXT PRIMARY KEY,
                    session_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS intake_forms (
                    id TEXT PRIMARY KEY,
                    patient_id TEXT NOT NULL,
                    surgery_id TEXT,
                    status TEXT NOT NULL DEFAULT 'NOT_STARTED',
                    interview_transcript_id TEXT,
                    form_data_json TEXT NOT NULL,
                    red_flags_json TEXT NOT NULL,
                    conflicts_json TEXT NOT NULL,
                    completed_at TEXT,
                    submitted_at TEXT,
                    last_edited_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS interview_transcripts (
                    id TEXT PRIMARY KEY,
                    intake_form_id TEXT NOT NULL,
                    full_transcript_json TEXT NOT NULL,
                    audio_blob_url TEXT,
                    duration INTEGER,
                    parsed_data_json TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS intake_form_edits (
                    id TEXT PRIMARY KEY,
                    intake_form_id TEXT NOT NULL,
                    edited_by TEXT NOT NULL,
                    section_name TEXT NOT NULL,
                    field_key TEXT NOT NULL,
                    previous_value_json TEXT,
                    new_value_json TEXT,
                    edited_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS intake_form_notifications (
                    id TEXT PRIMARY KEY,
                    doctor_id TEXT NOT NULL,
                    intake_form_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    is_read INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_team_members_hs ON team_members(health_system_id);
                CREATE INDEX IF NOT EXISTS idx_audit_hs ON audit_sign_ins(health_system_id);
                CREATE INDEX IF NOT EXISTS idx_otp_hs ON otp_challenges(health_system_id);
                CREATE INDEX IF NOT EXISTS idx_intake_forms_patient ON intake_forms(patient_id);
                CREATE INDEX IF NOT EXISTS idx_intake_forms_status ON intake_forms(status);
                CREATE INDEX IF NOT EXISTS idx_transcripts_intake_form ON interview_transcripts(intake_form_id);
                CREATE INDEX IF NOT EXISTS idx_intake_edits_form ON intake_form_edits(intake_form_id);
                CREATE INDEX IF NOT EXISTS idx_intake_notifications_doctor ON intake_form_notifications(doctor_id);

                CREATE TABLE IF NOT EXISTS _schema_migrations (
                    name TEXT PRIMARY KEY
                );
                """
            )
        self._migrate_schema()

    def _migrate_schema(self) -> None:
        """Add nullable columns to legacy tables (SQLite).

        episodes.clinic_code is retained (no physical RENAME); responses expose health_system_code as an alias.
        """
        with self._conn() as conn:
            self._add_column_if_missing(conn, "episodes", "health_system_id", "TEXT")
            self._add_column_if_missing(conn, "escalations", "health_system_id", "TEXT")
            self._add_column_if_missing(conn, "intake_forms", "interview_state_json", "TEXT")
            self._migrate_survey_responses_v2(conn)

    @staticmethod
    def _add_column_if_missing(conn: sqlite3.Connection, table: str, col: str, decl: str) -> None:
        cur = conn.execute(f"PRAGMA table_info({table})")
        names = {row[1] for row in cur.fetchall()}
        if col not in names:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    @staticmethod
    def _migrate_survey_responses_v2(conn: sqlite3.Connection) -> None:
        """Rebuild survey_responses with survey_type and UNIQUE(patient_id, survey_type, survey_day)."""
        done = conn.execute(
            "SELECT 1 FROM _schema_migrations WHERE name = ?",
            ("survey_responses_v2",),
        ).fetchone()
        if done:
            return
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='survey_responses'"
        ).fetchone()
        if not row or not row[0]:
            conn.execute(
                "INSERT OR IGNORE INTO _schema_migrations (name) VALUES (?)",
                ("survey_responses_v2",),
            )
            return
        rows = conn.execute("SELECT * FROM survey_responses").fetchall()
        colnames = [d[0] for d in conn.execute("PRAGMA table_info(survey_responses)").fetchall()]
        has_survey_type = "survey_type" in colnames
        conn.execute("DROP TABLE survey_responses")
        conn.execute(
            """
            CREATE TABLE survey_responses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_id TEXT NOT NULL,
                survey_type TEXT NOT NULL DEFAULT 'postop',
                survey_day INTEGER NOT NULL,
                answers_json TEXT NOT NULL,
                score REAL,
                tier TEXT,
                submitted_at TEXT NOT NULL,
                UNIQUE(patient_id, survey_type, survey_day)
            )
            """
        )
        for r in rows:
            d = dict(r)
            pid = d["patient_id"]
            day = int(d["survey_day"])
            st = str(d.get("survey_type") or "postop") if has_survey_type else "postop"
            conn.execute(
                """
                INSERT INTO survey_responses (patient_id, survey_type, survey_day, answers_json, score, tier, submitted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pid,
                    st,
                    day,
                    d.get("answers_json") or "[]",
                    d.get("score"),
                    d.get("tier"),
                    d.get("submitted_at") or _utcnow_iso(),
                ),
            )
        conn.execute(
            "INSERT OR IGNORE INTO _schema_migrations (name) VALUES (?)",
            ("survey_responses_v2",),
        )

    @staticmethod
    def _hash_onboarding_token(raw_token: str) -> str:
        pepper = os.getenv("AUTH_SECRET", "change-me-in-production-elysium")
        return hashlib.sha256(f"{pepper}:{raw_token}".encode()).hexdigest()

    @staticmethod
    def _generate_health_system_code() -> str:
        alphabet = string.ascii_uppercase + string.digits
        return "".join(secrets.choice(alphabet) for _ in range(8))

    @staticmethod
    def _slugify(s: str) -> str:
        s = (s or "").lower().strip()
        s = re.sub(r"[^a-z0-9]+", "-", s)
        s = re.sub(r"-+", "-", s).strip("-")
        return s[:48] or "health-system"

    def ensure_demo_health_system(
        self,
        *,
        hs_id: str,
        slug: str,
        name: str,
        health_system_code: str,
    ) -> None:
        now = _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO health_systems (
                    id, slug, name, surgery_department, phone, health_system_code, status,
                    onboarding_token_hash, onboarding_token_expires_at, onboarding_completed_at,
                    director_email, director_first_name, director_last_name, onboarding_step,
                    last_generated_invite_url, created_at
                )
                VALUES (?, ?, ?, '', '', ?, 'active', NULL, NULL, ?, NULL, NULL, NULL, 99, NULL, ?)
                ON CONFLICT(id) DO UPDATE SET
                    slug = excluded.slug,
                    name = excluded.name,
                    health_system_code = COALESCE(health_systems.health_system_code, excluded.health_system_code),
                    status = 'active',
                    onboarding_completed_at = COALESCE(health_systems.onboarding_completed_at, excluded.onboarding_completed_at)
                """,
                (hs_id, slug, name, health_system_code, now, now),
            )

    def create_health_system_invite(self, *, invite_base_url: str) -> Dict[str, Any]:
        """Admin: new pending health system + one-time onboarding URL (token shown once)."""
        raw_token = secrets.token_urlsafe(32)
        token_hash = self._hash_onboarding_token(raw_token)
        expires = (datetime.utcnow() + timedelta(days=30)).replace(microsecond=0).isoformat()
        now = _utcnow_iso()
        invite_url = f"{invite_base_url.rstrip('/')}/onboard/{raw_token}"
        hs_id = ""
        slug = ""
        for _ in range(30):
            hs_id = str(uuid.uuid4())
            slug = f"pending-{secrets.token_hex(4)}"
            try:
                with self._conn() as conn:
                    conn.execute(
                        """
                        INSERT INTO health_systems (
                            id, slug, name, surgery_department, phone, health_system_code, status,
                            onboarding_token_hash, onboarding_token_expires_at, onboarding_completed_at,
                            director_email, director_first_name, director_last_name, onboarding_step,
                            last_generated_invite_url, created_at
                        )
                        VALUES (?, ?, NULL, NULL, NULL, NULL, 'pending_onboarding', ?, ?, NULL,
                                NULL, NULL, NULL, 0, ?, ?)
                        """,
                        (hs_id, slug, token_hash, expires, invite_url, now),
                    )
                break
            except sqlite3.IntegrityError:
                continue
        else:
            raise RuntimeError("Could not allocate unique slug for health system invite")
        url = f"{invite_base_url.rstrip('/')}/onboard/{raw_token}"
        return {
            "health_system_id": hs_id,
            "slug": slug,
            "onboarding_url": url,
            "expires_at": expires,
        }

    def list_health_systems_admin(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM health_systems ORDER BY datetime(created_at) DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_health_system_by_id(self, hs_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM health_systems WHERE id = ?", (hs_id,)).fetchone()
            return dict(row) if row else None

    def get_health_system_by_code(self, code: str) -> Optional[Dict[str, Any]]:
        c = (code or "").strip()
        if not c:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM health_systems WHERE upper(trim(health_system_code)) = upper(trim(?)) LIMIT 1",
                (c,),
            ).fetchone()
            return dict(row) if row else None

    def get_health_system_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        s = (slug or "").strip()
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM health_systems WHERE slug = ?", (s,)).fetchone()
            if not row:
                row = conn.execute("SELECT * FROM health_systems WHERE lower(slug) = lower(?)", (s,)).fetchone()
            return dict(row) if row else None

    def get_health_system_by_onboarding_token(self, raw_token: str) -> Optional[Dict[str, Any]]:
        h = self._hash_onboarding_token(raw_token.strip())
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM health_systems WHERE onboarding_token_hash = ?",
                (h,),
            ).fetchone()
            return dict(row) if row else None

    def onboarding_token_valid(self, row: Dict[str, Any]) -> bool:
        if not row.get("onboarding_token_hash"):
            return False
        exp = row.get("onboarding_token_expires_at") or ""
        try:
            if datetime.fromisoformat(exp) < datetime.utcnow():
                return False
        except Exception:
            return False
        return True

    def update_health_system_director_identity(
        self,
        hs_id: str,
        *,
        first_name: str,
        last_name: str,
        email: str,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE health_systems SET
                    director_first_name = ?, director_last_name = ?, director_email = ?,
                    onboarding_step = CASE WHEN onboarding_step < 1 THEN 1 ELSE onboarding_step END
                WHERE id = ?
                """,
                (first_name.strip(), last_name.strip(), email.lower().strip(), hs_id),
            )

    def create_otp_challenge(self, hs_id: str, email: str, raw_code: str) -> int:
        expires = (datetime.utcnow() + timedelta(minutes=15)).replace(microsecond=0).isoformat()
        code_hash = _pwd.hash(raw_code)
        now = _utcnow_iso()
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO otp_challenges (health_system_id, email, code_hash, expires_at, consumed_at, created_at)
                VALUES (?, ?, ?, ?, NULL, ?)
                """,
                (hs_id, email.lower().strip(), code_hash, expires, now),
            )
            return int(cur.lastrowid)

    def verify_otp_challenge(self, hs_id: str, email: str, raw_code: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM otp_challenges
                WHERE health_system_id = ? AND email = ? AND consumed_at IS NULL
                ORDER BY id DESC LIMIT 1
                """,
                (hs_id, email.lower().strip()),
            ).fetchone()
            if not row:
                return False
            rec = dict(row)
            try:
                if datetime.fromisoformat(rec["expires_at"]) < datetime.utcnow():
                    return False
            except Exception:
                return False
            if not _pwd.verify(raw_code.strip(), rec["code_hash"]):
                return False
            conn.execute(
                "UPDATE otp_challenges SET consumed_at = ? WHERE id = ?",
                (_utcnow_iso(), rec["id"]),
            )
            conn.execute(
                """
                UPDATE health_systems SET
                    onboarding_step = CASE WHEN onboarding_step < 2 THEN 2 ELSE onboarding_step END
                WHERE id = ?
                """,
                (hs_id,),
            )
            return True

    def update_health_system_org_details(
        self,
        hs_id: str,
        *,
        name: str,
        surgery_department: str,
        phone: str,
    ) -> None:
        code = self._generate_health_system_code()
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE health_systems SET
                    name = ?, surgery_department = ?, phone = ?,
                    health_system_code = COALESCE(health_system_code, ?),
                    onboarding_step = CASE WHEN onboarding_step < 3 THEN 3 ELSE onboarding_step END
                WHERE id = ?
                """,
                (name.strip(), surgery_department.strip(), phone.strip(), code, hs_id),
            )

    def hash_team_password(self, raw: str) -> str:
        return _pwd.hash(raw)

    def verify_team_password(self, raw: str, hashed: str) -> bool:
        return _pwd.verify(raw, hashed)

    def insert_team_member(
        self,
        hs_id: str,
        *,
        email: str,
        name: str,
        role: str,
        password_hash: str,
    ) -> int:
        now = _utcnow_iso()
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO team_members (health_system_id, email, name, role, password_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(health_system_id, email) DO UPDATE SET
                    name = excluded.name,
                    role = excluded.role,
                    password_hash = excluded.password_hash
                """,
                (hs_id, email.lower().strip(), name.strip(), role, password_hash, now),
            )
            return int(cur.lastrowid)

    def list_team_members(self, hs_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, health_system_id, email, name, role, created_at FROM team_members WHERE health_system_id = ? ORDER BY id ASC",
                (hs_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_team_member(self, hs_id: str, email: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM team_members WHERE health_system_id = ? AND email = ?",
                (hs_id, email.lower().strip()),
            ).fetchone()
            return dict(row) if row else None

    def find_team_member_by_email_any_hs(self, email: str) -> Optional[Dict[str, Any]]:
        """First team_members row for this email (any health system), for landing-auth guardrails."""
        em = (email or "").lower().strip()
        if not em:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM team_members WHERE email = ? ORDER BY id ASC LIMIT 1",
                (em,),
            ).fetchone()
            return dict(row) if row else None

    def authenticate_team_member(self, slug: str, email: str, password: str) -> Optional[Dict[str, Any]]:
        hs = self.get_health_system_by_slug(slug)
        if not hs or hs.get("status") != "active":
            return None
        m = self.get_team_member(hs["id"], email)
        if not m:
            return None
        if not self.verify_team_password(password, m["password_hash"]):
            return None
        return {"member": m, "health_system": hs}

    def complete_onboarding_finalize(
        self,
        hs_id: str,
        *,
        director_email: str,
        director_first_name: str,
        director_last_name: str,
        director_password_hash: str,
    ) -> Dict[str, Any]:
        """Single-use token consumed; director account created/updated; HS active."""
        now = _utcnow_iso()
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM health_systems WHERE id = ?", (hs_id,)).fetchone()
            if not row:
                raise ValueError("health_system not found")
            name = f"{director_first_name} {director_last_name}".strip()
            conn.execute(
                """
                UPDATE health_systems SET
                    status = 'active',
                    onboarding_completed_at = ?,
                    onboarding_step = 99,
                    director_email = ?
                WHERE id = ?
                """,
                (now, director_email.lower().strip(), hs_id),
            )
            conn.execute(
                """
                INSERT INTO team_members (health_system_id, email, name, role, password_hash, created_at)
                VALUES (?, ?, ?, 'director', ?, ?)
                ON CONFLICT(health_system_id, email) DO UPDATE SET
                    name = excluded.name,
                    role = 'director',
                    password_hash = excluded.password_hash
                """,
                (hs_id, director_email.lower().strip(), name, director_password_hash, now),
            )
        return self.get_health_system_by_id(hs_id) or {}

    def maybe_update_slug_from_name(self, hs_id: str, desired_base: str) -> str:
        base = self._slugify(desired_base)
        with self._conn() as conn:
            for suffix in ["", f"-{secrets.token_hex(2)}", f"-{secrets.token_hex(3)}"]:
                cand = (base + suffix).strip("-")
                try:
                    conn.execute(
                        "UPDATE health_systems SET slug = ? WHERE id = ?",
                        (cand, hs_id),
                    )
                    return cand
                except sqlite3.IntegrityError:
                    continue
        return base

    def append_audit_sign_in(
        self,
        *,
        health_system_id: str,
        user_email: str,
        display_name: str,
        role: str,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO audit_sign_ins (health_system_id, user_email, display_name, role, occurred_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (health_system_id, user_email.lower().strip(), display_name, role, _utcnow_iso()),
            )

    def list_audit_sign_ins(self, health_system_id: str, *, limit: int = 500) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM audit_sign_ins
                WHERE health_system_id = ?
                ORDER BY datetime(occurred_at) DESC
                LIMIT ?
                """,
                (health_system_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def save_preop_intake_session(self, patient_id: str, session: Dict[str, Any]) -> None:
        now = _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO preop_intake_sessions (patient_id, session_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(patient_id) DO UPDATE SET
                    session_json = excluded.session_json,
                    updated_at = excluded.updated_at
                """,
                (patient_id, json.dumps(session), now),
            )

    def get_preop_intake_session(self, patient_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT session_json FROM preop_intake_sessions WHERE patient_id = ?",
                (patient_id,),
            ).fetchone()
            if not row:
                return None
            return json.loads(row["session_json"] or "{}")

    def delete_preop_intake_session(self, patient_id: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM preop_intake_sessions WHERE patient_id = ?", (patient_id,))

    def ensure_episode(
        self,
        *,
        patient_id: str,
        open_date: Optional[str] = None,
        procedure_type: str = "",
        clinic_code: str = "",
        resource_code: str = "",
        health_system_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        today = date.today()
        episode_open = date.fromisoformat(open_date) if open_date else today
        episode_close = episode_open + timedelta(days=29)
        created_at = _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO episodes (patient_id, open_date, close_date, status, procedure_type, clinic_code, resource_code, health_system_id, created_at)
                VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?)
                ON CONFLICT(patient_id) DO UPDATE SET
                    procedure_type = excluded.procedure_type,
                    clinic_code = COALESCE(excluded.clinic_code, episodes.clinic_code),
                    resource_code = COALESCE(excluded.resource_code, episodes.resource_code),
                    health_system_id = COALESCE(excluded.health_system_id, episodes.health_system_id)
                """,
                (
                    patient_id,
                    episode_open.isoformat(),
                    episode_close.isoformat(),
                    procedure_type,
                    clinic_code or None,
                    resource_code or None,
                    health_system_id,
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

    def has_survey_send(self, patient_id: str, survey_day: int) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM survey_sends WHERE patient_id = ? AND survey_day = ? LIMIT 1",
                (patient_id, survey_day),
            ).fetchone()
            return row is not None

    def save_survey_response(
        self,
        *,
        patient_id: str,
        survey_day: int,
        answers: List[Dict[str, Any]],
        score: Optional[float],
        tier: Optional[str],
        submitted_at: Optional[str] = None,
        survey_type: str = "postop",
    ) -> None:
        ts = submitted_at or _utcnow_iso()
        st = survey_type or "postop"
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO survey_responses (patient_id, survey_type, survey_day, answers_json, score, tier, submitted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(patient_id, survey_type, survey_day) DO UPDATE SET
                    answers_json = excluded.answers_json,
                    score = excluded.score,
                    tier = excluded.tier,
                    submitted_at = excluded.submitted_at
                """,
                (patient_id, st, survey_day, json.dumps(answers), score, tier, ts),
            )

    def get_survey_response(
        self, patient_id: str, survey_day: int, survey_type: str = "postop"
    ) -> Optional[Dict[str, Any]]:
        st = survey_type or "postop"
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM survey_responses WHERE patient_id = ? AND survey_day = ? AND survey_type = ?",
                (patient_id, survey_day, st),
            ).fetchone()
            if not row:
                return None
            rec = dict(row)
            rec["answers"] = json.loads(rec.get("answers_json") or "[]")
            return rec

    def get_survey_responses(self, patient_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM survey_responses WHERE patient_id = ? ORDER BY survey_type ASC, survey_day ASC",
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
        points = [
            float(r["score"])
            for r in rows
            if r.get("score") is not None
            and str(r.get("survey_type") or "postop") == "postop"
            and int(r.get("survey_day", 0)) in (7, 14, 30)
        ]
        if not points:
            return None
        return round(sum(points) / len(points), 2)

    def has_open_escalation(self, patient_id: str, trigger_type: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM escalations
                WHERE patient_id = ? AND trigger_type = ? AND resolved = 0
                LIMIT 1
                """,
                (patient_id, trigger_type),
            ).fetchone()
            return row is not None

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
        health_system_id: Optional[str] = None,
    ) -> int:
        ts = created_at or _utcnow_iso()
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO escalations (patient_id, tier, trigger_type, message, resolved, created_at, conversation_snapshot, health_system_id)
                VALUES (?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    patient_id,
                    tier,
                    trigger_type,
                    message,
                    ts,
                    json.dumps(conversation_snapshot),
                    health_system_id,
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

    # ─── Intake Bot v2 persistence ──────────────────────────────────────────────
    def create_intake_form(
        self,
        *,
        intake_form_id: str,
        patient_id: str,
        surgery_id: Optional[str],
        status: str,
        form_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        now = _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO intake_forms (
                    id, patient_id, surgery_id, status, interview_transcript_id,
                    form_data_json, red_flags_json, conflicts_json, completed_at,
                    submitted_at, last_edited_at, created_at, updated_at, interview_state_json
                )
                VALUES (?, ?, ?, ?, NULL, ?, '[]', '[]', NULL, NULL, NULL, ?, ?, ?)
                """,
                (
                    intake_form_id,
                    patient_id,
                    surgery_id,
                    status,
                    json.dumps(form_data or {}),
                    now,
                    now,
                    json.dumps(
                        {
                            "activeSection": 1,
                            "completedSections": [],
                            "messagesBySection": {},
                        }
                    ),
                ),
            )
        return self.get_intake_form(intake_form_id) or {}

    def update_intake_form_status(
        self,
        intake_form_id: str,
        *,
        status: str,
        completed_at: Optional[str] = None,
        submitted_at: Optional[str] = None,
        interview_transcript_id: Optional[str] = None,
    ) -> None:
        now = _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE intake_forms SET
                    status = ?,
                    completed_at = COALESCE(?, completed_at),
                    submitted_at = COALESCE(?, submitted_at),
                    interview_transcript_id = COALESCE(?, interview_transcript_id),
                    updated_at = ?
                WHERE id = ?
                """,
                (status, completed_at, submitted_at, interview_transcript_id, now, intake_form_id),
            )

    def update_intake_form_payload(
        self,
        intake_form_id: str,
        *,
        form_data: Dict[str, Any],
        red_flags: List[Dict[str, Any]],
        conflicts: List[Dict[str, Any]],
        status: Optional[str] = None,
        completed_at: Optional[str] = None,
        submitted_at: Optional[str] = None,
        interview_transcript_id: Optional[str] = None,
        interview_state: Optional[Dict[str, Any]] = None,
    ) -> None:
        now = _utcnow_iso()
        with self._conn() as conn:
            if interview_state is not None:
                conn.execute(
                    """
                    UPDATE intake_forms SET
                        form_data_json = ?,
                        red_flags_json = ?,
                        conflicts_json = ?,
                        status = COALESCE(?, status),
                        completed_at = COALESCE(?, completed_at),
                        submitted_at = COALESCE(?, submitted_at),
                        interview_transcript_id = COALESCE(?, interview_transcript_id),
                        interview_state_json = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        json.dumps(form_data or {}),
                        json.dumps(red_flags or []),
                        json.dumps(conflicts or []),
                        status,
                        completed_at,
                        submitted_at,
                        interview_transcript_id,
                        json.dumps(interview_state or {}),
                        now,
                        intake_form_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE intake_forms SET
                        form_data_json = ?,
                        red_flags_json = ?,
                        conflicts_json = ?,
                        status = COALESCE(?, status),
                        completed_at = COALESCE(?, completed_at),
                        submitted_at = COALESCE(?, submitted_at),
                        interview_transcript_id = COALESCE(?, interview_transcript_id),
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        json.dumps(form_data or {}),
                        json.dumps(red_flags or []),
                        json.dumps(conflicts or []),
                        status,
                        completed_at,
                        submitted_at,
                        interview_transcript_id,
                        now,
                        intake_form_id,
                    ),
                )

    def list_intake_forms_for_patient(self, patient_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM intake_forms
                WHERE patient_id = ?
                ORDER BY datetime(updated_at) DESC
                """,
                (patient_id,),
            ).fetchall()
            return [self._hydrate_intake_form_row(dict(r)) for r in rows]

    def get_latest_intake_form_for_patient(self, patient_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM intake_forms
                WHERE patient_id = ?
                ORDER BY datetime(updated_at) DESC, created_at DESC
                LIMIT 1
                """,
                (patient_id,),
            ).fetchone()
            if not row:
                return None
            return self._hydrate_intake_form_row(dict(row))

    def get_intake_form(self, intake_form_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM intake_forms WHERE id = ?", (intake_form_id,)).fetchone()
            if not row:
                return None
            return self._hydrate_intake_form_row(dict(row))

    def _hydrate_intake_form_row(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        rec["form_data"] = json.loads(rec.get("form_data_json") or "{}")
        rec["red_flags"] = json.loads(rec.get("red_flags_json") or "[]")
        rec["conflicts"] = json.loads(rec.get("conflicts_json") or "[]")
        raw_state = rec.get("interview_state_json")
        if raw_state:
            try:
                rec["interview_state"] = json.loads(raw_state) if isinstance(raw_state, str) else (raw_state or {})
            except json.JSONDecodeError:
                rec["interview_state"] = {}
        else:
            rec["interview_state"] = {
                "activeSection": 1,
                "completedSections": [],
                "messagesBySection": {},
            }
        return rec

    def save_interview_transcript(
        self,
        *,
        transcript_id: str,
        intake_form_id: str,
        full_transcript: List[Dict[str, Any]],
        audio_blob_url: Optional[str],
        duration: Optional[int],
        parsed_data: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        now = _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO interview_transcripts (
                    id, intake_form_id, full_transcript_json, audio_blob_url, duration, parsed_data_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    transcript_id,
                    intake_form_id,
                    json.dumps(full_transcript or []),
                    audio_blob_url,
                    duration,
                    json.dumps(parsed_data or {}),
                    now,
                ),
            )
        return self.get_interview_transcript(transcript_id) or {}

    def get_interview_transcript(self, transcript_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM interview_transcripts WHERE id = ?", (transcript_id,)).fetchone()
            if not row:
                return None
            rec = dict(row)
            rec["full_transcript"] = json.loads(rec.get("full_transcript_json") or "[]")
            rec["parsed_data"] = json.loads(rec.get("parsed_data_json") or "{}")
            return rec

    def create_intake_form_edit(
        self,
        *,
        edit_id: str,
        intake_form_id: str,
        edited_by: str,
        section_name: str,
        field_key: str,
        previous_value: Any,
        new_value: Any,
    ) -> None:
        now = _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO intake_form_edits (
                    id, intake_form_id, edited_by, section_name, field_key, previous_value_json, new_value_json, edited_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    edit_id,
                    intake_form_id,
                    edited_by,
                    section_name,
                    field_key,
                    json.dumps(previous_value),
                    json.dumps(new_value),
                    now,
                ),
            )
            conn.execute(
                "UPDATE intake_forms SET last_edited_at = ?, updated_at = ? WHERE id = ?",
                (now, now, intake_form_id),
            )

    def list_intake_form_edits(self, intake_form_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM intake_form_edits
                WHERE intake_form_id = ?
                ORDER BY datetime(edited_at) ASC, id ASC
                """,
                (intake_form_id,),
            ).fetchall()
            out: List[Dict[str, Any]] = []
            for row in rows:
                rec = dict(row)
                rec["previous_value"] = json.loads(rec.get("previous_value_json") or "null")
                rec["new_value"] = json.loads(rec.get("new_value_json") or "null")
                out.append(rec)
            return out

    def create_intake_notification(
        self,
        *,
        notification_id: str,
        doctor_id: str,
        intake_form_id: str,
        notification_type: str,
        message: str,
    ) -> None:
        now = _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO intake_form_notifications (
                    id, doctor_id, intake_form_id, type, message, is_read, created_at
                )
                VALUES (?, ?, ?, ?, ?, 0, ?)
                """,
                (notification_id, doctor_id, intake_form_id, notification_type, message, now),
            )

    def list_intake_notifications(
        self,
        doctor_id: str,
        *,
        unread_only: bool = False,
        notif_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        where = ["doctor_id = ?"]
        args: List[Any] = [doctor_id]
        if unread_only:
            where.append("is_read = 0")
        if notif_type:
            where.append("type = ?")
            args.append(notif_type)
        q = (
            "SELECT * FROM intake_form_notifications WHERE "
            + " AND ".join(where)
            + " ORDER BY datetime(created_at) DESC"
        )
        with self._conn() as conn:
            rows = conn.execute(q, tuple(args)).fetchall()
            out = []
            for row in rows:
                rec = dict(row)
                rec["read"] = bool(rec.get("is_read"))
                out.append(rec)
            return out

    def mark_intake_notification_read(self, doctor_id: str, notification_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE intake_form_notifications
                SET is_read = 1
                WHERE id = ? AND doctor_id = ?
                """,
                (notification_id, doctor_id),
            )
            return cur.rowcount > 0

