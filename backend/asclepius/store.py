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
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from passlib.context import CryptContext

_pwd = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


def _utcnow_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


def _iso_minus_seconds(seconds: int) -> str:
    """ISO timestamp ``seconds`` in the past — the cutoff for age-based sweeps
    (e.g. reconciling sealed keys unbound longer than an hour)."""
    return (datetime.utcnow() - timedelta(seconds=max(0, seconds))).replace(
        microsecond=0).isoformat()


def _needs_naming(raw: Optional[str]) -> str:
    """Label a backfilled health system that has no real organization name.

    A historical partner row may carry only an internal user id (``u_abc123``)
    or a contact email. Neither is an organization, and rendering one in the
    Health Systems table as though it were makes the operator believe a hospital
    is called that. Mark it instead, so it is obviously waiting to be named."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    looks_internal = raw.lower().startswith(("u_", "u-")) or "@" in raw
    return f"Unnamed partner ({raw})" if looks_internal else raw


def _legacy_partner_name(label: str) -> Optional[str]:
    """The raw name a previous release would have used for this label, or None.

    Only meaningful for the ``Unnamed partner (…)`` form: it lets the boot
    migration find and rename a row created before C-5.6 instead of inserting a
    duplicate beside it."""
    m = re.fullmatch(r"Unnamed partner \((.+)\)", label or "")
    return m.group(1) if m else None


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _empirical_difficulty_fields(ed):
    """Map a generation ``empirical_difficulty`` block → (value, measured_int) for the
    first-class task columns (PRD §9). Accepts a dict {value, declared, measured} or a
    bare number. The stored value is the MEASURED value when measured, else the declared
    value (for display); ``measured`` is 1 only for a live frontier measurement."""
    if isinstance(ed, (int, float)):
        return float(ed), 0
    if not isinstance(ed, dict):
        return None, 0
    measured = 1 if ed.get("measured") else 0
    val = ed.get("value")
    if val is None:
        val = ed.get("declared")
    try:
        val = float(val) if val is not None else None
    except (TypeError, ValueError):
        val = None
    return val, measured


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

                -- V4 image asset index (V4 Image Embedding PRD §4). Resolves an
                -- asset_id → sha256/mime/owning-task in ONE indexed lookup so serving
                -- an image never scans the tasks table. The image BYTES live in the
                -- content-addressed asset store, never here — only the reference.
                CREATE TABLE IF NOT EXISTS study_assets (
                    asset_id    TEXT PRIMARY KEY,
                    sha256      TEXT NOT NULL,
                    mime        TEXT NOT NULL,
                    task_id     TEXT,
                    case_source TEXT,
                    created_at  TEXT NOT NULL
                );

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
            # Serving hot path (Audit §21.6): next_task_for_evaluator /
            # eligible_tasks_for_evaluator run a NOT EXISTS correlated on ic.task_id to
            # exclude blocking-review cases. Index task_id so that stays O(log n).
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ingest_cases_task ON ingest_cases(task_id)")

            # ── Sealed ground truth (Buyer Response PRD §3 B1) ───────────────
            # The partner's adjudicated answer key, held SEPARATELY from the case,
            # ENCRYPTED at rest (field_crypto), keyed to the ingest case, readable
            # only by the adjudication surface, audited on ingest and on every read.
            # It must never enter the case body, task.prompt, or any export profile.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sealed_ground_truth (
                    sealed_id      TEXT PRIMARY KEY,
                    -- NULLABLE on purpose (Audit §H1): the key is STAGED first, keyed
                    -- on (upload_id, patient_key), then bound to the case row once it
                    -- exists. A crash between the two leaves the key on disk unbound,
                    -- never an ingested case with no answer key.
                    ingest_case_id TEXT,
                    upload_id      TEXT,
                    patient_key    TEXT,            -- staging key before binding
                    payload_enc    TEXT NOT NULL,   -- field_crypto-encrypted JSON
                    created_at     TEXT NOT NULL,
                    bound_at       TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sealed_case ON sealed_ground_truth(ingest_case_id)")
            # NOTE: the (upload_id, patient_key) staging index is created in _migrate,
            # AFTER the guarded rebuild adds patient_key — an existing DB has not been
            # migrated yet at this point, so the column may not exist here.

            # ── Frontier-model failure capture (FEAT-1) ──────────────────────
            # ``baseline_runs``: a frontier model's VERBATIM cold answer to a case,
            # the on-policy artifact that proves a case is hard.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS baseline_runs (
                    run_id        TEXT PRIMARY KEY,
                    task_id       TEXT NOT NULL,
                    model         TEXT NOT NULL,
                    provider      TEXT,
                    prompt_hash   TEXT,
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
            _br_cols = cols("baseline_runs")
            if "provider" not in _br_cols:
                conn.execute("ALTER TABLE baseline_runs ADD COLUMN provider TEXT")
            if "prompt_hash" not in _br_cols:
                conn.execute("ALTER TABLE baseline_runs ADD COLUMN prompt_hash TEXT")
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
                    provider        TEXT,
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
            if "provider" not in cols("model_failures"):
                conn.execute("ALTER TABLE model_failures ADD COLUMN provider TEXT")

            # ── V5 Clinical RL Environments (Clinical RL Environments PRD §10).
            # ONE row per environment OR per rollout run, keyed by run_id. A
            # ``mode='generated'`` row is a compiled environment (compiled_json set,
            # no trajectory); a ``mode='rollout'`` row is one agent trajectory over
            # that environment (provider + trajectory_json set), sharing task_id.
            # Fully additive; never touches the V1–V4 single-turn task queue.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS env_runs (
                    run_id                   TEXT PRIMARY KEY,
                    task_id                  TEXT NOT NULL,   -- the environment id (shared across providers)
                    specialty                TEXT NOT NULL DEFAULT 'general',
                    task_type                TEXT NOT NULL DEFAULT 'diagnostic_workup',
                    case_id                  TEXT,
                    case_source              TEXT NOT NULL DEFAULT 'gold',
                    provider                 TEXT,
                    model                    TEXT,
                    ab_source                TEXT,
                    mode                     TEXT NOT NULL DEFAULT 'generated',
                    compiled_json            TEXT,            -- the compiled environment spec (§8.4)
                    trajectory_json          TEXT,            -- the §1 record's trajectory
                    verification_json        TEXT,            -- §5 reward block
                    provenance_json          TEXT,
                    physician_annotation_json TEXT,           -- §7 latest/primary annotation
                    annotations_json         TEXT,            -- ALL annotator submissions (κ subset)
                    empirical_difficulty     REAL,
                    difficulty_measured      INTEGER NOT NULL DEFAULT 0,
                    passes_difficulty_gate   INTEGER,
                    created_at               TEXT NOT NULL,
                    updated_at               TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_env_runs_task ON env_runs(task_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_env_runs_specialty ON env_runs(specialty)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_env_runs_mode ON env_runs(mode)")
            # annotations_json added after the table shipped — guard for existing DBs.
            if "annotations_json" not in cols("env_runs"):
                conn.execute("ALTER TABLE env_runs ADD COLUMN annotations_json TEXT")

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
            # Decisive action (Buyer Response PRD §9.2 / Audit §13): the physician-named
            # verifiable outcome that turns a preference label into an RLVR reward.
            # supervision.DecisiveAction + packaging read it, but nothing wrote it —
            # so has_verifiable_outcome was false on every record. Persisted from the
            # submission, keyed to the task.
            if "decisive_action_json" not in task_cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN decisive_action_json TEXT")
            # Buyer Response PRD §7 F1: an agreement observation records whether the
            # second annotator was BLINDED. Only blinded observations enter the κ
            # computation (an unblinded second rater measures anchoring, not
            # agreement). NULLABLE with NO DEFAULT (Audit §H2): a legacy row whose
            # blinding was never verified stays NULL and is EXCLUDED from κ — not
            # silently asserted blinded. Every new observation is written with an
            # explicit 0/1 by insert_agreement, so only pre-flag rows are NULL.
            if "blinded" not in cols("agreement"):
                conn.execute("ALTER TABLE agreement ADD COLUMN blinded INTEGER")

            # Sealed-key ordering (Audit §H1). The original table declared
            # ingest_case_id NOT NULL, which forced "insert case → then store key" and
            # left a crash-window where an ingested case had no answer key. Relax it to
            # nullable + add the (upload_id, patient_key) staging columns by rebuilding
            # the table (SQLite can't ALTER a NOT NULL away).
            #
            # CRASH-IDEMPOTENT by construction: DDL auto-commits in sqlite3 (it does NOT
            # roll back with the surrounding transaction), so a rebuild interrupted at
            # boot must self-heal on the next boot without ever losing a sealed key. We
            # build a ``_new`` copy (a COMPLETE copy — CREATE + INSERT run back-to-back),
            # then drop+rename. Recovery invariant: whichever of the two tables HAS ROWS
            # is authoritative. (Note ``_init_schema`` runs before this and recreates a
            # missing ``sealed_ground_truth`` as an empty new-schema table, so "original
            # gone" presents as "main table empty" — hence the row-count test, not a
            # table-existence test.)
            tbls = {r["name"] for r in
                    conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "sealed_ground_truth_new" in tbls:
                main_n = (conn.execute("SELECT COUNT(*) FROM sealed_ground_truth").fetchone()[0]
                          if "sealed_ground_truth" in tbls else 0)
                if main_n == 0:
                    # Main is empty — either a fresh recreation (the keys are in _new)
                    # or there were never any keys; swap _new in, losing nothing.
                    conn.execute("DROP TABLE IF EXISTS sealed_ground_truth")
                    conn.execute("ALTER TABLE sealed_ground_truth_new RENAME TO sealed_ground_truth")
                else:
                    # Main holds the pre-migration rows → it is authoritative; _new is a
                    # partial/stale copy. Discard it and let the rebuild below redo it.
                    conn.execute("DROP TABLE sealed_ground_truth_new")
            if "patient_key" not in cols("sealed_ground_truth"):
                conn.execute(
                    """
                    CREATE TABLE sealed_ground_truth_new (
                        sealed_id      TEXT PRIMARY KEY,
                        ingest_case_id TEXT,
                        upload_id      TEXT,
                        patient_key    TEXT,
                        payload_enc    TEXT NOT NULL,
                        created_at     TEXT NOT NULL,
                        bound_at       TEXT
                    )
                    """
                )
                conn.execute(
                    """INSERT INTO sealed_ground_truth_new
                       (sealed_id, ingest_case_id, upload_id, patient_key, payload_enc,
                        created_at, bound_at)
                       SELECT sealed_id, ingest_case_id, upload_id, NULL, payload_enc,
                              created_at, created_at
                         FROM sealed_ground_truth"""
                )
                # Drop-then-rename: if a crash lands between these two, the recovery
                # clause above finishes the rename next boot (keys are safe in _new).
                conn.execute("DROP TABLE sealed_ground_truth")
                conn.execute("ALTER TABLE sealed_ground_truth_new RENAME TO sealed_ground_truth")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_sealed_case ON sealed_ground_truth(ingest_case_id)")
            # The staging index is created here (not in _init_schema) so it lands after
            # the column exists on both fresh (created above) and migrated DBs. Idempotent.
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sealed_stage ON sealed_ground_truth(upload_id, patient_key)")

            # Admin review queue (Audit PRD §21). review_status is NULLABLE on purpose:
            # NULL means "no check raised anything", which is NOT "reviewed and cleared"
            # — the same fail-open lesson as the blinded flag; never backfill a verdict
            # onto rows that were never assessed.
            if "review_status" not in cols("ingest_cases"):
                conn.execute("ALTER TABLE ingest_cases ADD COLUMN review_status TEXT")     # NULL|needs_review|cleared|rejected
                conn.execute("ALTER TABLE ingest_cases ADD COLUMN review_json TEXT")       # [{reason,severity,detail,raised_at}]
                conn.execute("ALTER TABLE ingest_cases ADD COLUMN reviewed_by_hashed TEXT")
                conn.execute("ALTER TABLE ingest_cases ADD COLUMN reviewed_at TEXT")

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
            # Specialty Hyper-Personalization PRD §9: the frontier-model failure rate
            # (wrong answer OR wrong reasoning). First-class columns so the serving gate
            # filters in SQL and admin/export can surface it. ``difficulty_measured`` =
            # 1 only when LIVE-measured; a declared/authored value carries the numeric
            # for display but 0 here. Each ALTER is guarded independently so a crash
            # between the two can't leave insert_task referencing a missing column.
            if "empirical_difficulty" not in task_cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN empirical_difficulty REAL")
            if "difficulty_measured" not in task_cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN difficulty_measured INTEGER NOT NULL DEFAULT 0")
            if "empirical_difficulty" not in task_cols or "difficulty_measured" not in task_cols:
                for r in conn.execute(
                    "SELECT task_id, generation_json FROM tasks WHERE generation_json IS NOT NULL"
                ).fetchall():
                    try:
                        ed = (json.loads(r["generation_json"]) or {}).get("empirical_difficulty")
                        val, measured = _empirical_difficulty_fields(ed)
                    except Exception:
                        val, measured = None, 0
                    if val is not None or measured:
                        conn.execute(
                            "UPDATE tasks SET empirical_difficulty = ?, difficulty_measured = ? WHERE task_id = ?",
                            (val, measured, r["task_id"]),
                        )

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
                # First-run tutorial ("Calibration Case 1") state. JSON blob —
                # {status, step, version, started_at, completed_at, skipped_at,
                # score} — because the shape evolves and nothing filters on it
                # in SQL (same reasoning as credentials_json above).
                ("tutorial_json", "TEXT"),
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
            # Retain-raw (Audit §9.4): when every entry in a bundle failed to parse,
            # the raw blob is kept past the normal retention window so it can be re-run
            # after a parser fix rather than purged on schedule.
            if "retain_raw" not in cols("ingest_uploads"):
                conn.execute("ALTER TABLE ingest_uploads ADD COLUMN retain_raw INTEGER NOT NULL DEFAULT 0")

            # ═══ PRD-B IDENTITY SCHEMA — owned by Agent 2, do not edit from other PRDs ═══
            # Verification / credentialing / tiering columns (§4 shared contract).
            # All nullable and deliberately WITHOUT defaults: on every status
            # column NULL means "not yet checked / not yet decided" and must stay
            # distinguishable from a decided value — a DEFAULT here would mark
            # every pre-existing user as pending/approved, which is wrong.
            for _col, _ddl in (
                ("phone",               "TEXT"),
                # Enumerated ONCE in asclepius/capabilities.py (TIERS). A stale
                # comment here is how the next person reintroduces a two-state
                # check, so it is kept in sync deliberately.
                ("tier",                "TEXT"),     # labeler | reviewer | advisor | NULL(unassigned)
                ("tier_score",          "REAL"),
                ("tier_assigned_at",    "TEXT"),
                ("tier_assigned_by",    "TEXT"),
                ("verification_status", "TEXT"),     # pending | approved | rejected | NULL
                ("verification_notes",  "TEXT"),
                ("verified_by",         "TEXT"),
                ("verified_at",         "TEXT"),
                ("npi_verified",        "INTEGER"),  # 1 | 0 | NULL(not checked)
                ("npi_payload_json",    "TEXT"),
                ("npi_checked_at",      "TEXT"),
                ("email_domain_class",  "TEXT"),     # academic | business | consumer
                ("linkedin_url",        "TEXT"),
                ("cv_asset_sha",        "TEXT"),
                # PRD-B extension beyond the §4 contract: cache of the parsed-CV
                # suggestions so the admin dossier doesn't re-parse (and possibly
                # re-OCR) the document on every view. Advisory data only.
                ("cv_parsed_json",      "TEXT"),
                ("health_system_id",    "TEXT"),     # FK -> health_systems (PRD-C table)
                ("slack_joined",        "INTEGER"),
                ("slack_checked_at",    "TEXT"),
                # F6: a non-definitive NPI check is an ATTEMPT, not a result.
                # It is logged here instead of overwriting npi_payload_json /
                # npi_verified, so a failed recheck can never erase evidence we
                # already hold. Also drives the retry sweep.
                ("npi_last_attempt_json", "TEXT"),
                ("npi_last_attempt_at",   "TEXT"),
            ):
                if _col not in cols("users"):
                    conn.execute(f"ALTER TABLE users ADD COLUMN {_col} {_ddl}")
            # ═══ END PRD-B ═══
            # ═══ PRD-A REVIEW SCHEMA — owned by Agent 1, do not edit from other PRDs ═══
            # Two-tier review product (PRD A §1): senior reviewers grade a labeler's
            # completed submission. One row per review; a submission may carry several.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS case_reviews (
                    review_id          TEXT PRIMARY KEY,
                    task_id            TEXT NOT NULL,
                    submission_id      TEXT NOT NULL,        -- the labeler submission under review
                    reviewer_user_id   TEXT NOT NULL,
                    reviewer_id_hashed TEXT NOT NULL,
                    verdict            TEXT NOT NULL,        -- accept | accept_with_edits | reject
                    dimension_json     TEXT,                 -- per-dimension scores
                    corrections_json   TEXT,                 -- reviewer's edits
                    reviewer_notes     TEXT,
                    time_spent_sec     INTEGER,
                    blinded            INTEGER,              -- 1 only when the reviewer saw no labeler identity
                    created_at         TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_review_task ON case_reviews(task_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_review_sub  ON case_reviews(submission_id)")
            # Review lifecycle on the labeler submission. NULL = not yet routed /
            # unreviewed; 'in_review' = claimed by a reviewer; 'reviewed' = at least
            # one review submitted. Deliberately NO DEFAULT — NULL ("not yet decided")
            # must stay distinguishable from any decided value (START_HERE §4).
            if "review_status" not in cols("submissions"):
                conn.execute("ALTER TABLE submissions ADD COLUMN review_status TEXT")
            # Review CLAIM state (FIX A Phases 2/3). Three separate columns, all
            # nullable, none defaulted:
            #   review_claimed_by  — who holds the lease, so a second reviewer
            #     cannot silently evict in-flight work by POSTing a guessed id.
            #   review_claimed_at  — the lease clock. Dedicated on purpose:
            #     ``updated_at`` is bumped by ANY write, so a background pipeline
            #     touching the submission used to silently extend a reviewer's lease.
            #   review_blinded     — the blinding DERIVED from the payload actually
            #     served at draw time (1/0/NULL = never asserted). Read back at
            #     submit; never recomputed from a payload we are no longer serving.
            if "review_claimed_by" not in cols("submissions"):
                conn.execute("ALTER TABLE submissions ADD COLUMN review_claimed_by TEXT")
            if "review_claimed_at" not in cols("submissions"):
                conn.execute("ALTER TABLE submissions ADD COLUMN review_claimed_at TEXT")
            if "review_blinded" not in cols("submissions"):
                conn.execute("ALTER TABLE submissions ADD COLUMN review_blinded INTEGER")
            # next_review_for filters on review_status and review_queue_stats runs
            # four COUNT(*)s over it on every draw — unindexed, that is four full
            # table scans per reviewer per case (FIX A A-3.5).
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sub_review_status ON submissions(review_status)")
            # Safe-Harbor identifier kinds found in the reviewer's own free text
            # at insert time (FIX A A-5.3). Tri-state: NULL = never scanned
            # (legacy row), '[]' = scanned clean, '["name",...]' = flagged, in
            # which case the free text is withheld from the buyer-facing block.
            if "identifier_flags" not in cols("case_reviews"):
                conn.execute("ALTER TABLE case_reviews ADD COLUMN identifier_flags TEXT")
            # ═══ END PRD-A ═══
            # ═══ PRD-C HEALTH SYSTEM SCHEMA — owned by Agent 3, do not edit from other PRDs ═══
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS health_systems (
                    hs_id         TEXT PRIMARY KEY,          -- hs-<slug>-<6hex>
                    name          TEXT NOT NULL,
                    contact_email TEXT,
                    notes         TEXT,
                    active        INTEGER NOT NULL DEFAULT 1,
                    created_at    TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS hs_portal_users (
                    username      TEXT PRIMARY KEY,
                    hs_id         TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    must_reset    INTEGER NOT NULL DEFAULT 1,
                    email         TEXT,
                    last_login    TEXT,
                    active        INTEGER NOT NULL DEFAULT 1,
                    created_at    TEXT NOT NULL
                )
                """
            )
            # Login-lockout bookkeeping — the portal is public on launch day, so a
            # per-account lock (not just per-IP throttling) is required. Guarded
            # ALTERs so a DB created from the bare contract table also gains them.
            if "failed_logins" not in cols("hs_portal_users"):
                conn.execute("ALTER TABLE hs_portal_users ADD COLUMN failed_logins INTEGER NOT NULL DEFAULT 0")
            if "locked_until" not in cols("hs_portal_users"):
                conn.execute("ALTER TABLE hs_portal_users ADD COLUMN locked_until TEXT")
            if "health_system_id" not in cols("ingest_uploads"):
                conn.execute("ALTER TABLE ingest_uploads ADD COLUMN health_system_id TEXT")
            # Session invalidation (FIX-C C-2.3): a token minted before the last
            # password change must stop working immediately. Stamped on every
            # password write and carried as a token claim.
            if "password_changed_at" not in cols("hs_portal_users"):
                conn.execute("ALTER TABLE hs_portal_users ADD COLUMN password_changed_at TEXT")
            # The session binding is this COUNTER, not the timestamp beside it:
            # ``_utcnow_iso()`` truncates to whole seconds, so a password change
            # within the same second as a session's issuance would produce an
            # identical stamp and silently fail to invalidate that session. A
            # monotonic epoch cannot collide no matter how fast the clock ticks.
            if "session_epoch" not in cols("hs_portal_users"):
                conn.execute("ALTER TABLE hs_portal_users ADD COLUMN "
                             "session_epoch INTEGER NOT NULL DEFAULT 0")
            # Brute-force bookkeeping keyed on (username, ip) — ONE table for
            # known and unknown usernames alike (FIX-C C-2.1/C-2.2). Two separate
            # mechanisms is what produced the enumeration oracle: the in-memory
            # unknown-user path recorded a failure AFTER checking the threshold
            # while the DB path recorded BEFORE, so the 5th attempt returned 429
            # for a real account and 401 for a fake one. A single code path
            # cannot drift like that. Scoping the hard lock to the IP as well as
            # the username also stops a remote attacker from locking a hospital
            # out of its own portal with five wrong guesses.
            #
            # DB-backed rather than a process dict so the threshold does not
            # silently become N x the intended value when the app scales out to
            # N workers — which would re-open C-2.1.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS hs_login_attempts (
                    attempt_key   TEXT PRIMARY KEY,   -- username|sha256(ip)[:16]
                    username      TEXT NOT NULL,
                    fails         INTEGER NOT NULL DEFAULT 0,
                    first_fail_at TEXT,
                    last_fail_at  TEXT,
                    locked_until  TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_hs_login_attempts_user "
                         "ON hs_login_attempts(username)")
            # Logout must actually end the session on a shared hospital
            # workstation, so the token's jti goes on a denylist until it would
            # have expired anyway (FIX-C C-2.3).
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS hs_revoked_tokens (
                    jti        TEXT PRIMARY KEY,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT NOT NULL
                )
                """
            )
            self._backfill_health_systems(conn)
            # ═══ END PRD-C ═══
            # ═══ PRD-D ADVISOR SCHEMA — owned by the advisor tier, do not edit from other PRDs ═══
            # The third physician tier (Advisor PRD). Guarded ALTERs, and — the
            # rule that holds everywhere in this file — NO DEFAULT on any status
            # column: NULL means "not decided / not applicable" and must stay
            # distinguishable from a decided value.
            for _col, _ddl in (
                # per_task | equity_only | NULL(unset). NULL is payable — see
                # asclepius/compensation.py for why that is not a default.
                ("compensation_model",    "TEXT"),
                ("advisor_since",         "TEXT"),
                ("advisor_agreement_ref", "TEXT"),  # signed advisor agreement on file
                ("referral_code",         "TEXT"),  # unique, minted on appointment
                ("slack_role",            "TEXT"),  # 'Medical Advisor' — the label, not a bool
            ):
                if _col not in cols("users"):
                    conn.execute(f"ALTER TABLE users ADD COLUMN {_col} {_ddl}")
            # A referral code is a claim on attribution, so two advisors must
            # never hold the same one. Partial index: NULL is the normal state
            # for the ~50 physicians who are not advisors, and SQLite would
            # otherwise treat every one of those NULLs as distinct anyway — the
            # WHERE clause states the intent rather than relying on that.
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_referral_code "
                "ON users(referral_code) WHERE referral_code IS NOT NULL")

            # Referrals (Advisor PRD §3.1). One row per invited physician.
            # ``status`` is nullable with no DEFAULT: NULL means "we have not
            # heard back", which is emphatically not "declined".
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS referrals (
                    referral_id     TEXT PRIMARY KEY,
                    referrer_id     TEXT NOT NULL,   -- users.id of the advisor
                    referral_code   TEXT NOT NULL,
                    invitee_email   TEXT,
                    invitee_name    TEXT,
                    note            TEXT,            -- "knows her from Stanford ortho"
                    status          TEXT,            -- invited|signed_up|verified|approved|declined|NULL
                    invited_at      TEXT NOT NULL,
                    user_id         TEXT,            -- set when the invitee actually signs up
                    resolved_at     TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_referrals_code ON referrals(referral_code)")
            # Resolution is by invitee email at provision time, so that lookup
            # must not be a full scan of a growing table.
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_referrals_email ON referrals(invitee_email)")

            # Advisory sign-offs (Advisor PRD §4.1). ONE mechanism, four
            # artifacts, discriminated by ``artifact_type`` — not four features
            # with four UIs and four permission checks to forget to update.
            #
            # ``relationship`` is written by the SERVER and never accepted from
            # the client (§0.2): an advisor holding equity who attests that a
            # batch is good enough to ship is a related-party attestation, and
            # recording the relationship next to the verdict converts something
            # a buyer could discover into something we disclosed.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS advisory_signoffs (
                    signoff_id     TEXT PRIMARY KEY,
                    artifact_type  TEXT NOT NULL,  -- task_batch|export_bundle|inbound_upload|product_spec
                    artifact_id    TEXT NOT NULL,
                    advisor_id     TEXT NOT NULL,
                    verdict        TEXT NOT NULL,  -- approved|approved_with_comments|changes_requested
                    comments       TEXT,
                    relationship   TEXT NOT NULL,  -- 'advisor_equity' — see PRD §0.2
                    created_at     TEXT NOT NULL
                )
                """
            )
            # What the advisor actually signed off ON (audit M3). A task_batch
            # id is a DERIVED key (specialty:YYYY-MM-DD over status='open'), so
            # its membership changes after the fact: tasks generated later the
            # same day join the batch and silently inherit an approval nobody
            # gave them. Resolving the member ids at write time makes the
            # subject of an attestation reconstructible, which is the whole
            # point of recording one.
            if "subject_ids" not in cols("advisory_signoffs"):
                conn.execute("ALTER TABLE advisory_signoffs ADD COLUMN subject_ids TEXT")
            if "subject_n" not in cols("advisory_signoffs"):
                conn.execute("ALTER TABLE advisory_signoffs ADD COLUMN subject_n INTEGER")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_signoff_artifact "
                "ON advisory_signoffs(artifact_type, artifact_id)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_signoff_advisor "
                "ON advisory_signoffs(advisor_id)")

            # Product specs (Advisor PRD §4.2, artifact_type='product_spec'): a
            # markdown document an admin puts up for advisory comment. Kept
            # deliberately thin — it is a document with a title, not a CMS.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS product_specs (
                    spec_id     TEXT PRIMARY KEY,
                    title       TEXT NOT NULL,
                    body_md     TEXT NOT NULL,
                    created_by  TEXT,
                    created_at  TEXT NOT NULL
                )
                """
            )

            # Sign-off state, surfaced next to the thing it describes. Recorded
            # and shown in admin — it NEVER blocks a build or a shipment. One
            # advisor with a day job must not sit on the revenue path, so an
            # export always builds and always ships; the advisor's verdict and
            # comments ride alongside it as feedback.
            # Tri-state and no DEFAULT: NULL = nobody has looked yet, which is a
            # different fact from 'approved' and from 'changes_requested'.
            if "signoff_status" not in cols("exports"):
                conn.execute("ALTER TABLE exports ADD COLUMN signoff_status TEXT")
            if "signoff_status" not in cols("tasks"):
                conn.execute("ALTER TABLE tasks ADD COLUMN signoff_status TEXT")
            if "signoff_status" not in cols("ingest_uploads"):
                conn.execute("ALTER TABLE ingest_uploads ADD COLUMN signoff_status TEXT")
            # ═══ END PRD-D ═══
            # ═══ PRD-I INGESTION SCHEMA — owned by Agent I, do not edit from other PRDs ═══
            # Purpose (PRD-I §2.1). 'task_creation' | 'brokering' | NULL.
            #
            # NO DEFAULT, deliberately, and this is the one column where that rule
            # carries real consequence rather than tidiness: NULL means "minted
            # before purpose existed", and the ONLY place it may be read as
            # task_creation is the promotion gate (§4.1). Everywhere else it renders
            # as an unresolved work item, because a legacy link silently defaulting
            # to task_creation is exactly how brokering data would become a task.
            #
            # Purpose lives on the row that AUTHORIZES an upload, and there are two
            # such rows because there are two doors: the magic-link door authorizes
            # on ingest_upload_links, and the health-system portal authorizes on
            # hs_portal_users (it has no link row at all — it carries the 'hs-portal'
            # sentinel link_id). PRD-I §2.1 names only the link table; putting it
            # solely there would leave every hospital-portal upload with no purpose,
            # which is the door the PRD's own §1 handshake is built on.
            for _tbl in ("ingest_upload_links", "hs_portal_users", "ingest_uploads",
                         "ingest_cases"):
                if "purpose" not in cols(_tbl):
                    conn.execute(f"ALTER TABLE {_tbl} ADD COLUMN purpose TEXT")
            # The chain-of-custody triple (PRD-I §1.1). sha256/size_bytes already
            # exist on ingest_uploads; verified_at is what makes them a CLAIM rather
            # than two numbers — it is stamped only after the whole-file digest was
            # recomputed over the assembled bytes and matched the declared value.
            if "verified_at" not in cols("ingest_uploads"):
                conn.execute("ALTER TABLE ingest_uploads ADD COLUMN verified_at TEXT")
            # Resumable chunked upload sessions (PRD-I §1.1). A session is NOT an
            # upload: no ingest_uploads row exists until `complete` verifies the
            # whole-file digest, which is what makes "an assembled file with no
            # verified row is invisible to the application" true by construction
            # rather than by convention.
            #
            # Per-part progress is deliberately NOT stored here — it is derived from
            # the parts on disk. Writing a row per chunk would put a single-writer
            # SQLite under a tight write loop for state the filesystem already holds.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ingest_upload_sessions (
                    session_id      TEXT PRIMARY KEY,
                    owner_kind      TEXT NOT NULL,   -- 'health_system'
                    owner_id        TEXT NOT NULL,
                    actor           TEXT,            -- portal username, for audit
                    filename        TEXT,
                    content_type    TEXT,
                    declared_sha256 TEXT NOT NULL,
                    declared_size   INTEGER NOT NULL,
                    chunk_size      INTEGER NOT NULL,
                    part_count      INTEGER NOT NULL,
                    storage_dir     TEXT NOT NULL,
                    -- No purpose column, deliberately. What an upload is FOR is a
                    -- mutable admin decision, and a session outlives it: 24 h,
                    -- resumable. A snapshot taken at declare is stale for every
                    -- byte that arrives after an admin corrects the mint, and the
                    -- single-request door resolves live — so the two doors would
                    -- record the same bytes differently. ``actor`` is stored and
                    -- everything derived is joined through it at completion.
                    status          TEXT,            -- NULL(open) | completing | verified | failed | aborted
                    upload_id       TEXT,            -- set at complete
                    verified_at     TEXT,
                    created_at      TEXT NOT NULL,
                    updated_at      TEXT NOT NULL
                )
                """
            )
            # Idempotency key (PRD-I §1.1): re-declaring the same bytes from the same
            # ACCOUNT returns the EXISTING session rather than starting a second one,
            # so "the contact refreshed the tab at 3.2 GB" is a non-event. Partial
            # index over OPEN sessions only — a failed session must not block a
            # genuine retry of the same file.
            #
            # ``actor`` is in the key, and that is a correctness requirement rather
            # than a refinement. Purpose lives on the ACCOUNT, and an organization
            # may legitimately hold one account of each kind. Keyed on the health
            # system alone, a brokering account re-declaring the same bytes as a
            # task-creation account would be handed that account's session — and
            # the completed upload would inherit its purpose. A brokering bundle
            # stamped task_creation and filed under Ready to promote: fail-open on
            # the one invariant this whole release exists to hold.
            #
            # The v1 index is dropped rather than left in place: it is UNIQUE, so
            # leaving it would keep enforcing the weaker key and still collapse the
            # two accounts onto one session.
            conn.execute("DROP INDEX IF EXISTS idx_ingest_sessions_idem")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_ingest_sessions_idem_v2 "
                "ON ingest_upload_sessions(owner_kind, owner_id, actor, "
                "declared_sha256, declared_size) WHERE status IS NULL"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ingest_sessions_owner "
                         "ON ingest_upload_sessions(owner_kind, owner_id)")
            # The reaper scans on (status, updated_at); without this it is a full
            # table scan on every declare.
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ingest_sessions_reap "
                         "ON ingest_upload_sessions(status, updated_at)")
            # A DB created before purpose became live-resolved still carries the
            # snapshot column. Drop it rather than leave a stale value that reads
            # as authoritative — a dead column holding a plausible-looking answer
            # is worse than no column. Guarded: DROP COLUMN needs SQLite >= 3.35,
            # and where it is unavailable the column simply stays NULL and unread.
            if "purpose" in cols("ingest_upload_sessions"):
                try:
                    conn.execute("ALTER TABLE ingest_upload_sessions DROP COLUMN purpose")
                except sqlite3.OperationalError:
                    conn.execute("UPDATE ingest_upload_sessions SET purpose = NULL")
            # ═══ END PRD-I ═══

            # ── Specialty-tagged task notifications (outbox + drain) ─────────
            # One row per (recipient, specialty, upload batch): the admin request
            # enqueues these synchronously (fast, transactional) and a background
            # drain sends the emails, so a crash mid-send leaves rows `pending`
            # for the manual re-drain endpoint rather than losing the tail.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_notify_outbox (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key   TEXT NOT NULL UNIQUE,
                    batch_id          TEXT NOT NULL,
                    specialty         TEXT NOT NULL,
                    task_count        INTEGER NOT NULL,
                    recipient_user_id TEXT NOT NULL,
                    recipient_email   TEXT NOT NULL,
                    status            TEXT NOT NULL DEFAULT 'pending',
                    send_attempts     INTEGER NOT NULL DEFAULT 0,
                    last_error        TEXT,
                    sent_at           TEXT,
                    claimed_at        TEXT,
                    created_at        TEXT NOT NULL
                )
                """
            )
            # Older DBs created before the claim/lease model gain claimed_at here.
            if "claimed_at" not in cols("task_notify_outbox"):
                conn.execute("ALTER TABLE task_notify_outbox ADD COLUMN claimed_at TEXT")
            # A drain claims rows by (status, claimed_at); index it so the claim
            # scan stays cheap once the table has a long 'sent' tail.
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_task_notify_status ON task_notify_outbox(status, claimed_at)"
            )

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

    def list_evaluators_by_specialty(self, specialty: str) -> List[Dict[str, Any]]:
        """Active evaluators whose specialty matches, for task-notify recipient
        resolution. Mirrors ``next_task_for_evaluator``'s match (equality on
        ``specialty``), just inverted: users for a specialty instead of a task
        for a user."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM users WHERE role = 'evaluator' AND active = 1 "
                "AND lower(trim(specialty)) = lower(trim(?))",
                (specialty,),
            ).fetchall()
            return [dict(r) for r in rows]

    # ─── Task-notify outbox (specialty-tagged task notifications) ───────────
    #
    # Lifecycle: pending → (claim) sending → sent | failed. A drain CLAIMS rows
    # atomically (pending → sending, stamping claimed_at + bumping send_attempts)
    # so two concurrent drains can never send the same row; the claim also
    # re-picks (a) 'failed' rows under the attempt cap for retry, and (b) stale
    # 'sending' rows whose lease expired (a drain crashed after claiming but
    # before marking) — so nothing is lost and nothing is stuck. Rows that
    # exhaust the attempt cap stay 'failed' as a visible dead-letter.
    def enqueue_task_notification(
        self, *, idempotency_key: str, batch_id: str, specialty: str, task_count: int,
        recipient_user_id: str, recipient_email: str,
    ) -> Optional[int]:
        """Insert one pending outbox row, deduped on ``idempotency_key`` (one
        email per clinician per specialty per upload batch). Returns the new row
        id, or None if this (recipient, specialty, batch) was already enqueued."""
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO task_notify_outbox
                  (idempotency_key, batch_id, specialty, task_count,
                   recipient_user_id, recipient_email, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    idempotency_key, batch_id, specialty, task_count,
                    recipient_user_id, recipient_email, _utcnow_iso(),
                ),
            )
            return int(cur.lastrowid) if cur.rowcount else None

    def enqueue_task_notifications_bulk(self, rows: List[Dict[str, Any]]) -> int:
        """Insert many pending outbox rows in ONE transaction (fan-out to ~1000
        recipients must not be ~1000 separate commits). Deduped on
        ``idempotency_key`` via INSERT OR IGNORE. Returns the count inserted."""
        if not rows:
            return 0
        now = _utcnow_iso()
        params = [
            (
                r["idempotency_key"], r["batch_id"], r["specialty"], int(r["task_count"]),
                r["recipient_user_id"], r["recipient_email"], now,
            )
            for r in rows
        ]
        with self._conn() as conn:
            before = conn.total_changes
            conn.executemany(
                """
                INSERT OR IGNORE INTO task_notify_outbox
                  (idempotency_key, batch_id, specialty, task_count,
                   recipient_user_id, recipient_email, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                params,
            )
            return conn.total_changes - before

    def claim_task_notifications(
        self, *, limit: int = 200, max_attempts: int = 5, lease_seconds: int = 300,
    ) -> List[Dict[str, Any]]:
        """Atomically claim up to ``limit`` deliverable rows for THIS drain:
        marks them 'sending', stamps ``claimed_at``, bumps ``send_attempts``, and
        returns them. Claims pending rows, failed rows still under the attempt
        cap (retry), and stale 'sending' rows whose lease expired (crash
        recovery). Atomic UPDATE ... RETURNING serializes against other drains
        (SQLite write lock), so no row is ever claimed by two drains at once."""
        now_dt = datetime.utcnow().replace(microsecond=0)
        now = now_dt.isoformat()
        lease_cutoff = (now_dt - timedelta(seconds=max(1, lease_seconds))).isoformat()
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE task_notify_outbox
                   SET status = 'sending', claimed_at = ?, send_attempts = send_attempts + 1
                 WHERE id IN (
                     SELECT id FROM task_notify_outbox
                      WHERE status = 'pending'
                         -- failed rows retry only AFTER the lease elapses, so a
                         -- transient failure isn't burned through all attempts
                         -- inside one drain — retries are spaced by ~lease.
                         OR (status = 'failed'  AND send_attempts < ?
                             AND (claimed_at IS NULL OR claimed_at < ?))
                         -- 'sending' rows whose claiming drain crashed (lease expired).
                         OR (status = 'sending' AND (claimed_at IS NULL OR claimed_at < ?))
                      ORDER BY created_at ASC
                      LIMIT ?
                 )
                RETURNING *
                """,
                (now, max_attempts, lease_cutoff, lease_cutoff, limit),
            )
            return [dict(r) for r in cur.fetchall()]

    def list_pending_task_notifications(self, limit: int = 500) -> List[Dict[str, Any]]:
        """Read-only view of still-queued rows (observability / admin). The drain
        uses :meth:`claim_task_notifications`, not this."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM task_notify_outbox WHERE status = 'pending' "
                "ORDER BY created_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def count_task_notifications_by_status(self) -> Dict[str, int]:
        """{'pending': n, 'sending': n, 'sent': n, 'failed': n} for observability.
        Includes a synthetic 'dead_letter' = failed rows past the default cap."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM task_notify_outbox GROUP BY status"
            ).fetchall()
            out: Dict[str, int] = {r["status"]: int(r["n"]) for r in rows}
            dead = conn.execute(
                "SELECT COUNT(*) AS n FROM task_notify_outbox "
                "WHERE status = 'failed' AND send_attempts >= 5"
            ).fetchone()
            out["dead_letter"] = int(dead["n"] if dead else 0)
            return out

    def mark_task_notification_sent(self, notification_id: int) -> None:
        # send_attempts was bumped at claim time; don't double-count here.
        with self._conn() as conn:
            conn.execute(
                "UPDATE task_notify_outbox SET status = 'sent', sent_at = ?, "
                "claimed_at = NULL WHERE id = ?",
                (_utcnow_iso(), notification_id),
            )

    def mark_task_notification_failed(self, notification_id: int, error: str) -> None:
        # Keep claimed_at as the retry-backoff anchor: the claim re-offers a
        # failed row only once its lease has elapsed, so a transient failure
        # isn't burned through all attempts inside a single drain. Past the
        # attempt cap the row stays 'failed' as a terminal dead-letter.
        with self._conn() as conn:
            conn.execute(
                "UPDATE task_notify_outbox SET status = 'failed', last_error = ?, "
                "claimed_at = ? WHERE id = ?",
                ((error or "")[:2000], _utcnow_iso(), notification_id),
            )

    # ─── Real EHR ingestion (EHR PRD §4, §5, §8) ─────────────────────────────
    def create_upload_link(
        self, *, token_hash: str, partner_id: str, partner_label: Optional[str],
        specialty: str, expires_at: str, one_time: bool, max_bytes: int,
        created_by: Optional[str], contact_email: Optional[str] = None,
        purpose: Optional[str] = None,
    ) -> Dict[str, Any]:
        lid = _new_id("lnk")
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO ingest_upload_links
                   (link_id, token_hash, partner_id, partner_label, specialty,
                    expires_at, one_time, max_bytes, created_by, contact_email,
                    purpose, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (lid, token_hash, partner_id, partner_label, specialty, expires_at,
                 1 if one_time else 0, int(max_bytes), created_by,
                 (contact_email or None), purpose, _utcnow_iso()),
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
        allowed = {"status", "reason", "files_json", "raw_path", "retain_raw"}
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

    def list_ingest_uploads(self, *, limit: int = 200, offset: int = 0,
                            status: Optional[str] = None) -> List[Dict[str, Any]]:
        where, params = "", []
        if status:
            where = "WHERE status = ? "
            params.append(status)
        params += [max(1, limit), max(0, offset)]
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM ingest_uploads {where}ORDER BY created_at DESC LIMIT ? OFFSET ?",
                tuple(params),
            ).fetchall()
        out = []
        for r in rows:
            rec = dict(r)
            rec["files"] = json.loads(rec.pop("files_json") or "[]")
            out.append(rec)
        return out

    def count_ingest_uploads(self, *, status: Optional[str] = None) -> int:
        """Total upload rows — lets the admin UI paginate over full history.
        With ``status`` set, counts only rows in that state (drives the filter chips)."""
        with self._conn() as conn:
            if status:
                return int(conn.execute(
                    "SELECT COUNT(*) FROM ingest_uploads WHERE status = ?", (status,)
                ).fetchone()[0])
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
        review_status: Optional[str] = None, review_json: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        cid = _new_id("icase")
        now = _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO ingest_cases
                   (ingest_case_id, upload_id, patient_key, specialty, case_json,
                    status, report_json, review_status, review_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (cid, upload_id, patient_key, specialty,
                 json.dumps(case) if case else None, status,
                 json.dumps(report) if report else None, review_status,
                 json.dumps(review_json) if review_json else None, now, now),
            )
        return self.get_ingest_case(cid)  # type: ignore[return-value]

    def update_ingest_case(self, ingest_case_id: str, **fields: Any) -> None:
        allowed = {"status", "case_json", "report_json", "task_id", "override_reason",
                   "review_status", "review_json", "reviewed_by_hashed", "reviewed_at"}
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k in ("case_json", "report_json", "review_json") and not isinstance(v, (str, type(None))):
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

    # ─── Sealed ground truth (Buyer Response PRD §3 B1) ──────────────────────
    def insert_sealed_ground_truth(
        self, *, ingest_case_id: str, upload_id: Optional[str],
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Store the adjudicated answer key ENCRYPTED at rest, keyed to the ingest
        case. Same field_crypto pattern as the raw partner blob — the payload is
        never written in cleartext when a key is configured.

        NOTE (Audit §H1): the crash-safe ingest path stages then binds
        (``stage_sealed_ground_truth`` → ``bind_sealed_ground_truth``). This
        one-shot insert is retained for callers that already have a case id."""
        from field_crypto import encrypt_field
        sid = _new_id("sealed")
        now = _utcnow_iso()
        blob = encrypt_field(json.dumps(payload))
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO sealed_ground_truth
                   (sealed_id, ingest_case_id, upload_id, payload_enc, created_at, bound_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (sid, ingest_case_id, upload_id, blob, now, now),
            )
        return {"sealed_id": sid, "ingest_case_id": ingest_case_id}

    def stage_sealed_ground_truth(
        self, *, upload_id: Optional[str], patient_key: Optional[str],
        payload: Dict[str, Any],
    ) -> str:
        """Stage the sealed answer key BEFORE the case row exists (Audit §H1), keyed
        on (upload_id, patient_key) with a NULL ingest_case_id. Encrypted at rest,
        exactly like the bound path. Returns the ``sealed_id`` to bind once the case
        is inserted. A crash after this and before binding leaves the key on disk,
        unbound — the strictly better failure than an ingested case with no key."""
        from field_crypto import encrypt_field
        sid = _new_id("sealed")
        now = _utcnow_iso()
        blob = encrypt_field(json.dumps(payload))
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO sealed_ground_truth
                   (sealed_id, ingest_case_id, upload_id, patient_key, payload_enc,
                    created_at, bound_at)
                   VALUES (?, NULL, ?, ?, ?, ?, NULL)""",
                (sid, upload_id, patient_key, blob, now),
            )
        return sid

    def bind_sealed_ground_truth(self, sealed_id: str, ingest_case_id: str) -> None:
        """Bind a staged key to its case row (Audit §H1). Idempotent; a bind that
        never lands leaves the row unbound for reconciliation to catch."""
        now = _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                "UPDATE sealed_ground_truth SET ingest_case_id = ?, bound_at = ? "
                "WHERE sealed_id = ?",
                (ingest_case_id, now, sealed_id),
            )

    def hold_ingest_case_for_review(self, ingest_case_id: str, reason: str,
                                    detail: str, *, severity: str = "blocking") -> bool:
        """Add a review reason to an already-terminal case and hold it out of the
        annotation queue (Audit §9.3). Idempotent per reason code; returns True if a
        new reason was added. Used by post-hoc reconciliation (unbound sealed key,
        missing asset blob) to flag a case that reached a terminal state but is
        internally inconsistent — a defect the ingest-time checks could not see."""
        existing = self.get_ingest_case(ingest_case_id)
        if not existing:
            return False
        reasons = list(existing.get("review") or [])
        if any(x.get("reason") == reason for x in reasons):
            return False
        reasons.append({"reason": reason, "severity": severity, "detail": detail,
                        "raised_at": _utcnow_iso()})
        fields: Dict[str, Any] = {"review_status": "needs_review", "review_json": reasons}
        # A blocking reason also flips the case status so the queue-hold SQL excludes
        # it; an advisory one leaves the case where it is.
        if severity == "blocking" and existing.get("status") == "ingested":
            fields["status"] = "needs_review"
        self.update_ingest_case(ingest_case_id, **fields)
        return True

    def reconcile_sealed_ground_truth(self, *, older_than_seconds: int = 3600) -> Dict[str, Any]:
        """Recover sealed keys left UNBOUND by a crash between staging and binding
        (Audit §H1). For each unbound row older than the threshold, try to bind it to
        its case by (upload_id, patient_key); if the case is found, bind it and raise a
        blocking ``sealed_key_unbound`` review reason on that case (a human confirms the
        recovered key before the case is annotated). A row with no matching case is
        reported as an orphan — the key exists but its case never landed."""
        cutoff = _iso_minus_seconds(older_than_seconds)
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT sealed_id, upload_id, patient_key, created_at "
                "FROM sealed_ground_truth "
                "WHERE ingest_case_id IS NULL AND created_at <= ?",
                (cutoff,),
            ).fetchall()
        bound, orphans = 0, []
        for r in rows:
            rec = dict(r)
            case = None
            with self._conn() as conn:
                crow = conn.execute(
                    "SELECT ingest_case_id, status FROM ingest_cases "
                    "WHERE upload_id = ? AND patient_key = ? "
                    "ORDER BY created_at ASC LIMIT 1",
                    (rec.get("upload_id"), rec.get("patient_key")),
                ).fetchone()
                if crow:
                    case = dict(crow)
            if case:
                self.bind_sealed_ground_truth(rec["sealed_id"], case["ingest_case_id"])
                # Hold the recovered case for review — a key that had to be reconciled
                # is a blocking signal until a human confirms the adjudication.
                self.hold_ingest_case_for_review(
                    case["ingest_case_id"], "sealed_key_unbound",
                    "sealed answer key was recovered by reconciliation after an "
                    "interrupted ingest")
                bound += 1
            else:
                orphans.append({"sealed_id": rec["sealed_id"], "upload_id": rec.get("upload_id"),
                                "reason": "sealed_key_unbound", "severity": "blocking",
                                "detail": "staged answer key has no matching ingest case"})
        return {"checked": len(rows), "bound": bound, "orphans": orphans}

    def get_sealed_ground_truth(
        self, ingest_case_id: str, *, actor: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Decrypt + return the sealed answer key for an ingest case, emitting an
        audit event on EVERY read (Buyer Response PRD §3 B1). Only the adjudication
        surface should call this — never render_case_prompt, an export profile, or a
        baseline runner."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM sealed_ground_truth WHERE ingest_case_id = ? "
                "ORDER BY created_at DESC LIMIT 1", (ingest_case_id,),
            ).fetchone()
        if not row:
            return None
        rec = dict(row)
        from field_crypto import decrypt_field
        payload = json.loads(decrypt_field(rec.get("payload_enc")) or "null")
        self.log_event(entity_type="sealed_ground_truth", entity_id=ingest_case_id,
                       event_type="sealed_ground_truth_read", actor=actor,
                       payload={"sealed_id": rec.get("sealed_id")})
        return {"sealed_id": rec.get("sealed_id"), "ingest_case_id": ingest_case_id,
                "upload_id": rec.get("upload_id"), "payload": payload}

    def get_sealed_ground_truth_raw(self, ingest_case_id: str) -> Optional[str]:
        """Return the ON-DISK (still-encrypted) payload token WITHOUT decrypting or
        auditing — used by tests to assert the content is unreadable at rest."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT payload_enc FROM sealed_ground_truth WHERE ingest_case_id = ? "
                "ORDER BY created_at DESC LIMIT 1", (ingest_case_id,),
            ).fetchone()
        return dict(row)["payload_enc"] if row else None

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
        rec["review"] = json.loads((rec.get("review_json") or "null") or "null")
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
            rec["review"] = json.loads((rec.get("review_json") or "null") or "null")
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

    def list_uploads_with_retained_raw(self) -> List[Dict[str, Any]]:
        """Uploads whose raw blob is retained past the normal window (Audit §9.4) —
        every entry failed to parse, so the file is kept for re-run after a fix. The
        retention purge skips these paths."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT upload_id, raw_path FROM ingest_uploads WHERE retain_raw = 1"
            ).fetchall()
        return [dict(r) for r in rows]

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

    def get_tutorial_state(self, user_id: str) -> Dict[str, Any]:
        """The user's first-run tutorial state; a default not_started shape when
        unset or unparseable (a corrupt blob must never lock someone out)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT tutorial_json FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        raw = row[0] if row else None
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict) and parsed.get("status"):
                    return parsed
            except (ValueError, TypeError):
                pass
        return {"status": "not_started", "version": None}

    def set_tutorial_state(self, user_id: str, state: Dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE users SET tutorial_json = ? WHERE id = ?",
                (json.dumps(state), user_id),
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
        """The credential block copied onto every emitted record (PRD §6.2).

        Emits the credential under the canonical key ``credential`` (singular) AND
        the deprecated alias ``credentials`` (plural) for one release. Two names for
        one concept is exactly the mechanism that produced the buyer's finding
        (Buyer Response PRD §6 E1: ``store.annotator_block`` wrote ``credentials``
        while the contributor rollup wrote ``credential``), so both are emitted here
        during the migration and packaging reads either."""
        cred = user.get("board_cert") or (
            f"board_certified_{user.get('specialty')}" if user.get("specialty") else "unspecified"
        )
        return {
            "id_hashed": user.get("id_hashed") or "",
            "credential": cred,       # canonical (Buyer Response PRD §6 E1)
            "credentials": cred,      # deprecated alias — kept for one release
            "specialty": user.get("specialty"),
            "years_experience": user.get("years_experience"),
            # Advisor PRD §5.1: the tier rides along so packaging can stamp
            # ``related_party`` from the SAME resolution that produced the
            # credential. Resolving them separately is how one of them ends up
            # hydrated and the other silently null on a record that ships.
            "tier": user.get("tier"),
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
        md = "multimodal" if (case and (case.get("lab_panels") or case.get("notes") or case.get("studies"))) else "text"
        # case_source is DERIVED from the case (EHR PRD §9.5): 'real_deid' only
        # when the case itself says so; any other case is 'synthetic'; a text
        # task has none. First-class column so the V4 routing wall is pure SQL.
        cs = ((case.get("case_source") or "synthetic") if case else None)
        # Empirical difficulty (PRD §9) rides in the generation block; lift it to the
        # first-class columns for the serving gate + admin/export.
        ed_val, ed_measured = _empirical_difficulty_fields(
            (generation or {}).get("empirical_difficulty")
        )
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO tasks
                  (task_id, specialty, difficulty, capture_reasoning, source, prompt,
                   candidate_answers_json, max_labels, grounding_mode, independent_mode,
                   buyer_request_id, generation_json, value_tier, modality, case_json,
                   case_source, empirical_difficulty, difficulty_measured,
                   status, created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
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
                    ed_val,
                    ed_measured,
                    created_by,
                    _utcnow_iso(),
                ),
            )
        return self.get_task(tid)  # type: ignore[return-value]

    def update_task_case(self, task_id: str, case: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Replace a task's stored case (V4 Image Embedding PRD §3.5 — attach an image
        asset to a study). Re-derives modality + case_source from the new case. Used by
        the image-ingest path; the image BYTES live in the asset store, only the
        StudyAsset reference is written here."""
        md = "multimodal" if (case and (case.get("lab_panels") or case.get("notes") or case.get("studies"))) else "text"
        cs = ((case.get("case_source") or "synthetic") if case else None)
        with self._conn() as conn:
            conn.execute(
                "UPDATE tasks SET case_json = ?, modality = ?, case_source = ? WHERE task_id = ?",
                (json.dumps(case) if case else None, md, cs, task_id),
            )
        return self.get_task(task_id)

    def insert_asset_ref(self, *, asset_id: str, sha256: str, mime: str,
                         task_id: Optional[str], case_source: Optional[str]) -> None:
        """Index a V4 image asset (V4 Image PRD §4) so serving resolves it in one
        indexed lookup instead of scanning tasks. Idempotent on ``asset_id`` (dedupe:
        the same content → same id → last owning task wins, which is fine — all image
        assets are on real_deid cases)."""
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO study_assets "
                "(asset_id, sha256, mime, task_id, case_source, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (asset_id, sha256, mime, task_id, case_source, _utcnow_iso()),
            )

    def get_asset_ref(self, asset_id: str) -> Optional[Dict[str, Any]]:
        """Resolve an ``asset_id`` → {sha256, mime, task_id, case_source} via the
        index (O(1)). None if unknown."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT asset_id, sha256, mime, task_id, case_source FROM study_assets WHERE asset_id = ?",
                (asset_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if not row:
            return None
        return self._task_row(row)

    def set_task_decisive_action(self, task_id: str, action: Dict[str, Any]) -> None:
        """Persist the physician-named verifiable outcome (Audit §13), written from the
        submission — never by an admin or a model: only the clinician who reasoned
        through the case can say which step the answer depends on."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE tasks SET decisive_action_json = ? WHERE task_id = ?",
                (json.dumps(action) if action else None, task_id),
            )

    @staticmethod
    def _task_row(row: sqlite3.Row) -> Dict[str, Any]:
        rec = dict(row)
        rec["capture_reasoning"] = bool(rec.get("capture_reasoning"))
        rec["candidate_answers"] = json.loads(rec.pop("candidate_answers_json", "[]") or "[]")
        rec["generation"] = json.loads(rec.pop("generation_json", "null") or "null")
        # Multimodal case (may be absent on legacy rows / text tasks).
        rec["case"] = json.loads(rec.pop("case_json", "null") or "null")
        # Decisive action (Audit §13): deserialize so packaging/export see a dict,
        # not a JSON string. Absent on legacy rows / tasks nobody named one for.
        rec["decisive_action"] = json.loads(rec.pop("decisive_action_json", "null") or "null")
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
        require_measured_difficulty: bool = False, min_empirical_difficulty: float = 0.0,
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
        # Empirical-difficulty gate (PRD §9): when required, serve only cases whose
        # frontier-failure rate was LIVE-measured above the floor. OFF by default so
        # declared/authored seeds still serve in dev without live frontier keys.
        if require_measured_difficulty:
            clauses.append("(t.difficulty_measured = 1 AND t.empirical_difficulty >= ?)")
            params.append(float(min_empirical_difficulty))
        clauses.append(
            "t.case_source = 'real_deid'" if real_only
            else "(t.case_source IS NULL OR t.case_source != 'real_deid')"
        )
        # Admin review queue (Audit PRD §21.6): a task whose ingest case still carries
        # an unresolved BLOCKING review reason (ingest_cases.status = 'needs_review')
        # must never be served for annotation — a physician must not label a case whose
        # image may still carry burned-in PHI. Clearing/rejecting flips that status, so
        # this releases the case the moment a human resolves it.
        clauses.append(
            "NOT EXISTS (SELECT 1 FROM ingest_cases ic WHERE ic.task_id = t.task_id "
            "AND ic.status = 'needs_review')"
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
        require_measured_difficulty: bool = False, min_empirical_difficulty: float = 0.0,
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
        # Empirical-difficulty gate (PRD §9) — same rule as next_task_for_evaluator.
        if require_measured_difficulty:
            clauses.append("(t.difficulty_measured = 1 AND t.empirical_difficulty >= ?)")
            params.append(float(min_empirical_difficulty))
        # The V4 wall (EHR PRD §9.5) — same rule as next_task_for_evaluator.
        clauses.append(
            "t.case_source = 'real_deid'" if real_only
            else "(t.case_source IS NULL OR t.case_source != 'real_deid')"
        )
        # Hold blocking-review cases out of the candidate set too (Audit PRD §21.6) —
        # same rule as next_task_for_evaluator, so value-aware routing never ranks a
        # case a human has not yet cleared.
        clauses.append(
            "NOT EXISTS (SELECT 1 FROM ingest_cases ic WHERE ic.task_id = t.task_id "
            "AND ic.status = 'needs_review')"
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

    def open_multimodal_count(self, specialty: Optional[str] = None) -> int:
        """Count OPEN (servable, not-yet-fully-labeled) V3 multimodal cases — the
        unlabeled pool a top-up fills to ``target_pool_size`` (PRD §B4). Optionally
        scoped to a specialty."""
        clauses = ["status = 'open'", "COALESCE(modality, 'text') = 'multimodal'"]
        params: List[Any] = []
        if specialty:
            clauses.append("specialty = ?")
            params.append(specialty)
        with self._conn() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM tasks WHERE {' AND '.join(clauses)}", tuple(params)
            ).fetchone()
        return int(row["n"] or 0)

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

    def evaluator_self_stats(self, evaluator_id: str) -> Dict[str, Any]:
        """Real, personal counts for the dashboard's own tracking widget: total
        cases this evaluator has completed, how many in the last 7 days, and
        when they last submitted one. No earnings/streak data exists anywhere
        in this schema, so this stays limited to what's actually true."""
        week_cutoff = (datetime.utcnow().replace(microsecond=0) - timedelta(days=7)).isoformat()
        with self._conn() as conn:
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM submissions WHERE evaluator_id = ?",
                (evaluator_id,),
            ).fetchone()["n"]
            this_week = conn.execute(
                "SELECT COUNT(*) AS n FROM submissions WHERE evaluator_id = ? AND created_at >= ?",
                (evaluator_id, week_cutoff),
            ).fetchone()["n"]
            last_at = conn.execute(
                "SELECT MAX(created_at) AS t FROM submissions WHERE evaluator_id = ?",
                (evaluator_id,),
            ).fetchone()["t"]
        return {
            "submissions_total": total,
            "submissions_this_week": this_week,
            "last_submission_at": last_at,
        }

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
        verdict_agree: bool, n_labels: int, flagged: bool, blinded: bool = True,
    ) -> None:
        # ``blinded`` (Buyer Response PRD §7 F1): whether the second annotator was
        # blind to the first's verdict. Only blinded observations enter κ.
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO agreement
                  (task_id, specialty, sub_a, sub_b, verdict_a, verdict_b, tags_a_json,
                   tags_b_json, jaccard_tags, verdict_agree, n_labels, flagged, blinded, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id, specialty, sub_a, sub_b, verdict_a, verdict_b,
                    json.dumps(tags_a or []), json.dumps(tags_b or []),
                    jaccard_tags, 1 if verdict_agree else 0, int(n_labels),
                    1 if flagged else 0, 1 if blinded else 0, _utcnow_iso(),
                ),
            )

    def external_adjudication_pairs(self) -> List[tuple]:
        """(partner_verdict, physician_verdict) pairs for external-adjudication
        agreement (Buyer Response PRD §7 F3). Reads the ``external_adjudication`` table
        when present; returns [] otherwise so the export degrades to a null-with-reason
        stat rather than failing. The table is populated by the adjudication surface
        as physicians answer cases that carry a sealed partner adjudication."""
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT partner_verdict, physician_verdict FROM external_adjudication "
                    "WHERE partner_verdict IS NOT NULL AND physician_verdict IS NOT NULL"
                ).fetchall()
        except Exception:
            return []
        return [(dict(r)["partner_verdict"], dict(r)["physician_verdict"]) for r in rows]

    def record_external_adjudication(
        self, *, ingest_case_id: str, partner_verdict: Optional[str],
        physician_verdict: Optional[str], physician_hashed: Optional[str] = None,
    ) -> None:
        """Record a physician's independent verdict against the partner's sealed
        adjudication for a case (Buyer Response PRD §7 F3)."""
        now = _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS external_adjudication (
                    ingest_case_id   TEXT PRIMARY KEY,
                    partner_verdict  TEXT,
                    physician_verdict TEXT,
                    physician_hashed TEXT,
                    created_at       TEXT NOT NULL
                )""")
            conn.execute(
                """INSERT INTO external_adjudication
                   (ingest_case_id, partner_verdict, physician_verdict, physician_hashed, created_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(ingest_case_id) DO UPDATE SET
                     partner_verdict=excluded.partner_verdict,
                     physician_verdict=excluded.physician_verdict,
                     physician_hashed=excluded.physician_hashed""",
                (ingest_case_id, partner_verdict, physician_verdict, physician_hashed, now))

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
        provider: Optional[str] = None, prompt_hash: Optional[str] = None,
    ) -> Dict[str, Any]:
        rid = _new_id("bl")
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO baseline_runs
                   (run_id, task_id, model, provider, prompt_hash, response_text, error,
                    latency_ms, tokens_in, tokens_out, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (rid, task_id, model, provider, prompt_hash, response_text, error,
                 latency_ms, tokens_in, tokens_out, _utcnow_iso()),
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
        expert_correction: Optional[str], prompt: Optional[str], provider: Optional[str] = None,
    ) -> str:
        fid = _new_id("mf")
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO model_failures
                   (failure_id, task_id, submission_id, model, provider, verdict, error_tags_json,
                    corrected_steps_json, expert_correction, prompt, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (fid, task_id, submission_id, model, provider, verdict, json.dumps(error_tags or []),
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
        headline ("GPT-5.5 failed N cases; top tags …"). Includes ``provider``."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT model, MAX(provider) AS provider, COUNT(*) AS n "
                "FROM model_failures GROUP BY model ORDER BY n DESC"
            ).fetchall()
        out = []
        for r in rows:
            tags: Dict[str, int] = {}
            for f in self.list_model_failures(model=r["model"]):
                for t in f.get("error_tags") or []:
                    tags[t] = tags.get(t, 0) + 1
            out.append({"model": r["model"], "provider": r["provider"],
                        "failures": int(r["n"]), "error_tags": tags})
        return out

    def ab_slot_balance(self) -> Dict[str, Any]:
        """Durable QC metric (A3): over all two-frontier A/B pairs actually built (a
        candidate with source='baseline' + a provider), how often is OpenAI in slot A?
        Must converge to ~0.5. Computed from stored candidates so it survives restart."""
        n_pairs = 0
        openai_in_A = 0
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT candidate_answers_json FROM tasks WHERE candidate_answers_json LIKE '%\"baseline\"%'"
            ).fetchall()
        for row in rows:
            try:
                cands = json.loads(row["candidate_answers_json"] or "[]")
            except (ValueError, TypeError):
                continue
            by_id = {c.get("id"): c for c in cands if isinstance(c, dict)}
            a, b = by_id.get("A"), by_id.get("B")
            if not a or not b:
                continue
            provs = {a.get("provider"), b.get("provider")}
            if provs != {"openai", "anthropic"}:
                continue
            n_pairs += 1
            if a.get("provider") == "openai":
                openai_in_A += 1
        rate = round(openai_in_A / n_pairs, 3) if n_pairs else None
        return {"pairs": n_pairs, "openai_in_A": openai_in_A, "openai_as_A_rate": rate}

    def ab_fallback_rate(self, *, window: int = 50) -> Optional[float]:
        """Rolling ``legacy_fallback / (two_frontier + legacy_fallback)`` over the last
        ``window`` assembled A/B pairs (PRD §A3 Rung 3). Reads the durable ``ab_source``
        stamped onto each task's generation block, newest first, so it survives restart
        and is worker-independent. ``anthropic_only_v4`` (the intended V4 path) is NOT a
        fallback and is excluded. Returns ``None`` when there is no pairing history yet
        (so a cold start never trips the guard)."""
        counted = ("two_frontier", "legacy_fallback")
        want = max(1, int(window))
        total = 0
        fallback = 0
        with self._conn() as conn:
            # Fetch generously (window may be diluted by interleaved anthropic_only_v4 /
            # needs_baseline rows) and take the newest ``want`` COUNTED pairs in Python.
            rows = conn.execute(
                "SELECT generation_json FROM tasks "
                "WHERE generation_json LIKE '%\"ab_source\"%' "
                "ORDER BY created_at DESC LIMIT ?",
                (want * 5,),
            ).fetchall()
        for row in rows:
            try:
                gen = json.loads(row["generation_json"] or "null") or {}
            except (ValueError, TypeError):
                continue
            src = gen.get("ab_source")
            if src not in counted:
                continue
            total += 1
            if src == "legacy_fallback":
                fallback += 1
            if total >= want:
                break
        if not total:
            return None
        return round(fallback / total, 3)

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

    # ─── V5 Clinical RL Environments (env_runs, PRD §10) ─────────────────────
    def insert_env_run(
        self, *, task_id: str, specialty: str, task_type: str,
        case_id: Optional[str] = None, case_source: str = "gold",
        provider: Optional[str] = None, model: Optional[str] = None,
        ab_source: Optional[str] = None, mode: str = "generated",
        compiled: Optional[Dict[str, Any]] = None,
        trajectory: Optional[List[Dict[str, Any]]] = None,
        verification: Optional[Dict[str, Any]] = None,
        provenance: Optional[Dict[str, Any]] = None,
        physician_annotation: Optional[Dict[str, Any]] = None,
        annotations: Optional[List[Dict[str, Any]]] = None,
        empirical_difficulty: Optional[float] = None,
        difficulty_measured: bool = False,
        passes_difficulty_gate: Optional[bool] = None,
    ) -> Dict[str, Any]:
        rid = _new_id("env")
        now = _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO env_runs
                   (run_id, task_id, specialty, task_type, case_id, case_source, provider,
                    model, ab_source, mode, compiled_json, trajectory_json, verification_json,
                    provenance_json, physician_annotation_json, annotations_json, empirical_difficulty,
                    difficulty_measured, passes_difficulty_gate, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (rid, task_id, specialty, task_type, case_id, case_source, provider, model,
                 ab_source, mode,
                 json.dumps(compiled) if compiled is not None else None,
                 json.dumps(trajectory) if trajectory is not None else None,
                 json.dumps(verification) if verification is not None else None,
                 json.dumps(provenance) if provenance is not None else None,
                 json.dumps(physician_annotation) if physician_annotation is not None else None,
                 json.dumps(annotations) if annotations is not None else None,
                 empirical_difficulty, 1 if difficulty_measured else 0,
                 (None if passes_difficulty_gate is None else (1 if passes_difficulty_gate else 0)),
                 now, now),
            )
        return self.get_env_run(rid)  # type: ignore[return-value]

    @staticmethod
    def _env_run_row(row: sqlite3.Row) -> Dict[str, Any]:
        rec = dict(row)
        for col in ("compiled", "trajectory", "verification", "provenance",
                    "physician_annotation", "annotations"):
            raw = rec.pop(f"{col}_json", None)
            rec[col] = json.loads(raw) if raw else None
        rec["difficulty_measured"] = bool(rec.get("difficulty_measured"))
        pg = rec.get("passes_difficulty_gate")
        rec["passes_difficulty_gate"] = None if pg is None else bool(pg)
        return rec

    def get_env_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM env_runs WHERE run_id = ?", (run_id,)).fetchone()
        return self._env_run_row(row) if row else None

    def get_environment(self, task_id: str) -> Optional[Dict[str, Any]]:
        """The compiled-environment row (mode='generated') for a task_id."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM env_runs WHERE task_id = ? AND mode = 'generated' "
                "ORDER BY created_at ASC LIMIT 1", (task_id,)
            ).fetchone()
        return self._env_run_row(row) if row else None

    def list_env_runs(
        self, *, task_id: Optional[str] = None, specialty: Optional[str] = None,
        mode: Optional[str] = None, case_source: Optional[str] = None,
        has_annotation: Optional[bool] = None, limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if task_id:
            clauses.append("task_id = ?"); params.append(task_id)
        if specialty:
            clauses.append("specialty = ?"); params.append(specialty)
        if mode:
            clauses.append("mode = ?"); params.append(mode)
        if case_source:
            clauses.append("case_source = ?"); params.append(case_source)
        if has_annotation is True:
            clauses.append("physician_annotation_json IS NOT NULL")
        elif has_annotation is False:
            clauses.append("physician_annotation_json IS NULL")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM env_runs {where} ORDER BY created_at DESC LIMIT ?", tuple(params)
            ).fetchall()
        return [self._env_run_row(r) for r in rows]

    def update_env_run(self, run_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        if not fields:
            return self.get_env_run(run_id)
        json_cols = {"compiled", "trajectory", "verification", "provenance",
                     "physician_annotation", "annotations"}
        sets, params = [], []
        for key, val in fields.items():
            if key in json_cols:
                sets.append(f"{key}_json = ?")
                params.append(json.dumps(val) if val is not None else None)
            elif key == "difficulty_measured":
                sets.append("difficulty_measured = ?"); params.append(1 if val else 0)
            elif key == "passes_difficulty_gate":
                sets.append("passes_difficulty_gate = ?")
                params.append(None if val is None else (1 if val else 0))
            else:
                sets.append(f"{key} = ?"); params.append(val)
        sets.append("updated_at = ?"); params.append(_utcnow_iso())
        params.append(run_id)
        with self._conn() as conn:
            conn.execute(f"UPDATE env_runs SET {', '.join(sets)} WHERE run_id = ?", tuple(params))
        return self.get_env_run(run_id)

    def env_annotation_records(self, *, specialty: Optional[str] = None) -> List[Dict[str, Any]]:
        """Rollout rows that carry a physician annotation — the PRM training set
        (PRD §7.5). Each dict has ``trajectory`` + ``physician_annotation``."""
        return self.list_env_runs(specialty=specialty, mode="rollout", has_annotation=True)

    # ═══ PRD-B IDENTITY METHODS — owned by Agent 2, do not edit from other PRDs ═══
    # NPIs are normalized on WRITE, but rows created before that fix can hold
    # "1234-567893". Comparing the raw column would leave those legacy rows
    # invisible to duplicate detection and to the cache — the exact defect the
    # write-side fix closed, still open for everyone already in the database.
    # Normalizing in SQL fixes them without rewriting stored data. Mirrors
    # ``credentialing.clean_npi`` (strips ``[\s\-\.]``).
    _NPI_NORM = ("REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(npi, '-', ''), '.', ''), "
                 "' ', ''), CHAR(9), ''), CHAR(10), '')")

    def set_npi_result(self, user_id: str, result: Dict[str, Any]) -> None:
        """Persist an NPI check outcome onto the user row.

        Three states, never collapsed:
          verified              -> npi_verified = 1
          mismatch / not_found  -> npi_verified = 0   (definitive negative)
          unavailable / other   -> npi_verified = NULL (could NOT check)

        ``npi_checked_at`` is stamped ONLY on definitive outcomes, so an
        UNAVAILABLE attempt never satisfies the 30-day NPI cache and never
        suppresses a retry.
        """
        outcome = (result or {}).get("result")
        if outcome == "verified":
            flag: Optional[int] = 1
        elif outcome in ("mismatch", "not_found"):
            flag = 0
        else:
            flag = None

        if flag is None:
            # F6: a failed check is an EVENT, not a result. Writing it through
            # would set npi_verified back to NULL, replace the NPPES record
            # with {"result":"unavailable","record":null}, and clear
            # npi_checked_at — erasing evidence we already have, dropping the
            # user's score by 25, making 'reviewer' unproposable, and evicting
            # the 30-day cache entry FOR EVERY user of that NPI. That happens
            # on the admin's Recheck click while NPPES is rate-limiting, i.e.
            # at exactly the moment the button gets used, and on any
            # re-onboard (provision_user is an idempotent upsert).
            with self._conn() as conn:
                conn.execute(
                    "UPDATE users SET npi_last_attempt_json = ?, "
                    "npi_last_attempt_at = ? WHERE id = ?",
                    (json.dumps(result or {}), _utcnow_iso(), user_id),
                )
            return

        with self._conn() as conn:
            conn.execute(
                "UPDATE users SET npi_verified = ?, npi_payload_json = ?, "
                "npi_checked_at = ?, npi_last_attempt_json = NULL, "
                "npi_last_attempt_at = NULL WHERE id = ?",
                (flag, json.dumps(result or {}), _utcnow_iso(), user_id),
            )

    def get_cached_npi_fetch(self, npi: str, max_age_days: int = 30) -> Optional[Dict[str, Any]]:
        """A fresh definitive NPPES answer for this NPI, if any user row holds
        one — shaped like ``credentialing.fetch_npi_record`` output so the
        caller can recompute the name match for the CURRENT signup (a cache
        keyed by NPI alone must never cache the verdict).

        Returns None when there is no definitive check within the window;
        UNAVAILABLE attempts never populate the cache (``npi_checked_at`` stays
        NULL for them).
        """
        npi = (npi or "").strip()
        if not npi:
            return None
        cutoff = (datetime.utcnow() - timedelta(days=max(0, max_age_days))).replace(
            microsecond=0).isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT npi_payload_json FROM users "
                f"WHERE {self._NPI_NORM} = ? AND npi_checked_at IS NOT NULL "
                "AND npi_checked_at >= ? "
                "AND npi_payload_json IS NOT NULL "
                "ORDER BY npi_checked_at DESC LIMIT 1",
                (npi, cutoff),
            ).fetchone()
        if not row:
            return None
        try:
            stored = json.loads(row["npi_payload_json"] or "{}")
        except (TypeError, ValueError):
            return None
        outcome = stored.get("result")
        if outcome == "not_found":
            return {"status": "not_found", "record": None, "reason": "cached"}
        if outcome in ("verified", "mismatch") and stored.get("record"):
            return {"status": "found", "record": stored["record"], "reason": "cached"}
        return None

    def set_verification_status(
        self, user_id: str, status: Optional[str], *, notes: Optional[str] = None
    ) -> None:
        """Set the human-review lifecycle state (pending | approved | rejected).
        Notes are only overwritten when provided."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE users SET verification_status = ?, "
                "verification_notes = COALESCE(?, verification_notes) WHERE id = ?",
                (status, notes, user_id),
            )

    def update_identity_capture(
        self,
        user_id: str,
        *,
        phone: Optional[str] = None,
        linkedin_url: Optional[str] = None,
        email_domain_class: Optional[str] = None,
    ) -> None:
        """Signup-time identity fields (PRD-B Phase 4). Only overwrites a field
        when a value is provided, so a sparse re-onboard never wipes data."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE users SET phone = COALESCE(?, phone), "
                "linkedin_url = COALESCE(?, linkedin_url), "
                "email_domain_class = COALESCE(?, email_domain_class) WHERE id = ?",
                (phone, linkedin_url, email_domain_class, user_id),
            )

    def set_cv(
        self, user_id: str, asset_sha: Optional[str],
        parsed: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Attach an uploaded CV (content-addressed sha) and its best-effort
        parse to the user row. The parse is advisory dossier data only."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE users SET cv_asset_sha = ?, cv_parsed_json = ? WHERE id = ?",
                (asset_sha, json.dumps(parsed) if parsed is not None else None, user_id),
            )

    def find_users_by_npi(self, npi: str) -> List[Dict[str, Any]]:
        """All user rows claiming this NPI — duplicate detection for the tier
        scorer's blocker list."""
        npi = (npi or "").strip()
        if not npi:
            return []
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM users WHERE {self._NPI_NORM} = ? ORDER BY created_at ASC",
                (npi,),
            ).fetchall()
        return [dict(r) for r in rows]

    def users_pending_npi_recheck(
        self, *, older_than_minutes: int = 60, limit: int = 200
    ) -> List[Dict[str, Any]]:
        """Rows whose NPI check never reached a definitive answer — the retry
        list PRD §1.2 asked for ("UNAVAILABLE routes to manual review AND
        schedules a retry").

        Deliberately a list an admin can bulk-run rather than a scheduler: no
        job framework, no background thread, and the work is visible and
        auditable. A row qualifies when it claims an NPI, has no definitive
        result (``npi_checked_at IS NULL``), and either has never been
        attempted or was last attempted longer than ``older_than_minutes`` ago
        — so a sweep cannot hot-loop against a rate-limiting registry.
        """
        cutoff = (datetime.utcnow() - timedelta(minutes=max(0, older_than_minutes))).replace(
            microsecond=0).isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM users "
                "WHERE npi IS NOT NULL AND TRIM(npi) != '' "
                "  AND npi_checked_at IS NULL "
                "  AND (npi_last_attempt_at IS NULL OR npi_last_attempt_at <= ?) "
                "ORDER BY COALESCE(npi_last_attempt_at, created_at) ASC LIMIT ?",
                (cutoff, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def npi_claim_counts(self) -> Dict[str, int]:
        """{normalized npi: number of accounts claiming it} — one grouped query
        so the queue does not run a full-table scan per row (B-5.8). Keyed by
        the NORMALIZED value so callers can look up with ``clean_npi(...)`` and
        so a legacy "1234-567893" row groups with its clean twin. Only NPIs
        with more than one claimant are returned; everything else is 1 by
        absence."""
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT {self._NPI_NORM} AS norm_npi, COUNT(*) AS n FROM users "
                "WHERE npi IS NOT NULL AND TRIM(npi) != '' "
                "GROUP BY norm_npi HAVING n > 1"
            ).fetchall()
        return {r["norm_npi"]: r["n"] for r in rows}

    def list_verification_queue(self, status: str = "pending") -> List[Dict[str, Any]]:
        """User rows in one verification state, newest signup first (the admin
        works the top of the queue). ``status`` ∈ pending|approved|rejected."""
        with self._conn() as conn:
            rows = conn.execute(
                # rowid tiebreak: created_at has second granularity, and two
                # launch-day signups in the same second must still order
                # deterministically (newest insertion first).
                "SELECT * FROM users WHERE verification_status = ? "
                "ORDER BY created_at DESC, rowid DESC",
                (status,),
            ).fetchall()
        return [dict(r) for r in rows]

    def record_verification_decision(
        self,
        user_id: str,
        *,
        status: str,
        decided_by: str,
        tier: Optional[str] = None,
        tier_score: Optional[float] = None,
        note: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """The human decision (PRD-B Phase 5). Stamps verified_by/verified_at on
        EVERY decision; tier fields are written only on approval — the tier is
        a decision, not a computation, so it arrives only from this method."""
        now = _utcnow_iso()
        with self._conn() as conn:
            if status == "approved":
                conn.execute(
                    "UPDATE users SET verification_status = 'approved', "
                    "verification_notes = COALESCE(?, verification_notes), "
                    "verified_by = ?, verified_at = ?, tier = ?, tier_score = ?, "
                    "tier_assigned_at = ?, tier_assigned_by = ? WHERE id = ?",
                    (note, decided_by, now, tier, tier_score, now, decided_by, user_id),
                )
            else:
                conn.execute(
                    "UPDATE users SET verification_status = ?, "
                    "verification_notes = COALESCE(?, verification_notes), "
                    "verified_by = ?, verified_at = ? WHERE id = ?",
                    (status, note, decided_by, now, user_id),
                )
        return self.get_user_by_id(user_id)

    def mark_community_welcomed(self, user_id: str) -> bool:
        """Community v2: idempotency flag for the one-time community welcome
        (repurposes the reserved ``slack_joined`` column — the community IS
        our Slack). Set BEFORE posting: the safe failure is a missed welcome,
        never a double-post. The guarded UPDATE is the arbiter — under
        multi-worker deploys a concurrent queue-approval + credential-PUT for
        the same user must not both win. Returns True when THIS call claimed
        the welcome."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE users SET slack_joined = 1, slack_checked_at = ? "
                "WHERE id = ? AND COALESCE(slack_joined, 0) = 0",
                (_utcnow_iso(), user_id),
            )
            return bool(cur.rowcount)
    # ═══ END PRD-B ═══
    # ═══ PRD-A REVIEW STORE METHODS — owned by Agent 1, do not edit from other PRDs ═══
    # Two-tier review product (PRD A): reviewer queue, case_reviews CRUD, and
    # double-label routing. Appended per START_HERE §3.2 — existing methods above
    # are never modified.

    @staticmethod
    def _case_review_row(row: sqlite3.Row) -> Dict[str, Any]:
        rec = dict(row)
        rec["dimensions"] = json.loads(rec.pop("dimension_json", "{}") or "{}")
        rec["corrections"] = json.loads(rec.pop("corrections_json", "null") or "null")
        raw_flags = rec.pop("identifier_flags", None)
        # None (never scanned) stays distinct from [] (scanned clean).
        rec["identifier_flags"] = json.loads(raw_flags) if raw_flags else (
            [] if raw_flags == "[]" else None)
        return rec

    def insert_case_review(
        self,
        *,
        task_id: str,
        submission_id: str,
        reviewer_user_id: str,
        reviewer_id_hashed: str,
        verdict: str,
        dimensions: Optional[Dict[str, str]] = None,
        corrections: Optional[Dict[str, Any]] = None,
        reviewer_notes: Optional[str] = None,
        time_spent_sec: Optional[int] = None,
        blinded: Optional[bool] = True,
        identifier_flags: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """One senior-reviewer verdict on a labeler submission (PRD A §2).

        ``blinded`` is tri-state on purpose (START_HERE §5 rule 4): 1 = the payload
        served to the reviewer verifiably carried no labeler identity, 0 = identity
        was (or may have been) visible, NULL = not asserted either way."""
        review_id = _new_id("rev")
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO case_reviews
                  (review_id, task_id, submission_id, reviewer_user_id, reviewer_id_hashed,
                   verdict, dimension_json, corrections_json, reviewer_notes,
                   time_spent_sec, blinded, identifier_flags, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review_id,
                    task_id,
                    submission_id,
                    reviewer_user_id,
                    reviewer_id_hashed,
                    verdict,
                    json.dumps(dimensions or {}),
                    json.dumps(corrections) if corrections is not None else None,
                    reviewer_notes,
                    int(time_spent_sec) if time_spent_sec is not None else None,
                    None if blinded is None else (1 if blinded else 0),
                    None if identifier_flags is None else json.dumps(sorted(identifier_flags)),
                    _utcnow_iso(),
                ),
            )
        return self.get_case_review(review_id)  # type: ignore[return-value]

    def get_case_review(self, review_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM case_reviews WHERE review_id = ?", (review_id,)
            ).fetchone()
        return self._case_review_row(row) if row else None

    def reviews_for_submission(self, submission_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM case_reviews WHERE submission_id = ? ORDER BY created_at ASC",
                (submission_id,),
            ).fetchall()
        return [self._case_review_row(r) for r in rows]

    def reviews_for_task(self, task_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM case_reviews WHERE task_id = ? ORDER BY created_at ASC",
                (task_id,),
            ).fetchall()
        return [self._case_review_row(r) for r in rows]

    def has_review_by(self, submission_id: str, reviewer_user_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM case_reviews WHERE submission_id = ? AND reviewer_user_id = ? LIMIT 1",
                (submission_id, reviewer_user_id),
            ).fetchone()
        return row is not None

    def next_review_for(
        self,
        user_id: str,
        *,
        specialty: Optional[str] = None,
        lease_minutes: int = 45,
        predicate: Any = None,
        scan_limit: int = 200,
        persist_routing_decision: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Oldest reviewable submission for this reviewer (PRD A §1.4).

        Self-review is impossible BY QUERY (``s.evaluator_id != ?``), not by caller
        discipline. Serves submissions never routed (``review_status IS NULL``) plus
        stale ``in_review`` claims past the lease, so an abandoned draw re-queues
        instead of vanishing forever. Excludes submissions this reviewer already
        reviewed and tasks held for blocking ingest review (Audit PRD §21.6 —
        a reviewer must not see a case whose image may carry burned-in PHI).

        ``predicate(task, submission) -> bool`` filters candidates in Python (the
        rate-based ``needs_review`` policy lives in ``asclepius.review``, not in SQL).
        """
        cutoff = _iso_minus_seconds(max(1, int(lease_minutes)) * 60)
        clauses = [
            "s.evaluator_id != ?",
            "s.verdict IS NOT NULL",
            # NULL = never routed; an 'in_review' row past its lease re-queues.
            # The lease clock is review_claimed_at, NOT updated_at — any unrelated
            # write (a pipeline re-value, a status change) bumps updated_at and
            # used to silently extend a reviewer's claim (FIX A A-3.7).
            # 'reviewed', 'orphaned' and 'not_routed' are all terminal here.
            "(s.review_status IS NULL OR (s.review_status = 'in_review'"
            " AND (s.review_claimed_at IS NULL OR s.review_claimed_at < ?)))",
            "NOT EXISTS (SELECT 1 FROM case_reviews cr WHERE cr.submission_id = s.submission_id"
            " AND cr.reviewer_user_id = ?)",
            "NOT EXISTS (SELECT 1 FROM ingest_cases ic WHERE ic.task_id = s.task_id"
            " AND ic.status = 'needs_review')",
        ]
        params: List[Any] = [user_id, cutoff, user_id]
        if specialty:
            clauses.append("t.specialty = ?")
            params.append(specialty)
        params.append(int(scan_limit))
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT s.* FROM submissions s
                JOIN tasks t ON t.task_id = s.task_id
                WHERE {' AND '.join(clauses)}
                ORDER BY s.created_at ASC LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        declined: List[str] = []
        orphaned: List[str] = []
        chosen: Optional[Dict[str, Any]] = None
        for r in rows:
            sub = self._submission_row(r)
            if predicate is not None:
                task = self.get_task(sub["task_id"])
                if task is None:
                    # A missing task is an ORPHAN, not a routing decline. Two
                    # different terminal states because they mean two different
                    # things: 'not_routed' is a decision the policy made, and
                    # 'orphaned' is data damage someone has to look at.
                    orphaned.append(sub["submission_id"])
                    continue
                if not predicate(task, sub):
                    declined.append(sub["submission_id"])
                    continue
            chosen = sub
            break
        # Persist the routing decision (FIX A A-3.3). Without this, a submission
        # the policy declines stays review_status IS NULL forever and re-occupies
        # a slot in this LIMITed window on every single draw. Once `scan_limit`
        # declined rows accumulate at the head, the portal reports "no submissions
        # awaiting review" permanently while real work sits behind them. Masked at
        # launch only because ASCLEPIUS_REVIEW_RATE defaults to 1.0.
        if persist_routing_decision and (declined or orphaned):
            with self._conn() as conn:
                if declined:
                    conn.executemany(
                        "UPDATE submissions SET review_status = 'not_routed' "
                        "WHERE submission_id = ? AND review_status IS NULL",
                        [(sid,) for sid in declined],
                    )
                if orphaned:
                    conn.executemany(
                        "UPDATE submissions SET review_status = 'orphaned' "
                        "WHERE submission_id = ? AND review_status IS NULL",
                        [(sid,) for sid in orphaned],
                    )
        return chosen

    def requeue_not_routed(self) -> int:
        """Clear every ``not_routed`` decision back to NULL (undecided).

        The escape hatch for raising ``ASCLEPIUS_REVIEW_RATE`` after launch:
        routing decisions are persisted, so without this a submission declined
        under a 0.5 rate would stay declined forever under a 1.0 rate. Returns
        the number of submissions returned to the queue."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE submissions SET review_status = NULL WHERE review_status = 'not_routed'")
            return int(cur.rowcount)

    def agreement_observation_count(self, *, specialty: Optional[str] = None) -> int:
        """COUNT(*) of stored agreement observations. Exists because the routing
        sweep only ever needed the count and used to materialize every row to
        call len() on it, once per candidate (FIX A A-3.4)."""
        sql = "SELECT COUNT(*) FROM agreement"
        params: tuple = ()
        if specialty:
            sql += " WHERE specialty = ?"
            params = (specialty,)
        with self._conn() as conn:
            return int(conn.execute(sql, params).fetchone()[0])

    def claim_submission_for_review(
        self, submission_id: str, *, reviewer_id: str, blinded: Optional[bool] = None,
        lease_minutes: int = 45,
    ) -> bool:
        """Atomically claim a submission for review (``review_status='in_review'``).

        Compare-and-set: the UPDATE only wins when the row is still unclaimed or its
        prior claim's lease has expired, so two reviewers drawing concurrently cannot
        both claim the same submission. Returns True when this caller won the claim.

        The claim also records WHO holds it and the blinding DERIVED from the
        payload served to them (FIX A F2). ``review_claimed_at`` is the lease
        clock rather than ``updated_at``, which any unrelated write bumps."""
        now = _utcnow_iso()
        cutoff = _iso_minus_seconds(max(1, int(lease_minutes)) * 60)
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE submissions
                   SET review_status = 'in_review', review_claimed_by = ?,
                       review_claimed_at = ?, review_blinded = ?, updated_at = ?
                WHERE submission_id = ?
                  AND (review_status IS NULL
                       OR (review_status = 'in_review'
                           AND (review_claimed_at IS NULL OR review_claimed_at < ?)))
                """,
                (reviewer_id, now, None if blinded is None else (1 if blinded else 0),
                 now, submission_id, cutoff),
            )
            return cur.rowcount > 0

    def review_claim(self, submission_id: str, *, lease_minutes: int = 45) -> Dict[str, Any]:
        """The current claim on a submission: ``{holder, claimed_at, blinded,
        expired, status}``. ``blinded`` is tri-state (True/False/None) — None
        means no draw ever asserted it, which is NOT the same as unblinded."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT review_status, review_claimed_by, review_claimed_at, review_blinded "
                "FROM submissions WHERE submission_id = ?", (submission_id,)
            ).fetchone()
        if row is None:
            return {"holder": None, "claimed_at": None, "blinded": None,
                    "expired": True, "status": None}
        rec = dict(row)
        claimed_at = rec.get("review_claimed_at")
        cutoff = _iso_minus_seconds(max(1, int(lease_minutes)) * 60)
        blinded = rec.get("review_blinded")
        return {
            "holder": rec.get("review_claimed_by"),
            "claimed_at": claimed_at,
            "blinded": None if blinded is None else bool(blinded),
            "expired": (claimed_at is None) or (claimed_at < cutoff),
            "status": rec.get("review_status"),
        }

    def review_queue_stats(self) -> Dict[str, Any]:
        """Counts for the review portal header, in ONE pass over an indexed
        column (four separate COUNT(*) full scans used to fire on every draw —
        FIX A A-3.5).

        ``unreviewed`` counts only genuinely undecided rows (review_status NULL).
        Declined ('not_routed') and orphaned rows are reported separately rather
        than folded in: a header claiming work exists that the draw cannot serve
        is how A-3.3 stayed invisible."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT review_status AS st, COUNT(*) AS n FROM submissions "
                "WHERE verdict IS NOT NULL GROUP BY review_status"
            ).fetchall()
            n_reviews = conn.execute("SELECT COUNT(*) FROM case_reviews").fetchone()[0]
        counts = {(dict(r)["st"] or "__null__"): int(dict(r)["n"]) for r in rows}
        return {
            "unreviewed": counts.get("__null__", 0),
            "in_review": counts.get("in_review", 0),
            "reviewed": counts.get("reviewed", 0),
            "not_routed": counts.get("not_routed", 0),
            "orphaned": counts.get("orphaned", 0),
            "n_reviews": int(n_reviews),
        }

    def flag_task_for_double_label(self, task_id: str) -> bool:
        """Route a task to a second INDEPENDENT labeler (PRD A §1.3) by lifting its
        label capacity to 2 AND reopening it.

        The reopen is the load-bearing half. A ``max_labels=1`` task is ALREADY
        ``'done'`` by the time a first label exists — ``refresh_task_status``
        closes it on the first submission, on the hot submit path. So routing a
        second independent label is always a REOPEN, never a flag on an open
        task; lifting ``max_labels`` without restoring ``status='open'`` leaves a
        task that neither ``next_double_label_for`` nor the ordinary labeler
        queue can ever serve, which is what made the whole κ deliverable dead
        code in the first round.

        Terminal statuses are NOT reopened: ``prompt_flagged`` / ``not_hard`` /
        ``case_incoherent`` mean a clinician rejected the prompt itself, and
        dragging one back into the labeler queue for a second opinion would
        re-serve work a physician already ruled out. Idempotent."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE tasks SET max_labels = 2, status = 'open' "
                "WHERE task_id = ? AND status IN ('open', 'done') AND max_labels < 2",
                (task_id,),
            )
            return cur.rowcount > 0

    def flag_tasks_for_double_label(
        self, decisions: List[Dict[str, Any]]
    ) -> List[str]:
        """Batch form of :meth:`flag_task_for_double_label` (FIX A A-3.4).

        One connection, one UPDATE per task and one event INSERT per flag,
        instead of two fresh connections (each paying two PRAGMAs) per candidate.
        This runs on the same single SQLite writer that labeler submissions need,
        so its statement count must not scale with the sweep window.

        ``decisions`` is ``[{task_id, specialty, current_rate}, ...]``. Returns
        the task_ids actually flagged (the UPDATE is the arbiter, so this stays
        idempotent under concurrent sweeps)."""
        if not decisions:
            return []
        flagged: List[str] = []
        now = _utcnow_iso()
        with self._conn() as conn:
            for d in decisions:
                cur = conn.execute(
                    "UPDATE tasks SET max_labels = 2, status = 'open' "
                    "WHERE task_id = ? AND status IN ('open', 'done') AND max_labels < 2",
                    (d["task_id"],),
                )
                if cur.rowcount > 0:
                    flagged.append(d["task_id"])
            if flagged:
                by_id = {d["task_id"]: d for d in decisions}
                conn.executemany(
                    "INSERT INTO events (entity_type, entity_id, event_type, actor, "
                    "occurred_at, payload_json) VALUES ('task', ?, 'double_label_flagged', "
                    "NULL, ?, ?)",
                    [(tid, now, json.dumps({
                        "specialty": by_id[tid].get("specialty"),
                        "current_rate": by_id[tid].get("current_rate"),
                    })) for tid in flagged],
                )
        return flagged

    def tasks_awaiting_double_label_decision(self, *, limit: int = 100) -> List[Dict[str, Any]]:
        """Single-label tasks that already carry at least one verdict-bearing
        submission — the candidate set the double-label router decides over.

        Accepts ``'done'`` as well as ``'open'``: on the real submit route a
        singly-labeled task is closed by the time this runs, so a status='open'
        filter here matches nothing, by construction. Terminal statuses stay
        excluded — a rejected prompt is not a double-label candidate."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT t.*,
                       (SELECT s.submission_id FROM submissions s
                         WHERE s.task_id = t.task_id AND s.verdict IS NOT NULL
                         ORDER BY s.created_at ASC LIMIT 1) AS first_submission_id,
                       -- Carried here so the sweep does not re-open a connection
                       -- per candidate just to read one column (FIX A A-3.4).
                       (SELECT s2.confidence FROM submissions s2
                         WHERE s2.task_id = t.task_id AND s2.verdict IS NOT NULL
                         ORDER BY s2.created_at ASC LIMIT 1) AS first_confidence
                FROM tasks t
                WHERE t.status IN ('open', 'done') AND t.max_labels < 2
                  AND EXISTS (SELECT 1 FROM submissions sf
                               WHERE sf.task_id = t.task_id AND sf.verdict IS NOT NULL)
                ORDER BY t.created_at ASC LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        out = []
        for r in rows:
            rec = self._task_row(r)
            out.append(rec)
        return out

    def double_label_counts(self) -> tuple:
        """``(n_labeled_tasks, n_flagged_for_double_label)`` in ONE pass.

        The sweep needs both numbers once per run, not a recomputed ratio per
        candidate (FIX A A-3.4). Returning the raw counts lets the caller advance
        the rate locally as it flags."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS labeled, "
                "       SUM(CASE WHEN t.max_labels >= 2 THEN 1 ELSE 0 END) AS flagged "
                "FROM tasks t WHERE EXISTS "
                "(SELECT 1 FROM submissions s WHERE s.task_id = t.task_id)"
            ).fetchone()
        rec = dict(row) if row else {}
        return int(rec.get("labeled") or 0), int(rec.get("flagged") or 0)

    def double_label_flag_rate(self) -> float:
        """Share of labeled tasks currently routed for a second independent label —
        the ``current_rate`` input to the stratified top-up policy (PRD A §1.3)."""
        labeled, flagged = self.double_label_counts()
        return (flagged / labeled) if labeled else 0.0

    def next_double_label_for(
        self,
        user_id: str,
        *,
        specialty: Optional[str] = None,
        allow_real: bool = False,
        scan_limit: int = 200,
    ) -> Optional[Dict[str, Any]]:
        """Oldest task flagged for a second independent label that this user may
        take (PRD A §1.4). The first labeler — and anyone who already submitted —
        is excluded BY QUERY (``NOT EXISTS`` on their submissions), never by caller
        discipline: the second observation must be independent or κ is fiction.
        ``allow_real`` is the V4 wall (EHR PRD §9.5): real_deid tasks are excluded
        unless the caller verified ``real_data_approved``."""
        clauses = [
            "t.status = 'open'",
            "t.max_labels >= 2",
            "EXISTS (SELECT 1 FROM submissions sf WHERE sf.task_id = t.task_id"
            " AND sf.verdict IS NOT NULL)",
            "NOT EXISTS (SELECT 1 FROM submissions sm WHERE sm.task_id = t.task_id"
            " AND sm.evaluator_id = ?)",
            "NOT EXISTS (SELECT 1 FROM ingest_cases ic WHERE ic.task_id = t.task_id"
            " AND ic.status = 'needs_review')",
        ]
        params: List[Any] = [user_id]
        if specialty:
            clauses.append("t.specialty = ?")
            params.append(specialty)
        if not allow_real:
            clauses.append("(t.case_source IS NULL OR t.case_source != 'real_deid')")
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT t.*,
                       (SELECT COUNT(*) FROM submissions s WHERE s.task_id = t.task_id) AS sub_count
                FROM tasks t
                WHERE {' AND '.join(clauses)}
                ORDER BY t.created_at ASC LIMIT ?
                """,
                tuple(params + [int(scan_limit)]),
            ).fetchall()
        for r in rows:
            rec = self._task_row(r)
            if int(rec.get("sub_count") or 0) >= int(rec.get("max_labels") or 1):
                continue  # already at capacity — second label landed
            rec.pop("sub_count", None)
            return rec
        return None

    def get_agreement_observation(self, task_id: str) -> Optional[Dict[str, Any]]:
        """The double-label agreement observation for one task, if it exists —
        the source of truth for a record's ``independent_second_label`` flag
        (PRD A Phase 3)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM agreement WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            return None
        rec = dict(row)
        rec["tags_a"] = json.loads(rec.pop("tags_a_json", "[]") or "[]")
        rec["tags_b"] = json.loads(rec.pop("tags_b_json", "[]") or "[]")
        return rec
    # ═══ END PRD-A ═══
    # ═══ PRD-C HEALTH SYSTEM STORE METHODS — owned by Agent 3, do not edit from other PRDs ═══
    @staticmethod
    def hs_id_for_name(name: str) -> str:
        """Deterministic ``hs-<slug>-<6hex>`` from the organization name, so the
        boot-time backfill mints the same id on every run (idempotent)."""
        norm = " ".join((name or "").lower().split())
        words = re.findall(r"[a-z0-9]+", norm)
        slug = "-".join(words)[:24].strip("-") or "org"
        hexpart = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:6]
        return f"hs-{slug}-{hexpart}"

    def _backfill_health_systems(self, conn: sqlite3.Connection) -> None:
        """One ``health_systems`` row per distinct historical partner label, and a
        ``health_system_id`` stamp on that partner's uploads — so no pre-portal
        upload is orphaned. Idempotent: matches by (case-insensitive) name and
        only touches uploads still missing a health_system_id."""
        labels: Dict[str, str] = {}
        for r in conn.execute("SELECT provider_id, org_name, email FROM data_providers").fetchall():
            # An email address is not an organization name (C-5.6). Where
            # org_name is blank there is nothing human to show, so the row is
            # named for manual correction rather than rendered as if a hospital
            # were literally called "it@mercy.org".
            label = (r["org_name"] or "").strip() or _needs_naming(r["email"])
            if label:
                labels[r["provider_id"]] = label
        for r in conn.execute("SELECT DISTINCT partner_id, partner_label FROM ingest_upload_links").fetchall():
            label = (r["partner_label"] or "").strip() or _needs_naming(r["partner_id"])
            if label:
                labels.setdefault(r["partner_id"], label)
        for r in conn.execute(
            "SELECT DISTINCT partner_id FROM ingest_uploads WHERE health_system_id IS NULL"
        ).fetchall():
            pid = (r["partner_id"] or "").strip()
            # 'account' is the LINK_ID sentinel and never a partner_id, so the
            # old guard here was dead — which is how an internal user id such as
            # 'u_abc123' ended up rendering in the Health Systems table as an
            # organization.
            if pid:
                labels.setdefault(pid, _needs_naming(pid))
        for partner_id, label in labels.items():
            row = conn.execute(
                "SELECT hs_id FROM health_systems WHERE LOWER(name) = LOWER(?)", (label,)
            ).fetchone()
            if not row:
                # UPGRADE PATH: a database written by the previous release may
                # already hold this partner under its raw internal id or email
                # (the name C-5.6 stopped producing). Rename that row in place
                # rather than inserting a second one — otherwise the operator
                # sees the same hospital twice with its upload history split
                # between them, and only on deployments that have run before.
                legacy = _legacy_partner_name(label)
                if legacy:
                    row = conn.execute(
                        "SELECT hs_id FROM health_systems WHERE LOWER(name) = LOWER(?)",
                        (legacy,),
                    ).fetchone()
                    if row:
                        conn.execute("UPDATE health_systems SET name = ? WHERE hs_id = ?",
                                     (label, row["hs_id"]))
            hs_id = row["hs_id"] if row else self.hs_id_for_name(label)
            if not row:
                conn.execute(
                    "INSERT OR IGNORE INTO health_systems (hs_id, name, active, created_at) "
                    "VALUES (?, ?, 1, ?)",
                    (hs_id, label, _utcnow_iso()),
                )
            conn.execute(
                "UPDATE ingest_uploads SET health_system_id = ? "
                "WHERE partner_id = ? AND health_system_id IS NULL",
                (hs_id, partner_id),
            )

    def ensure_health_system(self, name: str, *, contact_email: Optional[str] = None,
                             notes: Optional[str] = None) -> Dict[str, Any]:
        """Create-or-reuse by (case-insensitive) organization name."""
        clean = " ".join((name or "").split())
        if not clean:
            raise ValueError("health system name is required")
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM health_systems WHERE LOWER(name) = LOWER(?)", (clean,)
            ).fetchone()
            if row:
                if contact_email and not row["contact_email"]:
                    conn.execute("UPDATE health_systems SET contact_email = ? WHERE hs_id = ?",
                                 (contact_email, row["hs_id"]))
                    # Return the UPDATED values from this connection rather than
                    # re-reading (C-5.5): get_health_system opens a SECOND
                    # connection, and from inside this still-uncommitted block it
                    # read the pre-update row — so the caller got contact_email
                    # None while the committed row held the real address.
                    merged = dict(row)
                    merged["contact_email"] = contact_email
                    return merged
                return dict(row)
            hs_id = self.hs_id_for_name(clean)
            now = _utcnow_iso()
            conn.execute(
                "INSERT INTO health_systems (hs_id, name, contact_email, notes, active, created_at) "
                "VALUES (?, ?, ?, ?, 1, ?)",
                (hs_id, clean, contact_email, notes, now),
            )
            return {"hs_id": hs_id, "name": clean, "contact_email": contact_email,
                    "notes": notes, "active": 1, "created_at": now}

    def get_health_system(self, hs_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM health_systems WHERE hs_id = ?", (hs_id,)).fetchone()
        return dict(row) if row else None

    def list_health_systems(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM health_systems ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _hs_user_public(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        d.pop("password_hash", None)
        return d

    def create_hs_portal_user(self, *, username: str, hs_id: str, password: str,
                              email: Optional[str] = None) -> Dict[str, Any]:
        uname = (username or "").strip().lower()
        if not uname:
            raise ValueError("username is required")
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO hs_portal_users (username, hs_id, password_hash, must_reset, "
                "email, active, created_at) VALUES (?, ?, ?, 1, ?, 1, ?)",
                (uname, hs_id, hash_password(password), email, _utcnow_iso()),
            )
        return self.get_hs_portal_user_public(uname)  # type: ignore[return-value]

    def get_hs_portal_user(self, username: str) -> Optional[Dict[str, Any]]:
        """Full row including password_hash — internal auth use only."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM hs_portal_users WHERE username = ?", ((username or "").lower(),)
            ).fetchone()
        return dict(row) if row else None

    def get_hs_portal_user_public(self, username: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM hs_portal_users WHERE username = ?", ((username or "").lower(),)
            ).fetchone()
        return self._hs_user_public(row) if row else None

    def list_hs_portal_users(self, hs_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            if hs_id:
                rows = conn.execute(
                    "SELECT * FROM hs_portal_users WHERE hs_id = ? ORDER BY created_at DESC", (hs_id,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM hs_portal_users ORDER BY created_at DESC").fetchall()
        return [self._hs_user_public(r) for r in rows]

    def hs_username_exists(self, username: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM hs_portal_users WHERE username = ?", ((username or "").lower(),)
            ).fetchone()
        return row is not None

    def set_hs_portal_password(self, username: str, new_password: str, *,
                               must_reset: bool = False) -> None:
        """Set the password and stamp ``password_changed_at``. The stamp is what
        invalidates outstanding session cookies (FIX-C C-2.3) — without it a
        leaked cookie outlived the victim's own password reset by up to the full
        12-hour session TTL."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE hs_portal_users SET password_hash = ?, must_reset = ?, "
                "failed_logins = 0, locked_until = NULL, password_changed_at = ?, "
                "session_epoch = session_epoch + 1 WHERE username = ?",
                (hash_password(new_password), 1 if must_reset else 0, _utcnow_iso(),
                 (username or "").lower()),
            )

    def set_hs_portal_active(self, username: str, active: bool) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE hs_portal_users SET active = ? WHERE username = ?",
                         (1 if active else 0, (username or "").lower()))

    def set_health_system_active(self, hs_id: str, active: bool) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE health_systems SET active = ? WHERE hs_id = ?",
                         (1 if active else 0, hs_id))

    # ─── Brute-force bookkeeping, keyed on (username, ip) ────────────────────
    # ONE path for known and unknown usernames — see the schema note. Every
    # method here behaves identically whether or not the username exists, which
    # is the property that closes the enumeration oracle.
    @staticmethod
    def _hs_attempt_key(username: str, ip: str) -> str:
        ip_hash = hashlib.sha256((ip or "unknown").encode("utf-8")).hexdigest()[:16]
        return f"{(username or '').lower()}|{ip_hash}"

    def hs_login_locked(self, username: str, ip: str) -> bool:
        """True while (username, ip) is inside its lock window."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT locked_until FROM hs_login_attempts WHERE attempt_key = ?",
                (self._hs_attempt_key(username, ip),),
            ).fetchone()
        if not row or not row["locked_until"]:
            return False
        try:
            until = datetime.fromisoformat(row["locked_until"])
        except (TypeError, ValueError):
            return False
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return until > datetime.now(timezone.utc)

    def record_hs_login_failure(self, username: str, ip: str, *, lock_threshold: int = 5,
                                lock_minutes: int = 15) -> Dict[str, Any]:
        """Count a failed sign-in for (username, ip). Returns
        ``{"fails": n, "locked": bool}``.

        Counting happens BEFORE the caller decides the status code, and the
        decay window runs from the LAST failure for both the known and unknown
        case — the old split (record-after-check on one path, record-before on
        the other, decay from first vs fifth failure) is what leaked account
        existence on the 5th attempt.
        """
        uname = (username or "").lower()
        key = self._hs_attempt_key(uname, ip)
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        window_start = now - timedelta(minutes=lock_minutes)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT fails, last_fail_at FROM hs_login_attempts WHERE attempt_key = ?", (key,)
            ).fetchone()
            fails = 0
            if row:
                try:
                    last = datetime.fromisoformat(row["last_fail_at"] or "")
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)
                except (TypeError, ValueError):
                    last = None
                # A stale streak decays; a live one accumulates.
                fails = int(row["fails"] or 0) if (last and last > window_start) else 0
            fails += 1
            locked = fails >= lock_threshold
            until = (now + timedelta(minutes=lock_minutes)).isoformat() if locked else None
            conn.execute(
                "INSERT INTO hs_login_attempts (attempt_key, username, fails, first_fail_at, "
                "last_fail_at, locked_until) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(attempt_key) DO UPDATE SET fails = excluded.fails, "
                "last_fail_at = excluded.last_fail_at, locked_until = excluded.locked_until",
                (key, uname, fails, now_iso, now_iso, until),
            )
            # Opportunistic sweep: a username-spray attack mints one row per
            # (guessed name, ip) and nothing else would ever remove them, so the
            # table would grow without bound on a public endpoint. Rows past
            # their window carry no signal — they neither lock nor count.
            conn.execute(
                "DELETE FROM hs_login_attempts WHERE last_fail_at < ? "
                "AND (locked_until IS NULL OR locked_until < ?)",
                (window_start.isoformat(), now_iso),
            )
            # Username-scoped signal for the admin ("this account is being
            # attacked"). Deliberately NOT a gate: making it one is what turned
            # the lockout into a remote kill switch for a whole hospital.
            conn.execute(
                "UPDATE hs_portal_users SET failed_logins = failed_logins + 1 WHERE username = ?",
                (uname,),
            )
        return {"fails": fails, "locked": locked}

    def clear_hs_login_attempts(self, username: str, *, ip: Optional[str] = None) -> int:
        """Clear lock state for a username (optionally just from one ip).
        Returns the number of attempt rows cleared."""
        uname = (username or "").lower()
        with self._conn() as conn:
            if ip is not None:
                cur = conn.execute("DELETE FROM hs_login_attempts WHERE attempt_key = ?",
                                   (self._hs_attempt_key(uname, ip),))
            else:
                cur = conn.execute("DELETE FROM hs_login_attempts WHERE username = ?", (uname,))
            n = cur.rowcount or 0
            conn.execute(
                "UPDATE hs_portal_users SET failed_logins = 0, locked_until = NULL "
                "WHERE username = ?", (uname,),
            )
        return n

    def hs_login_failure_signal(self, username: str) -> int:
        """Live failure count for a username across all IPs — the admin-facing
        'under attack' signal, and the input to the progressive delay."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(fails), 0) FROM hs_login_attempts WHERE username = ?",
                ((username or "").lower(),),
            ).fetchone()
        return int(row[0] or 0)

    def mark_hs_login_success(self, username: str, ip: Optional[str] = None) -> None:
        self.clear_hs_login_attempts(username, ip=ip)
        with self._conn() as conn:
            conn.execute(
                "UPDATE hs_portal_users SET last_login = ? WHERE username = ?",
                (_utcnow_iso(), (username or "").lower()),
            )

    # ─── Session revocation ──────────────────────────────────────────────────
    def revoke_hs_token(self, jti: str, expires_at: str) -> None:
        """Denylist a session token until it would have expired anyway, so
        'Sign out' on a shared hospital workstation is real, not cosmetic."""
        if not jti:
            return
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO hs_revoked_tokens (jti, expires_at, revoked_at) "
                "VALUES (?, ?, ?)", (jti, expires_at, _utcnow_iso()),
            )
            # Opportunistic sweep — the denylist only needs to hold unexpired
            # tokens, so it stays small without a scheduled job.
            conn.execute("DELETE FROM hs_revoked_tokens WHERE expires_at < ?",
                         (datetime.now(timezone.utc).isoformat(),))

    def hs_token_revoked(self, jti: str) -> bool:
        if not jti:
            return False
        with self._conn() as conn:
            row = conn.execute("SELECT 1 FROM hs_revoked_tokens WHERE jti = ?", (jti,)).fetchone()
        return row is not None

    def hs_upload_bytes_since(self, hs_id: str, since_iso: str) -> int:
        """Bytes this health system has uploaded since ``since_iso`` — the input
        to the per-account quota (FIX-C C-2.6)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(size_bytes), 0) FROM ingest_uploads "
                "WHERE health_system_id = ? AND created_at >= ?", (hs_id, since_iso),
            ).fetchone()
        return int(row[0] or 0)

    def set_ingest_specialty_for_upload(self, upload_id: str, specialty: str) -> int:
        """Operator-assigned specialty for every case from one upload.

        Ingest leaves specialty NULL when nothing declared it (FIX-C C-3.2);
        promotion downstream still falls back to a literal, so the operator has
        to be able to set the real value BEFORE promoting rather than after a
        mislabeled case has shipped."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE ingest_cases SET specialty = ?, updated_at = ? WHERE upload_id = ?",
                (specialty, _utcnow_iso(), upload_id),
            )
            return cur.rowcount or 0

    def list_ingest_cases_for_uploads(self, upload_ids: List[str]) -> List[Dict[str, Any]]:
        """Every ingest case for a set of uploads in ONE query (FIX-C C-5.4).
        The health-system detail page issued one query per upload, up to 500."""
        if not upload_ids:
            return []
        out: List[Dict[str, Any]] = []
        with self._conn() as conn:
            # Chunked to stay under SQLite's variable limit on a large page.
            for i in range(0, len(upload_ids), 400):
                chunk = upload_ids[i:i + 400]
                qs = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"SELECT * FROM ingest_cases WHERE upload_id IN ({qs}) "
                    "ORDER BY created_at DESC", tuple(chunk),
                ).fetchall()
                for r in rows:
                    rec = dict(r)
                    rec["case"] = json.loads(rec.pop("case_json") or "null")
                    rec["report"] = json.loads(rec.pop("report_json") or "null")
                    rec["review"] = json.loads((rec.get("review_json") or "null") or "null")
                    out.append(rec)
        return out

    def upload_specialties(self, upload_id: str) -> List[Optional[str]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT specialty FROM ingest_cases WHERE upload_id = ?", (upload_id,)
            ).fetchall()
        return [r["specialty"] for r in rows]

    def set_upload_health_system(self, upload_id: str, hs_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE ingest_uploads SET health_system_id = ? WHERE upload_id = ?",
                (hs_id, upload_id),
            )

    def list_case_reviews_for_reviewer(self, user_id: str, *, limit: int = 200) -> List[Dict[str, Any]]:
        """A reviewer's case_reviews rows (PRD-A table, read-only from PRD-C).
        Defensive: before PRD-A merges the table does not exist — return []
        rather than 500 the physicians page."""
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT review_id, task_id, submission_id, verdict, created_at "
                    "FROM case_reviews WHERE reviewer_user_id = ? "
                    "ORDER BY created_at DESC LIMIT ?", (user_id, limit),
                ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            return []

    # Metrics — the four questions (PRD-C Phase 6). All reads, all defensive:
    # PRD-A's case_reviews may not exist yet, and a missing table must read as
    # "no data", never a 500 on the metrics page.
    _METRIC_SPARK_SOURCES = {
        # kind → (table, timestamp column, extra WHERE)
        "submissions": ("submissions", "created_at", ""),
        "reviews": ("case_reviews", "created_at", ""),
        "uploads": ("ingest_uploads", "created_at", ""),
        "exports": ("exports", "created_at", ""),
    }

    def metrics_daily_counts(self, kind: str, *, days: int = 14) -> List[int]:
        """Per-day row counts over the trailing window, oldest first — the
        sparkline series. Unknown/missing tables yield a flat zero series."""
        src = self._METRIC_SPARK_SOURCES.get(kind)
        if not src:
            return [0] * days
        table, col, extra = src
        start = (datetime.now(timezone.utc) - timedelta(days=days - 1)).date().isoformat()
        counts = {i: 0 for i in range(days)}
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    f"SELECT DATE({col}) AS d, COUNT(*) AS n FROM {table} "
                    f"WHERE DATE({col}) >= ? {extra} GROUP BY DATE({col})", (start,),
                ).fetchall()
        except sqlite3.OperationalError:
            return [0] * days
        base = datetime.now(timezone.utc).date()
        for r in rows:
            try:
                offset = (base - datetime.fromisoformat(r["d"]).date()).days
            except (TypeError, ValueError):
                continue
            if 0 <= offset < days:
                counts[days - 1 - offset] = int(r["n"])
        return [counts[i] for i in range(days)]

    def metrics_four_questions(self) -> Dict[str, Any]:
        """SUPPLY · QUALITY · PIPELINE · DEMAND — one rollup for the admin
        metrics header. Expert acceptance and Cohen's κ are computed and
        returned SEPARATELY (κ belongs to the caller's /stats agreement slice;
        acceptance comes from PRD-A's review verdicts here)."""
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        with self._conn() as conn:
            active_week = conn.execute(
                "SELECT COUNT(DISTINCT s.evaluator_id) FROM submissions s "
                "JOIN users u ON u.id = s.evaluator_id "
                "WHERE s.created_at >= ? AND u.is_mock = 0", (week_ago,),
            ).fetchone()[0]
            labeled_total = conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
            uploads_received = conn.execute("SELECT COUNT(*) FROM ingest_uploads").fetchone()[0]
            awaiting_review = conn.execute(
                "SELECT COUNT(*) FROM ingest_uploads WHERE status IN "
                "('received', 'scanning', 'parsing', 'needs_review')").fetchone()[0]
            promoted = conn.execute(
                "SELECT COUNT(*) FROM ingest_cases WHERE status = 'promoted'").fetchone()[0]
            buyer_requests = conn.execute("SELECT COUNT(*) FROM buyer_requests").fetchone()[0]
            exports_n = conn.execute("SELECT COUNT(*) FROM exports").fetchone()[0]
            shipped = conn.execute(
                "SELECT COUNT(*) FROM records WHERE status = 'exported'").fetchone()[0]
        # ONE definition of expert acceptance (Seam 3): PRD-A's
        # ``agreement.review_acceptance``. This block used to run its own SQL
        # that also counted edited-accept verdicts, while agreement.py counted
        # strict accepts only — the same word, over the same table, at two
        # numbers (~97% on the dashboard, ~84% in quality_report.md). It is the
        # figure a buyer audits most closely, so it gets one owner. The combined
        # figure is still available, under a DIFFERENT name ("not rejected").
        reviews: List[Dict[str, Any]] = []
        try:
            with self._conn() as conn:
                reviews = [dict(r) for r in conn.execute(
                    "SELECT verdict, dimension_json FROM case_reviews").fetchall()]
        except sqlite3.OperationalError:
            pass  # PRD-A not merged — reviews read as "no data", not zero-rate
        from asclepius import agreement as asc_agreement
        _review_acceptance = getattr(asc_agreement, "review_acceptance", None)
        if _review_acceptance is None:
            # The owner of the definition is not present (PRD-A not merged).
            # Report "unknown", never a locally-computed substitute — a second
            # definition is the defect, and a wrong number is worse than none.
            acc = {"n": len(reviews), "accept_rate": None, "edit_rate": None}
        else:
            acc = _review_acceptance(reviews)
        reviews_total = int(acc.get("n") or 0)
        acceptance = acc.get("accept_rate")
        edit_rate = acc.get("edit_rate")
        # "Not rejected" is a different number and therefore a different word.
        not_rejected = (None if (not reviews_total or acceptance is None)
                        else round((acceptance or 0) + (edit_rate or 0), 4))
        return {
            "supply": {
                "physicians_active_week": int(active_week or 0),
                "cases_labeled": int(labeled_total or 0),
                "cases_reviewed": int(reviews_total or 0),
                "spark": self.metrics_daily_counts("submissions"),
            },
            "quality": {
                # Tri-state: a float when reviews exist, null when none — "no
                # reviews yet" must never render as a 0% acceptance rate.
                # Strict accepts only, exactly as agreement.review_acceptance
                # defines it and as quality_report.md reports it.
                "expert_acceptance": acceptance,
                "edit_rate": edit_rate,
                "not_rejected": not_rejected,
                "reviews_scored": reviews_total,
                "spark": self.metrics_daily_counts("reviews"),
            },
            "pipeline": {
                "uploads_received": int(uploads_received or 0),
                "awaiting_review": int(awaiting_review or 0),
                "promoted_to_task": int(promoted or 0),
                "spark": self.metrics_daily_counts("uploads"),
            },
            "demand": {
                "buyer_requests": int(buyer_requests or 0),
                "exports": int(exports_n or 0),
                "records_shipped": int(shipped or 0),
                "spark": self.metrics_daily_counts("exports"),
            },
        }

    def count_case_reviews_for_tasks(self, task_ids: List[str]) -> int:
        """How many PRD-A case_reviews cover these tasks (read-only). Defensive:
        0 when the table does not exist yet (pre-merge-A)."""
        if not task_ids:
            return 0
        qs = ",".join("?" for _ in task_ids)
        try:
            with self._conn() as conn:
                row = conn.execute(
                    f"SELECT COUNT(*) FROM case_reviews WHERE task_id IN ({qs})",
                    tuple(task_ids),
                ).fetchone()
            return int(row[0] or 0)
        except sqlite3.OperationalError:
            return 0

    def list_uploads_for_health_system(self, hs_id: str, *, limit: int = 500) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM ingest_uploads WHERE health_system_id = ? "
                "ORDER BY created_at DESC LIMIT ?", (hs_id, limit),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["files"] = json.loads(d.pop("files_json") or "[]")
            out.append(d)
        return out
    # ═══ END PRD-C STORE METHODS ═══
    # ═══ PRD-D ADVISOR STORE METHODS — owned by the advisor tier, do not edit from other PRDs ═══
    # Appointment, referrals, and advisory sign-off. Appended per START_HERE
    # §3.2 — existing methods above are never modified.

    # ─── Appointment ─────────────────────────────────────────────────────────
    def _mint_referral_code(self, conn: sqlite3.Connection) -> str:
        """A short, speakable, collision-checked code. Advisors read these to
        people over the phone, so no ambiguous glyphs (0/O, 1/I/l).

        The SELECT is an optimisation, not the guarantee — the partial unique
        index on ``users(referral_code)`` is (audit L7). Two concurrent
        appointments could both see a free code and race; the loser used to
        surface as a 500. Retried here instead, so a race is invisible to the
        caller rather than an error page on the one action that mints an
        advisor.
        """
        alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
        for _ in range(12):
            code = "".join(secrets.choice(alphabet) for _ in range(8))
            row = conn.execute(
                "SELECT 1 FROM users WHERE referral_code = ?", (code,)).fetchone()
            if row is None:
                return code
        # 31^8 with a dozen tries: reaching here means something is very wrong
        # (a code column full of one value), and a silent duplicate would break
        # referral attribution invisibly.
        raise RuntimeError("could not mint a unique referral code")

    def appoint_advisor(
        self,
        user_id: str,
        *,
        agreement_ref: str,
        appointed_by: str,
        approve: bool = False,
        note: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Promote a user to the advisor tier — the WHOLE appointment, atomically.

        Every term of the relationship is one decision and therefore one
        statement: ``tier='advisor'``, ``compensation_model='equity_only'`` (an
        advisor is not paid per task — see compensation.py), the appointment
        stamp, the signed-agreement reference, the referral code and the Slack
        label. With ``approve=True`` the verification stamp
        (``verification_status``/``verified_by``/``verified_at``) lands in the
        SAME update.

        That single-statement property is the point, not a tidiness preference
        (audit H1). Both appointment doors previously did two writes, and the
        verification-queue door did them in the order where a crash in between
        leaves an account APPROVED and LIVE with ``tier='advisor'``, no
        agreement on file, and ``compensation_model`` NULL — full advisory
        capability, no signed agreement, and a payment predicate that would
        happily pay an equity-only advisor. There is no ordering of two writes
        that is safe, so there is one write.

        Idempotent on the referral code: re-appointing keeps the code an advisor
        may already have handed out. ``agreement_ref`` is re-checked here — the
        routers check it too, but this method is the last line and an advisor
        with equity and no agreement on file is a liability.
        """
        agreement_ref = (agreement_ref or "").strip()
        if not agreement_ref:
            raise ValueError("appoint_advisor requires a signed agreement reference")
        # A lost code race is retried rather than surfaced (audit L7): the
        # unique index is the guarantee, and two admins appointing at the same
        # moment should not produce a 500 on the action that mints an advisor.
        for attempt in range(3):
            try:
                return self._appoint_advisor_once(
                    user_id, agreement_ref=agreement_ref, appointed_by=appointed_by,
                    approve=approve, note=note)
            except sqlite3.IntegrityError:
                if attempt == 2:
                    raise
        return None  # pragma: no cover - unreachable; the loop returns or raises

    def _appoint_advisor_once(
        self,
        user_id: str,
        *,
        agreement_ref: str,
        appointed_by: str,
        approve: bool = False,
        note: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        now = _utcnow_iso()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT referral_code, advisor_since FROM users WHERE id = ?",
                (user_id,)).fetchone()
            if row is None:
                return None
            code = row["referral_code"] or self._mint_referral_code(conn)
            approval_sql = ""
            params: List[Any] = [now, appointed_by, now, agreement_ref, code]
            if approve:
                # Appended to the same UPDATE rather than issued as a second
                # statement — see the docstring. ``verification_notes`` APPENDS
                # (audit H5): the reason a physician was previously rejected is
                # the most important note in the table and must not be
                # overwritten by an appointment.
                approval_sql = (
                    ", verification_status = 'approved'"
                    ", verified_by = ?"
                    ", verified_at = ?"
                    ", verification_notes = CASE"
                    "    WHEN ? IS NULL THEN verification_notes"
                    "    WHEN verification_notes IS NULL OR TRIM(verification_notes) = ''"
                    "      THEN ?"
                    "    ELSE verification_notes || char(10) || ?"
                    "  END"
                )
                params.extend([appointed_by, now, note, note, note])
            params.append(user_id)
            conn.execute(
                f"""
                UPDATE users SET
                    tier = 'advisor',
                    tier_assigned_at = ?,
                    tier_assigned_by = ?,
                    compensation_model = 'equity_only',
                    advisor_since = COALESCE(advisor_since, ?),
                    advisor_agreement_ref = ?,
                    referral_code = ?,
                    slack_role = 'Medical Advisor'
                    {approval_sql}
                WHERE id = ?
                """,
                tuple(params),
            )
        return self.get_user_by_id(user_id)

    def set_advisor_display_name(self, user_id: str, full_name: str) -> None:
        """Fill in a name the appointment supplied, WITHOUT overwriting one the
        physician already gave us. COALESCE on the existing value, not the new
        one: an admin typing a name into the appointment form must never
        clobber the verified legal name on a credential record."""
        name = (full_name or "").strip()
        if not name:
            return
        with self._conn() as conn:
            conn.execute(
                "UPDATE users SET full_name = COALESCE(NULLIF(TRIM(full_name), ''), ?) "
                "WHERE id = ?", (name, user_id))

    def get_user_by_referral_code(self, code: str) -> Optional[Dict[str, Any]]:
        code = (code or "").strip().upper()
        if not code:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE referral_code = ?", (code,)).fetchone()
        return dict(row) if row else None

    # ─── Referrals (PRD §3) ──────────────────────────────────────────────────
    def insert_referral(
        self,
        *,
        referrer_id: str,
        referral_code: str,
        invitee_email: Optional[str] = None,
        invitee_name: Optional[str] = None,
        note: Optional[str] = None,
        status: Optional[str] = "invited",
    ) -> Dict[str, Any]:
        rid = _new_id("ref")
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO referrals (referral_id, referrer_id, referral_code,
                                       invitee_email, invitee_name, note, status, invited_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (rid, referrer_id, referral_code,
                 (invitee_email or "").lower().strip() or None,
                 invitee_name, note, status, _utcnow_iso()),
            )
        return self.get_referral(rid)  # type: ignore[return-value]

    def get_referral(self, referral_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM referrals WHERE referral_id = ?", (referral_id,)).fetchone()
        return dict(row) if row else None

    def list_referrals_by_referrer(self, referrer_id: str,
                                   *, limit: int = 500) -> List[Dict[str, Any]]:
        """Every referral THIS advisor made. Scoped by the caller's session id —
        never by a query parameter (the IDOR rule from the portal work).

        Bounded (audit L6): every other list method in this file takes a limit,
        and an unbounded SELECT on a table that only grows is a slow leak rather
        than a bug you notice.
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM referrals WHERE referrer_id = ? "
                "ORDER BY invited_at DESC LIMIT ?",
                (referrer_id, max(1, limit))).fetchall()
        return [dict(r) for r in rows]

    def referral_counts_by_referrer(self, referrer_ids: List[str]) -> Dict[str, Dict[str, int]]:
        """{referrer_id: {total, active}} for a page of advisors in ONE query.

        The admin roster previously ran two queries per advisor inside a loop
        over a full user scan (audit L6), each on a fresh SQLite connection.
        """
        ids = [i for i in (referrer_ids or []) if i]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT referrer_id, "
                f"       COUNT(*) AS total, "
                f"       SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END) AS active "
                f"FROM referrals WHERE referrer_id IN ({placeholders}) "
                f"GROUP BY referrer_id",
                tuple(ids)).fetchall()
        return {r["referrer_id"]: {"total": int(r["total"] or 0),
                                   "active": int(r["active"] or 0)} for r in rows}

    def signoff_counts_by_advisor(self, advisor_ids: List[str]) -> Dict[str, int]:
        """{advisor_id: n} in one query — same reason as above."""
        ids = [i for i in (advisor_ids or []) if i]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT advisor_id, COUNT(*) AS n FROM advisory_signoffs "
                f"WHERE advisor_id IN ({placeholders}) GROUP BY advisor_id",
                tuple(ids)).fetchall()
        return {r["advisor_id"]: int(r["n"] or 0) for r in rows}

    def find_open_referral_for_email(self, email: str) -> Optional[Dict[str, Any]]:
        """The newest referral for this email that has not yet been claimed by a
        signup. Resolution is by email because that is the identifier the invite
        was addressed to and the one the invitee signs up with; a code that has
        to survive a React landing app, a tenant-store wizard and two redirects
        is a code that arrives NULL and attributes the referral to nobody."""
        email = (email or "").lower().strip()
        if not email:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM referrals WHERE invitee_email = ? AND user_id IS NULL "
                "ORDER BY invited_at DESC LIMIT 1",
                (email,)).fetchone()
        return dict(row) if row else None

    def count_recent_referrals_for_email(self, email: str, *, hours: int = 24) -> int:
        """How many times this address has been invited recently, by ANYONE.

        Mirrors the public onboarding path's per-email pending cap (audit H4):
        without it, the same inbox can be mailed without bound by rotating the
        referring advisor, which is both a spam vector from a verified sending
        domain and a way to bury a real invite.
        """
        email = (email or "").lower().strip()
        if not email:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, hours))
                  ).replace(tzinfo=None).isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM referrals WHERE invitee_email = ? "
                "AND invited_at >= ?", (email, cutoff)).fetchone()
        return int(row["n"] if row else 0)

    def has_referral_for_email(self, referrer_id: str, email: str) -> bool:
        """True when this advisor already invited this address — so a second
        click reports 'already invited' rather than minting a duplicate row."""
        email = (email or "").lower().strip()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM referrals WHERE referrer_id = ? AND invitee_email = ?",
                (referrer_id, email)).fetchone()
        return row is not None

    # The funnel, in order. Status only ever moves FORWARD: a later NPI recheck
    # or a re-onboard must never walk an 'approved' referral back to
    # 'signed_up'. NULL ("we have not heard back") sorts before 'invited'.
    _REFERRAL_LADDER = ("invited", "signed_up", "verified", "approved")

    def advance_referral(self, referral_id: str, status: str) -> Optional[Dict[str, Any]]:
        """Move a referral forward along the funnel, or to a terminal
        'declined'. Monotonic by design — see ``_REFERRAL_LADDER``."""
        ref = self.get_referral(referral_id)
        if ref is None:
            return None
        current = ref.get("status")
        if status != "declined" and current in self._REFERRAL_LADDER and status in self._REFERRAL_LADDER:
            if self._REFERRAL_LADDER.index(status) <= self._REFERRAL_LADDER.index(current):
                return ref
        terminal = status in ("approved", "declined")
        with self._conn() as conn:
            conn.execute(
                "UPDATE referrals SET status = ?, resolved_at = CASE WHEN ? THEN ? "
                "ELSE resolved_at END WHERE referral_id = ?",
                (status, 1 if terminal else 0, _utcnow_iso(), referral_id),
            )
        return self.get_referral(referral_id)

    def claim_referral_for_signup(self, *, email: str, user_id: str) -> Optional[Dict[str, Any]]:
        """Attach a fresh signup to the referral(s) that invited them.

        ALL open referrals for the address resolve, not just the first (audit
        M5). Two advisors can both invite the same physician — the interesting
        case, since a well-connected candidate is exactly who gets referred
        twice — and claiming only one left the other sitting at ``invited``
        forever, rendering in that advisor's funnel as still pending long after
        the person joined. Attribution is not exclusive: both advisors did in
        fact refer them, and the funnel should say so.
        """
        email = (email or "").lower().strip()
        if not email:
            return None
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT referral_id FROM referrals WHERE invitee_email = ? "
                "AND user_id IS NULL ORDER BY invited_at ASC", (email,)).fetchall()
            if not rows:
                return None
            conn.execute(
                "UPDATE referrals SET user_id = ?, status = 'signed_up' "
                "WHERE invitee_email = ? AND user_id IS NULL",
                (user_id, email),
            )
        # The earliest invite is returned as "the" referral for logging; every
        # one of them is now resolved.
        return self.get_referral(rows[0]["referral_id"])

    def advance_referral_for_user(self, user_id: str, status: str) -> Optional[Dict[str, Any]]:
        """Move EVERY referral that claimed this user along the funnel.

        Used by the verification decision points, which know a user id and not a
        referral id. Plural for the same reason ``claim_referral_for_signup`` is
        (audit M5): two advisors may have referred the same physician, and
        advancing only one leaves the other's funnel permanently wrong.
        """
        if not user_id:
            return None
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT referral_id FROM referrals WHERE user_id = ? "
                "ORDER BY invited_at ASC", (user_id,)).fetchall()
        out = None
        for row in rows:
            out = self.advance_referral(row["referral_id"], status) or out
        return out

    # ─── Advisory sign-off (PRD §4) ──────────────────────────────────────────
    def insert_advisory_signoff(
        self,
        *,
        artifact_type: str,
        artifact_id: str,
        advisor_id: str,
        verdict: str,
        comments: Optional[str],
        relationship: str,
        subject_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Record one advisory verdict. ``relationship`` is supplied by the
        ROUTER from the advisor's own user row and is never accepted from the
        client — the disclosure is only worth something if the subject of it
        cannot write it.

        ``subject_ids`` pins WHAT was signed off (audit M3), which matters for
        artifact ids that are derived rather than stored: a task batch is
        ``specialty:YYYY-MM-DD`` over open tasks, so without this the membership
        keeps changing after the attestation and later work inherits an approval
        nobody gave it.
        """
        sid = _new_id("sgn")
        ids = [str(i) for i in (subject_ids or []) if i]
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO advisory_signoffs (signoff_id, artifact_type, artifact_id,
                                               advisor_id, verdict, comments,
                                               relationship, created_at,
                                               subject_ids, subject_n)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (sid, artifact_type, artifact_id, advisor_id, verdict,
                 (comments or "").strip() or None, relationship, _utcnow_iso(),
                 json.dumps(ids) if ids else None, len(ids) if ids else None),
            )
        return self.get_advisory_signoff(sid)  # type: ignore[return-value]

    @staticmethod
    def _signoff_row(row: sqlite3.Row) -> Dict[str, Any]:
        rec = dict(row)
        raw = rec.pop("subject_ids", None)
        rec["subject_ids"] = json.loads(raw) if raw else []
        return rec

    def get_advisory_signoff(self, signoff_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM advisory_signoffs WHERE signoff_id = ?",
                (signoff_id,)).fetchone()
        return self._signoff_row(row) if row else None

    def list_advisory_signoffs(
        self,
        *,
        artifact_type: Optional[str] = None,
        artifact_id: Optional[str] = None,
        advisor_id: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if artifact_type:
            clauses.append("artifact_type = ?")
            params.append(artifact_type)
        if artifact_id:
            clauses.append("artifact_id = ?")
            params.append(artifact_id)
        if advisor_id:
            clauses.append("advisor_id = ?")
            params.append(advisor_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM advisory_signoffs {where} ORDER BY created_at DESC LIMIT ?",
                tuple(params)).fetchall()
        return [self._signoff_row(r) for r in rows]

    # Verdict severity, worst first. The operator-facing status must reflect the
    # WORST outstanding verdict, not the most recent one (audit M2): with two
    # advisors, one approving after another requested changes would otherwise
    # erase the objection from the field an operator reads before shipping.
    # ``created_at`` is second-granularity, so "most recent" is not even
    # well-defined for same-second writes.
    _VERDICT_SEVERITY = {"changes_requested": 2, "approved_with_comments": 1, "approved": 0}

    @classmethod
    def worst_verdict(cls, verdicts: List[str]) -> Optional[str]:
        """The verdict an operator needs to see. Unknown values sort worst —
        an unrecognized verdict is not evidence of approval."""
        known = [v for v in verdicts if v]
        if not known:
            return None
        return max(known, key=lambda v: cls._VERDICT_SEVERITY.get(v, 99))

    def signoff_summary(self, artifact_type: str, artifact_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """{artifact_id: {n, verdict, latest_verdict, latest_at, verdicts}} for a
        page of artifacts — one query, not one per row, so the admin list does
        not degrade with the number of exports.

        ``verdict`` is the WORST outstanding verdict and is the one to display;
        ``latest_verdict`` is kept for anyone who genuinely wants recency, and
        the full list rides along so a caller can render "2 approved · 1 change
        requested" without a second query.
        """
        ids = [i for i in (artifact_ids or []) if i]
        if not ids:
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        placeholders = ",".join("?" for _ in ids)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT artifact_id, verdict, created_at FROM advisory_signoffs "
                f"WHERE artifact_type = ? AND artifact_id IN ({placeholders}) "
                f"ORDER BY created_at ASC",
                tuple([artifact_type] + ids)).fetchall()
        for r in rows:
            cur = out.setdefault(r["artifact_id"], {"n": 0, "verdict": None,
                                                    "latest_verdict": None,
                                                    "latest_at": None, "verdicts": []})
            cur["n"] += 1
            cur["verdicts"].append(r["verdict"])
            cur["latest_verdict"] = r["verdict"]   # ASC order -> last write wins
            cur["latest_at"] = r["created_at"]
        for cur in out.values():
            cur["verdict"] = self.worst_verdict(cur["verdicts"])
        return out

    def set_signoff_status(self, table: str, id_column: str, artifact_id: str,
                           status: Optional[str]) -> None:
        """Mirror the latest verdict onto the artifact's own row so admin lists
        can show it without a join. Advisory only — nothing reads this to decide
        whether work may proceed (PRD §4.4 as amended: sign-off never blocks an
        export or a shipment)."""
        allowed = {
            "exports": "export_id",
            "tasks": "task_id",
            "ingest_uploads": "upload_id",
        }
        # The table/column names are interpolated into SQL, so they are checked
        # against a literal allowlist rather than trusted from the caller.
        if allowed.get(table) != id_column:
            raise ValueError(f"refusing to write signoff_status to {table}.{id_column}")
        with self._conn() as conn:
            conn.execute(
                f"UPDATE {table} SET signoff_status = ? WHERE {id_column} = ?",
                (status, artifact_id))

    # ─── Task batches (PRD §4.2, artifact_type='task_batch') ─────────────────
    # There is no persisted generation-batch id on ``tasks``, and inventing one
    # would need a backfill that could only guess at history. The batch key is
    # DERIVED instead — specialty plus generation date over still-open tasks —
    # so it is stable, re-derivable, and means something to a human ("the
    # nephrology cases from the 3rd"). If a real batch id is ever persisted,
    # this method is the single place that changes.
    @staticmethod
    def task_batch_key(task: Dict[str, Any]) -> str:
        created = str(task.get("created_at") or "")[:10] or "unknown"
        return f"{task.get('specialty') or 'general'}:{created}"

    def list_open_task_batches(self, *, limit: int = 200) -> List[Dict[str, Any]]:
        """Open (not yet labeled out) tasks grouped into previewable batches.

        Counted with GROUP BY rather than by scanning a capped list of tasks and
        tallying in Python (audit M3). The old form applied a global 500-task
        cap and then grouped, so with enough open work a batch's ``n_tasks``
        silently under-reported — and the artifact view applied a *different*
        cap, so the same batch was two different sizes depending on which screen
        you were looking at. ``limit`` here bounds the number of BATCHES
        returned, not the count within one.

        Deliberately NO ``signoff_status``: a batch spans many task rows and the
        sign-off is not mirrored onto any of them, so reporting one row's column
        as the batch's status would be a field that looks authoritative and is
        whichever task happened to sort first. Callers read ``signoff_summary``.
        """
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT specialty,
                       substr(created_at, 1, 10) AS created_on,
                       COUNT(*)                  AS n_tasks
                FROM tasks
                WHERE status = 'open'
                GROUP BY specialty, substr(created_at, 1, 10)
                ORDER BY created_on DESC, specialty ASC
                LIMIT ?
                """,
                (limit,)).fetchall()
        return [
            {"batch_key": f"{r['specialty'] or 'general'}:{r['created_on'] or 'unknown'}",
             "specialty": r["specialty"],
             "created_on": r["created_on"],
             "n_tasks": r["n_tasks"]}
            for r in rows
        ]

    def list_tasks_in_batch(self, batch_key: str, *, limit: int = 50) -> List[Dict[str, Any]]:
        """Open tasks belonging to a derived batch key.

        The key is ``specialty:YYYY-MM-DD``, so both halves are pushed into SQL
        rather than filtered in Python: this is called on every sign-off POST
        (to prove the artifact exists) and scanning every open task each time
        would put an avoidable full scan on the single SQLite writer that
        labeler submissions also need.
        """
        specialty, _, created_on = (batch_key or "").rpartition(":")
        if not specialty or not created_on:
            return []
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status = 'open' AND specialty = ? "
                "AND substr(created_at, 1, 10) = ? ORDER BY created_at DESC LIMIT ?",
                (specialty, created_on, limit)).fetchall()
        return [self._task_row(r) for r in rows]

    # ─── Product specs (PRD §4.2, artifact_type='product_spec') ──────────────
    def insert_product_spec(self, *, title: str, body_md: str,
                            created_by: Optional[str]) -> Dict[str, Any]:
        sid = _new_id("spec")
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO product_specs (spec_id, title, body_md, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (sid, title, body_md, created_by, _utcnow_iso()))
        return self.get_product_spec(sid)  # type: ignore[return-value]

    def get_product_spec(self, spec_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM product_specs WHERE spec_id = ?", (spec_id,)).fetchone()
        return dict(row) if row else None

    def list_product_specs(self, *, limit: int = 100) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM product_specs ORDER BY created_at DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ─── Compensation (PRD §2.4) ─────────────────────────────────────────────
    def submissions_for_payment(self, *, limit: int = 1000) -> List[Dict[str, Any]]:
        """Completed submissions that should accrue money.

        There is NO payment feature today. This exists so that when one is
        built, the person building it finds a query that already excludes
        equity-only advisors instead of writing a fresh ``SELECT * FROM
        submissions`` that quietly bills the company for volunteer work.
        See ``asclepius/compensation.py`` — NULL is payable on purpose.
        """
        from asclepius.compensation import PAYABLE_SQL

        # LEFT, not INNER (audit M6). compensation.py argues at length that
        # under-paying a physician who did the work is worse than over-counting
        # a volunteer — and an INNER JOIN silently drops any submission whose
        # user row is missing, which is under-payment by a one-word typo. The
        # NULL a LEFT JOIN produces is then handled correctly by PAYABLE_SQL,
        # written `IS NULL OR != 'equity_only'` precisely so three-valued logic
        # cannot swallow it.
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT s.submission_id, s.evaluator_id, s.task_id, s.status, s.created_at
                FROM submissions s
                LEFT JOIN users u ON u.id = s.evaluator_id
                WHERE s.status != 'rejected' AND {PAYABLE_SQL}
                ORDER BY s.created_at DESC LIMIT ?
                """,
                (limit,)).fetchall()
        return [dict(r) for r in rows]
    # ═══ END PRD-D STORE METHODS ═══

    # ═══ PRD-I INGESTION STORE METHODS — owned by Agent I, do not edit elsewhere ═══
    # ─── Purpose (PRD-I §2) ──────────────────────────────────────────────────
    def set_upload_link_purpose(self, link_id: str, purpose: Optional[str]) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE ingest_upload_links SET purpose = ? WHERE link_id = ?",
                         (purpose, link_id))

    def set_hs_portal_purpose(self, username: str, purpose: Optional[str]) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE hs_portal_users SET purpose = ? WHERE username = ?",
                         (purpose, (username or "").lower()))

    def hs_portal_account_has_activity(self, username: str) -> bool:
        """Has this account sent anything, or started to?

        Purpose is resolved LIVE at completion so the two upload doors always
        agree — which also means changing an account's purpose reaches bytes that
        are already in flight. This is how a caller tells "correcting a fresh
        mis-click" (no activity) from "converting a partner's data" (any)."""
        uname = (username or "").lower()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT ("
                "  (SELECT COUNT(*) FROM ingest_upload_sessions "
                "     WHERE actor = ? AND status IS NOT 'aborted') + "
                "  (SELECT COUNT(*) FROM events "
                "     WHERE actor = ? AND event_type = 'upload_received')"
                ") AS n", (uname, uname)).fetchone()
        return bool(row and int(row["n"] or 0) > 0)

    def hs_purposes_for(self, hs_id: str) -> List[Optional[str]]:
        """Every distinct purpose across a health system's ACTIVE portal accounts.

        For ADMIN DISPLAY only. Uploads are stamped from the specific account that
        sent them (``attach_upload_provenance``), never from this aggregate — an
        organization may legitimately hold one account of each kind, and picking a
        winner between them is how a brokering upload would acquire a
        task_creation stamp."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT purpose FROM hs_portal_users "
                "WHERE hs_id = ? AND active = 1", (hs_id,)).fetchall()
        return [r["purpose"] for r in rows]

    def attach_upload_provenance(
        self, upload_id: str, *, portal_username: Optional[str] = None,
        session_id: Optional[str] = None, link_id: Optional[str] = None,
    ) -> None:
        """Record where an upload came from, and copy forward what that implies.

        The caller names the ORIGIN — the portal account, the chunked session, or
        the magic link — and this resolves everything derived from it by joining
        server-side, LIVE, at this instant. Deliberately not a
        ``set_purpose(value)`` call: the upload doors have no business holding a
        purpose value, so they are given no way to express one. Nothing a provider
        sends reaches this.

        All three doors resolve through this one function, which is what makes
        "the same bytes are recorded the same way whichever door they came in"
        true by construction rather than by three implementations agreeing."""
        now = _utcnow_iso()
        with self._conn() as conn:
            if session_id:
                # Resolved LIVE from the account the session belongs to, never
                # from a value snapshotted when the session opened. A session
                # lives 24 h and resumes across that window, so a snapshot taken
                # at declare is stale for every byte that arrives after an admin
                # corrects the mint — and the multipart door, which resolves live,
                # would record the same bytes differently. Two doors that disagree
                # about what an upload is for is the same defect class as the
                # cross-account hijack: the answer must not depend on which door
                # or which moment.
                conn.execute(
                    "UPDATE ingest_uploads SET purpose = (SELECT p.purpose FROM "
                    "hs_portal_users p JOIN ingest_upload_sessions s "
                    "ON s.actor = p.username WHERE s.session_id = ?), "
                    "updated_at = ? WHERE upload_id = ?",
                    (session_id, now, upload_id))
            elif portal_username:
                conn.execute(
                    "UPDATE ingest_uploads SET purpose = (SELECT purpose FROM "
                    "hs_portal_users WHERE username = ?), updated_at = ? "
                    "WHERE upload_id = ?",
                    ((portal_username or "").lower(), now, upload_id))
            elif link_id:
                # The magic-link door. Same shape as the other two: the caller
                # names the authorizing ROW and the value is joined here, so the
                # door itself never handles a purpose.
                conn.execute(
                    "UPDATE ingest_uploads SET purpose = (SELECT purpose FROM "
                    "ingest_upload_links WHERE link_id = ?), updated_at = ? "
                    "WHERE upload_id = ?", (link_id, now, upload_id))

    def set_upload_purpose(self, upload_id: str, purpose: Optional[str]) -> None:
        """Admin-side correction only (resolving a legacy row). The upload doors
        do not call this — they call ``attach_upload_provenance``."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE ingest_uploads SET purpose = ?, updated_at = ? WHERE upload_id = ?",
                (purpose, _utcnow_iso(), upload_id))

    def set_ingest_case_purpose(self, ingest_case_id: str, purpose: Optional[str]) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE ingest_cases SET purpose = ?, updated_at = ? WHERE ingest_case_id = ?",
                (purpose, _utcnow_iso(), ingest_case_id))

    def propagate_purpose_to_cases(self, upload_id: str) -> int:
        """Copy the upload's purpose onto every case it produced, server-side.

        A JOIN from the authorizing row, never a value a client sent: the purpose
        is not accepted from a request body anywhere, so there is no path by which
        a provider could influence it."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE ingest_cases SET purpose = "
                "(SELECT purpose FROM ingest_uploads WHERE upload_id = ?), "
                "updated_at = ? WHERE upload_id = ?",
                (upload_id, _utcnow_iso(), upload_id))
            return int(cur.rowcount or 0)

    def ingest_case_effective_purpose(self, ingest_case_id: str) -> Optional[str]:
        """The purpose the PROMOTION GATE must read: the case's own, falling back
        to its upload's.

        The fallback is the point. Purpose is copied onto cases at the end of
        ingest, and that copy is best-effort — it must never strand an upload, so
        a failure there is logged and swallowed. Reading only the case column would
        turn that swallowed failure into a brokering case with a NULL purpose,
        which the gate resolves as task_creation. Fail-open on the one check whose
        whole job is to fail closed. COALESCE removes the possibility rather than
        relying on the copy having happened."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(c.purpose, u.purpose) AS purpose FROM ingest_cases c "
                "LEFT JOIN ingest_uploads u ON u.upload_id = c.upload_id "
                "WHERE c.ingest_case_id = ?", (ingest_case_id,)).fetchone()
        return row["purpose"] if row else None

    def ingest_case_purposes_for_upload(self, upload_id: str) -> Dict[str, Optional[str]]:
        """``{ingest_case_id: effective purpose}`` for a whole upload — one query
        for the batch promote, same COALESCE semantics as above."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT c.ingest_case_id, COALESCE(c.purpose, u.purpose) AS purpose "
                "FROM ingest_cases c LEFT JOIN ingest_uploads u "
                "ON u.upload_id = c.upload_id WHERE c.upload_id = ?",
                (upload_id,)).fetchall()
        return {r["ingest_case_id"]: r["purpose"] for r in rows}

    def mark_upload_verified(self, upload_id: str, *, verified_at: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE ingest_uploads SET verified_at = ?, updated_at = ? WHERE upload_id = ?",
                (verified_at, _utcnow_iso(), upload_id))

    # ─── Chunked upload sessions (PRD-I §1.1) ────────────────────────────────
    def create_upload_session(
        self, *, owner_kind: str, owner_id: str, actor: Optional[str],
        filename: Optional[str], content_type: Optional[str],
        declared_sha256: str, declared_size: int, chunk_size: int,
        part_count: int, storage_root: str, portal_username: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Open a session. ``portal_username`` names the ACCOUNT that authorized it;
        anything derived from that account is resolved here by a server-side join,
        so the upload door never handles the derived value (PRD-I §3.1)."""
        sid = _new_id("ups")
        now = _utcnow_iso()
        # Derived from the SERVER-minted session id, so no component of the parts
        # directory is ever client-controlled.
        storage_dir = os.path.join(storage_root, sid)
        # ``actor`` is the ONLY record of who authorized this session, and it is
        # what everything derived is joined through at completion. Nothing is
        # snapshotted here: a session lives 24 h and a stored copy of a mutable
        # admin decision is stale the moment the admin changes it.
        actor = (portal_username or actor or "").lower() or None
        try:
            with self._conn() as conn:
                conn.execute(
                    """INSERT INTO ingest_upload_sessions
                       (session_id, owner_kind, owner_id, actor, filename, content_type,
                        declared_sha256, declared_size, chunk_size, part_count,
                        storage_dir, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (sid, owner_kind, owner_id, actor, filename, content_type,
                     declared_sha256, int(declared_size), int(chunk_size),
                     int(part_count), storage_dir, now, now))
        except sqlite3.IntegrityError:
            # Two declares for the same bytes raced. The idempotency index did its
            # job — return the session that won rather than surfacing a 500 for
            # what is, from the partner's side, one upload they asked for twice.
            existing = self.find_open_upload_session(
                owner_kind=owner_kind, owner_id=owner_id, actor=actor,
                declared_sha256=declared_sha256, declared_size=int(declared_size))
            if existing:
                return existing
            raise
        return self.get_upload_session(sid)  # type: ignore[return-value]

    def get_upload_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM ingest_upload_sessions WHERE session_id = ?",
                (session_id,)).fetchone()
        return dict(row) if row else None

    def find_open_upload_session(
        self, *, owner_kind: str, owner_id: str, actor: Optional[str],
        declared_sha256: str, declared_size: int,
    ) -> Optional[Dict[str, Any]]:
        """The idempotency lookup. Matches an OPEN session first (a resume), then a
        VERIFIED one (a duplicate declare of bytes already ingested — returning the
        existing upload_id is both idempotent and the only answer that does not
        ingest the same content twice).

        Scoped to the ACCOUNT, not just the health system. See the note on
        ``idx_ingest_sessions_idem_v2``: purpose lives on the account, so matching
        on the organization alone would hand a brokering account a task-creation
        account's session and let the completed upload inherit its purpose."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM ingest_upload_sessions "
                "WHERE owner_kind = ? AND owner_id = ? AND actor IS ? "
                "AND declared_sha256 = ? "
                "AND declared_size = ? AND (status IS NULL OR status = 'verified') "
                "ORDER BY CASE WHEN status IS NULL THEN 0 ELSE 1 END, created_at DESC "
                "LIMIT 1",
                (owner_kind, owner_id, actor, declared_sha256,
                 int(declared_size))).fetchone()
        return dict(row) if row else None

    def claim_upload_session_for_completion(self, session_id: str) -> bool:
        """ATOMIC claim on the assembly step. True for exactly one caller.

        Two concurrent ``complete`` calls on one session would otherwise both pass
        the "is it still open" read and both assemble: two ``ingest_uploads`` rows
        and two pipeline runs for the same bytes, and — because the winner's
        ``finalize`` deletes the parts while the loser is still reading them — an
        unhandled FileNotFoundError from inside the loser. A conditional UPDATE
        settles it in one statement, the same pattern ``consume_upload_link``
        already uses for the one-time link race."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE ingest_upload_sessions SET status = 'completing', "
                "updated_at = ? WHERE session_id = ? AND status IS NULL",
                (_utcnow_iso(), session_id))
            return cur.rowcount == 1

    def release_upload_session_claim(self, session_id: str) -> str:
        """Hand a claimed session back after a failed assembly, so the partner can
        retry rather than being locked out by our own crash.

        Returns ``"open"`` or ``"aborted"``. **Never raises** — this is called from
        inside an ``except`` block, so an exception here would replace the real
        assembly failure with a database error the operator cannot act on.

        The subtlety: the idempotency index is partial over ``status IS NULL``, and
        a partner who gives up on a stuck assembly re-declares — producing a fresh
        OPEN session for the same bytes. Setting the stuck one back to NULL then
        collides with the live one. When that happens the stuck session is
        genuinely obsolete (its replacement already exists and is being uploaded
        to), so it is retired instead. Blindly retrying the NULL write is what let
        the reaper abort the live retry and leave the stuck one behind."""
        now = _utcnow_iso()
        try:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE ingest_upload_sessions SET status = NULL, updated_at = ? "
                    "WHERE session_id = ? AND status = 'completing'", (now, session_id))
            return "open"
        except sqlite3.IntegrityError:
            pass
        try:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE ingest_upload_sessions SET status = 'aborted', "
                    "updated_at = ? WHERE session_id = ? AND status = 'completing'",
                    (now, session_id))
        except sqlite3.Error:  # pragma: no cover - defensive; never mask the caller
            return "open"
        return "aborted"

    def update_upload_session(self, session_id: str, **fields: Any) -> None:
        allowed = {"status", "upload_id", "verified_at"}
        # 'completing' is a claim, not a terminal state — release_upload_session_claim
        # is the only way back to NULL.
        sets, params = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k} = ?")
                params.append(v)
        if not sets:
            return
        sets.append("updated_at = ?")
        params.extend([_utcnow_iso(), session_id])
        with self._conn() as conn:
            conn.execute(
                f"UPDATE ingest_upload_sessions SET {', '.join(sets)} "
                "WHERE session_id = ?", tuple(params))

    def list_stale_upload_sessions(self, *, older_than_iso: str) -> List[Dict[str, Any]]:
        """Sessions past the reaper cutoff (PRD-I §1.1: unverified parts are deleted
        after 24 h).

        Four states, three reasons:

        * ``NULL`` — open and possibly abandoned; parts are deleted if idle.
        * ``completing`` — a claim that outlived the process that took it. A hard
          crash during assembly would otherwise lock a partner out of an upload
          they could still finish, so the reaper hands these back.
        * ``aborted`` / ``failed`` — already retired, but their parts may still be
          on disk if whichever path retired them did not purge. Included so part
          cleanup CONVERGES rather than depending on every caller remembering;
          the reaper only releases their disk, never touches the row.

        ``verified`` is never a candidate: its parts are gone and its row is
        chain-of-custody history."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM ingest_upload_sessions "
                "WHERE (status IS NULL OR status IN ('completing', 'aborted', 'failed')) "
                "AND updated_at < ?",
                (older_than_iso,)).fetchall()
        return [dict(r) for r in rows]

    def hs_uploads_bytes_in_open_sessions(self, hs_id: str) -> int:
        """Bytes already committed to open sessions for this health system. Counted
        against the quota at DECLARE time — otherwise a partner could declare
        unlimited concurrent multi-GB sessions and only trip the quota at complete,
        after the disk was already spent."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(declared_size), 0) FROM ingest_upload_sessions "
                "WHERE owner_kind = 'health_system' AND owner_id = ? AND status IS NULL",
                (hs_id,)).fetchone()
        return int(row[0] or 0)
    # ═══ END PRD-I STORE METHODS ═══


# ─── PRD-I §F3: database durability ───────────────────────────────────────────
def _db_storage_durable() -> tuple:
    """(ok, detail) — will the SQLite database survive a redeploy, and can we
    actually write to it? (PRD I-0 §F3)

    Every durability check that existed covered BLOBS. Nothing checked the database,
    which is the one loss that is unrecoverable in kind rather than degree: losing
    image blobs degrades cases, losing ``asclepius.db`` destroys every user, task,
    submission, review and payout record at once.

    Two questions, because they fail differently. Ephemerality is a configuration
    mistake you can see. Writability is a mount that ATTACHED WRONG — a read-only
    volume, or a failed attach leaving a bare directory where the mount should be —
    which looks completely healthy until the first write. So we probe it."""
    from asclepius.constants import path_is_ephemeral

    db_path = os.getenv("ASCLEPIUS_DB_PATH") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "asclepius.db")
    db_dir = os.path.dirname(os.path.abspath(db_path)) or "/"
    if path_is_ephemeral(db_dir):
        return False, (
            f"database directory {db_dir} is on ephemeral storage; a redeploy "
            "destroys every user, task, submission and payout row. Set "
            "ASCLEPIUS_DB_PATH to a path on your persistent volume.")
    if not os.getenv("ASCLEPIUS_DB_PATH", "").strip():
        return False, (
            f"ASCLEPIUS_DB_PATH is not set, so the database lives beside the code "
            f"at {db_path} and is replaced on every redeploy. Set it to a path on "
            "your persistent volume.")
    try:
        os.makedirs(db_dir, exist_ok=True)
        probe = os.path.join(db_dir, f".durability-probe-{os.getpid()}")
        with open(probe, "w") as fh:
            fh.write("ok")
        os.remove(probe)
    except OSError as exc:
        return False, f"database directory {db_dir} is not writable: {exc}"
    return True, f"database directory {db_dir} is durable and writable"


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
