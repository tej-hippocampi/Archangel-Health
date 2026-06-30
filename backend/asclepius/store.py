"""AsclepiusStore — SQLite persistence for the Expert Evaluation Portal.

Follows the ``team_store.py`` pattern exactly (``_conn()`` + ``row_factory``,
``_init_schema()`` via ``executescript``, parameterized SQL, JSON columns
deserialized on read) but writes to its OWN database file
(``ASCLEPIUS_DB_PATH``, default ``backend/asclepius.db``). It never touches
``team.db`` (PRD §0, §10).

Tables
  users         standalone Asclepius accounts (evaluator/admin/qa_reviewer)
  tasks         admin-loaded prompts + blinded candidate answers
  submissions   raw doctor output + lifecycle status + verification artifacts
  records       packaged training records (preference / ideal_answer / trace)
  events        append-only provenance log (mirrors team_store.event_logs)
  exports       delivery-batch manifests

A process-wide singleton is exposed via ``get_store()`` so the FastAPI router,
the auth dependencies, and the verification pipeline all share one instance.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from passlib.context import CryptContext

_pwd = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


def _utcnow_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def hash_password(password: str) -> str:
    return _pwd.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _pwd.verify(plain, hashed)
    except Exception:
        return False


# ─── Credential vault sealing (Tier B at rest) ────────────────────────────────
# The private credential vault (Tier B: name, NPI, license, education) is sealed
# with Fernet when ``ASCLEPIUS_VAULT_KEY`` is set, so PHI-adjacent identifiers are
# encrypted at rest. Without a key (dev) we store JSON plaintext but flag it, so a
# deployment can tell whether the vault is actually encrypted.
import logging as _logging

_vault_log = _logging.getLogger("asclepius.vault")


def _vault_key() -> Optional[str]:
    raw = (os.getenv("ASCLEPIUS_VAULT_KEY") or "").strip()
    return raw or None


def seal_vault(data: Dict[str, Any]) -> tuple:
    """Serialize + (optionally) encrypt a Tier B credential dict. Returns
    ``(blob, encrypted_flag)``."""
    plain = json.dumps(data or {}, ensure_ascii=False)
    key = _vault_key()
    if key:
        try:
            from cryptography.fernet import Fernet

            token = Fernet(key.encode("utf-8")).encrypt(plain.encode("utf-8")).decode("utf-8")
            return token, 1
        except Exception:
            _vault_log.warning(
                "ASCLEPIUS_VAULT_KEY is set but Fernet sealing failed; storing the "
                "credential vault unencrypted. Verify the key is a valid urlsafe-base64 "
                "32-byte Fernet key.",
                exc_info=True,
            )
    return plain, 0


def open_vault(blob: Optional[str], encrypted: int) -> Dict[str, Any]:
    """Inverse of :func:`seal_vault`. Returns ``{}`` (and logs) if an encrypted
    blob cannot be opened (e.g. the key was rotated away)."""
    if not blob:
        return {}
    if encrypted:
        key = _vault_key()
        if not key:
            _vault_log.error("Encrypted credential vault present but ASCLEPIUS_VAULT_KEY is not set.")
            return {}
        try:
            from cryptography.fernet import Fernet

            plain = Fernet(key.encode("utf-8")).decrypt(blob.encode("utf-8")).decode("utf-8")
            return json.loads(plain)
        except Exception:
            _vault_log.error("Failed to open the encrypted credential vault.", exc_info=True)
            return {}
    try:
        return json.loads(blob)
    except Exception:
        return {}


class AsclepiusStore:
    def __init__(self, db_path: Optional[str] = None):
        base_dir = os.path.dirname(__file__)
        # default lives next to the package, i.e. backend/asclepius.db
        default_path = os.path.join(os.path.dirname(base_dir), "asclepius.db")
        self.db_path = db_path or os.getenv("ASCLEPIUS_DB_PATH") or default_path
        # Create the parent dir so ASCLEPIUS_DB_PATH can point straight into a
        # mounted persistent volume (e.g. /data/asclepius.db) on first boot.
        parent = os.path.dirname(os.path.abspath(self.db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        # WAL = concurrent readers alongside a writer + writes that survive a
        # process crash / redeploy mid-request, so the labeled-data product is
        # never lost or corrupted. journal_mode persists on the file itself.
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
        self._init_schema()

    # ─── Connection ──────────────────────────────────────────────────────────
    def _conn(self) -> sqlite3.Connection:
        # busy_timeout: wait (don't error) if another request holds the write
        # lock — FastAPI serves requests from a threadpool against one file.
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id              TEXT PRIMARY KEY,
                    email           TEXT NOT NULL UNIQUE,
                    password_hash   TEXT NOT NULL,
                    role            TEXT NOT NULL DEFAULT 'evaluator',
                    specialty       TEXT,
                    board_cert      TEXT,
                    years_experience INTEGER,
                    organization    TEXT,
                    id_hashed       TEXT,
                    active          INTEGER NOT NULL DEFAULT 1,
                    full_name       TEXT,
                    org_name        TEXT,
                    clinical_role   TEXT,
                    npi             TEXT,
                    credentials_json TEXT,
                    attestations_json TEXT,
                    created_at      TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    task_id         TEXT PRIMARY KEY,
                    specialty       TEXT NOT NULL DEFAULT 'general',
                    difficulty      TEXT NOT NULL DEFAULT 'medium',
                    capture_reasoning INTEGER NOT NULL DEFAULT 0,
                    source          TEXT NOT NULL DEFAULT 'lab_supplied',
                    prompt          TEXT NOT NULL,
                    candidate_answers_json TEXT NOT NULL DEFAULT '[]',
                    max_labels      INTEGER NOT NULL DEFAULT 1,
                    grounding_mode  TEXT NOT NULL DEFAULT 'optional',
                    buyer_request_id TEXT,
                    generation_json TEXT,
                    status          TEXT NOT NULL DEFAULT 'open',
                    created_by      TEXT,
                    created_at      TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_specialty ON tasks(specialty);
                CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
                CREATE INDEX IF NOT EXISTS idx_tasks_buyer_req ON tasks(buyer_request_id);

                CREATE TABLE IF NOT EXISTS submissions (
                    submission_id   TEXT PRIMARY KEY,
                    task_id         TEXT NOT NULL,
                    evaluator_id    TEXT NOT NULL,
                    verdict         TEXT,
                    chosen_id       TEXT,
                    rejected_id     TEXT,
                    confidence      TEXT,
                    time_spent_sec  INTEGER NOT NULL DEFAULT 0,
                    status          TEXT NOT NULL DEFAULT 'submitted',
                    dedupe_hash     TEXT,
                    grounded        INTEGER NOT NULL DEFAULT 0,
                    grounding_mode  TEXT NOT NULL DEFAULT 'optional',
                    caught_flaw     INTEGER,
                    payload_json    TEXT NOT NULL DEFAULT '{}',
                    validation_json TEXT,
                    critic_json     TEXT,
                    qa_json         TEXT,
                    qa_reason       TEXT,
                    agreement_score REAL,
                    annotator_json  TEXT,
                    created_at      TEXT NOT NULL,
                    updated_at      TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sub_task ON submissions(task_id);
                CREATE INDEX IF NOT EXISTS idx_sub_status ON submissions(status);
                CREATE INDEX IF NOT EXISTS idx_sub_evaluator ON submissions(evaluator_id);
                CREATE INDEX IF NOT EXISTS idx_sub_dedupe ON submissions(dedupe_hash);

                CREATE TABLE IF NOT EXISTS records (
                    record_id       TEXT PRIMARY KEY,
                    submission_id   TEXT NOT NULL,
                    task_id         TEXT NOT NULL,
                    type            TEXT NOT NULL,
                    specialty       TEXT,
                    status          TEXT NOT NULL DEFAULT 'submitted',
                    payload_json    TEXT NOT NULL,
                    export_id       TEXT,
                    created_at      TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_rec_submission ON records(submission_id);
                CREATE INDEX IF NOT EXISTS idx_rec_status ON records(status);
                CREATE INDEX IF NOT EXISTS idx_rec_type ON records(type);

                CREATE TABLE IF NOT EXISTS events (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type     TEXT NOT NULL,
                    entity_id       TEXT,
                    event_type      TEXT NOT NULL,
                    actor           TEXT,
                    occurred_at     TEXT NOT NULL,
                    payload_json    TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_events_entity ON events(entity_type, entity_id);
                CREATE INDEX IF NOT EXISTS idx_events_occurred ON events(occurred_at);

                CREATE TABLE IF NOT EXISTS exports (
                    export_id       TEXT PRIMARY KEY,
                    created_by      TEXT,
                    created_at      TEXT NOT NULL,
                    record_count    INTEGER NOT NULL DEFAULT 0,
                    filters_json    TEXT,
                    dir_path        TEXT,
                    manifest_json   TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_exports_created ON exports(created_at);

                CREATE TABLE IF NOT EXISTS buyers (
                    buyer_id        TEXT PRIMARY KEY,
                    name            TEXT NOT NULL,
                    contact         TEXT,
                    export_profile  TEXT NOT NULL DEFAULT 'default',
                    notes           TEXT,
                    created_at      TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS buyer_requests (
                    request_id      TEXT PRIMARY KEY,
                    buyer_id        TEXT NOT NULL,
                    status          TEXT NOT NULL DEFAULT 'draft',
                    source          TEXT NOT NULL DEFAULT 'internal_prompt_bank',
                    export_profile  TEXT NOT NULL DEFAULT 'default',
                    constraints_json TEXT NOT NULL DEFAULT '{}',
                    uploaded_json   TEXT NOT NULL DEFAULT '[]',
                    note            TEXT,
                    created_by      TEXT,
                    created_at      TEXT NOT NULL,
                    updated_at      TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_buyer_requests_buyer ON buyer_requests(buyer_id);
                CREATE INDEX IF NOT EXISTS idx_buyer_requests_status ON buyer_requests(status);

                -- Per-task inter-annotator agreement observation (opt §1.3). One
                -- row per double-labeled task; the aggregate Cohen's kappa is
                -- computed across these observations.
                CREATE TABLE IF NOT EXISTS agreement (
                    task_id         TEXT PRIMARY KEY,
                    specialty       TEXT,
                    sub_a           TEXT,
                    sub_b           TEXT,
                    verdict_a       TEXT,
                    verdict_b       TEXT,
                    tags_a_json     TEXT,
                    tags_b_json     TEXT,
                    jaccard_tags    REAL,
                    verdict_agree   INTEGER NOT NULL DEFAULT 0,
                    n_labels        INTEGER NOT NULL DEFAULT 0,
                    flagged         INTEGER NOT NULL DEFAULT 0,
                    created_at      TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agreement_specialty ON agreement(specialty);

                -- Seedmaker auto-generation jobs (PRD §9.2): one row per
                -- ``generate_tasks`` run for the admin dashboard + auditing.
                CREATE TABLE IF NOT EXISTS generation_jobs (
                    job_id          TEXT PRIMARY KEY,
                    specialty       TEXT NOT NULL,
                    requested_n     INTEGER NOT NULL DEFAULT 0,
                    accepted        INTEGER NOT NULL DEFAULT 0,
                    dropped_json    TEXT NOT NULL DEFAULT '{}',
                    params_json     TEXT NOT NULL DEFAULT '{}',
                    created_by      TEXT,
                    created_at      TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_genjobs_specialty ON generation_jobs(specialty);
                CREATE INDEX IF NOT EXISTS idx_genjobs_created ON generation_jobs(created_at);

                -- Contributor credential vault (Contributors view + tiered export).
                -- Keyed by the same hashed annotator id that stamps every record, so
                -- a dossier (Tier B) matches the exact shipped records (Tier A).
                --   ship_json   = Tier A attributes (buyer-facing; safe to ship)
                --   verify_blob = Tier B identifying credentials (the private vault;
                --                 Fernet-sealed when ASCLEPIUS_VAULT_KEY is set)
                CREATE TABLE IF NOT EXISTS contributor_credentials (
                    id_hashed            TEXT PRIMARY KEY,
                    user_id              TEXT,
                    organization         TEXT,
                    role_title           TEXT,
                    blurb                TEXT,
                    credentials_verified INTEGER NOT NULL DEFAULT 0,
                    ship_json            TEXT NOT NULL DEFAULT '{}',
                    verify_blob          TEXT,
                    verify_enc           INTEGER NOT NULL DEFAULT 0,
                    created_at           TEXT NOT NULL,
                    updated_at           TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_cred_org ON contributor_credentials(organization);
                CREATE INDEX IF NOT EXISTS idx_cred_user ON contributor_credentials(user_id);
                """
            )
        self._migrate()

    def _migrate(self) -> None:
        """Additive column migrations for existing ``asclepius.db`` files so the
        data-optimization fields land without dropping prior data."""
        with self._conn() as conn:
            def cols(table: str) -> set:
                return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}

            task_cols = cols("tasks")
            if "grounding_mode" not in task_cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN grounding_mode TEXT NOT NULL DEFAULT 'optional'")
            if "buyer_request_id" not in task_cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN buyer_request_id TEXT")
            if "generation_json" not in task_cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN generation_json TEXT")

            user_cols = cols("users")
            if "organization" not in user_cols:
                conn.execute("ALTER TABLE users ADD COLUMN organization TEXT")

            sub_cols = cols("submissions")
            if "grounded" not in sub_cols:
                conn.execute("ALTER TABLE submissions ADD COLUMN grounded INTEGER NOT NULL DEFAULT 0")
            if "grounding_mode" not in sub_cols:
                conn.execute("ALTER TABLE submissions ADD COLUMN grounding_mode TEXT NOT NULL DEFAULT 'optional'")
            if "caught_flaw" not in sub_cols:
                conn.execute("ALTER TABLE submissions ADD COLUMN caught_flaw INTEGER")

            # Rich credential record provisioned by the Asclepius onboarding flow.
            user_cols = cols("users")
            for col, decl in (
                ("full_name", "TEXT"),
                ("org_name", "TEXT"),
                ("clinical_role", "TEXT"),
                ("npi", "TEXT"),
                ("credentials_json", "TEXT"),
                ("attestations_json", "TEXT"),
            ):
                if col not in user_cols:
                    conn.execute(f"ALTER TABLE users ADD COLUMN {col} {decl}")

    # ─── Users ────────────────────────────────────────────────────────────────
    def create_user(
        self,
        *,
        email: str,
        password: str,
        role: str = "evaluator",
        specialty: Optional[str] = None,
        board_cert: Optional[str] = None,
        years_experience: Optional[int] = None,
        organization: Optional[str] = None,
    ) -> Dict[str, Any]:
        email = email.lower().strip()
        uid = _new_id("u")
        id_hashed = hashlib.sha256(uid.encode("utf-8")).hexdigest()[:16]
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO users (id, email, password_hash, role, specialty, board_cert,
                                   years_experience, organization, id_hashed, active, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    uid,
                    email,
                    hash_password(password),
                    role,
                    specialty,
                    board_cert,
                    years_experience,
                    organization,
                    id_hashed,
                    _utcnow_iso(),
                ),
            )
        return self.get_user_by_id(uid)  # type: ignore[return-value]

    def provision_user(
        self,
        *,
        email: str,
        password: str,
        role: str = "evaluator",
        full_name: Optional[str] = None,
        org_name: Optional[str] = None,
        clinical_role: Optional[str] = None,
        specialty: Optional[str] = None,
        board_cert: Optional[str] = None,
        npi: Optional[str] = None,
        years_experience: Optional[int] = None,
        credentials: Optional[Dict[str, Any]] = None,
        attestations: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Idempotent upsert used by the Asclepius onboarding flow.

        Creates the portal account (or updates it if the person re-onboards),
        carrying the full credential + attestation record collected during
        onboarding. ``password`` is the standing access key mailed to the user.
        """
        email = email.lower().strip()
        creds_json = json.dumps(credentials or {})
        atts_json = json.dumps(attestations or {})
        existing = self.get_user_by_email(email)
        with self._conn() as conn:
            if existing:
                conn.execute(
                    """
                    UPDATE users SET
                        password_hash = ?, role = ?, specialty = ?, board_cert = ?,
                        years_experience = ?, active = 1, full_name = ?, org_name = ?,
                        clinical_role = ?, npi = ?, credentials_json = ?, attestations_json = ?
                    WHERE email = ?
                    """,
                    (
                        hash_password(password), role, specialty, board_cert,
                        years_experience, full_name, org_name, clinical_role, npi,
                        creds_json, atts_json, email,
                    ),
                )
                return self.get_user_by_email(email)  # type: ignore[return-value]
            uid = _new_id("u")
            id_hashed = hashlib.sha256(uid.encode("utf-8")).hexdigest()[:16]
            conn.execute(
                """
                INSERT INTO users (id, email, password_hash, role, specialty, board_cert,
                                   years_experience, id_hashed, active, full_name, org_name,
                                   clinical_role, npi, credentials_json, attestations_json,
                                   created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uid, email, hash_password(password), role, specialty, board_cert,
                    years_experience, id_hashed, full_name, org_name, clinical_role,
                    npi, creds_json, atts_json, _utcnow_iso(),
                ),
            )
        return self.get_user_by_id(uid)  # type: ignore[return-value]

    def get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE email = ?", (email.lower().strip(),)
            ).fetchone()
            return dict(row) if row else None

    def get_user_by_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return dict(row) if row else None

    def list_users(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM users ORDER BY created_at ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    def count_users(self) -> int:
        with self._conn() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def annotator_block(self, user: Dict[str, Any]) -> Dict[str, Any]:
        """The credential block copied onto every emitted record (PRD §6.2)."""
        cred = user.get("board_cert") or (
            f"board_certified_{user.get('specialty')}" if user.get("specialty") else "unspecified"
        )
        return {
            "id_hashed": user.get("id_hashed") or "",
            "credentials": cred,
            "specialty": user.get("specialty"),
            "years_experience": user.get("years_experience"),
        }

    # ─── Tasks ──────────────────────────────────────────────────────────────--
    def insert_task(
        self,
        *,
        prompt: str,
        specialty: str = "general",
        difficulty: str = "medium",
        capture_reasoning: bool = False,
        source: str = "lab_supplied",
        candidate_answers: Optional[List[Dict[str, Any]]] = None,
        max_labels: int = 1,
        grounding_mode: str = "optional",
        buyer_request_id: Optional[str] = None,
        generation: Optional[Dict[str, Any]] = None,
        task_id: Optional[str] = None,
        created_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        tid = task_id or _new_id("t")
        gm = grounding_mode if grounding_mode in ("optional", "required") else "optional"
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO tasks
                  (task_id, specialty, difficulty, capture_reasoning, source, prompt,
                   candidate_answers_json, max_labels, grounding_mode, buyer_request_id,
                   generation_json, status, created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
                """,
                (
                    tid,
                    specialty,
                    difficulty,
                    1 if capture_reasoning else 0,
                    source,
                    prompt,
                    json.dumps(candidate_answers or []),
                    max(1, int(max_labels or 1)),
                    gm,
                    buyer_request_id,
                    json.dumps(generation) if generation else None,
                    created_by,
                    _utcnow_iso(),
                ),
            )
        return self.get_task(tid)  # type: ignore[return-value]

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if not row:
            return None
        return self._task_row(row)

    @staticmethod
    def _task_row(row: sqlite3.Row) -> Dict[str, Any]:
        rec = dict(row)
        rec["capture_reasoning"] = bool(rec.get("capture_reasoning"))
        rec["candidate_answers"] = json.loads(rec.pop("candidate_answers_json", "[]") or "[]")
        rec["generation"] = json.loads(rec.pop("generation_json", "null") or "null")
        return rec

    def list_tasks(self, *, specialty: Optional[str] = None, limit: int = 500) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if specialty:
            clauses.append("specialty = ?")
            params.append(specialty)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM tasks {where} ORDER BY created_at DESC LIMIT ?", tuple(params)
            ).fetchall()
        return [self._task_row(r) for r in rows]

    def submission_count_for_task(self, task_id: str) -> int:
        with self._conn() as conn:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM submissions WHERE task_id = ?", (task_id,)
                ).fetchone()[0]
            )

    def next_task_for_evaluator(
        self, *, evaluator_id: str, specialty: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        """Oldest open task in the evaluator's specialty that (a) they have not
        already submitted and (b) still has label capacity (max_labels).

        TODO(scale): this scans candidate open tasks in Python; fine at pod scale.
        Push the not-mine + capacity filter fully into SQL when volume grows."""
        clauses = ["t.status = 'open'"]
        # NOTE: the ``mine`` correlated subquery placeholder appears BEFORE the
        # WHERE clause in the SQL text, so ``evaluator_id`` must bind first.
        params: List[Any] = [evaluator_id]
        if specialty:
            clauses.append("t.specialty = ?")
            params.append(specialty)
        where = " AND ".join(clauses)
        with self._conn() as conn:
            row = conn.execute(
                f"""
                SELECT t.*,
                       (SELECT COUNT(*) FROM submissions s WHERE s.task_id = t.task_id) AS sub_count,
                       (SELECT COUNT(*) FROM submissions s2
                         WHERE s2.task_id = t.task_id AND s2.evaluator_id = ?) AS mine
                FROM tasks t
                WHERE {where}
                ORDER BY t.created_at ASC
                """,
                tuple(params),
            ).fetchall()
        for r in row:
            rec = self._task_row(r)
            if rec.get("mine"):
                continue
            if int(r["sub_count"]) >= int(rec.get("max_labels") or 1):
                continue
            rec.pop("sub_count", None)
            rec.pop("mine", None)
            return rec
        return None

    def mark_task_status(self, task_id: str, status: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE tasks SET status = ? WHERE task_id = ?", (status, task_id))

    def refresh_task_status(self, task_id: str) -> None:
        """Close a task once it has reached its label capacity."""
        task = self.get_task(task_id)
        if not task:
            return
        count = self.submission_count_for_task(task_id)
        new_status = "done" if count >= int(task.get("max_labels") or 1) else "open"
        self.mark_task_status(task_id, new_status)

    # ─── Submissions ──────────────────────────────────────────────────────────
    def get_submission(self, submission_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM submissions WHERE submission_id = ?", (submission_id,)
            ).fetchone()
        return self._submission_row(row) if row else None

    @staticmethod
    def _submission_row(row: sqlite3.Row) -> Dict[str, Any]:
        rec = dict(row)
        rec["payload"] = json.loads(rec.pop("payload_json", "{}") or "{}")
        rec["validation"] = json.loads(rec.pop("validation_json", "null") or "null")
        rec["critic"] = json.loads(rec.pop("critic_json", "null") or "null")
        rec["qa"] = json.loads(rec.pop("qa_json", "null") or "null")
        rec["annotator"] = json.loads(rec.pop("annotator_json", "null") or "null")
        return rec

    def insert_submission(
        self,
        *,
        submission_id: str,
        task_id: str,
        evaluator_id: str,
        verdict: Optional[str],
        chosen_id: Optional[str],
        rejected_id: Optional[str],
        confidence: Optional[str],
        time_spent_sec: int,
        payload: Dict[str, Any],
        annotator: Dict[str, Any],
        dedupe_hash: Optional[str],
        grounded: bool = False,
        grounding_mode: str = "optional",
        status: str = "submitted",
    ) -> Dict[str, Any]:
        now = _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO submissions
                  (submission_id, task_id, evaluator_id, verdict, chosen_id, rejected_id,
                   confidence, time_spent_sec, status, dedupe_hash, grounded, grounding_mode,
                   payload_json, annotator_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    submission_id,
                    task_id,
                    evaluator_id,
                    verdict,
                    chosen_id,
                    rejected_id,
                    confidence,
                    int(time_spent_sec or 0),
                    status,
                    dedupe_hash,
                    1 if grounded else 0,
                    grounding_mode,
                    json.dumps(payload),
                    json.dumps(annotator),
                    now,
                    now,
                ),
            )
        return self.get_submission(submission_id)  # type: ignore[return-value]

    def update_submission(self, submission_id: str, **fields: Any) -> None:
        if not fields:
            return
        json_cols = {"validation", "critic", "qa"}
        sets, params = [], []
        for key, value in fields.items():
            if key in json_cols:
                sets.append(f"{key}_json = ?")
                params.append(json.dumps(value) if value is not None else None)
            else:
                sets.append(f"{key} = ?")
                params.append(value)
        sets.append("updated_at = ?")
        params.append(_utcnow_iso())
        params.append(submission_id)
        with self._conn() as conn:
            conn.execute(
                f"UPDATE submissions SET {', '.join(sets)} WHERE submission_id = ?",
                tuple(params),
            )

    def list_submissions(
        self,
        *,
        status: Optional[str] = None,
        specialty: Optional[str] = None,
        evaluator_id: Optional[str] = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if status:
            clauses.append("s.status = ?")
            params.append(status)
        if evaluator_id:
            clauses.append("s.evaluator_id = ?")
            params.append(evaluator_id)
        if specialty:
            clauses.append("t.specialty = ?")
            params.append(specialty)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT s.* FROM submissions s
                JOIN tasks t ON t.task_id = s.task_id
                {where}
                ORDER BY s.created_at DESC LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [self._submission_row(r) for r in rows]

    def submissions_for_task(self, task_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM submissions WHERE task_id = ? ORDER BY created_at ASC",
                (task_id,),
            ).fetchall()
        return [self._submission_row(r) for r in rows]

    # ─── Records ──────────────────────────────────────────────────────────────
    def insert_record(
        self,
        *,
        submission_id: str,
        task_id: str,
        rtype: str,
        specialty: Optional[str],
        payload: Dict[str, Any],
        status: str = "submitted",
    ) -> str:
        rid = _new_id("rec")
        payload = dict(payload)
        payload.setdefault("record_id", rid)
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO records
                  (record_id, submission_id, task_id, type, specialty, status, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rid,
                    submission_id,
                    task_id,
                    rtype,
                    specialty,
                    status,
                    json.dumps(payload),
                    _utcnow_iso(),
                ),
            )
        return rid

    @staticmethod
    def _record_row(row: sqlite3.Row) -> Dict[str, Any]:
        rec = dict(row)
        rec["payload"] = json.loads(rec.pop("payload_json", "{}") or "{}")
        return rec

    def records_for_submission(self, submission_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM records WHERE submission_id = ? ORDER BY created_at ASC",
                (submission_id,),
            ).fetchall()
        return [self._record_row(r) for r in rows]

    def update_records_status_for_submission(self, submission_id: str, status: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE records SET status = ? WHERE submission_id = ?",
                (status, submission_id),
            )

    def patch_record_payload(self, record_id: str, patch: Dict[str, Any]) -> None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT payload_json FROM records WHERE record_id = ?", (record_id,)
            ).fetchone()
            if not row:
                return
            payload = json.loads(row["payload_json"] or "{}")
            payload.update(patch)
            conn.execute(
                "UPDATE records SET payload_json = ? WHERE record_id = ?",
                (json.dumps(payload), record_id),
            )

    def list_records(
        self,
        *,
        status: Optional[str] = None,
        rtype: Optional[str] = None,
        specialty: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = 100000,
    ) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if rtype:
            clauses.append("type = ?")
            params.append(rtype)
        if specialty:
            clauses.append("specialty = ?")
            params.append(specialty)
        if since:
            clauses.append("created_at >= ?")
            params.append(since)
        if until:
            clauses.append("created_at <= ?")
            params.append(until)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM records {where} ORDER BY created_at ASC LIMIT ?", tuple(params)
            ).fetchall()
        return [self._record_row(r) for r in rows]

    def mark_records_exported(self, record_ids: List[str], export_id: str) -> None:
        if not record_ids:
            return
        with self._conn() as conn:
            conn.executemany(
                "UPDATE records SET status = 'exported', export_id = ? WHERE record_id = ?",
                [(export_id, rid) for rid in record_ids],
            )

    # ─── Events (provenance) ────────────────────────────────────────────────--
    def log_event(
        self,
        *,
        entity_type: str,
        event_type: str,
        entity_id: Optional[str] = None,
        actor: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
        occurred_at: Optional[str] = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO events (entity_type, entity_id, event_type, actor, occurred_at, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    entity_type,
                    entity_id,
                    event_type,
                    actor,
                    occurred_at or _utcnow_iso(),
                    json.dumps(payload or {}),
                ),
            )

    def list_events(
        self,
        *,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if entity_type:
            clauses.append("entity_type = ?")
            params.append(entity_type)
        if entity_id:
            clauses.append("entity_id = ?")
            params.append(entity_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM events {where} ORDER BY id DESC LIMIT ?", tuple(params)
            ).fetchall()
        out = []
        for r in rows:
            rec = dict(r)
            rec["payload"] = json.loads(rec.pop("payload_json", "{}") or "{}")
            out.append(rec)
        return out

    # ─── Exports ────────────────────────────────────────────────────────────--
    def insert_export(
        self,
        *,
        export_id: str,
        created_by: Optional[str],
        record_count: int,
        filters: Dict[str, Any],
        dir_path: str,
        manifest: Dict[str, Any],
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO exports
                  (export_id, created_by, created_at, record_count, filters_json, dir_path, manifest_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    export_id,
                    created_by,
                    _utcnow_iso(),
                    record_count,
                    json.dumps(filters),
                    dir_path,
                    json.dumps(manifest),
                ),
            )

    def get_export(self, export_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM exports WHERE export_id = ?", (export_id,)
            ).fetchone()
        if not row:
            return None
        rec = dict(row)
        rec["filters"] = json.loads(rec.pop("filters_json", "{}") or "{}")
        rec["manifest"] = json.loads(rec.pop("manifest_json", "{}") or "{}")
        return rec

    def list_exports(self, *, limit: int = 200) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM exports ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for r in rows:
            rec = dict(r)
            rec["filters"] = json.loads(rec.pop("filters_json", "{}") or "{}")
            rec["manifest"] = json.loads(rec.pop("manifest_json", "{}") or "{}")
            out.append(rec)
        return out

    # ─── Stats (admin dashboard, PRD §7.6) ────────────────────────────────────
    def status_counts(self) -> Dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM submissions GROUP BY status"
            ).fetchall()
        return {r["status"]: int(r["n"]) for r in rows}

    def evaluator_throughput(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT s.evaluator_id,
                       u.email,
                       u.specialty,
                       COUNT(*) AS submissions,
                       AVG(s.time_spent_sec) AS avg_time_sec,
                       SUM(CASE WHEN s.status IN ('export_ready','exported') THEN 1 ELSE 0 END) AS export_ready
                FROM submissions s
                LEFT JOIN users u ON u.id = s.evaluator_id
                GROUP BY s.evaluator_id
                ORDER BY submissions DESC
                """
            ).fetchall()
        out = []
        for r in rows:
            rec = dict(r)
            rec["avg_time_sec"] = round(rec["avg_time_sec"], 1) if rec["avg_time_sec"] is not None else None
            out.append(rec)
        return out

    def qa_pass_rate(self) -> Dict[str, Any]:
        counts = self.status_counts()
        reviewed = counts.get("export_ready", 0) + counts.get("exported", 0) + counts.get("rejected", 0)
        passed = counts.get("export_ready", 0) + counts.get("exported", 0)
        rate = round(passed / reviewed, 3) if reviewed else None
        return {"reviewed": reviewed, "passed": passed, "pass_rate": rate}

    def average_agreement(self) -> Optional[float]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT AVG(agreement_score) AS a FROM submissions WHERE agreement_score IS NOT NULL"
            ).fetchone()
        return round(row["a"], 3) if row and row["a"] is not None else None

    def grounded_counts(self) -> Dict[str, int]:
        """Grounded vs total submissions + records (opt §1.2 premium tier)."""
        with self._conn() as conn:
            sub_total = int(conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0])
            sub_grounded = int(
                conn.execute("SELECT COUNT(*) FROM submissions WHERE grounded = 1").fetchone()[0]
            )
        return {"submissions_total": sub_total, "submissions_grounded": sub_grounded}

    def contributor_stats(self) -> List[Dict[str, Any]]:
        """Per-evaluator credential mix, hours, counts, and PREMIUM (grounded /
        grounding_mode=required) completion tracked separately (opt §1.2, §1.4)."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT s.evaluator_id,
                       u.email,
                       u.specialty,
                       u.board_cert,
                       u.years_experience,
                       COUNT(*)                                  AS submissions,
                       SUM(s.time_spent_sec)                     AS total_time_sec,
                       AVG(s.time_spent_sec)                     AS avg_time_sec,
                       SUM(CASE WHEN s.grounding_mode = 'required' THEN 1 ELSE 0 END) AS premium_submissions,
                       SUM(CASE WHEN s.grounding_mode = 'required' THEN s.time_spent_sec ELSE 0 END) AS premium_time_sec,
                       SUM(CASE WHEN s.grounded = 1 THEN 1 ELSE 0 END) AS grounded_submissions
                FROM submissions s
                LEFT JOIN users u ON u.id = s.evaluator_id
                GROUP BY s.evaluator_id
                ORDER BY submissions DESC
                """
            ).fetchall()
        out = []
        for r in rows:
            rec = dict(r)
            rec["avg_time_sec"] = round(rec["avg_time_sec"], 1) if rec["avg_time_sec"] is not None else None
            rec["total_hours"] = round((rec.get("total_time_sec") or 0) / 3600.0, 2)
            rec["premium_hours"] = round((rec.get("premium_time_sec") or 0) / 3600.0, 2)
            credential = rec.get("board_cert") or (
                f"board_certified_{rec.get('specialty')}" if rec.get("specialty") else "unspecified"
            )
            rec["credential"] = credential
            out.append(rec)
        return out

    # ─── Buyers & buyer requests (opt §2.5) ──────────────────────────────────
    def create_buyer(
        self, *, name: str, contact: Optional[str] = None,
        export_profile: str = "default", notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        bid = _new_id("buyer")
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO buyers (buyer_id, name, contact, export_profile, notes, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (bid, name, contact, export_profile or "default", notes, _utcnow_iso()),
            )
        return self.get_buyer(bid)  # type: ignore[return-value]

    def get_buyer(self, buyer_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM buyers WHERE buyer_id = ?", (buyer_id,)).fetchone()
        return dict(row) if row else None

    def list_buyers(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM buyers ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    def create_buyer_request(
        self, *, buyer_id: str, source: str, export_profile: str,
        constraints: Dict[str, Any], uploaded: List[Dict[str, Any]],
        note: Optional[str] = None, created_by: Optional[str] = None,
        status: str = "draft",
    ) -> Dict[str, Any]:
        rid = _new_id("req")
        now = _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO buyer_requests
                  (request_id, buyer_id, status, source, export_profile, constraints_json,
                   uploaded_json, note, created_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rid, buyer_id, status, source, export_profile or "default",
                    json.dumps(constraints or {}), json.dumps(uploaded or []),
                    note, created_by, now, now,
                ),
            )
        return self.get_buyer_request(rid)  # type: ignore[return-value]

    @staticmethod
    def _buyer_request_row(row: sqlite3.Row) -> Dict[str, Any]:
        rec = dict(row)
        rec["constraints"] = json.loads(rec.pop("constraints_json", "{}") or "{}")
        rec["uploaded"] = json.loads(rec.pop("uploaded_json", "[]") or "[]")
        return rec

    def get_buyer_request(self, request_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM buyer_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
        return self._buyer_request_row(row) if row else None

    def list_buyer_requests(self, *, buyer_id: Optional[str] = None) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if buyer_id:
            clauses.append("buyer_id = ?")
            params.append(buyer_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM buyer_requests {where} ORDER BY created_at DESC", tuple(params)
            ).fetchall()
        return [self._buyer_request_row(r) for r in rows]

    def update_buyer_request_status(self, request_id: str, status: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE buyer_requests SET status = ?, updated_at = ? WHERE request_id = ?",
                (status, _utcnow_iso(), request_id),
            )

    # ─── Contributor credentials + organizations (tiered export) ─────────────
    def upsert_contributor_credentials(
        self,
        *,
        id_hashed: str,
        user_id: Optional[str] = None,
        organization: Optional[str] = None,
        role_title: Optional[str] = None,
        blurb: Optional[str] = None,
        credentials_verified: bool = False,
        ship: Optional[Dict[str, Any]] = None,
        verify: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create/replace a contributor's credential profile. ``ship`` is the Tier
        A (buyer-facing) attribute block; ``verify`` is the Tier B private vault
        (sealed at rest)."""
        now = _utcnow_iso()
        verify_blob, verify_enc = seal_vault(verify or {})
        existing = self.get_contributor_credentials(id_hashed)
        created_at = existing["created_at"] if existing else now
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO contributor_credentials
                  (id_hashed, user_id, organization, role_title, blurb,
                   credentials_verified, ship_json, verify_blob, verify_enc,
                   created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    id_hashed,
                    user_id,
                    organization,
                    role_title,
                    blurb,
                    1 if credentials_verified else 0,
                    json.dumps(ship or {}, ensure_ascii=False),
                    verify_blob,
                    verify_enc,
                    created_at,
                    now,
                ),
            )
        return self.get_contributor_credentials(id_hashed, include_verify=True)  # type: ignore[return-value]

    def get_contributor_credentials(
        self, id_hashed: str, *, include_verify: bool = False
    ) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM contributor_credentials WHERE id_hashed = ?", (id_hashed,)
            ).fetchone()
        if not row:
            return None
        rec = dict(row)
        rec["credentials_verified"] = bool(rec.get("credentials_verified"))
        rec["ship"] = json.loads(rec.pop("ship_json", "{}") or "{}")
        verify_blob = rec.pop("verify_blob", None)
        verify_enc = rec.pop("verify_enc", 0)
        rec["verify_encrypted"] = bool(verify_enc)
        if include_verify:
            rec["verify"] = open_vault(verify_blob, verify_enc)
        return rec

    def list_contributor_credentials(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id_hashed FROM contributor_credentials ORDER BY updated_at DESC"
            ).fetchall()
        return [self.get_contributor_credentials(r["id_hashed"]) for r in rows if r]  # type: ignore[misc]

    def _record_counts_by_evaluator(self) -> Dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT s.evaluator_id AS eid, COUNT(*) AS n
                FROM records r
                JOIN submissions s ON s.submission_id = r.submission_id
                GROUP BY s.evaluator_id
                """
            ).fetchall()
        return {r["eid"]: int(r["n"]) for r in rows}

    def contributor_directory(self) -> List[Dict[str, Any]]:
        """One row per contributor (a user who has labeled OR has a credential
        profile): internal display name, hashed id, organization, role, primary
        specialty, # records labeled, verified status, and last-labeled time."""
        rec_counts = self._record_counts_by_evaluator()
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT u.id, u.id_hashed, u.email, u.role, u.specialty,
                       u.organization AS user_org,
                       COUNT(DISTINCT s.submission_id) AS submission_count,
                       MAX(s.created_at) AS last_labeled_at
                FROM users u
                LEFT JOIN submissions s ON s.evaluator_id = u.id
                GROUP BY u.id
                ORDER BY u.created_at ASC
                """
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            cred = self.get_contributor_credentials(r["id_hashed"]) if r["id_hashed"] else None
            submission_count = int(r["submission_count"] or 0)
            has_cred = cred is not None
            # Skip accounts that have neither labeled nor been credentialed (e.g.
            # the bootstrap admin) — they are not "contributors".
            if submission_count == 0 and not has_cred:
                continue
            ship = (cred or {}).get("ship") or {}
            organization = (
                (cred or {}).get("organization")
                or r["user_org"]
                or "Unaffiliated"
            )
            primary_specialty = ship.get("primary_specialty") or r["specialty"]
            out.append(
                {
                    "user_id": r["id"],
                    "id_hashed": r["id_hashed"],
                    "display_name": r["email"],   # internal-only display label
                    "email": r["email"],
                    "role": r["role"],
                    "organization": organization,
                    "role_title": (cred or {}).get("role_title"),
                    "primary_specialty": primary_specialty,
                    "specialty": r["specialty"],
                    "degree": ship.get("degree"),
                    "credentials_verified": bool((cred or {}).get("credentials_verified")),
                    "has_credentials": has_cred,
                    "record_count": int(rec_counts.get(r["id"], 0)),
                    "submission_count": submission_count,
                    "last_labeled_at": r["last_labeled_at"],
                }
            )
        return out

    def get_contributor(self, id_hashed: str) -> Optional[Dict[str, Any]]:
        for c in self.contributor_directory():
            if c["id_hashed"] == id_hashed:
                return c
        return None

    def organization_directory(self) -> List[Dict[str, Any]]:
        """Aggregate the contributor directory by organization for the top-level
        Contributors view (list by org, click in)."""
        orgs: Dict[str, Dict[str, Any]] = {}
        for c in self.contributor_directory():
            org = c["organization"] or "Unaffiliated"
            agg = orgs.setdefault(
                org,
                {
                    "organization": org,
                    "contributor_count": 0,
                    "verified_count": 0,
                    "record_count": 0,
                    "submission_count": 0,
                    "last_labeled_at": None,
                },
            )
            agg["contributor_count"] += 1
            agg["verified_count"] += 1 if c["credentials_verified"] else 0
            agg["record_count"] += c["record_count"]
            agg["submission_count"] += c["submission_count"]
            ll = c.get("last_labeled_at")
            if ll and (agg["last_labeled_at"] is None or ll > agg["last_labeled_at"]):
                agg["last_labeled_at"] = ll
        return sorted(orgs.values(), key=lambda o: o["organization"].lower())

    def hashed_ids_for_organization(self, organization: str) -> List[str]:
        return [
            c["id_hashed"]
            for c in self.contributor_directory()
            if (c["organization"] or "Unaffiliated") == organization and c["id_hashed"]
        ]

    # ─── Inter-annotator agreement observations (opt §1.3) ───────────────────
    def upsert_agreement(
        self, *, task_id: str, specialty: Optional[str], sub_a: str, sub_b: str,
        verdict_a: Optional[str], verdict_b: Optional[str],
        tags_a: List[str], tags_b: List[str], jaccard_tags: float,
        verdict_agree: bool, n_labels: int, flagged: bool,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO agreement
                  (task_id, specialty, sub_a, sub_b, verdict_a, verdict_b, tags_a_json,
                   tags_b_json, jaccard_tags, verdict_agree, n_labels, flagged, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id, specialty, sub_a, sub_b, verdict_a, verdict_b,
                    json.dumps(tags_a or []), json.dumps(tags_b or []),
                    jaccard_tags, 1 if verdict_agree else 0, int(n_labels),
                    1 if flagged else 0, _utcnow_iso(),
                ),
            )

    def list_agreement_observations(self, *, specialty: Optional[str] = None) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if specialty:
            clauses.append("specialty = ?")
            params.append(specialty)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM agreement {where} ORDER BY created_at ASC", tuple(params)
            ).fetchall()
        out = []
        for r in rows:
            rec = dict(r)
            rec["tags_a"] = json.loads(rec.pop("tags_a_json", "[]") or "[]")
            rec["tags_b"] = json.loads(rec.pop("tags_b_json", "[]") or "[]")
            out.append(rec)
        return out

    # ─── Generation jobs (Seedmaker, PRD §9.2) ───────────────────────────────
    def insert_generation_job(
        self, *, specialty: str, requested_n: int, accepted: int,
        dropped_by_reason: Dict[str, int], params: Dict[str, Any],
        created_by: Optional[str] = None,
    ) -> str:
        job_id = _new_id("genjob")
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO generation_jobs
                  (job_id, specialty, requested_n, accepted, dropped_json, params_json,
                   created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id, specialty, int(requested_n or 0), int(accepted or 0),
                    json.dumps(dropped_by_reason or {}), json.dumps(params or {}),
                    created_by, _utcnow_iso(),
                ),
            )
        return job_id

    @staticmethod
    def _generation_job_row(row: sqlite3.Row) -> Dict[str, Any]:
        rec = dict(row)
        rec["dropped"] = json.loads(rec.pop("dropped_json", "{}") or "{}")
        rec["params"] = json.loads(rec.pop("params_json", "{}") or "{}")
        return rec

    def get_generation_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM generation_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return self._generation_job_row(row) if row else None

    def list_generation_jobs(
        self, *, specialty: Optional[str] = None, limit: int = 200
    ) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if specialty:
            clauses.append("specialty = ?")
            params.append(specialty)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM generation_jobs {where} ORDER BY created_at DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [self._generation_job_row(r) for r in rows]

    def flaw_catch_rate(self) -> Dict[str, Any]:
        """How often evaluators reject the intended-flawed candidate on generated
        tasks (PRD §16). Only counts graded A/B submissions where the task carried
        an ``intended_flawed_id`` (caught_flaw IS NOT NULL)."""
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS scored, SUM(caught_flaw) AS caught
                FROM submissions WHERE caught_flaw IS NOT NULL
                """
            ).fetchone()
        scored = int(row["scored"] or 0)
        caught = int(row["caught"] or 0)
        rate = round(caught / scored, 3) if scored else None
        return {"scored": scored, "caught": caught, "rate": rate}


# ─── Process-wide singleton ───────────────────────────────────────────────────
_STORE: Optional[AsclepiusStore] = None


def get_store() -> AsclepiusStore:
    global _STORE
    if _STORE is None:
        _STORE = AsclepiusStore()
    return _STORE


def reset_store_for_tests(db_path: Optional[str] = None) -> AsclepiusStore:
    """Rebuild the singleton against a fresh DB path (test helper only)."""
    global _STORE
    _STORE = AsclepiusStore(db_path=db_path)
    return _STORE
