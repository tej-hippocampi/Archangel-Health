"""
TeamStore — SQLite persistence for the four triage stages.

Architecture decision (Triage Suite Pass 2 — Option B / event-stream;
Pass 3 — episode_snapshots table is now source of truth on cold start):

The `episodes` table is intentionally minimal. All per-stage state is
read from event/snapshot tables that mirror the four triage stages:

    initial pre-op tier    → `episode_snapshots.initial_tier_was_hard_escalator`
                              + in-memory `_patient_store` blob (hot cache)
                              + `event_logs INITIAL_TIER_ASSIGNED`
    pre-op re-tier         → `pam_assessments` (intake PAM rows)
                              + `survey_responses` (T-96 / T-48 / T-24)
                              + `event_logs PREOP_VIDEO_PLAYED / BATTLECARD_VIEWED`
                              + `preop_retier_events` (snapshot per recompute)
                              + `episode_snapshots.post_intake_tier` (one-shot)
    intra-op reassessment  → `intraop_reassessments`
                              + `episode_snapshots.post_intraop_tier`
    post-op scoring        → `daily_checkin_responses`, `dayx_surveys`,
                              `med_adherence_*`, `postop_video_events`,
                              `patient_self_flags`, `postop_retier_events`,
                              + `event_logs care_companion_semantic_escalation`
                              + `escalations chat:semantic*`

Three denormalized fields live on the in-memory `_patient_store` blob
(hot cache) AND in the `episode_snapshots` row (cold-start source of
truth, Pass 3). Read-through pattern: writers update both; readers
prefer the blob, fall back to the table when the blob is empty:

  - `initial_tier_was_hard_escalator: bool` — set by
    `routers/initial_tier.py` and consumed by
    `triage.preop_retier.algo` for the sticky-hard guard.
  - `post_intake_tier: str | None` — set ONCE by the intake-finalize
    handler the first time the intake triggers a re-tier. Distinct
    from `initial_tier` (immutable) and `current_tier` (rolling).
  - `post_intraop_tier: str | None` — set by `triage.intraop.apply`
    and consumed by `triage.postop.apply` as the immutable floor.

`current_tier` is hot-only (rolling, mutated frequently) and is not
worth persisting; on cold start it rehydrates from the most recent
`{preop,intraop,postop}_retier_events` row's `tier_after`.
"""

import hashlib
import json
import os
import re
import secrets
import sqlite3
import string
import uuid
from datetime import date, datetime, timedelta, timezone
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
                    patient_id TEXT,
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

                -- Intra-Op Reassessment (PRD v1.0) ────────────────────────────
                CREATE TABLE IF NOT EXISTS intraop_forms (
                    id TEXT PRIMARY KEY,
                    patient_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,           -- NEW|IN_PROGRESS|READY_FOR_SURGEON_REVIEW|LOCKED|REOPENED
                    or_started_at TEXT,
                    or_ended_at TEXT,
                    or_duration_minutes INTEGER,
                    fields_json TEXT NOT NULL,
                    field_origins_json TEXT NOT NULL,
                    procedure_specific_json TEXT,
                    pdf_blob_url TEXT,
                    extraction_id TEXT,
                    surgeon_locked_by TEXT,
                    surgeon_locked_at TEXT,
                    conservative_default_applied_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS intraop_extractions (
                    id TEXT PRIMARY KEY,
                    patient_id TEXT NOT NULL,
                    pdf_blob_url TEXT NOT NULL,
                    raw_text TEXT,
                    fields_json TEXT,
                    field_confidences_json TEXT,
                    model_version TEXT,
                    prompt_version TEXT,
                    warnings_json TEXT,
                    status TEXT NOT NULL,           -- PENDING|RUNNING|COMPLETE|FAILED
                    error_message TEXT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS intraop_reassessments (
                    id TEXT PRIMARY KEY,
                    patient_id TEXT NOT NULL,
                    intraop_form_id TEXT NOT NULL,
                    form_snapshot_json TEXT NOT NULL,
                    pre_or_current_tier TEXT NOT NULL,
                    proposed_tier TEXT NOT NULL,
                    final_tier TEXT NOT NULL,
                    hard_upgrade_applied INTEGER NOT NULL DEFAULT 0,
                    upgrade_steps INTEGER NOT NULL DEFAULT 0,
                    reasons_json TEXT,
                    is_conservative_default INTEGER NOT NULL DEFAULT 0,
                    procedure_family TEXT,
                    model_version TEXT,
                    tuning_version INTEGER,
                    triggered_by TEXT,
                    triggered_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_intraop_forms_status      ON intraop_forms(status);
                CREATE INDEX IF NOT EXISTS idx_intraop_forms_or_ended    ON intraop_forms(or_ended_at);
                CREATE INDEX IF NOT EXISTS idx_intraop_extract_patient   ON intraop_extractions(patient_id);
                CREATE INDEX IF NOT EXISTS idx_intraop_reassess_patient  ON intraop_reassessments(patient_id, triggered_at);

                -- Post-Op Scoring (PRD v1.0) ──────────────────────────────────
                CREATE TABLE IF NOT EXISTS daily_checkin_sends (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    patient_id TEXT NOT NULL,
                    episode_day INTEGER NOT NULL,
                    sent_at TEXT NOT NULL,
                    channel TEXT NOT NULL DEFAULT 'PUSH',
                    UNIQUE(patient_id, episode_day)
                );

                CREATE TABLE IF NOT EXISTS daily_checkin_responses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    patient_id TEXT NOT NULL,
                    episode_day INTEGER NOT NULL,
                    submitted_at TEXT NOT NULL,
                    answers_json TEXT NOT NULL,
                    raw_total REAL NOT NULL,
                    tier TEXT NOT NULL,
                    red_flags_json TEXT NOT NULL,
                    new_red_flag INTEGER NOT NULL DEFAULT 0,
                    wound_concern INTEGER NOT NULL DEFAULT 0,
                    pain_nrs INTEGER,
                    pain_trajectory TEXT,
                    item_scores_json TEXT NOT NULL,
                    completed INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS daily_checkin_misses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    patient_id TEXT NOT NULL,
                    episode_day INTEGER NOT NULL,
                    marked_at TEXT NOT NULL,
                    UNIQUE(patient_id, episode_day)
                );

                CREATE TABLE IF NOT EXISTS dayx_surveys (
                    id TEXT PRIMARY KEY,
                    patient_id TEXT NOT NULL,
                    day INTEGER NOT NULL,
                    sent_at TEXT NOT NULL,
                    submitted_at TEXT,
                    status TEXT NOT NULL,
                    section_scores_json TEXT,
                    total_score REAL,
                    tier TEXT,
                    red_flags_json TEXT,
                    raw_answers_json TEXT,
                    procedure_family TEXT,
                    UNIQUE(patient_id, day)
                );

                CREATE TABLE IF NOT EXISTS med_adherence_pings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    patient_id TEXT NOT NULL,
                    episode_day INTEGER NOT NULL,
                    sent_at TEXT NOT NULL,
                    UNIQUE(patient_id, episode_day)
                );

                CREATE TABLE IF NOT EXISTS med_adherence_responses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    patient_id TEXT NOT NULL,
                    episode_day INTEGER NOT NULL,
                    responded_at TEXT,
                    response TEXT NOT NULL,
                    UNIQUE(patient_id, episode_day)
                );

                CREATE TABLE IF NOT EXISTS postop_video_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    patient_id TEXT NOT NULL,
                    video_kind TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    payload_json TEXT
                );

                CREATE TABLE IF NOT EXISTS patient_self_flags (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    patient_id TEXT NOT NULL,
                    flagged_at TEXT NOT NULL,
                    resolved_at TEXT,
                    resolved_by TEXT,
                    free_text TEXT,
                    source TEXT NOT NULL DEFAULT 'PATIENT_APP'
                );

                CREATE TABLE IF NOT EXISTS postop_retier_events (
                    id TEXT PRIMARY KEY,
                    patient_id TEXT NOT NULL,
                    triggered_by TEXT NOT NULL,
                    inputs_snapshot_json TEXT NOT NULL,
                    post_intraop_tier TEXT NOT NULL,
                    computed_delta INTEGER NOT NULL,
                    computed_tier TEXT NOT NULL,
                    tier_before TEXT NOT NULL,
                    tier_after TEXT NOT NULL,
                    changed INTEGER NOT NULL,
                    reasons_json TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    tuning_version INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_dc_resp_patient_day        ON daily_checkin_responses(patient_id, episode_day);
                CREATE INDEX IF NOT EXISTS idx_dc_resp_submitted          ON daily_checkin_responses(submitted_at);
                CREATE INDEX IF NOT EXISTS idx_dayx_surveys_patient       ON dayx_surveys(patient_id, day);
                CREATE INDEX IF NOT EXISTS idx_med_ping_patient_day       ON med_adherence_pings(patient_id, episode_day);
                CREATE INDEX IF NOT EXISTS idx_med_resp_patient_day       ON med_adherence_responses(patient_id, episode_day);
                CREATE INDEX IF NOT EXISTS idx_postop_video_patient_kind  ON postop_video_events(patient_id, video_kind, occurred_at);
                CREATE INDEX IF NOT EXISTS idx_self_flags_patient_open    ON patient_self_flags(patient_id, resolved_at);
                CREATE INDEX IF NOT EXISTS idx_postop_retier_patient      ON postop_retier_events(patient_id, created_at);

                CREATE TABLE IF NOT EXISTS pam_assessments (
                    id TEXT PRIMARY KEY,
                    episode_id TEXT NOT NULL,
                    patient_id TEXT NOT NULL,
                    responses_json TEXT NOT NULL,
                    raw_sum INTEGER NOT NULL,
                    items_scored INTEGER NOT NULL,
                    raw_average REAL NOT NULL,
                    activation_score REAL NOT NULL,
                    level TEXT NOT NULL CHECK(level IN ('LOW','MODERATE','HIGH')),
                    is_complete INTEGER NOT NULL DEFAULT 0,
                    model_version TEXT,
                    tuning_version INTEGER,
                    completed_at TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE INDEX IF NOT EXISTS idx_pam_assessments_episode_created
                    ON pam_assessments(episode_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_pam_assessments_patient_created
                    ON pam_assessments(patient_id, created_at);

                CREATE TABLE IF NOT EXISTS preop_retier_events (
                    id TEXT PRIMARY KEY,
                    episode_id TEXT NOT NULL,
                    triggered_by TEXT NOT NULL,
                    inputs_snapshot_json TEXT NOT NULL,
                    initial_tier TEXT NOT NULL,
                    initial_tier_was_hard INTEGER NOT NULL,
                    computed_delta INTEGER NOT NULL,
                    computed_tier TEXT NOT NULL,
                    tier_before TEXT NOT NULL,
                    tier_after TEXT NOT NULL,
                    changed INTEGER NOT NULL,
                    reasons_json TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    tuning_version INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE INDEX IF NOT EXISTS idx_preop_retier_events_episode_created
                    ON preop_retier_events(episode_id, created_at);

                CREATE TABLE IF NOT EXISTS episode_snapshots (
                    patient_id TEXT PRIMARY KEY,
                    initial_tier_was_hard_escalator INTEGER NOT NULL DEFAULT 0,
                    post_intake_tier TEXT,
                    post_intraop_tier TEXT,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                );

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

                CREATE TABLE IF NOT EXISTS grounding_check_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    patient_id TEXT NOT NULL,
                    track TEXT NOT NULL,
                    verdict TEXT NOT NULL,
                    coverage_pct REAL,
                    faithfulness_pct REAL,
                    critical_failures INTEGER DEFAULT 0,
                    summary TEXT,
                    script_excerpt TEXT,
                    report_json TEXT NOT NULL,
                    model TEXT,
                    prompt_version TEXT,
                    regenerated INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_gcr_created ON grounding_check_reports(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_gcr_verdict ON grounding_check_reports(verdict);

                CREATE TABLE IF NOT EXISTS teachback_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    patient_id TEXT NOT NULL,
                    track TEXT NOT NULL,
                    questions_json TEXT NOT NULL,
                    results_json TEXT NOT NULL,
                    completed INTEGER NOT NULL DEFAULT 0,
                    prompt_version TEXT,
                    model TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tbs_created ON teachback_sessions(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_tbs_patient_track ON teachback_sessions(patient_id, track, created_at DESC);

                CREATE TABLE IF NOT EXISTS inspector_recall_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    prompt_version TEXT NOT NULL,
                    table_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS teachback_recall_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    prompt_version TEXT NOT NULL,
                    table_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

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
            self._add_column_if_missing(
                conn, "team_members", "is_team_director", "INTEGER NOT NULL DEFAULT 0"
            )
            self._add_column_if_missing(conn, "intraop_forms", "draft_completed_by", "TEXT")
            self._add_column_if_missing(conn, "intraop_forms", "draft_completed_at", "TEXT")
            conn.execute(
                "UPDATE intraop_forms SET status = 'READY_FOR_SURGEON_REVIEW' "
                "WHERE status = 'READY_FOR_LOCK'"
            )
            self._migrate_team_member_roles_v4(conn)
            self._migrate_event_logs_patient_nullable(conn)

    @staticmethod
    def _migrate_team_member_roles_v4(conn: sqlite3.Connection) -> None:
        """Pass-4 role-token migration: doctor/nurse/director → surgeon/rn_coordinator/surgeon+is_team_director.

        Idempotent: keyed on `_schema_migrations.name = 'team_members_roles_v4'`.
        Only mutates `team_members` (and `audit_sign_ins.role` for historical
        consistency). Legacy JWTs and landing users are migrated lazily by the
        staff-context resolver and `_normalize_legacy_role`.
        """
        done = conn.execute(
            "SELECT 1 FROM _schema_migrations WHERE name = ?",
            ("team_members_roles_v4",),
        ).fetchone()
        if done:
            return
        conn.execute(
            "UPDATE team_members SET role='surgeon', is_team_director=1 WHERE role='director'",
        )
        conn.execute("UPDATE team_members SET role='surgeon' WHERE role='doctor'")
        conn.execute("UPDATE team_members SET role='rn_coordinator' WHERE role='nurse'")
        try:
            conn.execute("UPDATE audit_sign_ins SET role='surgeon' WHERE role IN ('doctor','director')")
            conn.execute("UPDATE audit_sign_ins SET role='rn_coordinator' WHERE role='nurse'")
        except sqlite3.OperationalError:
            pass
        conn.execute(
            "INSERT OR IGNORE INTO _schema_migrations (name) VALUES (?)",
            ("team_members_roles_v4",),
        )

    @staticmethod
    def _migrate_event_logs_patient_nullable(conn: sqlite3.Connection) -> None:
        """Allow event_logs.patient_id to be nullable for non-patient LLM telemetry."""
        done = conn.execute(
            "SELECT 1 FROM _schema_migrations WHERE name = ?",
            ("event_logs_patient_nullable_v1",),
        ).fetchone()
        if done:
            return

        cols = conn.execute("PRAGMA table_info(event_logs)").fetchall()
        patient_col = next((row for row in cols if row[1] == "patient_id"), None)
        patient_notnull = int(patient_col[3]) if patient_col else 0
        if patient_notnull == 1:
            conn.executescript(
                """
                CREATE TABLE event_logs_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    patient_id TEXT,
                    event_type TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    payload_json TEXT,
                    episode_open_date TEXT
                );
                INSERT INTO event_logs_new (id, patient_id, event_type, occurred_at, payload_json, episode_open_date)
                SELECT id, patient_id, event_type, occurred_at, payload_json, episode_open_date
                FROM event_logs;
                DROP TABLE event_logs;
                ALTER TABLE event_logs_new RENAME TO event_logs;
                CREATE INDEX IF NOT EXISTS idx_event_logs_patient ON event_logs(patient_id);
                CREATE INDEX IF NOT EXISTS idx_event_logs_occured ON event_logs(occurred_at);
                """
            )
        conn.execute(
            "INSERT OR IGNORE INTO _schema_migrations (name) VALUES (?)",
            ("event_logs_patient_nullable_v1",),
        )

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
        phone: str = "",
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
                VALUES (?, ?, ?, '', ?, ?, 'active', NULL, NULL, ?, NULL, NULL, NULL, 99, NULL, ?)
                ON CONFLICT(id) DO UPDATE SET
                    slug = excluded.slug,
                    name = excluded.name,
                    phone = CASE WHEN excluded.phone != '' THEN excluded.phone ELSE health_systems.phone END,
                    health_system_code = COALESCE(health_systems.health_system_code, excluded.health_system_code),
                    status = 'active',
                    onboarding_completed_at = COALESCE(health_systems.onboarding_completed_at, excluded.onboarding_completed_at)
                """,
                (hs_id, slug, name, phone, health_system_code, now, now),
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
        is_team_director: bool = False,
    ) -> int:
        now = _utcnow_iso()
        itd = 1 if is_team_director else 0
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO team_members (health_system_id, email, name, role, password_hash, is_team_director, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(health_system_id, email) DO UPDATE SET
                    name = excluded.name,
                    role = excluded.role,
                    password_hash = excluded.password_hash,
                    is_team_director = excluded.is_team_director
                """,
                (hs_id, email.lower().strip(), name.strip(), role, password_hash, itd, now),
            )
            return int(cur.lastrowid)

    def list_team_members(self, hs_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, health_system_id, email, name, role, is_team_director, created_at "
                "FROM team_members WHERE health_system_id = ? ORDER BY id ASC",
                (hs_id,),
            ).fetchall()
            out: List[Dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                d["is_team_director"] = bool(d.get("is_team_director") or 0)
                out.append(d)
            return out

    def get_team_member(self, hs_id: str, email: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM team_members WHERE health_system_id = ? AND email = ?",
                (hs_id, email.lower().strip()),
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            d["is_team_director"] = bool(d.get("is_team_director") or 0)
            return d

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
                INSERT INTO team_members (health_system_id, email, name, role, password_hash, is_team_director, created_at)
                VALUES (?, ?, ?, 'surgeon', ?, 1, ?)
                ON CONFLICT(health_system_id, email) DO UPDATE SET
                    name = excluded.name,
                    role = 'surgeon',
                    is_team_director = 1,
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
        patient_id: Optional[str] = None,
        event_type: str,
        occurred_at: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        ts = occurred_at or _utcnow_iso()
        episode = self.get_episode(patient_id) if patient_id else None
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
                    episode.get("open_date") if episode else None,
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

    def delete_event_logs_from_episode_day(self, patient_id: str, min_day: int) -> int:
        """Remove event_logs whose 1-indexed episode day is >= min_day."""
        episode = self.get_episode(patient_id)
        if not episode or not episode.get("open_date"):
            return 0
        try:
            open_dt = date.fromisoformat(episode["open_date"])
        except (TypeError, ValueError):
            return 0
        delete_ids: List[int] = []
        for ev in self.get_events(patient_id):
            try:
                event_date = datetime.fromisoformat(str(ev["occurred_at"]).replace("Z", "")).date()
                day_num = (event_date - open_dt).days + 1
            except (TypeError, ValueError):
                continue
            if day_num >= min_day:
                delete_ids.append(int(ev["id"]))
        if not delete_ids:
            return 0
        placeholders = ",".join("?" for _ in delete_ids)
        with self._conn() as conn:
            conn.execute(f"DELETE FROM event_logs WHERE id IN ({placeholders})", delete_ids)
        return len(delete_ids)

    def delete_daily_checkin_sends_from_episode_day(self, patient_id: str, min_day: int) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM daily_checkin_sends WHERE patient_id = ? AND episode_day >= ?",
                (patient_id, min_day),
            )
            return int(cur.rowcount or 0)

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

    # ─── Intra-Op Reassessment (PRD v1.0) ─────────────────────────────────

    def _row_to_intraop_form(self, row: sqlite3.Row) -> Dict[str, Any]:
        rec = dict(row)
        rec["fields"] = json.loads(rec.pop("fields_json") or "{}")
        rec["field_origins"] = json.loads(rec.pop("field_origins_json") or "{}")
        ps = rec.pop("procedure_specific_json", None)
        rec["procedure_specific"] = json.loads(ps) if ps else None
        return rec

    def get_or_create_intraop_form(
        self,
        *,
        patient_id: str,
        or_started_at: Optional[str] = None,
        or_ended_at: Optional[str] = None,
        or_duration_minutes: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Idempotent: returns the existing form if one is already on file,
        else creates a new NEW row and returns it."""
        existing = self.get_intraop_form(patient_id)
        if existing:
            return existing
        now = _utcnow_iso()
        form_id = uuid.uuid4().hex
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO intraop_forms (
                    id, patient_id, status,
                    or_started_at, or_ended_at, or_duration_minutes,
                    fields_json, field_origins_json, procedure_specific_json,
                    created_at, updated_at
                ) VALUES (?, ?, 'NEW', ?, ?, ?, '{}', '{}', NULL, ?, ?)
                """,
                (form_id, patient_id, or_started_at, or_ended_at, or_duration_minutes, now, now),
            )
        return self.get_intraop_form(patient_id)  # type: ignore[return-value]

    def get_intraop_form(self, patient_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM intraop_forms WHERE patient_id = ?",
                (patient_id,),
            ).fetchone()
            if not row:
                return None
            return self._row_to_intraop_form(row)

    def get_intraop_form_by_id(self, form_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM intraop_forms WHERE id = ?",
                (form_id,),
            ).fetchone()
            if not row:
                return None
            return self._row_to_intraop_form(row)

    def update_intraop_form_fields(
        self,
        *,
        patient_id: str,
        fields: Dict[str, Any],
        field_origins: Dict[str, Any],
        procedure_specific: Optional[Dict[str, Any]] = None,
        or_started_at: Optional[str] = None,
        or_ended_at: Optional[str] = None,
        or_duration_minutes: Optional[int] = None,
        status: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Whole-blob replace of fields + origins. Caller is expected to
        merge upstream and pass the full intended state."""
        now = _utcnow_iso()
        sets: List[str] = ["fields_json = ?", "field_origins_json = ?", "updated_at = ?"]
        args: List[Any] = [json.dumps(fields), json.dumps(field_origins), now]
        if procedure_specific is not None:
            sets.append("procedure_specific_json = ?")
            args.append(json.dumps(procedure_specific))
        if or_started_at is not None:
            sets.append("or_started_at = ?"); args.append(or_started_at)
        if or_ended_at is not None:
            sets.append("or_ended_at = ?"); args.append(or_ended_at)
        if or_duration_minutes is not None:
            sets.append("or_duration_minutes = ?"); args.append(or_duration_minutes)
        if status is not None:
            sets.append("status = ?"); args.append(status)
        args.append(patient_id)
        with self._conn() as conn:
            conn.execute(
                f"UPDATE intraop_forms SET {', '.join(sets)} WHERE patient_id = ?",
                tuple(args),
            )
        return self.get_intraop_form(patient_id)

    def lock_intraop_form(self, *, patient_id: str, surgeon_user_id: str) -> Optional[Dict[str, Any]]:
        """Lock the form. Pass-4: only allowed when status='READY_FOR_SURGEON_REVIEW'."""
        now = _utcnow_iso()
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE intraop_forms
                SET status = 'LOCKED',
                    surgeon_locked_by = ?,
                    surgeon_locked_at = ?,
                    updated_at = ?
                WHERE patient_id = ? AND status = 'READY_FOR_SURGEON_REVIEW'
                """,
                (surgeon_user_id, now, now, patient_id),
            )
            if cur.rowcount == 0:
                return None
        return self.get_intraop_form(patient_id)

    def mark_intraop_form_ready_for_review(
        self,
        *,
        patient_id: str,
        rn_user_id: str,
    ) -> Optional[Dict[str, Any]]:
        """RN flips an IN_PROGRESS form to READY_FOR_SURGEON_REVIEW."""
        now = _utcnow_iso()
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE intraop_forms
                SET status = 'READY_FOR_SURGEON_REVIEW',
                    draft_completed_by = ?,
                    draft_completed_at = ?,
                    updated_at = ?
                WHERE patient_id = ?
                  AND status IN ('NEW', 'IN_PROGRESS', 'REOPENED')
                """,
                (rn_user_id, now, now, patient_id),
            )
            if cur.rowcount == 0:
                return None
        return self.get_intraop_form(patient_id)

    def recall_intraop_form_draft(self, *, patient_id: str) -> Optional[Dict[str, Any]]:
        """RN pulls a READY_FOR_SURGEON_REVIEW form back to IN_PROGRESS, clearing
        draft-completion attribution."""
        now = _utcnow_iso()
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE intraop_forms
                SET status = 'IN_PROGRESS',
                    draft_completed_by = NULL,
                    draft_completed_at = NULL,
                    updated_at = ?
                WHERE patient_id = ? AND status = 'READY_FOR_SURGEON_REVIEW'
                """,
                (now, patient_id),
            )
            if cur.rowcount == 0:
                return None
        return self.get_intraop_form(patient_id)

    def reopen_intraop_form(self, *, patient_id: str) -> Optional[Dict[str, Any]]:
        now = _utcnow_iso()
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE intraop_forms
                SET status = 'REOPENED', updated_at = ?
                WHERE patient_id = ? AND status = 'LOCKED'
                """,
                (now, patient_id),
            )
            if cur.rowcount == 0:
                return None
        return self.get_intraop_form(patient_id)

    def mark_intraop_conservative_default_applied(self, *, patient_id: str) -> bool:
        """Atomic CAS — returns True iff the flag was newly set."""
        now = _utcnow_iso()
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE intraop_forms
                SET conservative_default_applied_at = ?, updated_at = ?
                WHERE patient_id = ?
                  AND conservative_default_applied_at IS NULL
                """,
                (now, now, patient_id),
            )
            return cur.rowcount > 0

    def list_intraop_forms_by_status(self, status: str) -> List[Dict[str, Any]]:
        """Pass-4: surgeon "Forms awaiting your review" surface."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM intraop_forms
                WHERE status = ?
                ORDER BY draft_completed_at ASC, updated_at ASC
                """,
                (status,),
            ).fetchall()
            return [self._row_to_intraop_form(r) for r in rows]

    def list_intraop_overdue_forms(
        self,
        *,
        now_iso: str,
        threshold_hours: int,
    ) -> List[Dict[str, Any]]:
        """Forms whose `or_ended_at` ≥ `threshold_hours` ago, not yet
        LOCKED and not yet flagged with the conservative default."""
        cutoff = (datetime.fromisoformat(now_iso) - timedelta(hours=threshold_hours)).isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM intraop_forms
                WHERE or_ended_at IS NOT NULL
                  AND or_ended_at <= ?
                  AND status != 'LOCKED'
                  AND conservative_default_applied_at IS NULL
                ORDER BY or_ended_at ASC
                """,
                (cutoff,),
            ).fetchall()
            return [self._row_to_intraop_form(r) for r in rows]

    # Extractions

    def save_intraop_extraction(
        self,
        *,
        extraction_id: str,
        patient_id: str,
        pdf_blob_url: str,
        status: str = "PENDING",
        model_version: Optional[str] = None,
        prompt_version: Optional[str] = None,
    ) -> Dict[str, Any]:
        now = _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO intraop_extractions (
                    id, patient_id, pdf_blob_url, status,
                    model_version, prompt_version, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (extraction_id, patient_id, pdf_blob_url, status, model_version, prompt_version, now),
            )
            conn.execute(
                "UPDATE intraop_forms SET extraction_id = ?, pdf_blob_url = ?, updated_at = ? WHERE patient_id = ?",
                (extraction_id, pdf_blob_url, now, patient_id),
            )
        return self.get_intraop_extraction(extraction_id)  # type: ignore[return-value]

    def update_intraop_extraction(
        self,
        *,
        extraction_id: str,
        status: Optional[str] = None,
        raw_text: Optional[str] = None,
        fields: Optional[Dict[str, Any]] = None,
        field_confidences: Optional[Dict[str, Any]] = None,
        warnings: Optional[List[str]] = None,
        error_message: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        sets: List[str] = []
        args: List[Any] = []
        if status is not None:
            sets.append("status = ?"); args.append(status)
            if status in ("COMPLETE", "FAILED"):
                sets.append("completed_at = ?"); args.append(_utcnow_iso())
        if raw_text is not None:
            sets.append("raw_text = ?"); args.append(raw_text)
        if fields is not None:
            sets.append("fields_json = ?"); args.append(json.dumps(fields))
        if field_confidences is not None:
            sets.append("field_confidences_json = ?"); args.append(json.dumps(field_confidences))
        if warnings is not None:
            sets.append("warnings_json = ?"); args.append(json.dumps(warnings))
        if error_message is not None:
            sets.append("error_message = ?"); args.append(error_message)
        if not sets:
            return self.get_intraop_extraction(extraction_id)
        args.append(extraction_id)
        with self._conn() as conn:
            conn.execute(
                f"UPDATE intraop_extractions SET {', '.join(sets)} WHERE id = ?",
                tuple(args),
            )
        return self.get_intraop_extraction(extraction_id)

    def get_intraop_extraction(self, extraction_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM intraop_extractions WHERE id = ?",
                (extraction_id,),
            ).fetchone()
            if not row:
                return None
            rec = dict(row)
            rec["fields"] = json.loads(rec.pop("fields_json") or "{}") if rec.get("fields_json") else {}
            rec["field_confidences"] = (
                json.loads(rec.pop("field_confidences_json") or "{}") if rec.get("field_confidences_json") else {}
            )
            rec["warnings"] = json.loads(rec.pop("warnings_json") or "[]") if rec.get("warnings_json") else []
            return rec

    # Reassessments

    def save_intraop_reassessment(
        self,
        *,
        reassessment_id: str,
        patient_id: str,
        intraop_form_id: str,
        form_snapshot: Dict[str, Any],
        pre_or_current_tier: str,
        proposed_tier: str,
        final_tier: str,
        hard_upgrade_applied: bool,
        upgrade_steps: int,
        reasons: List[Dict[str, Any]],
        is_conservative_default: bool,
        procedure_family: Optional[str],
        model_version: str,
        tuning_version: int,
        triggered_by: str,
    ) -> Dict[str, Any]:
        now = _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO intraop_reassessments (
                    id, patient_id, intraop_form_id, form_snapshot_json,
                    pre_or_current_tier, proposed_tier, final_tier,
                    hard_upgrade_applied, upgrade_steps, reasons_json,
                    is_conservative_default, procedure_family,
                    model_version, tuning_version, triggered_by, triggered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reassessment_id, patient_id, intraop_form_id,
                    json.dumps(form_snapshot),
                    pre_or_current_tier, proposed_tier, final_tier,
                    1 if hard_upgrade_applied else 0,
                    int(upgrade_steps),
                    json.dumps(reasons),
                    1 if is_conservative_default else 0,
                    procedure_family,
                    model_version, int(tuning_version), triggered_by, now,
                ),
            )
        return self.get_intraop_reassessment(reassessment_id)  # type: ignore[return-value]

    def _row_to_reassessment(self, row: sqlite3.Row) -> Dict[str, Any]:
        rec = dict(row)
        rec["form_snapshot"] = json.loads(rec.pop("form_snapshot_json") or "{}")
        rec["reasons"] = json.loads(rec.pop("reasons_json") or "[]")
        rec["hard_upgrade_applied"] = bool(rec.get("hard_upgrade_applied"))
        rec["is_conservative_default"] = bool(rec.get("is_conservative_default"))
        return rec

    def get_intraop_reassessment(self, reassessment_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM intraop_reassessments WHERE id = ?",
                (reassessment_id,),
            ).fetchone()
            if not row:
                return None
            return self._row_to_reassessment(row)

    def list_intraop_reassessments(self, patient_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM intraop_reassessments
                WHERE patient_id = ?
                ORDER BY datetime(triggered_at) DESC, rowid DESC
                """,
                (patient_id,),
            ).fetchall()
            return [self._row_to_reassessment(r) for r in rows]

    # ─── Post-Op Scoring (PRD v1.0) ───────────────────────────────────────

    # Daily check-in sends + responses
    def record_daily_checkin_send(
        self,
        *,
        patient_id: str,
        episode_day: int,
        sent_at: Optional[str] = None,
        channel: str = "PUSH",
    ) -> bool:
        """Insert-if-absent so each (patient, day) sends at most once.
        Returns True if a new row was created, False if it already
        existed (idempotent under cron retry)."""
        ts = sent_at or _utcnow_iso()
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO daily_checkin_sends (patient_id, episode_day, sent_at, channel) VALUES (?, ?, ?, ?)",
                    (patient_id, int(episode_day), ts, channel),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def has_daily_checkin_send(self, patient_id: str, episode_day: int) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM daily_checkin_sends WHERE patient_id = ? AND episode_day = ? LIMIT 1",
                (patient_id, int(episode_day)),
            ).fetchone()
            return row is not None

    def list_daily_checkin_sends_without_response(
        self,
        *,
        cutoff_iso: str,
    ) -> List[Dict[str, Any]]:
        """Sends whose `sent_at` is older than `cutoff_iso` and have no
        matching response row and no existing miss row.

        Used by `_postop_checkin_missed_watcher_loop` to mark misses
        past the 36-hour window (PRD §4.3)."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT s.*
                FROM daily_checkin_sends s
                LEFT JOIN daily_checkin_responses r
                  ON r.patient_id = s.patient_id AND r.episode_day = s.episode_day
                LEFT JOIN daily_checkin_misses   m
                  ON m.patient_id = s.patient_id AND m.episode_day = s.episode_day
                WHERE r.id IS NULL
                  AND m.id IS NULL
                  AND s.sent_at <= ?
                ORDER BY s.sent_at ASC
                """,
                (cutoff_iso,),
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_daily_checkin_miss(self, patient_id: str, episode_day: int) -> bool:
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO daily_checkin_misses (patient_id, episode_day, marked_at) VALUES (?, ?, ?)",
                    (patient_id, int(episode_day), _utcnow_iso()),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def save_daily_checkin_response(
        self,
        *,
        patient_id: str,
        episode_day: int,
        submitted_at: Optional[str],
        answers: Dict[str, Any],
        raw_total: float,
        tier: str,
        red_flags: List[str],
        new_red_flag: bool,
        wound_concern: bool,
        pain_nrs: Optional[int],
        pain_trajectory: Optional[str],
        item_scores: Dict[str, float],
    ) -> int:
        ts = submitted_at or _utcnow_iso()
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO daily_checkin_responses (
                    patient_id, episode_day, submitted_at, answers_json,
                    raw_total, tier, red_flags_json, new_red_flag, wound_concern,
                    pain_nrs, pain_trajectory, item_scores_json, completed
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    patient_id, int(episode_day), ts, json.dumps(answers or {}),
                    float(raw_total), tier, json.dumps(red_flags or []),
                    1 if new_red_flag else 0, 1 if wound_concern else 0,
                    pain_nrs, pain_trajectory, json.dumps(item_scores or {}),
                ),
            )
            return int(cur.lastrowid)

    def get_latest_daily_checkin_response(self, patient_id: str, episode_day: int) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM daily_checkin_responses
                WHERE patient_id = ? AND episode_day = ?
                ORDER BY submitted_at DESC, id DESC
                LIMIT 1
                """,
                (patient_id, int(episode_day)),
            ).fetchone()
            if not row:
                return None
            return self._hydrate_daily_checkin_response(dict(row))

    def list_recent_daily_checkin_responses(
        self,
        patient_id: str,
        *,
        since_iso: Optional[str] = None,
        limit: int = 60,
    ) -> List[Dict[str, Any]]:
        params: List[Any] = [patient_id]
        sql = "SELECT * FROM daily_checkin_responses WHERE patient_id = ?"
        if since_iso:
            sql += " AND submitted_at >= ?"
            params.append(since_iso)
        sql += " ORDER BY submitted_at DESC, id DESC LIMIT ?"
        params.append(int(limit))
        with self._conn() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._hydrate_daily_checkin_response(dict(r)) for r in rows]

    def list_daily_checkin_responses_in_range(
        self,
        patient_id: str,
        *,
        day_from: int,
        day_to: int,
    ) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM daily_checkin_responses
                WHERE patient_id = ? AND episode_day >= ? AND episode_day <= ?
                ORDER BY episode_day ASC, submitted_at DESC, id DESC
                """,
                (patient_id, int(day_from), int(day_to)),
            ).fetchall()
        return [self._hydrate_daily_checkin_response(dict(r)) for r in rows]

    @staticmethod
    def _hydrate_daily_checkin_response(rec: Dict[str, Any]) -> Dict[str, Any]:
        rec["answers"] = json.loads(rec.get("answers_json") or "{}")
        rec["red_flags"] = json.loads(rec.get("red_flags_json") or "[]")
        rec["item_scores"] = json.loads(rec.get("item_scores_json") or "{}")
        rec["new_red_flag"] = bool(rec.get("new_red_flag"))
        rec["wound_concern"] = bool(rec.get("wound_concern"))
        return rec

    # Day-X surveys (PRD §5)
    def upsert_dayx_survey_send(
        self,
        *,
        patient_id: str,
        day: int,
        sent_at: Optional[str] = None,
        procedure_family: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Idempotent: if a row already exists for (patient, day) it is
        returned untouched. Otherwise a PENDING row is created."""
        existing = self.get_dayx_survey(patient_id, day)
        if existing:
            return existing
        survey_id = uuid.uuid4().hex
        ts = sent_at or _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO dayx_surveys (
                    id, patient_id, day, sent_at, status, procedure_family
                )
                VALUES (?, ?, ?, ?, 'PENDING', ?)
                """,
                (survey_id, patient_id, int(day), ts, procedure_family),
            )
        return self.get_dayx_survey(patient_id, day)  # type: ignore[return-value]

    def submit_dayx_survey(
        self,
        *,
        patient_id: str,
        day: int,
        section_scores: Dict[str, float],
        total_score: float,
        tier: str,
        red_flags: List[str],
        raw_answers: Dict[str, Any],
        submitted_at: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        ts = submitted_at or _utcnow_iso()
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE dayx_surveys
                SET status = 'COMPLETED',
                    submitted_at = ?,
                    section_scores_json = ?,
                    total_score = ?,
                    tier = ?,
                    red_flags_json = ?,
                    raw_answers_json = ?
                WHERE patient_id = ? AND day = ?
                """,
                (
                    ts,
                    json.dumps(section_scores or {}),
                    float(total_score),
                    tier,
                    json.dumps(red_flags or []),
                    json.dumps(raw_answers or {}),
                    patient_id, int(day),
                ),
            )
            if cur.rowcount == 0:
                # No PENDING row — create a fresh COMPLETED row.
                self.upsert_dayx_survey_send(patient_id=patient_id, day=day, sent_at=ts)
                conn.execute(
                    """
                    UPDATE dayx_surveys
                    SET status = 'COMPLETED',
                        submitted_at = ?,
                        section_scores_json = ?,
                        total_score = ?,
                        tier = ?,
                        red_flags_json = ?,
                        raw_answers_json = ?
                    WHERE patient_id = ? AND day = ?
                    """,
                    (
                        ts,
                        json.dumps(section_scores or {}),
                        float(total_score),
                        tier,
                        json.dumps(red_flags or []),
                        json.dumps(raw_answers or {}),
                        patient_id, int(day),
                    ),
                )
        return self.get_dayx_survey(patient_id, day)

    def mark_dayx_survey_missed(self, patient_id: str, day: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE dayx_surveys
                SET status = 'MISSED'
                WHERE patient_id = ? AND day = ? AND status = 'PENDING'
                """,
                (patient_id, int(day)),
            )
            return cur.rowcount > 0

    def get_dayx_survey(self, patient_id: str, day: int) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM dayx_surveys WHERE patient_id = ? AND day = ?",
                (patient_id, int(day)),
            ).fetchone()
            if not row:
                return None
            return self._hydrate_dayx_survey(dict(row))

    def list_dayx_surveys(self, patient_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM dayx_surveys WHERE patient_id = ? ORDER BY day ASC",
                (patient_id,),
            ).fetchall()
        return [self._hydrate_dayx_survey(dict(r)) for r in rows]

    def list_overdue_dayx_surveys(self, *, cutoff_iso: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM dayx_surveys
                WHERE status = 'PENDING' AND sent_at <= ?
                ORDER BY sent_at ASC
                """,
                (cutoff_iso,),
            ).fetchall()
        return [self._hydrate_dayx_survey(dict(r)) for r in rows]

    @staticmethod
    def _hydrate_dayx_survey(rec: Dict[str, Any]) -> Dict[str, Any]:
        rec["section_scores"] = json.loads(rec.get("section_scores_json") or "{}") if rec.get("section_scores_json") else {}
        rec["red_flags"] = json.loads(rec.get("red_flags_json") or "[]") if rec.get("red_flags_json") else []
        rec["raw_answers"] = json.loads(rec.get("raw_answers_json") or "{}") if rec.get("raw_answers_json") else {}
        return rec

    # Med adherence (PRD §7)
    def record_med_adherence_ping(
        self,
        *,
        patient_id: str,
        episode_day: int,
        sent_at: Optional[str] = None,
    ) -> bool:
        ts = sent_at or _utcnow_iso()
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO med_adherence_pings (patient_id, episode_day, sent_at) VALUES (?, ?, ?)",
                    (patient_id, int(episode_day), ts),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def has_med_adherence_ping(self, patient_id: str, episode_day: int) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM med_adherence_pings WHERE patient_id = ? AND episode_day = ? LIMIT 1",
                (patient_id, int(episode_day)),
            ).fetchone()
        return row is not None

    def upsert_med_adherence_response(
        self,
        *,
        patient_id: str,
        episode_day: int,
        response: str,
        responded_at: Optional[str] = None,
    ) -> None:
        ts = responded_at or _utcnow_iso() if response != "MISSED_NON_RESPONSE" else None
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO med_adherence_responses (patient_id, episode_day, responded_at, response)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(patient_id, episode_day) DO UPDATE SET
                    responded_at = COALESCE(excluded.responded_at, med_adherence_responses.responded_at),
                    response = excluded.response
                """,
                (patient_id, int(episode_day), ts, response),
            )

    def list_med_adherence_responses(
        self,
        patient_id: str,
        *,
        day_from: Optional[int] = None,
        day_to: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        params: List[Any] = [patient_id]
        sql = "SELECT * FROM med_adherence_responses WHERE patient_id = ?"
        if day_from is not None:
            sql += " AND episode_day >= ?"; params.append(int(day_from))
        if day_to is not None:
            sql += " AND episode_day <= ?"; params.append(int(day_to))
        sql += " ORDER BY episode_day ASC"
        with self._conn() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def list_pings_without_response(self, *, cutoff_iso: str) -> List[Dict[str, Any]]:
        """Pings whose `sent_at` is older than `cutoff_iso` and have no
        response (used by the 23:00 non-response watcher)."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT p.*
                FROM med_adherence_pings p
                LEFT JOIN med_adherence_responses r
                  ON r.patient_id = p.patient_id AND r.episode_day = p.episode_day
                WHERE r.id IS NULL AND p.sent_at <= ?
                ORDER BY p.sent_at ASC
                """,
                (cutoff_iso,),
            ).fetchall()
        return [dict(r) for r in rows]

    # Post-op video events (PRD §6)
    def record_postop_video_event(
        self,
        *,
        patient_id: str,
        video_kind: str,
        event_type: str,
        session_id: str,
        occurred_at: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> int:
        ts = occurred_at or _utcnow_iso()
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO postop_video_events (
                    patient_id, video_kind, event_type, session_id, occurred_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (patient_id, video_kind, event_type, session_id, ts, json.dumps(payload or {})),
            )
            return int(cur.lastrowid)

    def list_postop_video_events(
        self,
        patient_id: str,
        *,
        video_kind: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        params: List[Any] = [patient_id]
        sql = "SELECT * FROM postop_video_events WHERE patient_id = ?"
        if video_kind:
            sql += " AND video_kind = ?"
            params.append(video_kind)
        sql += " ORDER BY occurred_at ASC, id ASC"
        with self._conn() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            rec = dict(r)
            rec["payload"] = json.loads(rec.get("payload_json") or "{}")
            out.append(rec)
        return out

    # Patient self-flag (PRD §9)
    def create_self_flag(
        self,
        *,
        patient_id: str,
        free_text: Optional[str] = None,
        source: str = "PATIENT_APP",
        flagged_at: Optional[str] = None,
    ) -> int:
        ts = flagged_at or _utcnow_iso()
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO patient_self_flags (patient_id, flagged_at, free_text, source)
                VALUES (?, ?, ?, ?)
                """,
                (patient_id, ts, free_text, source),
            )
            return int(cur.lastrowid)

    def resolve_self_flag(
        self,
        *,
        flag_id: int,
        resolved_by: str,
        resolved_at: Optional[str] = None,
    ) -> bool:
        ts = resolved_at or _utcnow_iso()
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE patient_self_flags
                SET resolved_at = ?, resolved_by = ?
                WHERE id = ? AND resolved_at IS NULL
                """,
                (ts, resolved_by, int(flag_id)),
            )
            return cur.rowcount > 0

    def has_active_self_flag(self, patient_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM patient_self_flags
                WHERE patient_id = ? AND resolved_at IS NULL
                LIMIT 1
                """,
                (patient_id,),
            ).fetchone()
        return row is not None

    def list_self_flags(self, patient_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM patient_self_flags
                WHERE patient_id = ?
                ORDER BY flagged_at DESC, id DESC
                """,
                (patient_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def last_self_flag_resolved_at(self, patient_id: str) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT resolved_at FROM patient_self_flags
                WHERE patient_id = ? AND resolved_at IS NOT NULL
                ORDER BY resolved_at DESC, id DESC
                LIMIT 1
                """,
                (patient_id,),
            ).fetchone()
        return row["resolved_at"] if row else None

    def last_response_timestamp_across_channels(self, patient_id: str) -> Optional[str]:
        """Most recent activity timestamp across check-in / med-adherence /
        survey / video / self-flag (PRD §10.2 LOST_CONTACT_*)."""
        sources: List[str] = []
        with self._conn() as conn:
            r1 = conn.execute(
                "SELECT MAX(submitted_at) AS ts FROM daily_checkin_responses WHERE patient_id = ?",
                (patient_id,),
            ).fetchone()
            if r1 and r1["ts"]:
                sources.append(r1["ts"])
            r2 = conn.execute(
                "SELECT MAX(responded_at) AS ts FROM med_adherence_responses WHERE patient_id = ? AND responded_at IS NOT NULL",
                (patient_id,),
            ).fetchone()
            if r2 and r2["ts"]:
                sources.append(r2["ts"])
            r3 = conn.execute(
                "SELECT MAX(submitted_at) AS ts FROM dayx_surveys WHERE patient_id = ? AND submitted_at IS NOT NULL",
                (patient_id,),
            ).fetchone()
            if r3 and r3["ts"]:
                sources.append(r3["ts"])
            r4 = conn.execute(
                "SELECT MAX(occurred_at) AS ts FROM postop_video_events WHERE patient_id = ?",
                (patient_id,),
            ).fetchone()
            if r4 and r4["ts"]:
                sources.append(r4["ts"])
            r5 = conn.execute(
                "SELECT MAX(flagged_at) AS ts FROM patient_self_flags WHERE patient_id = ?",
                (patient_id,),
            ).fetchone()
            if r5 and r5["ts"]:
                sources.append(r5["ts"])
        if not sources:
            return None
        return max(sources)

    # Post-op re-tier events (PRD §10)
    def save_postop_retier_event(
        self,
        *,
        event_id: str,
        patient_id: str,
        triggered_by: str,
        inputs_snapshot: Dict[str, Any],
        post_intraop_tier: str,
        computed_delta: int,
        computed_tier: str,
        tier_before: str,
        tier_after: str,
        changed: bool,
        reasons: List[Dict[str, Any]],
        model_version: str,
        tuning_version: int,
    ) -> Dict[str, Any]:
        now = _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO postop_retier_events (
                    id, patient_id, triggered_by, inputs_snapshot_json,
                    post_intraop_tier, computed_delta, computed_tier,
                    tier_before, tier_after, changed, reasons_json,
                    model_version, tuning_version, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id, patient_id, triggered_by, json.dumps(inputs_snapshot or {}),
                    post_intraop_tier, int(computed_delta), computed_tier,
                    tier_before, tier_after, 1 if changed else 0, json.dumps(reasons or []),
                    model_version, int(tuning_version), now,
                ),
            )
        return self.get_postop_retier_event(event_id)  # type: ignore[return-value]

    def get_postop_retier_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM postop_retier_events WHERE id = ?",
                (event_id,),
            ).fetchone()
            if not row:
                return None
            return self._hydrate_postop_retier_event(dict(row))

    def list_postop_retier_events(
        self,
        patient_id: str,
        *,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM postop_retier_events
                WHERE patient_id = ?
                ORDER BY datetime(created_at) DESC, rowid DESC
                LIMIT ?
                """,
                (patient_id, int(limit)),
            ).fetchall()
        return [self._hydrate_postop_retier_event(dict(r)) for r in rows]

    @staticmethod
    def _hydrate_postop_retier_event(rec: Dict[str, Any]) -> Dict[str, Any]:
        rec["inputs_snapshot"] = json.loads(rec.get("inputs_snapshot_json") or "{}")
        rec["reasons"] = json.loads(rec.get("reasons_json") or "[]")
        rec["changed"] = bool(rec.get("changed"))
        return rec

    # ─── PAM assessments (Pre-Op Re-Tier PRD §4.1) ──────────────────────────

    def save_pam_assessment(
        self,
        *,
        assessment_id: Optional[str] = None,
        episode_id: str,
        patient_id: str,
        responses: List[Dict[str, Any]],
        raw_sum: int,
        items_scored: int,
        raw_average: float,
        activation_score: float,
        level: str,
        is_complete: bool,
        model_version: Optional[str] = None,
        tuning_version: Optional[int] = None,
        completed_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Insert a `pam_assessments` row and return the hydrated record.

        `episode_id` is the per-episode identifier used by the pre-op
        re-tier router; in the v1 single-episode-per-patient model, it
        equals `patient_id`. The duplicate column is kept so a future
        multi-episode-per-patient migration is a no-op on this table.
        """
        aid = assessment_id or uuid.uuid4().hex
        now = _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO pam_assessments (
                    id, episode_id, patient_id, responses_json,
                    raw_sum, items_scored, raw_average, activation_score,
                    level, is_complete, model_version, tuning_version,
                    completed_at, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    aid, episode_id, patient_id,
                    json.dumps(responses or []),
                    int(raw_sum), int(items_scored),
                    float(raw_average), float(activation_score),
                    level, 1 if is_complete else 0,
                    model_version, (int(tuning_version) if tuning_version is not None else None),
                    completed_at, now,
                ),
            )
        return self.get_pam_assessment(aid)  # type: ignore[return-value]

    def get_pam_assessment(self, assessment_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM pam_assessments WHERE id = ?",
                (assessment_id,),
            ).fetchone()
        if not row:
            return None
        return self._hydrate_pam_assessment(dict(row))

    def get_latest_pam_assessment(self, patient_id: str) -> Optional[Dict[str, Any]]:
        """Most recent PAM row for the patient (any episode)."""
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM pam_assessments
                WHERE patient_id = ?
                ORDER BY datetime(created_at) DESC, rowid DESC
                LIMIT 1
                """,
                (patient_id,),
            ).fetchone()
        if not row:
            return None
        return self._hydrate_pam_assessment(dict(row))

    def list_pam_assessments(
        self,
        patient_id: str,
        *,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM pam_assessments
                WHERE patient_id = ?
                ORDER BY datetime(created_at) DESC, rowid DESC
                LIMIT ?
                """,
                (patient_id, int(limit)),
            ).fetchall()
        return [self._hydrate_pam_assessment(dict(r)) for r in rows]

    @staticmethod
    def _hydrate_pam_assessment(rec: Dict[str, Any]) -> Dict[str, Any]:
        rec["responses"] = json.loads(rec.get("responses_json") or "[]")
        rec["is_complete"] = bool(rec.get("is_complete"))
        return rec

    # ─── Pre-Op re-tier events (Pre-Op Re-Tier PRD §10) ─────────────────────

    def save_preop_retier_event(
        self,
        *,
        event_id: str,
        episode_id: str,
        triggered_by: str,
        inputs_snapshot: Dict[str, Any],
        initial_tier: str,
        initial_tier_was_hard: bool,
        computed_delta: int,
        computed_tier: str,
        tier_before: str,
        tier_after: str,
        changed: bool,
        reasons: List[Dict[str, Any]],
        model_version: str,
        tuning_version: int,
    ) -> Dict[str, Any]:
        now = _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO preop_retier_events (
                    id, episode_id, triggered_by, inputs_snapshot_json,
                    initial_tier, initial_tier_was_hard,
                    computed_delta, computed_tier,
                    tier_before, tier_after, changed, reasons_json,
                    model_version, tuning_version, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id, episode_id, triggered_by,
                    json.dumps(inputs_snapshot or {}),
                    initial_tier, 1 if initial_tier_was_hard else 0,
                    int(computed_delta), computed_tier,
                    tier_before, tier_after, 1 if changed else 0,
                    json.dumps(reasons or []),
                    model_version, int(tuning_version), now,
                ),
            )
        return self.get_preop_retier_event(event_id)  # type: ignore[return-value]

    def get_preop_retier_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM preop_retier_events WHERE id = ?",
                (event_id,),
            ).fetchone()
        if not row:
            return None
        return self._hydrate_preop_retier_event(dict(row))

    def list_preop_retier_events(
        self,
        patient_id: str,
        *,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """List by episode_id (== patient_id in the v1 single-episode model)."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM preop_retier_events
                WHERE episode_id = ?
                ORDER BY datetime(created_at) DESC, rowid DESC
                LIMIT ?
                """,
                (patient_id, int(limit)),
            ).fetchall()
        return [self._hydrate_preop_retier_event(dict(r)) for r in rows]

    @staticmethod
    def _hydrate_preop_retier_event(rec: Dict[str, Any]) -> Dict[str, Any]:
        rec["inputs_snapshot"] = json.loads(rec.get("inputs_snapshot_json") or "{}")
        rec["reasons"] = json.loads(rec.get("reasons_json") or "[]")
        rec["changed"] = bool(rec.get("changed"))
        rec["initial_tier_was_hard"] = bool(rec.get("initial_tier_was_hard"))
        return rec

    # ─── Episode snapshots (Triage Suite Pass 3 §1) ────────────────────────

    _EPISODE_SNAPSHOT_COLUMNS = (
        "initial_tier_was_hard_escalator",
        "post_intake_tier",
        "post_intraop_tier",
    )

    def upsert_episode_snapshot(
        self,
        patient_id: str,
        **fields: Any,
    ) -> Dict[str, Any]:
        """Partial upsert. Only writes the columns explicitly passed.

        `initial_tier_was_hard_escalator` is coerced to INTEGER 0/1.
        Tier columns may be either str or None.
        """
        unknown = set(fields) - set(self._EPISODE_SNAPSHOT_COLUMNS)
        if unknown:
            raise ValueError(f"unknown episode_snapshot fields: {sorted(unknown)}")

        coerced: Dict[str, Any] = {}
        for k, v in fields.items():
            if k == "initial_tier_was_hard_escalator":
                coerced[k] = 1 if bool(v) else 0
            else:
                coerced[k] = v if v is None else str(v)

        now = _utcnow_iso()
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT * FROM episode_snapshots WHERE patient_id = ?",
                (patient_id,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO episode_snapshots (
                        patient_id, initial_tier_was_hard_escalator,
                        post_intake_tier, post_intraop_tier, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        patient_id,
                        coerced.get("initial_tier_was_hard_escalator", 0),
                        coerced.get("post_intake_tier"),
                        coerced.get("post_intraop_tier"),
                        now,
                    ),
                )
            else:
                set_clauses = []
                values: list[Any] = []
                for k in self._EPISODE_SNAPSHOT_COLUMNS:
                    if k in coerced:
                        set_clauses.append(f"{k} = ?")
                        values.append(coerced[k])
                set_clauses.append("updated_at = ?")
                values.append(now)
                values.append(patient_id)
                conn.execute(
                    f"UPDATE episode_snapshots SET {', '.join(set_clauses)} "
                    "WHERE patient_id = ?",
                    tuple(values),
                )
        return self.get_episode_snapshot(patient_id) or {}

    def get_episode_snapshot(self, patient_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM episode_snapshots WHERE patient_id = ?",
                (patient_id,),
            ).fetchone()
        if not row:
            return None
        rec = dict(row)
        rec["initial_tier_was_hard_escalator"] = bool(
            rec.get("initial_tier_was_hard_escalator")
        )
        return rec

    # ─── Grounding check reports ───────────────────────────────────────────

    def save_grounding_report(
        self,
        *,
        patient_id: str,
        track: str,
        report: Dict[str, Any],
        accuracy: Dict[str, Any],
        script: str,
        regenerated: bool = False,
    ) -> int:
        ts = _utcnow_iso()
        excerpt = (script or "")[:600]
        report_json = json.dumps(report)
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO grounding_check_reports (
                    patient_id, track, verdict, coverage_pct, faithfulness_pct,
                    critical_failures, summary, script_excerpt, report_json,
                    model, prompt_version, regenerated, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    patient_id,
                    track,
                    report.get("verdict", "BLOCK"),
                    accuracy.get("coverage_pct"),
                    accuracy.get("faithfulness_pct"),
                    accuracy.get("critical_failures", len(report.get("critical_failures") or [])),
                    report.get("summary"),
                    excerpt,
                    report_json,
                    report.get("model"),
                    report.get("prompt_version"),
                    1 if regenerated else 0,
                    ts,
                ),
            )
            return int(cur.lastrowid)

    def list_grounding_reports(
        self,
        *,
        limit: int = 100,
        verdict: Optional[str] = None,
        track: Optional[str] = None,
        prompt_version: Optional[str] = None,
        since: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if verdict:
            clauses.append("verdict = ?")
            params.append(verdict.upper())
        if track:
            clauses.append("track = ?")
            params.append(track)
        if prompt_version:
            clauses.append("prompt_version = ?")
            params.append(prompt_version)
        if since:
            clauses.append("created_at >= ?")
            params.append(since)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT id, patient_id, track, verdict, coverage_pct, faithfulness_pct,
                       critical_failures, summary, script_excerpt, model, prompt_version,
                       regenerated, created_at
                FROM grounding_check_reports
                {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_llm_calls(
        self,
        *,
        limit: int = 200,
        role: Optional[str] = None,
        prompt_id: Optional[str] = None,
        prompt_version: Optional[str] = None,
        since: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        clauses = ["event_type = 'llm_call'"]
        params: List[Any] = []
        if since:
            clauses.append("occurred_at >= ?")
            params.append(since)
        where = " AND ".join(clauses)
        params.append(limit * 4)
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT id, patient_id, occurred_at, payload_json
                FROM event_logs
                WHERE {where}
                ORDER BY occurred_at DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except Exception:
                continue
            pmeta = payload.get("prompt") or {}
            if role and payload.get("role") != role:
                continue
            if prompt_id and pmeta.get("prompt_id") != prompt_id:
                continue
            if prompt_version and pmeta.get("version") != prompt_version:
                continue
            usage = payload.get("usage") or {}
            out.append(
                {
                    "id": row["id"],
                    "occurred_at": row["occurred_at"],
                    "patient_id": row["patient_id"],
                    "role": payload.get("role"),
                    "model": payload.get("model"),
                    "ai_config_version": payload.get("ai_config_version"),
                    "prompt_id": pmeta.get("prompt_id"),
                    "prompt_version": pmeta.get("version"),
                    "prompt_sha": pmeta.get("sha"),
                    "purpose": payload.get("purpose"),
                    "latency_ms": payload.get("latency_ms"),
                    "input_tokens": usage.get("input"),
                    "output_tokens": usage.get("output"),
                    "request_id": payload.get("anthropic_request_id"),
                    "audit_error": payload.get("audit_error"),
                }
            )
            if len(out) >= limit:
                break
        return out

    def llm_call_stats(self, *, window_days: int = 30) -> Dict[str, Any]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM event_logs WHERE event_type='llm_call' AND occurred_at >= ?",
                (cutoff,),
            ).fetchall()
        by_role: Dict[str, Dict[str, Any]] = {}
        total_in = 0
        total_out = 0
        total_calls = 0
        models_in_use: set[str] = set()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except json.JSONDecodeError:
                # Keep stats endpoint resilient to legacy malformed payload rows.
                continue
            role = payload.get("role") or "unknown"
            usage = payload.get("usage") or {}
            if payload.get("model"):
                models_in_use.add(payload["model"])
            bucket = by_role.setdefault(
                role,
                {"calls": 0, "input_tokens": 0, "output_tokens": 0, "latency_ms_sum": 0},
            )
            bucket["calls"] += 1
            bucket["input_tokens"] += usage.get("input") or 0
            bucket["output_tokens"] += usage.get("output") or 0
            bucket["latency_ms_sum"] += payload.get("latency_ms") or 0
            total_calls += 1
            total_in += usage.get("input") or 0
            total_out += usage.get("output") or 0
        for bucket in by_role.values():
            bucket["avg_latency_ms"] = round(bucket["latency_ms_sum"] / bucket["calls"]) if bucket["calls"] else 0
            bucket.pop("latency_ms_sum", None)
        return {
            "window_days": window_days,
            "total_calls": total_calls,
            "total_input_tokens": total_in,
            "total_output_tokens": total_out,
            "by_role": by_role,
            "models_in_use": sorted(models_in_use),
        }

    def get_grounding_report(self, report_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM grounding_check_reports WHERE id = ?",
                (report_id,),
            ).fetchone()
        if not row:
            return None
        rec = dict(row)
        rec["regenerated"] = bool(rec.get("regenerated"))
        try:
            rec["report"] = json.loads(rec.pop("report_json") or "{}")
        except json.JSONDecodeError:
            rec["report"] = {}
        return rec

    def grounding_summary_stats(self, *, window_days: int = 30) -> Dict[str, Any]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT track, verdict, coverage_pct, faithfulness_pct
                FROM grounding_check_reports
                WHERE created_at >= ?
                """,
                (cutoff,),
            ).fetchall()
        total = len(rows)
        pass_n = sum(1 for r in rows if r["verdict"] == "PASS")
        review_n = sum(1 for r in rows if r["verdict"] == "REVIEW")
        block_n = sum(1 for r in rows if r["verdict"] == "BLOCK")
        cov_vals = [r["coverage_pct"] for r in rows if r["coverage_pct"] is not None]
        faith_vals = [r["faithfulness_pct"] for r in rows if r["faithfulness_pct"] is not None]
        by_track: Dict[str, Dict[str, int]] = {}
        for r in rows:
            t = r["track"] or "unknown"
            by_track.setdefault(t, {"total": 0, "pass": 0, "review": 0, "block": 0})
            by_track[t]["total"] += 1
            v = (r["verdict"] or "").lower()
            if v in by_track[t]:
                by_track[t][v] += 1
        return {
            "window_days": window_days,
            "total": total,
            "pass": pass_n,
            "review": review_n,
            "block": block_n,
            "block_rate": round(block_n / total, 3) if total else 0.0,
            "review_rate": round(review_n / total, 3) if total else 0.0,
            "avg_coverage_pct": round(sum(cov_vals) / len(cov_vals), 1) if cov_vals else None,
            "avg_faithfulness_pct": round(sum(faith_vals) / len(faith_vals), 1) if faith_vals else None,
            "by_track": by_track,
        }

    # ─── Teach-back sessions ────────────────────────────────────────────────

    def save_teachback_session(
        self,
        *,
        patient_id: str,
        track: str,
        questions: List[Dict[str, Any]],
        results: Dict[str, Any],
        completed: bool,
        prompt_version: str,
        model: str,
    ) -> int:
        ts = _utcnow_iso()
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO teachback_sessions (
                    patient_id, track, questions_json, results_json, completed,
                    prompt_version, model, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    patient_id,
                    track,
                    json.dumps(questions or []),
                    json.dumps(results or {}),
                    1 if completed else 0,
                    prompt_version,
                    model,
                    ts,
                    ts,
                ),
            )
            return int(cur.lastrowid)

    def update_teachback_session(
        self,
        *,
        session_id: int,
        questions: Optional[List[Dict[str, Any]]] = None,
        results: Optional[Dict[str, Any]] = None,
        completed: Optional[bool] = None,
        prompt_version: Optional[str] = None,
        model: Optional[str] = None,
    ) -> bool:
        updates: List[str] = []
        values: List[Any] = []
        if questions is not None:
            updates.append("questions_json = ?")
            values.append(json.dumps(questions))
        if results is not None:
            updates.append("results_json = ?")
            values.append(json.dumps(results))
        if completed is not None:
            updates.append("completed = ?")
            values.append(1 if completed else 0)
        if prompt_version is not None:
            updates.append("prompt_version = ?")
            values.append(prompt_version)
        if model is not None:
            updates.append("model = ?")
            values.append(model)
        updates.append("updated_at = ?")
        values.append(_utcnow_iso())
        values.append(session_id)
        with self._conn() as conn:
            cur = conn.execute(
                f"UPDATE teachback_sessions SET {', '.join(updates)} WHERE id = ?",
                tuple(values),
            )
            return bool(cur.rowcount)

    def get_teachback_session(self, session_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM teachback_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if not row:
            return None
        rec = dict(row)
        rec["completed"] = bool(rec.get("completed"))
        try:
            rec["questions"] = json.loads(rec.pop("questions_json") or "[]")
        except json.JSONDecodeError:
            rec["questions"] = []
        try:
            rec["results"] = json.loads(rec.pop("results_json") or "{}")
        except json.JSONDecodeError:
            rec["results"] = {}
        return rec

    def get_latest_teachback_session(self, *, patient_id: str, track: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM teachback_sessions
                WHERE patient_id = ? AND track = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (patient_id, track),
            ).fetchone()
        if not row:
            return None
        return self.get_teachback_session(int(row["id"]))

    def list_teachback_sessions(
        self,
        *,
        limit: int = 100,
        track: Optional[str] = None,
        since: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if track:
            clauses.append("track = ?")
            params.append(track)
        if since:
            clauses.append("created_at >= ?")
            params.append(since)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT id, patient_id, track, completed, prompt_version, model, created_at, updated_at,
                       results_json
                FROM teachback_sessions
                {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for row in rows:
            rec = dict(row)
            rec["completed"] = bool(rec.get("completed"))
            try:
                rec["results"] = json.loads(rec.pop("results_json") or "{}")
            except json.JSONDecodeError:
                rec["results"] = {}
            out.append(rec)
        return out

    def teachback_summary_stats(self, *, window_days: int = 30) -> Dict[str, Any]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT track, completed, results_json
                FROM teachback_sessions
                WHERE created_at >= ?
                """,
                (cutoff,),
            ).fetchall()
        total_sessions = len(rows)
        completed_sessions = sum(1 for r in rows if int(r["completed"] or 0) == 1)
        status_counts: Dict[str, int] = {"PASS": 0, "PARTIAL": 0, "FAIL": 0}
        by_track: Dict[str, Dict[str, Any]] = {}
        by_domain: Dict[str, Dict[str, int]] = {}
        for row in rows:
            track = row["track"] or "unknown"
            try:
                results = json.loads(row["results_json"] or "{}")
            except json.JSONDecodeError:
                results = {}
            aggregate = (results or {}).get("aggregate") or {}
            final_status = str(aggregate.get("final_status") or "").upper()
            if final_status in status_counts:
                status_counts[final_status] += 1
            stats = by_track.setdefault(track, {"total": 0, "completed": 0, "status_counts": {"PASS": 0, "PARTIAL": 0, "FAIL": 0}})
            stats["total"] += 1
            stats["completed"] += 1 if int(row["completed"] or 0) == 1 else 0
            if final_status in stats["status_counts"]:
                stats["status_counts"][final_status] += 1
            for item in (results or {}).get("items") or []:
                domain = str(item.get("domain") or "unknown")
                status = str((item.get("final_grade") or {}).get("status") or "PARTIAL").upper()
                bucket = by_domain.setdefault(domain, {"PASS": 0, "PARTIAL": 0, "FAIL": 0})
                if status in bucket:
                    bucket[status] += 1
        return {
            "window_days": window_days,
            "total_sessions": total_sessions,
            "completed_sessions": completed_sessions,
            "completion_rate": round(completed_sessions / total_sessions, 3) if total_sessions else 0.0,
            "status_counts": status_counts,
            "by_track": by_track,
            "by_domain": by_domain,
        }

    def save_inspector_recall_snapshot(
        self, *, table_json: Dict[str, Any], prompt_version: str
    ) -> None:
        ts = _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO inspector_recall_snapshots (prompt_version, table_json, created_at)
                VALUES (?, ?, ?)
                """,
                (prompt_version, json.dumps(table_json), ts),
            )

    def get_latest_inspector_recall(self) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT prompt_version, table_json, created_at
                FROM inspector_recall_snapshots
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        if not row:
            return None
        rec = dict(row)
        try:
            rec["table"] = json.loads(rec.pop("table_json") or "{}")
        except json.JSONDecodeError:
            rec["table"] = {}
        return rec

    def save_teachback_recall_snapshot(
        self, *, table_json: Dict[str, Any], prompt_version: str
    ) -> None:
        ts = _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO teachback_recall_snapshots (prompt_version, table_json, created_at)
                VALUES (?, ?, ?)
                """,
                (prompt_version, json.dumps(table_json), ts),
            )

    def get_latest_teachback_recall(self) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT prompt_version, table_json, created_at
                FROM teachback_recall_snapshots
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        if not row:
            return None
        rec = dict(row)
        try:
            rec["table"] = json.loads(rec.pop("table_json") or "{}")
        except json.JSONDecodeError:
            rec["table"] = {}
        return rec

