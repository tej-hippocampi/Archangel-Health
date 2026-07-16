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
                    independent_mode TEXT NOT NULL DEFAULT 'stance',
                    buyer_request_id TEXT,
                    generation_json TEXT,
                    value_tier      TEXT,
                    modality        TEXT NOT NULL DEFAULT 'text',
                    case_json       TEXT,
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
                    portal_version  TEXT NOT NULL DEFAULT 'v2',
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

                -- Independent-answer reveal gate (Eval Flow Upgrade §1, v2 anti-
                -- peeking). One row per (task, evaluator) proving the evaluator
                -- committed their blind independent answer BEFORE any candidate
                -- answer text was revealed. The reveal endpoints refuse to return
                -- answer text without this row, and the committed answer is the
                -- authoritative one packaged — so it is provably pre-reveal.
                CREATE TABLE IF NOT EXISTS independent_commits (
                    task_id       TEXT NOT NULL,
                    evaluator_id  TEXT NOT NULL,
                    payload_json  TEXT NOT NULL DEFAULT '{}',
                    created_at    TEXT NOT NULL,
                    PRIMARY KEY (task_id, evaluator_id)
                );
                """
            )
        self._migrate()

    def _migrate(self) -> None:
        """Additive column migrations for existing ``asclepius.db`` files so the
        data-optimization fields land without dropping prior data."""
        with self._conn() as conn:
            def cols(table: str) -> set:
                return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}

            # ── Real EHR ingestion (EHR PRD §4, §5, §8) — new tables (idempotent).
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ingest_upload_links (
                    link_id       TEXT PRIMARY KEY,
                    token_hash    TEXT NOT NULL UNIQUE,   -- SHA-256; raw token never stored
                    partner_id    TEXT NOT NULL,
                    partner_label TEXT,
                    specialty     TEXT NOT NULL DEFAULT 'nephrology',
                    expires_at    TEXT NOT NULL,
                    one_time      INTEGER NOT NULL DEFAULT 1,
                    max_bytes     INTEGER NOT NULL DEFAULT 104857600,
                    used_count    INTEGER NOT NULL DEFAULT 0,
                    revoked       INTEGER NOT NULL DEFAULT 0,
                    created_by    TEXT,
                    created_at    TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                -- Data-provider ACCOUNTS (email + password door, EHR PRD §4 —
                -- complementary to the magic-link door). The account itself lives
                -- in ``users`` (role='data_partner'); this row carries the invite /
                -- upload lifecycle + relationship metadata the admin sees. Uploads
                -- still flow through the shared ingest_uploads pipeline (partner_id
                -- = this provider_id), so there is ONE inbox for both doors.
                CREATE TABLE IF NOT EXISTS data_providers (
                    provider_id         TEXT PRIMARY KEY,   -- = users.id
                    email               TEXT NOT NULL UNIQUE,
                    org_name            TEXT,
                    specialty           TEXT,
                    note                TEXT,
                    status              TEXT NOT NULL DEFAULT 'invited', -- invited|active|revoked
                    must_reset_password INTEGER NOT NULL DEFAULT 1,
                    invited_by          TEXT,
                    invited_at          TEXT,
                    invite_expires_at   TEXT,
                    created_at          TEXT NOT NULL,
                    updated_at          TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                -- Buyer ACCOUNTS for the secure data workspace. The account itself
                -- lives in ``users`` (role='buyer'); this row carries the invite /
                -- workspace lifecycle metadata the admin sees. Data delivered to a
                -- buyer is recorded in ``buyer_deliveries`` and always appears in
                -- their workspace when they sign in.
                CREATE TABLE IF NOT EXISTS buyer_accounts (
                    buyer_account_id    TEXT PRIMARY KEY,   -- = users.id
                    email               TEXT NOT NULL UNIQUE,
                    buyer_name          TEXT,
                    note                TEXT,
                    status              TEXT NOT NULL DEFAULT 'invited', -- invited|active|revoked
                    must_reset_password INTEGER NOT NULL DEFAULT 1,
                    invited_by          TEXT,
                    invited_at          TEXT,
                    invite_expires_at   TEXT,
                    created_at          TEXT NOT NULL,
                    updated_at          TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                -- One row per dataset delivered to a buyer. Joins a built export
                -- (exports.export_id) to a buyer account, so "data sent to that
                -- email always appears in their workspace" falls out of a lookup.
                CREATE TABLE IF NOT EXISTS buyer_deliveries (
                    delivery_id       TEXT PRIMARY KEY,
                    buyer_account_id  TEXT NOT NULL,
                    buyer_email       TEXT NOT NULL,
                    export_id         TEXT NOT NULL,
                    label             TEXT,
                    data_format       TEXT,
                    record_count      INTEGER NOT NULL DEFAULT 0,
                    note              TEXT,
                    sent_by           TEXT,
                    sent_at           TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_buyer_deliveries_acct ON buyer_deliveries(buyer_account_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_buyer_deliveries_email ON buyer_deliveries(buyer_email)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ingest_uploads (
                    upload_id   TEXT PRIMARY KEY,
                    link_id     TEXT NOT NULL,
                    partner_id  TEXT NOT NULL,
                    filename    TEXT,
                    sha256      TEXT,
                    size_bytes  INTEGER,
                    status      TEXT NOT NULL DEFAULT 'received',
                    reason      TEXT,
                    files_json  TEXT,           -- per-entry classification/outcome
                    raw_path    TEXT,           -- encrypted quarantine blob on disk
                    source_ip   TEXT,
                    created_at  TEXT NOT NULL,
                    updated_at  TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ingest_cases (
                    ingest_case_id TEXT PRIMARY KEY,
                    upload_id      TEXT NOT NULL,
                    patient_key    TEXT,
                    specialty      TEXT,
                    case_json      TEXT,
                    status         TEXT NOT NULL DEFAULT 'ingested',
                    report_json    TEXT,        -- timeline + verify findings (masked)
                    override_reason TEXT,
                    task_id        TEXT,        -- set on promote
                    created_at     TEXT NOT NULL,
                    updated_at     TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ingest_cases_upload ON ingest_cases(upload_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ingest_cases_status ON ingest_cases(status)")

            # ── Frontier-model failure capture (FEAT-1) ──────────────────────
            # ``baseline_runs``: a frontier model's VERBATIM cold answer to a case,
            # the on-policy artifact that proves a case is hard.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS baseline_runs (
                    run_id        TEXT PRIMARY KEY,
                    task_id       TEXT NOT NULL,
                    model         TEXT NOT NULL,
                    response_text TEXT,
                    error         TEXT,
                    latency_ms    INTEGER,
                    tokens_in     INTEGER,
                    tokens_out    INTEGER,
                    created_at    TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_baseline_runs_task ON baseline_runs(task_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_baseline_runs_model ON baseline_runs(model)")
            # ``model_failures``: the per-model failure record computed AFTER a
            # specialist grades a real-model A/B pair — which model was rejected,
            # which error tags applied, which steps were wrong, + the correction.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS model_failures (
                    failure_id      TEXT PRIMARY KEY,
                    task_id         TEXT NOT NULL,
                    submission_id   TEXT NOT NULL,
                    model           TEXT NOT NULL,
                    verdict         TEXT,
                    error_tags_json TEXT NOT NULL DEFAULT '[]',
                    corrected_steps_json TEXT NOT NULL DEFAULT '[]',
                    expert_correction    TEXT,
                    prompt          TEXT,
                    created_at      TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_model_failures_model ON model_failures(model)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_model_failures_task ON model_failures(task_id)")

            task_cols = cols("tasks")
            if "grounding_mode" not in task_cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN grounding_mode TEXT NOT NULL DEFAULT 'optional'")
            if "buyer_request_id" not in task_cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN buyer_request_id TEXT")
            if "generation_json" not in task_cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN generation_json TEXT")
            if "independent_mode" not in task_cols:
                # Speed Optimization §1: ``independent_mode`` is the ADMIN's
                # per-task intent — 'stance' (quick take, the default) or 'full'
                # (long-form blind ideal, premium/eval batches). Pre-existing
                # rows default to 'stance' BY DESIGN: the product requirement is
                # that legacy tasks read as stance in V2. This is not a silent
                # data loss — a premium blind ideal answer is still produced
                # whenever the contributor selects the V1 (classic) experience
                # (``_independent_kind`` forces 'full' for v1) OR the admin marks
                # the task ``independent_mode='full'`` (honored in V2). Only the
                # DEFAULT capture on an unmarked task in the DEFAULT (v2)
                # experience is the quick stance.
                conn.execute("ALTER TABLE tasks ADD COLUMN independent_mode TEXT NOT NULL DEFAULT 'stance'")

            sub_cols = cols("submissions")
            if "portal_version" not in sub_cols:
                # Asclepius V2 launch: which evaluator flow produced the row
                # (v1 classic | v2 assisted). Rows written before this column
                # existed were all the classic flow, so backfill them to 'v1'.
                conn.execute("ALTER TABLE submissions ADD COLUMN portal_version TEXT NOT NULL DEFAULT 'v2'")
                conn.execute("UPDATE submissions SET portal_version = 'v1'")

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

            # Value-per-Minute (PRD Part A): the estimated sellable value of a
            # judgment + the clinician-minutes it took, persisted per submission
            # so V/T is reported next to κ. Purely additive measurement columns —
            # no existing flow (v1 or v2) reads them; NULL on legacy rows means
            # "not yet estimated" and the metrics endpoint skips them.
            if "value_estimate_usd" not in sub_cols:
                conn.execute("ALTER TABLE submissions ADD COLUMN value_estimate_usd REAL")
            if "value_estimate_projected_usd" not in sub_cols:
                conn.execute("ALTER TABLE submissions ADD COLUMN value_estimate_projected_usd REAL")
            if "clinician_review_seconds" not in sub_cols:
                conn.execute("ALTER TABLE submissions ADD COLUMN clinician_review_seconds INTEGER")
            if "progress_json" not in sub_cols:
                # Real submit progress (BUG-5): the backend stamps {phase, pct,
                # detail} onto the row as each pipeline stage ACTUALLY starts, so
                # the client polls a truthful phase — never an invented percentage.
                conn.execute("ALTER TABLE submissions ADD COLUMN progress_json TEXT")

            if "value_tier" not in task_cols:
                # Optional admin routing hint (Value-per-Minute PRD B3). Additive;
                # NULL means "unspecified" and routing scores from attributes.
                conn.execute("ALTER TABLE tasks ADD COLUMN value_tier TEXT")
            if "modality" not in task_cols:
                # Multimodal clinical cases (Synthetic Multimodal Cases PRD). Additive;
                # 'text' (default) is today's one-line prompt, 'multimodal' carries a case.
                conn.execute("ALTER TABLE tasks ADD COLUMN modality TEXT NOT NULL DEFAULT 'text'")
            if "case_json" not in task_cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN case_json TEXT")
            if "case_source" not in task_cols:
                # Real EHR Ingestion PRD §9.5: 'synthetic' | 'real_deid' as a first-
                # class COLUMN so the V4 routing wall filters in SQL (a real case is
                # only ever served to a v4 session). NULL = text task (no case).
                # Backfill existing multimodal rows from their stored case.
                conn.execute("ALTER TABLE tasks ADD COLUMN case_source TEXT")
                for r in conn.execute(
                    "SELECT task_id, case_json FROM tasks WHERE case_json IS NOT NULL"
                ).fetchall():
                    try:
                        cs = (json.loads(r["case_json"]) or {}).get("case_source") or "synthetic"
                    except Exception:
                        cs = "synthetic"
                    conn.execute("UPDATE tasks SET case_source = ? WHERE task_id = ?", (cs, r["task_id"]))

            # Rich credential record provisioned by the Asclepius onboarding flow.
            user_cols = cols("users")
            for col, decl in (
                ("full_name", "TEXT"),
                ("org_name", "TEXT"),
                ("clinical_role", "TEXT"),
                ("npi", "TEXT"),
                ("credentials_json", "TEXT"),
                ("attestations_json", "TEXT"),
                # Mock/sandbox contributor (internal demo tool): submissions are
                # HARD-EXCLUDED from real exports by default so a demo can exercise
                # the live portal without contaminating a shipped training batch.
                ("is_mock", "INTEGER NOT NULL DEFAULT 0"),
                # Real-data access gate (EHR PRD §9.5): V4 (real de-identified
                # cases) is served ONLY to contributors flagged approved (BAA /
                # training complete). Default off for everyone.
                ("real_data_approved", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if col not in user_cols:
                    conn.execute(f"ALTER TABLE users ADD COLUMN {col} {decl}")

            # ── Organization backfill (BUG-6) ────────────────────────────────
            # A contributor whose ``organization`` is NULL fell out of EVERY org
            # grouping in Exports/Metrics — their labeled records existed but
            # appeared nowhere (the worst admin failure mode). Backfill a stable
            # org for every existing contributor so their historical submissions
            # (resolved via this users row at read time) group correctly:
            #   * the mock/demo account collapses to 'mockadmin';
            #   * else the onboarding-collected org_name;
            #   * else the account email's local-part (a stable, non-null bucket).
            # Idempotent: only touches NULL/blank organizations, so it no-ops on
            # every boot after the first.
            conn.execute(
                "UPDATE users SET organization = 'mockadmin' "
                "WHERE is_mock = 1 AND (organization IS NULL OR TRIM(organization) = '')"
            )
            conn.execute(
                """
                UPDATE users SET organization = CASE
                    WHEN org_name IS NOT NULL AND TRIM(org_name) != '' THEN org_name
                    WHEN instr(email, '@') > 1 THEN substr(email, 1, instr(email, '@') - 1)
                    ELSE email
                END
                WHERE organization IS NULL OR TRIM(organization) = ''
                """
            )

            # EHR ingestion: an optional sender contact email on a link, so a
            # failed upload can notify the partner who sent it; plus a stamp on the
            # upload to dedupe the auto-notification. Both additive/nullable.
            if "contact_email" not in cols("ingest_upload_links"):
                conn.execute("ALTER TABLE ingest_upload_links ADD COLUMN contact_email TEXT")
            if "failure_notified_at" not in cols("ingest_uploads"):
                conn.execute("ALTER TABLE ingest_uploads ADD COLUMN failure_notified_at TEXT")

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
        is_mock: bool = False,
    ) -> Dict[str, Any]:
        email = email.lower().strip()
        uid = _new_id("u")
        id_hashed = hashlib.sha256(uid.encode("utf-8")).hexdigest()[:16]
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO users (id, email, password_hash, role, specialty, board_cert,
                                   years_experience, organization, id_hashed, active, is_mock, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
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
                    1 if is_mock else 0,
                    _utcnow_iso(),
                ),
            )
        return self.get_user_by_id(uid)  # type: ignore[return-value]

    def ensure_admin(self, *, email: str, password: str) -> Dict[str, Any]:
        """Idempotently guarantee a bootstrap admin exists for ``email``.

        Unlike ``seed_default_admin`` (which only runs when the user table is
        empty), this runs on every boot when ``ASCLEPIUS_ADMIN_EMAIL`` /
        ``ASCLEPIUS_ADMIN_PASSWORD`` are set, so an operator can always (re)gain
        access by setting those env vars and redeploying:

          * missing        -> create the account with role='admin', active=1
          * exists, drifted-> force role='admin', active=1, and reset the
                              password to match the env value
          * exists, matches-> no-op (no write, password already correct)

        Only touches this one account; other users are never modified.
        """
        email = email.lower().strip()
        existing = self.get_user_by_email(email)
        if not existing:
            return self.create_user(email=email, password=password, role="admin")
        # Only write when something actually needs to change, so a redeploy with
        # unchanged credentials doesn't churn the row (or revert a matching pw).
        needs_update = (
            existing.get("role") != "admin"
            or not existing.get("active")
            or not verify_password(password, existing["password_hash"])
        )
        if needs_update:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE users SET password_hash = ?, role = 'admin', active = 1 "
                    "WHERE email = ?",
                    (hash_password(password), email),
                )
            return self.get_user_by_email(email)  # type: ignore[return-value]
        return existing

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
                        -- Keep the canonical organization in sync with the
                        -- health-system name, but never wipe a previously-set org
                        -- if a re-onboard omits it (COALESCE keeps the old value).
                        organization = COALESCE(?, organization), clinical_role = ?,
                        npi = ?, credentials_json = ?, attestations_json = ?
                    WHERE email = ?
                    """,
                    (
                        hash_password(password), role, specialty, board_cert,
                        years_experience, full_name, org_name, org_name, clinical_role, npi,
                        creds_json, atts_json, email,
                    ),
                )
                return self.get_user_by_email(email)  # type: ignore[return-value]
            uid = _new_id("u")
            id_hashed = hashlib.sha256(uid.encode("utf-8")).hexdigest()[:16]
            conn.execute(
                """
                INSERT INTO users (id, email, password_hash, role, specialty, board_cert,
                                   years_experience, organization, id_hashed, active, full_name,
                                   org_name, clinical_role, npi, credentials_json,
                                   attestations_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uid, email, hash_password(password), role, specialty, board_cert,
                    years_experience, org_name, id_hashed, full_name, org_name, clinical_role,
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

    def set_user_password(self, user_id: str, new_password: str) -> None:
        """Set a user's password hash (data-provider forced first-login reset)."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (hash_password(new_password), user_id),
            )

    # ── Data-provider accounts (email+password door, EHR PRD §4) ────────────
    @staticmethod
    def _data_provider_row(row: sqlite3.Row) -> Dict[str, Any]:
        rec = dict(row)
        rec["must_reset_password"] = bool(rec.get("must_reset_password"))
        return rec

    def provision_data_provider(
        self, *, email: str, password: str, org_name: Optional[str] = None,
        specialty: Optional[str] = None, note: Optional[str] = None,
        invited_by: Optional[str] = None, invite_expires_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create (or rotate) a ``data_partner`` account + its provider record.
        Idempotent: an existing provider gets a fresh password, is re-activated,
        ``must_reset_password`` is re-armed, and the invite window resets — so
        Resend rotates credentials rather than duplicating. The account is
        provisioned via the shared ``provision_user`` path with role='data_partner'."""
        user = self.provision_user(
            email=email, password=password, role="data_partner",
            org_name=org_name, specialty=specialty,
        )
        pid = user["id"]
        now = _utcnow_iso()
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT provider_id FROM data_providers WHERE provider_id = ?", (pid,)
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE data_providers SET status='invited', must_reset_password=1,
                       org_name=COALESCE(?, org_name), specialty=COALESCE(?, specialty),
                       note=COALESCE(?, note), invited_by=?, invited_at=?,
                       invite_expires_at=?, updated_at=? WHERE provider_id=?""",
                    (org_name, specialty, note, invited_by, now, invite_expires_at, now, pid),
                )
            else:
                conn.execute(
                    """INSERT INTO data_providers
                       (provider_id, email, org_name, specialty, note, status,
                        must_reset_password, invited_by, invited_at, invite_expires_at,
                        created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, 'invited', 1, ?, ?, ?, ?, ?)""",
                    (pid, email.lower().strip(), org_name, specialty, note,
                     invited_by, now, invite_expires_at, now, now),
                )
        return self.get_data_provider(pid)  # type: ignore[return-value]

    def get_data_provider(self, provider_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM data_providers WHERE provider_id = ?", (provider_id,)
            ).fetchone()
        return self._data_provider_row(row) if row else None

    def list_data_providers(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM data_providers ORDER BY created_at DESC"
            ).fetchall()
        return [self._data_provider_row(r) for r in rows]

    def revoke_data_provider(self, provider_id: str) -> Optional[Dict[str, Any]]:
        """Revoke access: mark revoked AND deactivate the account so its token
        stops authenticating (deny-by-default)."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE data_providers SET status='revoked', updated_at=? WHERE provider_id=?",
                (_utcnow_iso(), provider_id),
            )
            conn.execute("UPDATE users SET active = 0 WHERE id = ?", (provider_id,))
        return self.get_data_provider(provider_id)

    def clear_provider_password_reset(self, provider_id: str) -> None:
        """First-login forced reset done: drop the reset flag + activate."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE data_providers SET must_reset_password=0, status='active', "
                "updated_at=? WHERE provider_id=?",
                (_utcnow_iso(), provider_id),
            )

    def provider_quality_score(self, provider_id: str) -> Dict[str, Any]:
        """% of a provider's upload bundles that ingested clean — the early-warning
        that a partner's de-id is drifting. Reads the shared ingest_uploads inbox
        filtered to this provider (partner_id)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status FROM ingest_uploads WHERE partner_id = ?", (provider_id,)
            ).fetchall()
        total = len(rows)
        clean = sum(1 for r in rows if r["status"] == "ingested")
        return {
            "total_uploads": total,
            "clean_uploads": clean,
            "clean_pct": round(100.0 * clean / total, 1) if total else None,
        }

    # ── Buyer accounts + deliveries (secure data workspace) ─────────────────
    @staticmethod
    def _buyer_account_row(row: sqlite3.Row) -> Dict[str, Any]:
        rec = dict(row)
        rec["must_reset_password"] = bool(rec.get("must_reset_password"))
        return rec

    def provision_buyer(
        self, *, email: str, password: str, buyer_name: Optional[str] = None,
        note: Optional[str] = None, invited_by: Optional[str] = None,
        invite_expires_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create (or rotate) a ``buyer`` account + its workspace record. Idempotent:
        an existing buyer keeps their delivery history but gets a fresh password and
        a re-armed forced reset when re-provisioned. The login lives in ``users``
        (role='buyer') via the shared ``provision_user`` path."""
        user = self.provision_user(
            email=email, password=password, role="buyer",
            full_name=buyer_name, org_name=buyer_name,
        )
        bid = user["id"]
        now = _utcnow_iso()
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT buyer_account_id FROM buyer_accounts WHERE buyer_account_id = ?", (bid,)
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE buyer_accounts SET status='invited', must_reset_password=1,
                       buyer_name=COALESCE(?, buyer_name), note=COALESCE(?, note),
                       invited_by=?, invited_at=?, invite_expires_at=?, updated_at=?
                       WHERE buyer_account_id=?""",
                    (buyer_name, note, invited_by, now, invite_expires_at, now, bid),
                )
            else:
                conn.execute(
                    """INSERT INTO buyer_accounts
                       (buyer_account_id, email, buyer_name, note, status,
                        must_reset_password, invited_by, invited_at, invite_expires_at,
                        created_at, updated_at)
                       VALUES (?, ?, ?, ?, 'invited', 1, ?, ?, ?, ?, ?)""",
                    (bid, email.lower().strip(), buyer_name, note,
                     invited_by, now, invite_expires_at, now, now),
                )
        return self.get_buyer_account(bid)  # type: ignore[return-value]

    def get_buyer_account(self, buyer_account_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM buyer_accounts WHERE buyer_account_id = ?", (buyer_account_id,)
            ).fetchone()
        return self._buyer_account_row(row) if row else None

    def get_buyer_account_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM buyer_accounts WHERE email = ?", (email.lower().strip(),)
            ).fetchone()
        return self._buyer_account_row(row) if row else None

    def list_buyer_accounts(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM buyer_accounts ORDER BY created_at DESC"
            ).fetchall()
        return [self._buyer_account_row(r) for r in rows]

    def clear_buyer_password_reset(self, buyer_account_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE buyer_accounts SET must_reset_password=0, status='active', "
                "updated_at=? WHERE buyer_account_id=?",
                (_utcnow_iso(), buyer_account_id),
            )

    def record_buyer_delivery(
        self, *, buyer_account_id: str, buyer_email: str, export_id: str,
        label: Optional[str] = None, data_format: Optional[str] = None,
        record_count: int = 0, note: Optional[str] = None, sent_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        did = _new_id("del")
        now = _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO buyer_deliveries
                   (delivery_id, buyer_account_id, buyer_email, export_id, label,
                    data_format, record_count, note, sent_by, sent_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (did, buyer_account_id, buyer_email.lower().strip(), export_id, label,
                 data_format, int(record_count or 0), note, sent_by, now),
            )
        return self.get_buyer_delivery(did)  # type: ignore[return-value]

    def get_buyer_delivery(self, delivery_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM buyer_deliveries WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_buyer_deliveries(
        self, *, buyer_account_id: Optional[str] = None, export_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if buyer_account_id:
            clauses.append("buyer_account_id = ?")
            params.append(buyer_account_id)
        if export_id:
            clauses.append("export_id = ?")
            params.append(export_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM buyer_deliveries {where} ORDER BY sent_at DESC", tuple(params)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_user_by_id_hashed(self, id_hashed: str) -> Optional[Dict[str, Any]]:
        """Resolve the user (incl. onboarding-collected credential fields) from the
        hashed annotator id that stamps every record."""
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE id_hashed = ?", (id_hashed,)).fetchone()
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

    # ─── Real EHR ingestion (EHR PRD §4, §5, §8) ─────────────────────────────
    def create_upload_link(
        self, *, token_hash: str, partner_id: str, partner_label: Optional[str],
        specialty: str, expires_at: str, one_time: bool, max_bytes: int,
        created_by: Optional[str], contact_email: Optional[str] = None,
    ) -> Dict[str, Any]:
        lid = _new_id("lnk")
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO ingest_upload_links
                   (link_id, token_hash, partner_id, partner_label, specialty,
                    expires_at, one_time, max_bytes, created_by, contact_email, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (lid, token_hash, partner_id, partner_label, specialty, expires_at,
                 1 if one_time else 0, int(max_bytes), created_by,
                 (contact_email or None), _utcnow_iso()),
            )
        return self.get_upload_link(lid)  # type: ignore[return-value]

    def get_upload_link(self, link_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM ingest_upload_links WHERE link_id = ?", (link_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_upload_link_by_token_hash(self, token_hash: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM ingest_upload_links WHERE token_hash = ?", (token_hash,)
            ).fetchone()
        return dict(row) if row else None

    def list_upload_links(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM ingest_upload_links ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_upload_link_used(self, link_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE ingest_upload_links SET used_count = used_count + 1 WHERE link_id = ?",
                (link_id,),
            )

    def consume_upload_link(self, link_id: str, *, one_time: bool) -> bool:
        """ATOMIC use-claim (security review: closes the one-time TOCTOU race —
        two concurrent uploads both passing the used_count==0 read). For a
        one-time link the conditional UPDATE succeeds for exactly one caller;
        multi-use links just increment. Returns False when the claim lost."""
        with self._conn() as conn:
            if one_time:
                cur = conn.execute(
                    "UPDATE ingest_upload_links SET used_count = used_count + 1 "
                    "WHERE link_id = ? AND used_count = 0 AND revoked = 0",
                    (link_id,),
                )
            else:
                cur = conn.execute(
                    "UPDATE ingest_upload_links SET used_count = used_count + 1 "
                    "WHERE link_id = ? AND revoked = 0",
                    (link_id,),
                )
            return cur.rowcount == 1

    def revoke_upload_link(self, link_id: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE ingest_upload_links SET revoked = 1 WHERE link_id = ?", (link_id,))

    def new_upload_id(self) -> str:
        """Mint an upload id up front so the raw blob can be written to durable
        storage BEFORE the row is inserted (the row then always carries a valid
        raw_path — no None window where the file is on disk but unreachable)."""
        return _new_id("upl")

    def insert_ingest_upload(
        self, *, link_id: str, partner_id: str, filename: Optional[str],
        sha256: Optional[str], size_bytes: Optional[int], raw_path: Optional[str],
        source_ip: Optional[str], upload_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        uid = upload_id or _new_id("upl")
        now = _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO ingest_uploads
                   (upload_id, link_id, partner_id, filename, sha256, size_bytes,
                    status, raw_path, source_ip, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'received', ?, ?, ?, ?)""",
                (uid, link_id, partner_id, filename, sha256, size_bytes,
                 raw_path, source_ip, now, now),
            )
        return self.get_ingest_upload(uid)  # type: ignore[return-value]

    def update_ingest_upload(self, upload_id: str, **fields: Any) -> None:
        allowed = {"status", "reason", "files_json", "raw_path"}
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "files_json" and not isinstance(v, (str, type(None))):
                v = json.dumps(v)
            sets.append(f"{k} = ?")
            params.append(v)
        if not sets:
            return
        sets.append("updated_at = ?")
        params.extend([_utcnow_iso(), upload_id])
        with self._conn() as conn:
            conn.execute(f"UPDATE ingest_uploads SET {', '.join(sets)} WHERE upload_id = ?", tuple(params))

    def get_ingest_upload(self, upload_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM ingest_uploads WHERE upload_id = ?", (upload_id,)).fetchone()
        if not row:
            return None
        rec = dict(row)
        rec["files"] = json.loads(rec.pop("files_json") or "[]")
        return rec

    def list_ingest_uploads(self, *, limit: int = 200, offset: int = 0) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM ingest_uploads ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (max(1, limit), max(0, offset)),
            ).fetchall()
        out = []
        for r in rows:
            rec = dict(r)
            rec["files"] = json.loads(rec.pop("files_json") or "[]")
            out.append(rec)
        return out

    def count_ingest_uploads(self) -> int:
        """Total upload rows — lets the admin UI paginate over full history."""
        with self._conn() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM ingest_uploads").fetchone()[0])

    def mark_upload_failure_notified(self, upload_id: str) -> None:
        """Stamp the moment we emailed the sender that their upload failed, so the
        auto-notifier fires at most once per upload (manual re-sends are allowed)."""
        now = _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                "UPDATE ingest_uploads SET failure_notified_at = ?, updated_at = ? "
                "WHERE upload_id = ?",
                (now, now, upload_id),
            )

    def insert_ingest_case(
        self, *, upload_id: str, patient_key: Optional[str], specialty: Optional[str],
        case: Optional[Dict[str, Any]], status: str, report: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        cid = _new_id("icase")
        now = _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO ingest_cases
                   (ingest_case_id, upload_id, patient_key, specialty, case_json,
                    status, report_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (cid, upload_id, patient_key, specialty,
                 json.dumps(case) if case else None, status,
                 json.dumps(report) if report else None, now, now),
            )
        return self.get_ingest_case(cid)  # type: ignore[return-value]

    def update_ingest_case(self, ingest_case_id: str, **fields: Any) -> None:
        allowed = {"status", "case_json", "report_json", "task_id", "override_reason"}
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k in ("case_json", "report_json") and not isinstance(v, (str, type(None))):
                v = json.dumps(v)
            sets.append(f"{k} = ?")
            params.append(v)
        if not sets:
            return
        sets.append("updated_at = ?")
        params.extend([_utcnow_iso(), ingest_case_id])
        with self._conn() as conn:
            conn.execute(
                f"UPDATE ingest_cases SET {', '.join(sets)} WHERE ingest_case_id = ?", tuple(params)
            )

    def get_ingest_case(self, ingest_case_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM ingest_cases WHERE ingest_case_id = ?", (ingest_case_id,)
            ).fetchone()
        if not row:
            return None
        rec = dict(row)
        rec["case"] = json.loads(rec.pop("case_json") or "null")
        rec["report"] = json.loads(rec.pop("report_json") or "null")
        return rec

    def list_ingest_cases(
        self, *, upload_id: Optional[str] = None, status: Optional[str] = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if upload_id:
            clauses.append("upload_id = ?")
            params.append(upload_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM ingest_cases {where} ORDER BY created_at DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        out = []
        for r in rows:
            rec = dict(r)
            rec["case"] = json.loads(rec.pop("case_json") or "null")
            rec["report"] = json.loads(rec.pop("report_json") or "null")
            out.append(rec)
        return out

    def delete_unpromoted_ingest_cases(self, upload_id: str) -> int:
        """Remove an upload's cases that have NOT been promoted to a task
        (``task_id`` still null). Lets a reprocess (startup recovery of an
        upload interrupted by a redeploy) start from a clean slate without
        creating duplicate cases — while never touching promoted work."""
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM ingest_cases WHERE upload_id = ? "
                "AND (task_id IS NULL OR task_id = '')",
                (upload_id,),
            )
            return cur.rowcount

    def list_uploads_in_status(self, statuses: List[str]) -> List[Dict[str, Any]]:
        """Uploads currently sitting in any of ``statuses`` — used by startup
        recovery to find work interrupted mid-pipeline (received/scanning/parsing)."""
        if not statuses:
            return []
        qs = ",".join("?" for _ in statuses)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM ingest_uploads WHERE status IN ({qs}) "
                "ORDER BY created_at ASC", tuple(statuses),
            ).fetchall()
        out = []
        for r in rows:
            rec = dict(r)
            rec["files"] = json.loads(rec.pop("files_json") or "[]")
            out.append(rec)
        return out

    def set_real_data_approved(self, user_id: str, approved: bool) -> None:
        """Grant/revoke V4 real-case access (EHR PRD §9.5)."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE users SET real_data_approved = ? WHERE id = ?",
                (1 if approved else 0, user_id),
            )

    def mock_annotator_id_hashes(self) -> set:
        """The ``id_hashed`` of every mock/sandbox contributor. Records carry the
        annotator's ``id_hashed``; export hard-excludes these by default and the
        admin labels them, so a demo never contaminates a shipped batch."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id_hashed FROM users WHERE is_mock = 1 AND id_hashed IS NOT NULL"
            ).fetchall()
        return {r[0] for r in rows if r[0]}

    def ensure_mock_user(
        self,
        *,
        email: str,
        password: str,
        specialty: Optional[str] = None,
        board_cert: Optional[str] = None,
        years_experience: Optional[int] = None,
        organization: Optional[str] = None,
        real_data_approved: bool = False,
    ) -> Dict[str, Any]:
        """Idempotently guarantee the mock/sandbox contributor exists (internal demo
        tool). Runs on every boot: creates the account if missing, else forces it to
        role='evaluator', active, is_mock=1, and resets the password to match the
        configured value (so an operator can always regain the sandbox login). Only
        touches this one account.

        ``real_data_approved`` is DECIDED BY THE CALLER (auth.ensure_mock_contributor):
        the sandbox may demo V4 real cases only when its password is NOT the known
        default in production — a default-credential account must never grant read
        access to real patient data (security review finding)."""
        email = email.lower().strip()
        approved = 1 if real_data_approved else 0
        existing = self.get_user_by_email(email)
        if not existing:
            u = self.create_user(
                email=email, password=password, role="evaluator",
                specialty=specialty, board_cert=board_cert,
                years_experience=years_experience, organization=organization,
                is_mock=True,
            )
            self.set_real_data_approved(u["id"], bool(real_data_approved))
            return self.get_user_by_id(u["id"])  # type: ignore[return-value]
        with self._conn() as conn:
            conn.execute(
                """UPDATE users SET password_hash = ?, role = 'evaluator', active = 1,
                       is_mock = 1, real_data_approved = ?,
                       specialty = COALESCE(specialty, ?),
                       board_cert = COALESCE(board_cert, ?),
                       years_experience = COALESCE(years_experience, ?),
                       organization = COALESCE(organization, ?)
                   WHERE email = ?""",
                (hash_password(password), approved, specialty, board_cert,
                 years_experience, organization, email),
            )
        return self.get_user_by_email(email)  # type: ignore[return-value]

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
        independent_mode: str = "stance",
        buyer_request_id: Optional[str] = None,
        generation: Optional[Dict[str, Any]] = None,
        value_tier: Optional[str] = None,
        modality: str = "text",
        case: Optional[Dict[str, Any]] = None,
        task_id: Optional[str] = None,
        created_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        from asclepius.constants import normalize_independent_mode

        tid = task_id or _new_id("t")
        gm = grounding_mode if grounding_mode in ("optional", "required") else "optional"
        im = normalize_independent_mode(independent_mode)
        # Multimodal (Synthetic Multimodal Cases PRD): modality is DERIVED from case
        # presence — a task is multimodal iff it carries a structured case. We do
        # NOT honor a bare modality='multimodal' label with no case: that would
        # stamp records multimodal + grant the value premium with no case data
        # behind it (a mislabel from a hand-built upload). Case is the single source
        # of truth; the ``modality`` param is advisory. The FULL case (incl. internal
        # ground_truth) is stored server-side; blinding/packaging strip the answer
        # key downstream — the same contract as the server-side ``intended_flawed_id``.
        # Modality is derived from CONTENT, not presence (BUG-1 §3): a case dict
        # that carries no labs AND no notes is an empty case and can never be
        # stamped multimodal (which would grant the value premium + a multimodal
        # label with no data behind it). An empty ``case={}`` is treated as text.
        md = "multimodal" if (case and (case.get("lab_panels") or case.get("notes"))) else "text"
        # case_source is DERIVED from the case (EHR PRD §9.5): 'real_deid' only
        # when the case itself says so; any other case is 'synthetic'; a text
        # task has none. First-class column so the V4 routing wall is pure SQL.
        cs = ((case.get("case_source") or "synthetic") if case else None)
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO tasks
                  (task_id, specialty, difficulty, capture_reasoning, source, prompt,
                   candidate_answers_json, max_labels, grounding_mode, independent_mode,
                   buyer_request_id, generation_json, value_tier, modality, case_json,
                   case_source, status, created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
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
                    im,
                    buyer_request_id,
                    json.dumps(generation) if generation else None,
                    (value_tier or None),
                    md,
                    json.dumps(case) if case else None,
                    cs,
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
        # Multimodal case (may be absent on legacy rows / text tasks).
        rec["case"] = json.loads(rec.pop("case_json", "null") or "null")
        return rec

    def list_tasks(
        self, *, specialty: Optional[str] = None, status: Optional[str] = None, limit: int = 500
    ) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if specialty:
            clauses.append("specialty = ?")
            params.append(specialty)
        if status:
            clauses.append("status = ?")
            params.append(status)
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
        self, *, evaluator_id: str, specialty: Optional[str], hard_only: bool = False,
        real_only: bool = False, multimodal_only: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Oldest open task in the evaluator's specialty that (a) they have not
        already submitted and (b) still has label capacity (max_labels).
        ``hard_only`` (Seamless PRD WS2, the V3 hard-case queue) restricts to
        ``difficulty='hard'`` tasks.

        ``real_only`` is the V4 wall (EHR PRD §9.5), enforced in SQL: True serves
        ONLY ``case_source='real_deid'`` tasks (the V4 queue); False EXCLUDES
        them entirely (v1/v2/v3 can never be served a real patient case).

        TODO(scale): this scans candidate open tasks in Python; fine at pod scale.
        Push the not-mine + capacity filter fully into SQL when volume grows."""
        clauses = ["t.status = 'open'"]
        # NOTE: the ``mine`` correlated subquery placeholder appears BEFORE the
        # WHERE clause in the SQL text, so ``evaluator_id`` must bind first.
        params: List[Any] = [evaluator_id]
        if specialty:
            clauses.append("t.specialty = ?")
            params.append(specialty)
        if hard_only:
            clauses.append("t.difficulty = 'hard'")
        # V3 multimodal-only queue (default): serve structured cases only.
        if multimodal_only:
            clauses.append("t.modality = 'multimodal'")
        clauses.append(
            "t.case_source = 'real_deid'" if real_only
            else "(t.case_source IS NULL OR t.case_source != 'real_deid')"
        )
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

    def eligible_tasks_for_evaluator(
        self, *, evaluator_id: str, specialty: Optional[str], limit: Optional[int] = None,
        hard_only: bool = False, real_only: bool = False, multimodal_only: bool = False,
    ) -> List[Dict[str, Any]]:
        """All open tasks this evaluator may take (not already theirs + still has
        label capacity), oldest first — the candidate set value-aware routing
        (Value-per-Minute PRD B3) ranks by expected value-per-minute.
        ``hard_only`` (Seamless PRD WS2) restricts to ``difficulty='hard'`` (the
        V3 hard-case queue).

        The scan is UNBOUNDED, exactly like the classic ``next_task_for_evaluator``
        (both filter ``mine``/capacity in Python at pod scale). A SQL ``LIMIT``
        applied before that filter would starve value-aware routing: if the N
        oldest open tasks are all already-labeled or at capacity, a capped fetch
        returns nothing even though eligible tasks exist further down — and a
        newer high-value task could never enter the ranked set. ``limit`` (if
        given) caps only the returned candidate count AFTER filtering."""
        clauses = ["t.status = 'open'"]
        params: List[Any] = [evaluator_id]
        if specialty:
            clauses.append("t.specialty = ?")
            params.append(specialty)
        if hard_only:
            clauses.append("t.difficulty = 'hard'")
        # V3 multimodal-only queue (default): structured cases only.
        if multimodal_only:
            clauses.append("t.modality = 'multimodal'")
        # The V4 wall (EHR PRD §9.5) — same rule as next_task_for_evaluator.
        clauses.append(
            "t.case_source = 'real_deid'" if real_only
            else "(t.case_source IS NULL OR t.case_source != 'real_deid')"
        )
        where = " AND ".join(clauses)
        with self._conn() as conn:
            rows = conn.execute(
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
        out: List[Dict[str, Any]] = []
        for r in rows:
            rec = self._task_row(r)
            if rec.get("mine"):
                continue
            if int(r["sub_count"]) >= int(rec.get("max_labels") or 1):
                continue
            rec.pop("sub_count", None)
            rec.pop("mine", None)
            out.append(rec)
            if limit is not None and len(out) >= limit:
                break
        return out

    def evaluator_median_seconds(self, evaluator_id: str) -> Optional[float]:
        """The contributor's rolling median seconds-per-task (Value-per-Minute
        PRD B3 routing denominator). Median, not mean, so one slow outlier task
        doesn't distort routing. None until they have any timed submission."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT time_spent_sec FROM submissions "
                "WHERE evaluator_id = ? AND time_spent_sec > 0 ORDER BY time_spent_sec ASC",
                (evaluator_id,),
            ).fetchall()
        vals = [int(r["time_spent_sec"]) for r in rows]
        if not vals:
            return None
        n = len(vals)
        mid = n // 2
        return float(vals[mid]) if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0

    def mark_task_status(self, task_id: str, status: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE tasks SET status = ? WHERE task_id = ?", (status, task_id))

    def set_task_candidates(
        self, task_id: str, candidates: List[Dict[str, Any]], *, generation_patch: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """Replace a task's candidate answers in place (FEAT-1 "grade the real
        models" mode swaps in a baseline A/B pair). Optionally merge a patch into
        the task's generation provenance block. Does not touch status/created_at."""
        task = self.get_task(task_id)
        if not task:
            return None
        gen = task.get("generation") or {}
        if generation_patch:
            gen = {**gen, **generation_patch}
        with self._conn() as conn:
            conn.execute(
                "UPDATE tasks SET candidate_answers_json = ?, generation_json = ? WHERE task_id = ?",
                (json.dumps(candidates or []), json.dumps(gen) if gen else None, task_id),
            )
        return self.get_task(task_id)

    def refresh_task_status(self, task_id: str) -> None:
        """Close a task once it has reached its label capacity.

        A task a clinician flagged as having an invalid prompt (Eval Flow Upgrade
        §2) is terminal — never reopen/close it back to a normal status, so it
        stays out of the queue and visible in the admin flagged list even if a
        concurrent normal submission also lands on it."""
        task = self.get_task(task_id)
        if not task:
            return
        # Terminal Stage-1 flags never reopen/close back to a normal status, so
        # they stay out of the queue even if a concurrent normal submission lands:
        #   prompt_flagged — clinically invalid prompt (Eval Flow Upgrade §2)
        #   not_hard       — valid but not a hard case (Seamless PRD WS2)
        #   case_incoherent— internally inconsistent multimodal case (Multimodal §5)
        if task.get("status") in ("prompt_flagged", "not_hard", "case_incoherent"):
            return
        count = self.submission_count_for_task(task_id)
        new_status = "done" if count >= int(task.get("max_labels") or 1) else "open"
        self.mark_task_status(task_id, new_status)

    # ─── Independent-answer reveal gate (Eval Flow Upgrade §1) ──────────────────
    def commit_independent_answer(
        self, *, task_id: str, evaluator_id: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Record (idempotently) that ``evaluator_id`` committed a blind independent
        answer for ``task_id`` BEFORE the candidate answers were revealed. The FIRST
        commit wins (``INSERT OR IGNORE``) — a later re-reveal never overwrites the
        original pre-reveal answer or timestamp. ``captured_at`` is forced to server
        time, never trusted from the client."""
        now = _utcnow_iso()
        payload = dict(payload or {})
        payload["captured_at"] = now
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO independent_commits "
                "(task_id, evaluator_id, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (task_id, evaluator_id, json.dumps(payload), now),
            )
        return self.get_independent_commit(task_id, evaluator_id)  # type: ignore[return-value]

    def get_independent_commit(
        self, task_id: str, evaluator_id: str
    ) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM independent_commits WHERE task_id = ? AND evaluator_id = ?",
                (task_id, evaluator_id),
            ).fetchone()
        if not row:
            return None
        rec = dict(row)
        rec["payload"] = json.loads(rec.pop("payload_json", "{}") or "{}")
        return rec

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
        rec["progress"] = json.loads(rec.pop("progress_json", "null") or "null")
        return rec

    def set_submission_progress(
        self, submission_id: str, *, phase: str, pct: int, detail: Optional[str] = None
    ) -> None:
        """Stamp the real, backend-observed pipeline phase onto a submission (BUG-5).
        Called by the pipeline when each stage ACTUALLY starts — the client polls
        ``GET /submissions/{id}/status`` and shows this exact phase + pct."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE submissions SET progress_json = ?, updated_at = ? WHERE submission_id = ?",
                (json.dumps({"phase": phase, "pct": int(pct), "detail": detail}),
                 _utcnow_iso(), submission_id),
            )

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
        portal_version: str = "v2",
        status: str = "submitted",
    ) -> Dict[str, Any]:
        now = _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO submissions
                  (submission_id, task_id, evaluator_id, verdict, chosen_id, rejected_id,
                   confidence, time_spent_sec, status, dedupe_hash, grounded, grounding_mode,
                   portal_version, payload_json, annotator_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    portal_version,
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

    def set_submission_value(
        self,
        submission_id: str,
        *,
        realized: Optional[float],
        projected: Optional[float],
        clinician_review_seconds: Optional[int],
    ) -> None:
        """Persist the value estimate + clinician-minutes for a submission
        (Value-per-Minute PRD A4). Measurement only — never touches records,
        status, or any v1/v2 behavior."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE submissions SET value_estimate_usd = ?, "
                "value_estimate_projected_usd = ?, clinician_review_seconds = ?, updated_at = ? "
                "WHERE submission_id = ?",
                (
                    None if realized is None else round(float(realized), 2),
                    None if projected is None else round(float(projected), 2),
                    None if clinician_review_seconds is None else int(clinician_review_seconds),
                    _utcnow_iso(),
                    submission_id,
                ),
            )

    @staticmethod
    def _median(vals: List[float]) -> Optional[float]:
        vals = sorted(v for v in vals if v is not None)
        if not vals:
            return None
        n = len(vals)
        mid = n // 2
        return round(float(vals[mid]) if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0, 2)

    def value_per_time_rows(self) -> List[Dict[str, Any]]:
        """Raw per-submission value + time + segmenting attributes for the V/T
        report (Value-per-Minute PRD A4). Only rows with a value estimate AND
        positive time contribute a ratio (an un-timed row has no defined V/T)."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT s.evaluator_id,
                       u.email                                   AS evaluator_email,
                       s.portal_version                          AS portal_version,
                       t.difficulty                              AS difficulty,
                       t.source                                  AS source,
                       s.grounded                                AS grounded,
                       s.value_estimate_usd                      AS realized,
                       s.value_estimate_projected_usd            AS projected,
                       COALESCE(s.clinician_review_seconds, s.time_spent_sec) AS seconds
                FROM submissions s
                JOIN tasks t ON t.task_id = s.task_id
                LEFT JOIN users u ON u.id = s.evaluator_id
                WHERE s.value_estimate_usd IS NOT NULL
                  AND COALESCE(s.clinician_review_seconds, s.time_spent_sec) > 0
                  AND s.status != 'rejected'
                """
            ).fetchall()
        return [dict(r) for r in rows]

    def value_per_time_stats(self) -> Dict[str, Any]:
        """Median realized + projected value-per-minute, split by portal_version
        (v1 vs v2), difficulty, grounded vs plain, Mode A vs B, and per
        contributor (Value-per-Minute PRD A4). Medians (robust to outliers).
        Realized is what the team is held to; projected is the reuse forecast."""
        rows = self.value_per_time_rows()

        def vpm(r: Dict[str, Any], key: str) -> Optional[float]:
            secs = r.get("seconds") or 0
            val = r.get(key)
            if not secs or val is None:
                return None
            return float(val) / (secs / 60.0)

        def summarize(subset: List[Dict[str, Any]]) -> Dict[str, Any]:
            realized = [x for x in (vpm(r, "realized") for r in subset) if x is not None]
            projected = [x for x in (vpm(r, "projected") for r in subset) if x is not None]
            return {
                "n": len(subset),
                "realized_vpm": self._median(realized),
                "projected_vpm": self._median(projected),
                "realized_value_median": self._median([float(r["realized"]) for r in subset if r.get("realized") is not None]),
            }

        def group_by(fn) -> Dict[str, Any]:
            buckets: Dict[str, List[Dict[str, Any]]] = {}
            for r in rows:
                buckets.setdefault(str(fn(r)), []).append(r)
            return {k: summarize(v) for k, v in buckets.items()}

        return {
            "overall": summarize(rows),
            "by_portal_version": group_by(lambda r: r.get("portal_version") or "v2"),
            "by_difficulty": group_by(lambda r: r.get("difficulty") or "medium"),
            "by_grounded": group_by(lambda r: "grounded" if r.get("grounded") else "plain"),
            "by_mode": group_by(lambda r: "mode_b" if (r.get("source") == "lab_supplied") else "mode_a"),
            "by_contributor": group_by(lambda r: r.get("evaluator_email") or r.get("evaluator_id") or "—"),
            "target": None,  # filled by the router from constants (keeps store I/O-free)
        }

    def override_rate_stats(self, *, portal_version: Optional[str] = "v2") -> Dict[str, Any]:
        """Model-assist override rate (Value-per-Minute PRD Part D quality gate):
        of the assisted submissions where a suggestion existed, how often did the
        clinician's FINAL differ from the machine SUGGESTION? A near-zero rate
        flags rubber-stamping. Scoped to v2 (only the assisted flow pre-labels).

        Verdict override: final verdict != assist.suggested_verdict.
        Step override: any reasoning step whose final label != suggested_label."""
        params: List[Any] = []
        pv_clause = ""
        if portal_version:
            pv_clause = "WHERE s.portal_version = ?"
            params.append(portal_version)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT s.verdict, s.payload_json FROM submissions s {pv_clause}",
                tuple(params),
            ).fetchall()
        verdict_total = verdict_overrides = 0
        step_total = step_overrides = 0
        for r in rows:
            try:
                payload = json.loads(r["payload_json"] or "{}")
            except (ValueError, TypeError):
                continue
            assist = payload.get("assist") or {}
            if assist.get("prelabeled") and assist.get("suggested_verdict"):
                verdict_total += 1
                if (r["verdict"] or None) != assist.get("suggested_verdict"):
                    verdict_overrides += 1
            for step in payload.get("reasoning_steps") or []:
                sug = step.get("suggested_label")
                if sug is None:
                    continue
                step_total += 1
                if (step.get("label") or None) != sug:
                    step_overrides += 1
            for src in ("from_scratch",):
                for step in (payload.get(src) or {}).get("reasoning_steps") or []:
                    sug = step.get("suggested_label")
                    if sug is None:
                        continue
                    step_total += 1
                    if (step.get("label") or None) != sug:
                        step_overrides += 1
        return {
            "portal_version": portal_version,
            "verdict": {
                "assisted": verdict_total,
                "overrides": verdict_overrides,
                "override_rate": round(verdict_overrides / verdict_total, 3) if verdict_total else None,
            },
            "steps": {
                "assisted": step_total,
                "overrides": step_overrides,
                "override_rate": round(step_overrides / step_total, 3) if step_total else None,
            },
        }

    def portal_version_counts(self) -> Dict[str, int]:
        """Submissions by evaluator product version — lets the admin dashboard
        show how much data came from V1 (classic) vs V2 (assisted) vs V3
        (seamless)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT portal_version, COUNT(*) AS n FROM submissions GROUP BY portal_version"
            ).fetchall()
        return {(r["portal_version"] or "v2"): int(r["n"]) for r in rows}

    def open_modality_counts(self) -> Dict[str, int]:
        """OPEN (servable) tasks by modality (Multimodal Debug PRD P3.11) — the
        admin dashboard shows "multimodal in queue: N" so an operator always knows
        whether structured cases exist without inspecting the tasks table."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT COALESCE(modality, 'text') AS m, COUNT(*) AS n "
                "FROM tasks WHERE status = 'open' GROUP BY m"
            ).fetchall()
        counts = {r["m"]: int(r["n"]) for r in rows}
        counts.setdefault("text", 0)
        counts.setdefault("multimodal", 0)
        return counts

    def ab_balance_stats(self) -> Dict[str, Any]:
        """Position-bias QC (Seamless PRD WS6). The stronger/weaker A-B slot is
        randomized 50/50 at candidate build (``critic.generate_candidates_ex``)
        so a reward model can't learn "A is better" instead of "the better answer
        is better". Over generated tasks carrying a server-side
        ``intended_flawed_id`` (never shown to the blinded evaluator), report the
        fraction whose STRONGER answer landed in slot A — a rate that drifts from
        ~0.5 is a QC alarm a competent buyer would also detect."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT generation_json FROM tasks WHERE generation_json IS NOT NULL"
            ).fetchall()
        n = 0
        a_stronger = 0
        for r in rows:
            try:
                gen = json.loads(r["generation_json"] or "null")
            except (ValueError, TypeError):
                continue
            if not isinstance(gen, dict):
                continue
            fid = gen.get("intended_flawed_id")
            if fid not in ("A", "B"):
                continue
            n += 1
            if fid == "B":  # flawed answer in B ⇒ the stronger answer is in A
                a_stronger += 1
        return {
            "n": n,
            "a_stronger": a_stronger,
            "a_stronger_rate": round(a_stronger / n, 3) if n else None,
        }

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
                SELECT u.id, u.id_hashed, u.email, u.role, u.specialty, u.is_mock,
                       -- The onboarding flow historically wrote the health-system
                       -- name to org_name; the canonical column is organization.
                       -- COALESCE both so existing onboarded users (organization
                       -- NULL, org_name set) resolve to their real org, not
                       -- "Unaffiliated".
                       COALESCE(u.organization, u.org_name) AS user_org,
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
            from asclepius.constants import UNASSIGNED_ORG
            organization = (
                (cred or {}).get("organization")
                or r["user_org"]
                or UNASSIGNED_ORG
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
                    # Mock/sandbox contributor — labeled in the admin and hard-
                    # excluded from real exports (internal demo tool).
                    "is_mock": bool(r["is_mock"]),
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
        from asclepius.constants import UNASSIGNED_ORG
        orgs: Dict[str, Dict[str, Any]] = {}
        for c in self.contributor_directory():
            org = c["organization"] or UNASSIGNED_ORG
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
        from asclepius.constants import UNASSIGNED_ORG
        return [
            c["id_hashed"]
            for c in self.contributor_directory()
            if (c["organization"] or UNASSIGNED_ORG) == organization and c["id_hashed"]
        ]

    # ─── Contributor record diagnostics & re-attribution (ops tooling) ────────
    def contributor_record_diagnostics(self, user: Dict[str, Any]) -> Dict[str, Any]:
        """Explain whether a per-contributor "Export Data" will work for ``user``:
        submission counts by status, record counts by status, and — among the
        records a scoped export can ship (status export_ready | exported) — how
        many actually carry this user's hashed-annotator id (the export match
        key) vs a mismatched/blank id. A non-zero ``annotator_mismatch`` is the
        usual reason an export of a contributor with records still ships nothing."""
        uid = user["id"]
        idh = user.get("id_hashed")
        with self._conn() as conn:
            sub_by_status = {
                r["status"]: int(r["n"])
                for r in conn.execute(
                    "SELECT status, COUNT(*) AS n FROM submissions WHERE evaluator_id = ? GROUP BY status",
                    (uid,),
                ).fetchall()
            }
            rec_by_status = {
                r["status"]: int(r["n"])
                for r in conn.execute(
                    "SELECT r.status, COUNT(*) AS n FROM records r "
                    "JOIN submissions s ON s.submission_id = r.submission_id "
                    "WHERE s.evaluator_id = ? GROUP BY r.status",
                    (uid,),
                ).fetchall()
            }
            shippable_rows = conn.execute(
                "SELECT r.payload_json FROM records r "
                "JOIN submissions s ON s.submission_id = r.submission_id "
                "WHERE s.evaluator_id = ? AND r.status IN ('export_ready', 'exported')",
                (uid,),
            ).fetchall()
        annotator_match = annotator_mismatch = 0
        for row in shippable_rows:
            payload = json.loads(row["payload_json"] or "{}")
            if payload.get("annotator_id_hashed") == idh:
                annotator_match += 1
            else:
                annotator_mismatch += 1
        return {
            "user_id": uid,
            "id_hashed": idh,
            "email": user.get("email"),
            "active": bool(user.get("active")),
            "submissions_by_status": sub_by_status,
            "records_by_status": rec_by_status,
            "submissions_total": sum(sub_by_status.values()),
            "records_total": sum(rec_by_status.values()),
            # What the contributor "Export Data" button would actually emit
            # (now that scoped exports include already-exported records):
            "exportable_records": annotator_match,
            "annotator_id_mismatch_records": annotator_mismatch,
        }

    def reattribute_contributor(
        self, *, source_user: Dict[str, Any], target_user: Dict[str, Any],
        deactivate_source: bool = True,
    ) -> Dict[str, Any]:
        """Move every submission, packaged record, and independent-answer commit
        from ``source_user`` to ``target_user`` and rewrite the annotator
        provenance (hashed id + credential attributes) on both the submissions and
        the shipped records, so a contributor-scoped export of the target now
        includes this work. Optionally deactivates the now-empty source account.

        Atomic (single transaction). Returns a summary of what changed."""
        source_id = source_user["id"]
        target_id = target_user["id"]
        if source_id == target_id:
            raise ValueError("source and target are the same account")
        block = self.annotator_block(target_user)
        # The exact provenance fields packaging stamps onto every record.
        prov_patch = {
            "annotator_id_hashed": block.get("id_hashed"),
            "annotator_credential": block.get("credentials"),
            "annotator_specialty": block.get("specialty"),
            "annotator_years_experience": block.get("years_experience"),
        }
        with self._conn() as conn:
            submission_ids = [
                r["submission_id"]
                for r in conn.execute(
                    "SELECT submission_id FROM submissions WHERE evaluator_id = ?", (source_id,)
                ).fetchall()
            ]
            records_rewritten = 0
            for sid in submission_ids:
                for rec in conn.execute(
                    "SELECT record_id, payload_json FROM records WHERE submission_id = ?", (sid,)
                ).fetchall():
                    payload = json.loads(rec["payload_json"] or "{}")
                    payload.update(prov_patch)
                    conn.execute(
                        "UPDATE records SET payload_json = ? WHERE record_id = ?",
                        (json.dumps(payload), rec["record_id"]),
                    )
                    records_rewritten += 1
            conn.execute(
                "UPDATE submissions SET evaluator_id = ?, annotator_json = ? WHERE evaluator_id = ?",
                (target_id, json.dumps(block), source_id),
            )
            # PK is (task_id, evaluator_id); OR IGNORE skips a commit the target
            # already has for the same task (the source row is simply left behind).
            conn.execute(
                "UPDATE OR IGNORE independent_commits SET evaluator_id = ? WHERE evaluator_id = ?",
                (target_id, source_id),
            )
            if deactivate_source:
                conn.execute("UPDATE users SET active = 0 WHERE id = ?", (source_id,))
        return {
            "source_email": source_user.get("email"),
            "target_email": target_user.get("email"),
            "submissions_moved": len(submission_ids),
            "records_rewritten": records_rewritten,
            "target_id_hashed": block.get("id_hashed"),
            "source_deactivated": bool(deactivate_source),
        }

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

    # ─── Frontier-model baselines + failure capture (FEAT-1) ─────────────────
    def insert_baseline_run(
        self, *, task_id: str, model: str, response_text: Optional[str],
        error: Optional[str] = None, latency_ms: Optional[int] = None,
        tokens_in: Optional[int] = None, tokens_out: Optional[int] = None,
    ) -> Dict[str, Any]:
        rid = _new_id("bl")
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO baseline_runs
                   (run_id, task_id, model, response_text, error, latency_ms,
                    tokens_in, tokens_out, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (rid, task_id, model, response_text, error, latency_ms,
                 tokens_in, tokens_out, _utcnow_iso()),
            )
        return self.get_baseline_run(rid)  # type: ignore[return-value]

    def get_baseline_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM baseline_runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_baseline_runs(
        self, *, task_id: Optional[str] = None, model: Optional[str] = None, limit: int = 1000
    ) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if task_id:
            clauses.append("task_id = ?"); params.append(task_id)
        if model:
            clauses.append("model = ?"); params.append(model)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM baseline_runs {where} ORDER BY created_at DESC LIMIT ?", tuple(params)
            ).fetchall()
        return [dict(r) for r in rows]

    def insert_model_failure(
        self, *, task_id: str, submission_id: str, model: str, verdict: Optional[str],
        error_tags: List[str], corrected_steps: List[Dict[str, Any]],
        expert_correction: Optional[str], prompt: Optional[str],
    ) -> str:
        fid = _new_id("mf")
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO model_failures
                   (failure_id, task_id, submission_id, model, verdict, error_tags_json,
                    corrected_steps_json, expert_correction, prompt, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (fid, task_id, submission_id, model, verdict, json.dumps(error_tags or []),
                 json.dumps(corrected_steps or []), expert_correction, prompt, _utcnow_iso()),
            )
        return fid

    @staticmethod
    def _model_failure_row(row: sqlite3.Row) -> Dict[str, Any]:
        rec = dict(row)
        rec["error_tags"] = json.loads(rec.pop("error_tags_json", "[]") or "[]")
        rec["corrected_steps"] = json.loads(rec.pop("corrected_steps_json", "[]") or "[]")
        return rec

    def list_model_failures(
        self, *, model: Optional[str] = None, error_tag: Optional[str] = None, limit: int = 1000
    ) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if model:
            clauses.append("model = ?"); params.append(model)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM model_failures {where} ORDER BY created_at DESC LIMIT ?", tuple(params)
            ).fetchall()
        out = [self._model_failure_row(r) for r in rows]
        if error_tag:
            out = [f for f in out if error_tag in (f.get("error_tags") or [])]
        return out

    def model_failure_summary(self) -> List[Dict[str, Any]]:
        """Per-model failure counts + the error-tag mix — the datasheet/admin
        headline ("GPT-5.5 failed N cases; top tags …")."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT model, COUNT(*) AS n FROM model_failures GROUP BY model ORDER BY n DESC"
            ).fetchall()
        out = []
        for r in rows:
            tags: Dict[str, int] = {}
            for f in self.list_model_failures(model=r["model"]):
                for t in f.get("error_tags") or []:
                    tags[t] = tags.get(t, 0) + 1
            out.append({"model": r["model"], "failures": int(r["n"]), "error_tags": tags})
        return out

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
